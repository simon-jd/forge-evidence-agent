import difflib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from forge.domain.types import RepoProfile
from forge.evals.contracts import PrivateGraderSpec
from forge.evals.dataset import Dataset
from forge.evals.grader import DockerGrader


class ReceiptExecutor:
    def __init__(self, *, skipped: int = 0, target_count: int = 2) -> None:
        self.skipped, self.target_count = skipped, target_count
        self.workspace = None

    async def execute(self, workspace, argv, timeout_seconds, operation_id, profile, epoch, **kwargs):
        self.workspace = workspace
        assert kwargs["read_only_workspace"] is True
        stats = {"tests_run": self.target_count, "failures": 0, "errors": 0, "skipped": self.skipped,
                 "unexpected_successes": 0, "expected_failures": 0}
        return SimpleNamespace(status="ok", exit_code=0, rootless=True, image_id="fixture-image-id",
                               stdout="FORGE_GRADE_JSON=" + json.dumps({"target": stats, "regression": {**stats, "tests_run": 1}}),
                               stdout_path="stdout-artifact", stderr_path="stderr-artifact")


@pytest.mark.asyncio
async def test_grader_uses_distinct_worktree_and_rejects_skip_or_missing_tests(tmp_path: Path) -> None:
    dataset = Dataset(Path("evals"))
    task = dataset.tasks[0]
    repo = dataset.materialize(task, tmp_path / "source")
    spec = PrivateGraderSpec(task_id=task.task_id, target_source="PRIVATE_TARGET_SENTINEL",
                             target_test_count=2, reference_patch="", author_note="contract")
    profile = RepoProfile(name="fixture", image="fixture", validation_argv=["true"])
    for label, skipped, count, status in [("complete", 0, 2, "verified"), ("skipped", 1, 2, "failed"), ("empty", 0, 0, "failed")]:
        executor = ReceiptExecutor(skipped=skipped, target_count=count)
        grader = DockerGrader(tmp_path / "graders", executor)
        result = await grader.grade(task, repo, "", spec, profile, label, allow_empty=True)
        assert result.status == status
        assert executor.workspace.path != repo
        assert (executor.workspace.path / ".forge_grader/target_test.py").read_text() == "PRIVATE_TARGET_SENTINEL"
        assert not (repo / ".forge_grader").exists()


@pytest.mark.asyncio
async def test_grader_rejects_patch_to_public_tests_even_if_it_would_pass(tmp_path: Path) -> None:
    dataset = Dataset(Path("evals"))
    task = dataset.tasks[0]
    repo = dataset.materialize(task, tmp_path / "source")
    original = (repo / "tests/test_public.py").read_text()
    patch = "".join(difflib.unified_diff(original.splitlines(keepends=True), ["# removed tests\n"],
                                      fromfile="a/tests/test_public.py", tofile="b/tests/test_public.py"))
    spec = PrivateGraderSpec(task_id=task.task_id, target_source="private", target_test_count=2, reference_patch="", author_note="contract")
    result = await DockerGrader(tmp_path / "graders", ReceiptExecutor()).grade(
        task, repo, patch, spec, RepoProfile(name="fixture", image="fixture", validation_argv=["true"]), "modified-tests",
    )
    assert result.status == "failed" and result.category == "protected_path"


@pytest.mark.asyncio
async def test_runtime_mutation_of_sealed_grader_inputs_cannot_be_verified(tmp_path: Path) -> None:
    dataset = Dataset(Path("evals"))
    task = dataset.tasks[0]
    repo = dataset.materialize(task, tmp_path / "source")
    spec = PrivateGraderSpec(task_id=task.task_id, target_source="private", target_test_count=2,
                             reference_patch="", author_note="contract")

    class TamperingExecutor(ReceiptExecutor):
        async def execute(self, workspace, *args, **kwargs):
            receipt = await super().execute(workspace, *args, **kwargs)
            workspace.files.write("tests/test_public.py", b"# validation overwritten at runtime\n")
            return receipt

    result = await DockerGrader(tmp_path / "graders", TamperingExecutor()).grade(
        task, repo, "", spec, RepoProfile(name="fixture", image="fixture", validation_argv=["true"]),
        "runtime-tampering", allow_empty=True,
    )
    assert result.status == "failed" and result.category == "protected_path"
    assert result.tampered_paths == ["tests/test_public.py"]
    assert result.snapshot_before != result.snapshot_after
