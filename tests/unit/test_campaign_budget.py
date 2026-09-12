import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from forge.context.budget import BudgetManager
from forge.domain.types import BudgetLimits, ModelSpec, TokenUsage, canonical_hash
from forge.evals.campaign import CampaignLedger, CampaignPlan, CampaignRefused, CampaignSlot
from forge.evals.contracts import EvalTrial
from forge.evals.deepseek_campaign import load_plan
from forge.evals.report import summarize, write_report
from forge.settings import Settings


def plan() -> CampaignPlan:
    return CampaignPlan(campaign_id="test", authorization_usd=Decimal("1"), dataset_hash="frozen",
        slots={"smoke": CampaignSlot(config_hash="a", limit_usd=Decimal(".2"), kind="smoke"),
               "trial": CampaignSlot(config_hash="b", limit_usd=Decimal(".7"), kind="evaluation")})


def test_unknown_smoke_cost_survives_restart_and_never_allows_replay(tmp_path: Path) -> None:
    ledger = CampaignLedger(tmp_path / "ledger.json", plan())
    ledger.claim("smoke", "a")
    ledger.settle("smoke", None, "unknown")
    restored = CampaignLedger(ledger.path, plan())
    assert restored.snapshot()["committed_upper_or_unknown_usd"] == "0.2"
    with pytest.raises(CampaignRefused, match="already claimed"):
        restored.claim("smoke", "a")
    restored.attach_run("smoke", "original-run")
    restored.settle("smoke", Decimal(".05"), "upper_bound")
    assert restored.snapshot()["committed_upper_or_unknown_usd"] == "0.05"
    assert restored.snapshot()["slots"]["smoke"]["reconciliations"][0]["cost_status"] == "unknown"
    with pytest.raises(CampaignRefused):
        restored.claim("smoke", "a")


def test_claim_before_run_id_loss_keeps_whole_allocation(tmp_path: Path) -> None:
    ledger = CampaignLedger(tmp_path / "ledger.json", plan())
    ledger.claim("smoke", "a")
    restored = CampaignLedger(ledger.path, plan())
    assert restored.snapshot()["slots"]["smoke"]["state"] == "claimed"
    assert restored.snapshot()["committed_upper_or_unknown_usd"] == "0.2"
    with pytest.raises(CampaignRefused):
        restored.claim("smoke", "a")


def test_concurrent_ledger_instances_claim_one_paid_slot_once(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    CampaignLedger(path, plan())

    def attempt(_: int) -> bool:
        try:
            CampaignLedger(path, plan()).claim("smoke", "a")
            return True
        except CampaignRefused:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(16))) == 1
    assert CampaignLedger(path, plan()).snapshot()["committed_upper_or_unknown_usd"] == "0.2"


def test_overrun_halts_campaign_without_clamping_its_evidence(tmp_path: Path) -> None:
    ledger = CampaignLedger(tmp_path / "ledger.json", plan())
    ledger.claim("smoke", "a")
    ledger.settle("smoke", Decimal(".21"), "upper_bound")
    assert ledger.snapshot()["halted"] is True
    assert ledger.snapshot()["committed_upper_or_unknown_usd"] == "0.21"
    with pytest.raises(CampaignRefused, match="halted"):
        ledger.claim("trial", "b")


def test_changed_plan_unplanned_call_and_second_run_id_are_rejected(tmp_path: Path) -> None:
    ledger = CampaignLedger(tmp_path / "ledger.json", plan())
    changed = plan().model_copy(update={"authorization_usd": Decimal("10")})
    with pytest.raises(CampaignRefused, match="frozen plan"):
        CampaignLedger(ledger.path, changed)
    for slot, fingerprint in [("extra", "a"), ("smoke", "changed")]:
        with pytest.raises(CampaignRefused, match="Unplanned"):
            ledger.claim(slot, fingerprint)
    ledger.claim("smoke", "a")
    ledger.attach_run("smoke", "first")
    with pytest.raises(CampaignRefused, match="original paid run"):
        ledger.attach_run("smoke", "second")


