from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from forge.artifacts.store import LocalArtifactStore
from forge.context.budget import BudgetManager
from forge.domain.types import ModelSpec, RepoProfile, RunConfig, TokenUsage
from forge.evals.contracts import ExperimentCondition, PublicTask, PublicTrialRequest
from forge.evals.worker import ServiceRunCallback
from forge.settings import Settings


class CompletedRepository:
    def __init__(self, run, artifacts):
        self.run, self.records = run, artifacts

    async def list_artifacts(self, run_id, owner_id):
        return self.records

    async def list_events(self, run_id, owner_id, after, limit):
        return [{"seq": 1, "kind": "context.compacted"}]


class SubmittedService:
    def __init__(self, run):
        self.run = run
        self.submitted = None

    async def register_project(self, name, source, profile_id):
        return {"id": "project"}

    async def submit_run(self, **kwargs):
        self.submitted = kwargs
        return self.run


@pytest.mark.asyncio
async def test_callback_preserves_frozen_condition_and_treats_regression_only_as_candidate(tmp_path: Path) -> None:
    model = ModelSpec(provider="openai", model_id="contract-model-never-called",
                      input_usd_per_million=Decimal("1"), output_usd_per_million=Decimal("2"))
    config = RunConfig(models=[model])
    config.context.evidence_guard = False
    budget = BudgetManager(config.budget)
    budget.reserve("attempt", model, 1000, 100)
    budget.reconcile("attempt", TokenUsage(input_tokens=500, output_tokens=50))
    run = {"id": "run-id", "status": "failed", "created_at": 1, "updated_at": 3,
           "verification": {"status": "regression_only"},
           "state": {"budget": budget.state.model_dump(mode="json"), "stop_reason": "No pinned target"}}
    settings = Settings(data_dir=tmp_path / "data", config_dir=tmp_path / "configs")
    store = LocalArtifactStore(settings.data_dir / "artifacts")
    patch_ref = store.put_text("run-id", "patch", "candidate-patch")
    repository = CompletedRepository(run, [{"id": patch_ref, "kind": "patch"}])
    updates = []

    async def progress(update):
        updates.append(update)

    callback = ServiceRunCallback(settings, repository, "eval-id", progress)
    callback.service = SubmittedService(run)
    request = PublicTrialRequest(
        trial_id="trial-id", repository=str(tmp_path / "source"),
        task=PublicTask(task_id="task", base_commit="a" * 40, instruction="Repair behavior",
                        constraints=["Keep API"], allowed_patch_paths=["source.py"]),
        profile=RepoProfile(name="profile", image="sha256:" + "b" * 64,
                            validation_argv=["python", "-m", "unittest"]),
        condition=ExperimentCondition(name="B2_guard_off", config=config, evidence_guard=False),
        repeat=0, seed=1,
    )
    outcome = await callback(request)
    assert outcome.status == "candidate"
    assert outcome.patch == "candidate-patch"
    assert outcome.cost_usd == Decimal("0.0006")
    assert outcome.cost_status == "exact" and outcome.compactions == 1
    assert outcome.usage.input_tokens == 500 and outcome.usage.output_tokens == 50
    assert updates[-1]["active_run_id"] == "run-id"
    configs = list((settings.config_dir / "generated/eval-id/runs").glob("*.yaml"))
    assert len(configs) == 1
    generated = yaml.safe_load(configs[0].read_text())
    assert generated["run"]["context"]["evidence_guard"] is False
    assert "scripted_turns" not in generated
    assert callback.service.submitted["idempotency_key"] == "evaluation:trial-id"
