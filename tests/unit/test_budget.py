from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from forge.context.budget import (
    BudgetExceeded,
    BudgetManager,
    BudgetState,
    PriceUnavailable,
    RuleRouter,
)
from forge.domain.types import BudgetLimits, ModelCapabilities, ModelSpec, RunConfig, TokenUsage


def model(rate: str = "1", *, capacity: int = 128_000, name: str = "model") -> ModelSpec:
    return ModelSpec(provider="fake", model_id=name, input_usd_per_million=Decimal(rate),
                     output_usd_per_million=Decimal(rate), price_version="fixture-v1",
                     capabilities=ModelCapabilities(context_window=capacity))


def test_reservations_unknown_usage_survive_restart_and_are_not_zero() -> None:
    limits = BudgetLimits(max_cost_usd=Decimal("0.003"))
    manager = BudgetManager(limits)
    reservation = manager.reserve("attempt-1", model(), 1000, 1000)
    manager.reconcile("attempt-1", None)
    restored = BudgetManager(limits, manager.state.model_dump(mode="json"))
    assert restored.remaining == Decimal("0.001")
    assert restored.known_spend_usd == Decimal(0)
    assert restored.state.reservations["attempt-1"].status == "unknown"
    assert restored.reserve("attempt-1", model(), 1000, 1000).reserved_usd == reservation.reserved_usd
    with pytest.raises(BudgetExceeded):
        restored.reserve("attempt-2", model(), 1000, 1000)
    restored.reconcile("attempt-1", TokenUsage(input_tokens=100, output_tokens=100))
    assert restored.remaining == Decimal("0.0028")


def test_estimated_low_usage_does_not_release_reservation() -> None:
    manager = BudgetManager(BudgetLimits())
    manager.reserve("a", model(), 2000, 1000)
    manager.reconcile("a", TokenUsage(input_tokens=1, output_tokens=1, estimated=True))
    assert manager.committed_usd == Decimal("0.003")
    manager.reconcile("a", TokenUsage(input_tokens=1000, output_tokens=500))
    assert manager.committed_usd == Decimal("0.0015")


def test_actual_overrun_blocks_future_requests_without_hiding_debt() -> None:
    manager = BudgetManager(BudgetLimits(max_cost_usd=Decimal("0.001")))
    manager.reserve("a", model(), 400, 400)
    manager.reconcile("a", TokenUsage(input_tokens=1500, output_tokens=500))
    assert manager.remaining == Decimal("-0.001")
    with pytest.raises(BudgetExceeded):
        manager.reserve("b", model(), 1, 1)


def test_cache_components_and_reasoning_are_not_double_charged() -> None:
    priced = model("10").model_copy(update={
        "cached_input_usd_per_million": Decimal("1"),
        "cache_write_usd_per_million": Decimal("12.5"),
    })
    manager = BudgetManager(BudgetLimits())
    manager.reserve("a", priced, 1000, 100)
    item = manager.reconcile("a", TokenUsage(
        input_tokens=1000, cached_input_tokens=500, cache_write_tokens=200,
        output_tokens=100, reasoning_tokens=80,
    ))
    assert item.charged_usd == Decimal("0.007")
    assert item.status == "settled"


def test_missing_price_fails_and_unknown_cache_write_price_keeps_reserve() -> None:
    manager = BudgetManager(BudgetLimits())
    with pytest.raises(PriceUnavailable):
        manager.reserve("a", ModelSpec(provider="openai", model_id="unknown"), 100, 100)
    manager.reserve("a", model(), 1000, 1000)
    item = manager.reconcile("a", TokenUsage(input_tokens=100, output_tokens=100, cache_write_tokens=50))
    assert item.status == "unknown" and item.charged_usd == Decimal("0.002")


def test_retry_attempts_tool_calls_repair_and_runtime_have_distinct_limits() -> None:
    limits = BudgetLimits(max_model_turns=1, max_tool_calls=2, max_repair_rounds=1)
    manager = BudgetManager(limits)
    manager.reserve("a", model(), 100, 100)
    manager.reconcile("a", None)
    with pytest.raises(BudgetExceeded, match="attempt"):
        manager.reserve("retry", model(), 100, 100)
    manager.consume_tools(2)
    with pytest.raises(BudgetExceeded, match="tool"):
        manager.consume_tools()
    manager.consume_repair()
    with pytest.raises(BudgetExceeded, match="repair"):
        manager.consume_repair()
    expired = BudgetManager(limits, BudgetState(started_at=datetime.now(UTC) - timedelta(hours=2)))
    with pytest.raises(BudgetExceeded, match="wall-clock"):
        expired.reserve("a", model(), 1, 1)


def test_request_context_and_output_upper_bounds_are_enforced() -> None:
    manager = BudgetManager(BudgetLimits(max_output_tokens=100))
    with pytest.raises(BudgetExceeded, match="output"):
        manager.reserve("a", model(), 1, 101)
    with pytest.raises(BudgetExceeded, match="context"):
        manager.reserve("a", model(capacity=200), 150, 100)


def test_reconciliation_is_idempotent_and_settled_cost_immutable() -> None:
    manager = BudgetManager(BudgetLimits())
    manager.reserve("a", model(), 100, 100)
    usage = TokenUsage(input_tokens=50, output_tokens=50)
    manager.reconcile("a", usage)
    manager.reconcile("a", usage)
    assert manager.committed_usd == Decimal("0.0001")
    with pytest.raises(ValueError, match="immutable"):
        manager.reconcile("a", TokenUsage(input_tokens=1, output_tokens=1))
    with pytest.raises(ValueError, match="different"):
        manager.reserve("a", model(), 99, 100)


def test_router_escalates_observable_failure_and_retains_auditable_state() -> None:
    config = RunConfig(models=[model(name="cheap"), model("3", name="strong")])
    manager = BudgetManager(config.budget)
    router = RuleRouter(config)
    first = router.select(manager, 100, 100)
    assert first.model.model_id == "cheap" and not first.escalated
    second = router.select(manager, 100, 100, consecutive_failures=2)
    assert second.reason == "repeated_repair_failure"
    assert second.model.model_id == "strong" and second.escalated
    restored = RuleRouter(config, router.state.model_dump(mode="json"))
    third = restored.select(manager, 100, 100, invalid_tool_calls=9)
    assert not third.escalated and third.escalation_count == 1
    assert len(restored.state.decisions) == 3


def test_router_does_not_upgrade_beyond_remaining_money() -> None:
    config = RunConfig(models=[model(name="cheap"), model("100", name="strong")],
                       budget=BudgetLimits(max_cost_usd=Decimal("0.001")))
    decision = RuleRouter(config).select(BudgetManager(config.budget), 100, 100, consecutive_failures=2)
    assert decision.model.model_id == "cheap"
    assert "escalation_blocked" in decision.reason


def test_context_rejection_requires_larger_affordable_model() -> None:
    config = RunConfig(models=[model(capacity=1000), model("2", capacity=5000, name="large")])
    decision = RuleRouter(config).select(BudgetManager(config.budget), 100, 100, context_exceeded=True)
    assert decision.model.model_id == "large"
    fixed = config.model_copy(update={"max_escalations": 0})
    with pytest.raises(BudgetExceeded, match="larger"):
        RuleRouter(fixed).select(BudgetManager(config.budget), 100, 100, context_exceeded=True)