@pytest.mark.parametrize("amount", [Decimal("NaN"), Decimal("Infinity"), Decimal("-.01")])
def test_invalid_reconciliation_cannot_release_budget(tmp_path: Path, amount: Decimal) -> None:
    ledger = CampaignLedger(tmp_path / "ledger.json", plan())
    ledger.claim("smoke", "a")
    with pytest.raises(ValueError):
        ledger.settle("smoke", amount, "exact")
    assert ledger.snapshot()["committed_upper_or_unknown_usd"] == "0.2"


def test_campaign_is_fixed_before_measurement_within_ten_dollars() -> None:
    frozen, description, smokes, conditions = load_plan()
    assert len(frozen.slots) == 76 and len(description["task_ids"]) == 12
    assert len(conditions) == 6 and len(smokes) == 2
    assert sum(slot.limit_usd for slot in frozen.slots.values()) == Decimal("9.44")
    assert frozen.authorization_usd == Decimal("10")
    for config in [*smokes.values(), *(condition.config for condition in conditions)]:
        assert config.sampling_config == {"reasoning": {"effort": "none"}}
        assert config.context.continuation_mode == "portable"
        assert config.context.input_budget == 32000
        assert all(model.price_basis == "upper_bound" for model in config.models)
    assert canonical_hash(frozen) == canonical_hash(load_plan()[0])


def test_complete_usage_at_ceiling_price_is_not_invoice_exact() -> None:
    model = ModelSpec(provider="deepseek", model_id="fixture", input_usd_per_million=Decimal("1"),
        output_usd_per_million=Decimal("2"), price_basis="upper_bound")
    budget = BudgetManager(BudgetLimits(max_cost_usd=Decimal("1")))
    budget.reserve("attempt", model, 1000, 1000)
    assert budget.cost_status == "unknown"
    budget.reconcile("attempt", TokenUsage(input_tokens=100, output_tokens=50))
    assert budget.state.reservations["attempt"].status == "settled"
    assert budget.cost_status == "upper_bound"
    assert budget.known_spend_usd == Decimal(".0002")


def test_upper_bound_report_exposes_ceiling_without_exact_cost(tmp_path: Path) -> None:
    # Reuse a public, redacted row shape; this test changes its accounting only.
    data = json.loads(Path("evals/reports/qualification/trials.jsonl").read_text().splitlines()[1])
    data.update(cost_status="upper_bound", cost_usd=".10", success_within_budget=True)
    data["outcome"].update(cost_status="upper_bound", cost_usd=".10")
    trial = EvalTrial.model_validate(data)
    condition = summarize([trial])["conditions"][trial.condition]
    assert condition["cost_exact_usd"] is None and condition["cost_per_verified_usd"] is None
    assert Decimal(condition["cost_upper_bound_usd"]) == Decimal(".10")
    assert condition["upper_bound_cost_trials"] == 1 and condition["unknown_cost_trials"] == 0
    paths = write_report([trial], tmp_path)
    assert "Upper-bound $ / verified" in paths["html"].read_text()


