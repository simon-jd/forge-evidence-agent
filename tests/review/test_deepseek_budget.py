"""Independent accounting regressions; no provider, Docker or database calls."""

import subprocess
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from forge.context.budget import BudgetManager
from forge.domain.types import (
    BudgetLimits,
    ModelSpec,
    RepoProfile,
    RunConfig,
    TokenUsage,
    canonical_hash,
)
from forge.evals import deepseek_campaign
from forge.evals.campaign import CampaignLedger, CampaignPlan, CampaignRefused, CampaignSlot
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
    model = description["smoke_models"][0]
    first_slot = description.get("smoke_slot_ids", {}).get(model, "smoke-" + model)
    assert state["slots"][first_slot]["state"] == "uncertain"
    assert state["slots"][first_slot]["result"]["cost_status"] == "unknown"
    assert Decimal(state["committed_upper_or_unknown_usd"]) == Decimal("0.20")
    assert all(row["state"] == "pending" for key, row in state["slots"].items() if key != first_slot)


def amendment_fixture(tmp_path: Path) -> tuple[CampaignLedger, CampaignPlan]:
    original = CampaignPlan(campaign_id="review-amendment", authorization_usd=Decimal("10"),
        dataset_hash="frozen-dataset", slots={
            "original-flash": CampaignSlot(config_hash="16k-flash", limit_usd=Decimal(".20"), kind="smoke"),
            "pro": CampaignSlot(config_hash="16k-pro", limit_usd=Decimal(".20"), kind="smoke"),
            "heldout": CampaignSlot(config_hash="16k-heldout", limit_usd=Decimal(".12"), kind="evaluation"),
        })
    ledger = CampaignLedger(tmp_path / "ledger.json", original)
    ledger.claim("original-flash", "16k-flash")
    ledger.attach_run("original-flash", "failed-original-run")
    ledger.settle("original-flash", Decimal(".001526112"), "upper_bound", evidence="failed-context-budget.json")
    proposed = original.model_copy(deep=True)
    proposed.slots["pro"].config_hash = "32k-pro"
    proposed.slots["heldout"].config_hash = "32k-heldout"
    proposed.slots["new-flash"] = CampaignSlot(config_hash="32k-flash", limit_usd=Decimal(".20"), kind="smoke")
    return ledger, proposed


def test_amendment_preserves_failed_development_run_and_audits_original_plan(tmp_path: Path) -> None:
    ledger, proposed = amendment_fixture(tmp_path)
    original = ledger.snapshot()
    ledger.amend_plan(proposed, "Development smoke exceeded context capacity before heldout began")
    restored = CampaignLedger(ledger.path, proposed).snapshot()
    assert restored["slots"]["original-flash"] == original["slots"]["original-flash"]
    assert restored["slots"]["new-flash"]["state"] == "pending"
    assert restored["plan"]["slots"]["original-flash"] == original["plan"]["slots"]["original-flash"]
    revision = restored["plan_revisions"][-1]
    assert revision["previous_plan"] == original["plan"]
    assert revision["previous_plan_hash"] == original["plan_hash"]
    assert revision["new_plan_hash"] == canonical_hash(proposed)
    assert revision["reason"]
    with pytest.raises(CampaignRefused):
        ledger.claim("original-flash", "16k-flash")


@pytest.mark.parametrize("mutation", [
    "authorization", "dataset", "remove", "finished_config", "pending_limit", "pending_kind", "new_evaluation",
])
def test_amendment_cannot_rewrite_prior_authorization_or_trial_identity(tmp_path: Path, mutation: str) -> None:
    ledger, proposed = amendment_fixture(tmp_path)
    before = ledger.path.read_bytes()
    if mutation == "authorization":
        proposed.authorization_usd = Decimal("11")
    elif mutation == "dataset":
        proposed.dataset_hash = "different-dataset"
    elif mutation == "remove":
        del proposed.slots["pro"]
    elif mutation == "finished_config":
        proposed.slots["original-flash"].config_hash = "rewritten"
    elif mutation == "pending_limit":
        proposed.slots["heldout"].limit_usd = Decimal(".13")
    elif mutation == "pending_kind":
        proposed.slots["heldout"].kind = "smoke"
    else:
        proposed.slots["additional-heldout"] = CampaignSlot(config_hash="new", limit_usd=Decimal(".12"), kind="evaluation")
    with pytest.raises(CampaignRefused):
        ledger.amend_plan(proposed, "This mutation must be rejected")
    assert ledger.path.read_bytes() == before


@pytest.mark.parametrize("state", ["claimed", "uncertain", "finished"])
def test_any_started_heldout_trial_forbids_development_amendment(tmp_path: Path, state: str) -> None:
    ledger, proposed = amendment_fixture(tmp_path)
    ledger.claim("heldout", "16k-heldout")
    if state == "uncertain":
        ledger.settle("heldout", None, "unknown")
    elif state == "finished":
        ledger.settle("heldout", Decimal(0), "exact")
    before = ledger.path.read_bytes()
    with pytest.raises(CampaignRefused):
        ledger.amend_plan(proposed, "Heldout has already started")
    assert ledger.path.read_bytes() == before


@pytest.mark.parametrize("state", ["claimed", "uncertain"])
def test_development_amendment_cannot_forget_an_inflight_smoke(tmp_path: Path, state: str) -> None:
    ledger, proposed = amendment_fixture(tmp_path)
    ledger.claim("pro", "16k-pro")
    if state == "uncertain":
        ledger.settle("pro", None, "unknown")
    before = ledger.path.read_bytes()
    with pytest.raises(CampaignRefused):
        ledger.amend_plan(proposed, "In-flight smoke must first be reconciled")
    assert ledger.path.read_bytes() == before


def test_stale_amendment_handle_and_noop_cannot_create_second_revision(tmp_path: Path) -> None:
    ledger, proposed = amendment_fixture(tmp_path)
    stale = CampaignLedger(ledger.path, ledger.plan)
    ledger.amend_plan(proposed, "Approved development configuration change")
    before = ledger.path.read_bytes()
    with pytest.raises(CampaignRefused):
        stale.amend_plan(proposed, "Stale operator retry")
    with pytest.raises(CampaignRefused):
        ledger.amend_plan(proposed, "No-op must not be another authorization")
    assert ledger.path.read_bytes() == before
