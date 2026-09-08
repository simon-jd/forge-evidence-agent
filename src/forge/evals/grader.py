from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from pathlib import Path
from typing import Any

from unidiff import PatchSet
from unidiff.errors import UnidiffParseError

from forge.domain.types import RepoProfile, canonical_hash
from forge.evals.contracts import EvalTask, GradeResult, PrivateGraderSpec
from forge.execution.docker import DockerExecutor
from forge.execution.errors import NeedsReconciliation, SandboxUnavailable, UnsafePath
from forge.execution.workspace import WorkspaceManager
from forge.tools.patching import prepare_patch

GRADER_RUNNER = '''import json, sys, unittest
sys.path.insert(0, "/workspace")
sys.path.insert(0, "/workspace/.forge_grader")
import target_test
loader = unittest.TestLoader()
target = loader.loadTestsFromModule(target_test)
regression = loader.discover("/workspace/tests", pattern="test_public.py")
target_result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(target)
regression_result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(regression)
def stats(result):
    return dict(tests_run=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                skipped=len(result.skipped), unexpected_successes=len(result.unexpectedSuccesses),
                expected_failures=len(result.expectedFailures))
print("FORGE_GRADE_JSON=" + json.dumps(dict(target=stats(target_result), regression=stats(regression_result))))
'''


def sealed_snapshot(root: Path) -> dict[str, str]:
    """Hash all grader inputs, including hidden directories and validation files.

    Unlike the editable-workspace index, this deliberately has no cache or
    hidden-directory exclusions. No process may mutate an input during grading.
    """
    snapshot: dict[str, str] = {}
    total = 0
    for directory, directories, names in os.walk(root, followlinks=False):
        for name in directories:
            if (Path(directory) / name).is_symlink():
                raise ValueError("grader input directory became a symlink")
        for name in names:
            path = Path(directory) / name
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError("grader input is not a regular file")
            if len(snapshot) >= 20_000 or path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("grader input exceeds snapshot limits")
            data = path.read_bytes()
            total += len(data)
            if total > 128 * 1024 * 1024:
                raise ValueError("grader input exceeds snapshot limits")
            snapshot[path.relative_to(root).as_posix()] = hashlib.sha256(data).hexdigest()
    return snapshot


class DockerGrader:
    """Apply candidates to a new checkout and run evaluator-owned target tests.

    The injected Executor always runs a container; there is no host execution
    fallback. Private tests exist only in this separate evaluator workspace.
    This boundary protects against accidental test contamination, not an
    adversarial Python program that attacks the interpreter running its tests.
    """

    def __init__(self, workspace_root: Path, executor: DockerExecutor) -> None:
        self.workspaces = WorkspaceManager(workspace_root)
        self.executor = executor

    async def grade(
        self, task: EvalTask, repository: Path, patch: str, spec: PrivateGraderSpec,
        profile: RepoProfile, trial_id: str, *, allow_empty: bool = False,
    ) -> GradeResult:
        started = time.monotonic()
        run_id = "grade-" + hashlib.sha256(trial_id.encode()).hexdigest()[:32]
        workspace = self.workspaces.prepare(run_id, repository, task.base_commit)
        workspace.fence(1)
        strict_profile = profile.model_copy(update={
            "allowed_patch_paths": task.allowed_patch_paths,
            "protected_paths": [".git/**", ".forge/**", ".forge_grader/**", "tests/**",
                                "conftest.py", "sitecustomize.py", "usercustomize.py", "pytest.ini", "pyproject.toml"],
        })
        try:
            if patch:
                parsed = PatchSet(patch.splitlines(keepends=True))
                expected: dict[str, str | None] = {}
                for item in parsed:
                    path = item.target_file[2:] if item.target_file != "/dev/null" else item.source_file[2:]
                    if path not in task.allowed_patch_paths:
                        return GradeResult(status="failed", category="protected_path", sandbox="not_started",
                                           message=f"candidate changes a path outside this task's source allowlist: {path}")
                    expected[path] = workspace.files.digest(path)
                changes = prepare_patch(workspace.files, patch, expected, strict_profile)
                for change in changes:
                    if change.after is None:
                        workspace.files.remove(change.path)
                    else:
                        workspace.files.write(change.path, change.after)
            elif not allow_empty:
                return GradeResult(status="failed", category="invalid_patch", sandbox="not_started",
                                   message="candidate contains no patch")
        except (ValueError, UnsafePath, UnidiffParseError) as error:
            return GradeResult(status="failed", category="invalid_patch", sandbox="not_started", message=str(error))

        # Neither these files nor their references appear in PublicTrialRequest.
        workspace.files.write(".forge_grader/target_test.py", spec.target_source.encode())
        workspace.files.write(".forge_grader/run.py", GRADER_RUNNER.encode())
        workspace.refresh_revision()
        sealed = sealed_snapshot(workspace.path)
        try:
            receipt = await self.executor.execute(
                workspace, ["python", "-I", "-B", "/workspace/.forge_grader/run.py"],
                profile.command_timeout_seconds, "verify", strict_profile, 1,
                read_only_workspace=True,
            )
        except (SandboxUnavailable, NeedsReconciliation, OSError) as error:
            return GradeResult(status="infrastructure_failure", category="infrastructure",
                               sandbox="unavailable", message=str(error), grader_workspace=str(workspace.path),
                               duration_seconds=time.monotonic() - started)
        sandbox = "rootless_docker" if receipt.rootless else "rootful_docker_dev"
        base: dict[str, Any] = {
            "sandbox": sandbox, "image_id": receipt.image_id, "grader_workspace": str(workspace.path),
            "artifact_refs": [receipt.stdout_path, receipt.stderr_path],
            "duration_seconds": time.monotonic() - started,
            "snapshot_before": canonical_hash(sealed),
        }
        try:
            after = sealed_snapshot(workspace.path)
        except (OSError, ValueError):
            return GradeResult(status="failed", category="protected_path",
                               message="grader inputs became unreadable or unsafe during validation", **base)
        base["snapshot_after"] = canonical_hash(after)
        if sealed != after:
            changed = sorted(path for path in set(sealed) | set(after) if sealed.get(path) != after.get(path))
            return GradeResult(status="failed", category="protected_path", tampered_paths=changed,
                               message="candidate changed sealed grader inputs during validation", **base)
        if receipt.status in {"timeout", "cancelled"}:
            return GradeResult(status="failed", category="target_failure", message="grader timed out or was cancelled", **base)
        rows = [row.removeprefix("FORGE_GRADE_JSON=") for row in receipt.stdout.splitlines()
                if row.startswith("FORGE_GRADE_JSON=")]
        if receipt.exit_code != 0 or len(rows) != 1:
            return GradeResult(status="failed", category="target_failure", message="grader did not produce one complete result", **base)
        try:
            counts = json.loads(rows[0])
            target, regression = counts["target"], counts["regression"]
            forbidden = ("failures", "errors", "skipped", "unexpected_successes", "expected_failures")
            target_ok = target["tests_run"] == spec.target_test_count and not any(target[key] for key in forbidden)
            regression_ok = regression["tests_run"] >= 1 and not any(regression[key] for key in forbidden)
            return GradeResult(
                status="verified" if target_ok and regression_ok else "failed",
                category="none" if target_ok and regression_ok else
                         "target_failure" if not target_ok else "regression_failure",
                target_passed=target_ok, regression_passed=regression_ok,
                target_tests_run=target["tests_run"], regression_tests_run=regression["tests_run"], **base,
            )
        except (ValueError, KeyError, TypeError):
            return GradeResult(status="failed", category="target_failure", message="invalid grader result schema", **base)