def test_deepseek_dotenv_secret_is_excluded_from_serialization(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("FORGE_DEEPSEEK_API_KEY", raising=False)
    Path(".env").write_text("DEEPSEEK_API_KEY=unit-test-only\n")
    settings = Settings()
    assert settings.deepseek_api_key.get_secret_value() == "unit-test-only"
    assert "unit-test-only" not in settings.model_dump_json()
    assert "unit-test-only" not in repr(settings)


def amended_plan() -> CampaignPlan:
    candidate = plan()
    candidate.slots["trial"].config_hash = "larger-context"
    candidate.slots["smoke-retry"] = CampaignSlot(
        config_hash="larger-context-smoke", limit_usd=Decimal(".05"), kind="smoke",
    )
    return candidate


def test_amendment_preserves_failed_smoke_accounting_and_audits_original_plan(tmp_path: Path) -> None:
    ledger = CampaignLedger(tmp_path / "ledger.json", plan())
    ledger.claim("smoke", "a")
    ledger.attach_run("smoke", "original-context-failure")
    ledger.settle("smoke", Decimal(".001526112"), "upper_bound", evidence="first-failure.json")
    before = ledger.snapshot()
    candidate = amended_plan()
    ledger.amend_plan(candidate, "  Increase context after development smoke exhausted its limit.  ")
    after = ledger.snapshot()
    assert ledger.plan == candidate
    assert after["plan_hash"] == canonical_hash(candidate)
    assert after["slots"]["smoke"] == before["slots"]["smoke"]
    assert after["slots"]["trial"] == before["slots"]["trial"]
    assert after["slots"]["smoke-retry"] == {"state": "pending", "reserved_usd": "0"}
    assert after["committed_upper_or_unknown_usd"] == before["committed_upper_or_unknown_usd"]
    assert Decimal(after["unallocated_margin_usd"]) == Decimal(".05")
    assert sum(slot.limit_usd for slot in ledger.plan.slots.values()) == Decimal(".95")
    revision = after["plan_revisions"][0]
    assert revision["revision"] == 1
    assert revision["previous_plan"] == before["plan"]
    assert revision["previous_plan_hash"] == before["plan_hash"]
    assert revision["new_plan_hash"] == after["plan_hash"]
    assert revision["reason"] == "Increase context after development smoke exhausted its limit."
    assert datetime.fromisoformat(revision["amended_at"]).tzinfo is not None
    assert CampaignLedger(ledger.path, candidate).snapshot() == after
    with pytest.raises(CampaignRefused):
        ledger.claim("smoke", "a")


def test_amendment_retains_revision_chain_and_rejects_duplicate_or_stale_handles(tmp_path: Path) -> None:
    ledger = CampaignLedger(tmp_path / "ledger.json", plan())
    stale = CampaignLedger(ledger.path, plan())
    first = amended_plan()
    ledger.amend_plan(first, "First development adjustment")
    persisted = ledger.path.read_bytes()
    with pytest.raises(CampaignRefused, match="no change"):
        ledger.amend_plan(first, "Repeated submission")
    with pytest.raises(CampaignRefused, match="frozen plan"):
        stale.amend_plan(first, "A stale process must not overwrite the amendment")
    with pytest.raises(CampaignRefused):
        stale.claim("trial", "b")
    assert ledger.path.read_bytes() == persisted
    second = first.model_copy(deep=True)
    second.slots["trial"].config_hash = "final-development-configuration"
    ledger.amend_plan(second, "Freeze the configuration before any heldout trial")
    history = ledger.snapshot()["plan_revisions"]
    assert [item["revision"] for item in history] == [1, 2]
    assert history[0]["new_plan_hash"] == history[1]["previous_plan_hash"]
    assert history[1]["previous_plan"] == first.model_dump(mode="json")
    assert history[1]["new_plan_hash"] == canonical_hash(second)


def test_concurrent_amendment_instances_cannot_overwrite_each_other(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    ledgers = [CampaignLedger(path, plan()) for _ in range(8)]

    def attempt(index: int) -> bool:
        candidate = amended_plan()
        candidate.slots["trial"].config_hash = f"candidate-{index}"
        try:
            ledgers[index].amend_plan(candidate, f"Development candidate {index}")
            return True
        except CampaignRefused:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(8))) == 1
    data = json.loads(path.read_text())
    assert len(data["plan_revisions"]) == 1
    assert data["plan_revisions"][0]["new_plan_hash"] == data["plan_hash"]
    assert all(row == {"state": "pending", "reserved_usd": "0"} for row in data["slots"].values())


@pytest.mark.parametrize("state", ["claimed", "unknown", "estimated"])
def test_amendment_requires_reconciling_all_active_or_uncertain_runs(tmp_path: Path, state: str) -> None:
    ledger = CampaignLedger(tmp_path / "ledger.json", plan())
    ledger.claim("smoke", "a")
    if state != "claimed":
        ledger.settle("smoke", None if state == "unknown" else Decimal(".01"), state)
    before = ledger.path.read_bytes()
    with pytest.raises(CampaignRefused, match="claimed or uncertain"):
        ledger.amend_plan(amended_plan(), "Cannot release an unresolved charge")
    assert ledger.path.read_bytes() == before


