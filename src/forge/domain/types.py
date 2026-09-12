"""Serializable contracts shared by the runtime, adapters and evaluation harness.

Provider-native data is deliberately separate from portable messages. The latter
can cross provider boundaries; native continuation items cannot.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def canonical_hash(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolCall(Contract):
    call_id: str
    name: str
    arguments: dict[str, Any]


class TextPart(Contract):
    type: Literal["text"] = "text"
    text: str


class ToolCallPart(Contract):
    type: Literal["tool_call"] = "tool_call"
    call: ToolCall


class ToolResultPart(Contract):
    type: Literal["tool_result"] = "tool_result"
    call_id: str
    content: str
    is_error: bool = False


class FileReferencePart(Contract):
    type: Literal["file_reference"] = "file_reference"
    path: str
    sha256: str
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    text: str = ""


ContentPart = Annotated[
    TextPart | ToolCallPart | ToolResultPart | FileReferencePart, Field(discriminator="type")
]


class CanonicalMessage(Contract):
    role: Literal["system", "user", "assistant", "tool"]
    parts: list[ContentPart]

    @classmethod
    def text(cls, role: Literal["system", "user", "assistant", "tool"], text: str) -> CanonicalMessage:
        return cls(role=role, parts=[TextPart(text=text)])


class ModelCapabilities(Contract):
    tool_calling: bool = True
    structured_output: bool = False
    parallel_tool_calls: bool = False
    native_compaction: bool = False
    native_async_tools: bool = False
    tool_search: bool = False
    context_window: int = Field(default=128_000, gt=0)
    max_output_tokens: int = Field(default=8_192, gt=0)
    incompatible_features: list[list[str]] = Field(default_factory=list)


class ToolSpec(Contract):
    name: str
    description: str
    parameters: dict[str, Any]
    read_only: bool = False


class NativeState(Contract):
    provider: str
    model_id: str
    items: list[dict[str, Any]] = Field(default_factory=list)
    continuation_id: str | None = None


class TokenUsage(Contract):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    estimated: bool = False


class ModelRequest(Contract):
    run_id: str
    turn_id: str
    model_id: str
    canonical_messages: list[CanonicalMessage]
    tools: list[ToolSpec] = Field(default_factory=list)
    native_state: NativeState | None = None
    sampling_config: dict[str, Any] = Field(default_factory=dict)
    output_limit: int = Field(default=4_096, gt=0)
    deadline_seconds: float = Field(default=120, gt=0)
    requested_features: list[str] = Field(default_factory=list)


class ModelTurn(Contract):
    provider: str
    requested_model: str
    reported_model: str | None = None
    response_id: str | None = None
    visible_text: str = ""
    complete_tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    finish_reason: str
    native_state: NativeState | None = None
    raw_response: dict[str, Any] = Field(default_factory=dict)


class TextDelta(Contract):
    type: Literal["text_delta"] = "text_delta"
    text: str


class ToolCallDelta(Contract):
    type: Literal["tool_call_delta"] = "tool_call_delta"
    call_id: str
    name: str | None = None
    arguments_delta: str = ""


class ToolCallReady(Contract):
    type: Literal["tool_call_ready"] = "tool_call_ready"
    call: ToolCall


class UsageUpdate(Contract):
    type: Literal["usage_update"] = "usage_update"
    usage: TokenUsage
    # True only after provider terminal confirmation, not for cumulative deltas.
    final: bool = False


class TurnCompleted(Contract):
    type: Literal["turn_completed"] = "turn_completed"
    turn: ModelTurn


ProviderErrorCode = Literal[
    "rate_limited", "transient_provider_error", "invalid_request", "context_exceeded",
    "authentication_error", "stream_interrupted", "unsupported_capability", "invalid_tool_arguments",
]


class ProviderError(Contract):
    type: Literal["provider_error"] = "provider_error"
    code: ProviderErrorCode
    message: str
    retryable: bool = False
    retry_after_seconds: float | None = None


ModelEvent = Annotated[
    TextDelta | ToolCallDelta | ToolCallReady | UsageUpdate | TurnCompleted | ProviderError,
    Field(discriminator="type"),
]


class CompactRequest(Contract):
    model_id: str
    canonical_messages: list[CanonicalMessage]
    native_state: NativeState | None = None
    instructions: str = "Preserve task constraints, verified evidence and unresolved work."


class CompactResult(Contract):
    native_state: NativeState
    usage: TokenUsage = Field(default_factory=TokenUsage)


class EvidenceAnchor(Contract):
    evidence_id: str
    path: str
    sha256: str
    start_line: int = Field(default=1, ge=1)
    end_line: int = Field(ge=1)
    excerpt: str
    origin_call_id: str | None = None
    status: Literal["current", "stale", "missing"] = "current"


class CompactionSummary(Contract):
    task: str
    constraints: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    test_state: list[str] = Field(default_factory=list)
    unresolved_items: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class ContextConfig(Contract):
    continuation_mode: Literal["portable", "provider_native"] = "portable"
    evidence_guard: bool = True
    strategy: Literal["recent", "evidence", "evidence_compact"] = "evidence_compact"
    input_budget: int = Field(default=32_000, gt=0)
    compact_threshold: float = Field(default=0.7, gt=0, le=1)
    recent_turns: int = Field(default=6, ge=1)
    max_file_tokens: int = Field(default=4_000, gt=0)


class ContextBundle(Contract):
    messages: list[CanonicalMessage]
    evidence: list[EvidenceAnchor] = Field(default_factory=list)
    stale_evidence_ids: list[str] = Field(default_factory=list)
    token_estimate: int = Field(ge=0)
    token_breakdown: dict[str, int] = Field(default_factory=dict)
    token_count_kind: Literal["estimated", "exact"] = "estimated"
    compacted: bool = False
    summary: CompactionSummary | None = None
    context_hash: str


class ModelSpec(Contract):
    provider: Literal["fake", "openai", "anthropic", "deepseek"]
    model_id: str
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    input_usd_per_million: Decimal | None = Field(default=None, ge=0)
    output_usd_per_million: Decimal | None = Field(default=None, ge=0)
    cached_input_usd_per_million: Decimal | None = Field(default=None, ge=0)
    cache_write_usd_per_million: Decimal | None = Field(default=None, ge=0)
    price_version: str = "unconfigured"
    price_basis: Literal["exact", "upper_bound"] = "exact"


class BudgetLimits(Contract):
    max_cost_usd: Decimal = Field(default=Decimal("3"), gt=0)
    max_model_turns: int = Field(default=40, ge=1)
    max_tool_calls: int = Field(default=100, ge=1)
    max_repair_rounds: int = Field(default=2, ge=0)
    max_output_tokens: int = Field(default=4_096, ge=1)
    max_runtime_seconds: int = Field(default=1_800, ge=1)


class RunConfig(Contract):
    sampling_config: dict[str, Any] = Field(default_factory=dict)
    stalled_turn_limit: int = Field(default=4, ge=2, le=20)
    models: list[ModelSpec] = Field(default_factory=lambda: [
        ModelSpec(provider="fake", model_id="scripted", input_usd_per_million=Decimal(0),
                  output_usd_per_million=Decimal(0), price_version="offline-zero")
    ])
    initial_model: int = Field(default=0, ge=0)
    max_escalations: int = Field(default=1, ge=0)
    escalation_after_failures: int = Field(default=2, ge=1)
    budget: BudgetLimits = Field(default_factory=BudgetLimits)
    context: ContextConfig = Field(default_factory=ContextConfig)
    max_concurrent_read_tools: int = Field(default=4, ge=1, le=16)
    allow_commands: bool = True
    require_write_approval: bool = False

    @model_validator(mode="after")
    def model_exists(self) -> RunConfig:
        if not self.models or self.initial_model >= len(self.models):
            raise ValueError("initial_model must select an available model")
        return self


class RepoProfile(Contract):
    name: str
    language: str = "python"
    image: str
    validation_argv: list[str]
    target_test_argv: list[str] = Field(default_factory=list)
    allowed_patch_paths: list[str] = Field(default_factory=lambda: ["**"])
    protected_paths: list[str] = Field(default_factory=lambda: [".git/**", ".forge/**"])
    command_timeout_seconds: int = Field(default=120, ge=1, le=1_800)
    memory_bytes: int = Field(default=512 * 1024 * 1024, ge=64 * 1024 * 1024)
    cpu_limit: float = Field(default=1, gt=0)
    pid_limit: int = Field(default=128, ge=16)


class ToolResult(Contract):
    call_id: str
    status: Literal["ok", "error", "denied", "timeout", "cancelled"]
    summary: str
    artifact_refs: list[str] = Field(default_factory=list)
    workspace_revision: int = Field(default=0, ge=0)
    exit_code: int | None = None
    truncated: bool = False
    evidence: list[EvidenceAnchor] = Field(default_factory=list)


RunStatus = Literal[
    "queued", "running", "waiting_approval", "completed", "failed", "cancelled",
    "budget_exhausted", "needs_reconciliation",
]


class ValidationResult(Contract):
    checks: dict[str, Any] = Field(default_factory=dict)
    status: Literal["verified", "regression_only", "unverified"]
    baseline_exit_code: int | None = None
    final_exit_code: int | None = None
    target_exit_code: int | None = None
    summary: str
    artifact_refs: list[str] = Field(default_factory=list)
