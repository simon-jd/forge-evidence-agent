"""Decimal cost reservations with persisted unknown-usage debt.

This module is intentionally storage-agnostic. The runtime persists ``state``
in the same transaction as the corresponding model-attempt ledger. A reservation
limits future requests; it cannot undo an already billed provider request.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field

from forge.domain.types import BudgetLimits, Contract, ModelSpec, RunConfig, TokenUsage

MILLION = Decimal(1_000_000)


class BudgetExceeded(ValueError):
    pass


class PriceUnavailable(ValueError):
    pass


class Reservation(Contract):
    attempt_id: str
    model: ModelSpec
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(gt=0)
    reserved_usd: Decimal = Field(ge=0)
    charged_usd: Decimal = Field(ge=0)
    status: Literal["reserved", "unknown", "estimated", "settled"] = "reserved"
    usage: TokenUsage | None = None


class BudgetState(Contract):
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    reservations: dict[str, Reservation] = Field(default_factory=dict)
    tool_calls: int = Field(default=0, ge=0)
    repair_rounds: int = Field(default=0, ge=0)


def estimate_cost(model: ModelSpec, input_tokens: int, output_tokens: int) -> Decimal:
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("token counts must be nonnegative")
    if model.input_usd_per_million is None or model.output_usd_per_million is None:
        raise PriceUnavailable(f"freeze input and output prices for {model.provider}/{model.model_id}")
    # Cache writes can cost more than normal input. Reserve at the most expensive
    # configured input rate; eventual exact usage releases the over-reservation.
    input_rate = max(rate for rate in (
        model.input_usd_per_million, model.cached_input_usd_per_million,
        model.cache_write_usd_per_million,
    ) if rate is not None)
    return (Decimal(input_tokens) * input_rate
            + Decimal(output_tokens) * model.output_usd_per_million) / MILLION


def _usage_cost(model: ModelSpec, usage: TokenUsage) -> tuple[Decimal, bool]:
    """Input tokens include cache reads/writes; reasoning is an output subset.

    Provider adapters normalize Anthropic's input components to that shared
    contract. Unreported cache splits are charged at the normal input rate, which
    is conservative for read caches. Unpriced cache writes remain uncertain.
    """
    if model.input_usd_per_million is None or model.output_usd_per_million is None:
        raise PriceUnavailable("reservation did not preserve model prices")
    cached = usage.cached_input_tokens or 0
    writes = usage.cache_write_tokens or 0
    if usage.input_tokens is not None and cached + writes > usage.input_tokens:
        raise ValueError("cache input subsets cannot exceed total input tokens")
    if (usage.reasoning_tokens is not None and usage.output_tokens is not None
            and usage.reasoning_tokens > usage.output_tokens):
        raise ValueError("reasoning tokens cannot exceed total output tokens")
    complete = usage.input_tokens is not None and usage.output_tokens is not None
    input_total = usage.input_tokens if usage.input_tokens is not None else cached + writes
    regular = input_total - cached - writes
    read_rate = model.cached_input_usd_per_million
    if read_rate is None:
        read_rate = model.input_usd_per_million
    write_rate = model.cache_write_usd_per_million
    if write_rate is None:
        write_rate = model.input_usd_per_million
        if writes:
            complete = False
    value = (Decimal(regular) * model.input_usd_per_million
             + Decimal(cached) * read_rate + Decimal(writes) * write_rate
             + Decimal(usage.output_tokens or 0) * model.output_usd_per_million) / MILLION
    return value, complete


class BudgetManager:
    def __init__(self, limits: BudgetLimits, state: BudgetState | dict | None = None) -> None:
        self.limits = limits.model_copy(deep=True)
        self.state = (BudgetState.model_validate(state) if state is not None else BudgetState()).model_copy(deep=True)

    @property
    def committed_usd(self) -> Decimal:
        return sum((item.charged_usd for item in self.state.reservations.values()), Decimal(0))

    @property
    def known_spend_usd(self) -> Decimal:
        return sum((item.charged_usd for item in self.state.reservations.values()
                    if item.status == "settled"), Decimal(0))

    @property
    def remaining(self) -> Decimal:
        # A negative value is deliberately visible if actual usage exceeds an
        # estimate. Never clamp away evidence of an overrun.
        return self.limits.max_cost_usd - self.committed_usd

    def check_runtime(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        elapsed = (now - self.state.started_at).total_seconds()
        if elapsed >= self.limits.max_runtime_seconds:
            raise BudgetExceeded("run wall-clock budget exhausted")

    def can_afford(self, model: ModelSpec, input_tokens: int, output_tokens: int) -> bool:
        try:
            self._check_request(model, input_tokens, output_tokens)
            return estimate_cost(model, input_tokens, output_tokens) <= self.remaining
        except (PriceUnavailable, BudgetExceeded):
            return False

    def _check_request(self, model: ModelSpec, input_tokens: int, output_tokens: int) -> None:
        if input_tokens < 0 or output_tokens <= 0:
            raise ValueError("input tokens must be nonnegative and output tokens positive")
        self.check_runtime()
        if len(self.state.reservations) >= self.limits.max_model_turns:
            raise BudgetExceeded("model attempt budget exhausted (retries count as attempts)")
        if output_tokens > min(self.limits.max_output_tokens, model.capabilities.max_output_tokens):
            raise BudgetExceeded("output token limit exceeds run or model capacity")
        if input_tokens + output_tokens > model.capabilities.context_window:
            raise BudgetExceeded("input plus reserved output exceeds model context window")

    def reserve(
        self, attempt_id: str, model: ModelSpec, input_tokens: int, output_tokens: int,
    ) -> Reservation:
        if not attempt_id:
            raise ValueError("attempt ID must be nonempty")
        previous = self.state.reservations.get(attempt_id)
        if previous is not None:
            if (previous.model != model or previous.input_tokens != input_tokens
                    or previous.output_tokens != output_tokens):
                raise ValueError("reservation ID was reused with a different request")
            return previous.model_copy(deep=True)
        self._check_request(model, input_tokens, output_tokens)
        amount = estimate_cost(model, input_tokens, output_tokens)
        if amount > self.remaining:
            raise BudgetExceeded(f"request needs ${amount}; only ${self.remaining} remains")
        reservation = Reservation(
            attempt_id=attempt_id, model=model.model_copy(deep=True),
            input_tokens=input_tokens, output_tokens=output_tokens,
            reserved_usd=amount, charged_usd=amount,
        )
        self.state.reservations[attempt_id] = reservation
        return reservation.model_copy(deep=True)

    def reconcile(self, attempt_id: str, usage: TokenUsage | None) -> Reservation:
        reservation = self.state.reservations[attempt_id]
        if reservation.status == "settled":
            if usage != reservation.usage:
                raise ValueError("settled usage is immutable; record a separate billing adjustment")
            return reservation.model_copy(deep=True)
        usage = usage or TokenUsage()
        amount, complete = _usage_cost(reservation.model, usage)
        reservation.usage = usage.model_copy(deep=True)
        if complete and not usage.estimated:
            reservation.status = "settled"
            reservation.charged_usd = amount
        else:
            reservation.status = "estimated" if usage.estimated else "unknown"
            reservation.charged_usd = max(reservation.charged_usd, reservation.reserved_usd, amount)
        return reservation.model_copy(deep=True)

    def consume_tools(self, count: int = 1) -> None:
        if count < 0:
            raise ValueError("tool count cannot decrease")
        if self.state.tool_calls + count > self.limits.max_tool_calls:
            raise BudgetExceeded("tool call budget exhausted")
        self.state.tool_calls += count

    def consume_repair(self) -> None:
        if self.state.repair_rounds >= self.limits.max_repair_rounds:
            raise BudgetExceeded("repair round budget exhausted")
        self.state.repair_rounds += 1


class RouteDecision(Contract):
    model_index: int
    model: ModelSpec
    escalated: bool
    reason: str
    escalation_count: int
    remaining_usd: Decimal
    projected_request_usd: Decimal


class RouterState(Contract):
    current_model: int = Field(ge=0)
    escalations: int = Field(default=0, ge=0)
    decisions: list[RouteDecision] = Field(default_factory=list)


class RuleRouter:
    def __init__(self, config: RunConfig, state: RouterState | dict | None = None) -> None:
        self.config = config.model_copy(deep=True)
        self.state = (RouterState.model_validate(state) if state is not None
                      else RouterState(current_model=config.initial_model)).model_copy(deep=True)
        if self.state.current_model >= len(config.models):
            raise ValueError("restored router references an unavailable model")

    def select(
        self, manager: BudgetManager, input_tokens: int, output_tokens: int, *,
        consecutive_failures: int = 0, invalid_tool_calls: int = 0,
        context_exceeded: bool = False,
    ) -> RouteDecision:
        if consecutive_failures < 0 or invalid_tool_calls < 0:
            raise ValueError("failure counts must be nonnegative")
        index = self.state.current_model
        current = self.config.models[index]
        reason = "fixed_model"
        trigger: str | None = None
        if context_exceeded or input_tokens + output_tokens > current.capabilities.context_window:
            trigger = "context_capacity"
        elif consecutive_failures >= self.config.escalation_after_failures:
            trigger = "repeated_repair_failure"
        elif invalid_tool_calls >= self.config.escalation_after_failures:
            trigger = "repeated_invalid_tool_arguments"
        escalated = False
        if trigger:
            reason = f"escalation_blocked:{trigger}:limit_or_capacity_or_cost"
            if self.state.escalations < self.config.max_escalations:
                for candidate_index in range(index + 1, len(self.config.models)):
                    candidate = self.config.models[candidate_index]
                    if context_exceeded and candidate.capabilities.context_window <= current.capabilities.context_window:
                        continue
                    if manager.can_afford(candidate, input_tokens, output_tokens):
                        index = candidate_index
                        current = candidate
                        reason = trigger
                        escalated = True
                        break
        if not manager.can_afford(current, input_tokens, output_tokens):
            raise BudgetExceeded(f"no affordable model with sufficient capacity; routing reason={reason}")
        if context_exceeded and not escalated:
            raise BudgetExceeded("provider rejected context and no larger affordable model is available")
        if escalated:
            self.state.current_model = index
            self.state.escalations += 1
        decision = RouteDecision(
            model_index=index, model=current.model_copy(deep=True), escalated=escalated,
            reason=reason, escalation_count=self.state.escalations,
            remaining_usd=manager.remaining,
            projected_request_usd=estimate_cost(current, input_tokens, output_tokens),
        )
        self.state.decisions.append(decision)
        return decision.model_copy(deep=True)
