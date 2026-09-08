from __future__ import annotations

from pathlib import Path
from typing import Literal

from forge.artifacts import LocalArtifactStore
from forge.domain.types import RepoProfile, ToolCall, ValidationResult, canonical_hash
from forge.execution.docker import DockerExecutor
from forge.execution.workspace import Workspace, WorkspaceManager
from forge.runtime.test_runner import RUNNER, completion, instrument
from forge.tools import ToolDispatcher


class CandidateVerifier:
    """Run trusted validation in a separate clean, content-addressed workspace.

    Hidden benchmark tests belong to the evaluator, never to this runtime
    verifier. Here a trusted profile can pin public acceptance tests. Regression
    success alone is expressly insufficient to return ``verified``.
    """
    def __init__(self, workspaces: WorkspaceManager, executor: DockerExecutor,
                 artifacts: LocalArtifactStore) -> None:
        self.workspaces, self.executor, self.artifacts = workspaces, executor, artifacts

    async def verify(self, *, run_id: str, source: Path, base_commit: str,
                     candidate: Workspace, profile: RepoProfile, epoch: int,
                     baseline: bool = False) -> ValidationResult:
        patch = "" if baseline else candidate.get_diff()
        identity = canonical_hash({"patch": patch, "profile": profile.model_dump(mode="json"),
                                   "baseline": baseline, "base_commit": base_commit, "runner_hash": canonical_hash(RUNNER)})
        workspace = self.workspaces.prepare(f"{run_id}-v-{identity[:16]}", source, base_commit)
        workspace.fence(epoch)
        dispatcher = ToolDispatcher(workspace, profile, self.executor, self.artifacts)
        refs: list[str] = []
        if patch:
            before = workspace.metadata["snapshot"]
            # prepare_patch requires a hash entry for every changed path. Get
            # paths by parsing, rather than trusting model claimed_changes.
            from unidiff import PatchSet
            paths = [item.path for item in PatchSet(patch)]
            call = ToolCall(call_id="verify-apply", name="apply_patch", arguments={
                "patch": patch, "expected_file_hashes": {path: before.get(path) for path in paths}})
            applied = await dispatcher.execute(call, "verify-apply", epoch)
            # Helper workspace artifacts are copied into the parent run's
            # ownership boundary; the API never relaxes owner integrity checks.
            refs.extend(self.artifacts.put_text(run_id, "applied_patch", self.artifacts.read_text(ref))
                        for ref in applied.artifact_refs)
            if applied.status != "ok":
                return ValidationResult(status="unverified", summary="Independent patch application rejected: " + applied.summary,
                                        artifact_refs=refs)
        snapshot = canonical_hash(workspace.snapshot())
        regression_nonce = canonical_hash([identity, "regression"])
        regression_argv, regression_framework = instrument(profile.validation_argv, regression_nonce)
        regression = await self.executor.execute(workspace, regression_argv,
            profile.command_timeout_seconds, "verify-regression", profile, epoch, read_only_workspace=True)
        regression_check = completion(regression, regression_framework, regression_nonce)
        refs.append(self.artifacts.put_text(run_id, "baseline_receipt" if baseline else "validation_receipt",
                                            regression.model_dump_json()))
        target_exit: int | None = None
        target_check: dict = {}
        if profile.target_test_argv:
            target_nonce = canonical_hash([identity, "target"])
            target_argv, target_framework = instrument(profile.target_test_argv, target_nonce)
            target = await self.executor.execute(workspace, target_argv,
                profile.command_timeout_seconds, "verify-target", profile, epoch, read_only_workspace=True)
            target_check = completion(target, target_framework, target_nonce)
            refs.append(self.artifacts.put_text(run_id, "baseline_target_receipt" if baseline else "target_receipt",
                                                target.model_dump_json()))
            target_exit = target.exit_code if target.status == "ok" else target.exit_code or 1
        clean = canonical_hash(workspace.snapshot()) == snapshot
        passed = regression.status == "ok" and regression.exit_code == 0 and clean
        proven = passed and regression_check["passed"] and target_check.get("passed", False)
        status: Literal["verified", "regression_only", "unverified"] = ("verified" if proven else
                  "regression_only" if passed and target_exit is None else "unverified")
        return ValidationResult(
            checks={"regression": regression_check, "target": target_check, "workspace_unchanged": clean},
            status=status, baseline_exit_code=regression.exit_code if baseline else None,
            final_exit_code=None if baseline else regression.exit_code, target_exit_code=target_exit,
            summary=(f"regression={regression.exit_code}; target={target_exit}; "
                     f"validation_workspace_unchanged={clean}; "
                     f"regression_completion={regression_check.get('complete')}; target_completion={target_check.get('complete')}\n"
                     + regression.stdout[-8000:] + regression.stderr[-4000:]),
            artifact_refs=refs,
        )
