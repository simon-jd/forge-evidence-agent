"""Independent accounting regressions; no provider, Docker or database calls."""

import subprocess
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from forge.context.budget import BudgetManager
from forge.domain.types import BudgetLimits, ModelSpec, RepoProfile, RunConfig, TokenUsage
from forge.evals import deepseek_campaign
from forge.evals.campaign import CampaignLedger, CampaignRefused
from forge.evals.contracts import ExperimentCondition, PublicTask, PublicTrialRequest
from forge.evals.worker import ServiceRunCallback
from forge.settings import Settings


@pytest.mark.asyncio
@pytest.mark.parametrize("settled_usage", [False, True], ids=["no-usage", "earlier-attempt-settled"])
async def test_nonterminal_run_does_not_release_campaign_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settled_usage: bool,
) -> None:
    """A recovery pause is not final proof of zero further provider charges."""
    config = RunConfig(
        models=[ModelSpec(provider="deepseek", model_id="deepseek-flash",
                          input_usd_per_million=Decimal("0.30"),
                          output_usd_per_million=Decimal("1.20"), price_basis="upper_bound")],
        budget=BudgetLimits(max_cost_usd=Decimal("0.12")),
    )
    request = PublicTrialRequest(
        trial_id="review-nonterminal-B0-0", repository=str(tmp_path),
        task=PublicTask(task_id="review-nonterminal", base_commit="0" * 40,
                        instruction="Repair the frozen task", constraints=[], allowed_patch_paths=["src.py"]),
        profile=RepoProfile(name="review", image="unused", validation_argv=["true"]),
        condition=ExperimentCondition(name="B0", config=config), repeat=0, seed=1729,
    )
    budget = BudgetManager(config.budget)
    if settled_usage:
        # The first request finished billing, but later steps of its run have
        # not. Complete earlier token counters must not finalize the run bill.
        budget.reserve("earlier-attempt", config.models[0], 10_000, 2_000)
        budget.reconcile("earlier-attempt", TokenUsage(input_tokens=10_000, output_tokens=1_000))
        assert budget.cost_status == "upper_bound"
    run = {"id": "review-run", "status": "needs_reconciliation",
           "state": {"budget": budget.state.model_dump(mode="json")},
           "updated_at": 1, "created_at": 0, "verification": {}}
    repository = SimpleNamespace(list_artifacts=AsyncMock(return_value=[]),
                                 list_events=AsyncMock(return_value=[]))
    settings = Settings(data_dir=tmp_path / "data", config_dir=tmp_path / "config")
    callback = ServiceRunCallback(settings, repository, "review", AsyncMock())
    monkeypatch.setattr(callback, "_configuration", lambda _: ("unused", "unused"))
    callback.service = SimpleNamespace(register_project=AsyncMock(return_value={"id": "project"}),
                                       submit_run=AsyncMock(return_value=run))

    outcome = await callback(request)

    assert outcome.status == "infrastructure_failure"
    assert outcome.cost_status == "unknown", outcome.model_dump(mode="json")
    assert outcome.usage.estimated is True
    assert outcome.cost_usd == budget.known_spend_usd
    assert outcome.reserved_unknown_usd == config.budget.max_cost_usd - budget.known_spend_usd
    assert (outcome.cost_usd or Decimal(0)) + outcome.reserved_unknown_usd == config.budget.max_cost_usd
    if settled_usage:
        assert outcome.cost_usd == Decimal("0.0042")
        assert outcome.usage.input_tokens == 10_000
        assert outcome.usage.output_tokens == 1_000


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "nonzero_exit"])
async def test_failed_smoke_process_keeps_its_allocation_and_stops_campaign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    """Exercise the orchestration error path without a paid HTTP request."""
    plan, description, smokes, conditions = deepseek_campaign.load_plan()
    ledger = CampaignLedger(tmp_path / "ledger.json", plan)
    settings = Settings(DEEPSEEK_API_KEY="offline-contract-key")
    monkeypatch.setattr(deepseek_campaign, "Settings", lambda: settings)
    monkeypatch.setattr(deepseek_campaign, "DockerExecutor", lambda *args, **kwargs:
                        SimpleNamespace(_client=lambda: None, _docker=None))
    monkeypatch.setattr(deepseek_campaign, "freeze_local_image", lambda *args: "sha256:" + "1" * 64)
    monkeypatch.setattr(deepseek_campaign, "PrivateSpecStore", lambda *args: SimpleNamespace(load=lambda task: None))
    monkeypatch.setattr(deepseek_campaign, "provision_database", lambda settings, *args: settings)

    class ModelPreflight:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            assert url == "https://api.deepseek.com/models"
            return SimpleNamespace(status_code=200, json=lambda: {
                "data": [{"id": model} for model in description["smoke_models"]],
            })

    monkeypatch.setattr(deepseek_campaign.httpx, "AsyncClient", lambda **kwargs: ModelPreflight())
    launched = []

    def failed_process(argv, **kwargs):
        launched.append(argv)
        assert "--live-config" in argv
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 700)
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(deepseek_campaign.subprocess, "run", failed_process)
    with pytest.raises(CampaignRefused, match="smoke"):
        await deepseek_campaign.execute(plan, description, smokes, conditions, ledger,
                                        tmp_path, image="not-used")

    assert len(launched) == 1
    state = CampaignLedger(ledger.path, plan).snapshot()
    first_slot = "smoke-" + description["smoke_models"][0]
    assert state["slots"][first_slot]["state"] == "uncertain"
    assert state["slots"][first_slot]["result"]["cost_status"] == "unknown"
    assert Decimal(state["committed_upper_or_unknown_usd"]) == Decimal("0.20")
    assert all(row["state"] == "pending" for key, row in state["slots"].items() if key != first_slot)
