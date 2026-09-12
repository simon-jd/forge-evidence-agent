"""A paid smoke's settled bill cannot substitute for successful verification."""

from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from forge.domain.types import RunConfig, canonical_hash
from forge.evals.campaign import CampaignLedger, CampaignPlan, CampaignRefused, CampaignSlot
from forge.evals.deepseek_campaign import check_resume, validate_smoke


def verified(config: RunConfig) -> dict:
    return {"status": "completed", "verification": {"status": "verified"},
            "execution_mode": "live", "manifest": {"config_hash": canonical_hash(config)},
            "source_unchanged": True, "receipts": [{"rootless": True}], "run_id": "original"}


@pytest.mark.parametrize("change", [
    {"status": "budget_exhausted"}, {"verification": {"status": "regression_only"}},
    {"manifest": {"config_hash": "old-config"}}, {"execution_mode": "offline_scripted"},
    {"source_unchanged": False}, {"receipts": []}, {"receipts": [{"rootless": False}]},
    {"run_id": "replacement"},
])
def test_resume_requires_current_verified_smoke_not_merely_accounting(change: dict) -> None:
    config = RunConfig()
    evidence = verified(config)
    validate_smoke(evidence, config, "original")
    evidence.update(deepcopy(change))
    with pytest.raises(CampaignRefused, match="verification"):
        validate_smoke(evidence, config, "original")


def test_only_reconciled_development_can_resume(tmp_path: Path) -> None:
    plan = CampaignPlan(campaign_id="test", authorization_usd=Decimal("1"), dataset_hash="fixed",
        slots={"smoke": CampaignSlot(config_hash="s", limit_usd=Decimal(".2"), kind="smoke"),
               "trial": CampaignSlot(config_hash="t", limit_usd=Decimal(".2"), kind="evaluation")})
    ledger = CampaignLedger(tmp_path / "ledger.json", plan)
    ledger.claim("smoke", "s")
    with pytest.raises(CampaignRefused, match="reconciled"):
        check_resume(plan, ledger, True)
    ledger.settle("smoke", None, "unknown")
    with pytest.raises(CampaignRefused, match="reconciled"):
        check_resume(plan, ledger, True)
    ledger.settle("smoke", Decimal(".0015"), "upper_bound")
    with pytest.raises(CampaignRefused, match="prior progress"):
        check_resume(plan, ledger, False)
    check_resume(plan, ledger, True)
    ledger.claim("trial", "t")
    ledger.settle("trial", Decimal(".01"), "upper_bound")
    with pytest.raises(CampaignRefused, match="heldout"):
        check_resume(plan, ledger, True)