def test_amendment_is_forbidden_after_a_completed_evaluation(tmp_path: Path) -> None:
    ledger = CampaignLedger(tmp_path / "ledger.json", plan())
    ledger.claim("trial", "b")
    ledger.settle("trial", Decimal(".01"), "upper_bound", evidence="heldout-started.json")
    before = ledger.path.read_bytes()
    with pytest.raises(CampaignRefused, match="evaluation has started"):
        ledger.amend_plan(amended_plan(), "Heldout observations cannot guide a revision")
    assert ledger.path.read_bytes() == before


@pytest.mark.parametrize("change", ["campaign", "dataset", "authorization", "delete",
                                    "finished_config", "pending_limit", "pending_kind",
                                    "new_evaluation", "over_allocate"])
def test_amendment_cannot_rewrite_identity_allocations_or_paid_definitions(tmp_path: Path, change: str) -> None:
    ledger = CampaignLedger(tmp_path / "ledger.json", plan())
    ledger.claim("smoke", "a")
    ledger.settle("smoke", Decimal(".01"), "upper_bound", evidence="failure-preserved.json")
    candidate = amended_plan()
    if change == "campaign":
        candidate.campaign_id = "different"
    elif change == "dataset":
        candidate.dataset_hash = "different"
    elif change == "authorization":
        candidate.authorization_usd = Decimal("10")
    elif change == "delete":
        del candidate.slots["smoke"]
    elif change == "finished_config":
        candidate.slots["smoke"].config_hash = "erase-the-failed-configuration"
    elif change == "pending_limit":
        candidate.slots["trial"].limit_usd = Decimal(".6")
    elif change == "pending_kind":
        candidate.slots["trial"].kind = "smoke"
    elif change == "new_evaluation":
        candidate.slots["smoke-retry"].kind = "evaluation"
    else:
        # This fits if finished actual spend is substituted for its original
        # allocation. The fixed-plan total correctly still refuses the increase.
        candidate.slots["smoke-retry"].limit_usd = Decimal(".2")
    before = ledger.path.read_bytes()
    with pytest.raises(CampaignRefused):
        ledger.amend_plan(candidate, "Invalid amendment must be atomic")
    assert ledger.path.read_bytes() == before
    assert ledger.plan == plan()


@pytest.mark.parametrize("reason", ["", " \n\t"])
def test_amendment_requires_an_audit_reason(tmp_path: Path, reason: str) -> None:
    ledger = CampaignLedger(tmp_path / "ledger.json", plan())
    with pytest.raises(CampaignRefused, match="audit reason"):
        ledger.amend_plan(amended_plan(), reason)


def test_amendment_does_not_rearm_a_halted_campaign(tmp_path: Path) -> None:
    ledger = CampaignLedger(tmp_path / "ledger.json", plan())
    ledger.claim("smoke", "a")
    ledger.settle("smoke", Decimal(".21"), "upper_bound")
    before = ledger.path.read_bytes()
    with pytest.raises(CampaignRefused, match="halted"):
        ledger.amend_plan(amended_plan(), "An amendment cannot erase a previous cost overrun")
    assert ledger.path.read_bytes() == before


def test_amendment_write_failure_preserves_current_handle_and_persisted_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = CampaignLedger(tmp_path / "ledger.json", plan())
    before = ledger.path.read_bytes()

    def write_failure(data: dict) -> None:
        raise OSError("Synthetic failure before atomic replacement")

    monkeypatch.setattr(ledger, "_write", write_failure)
    with pytest.raises(OSError, match="Synthetic failure"):
        ledger.amend_plan(amended_plan(), "A failed write cannot advance the in-memory revision")
    assert ledger.plan == plan()
    assert ledger.path.read_bytes() == before
    assert "plan_revisions" not in ledger.snapshot()
