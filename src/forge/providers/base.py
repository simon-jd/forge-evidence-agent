"""Provider contracts, validation, and one bounded retry/attempt-accounting layer.

Adapters never execute tools. Even a syntactically complete tool block is withheld
until the provider confirms the entire response: a torn stream is not a commit.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any, Protocol

import httpx
import httpx2
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from forge.domain.types import (
    CompactRequest,
    CompactResult,
    ModelCapabilities,
    ModelEvent,
    ModelRequest,
    NativeState,
    ProviderError,
    TokenUsage,
    ToolCall,
    ToolSpec,
    TurnCompleted,
    UsageUpdate,
)

AttemptSink = Callable[[dict[str, Any]], Awaitable[None]]


class ProviderAdapter(Protocol):
    def capabilities(self, model_id: str) -> ModelCapabilities: ...
    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]: ...
    async def compact(self, request: CompactRequest) -> CompactResult: ...


class ProviderFailure(Exception):
    def __init__(self, error: ProviderError):
        super().__init__(error.message)
        self.error = error


def invalid(message: str) -> ProviderFailure:
    return ProviderFailure(ProviderError(code="invalid_request", message=message))


def invalid_tool(message: str) -> ProviderFailure:
    """A confirmed generation needs model feedback, not an HTTP request retry."""
    return ProviderFailure(ProviderError(code="invalid_tool_arguments", message=message))


def unsupported(message: str) -> ProviderFailure:
    return ProviderFailure(ProviderError(code="unsupported_capability", message=message))


def validate_native(state: NativeState | None, provider: str, model_id: str) -> None:
    if state is not None and (state.provider != provider or state.model_id != model_id):
        raise invalid("Native state is bound to its provider and model; rebuild portable context.")
    if state is not None and state.continuation_id and not state.items:
        raise unsupported("Server-only continuation IDs are unsupported; supply native items.")


def validate_call(call_id: str, name: str, arguments: str | dict[str, Any], tools: list[ToolSpec]) -> ToolCall:
    if not call_id:
        raise invalid("Provider returned a tool call without a call ID.")
    spec = next((tool for tool in tools if tool.name == name), None)
    if spec is None:
        raise invalid_tool("Provider selected a tool outside the declared tool set.")
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
        if not isinstance(args, dict):
            raise invalid_tool(f"Arguments for {name} must be a JSON object.")
        Draft202012Validator(spec.parameters).validate(args)
    except (json.JSONDecodeError, ValidationError) as exc:
        # Do not copy arbitrary arguments, which may contain secrets, into errors.
        raise invalid_tool(f"Arguments for {name} failed JSON/schema validation.") from exc
    return ToolCall(call_id=call_id, name=name, arguments=args)


def check_pairing(items: list[dict[str, Any]], provider: str) -> None:
    """Reject duplicate/orphan/unresolved public calls before making a paid request."""
    pending: set[str] = set()
    seen: set[str] = set()
    for item in items:
        blocks = [item] if provider == "openai" else item.get("content", [])
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind in {"function_call", "tool_use"}:
                call_id = block.get("call_id") if provider == "openai" else block.get("id")
                if not call_id or call_id in seen:
                    raise invalid("Tool call IDs must be nonempty and unique in the transcript.")
                seen.add(call_id)
                pending.add(call_id)
            elif kind in {"function_call_output", "tool_result"}:
                call_id = block.get("call_id") if provider == "openai" else block.get("tool_use_id")
                if call_id not in pending:
                    raise invalid("A tool result has no matching unresolved call.")
                pending.remove(call_id)
    if pending:
        raise invalid("Resolve every pending tool call before requesting the next model turn.")


def normalize_error(exc: Exception, *, partial: bool = False) -> ProviderError:
    if isinstance(exc, ProviderFailure):
        return exc.error
    status = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    code = ""
    error_message = ""
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict):
            code = str(error.get("code") or error.get("type") or "")
            error_message = str(error.get("message") or "").lower()
    headers = getattr(getattr(exc, "response", None), "headers", {})
    delay = None
    try:
        if headers.get("retry-after") is not None:
            delay = max(0.0, float(headers["retry-after"]))
    except (ValueError, TypeError):
        pass
    if status in {401, 403}:
        return ProviderError(code="authentication_error", message="Provider authentication failed.")
    if code in {"context_length_exceeded", "prompt_too_long"} or (
        status == 400 and any(phrase in error_message for phrase in
                              ("prompt is too long", "input is too long", "maximum context length"))
    ):
        return ProviderError(code="context_exceeded", message="Provider context limit exceeded.")
    if status == 429 or code == "rate_limit_error":
        return ProviderError(code="rate_limited", message="Provider rate limit reached.",
                             retryable=not partial, retry_after_seconds=delay)
    if status in {408, 409, 500, 502, 503, 504, 529} or code in {"overloaded_error", "server_error"}:
        return ProviderError(code="transient_provider_error", message="Provider temporarily unavailable.",
                             retryable=not partial, retry_after_seconds=delay)
    if status is not None and 400 <= status < 500:
        return ProviderError(code="invalid_request", message="Provider rejected the request.")
    if isinstance(exc, (TimeoutError, httpx.TransportError, httpx2.TransportError)) or type(exc).__name__ in {"APIConnectionError", "APITimeoutError"}:
        return ProviderError(code="stream_interrupted" if partial else "transient_provider_error",
                             message="Provider transport interrupted; usage may be unknown.", retryable=not partial)
    return ProviderError(code="stream_interrupted", message="Provider response could not be completed.")


class BaseProvider:
    provider: str
    implemented_features: frozenset[str] = frozenset({"tool_calling", "parallel_tool_calls"})

    def __init__(self, *, capabilities: Mapping[str, ModelCapabilities] | None = None,
                 max_attempts: int = 3, attempt_sink: AttemptSink | None = None,
                 backoff_base_seconds: float = 0.25, backoff_cap_seconds: float = 8.0):
        if not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts must be between 1 and 10")
        if backoff_base_seconds < 0 or backoff_cap_seconds < 0:
            raise ValueError("Retry backoff must be nonnegative")
        self._capabilities = dict(capabilities or {})
        self.max_attempts = max_attempts
        self.attempt_sink = attempt_sink
        self.backoff_base_seconds = backoff_base_seconds
        self.backoff_cap_seconds = backoff_cap_seconds

    def capabilities(self, model_id: str) -> ModelCapabilities:
        configured = self._capabilities.get(model_id, ModelCapabilities())
        result = configured.model_copy(deep=True)
        for feature in ("tool_calling", "structured_output", "parallel_tool_calls", "native_compaction", "native_async_tools", "tool_search"):
            if feature not in self.implemented_features:
                setattr(result, feature, False)
        return result

    def validate(self, request: ModelRequest) -> None:
        validate_native(request.native_state, self.provider, request.model_id)
        caps = self.capabilities(request.model_id)
        for feature in request.requested_features:
            if feature not in self.implemented_features or not getattr(caps, feature, False):
                raise unsupported(f"{self.provider}/{request.model_id} does not enable {feature}.")
        requested = set(request.requested_features)
        for conflict in caps.incompatible_features:
            if set(conflict).issubset(requested):
                raise unsupported(f"Incompatible requested feature combination: {conflict}.")
        if request.tools and not caps.tool_calling:
            raise unsupported("Configured model does not support tool calling.")
        if request.output_limit > caps.max_output_tokens:
            raise invalid("Requested output exceeds the frozen model output limit.")
        names: set[str] = set()
        for tool in request.tools:
            if tool.name in names:
                raise invalid("Tool names must be unique.")
            names.add(tool.name)
            try:
                Draft202012Validator.check_schema(tool.parameters)
            except SchemaError as exc:
                raise invalid(f"Tool {tool.name} has an invalid JSON schema.") from exc

    async def _record(self, payload: dict[str, Any]) -> None:
        if self.attempt_sink is not None:
            await self.attempt_sink(payload)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        try:
            self.validate(request)
        except ProviderFailure as exc:
            yield exc.error
            return
        deadline = time.monotonic() + request.deadline_seconds
        for attempt in range(1, self.max_attempts + 1):
            record = {"attempt_id": str(uuid.uuid4()), "run_id": request.run_id,
                      "turn_id": request.turn_id, "provider": self.provider,
                      "model_id": request.model_id, "attempt": attempt}
            await self._record({**record, "status": "started", "usage": TokenUsage().model_dump(),
                                "usage_final": False})
            partial = False
            usage = TokenUsage()
            usage_final = False
            started = time.monotonic()
            try:
                async with asyncio.timeout(max(0, deadline - time.monotonic())):
                    async for event in self._stream_once(request):
                        if isinstance(event, ProviderError):
                            raise ProviderFailure(event)
                        if isinstance(event, UsageUpdate):
                            usage = event.usage
                            usage_final = event.final
                        if isinstance(event, TurnCompleted):
                            usage = event.turn.usage
                            usage_final = True
                            event.turn.raw_response["forge_attempt"] = {**record, "status": "completed"}
                            await self._record({**record, "status": "completed", "usage": usage.model_dump(),
                                                "usage_final": usage_final,
                                                "elapsed_seconds": time.monotonic() - started})
                            yield event
                            return
                        partial = True
                        yield event
                raise ProviderFailure(ProviderError(code="stream_interrupted", message="Stream ended without a confirmed response."))
            except asyncio.CancelledError:
                if not usage_final:
                    usage = usage.model_copy(update={"estimated": True})
                await self._record({**record, "status": "cancelled", "usage": usage.model_dump(),
                                    "usage_final": usage_final,
                                    "elapsed_seconds": time.monotonic() - started})
                raise
            except Exception as exc:
                # Mid-stream counters are not final billed usage, even when
                # both input and output numbers have already been reported.
                if not usage_final:
                    usage = usage.model_copy(update={"estimated": True})
                error = normalize_error(exc, partial=partial)
                if partial:
                    error.retryable = False
                await self._record({**record, "status": "failed", "usage": usage.model_dump(),
                                    "usage_final": usage_final,
                                    "error": error.model_dump(), "elapsed_seconds": time.monotonic() - started})
                delay = error.retry_after_seconds
                if delay is None:
                    delay = random.uniform(0, min(self.backoff_cap_seconds, self.backoff_base_seconds * 2 ** (attempt - 1)))
                if not error.retryable or attempt == self.max_attempts or time.monotonic() + delay >= deadline:
                    yield error
                    return
                await asyncio.sleep(delay)

    async def _stream_once(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        raise NotImplementedError
        yield  # pragma: no cover

    async def compact(self, request: CompactRequest) -> CompactResult:
        validate_native(request.native_state, self.provider, request.model_id)
        raise unsupported("Native compaction is not enabled; explicitly select portable compaction.")
