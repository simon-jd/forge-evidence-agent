"""Actual pytest completion through CandidateVerifier and isolated Docker jobs.

Requires an explicit Docker endpoint and prebuilt runner image. No database,
provider calls, host test execution, or public demo process is involved.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from forge.artifacts import LocalArtifactStore
from forge.domain.types import RepoProfile, ValidationResult
from forge.execution.docker import DockerExecutor
from forge.execution.workspace import Workspace, WorkspaceManager
from forge.runtime.verification import CandidateVerifier

pytestmark = pytest.mark.docker

ORIGINAL = "def resume(events, after):\n    return [event for event in events if event[0] > after[0]]\n"
FIXED = "def resume(events, after):\n    return [event for event in events if event > after]\n"
TESTS = """import sys
import pytest
from replay import resume

# Confirm the real trusted image and interpreter used by the completion probe.
assert pytest.__version__ == "8.4.2"
assert sys.flags.isolated == 1

def test_empty():
    assert resume([], (10, 1)) == []

def test_later_timestamp():
    assert resume([(10, 1), (11, 0)], (10, 1)) == [(11, 0)]

def test_same_timestamp():
    assert resume([(10, 1), (10, 2), (11, 0)], (10, 1)) == [(10, 2), (11, 0)]
"""
TARGET = "tests/test_replay.py::test_same_timestamp"
NODES = {f"tests/test_replay.py::{name}" for name in (
    "test_empty", "test_later_timestamp", "test_same_timestamp",
)}
Case = tuple[CandidateVerifier, Workspace, Path, str, RepoProfile]


@pytest.fixture
def verifier_case(tmp_path: Path) -> Iterator[Case]:
    host = (os.environ.get("FORGE_TEST_DOCKER_HOST") or os.environ.get("FORGE_DOCKER_HOST")
            or os.environ.get("DOCKER_HOST"))
    image = os.environ.get("FORGE_TEST_IMAGE")
    if not host or not image:
        pytest.skip("Set FORGE_TEST_DOCKER_HOST and FORGE_TEST_IMAGE for actual Docker verification")
    executor = DockerExecutor(host, allow_rootful=os.environ.get("FORGE_ALLOW_ROOTFUL_TESTS") == "1")
    client = executor._client()  # Explicit but unusable daemon must fail, not skip.
    run_id = "pytest-verifier-" + uuid.uuid4().hex
    try:
        source = tmp_path / "source"
        (source / "tests").mkdir(parents=True)
        (source / "replay.py").write_text(ORIGINAL)
        (source / "tests/test_replay.py").write_text(TESTS)
        for argv in (["git", "init", "-q"], ["git", "add", "."],
                     ["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=Forge test fixture",
                      "-c", "user.email=fixture@example.invalid", "commit", "-qm", "Public cursor test fixture"]):
            subprocess.run(argv, cwd=source, check=True, capture_output=True)
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
        workspaces = WorkspaceManager(tmp_path / "workspaces")
        candidate = workspaces.prepare(run_id, source, commit)
        candidate.fence(1)
        profile = RepoProfile(
            name="pytest-completion", image=image,
            validation_argv=["python", "-I", "-m", "pytest", "-q", "tests/test_replay.py"],
            target_test_argv=["python", "-I", "-m", "pytest", "-q", TARGET],
            allowed_patch_paths=["replay.py"], protected_paths=["tests/**"], command_timeout_seconds=20,
        )
        yield CandidateVerifier(workspaces, executor, LocalArtifactStore(tmp_path / "artifacts")), candidate, source, commit, profile
    finally:
        # Verification jobs append a patch identity to this unguessable run ID.
        # Inspecting labels is read-only; only this fixture's containers are removed.
        for container in client.containers.list(all=True, filters={"label": "forge.managed=true"}):
            owner = container.labels.get("forge.run_id", "")
            if owner.startswith(run_id + "-v-"):
                container.remove(force=True)
        client.close()


async def verify(case: Case, *, baseline: bool = False) -> ValidationResult:
    verifier, candidate, source, commit, profile = case
    result = await verifier.verify(run_id=candidate.run_id, source=source, base_commit=commit,
                                   candidate=candidate, profile=profile, epoch=1, baseline=baseline)
    receipts = [json.loads(verifier.artifacts.read_text(ref)) for ref in result.artifact_refs
                if verifier.artifacts.metadata(ref)["kind"].endswith("receipt")]
    assert len(receipts) == 2
    for receipt in receipts:
        assert receipt["rootless"] or os.environ.get("FORGE_ALLOW_ROOTFUL_TESTS") == "1"
        assert receipt["limits"]["read_only_workspace"] is True
        assert receipt["image_id"].startswith("sha256:")
    assert result.checks["workspace_unchanged"]
    assert (source / "replay.py").read_text() == ORIGINAL
    assert (source / "tests/test_replay.py").read_text() == TESTS
    return result


@pytest.mark.parametrize("launcher", [["python", "-I", "-m", "pytest"], ["pytest"]],
                         ids=["python-isolated-module", "pytest-console"])
async def test_pytest_baseline_fails_then_patch_completes_real_tests(
    verifier_case: Case, launcher: list[str],
) -> None:
    verifier, candidate, source, commit, profile = verifier_case
    profile = profile.model_copy(update={"validation_argv": [*launcher, "-q", "tests/test_replay.py"],
                                        "target_test_argv": [*launcher, "-q", TARGET]})
    case = verifier, candidate, source, commit, profile
    baseline = await verify(case, baseline=True)
    assert baseline.status == "unverified" and baseline.baseline_exit_code == baseline.target_exit_code == 1
    assert baseline.checks["target"]["record"]["failures"] == 1
    candidate.files.write("replay.py", FIXED.encode())
    candidate.refresh_revision()
    final = await verify(case)
    assert final.status == "verified" and final.final_exit_code == final.target_exit_code == 0
    for name, nodes in (("regression", NODES), ("target", {TARGET})):
        check = final.checks[name]
        assert check["supported"] and check["complete"] and check["passed"]
        assert check["record"]["framework"] == "pytest"
        assert set(check["record"]["collected"]) == set(check["record"]["executed"]) == nodes
        assert check["record"]["skipped"] == check["record"]["failures"] == check["record"]["errors"] == 0


async def test_pytest_all_skipped_is_zero_exit_but_unverified(verifier_case: Case) -> None:
    candidate = verifier_case[1]
    candidate.files.write("replay.py", b"def resume(events, after):\n    import pytest\n    pytest.skip('No implementation')\n")
    candidate.refresh_revision()
    result = await verify(verifier_case)
    assert result.final_exit_code == result.target_exit_code == 0
    assert result.status == "unverified"
    for name, count in (("regression", 3), ("target", 1)):
        check = result.checks[name]
        assert check["supported"] and check["complete"] and not check["passed"]
        assert check["record"]["skipped"] == len(check["record"]["collected"]) == count


async def test_pytest_early_zero_exit_has_no_completion_and_is_unverified(verifier_case: Case) -> None:
    candidate = verifier_case[1]
    candidate.files.write("replay.py", b"import os\nos._exit(0)\n" + ORIGINAL.encode())
    candidate.refresh_revision()
    result = await verify(verifier_case)
    assert result.final_exit_code == result.target_exit_code == 0
    assert result.status == "unverified"
    for name in ("regression", "target"):
        check = result.checks[name]
        assert check["supported"] and not check["complete"] and not check["passed"]
        assert check["reason"] == "Missing or ambiguous test-runner completion record"
