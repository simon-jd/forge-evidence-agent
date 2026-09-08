"""Independent evaluator tests need the external private store, never a public answer key."""

import difflib
import hashlib
import os
import uuid
from pathlib import Path

import pytest

from forge.domain.types import RepoProfile
from forge.evals.contracts import EvalTask, PrivateGraderSpec
from forge.evals.dataset import Dataset, PrivateSpecStore, fixture_git
from forge.evals.grader import DockerGrader
from forge.execution.docker import DockerExecutor

pytestmark = pytest.mark.docker


@pytest.mark.asyncio
async def test_real_grader_rejects_baseline_and_accepts_private_reference(tmp_path: Path) -> None:
    private = os.environ.get("FORGE_EVAL_PRIVATE_ROOT")
    image = os.environ.get("FORGE_TEST_IMAGE")
    if not private or not image:
        pytest.skip("Set FORGE_EVAL_PRIVATE_ROOT and FORGE_TEST_IMAGE for private independent grading")
    dataset = Dataset(Path("evals"))
    store = PrivateSpecStore(Path(private))
    task = next(task for task in dataset.tasks if task.task_id == "explicit-overrides")
    source = dataset.materialize(task, tmp_path / "repository")
    spec = store.load(task)
    executor = DockerExecutor(docker_host=os.environ.get("FORGE_DOCKER_HOST"),
                              allow_rootful=os.environ.get("FORGE_ALLOW_ROOTFUL_TESTS") == "1")
    grader = DockerGrader(tmp_path / "private-workspaces", executor)
    profile = RepoProfile(name="independent-eval", image=image, validation_argv=["python", "-m", "unittest", "discover"],
                          command_timeout_seconds=20)
    # Receipts retain named containers across process restarts. Every independent
    # experiment needs a fresh identity; recovery reuses that identity.
    experiment = uuid.uuid4().hex
    baseline = await grader.grade(task, source, "", spec, profile, experiment + "-baseline", allow_empty=True)
    fixed = await grader.grade(task, source, spec.reference_patch, spec, profile, experiment + "-reference")
    assert baseline.status == "failed" and baseline.regression_passed and not baseline.target_passed, baseline.message
    assert fixed.status == "verified" and fixed.target_tests_run == 2
    assert baseline.grader_workspace != fixed.grader_workspace
    assert not (source / ".forge_grader").exists()
    assert (source / "profiles.py").read_text() == (dataset.root / task.fixture_dir / "profiles.py").read_text()


@pytest.mark.asyncio
async def test_import_time_regression_rewrite_is_blocked_by_read_only_mount(tmp_path: Path) -> None:
    image = os.environ.get("FORGE_TEST_IMAGE")
    if not image:
        pytest.skip("Set FORGE_TEST_IMAGE and a Docker daemon for the adversarial grader test")
    source = tmp_path / "repository"
    source.mkdir()
    original = "def value():\n    return 1\n"
    (source / "subject.py").write_text(original)
    (source / "tests").mkdir()
    public = "import unittest\nfrom subject import value\nclass Public(unittest.TestCase):\n    def test_regression(self): self.assertEqual(value(), 1)\n"
    (source / "tests/test_public.py").write_text(public)
    fixture_git(source, ["init", "--quiet", "--initial-branch=main", "--object-format=sha1"])
    fixture_git(source, ["add", "--all"])
    fixture_git(source, ["commit", "--quiet", "-m", "Adversarial grader fixture"])
    commit = fixture_git(source, ["rev-parse", "HEAD"])
    fake_test = "import unittest\nclass Public(unittest.TestCase):\n    def test_fake(self): self.assertTrue(True)\n"
    malicious = (
        "from pathlib import Path\n"
        f"try:\n    Path('tests/test_public.py').write_text({fake_test!r})\n"
        "except OSError:\n    pass\n"
        "def value():\n    return 2\n"
    )
    patch = "".join(difflib.unified_diff(original.splitlines(keepends=True), malicious.splitlines(keepends=True),
                                      fromfile="a/subject.py", tofile="b/subject.py"))
    task = EvalTask(task_id="adversarial-grader", split="dev", family="synthetic-attack", fixture_dir="unused",
                    base_commit=commit, file_hashes={"subject.py": hashlib.sha256(original.encode()).hexdigest()},
                    instruction="Test grading boundary", allowed_patch_paths=["subject.py"], private_spec_sha256="0" * 64)
    spec = PrivateGraderSpec(task_id=task.task_id, target_test_count=1, reference_patch="", author_note="synthetic attack",
                             target_source="import unittest\nfrom subject import value\nclass Target(unittest.TestCase):\n    def test_target(self): self.assertEqual(value(),2)\n")
    executor = DockerExecutor(docker_host=os.environ.get("FORGE_DOCKER_HOST"),
                              allow_rootful=os.environ.get("FORGE_ALLOW_ROOTFUL_TESTS") == "1")
    grader = DockerGrader(tmp_path / "grader", executor)
    result = await grader.grade(task, source, patch, spec,
                                RepoProfile(name="attack", image=image, validation_argv=["true"]), "attack-" + uuid.uuid4().hex)
    assert result.status == "failed" and result.category == "regression_failure"
    assert result.target_passed and not result.regression_passed
    assert result.snapshot_before == result.snapshot_after
    assert (source / "tests/test_public.py").read_text() == public
    assert result.grader_workspace is not None
    assert (Path(result.grader_workspace) / "tests/test_public.py").read_text() == public
