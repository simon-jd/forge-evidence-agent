"""Real PostgreSQL claim/recovery semantics with an explicitly injected offline callback."""

import asyncio
import os
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from forge.domain.types import ModelSpec, RepoProfile, RunConfig
from forge.evals.contracts import AgentTrialOutcome
from forge.evals.worker import EvaluationWorker
from forge.persistence.models import EvalTrialRow, EvaluationRow
from forge.persistence.repository import Repository
from forge.settings import Settings

pytestmark = pytest.mark.postgres


async def setup(tmp_path: Path, *, status: str = "queued"):
    url = os.environ.get("FORGE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set FORGE_TEST_DATABASE_URL to a dedicated migrated PostgreSQL database")
    repository = Repository(url)
    owner = "eval-test-" + uuid.uuid4().hex
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    settings = Settings(database_url=url, owner_id=owner, enable_evaluations=True,
                        eval_private_dir=private, data_dir=tmp_path / "data",
                        config_dir=tmp_path / "configs", allowed_repo_roots=[tmp_path])
    config = RunConfig(models=[
        ModelSpec(provider="fake", model_id=name, input_usd_per_million=Decimal(0), output_usd_per_million=Decimal(0))
        for name in ["cheap", "strong"]
    ])
    identifier = uuid.uuid4().hex
    async with repository.sessions() as session, session.begin():
        session.add(EvaluationRow(id=identifier, owner_id=owner, status=status, created_at=await repository.now(session),
                                 config={"experiment_id": "contract", "dataset_id": "forge-diagnostics-v1",
                                         "run_config": config.model_dump(mode="json"), "task_ids": ["window-admission"],
                                         "conditions": ["B0"], "repeats": 2, "budget_usd": "1",
                                         "profile": RepoProfile(name="contract", image="not-used", validation_argv=["true"]).model_dump(mode="json")},
                                 results={"active_run_id": "previous-run"} if status == "running" else {}))
    return repository, settings, identifier


@pytest.mark.asyncio
async def test_only_one_eval_worker_claims_and_persists_trial_matrix(tmp_path: Path) -> None:
    repository, settings, identifier = await setup(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    def factory(evaluation_id, progress):
        async def callback(request):
            entered.set()
            await release.wait()
            return AgentTrialOutcome(execution_kind="fake_control_flow", status="failed", failure_category="agent_failure",
                                     cost_usd=Decimal(0), cost_status="exact")
        return callback

    first = EvaluationWorker(settings, repository, callback_factory=factory)
    second = EvaluationWorker(settings, repository, callback_factory=factory)
    pending = asyncio.create_task(first.once())
    try:
        await asyncio.wait_for(entered.wait(), 10)
        assert await second.once() is False
        release.set()
        assert await asyncio.wait_for(pending, 10)
        async with repository.sessions() as session:
            row = await session.get(EvaluationRow, identifier)
            assert row.status == "completed"
            trials = list(await session.scalars(select(EvalTrialRow).where(EvalTrialRow.evaluation_id == identifier)))
            assert len(trials) == 2
            assert all(trial.result["execution_kind"] == "fake_control_flow" for trial in trials)
            assert row.results["summary"]["conditions"]["B0"]["verified_trials"] == 0
    finally:
        release.set()
        await pending
        await repository.close()


@pytest.mark.asyncio
async def test_abandoned_experiment_requires_reconciliation_without_replaying(tmp_path: Path) -> None:
    repository, settings, identifier = await setup(tmp_path, status="running")
    called = False

    def factory(evaluation_id, progress):
        nonlocal called
        called = True
        raise AssertionError("abandoned trials must not replay")

    try:
        assert await EvaluationWorker(settings, repository, callback_factory=factory).once()
        assert not called
        async with repository.sessions() as session:
            row = await session.get(EvaluationRow, identifier)
            assert row.status == "needs_reconciliation"
            assert row.results["active_run_id"] == "previous-run"
            trials = list(await session.scalars(select(EvalTrialRow).where(EvalTrialRow.evaluation_id == identifier)))
            assert not trials
    finally:
        await repository.close()
