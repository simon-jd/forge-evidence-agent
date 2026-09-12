import json
from concurrent.futures import ThreadPoolExecutor
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
    assert len(frozen.slots) == 74 and len(description["task_ids"]) == 12
    assert len(conditions) == 6 and len(smokes) == 2
    assert sum(slot.limit_usd for slot in frozen.slots.values()) == Decimal("9.04")
    assert frozen.authorization_usd == Decimal("10")
    for config in [*smokes.values(), *(condition.config for condition in conditions)]:
        assert config.sampling_config == {"reasoning": {"effort": "none"}}
        assert config.context.continuation_mode == "portable"
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
