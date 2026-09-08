"""Independent reviewer regressions. Failures describe observed product defects.

No real provider requests are made. The optional Docker test uses a dedicated
review workspace and explicitly identifies rootful development verification.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from anthropic import AsyncAnthropic

from forge.api.app import create_app
from forge.artifacts import LocalArtifactStore
from forge.context.budget import BudgetManager
from forge.domain.types import (
    BudgetLimits,
    CanonicalMessage,
    ModelRequest,
    ModelSpec,
    ProviderError,
    RepoProfile,
    RunConfig,
    TokenUsage,
)
from forge.evals.contracts import EvalTask, PrivateGraderSpec
from forge.evals.grader import DockerGrader
from forge.execution.docker import DockerExecutor
from forge.execution.workspace import WorkspaceManager
from forge.persistence import Repository
from forge.providers import AnthropicProvider
from forge.runtime.engine import Runtime
from forge.runtime.verification import CandidateVerifier
from forge.settings import Settings
from forge.worker import Worker


async def test_interrupted_anthropic_usage_does_not_release_unknown_output_budget() -> None:
    """A real SDK parses message_start + text, then EOF before terminal usage."""
    events = [
        {"type": "message_start", "message": {
            "id": "review-msg", "type": "message", "role": "assistant", "content": [],
            "model": "review-model", "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 1},
        }},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "billable text " * 100}},
    ]
    encoded = b"".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode() for event in events)
    transport = httpx2.MockTransport(lambda request: httpx2.Response(
        200, content=encoded, headers={"content-type": "text/event-stream"},
    ))
    client = AsyncAnthropic(api_key="review-fixture-only", base_url="https://review.invalid",
                            http_client=httpx2.AsyncClient(transport=transport), max_retries=0)
    spec = ModelSpec(provider="anthropic", model_id="review-model",
                     input_usd_per_million=Decimal(1), output_usd_per_million=Decimal(1),
                     price_version="review-fixture")
    manager = BudgetManager(BudgetLimits(max_cost_usd=Decimal("0.0011")))
    attempts: list[dict[str, Any]] = []

    async def sink(payload: dict[str, Any]) -> None:
        attempts.append(payload)
        if payload["status"] == "started":
            manager.reserve(payload["attempt_id"], spec, 10, 1000)
        else:
            manager.reconcile(payload["attempt_id"], TokenUsage.model_validate(payload["usage"]))

    provider = AnthropicProvider(client=client, attempt_sink=sink)
    try:
        output = [event async for event in provider.stream(ModelRequest(
            run_id="review-run", turn_id="review-turn", model_id="review-model",
            canonical_messages=[CanonicalMessage.text("user", "Describe the bug")], output_limit=1000,
        ))]
    finally:
        await provider.aclose()
    assert isinstance(output[-1], ProviderError)
    assert output[-1].code == "stream_interrupted"
    assert attempts[-1]["status"] == "failed"
    reservation = next(iter(manager.state.reservations.values()))
    assert reservation.status != "settled", (
        "Only initial output_tokens=1 was observed, but failed stream settled at "
        f"${reservation.charged_usd}, releasing ${reservation.reserved_usd - reservation.charged_usd}"
    )
    assert not manager.can_afford(spec, 10, 1000)


def _source(root: Path) -> tuple[Path, str]:
    root.mkdir()
    (root / "maths.py").write_text("def add(a, b):\n    return a - b\n")
    (root / "test_public.py").write_text(
        "import unittest\nfrom maths import add\n"
        "class Acceptance(unittest.TestCase):\n"
        "    def test_positive(self):\n        self.assertEqual(add(2, 3), 5)\n",
    )
    for argv in (["git", "init", "-q"], ["git", "add", "."], ["git", "-c", "user.name=Forge Review",
                 "-c", "user.email=review@example.invalid", "commit", "-qm", "Independent review fixture"]):
        subprocess.run(argv, cwd=root, check=True, capture_output=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    return root, commit


@pytest.mark.docker
async def test_zero_exit_before_acceptance_executes_cannot_be_verified(tmp_path: Path) -> None:
    if os.environ.get("FORGE_REVIEW_DOCKER") != "1":
        pytest.skip("Set FORGE_REVIEW_DOCKER=1 for the isolated reviewer Docker reproduction")
    source, commit = _source(tmp_path / "source")
    workspaces = WorkspaceManager(tmp_path / "workspaces")
    candidate = workspaces.prepare("review-exit-zero", source, commit)
    candidate.fence(1)
    # The protected public test is unchanged. Only the allowed source file is
    # edited, and the defect is still present after these lines.
    candidate.files.write("maths.py", b"import os\nos._exit(0)\ndef add(a, b):\n    return a - b\n")
    candidate.refresh_revision()
    profile = RepoProfile(name="review-public-acceptance", image="forge-runner:dev",
                          validation_argv=["python", "-m", "unittest", "discover"],
                          target_test_argv=["python", "-m", "unittest", "test_public.Acceptance"],
                          allowed_patch_paths=["maths.py"], protected_paths=["test_public.py"],
                          command_timeout_seconds=15)
    executor = DockerExecutor(os.environ.get("FORGE_REVIEW_DOCKER_HOST"),
                              allow_rootful=os.environ.get("FORGE_REVIEW_ROOTFUL") == "1")
    result = await CandidateVerifier(workspaces, executor, LocalArtifactStore(tmp_path / "artifacts")).verify(
        run_id="review-exit-zero", source=source, base_commit=commit, candidate=candidate,
        profile=profile, epoch=1,
    )
    assert result.status != "verified", (
        "Both commands exited 0 before loading any test, but CandidateVerifier returned verified: "
        + result.summary
    )


def _tampering_source(source: Path) -> tuple[str, str]:
    source.mkdir()
    (source / "tests").mkdir()
    original = "def add(a, b):\n    return a - b\n"
    (source / "maths.py").write_text(original)
    (source / "tests/test_public.py").write_text(
        "import unittest\nfrom maths import add\n"
        "class Regression(unittest.TestCase):\n"
        "    def test_negative(self):\n        self.assertEqual(add(-2, -3), -5)\n",
    )
    for argv in (["git", "init", "-q"], ["git", "add", "."], ["git", "-c", "user.name=Forge Review",
                 "-c", "user.email=review@example.invalid", "commit", "-qm", "Independent review grader fixture"]):
        subprocess.run(argv, cwd=source, check=True, capture_output=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    replacement_test = "import unittest\nclass Regression(unittest.TestCase):\n    def test_empty(self):\n        pass\n"
    changed = (
        "from pathlib import Path\n"
        f"Path('/workspace/tests/test_public.py').write_text({replacement_test!r})\n"
        "def add(a, b):\n    return 5\n"
    )
    patch = "".join(difflib.unified_diff(original.splitlines(keepends=True), changed.splitlines(keepends=True),
                                        fromfile="a/maths.py", tofile="b/maths.py"))
    return commit, patch


@pytest.mark.docker
async def test_grader_rejects_protected_test_mutation_during_import(tmp_path: Path) -> None:
    if os.environ.get("FORGE_REVIEW_DOCKER") != "1":
        pytest.skip("Set FORGE_REVIEW_DOCKER=1 for the isolated reviewer Docker reproduction")
    source = tmp_path / "source"
    commit, patch = await asyncio.to_thread(_tampering_source, source)
    task = EvalTask(task_id="review-grader-tamper", split="dev", family="review", fixture_dir="review",
                    base_commit=commit, file_hashes={}, instruction="Fix addition",
                    allowed_patch_paths=["maths.py"], private_spec_sha256="0" * 64)
    spec = PrivateGraderSpec(task_id=task.task_id, target_test_count=1, reference_patch="", author_note="review only",
                             target_source=("import unittest\nfrom maths import add\n"
                                            "class Target(unittest.TestCase):\n"
                                            "    def test_positive(self):\n        self.assertEqual(add(2, 3), 5)\n"))
    profile = RepoProfile(name="review-grader", image="forge-runner:dev",
                          validation_argv=["python", "-m", "unittest", "discover"], command_timeout_seconds=15)
    executor = DockerExecutor(os.environ.get("FORGE_REVIEW_DOCKER_HOST"),
                              allow_rootful=os.environ.get("FORGE_REVIEW_ROOTFUL") == "1")
    result = await DockerGrader(tmp_path / "graders", executor).grade(task, source, patch, spec, profile, "review-grader-tamper")
    assert result.status != "verified", (
        "Candidate import rewrote protected public regression tests; original negative case is still broken: "
        + result.model_dump_json()
    )


@pytest.mark.docker
async def test_verifier_artifact_list_entry_is_downloadable_for_run_owner(tmp_path: Path) -> None:
    if os.environ.get("FORGE_REVIEW_DOCKER") != "1":
        pytest.skip("Set FORGE_REVIEW_DOCKER=1 for the isolated reviewer Docker reproduction")
    source, commit = _source(tmp_path / "source")
    profile = RepoProfile(name="review-artifacts", image="forge-runner:dev",
                          validation_argv=["python", "-m", "unittest", "discover"],
                          target_test_argv=["python", "-m", "unittest", "test_public.Acceptance"],
                          allowed_patch_paths=["maths.py"], protected_paths=["test_public.py"],
                          command_timeout_seconds=15)
    repo = Repository(f"sqlite+aiosqlite:///{tmp_path / 'review.db'}")
    await repo.create_schema_for_tests()
    try:
        project = await repo.create_project("review-owner", "review", str(source), profile.model_dump(mode="json"))
        run = await repo.create_run(owner="review-owner", project_id=project["id"],
                                    idempotency_key="review", request_hash="hash", task="review",
                                    base_commit=commit, config=RunConfig().model_dump(mode="json"),
                                    profile=profile.model_dump(mode="json"), manifest={})
        claimed = await repo.claim("review-worker", 120)
        assert claimed is not None
        artifacts = LocalArtifactStore(tmp_path / "artifacts")
        workspaces = WorkspaceManager(tmp_path / "workspaces")
        workspace = workspaces.prepare(run["id"], source, commit)
        workspace.fence(claimed["lease_epoch"])
        workspace.files.write("maths.py", b"def add(a, b):\n    return a + b\n")
        workspace.refresh_revision()
        executor = DockerExecutor(os.environ.get("FORGE_REVIEW_DOCKER_HOST"),
                                  allow_rootful=os.environ.get("FORGE_REVIEW_ROOTFUL") == "1")
        runtime = Runtime(repo, workspaces, executor, artifacts,
                          epoch=claimed["lease_epoch"])
        verified = await runtime.verifier.verify(run_id=run["id"], source=source, base_commit=commit,
                                                 candidate=workspace, profile=profile, epoch=claimed["lease_epoch"])
        assert verified.status == "verified"
        await runtime._save(run["id"], {}, "context", refs=verified.artifact_refs)
        settings = Settings(data_dir=tmp_path, api_token="review-token", owner_id="review-owner")
        app = create_app(settings, repo)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://review.invalid",
                                     headers={"Authorization": "Bearer review-token"}) as client:
            listing = await client.get(f"/v1/runs/{run['id']}/artifacts")
            assert listing.status_code == 200
            assert any(item["kind"] == "applied_patch" for item in listing.json())
            for item in listing.json():
                response = await client.get(f"/v1/artifacts/{item['id']}")
                assert response.status_code == 200, response.text
    finally:
        await repo.close()


async def test_worker_observer_enforces_total_runtime_while_node_is_running(tmp_path: Path) -> None:
    config = RunConfig()
    config.budget.max_runtime_seconds = 1
    manager = BudgetManager(config.budget)
    manager.state.started_at = datetime.now(UTC) - timedelta(seconds=10)
    run = {"id": "review-runtime", "lease_epoch": 1, "status": "running", "cancel_requested": False,
           "config": config.model_dump(mode="json"), "state": {"budget": manager.state.model_dump(mode="json")}}

    class ReviewRepository:
        async def get_run(self, run_id: str) -> dict[str, Any]:
            return run

        async def heartbeat(self, *args: Any) -> None:
            pass

    class ReviewRuntime:
        async def _cancel(self, value: dict[str, Any], **kwargs: Any) -> None:
            pass

        async def _stop(self, *args: Any, **kwargs: Any) -> None:
            pass

    worker = object.__new__(Worker)
    worker.settings = Settings(data_dir=tmp_path, lease_seconds=6)
    worker.repository = ReviewRepository()
    task = asyncio.create_task(asyncio.sleep(1.2))
    try:
        await worker._observe(task, run, ReviewRuntime())
        assert task.cancelled(), "The node continued to completion after the run's total wall-clock budget expired"
    finally:
        task.cancel()
