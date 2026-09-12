"""OpenAI Responses adapter using the native async SDK (never Chat Completions).

Protocol references verified 2026-09-07:
https://developers.openai.com/api/docs/guides/function-calling
https://developers.openai.com/api/docs/guides/compaction

NativeState.items is the full input window plus confirmed output items. When
reusing it, canonical_messages must contain only newly appended messages. Opaque
reasoning/compaction items remain native JSON, never portable summary text.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import Any, cast

from openai import AsyncOpenAI
from openai.types.responses import ResponseInputParam

from forge.domain.types import (
    CanonicalMessage,
    CompactRequest,
    CompactResult,
    ModelCapabilities,
    ModelEvent,
    ModelRequest,
    ModelTurn,
    NativeState,
    ProviderError,
    TextDelta,
    TokenUsage,
    ToolCallDelta,
    ToolCallReady,
    TurnCompleted,
    UsageUpdate,
)
from forge.providers.base import (
    AttemptSink,
    BaseProvider,
    ProviderFailure,
    check_pairing,
    invalid,
    normalize_error,
    unsupported,
    validate_call,
    validate_native,
)


def openai_items(messages: list[CanonicalMessage]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        for part in message.parts:
            if part.type in {"text", "file_reference"}:
                if message.role == "tool":
                    raise invalid("Tool messages must contain correlated tool_result parts.")
                text = part.text
                if part.type == "file_reference":
                    text = f"[{part.path}:{part.start_line}-{part.end_line or '?'} sha256={part.sha256}]\n{text}"
                items.append({"role": message.role, "content": text})
            elif part.type == "tool_call":
                if message.role != "assistant":
                    raise invalid("Only assistant messages can contain tool calls.")
                items.append({"type": "function_call", "call_id": part.call.call_id,
                              "name": part.call.name, "arguments": json.dumps(part.call.arguments)})
            elif part.type == "tool_result":
                if message.role not in {"tool", "user"}:
                    raise invalid("Tool results require tool or user role.")
                items.append({"type": "function_call_output", "call_id": part.call_id,
                              "output": part.content})
    return items


def openai_usage(data: dict[str, Any] | None) -> TokenUsage:
    data = data or {}
    return TokenUsage(input_tokens=data.get("input_tokens"), output_tokens=data.get("output_tokens"),
                      cached_input_tokens=(data.get("input_tokens_details") or {}).get("cached_tokens"),
                      cache_write_tokens=(data.get("input_tokens_details") or {}).get("cache_write_tokens"),
                      reasoning_tokens=(data.get("output_tokens_details") or {}).get("reasoning_tokens"))


class OpenAIProvider(BaseProvider):
    provider = "openai"
    implemented_features = frozenset({"tool_calling", "parallel_tool_calls", "native_compaction", "structured_output"})

    def __init__(self, *, client: AsyncOpenAI | None = None,
                 capabilities: Mapping[str, ModelCapabilities] | None = None,
                 max_attempts: int = 3, attempt_sink: AttemptSink | None = None,
                 backoff_base_seconds: float = 0.25, backoff_cap_seconds: float = 8):
        super().__init__(capabilities=capabilities, max_attempts=max_attempts, attempt_sink=attempt_sink,
                         backoff_base_seconds=backoff_base_seconds, backoff_cap_seconds=backoff_cap_seconds)
        self.client = (client or AsyncOpenAI(max_retries=0)).with_options(max_retries=0)

    def _input(self, request: ModelRequest | CompactRequest) -> list[dict[str, Any]]:
        validate_native(request.native_state, self.provider, request.model_id)
        items = list(request.native_state.items) if request.native_state else []
        items.extend(openai_items(request.canonical_messages))
        check_pairing(items, self.provider)
        return items

    def _params(self, request: ModelRequest) -> dict[str, Any]:
        config = dict(request.sampling_config)
        allowed = {"temperature", "top_p", "reasoning", "text", "tool_choice", "parallel_tool_calls"}
        if set(config) - allowed:
            raise unsupported(f"Unsupported OpenAI sampling options: {sorted(set(config) - allowed)}")
        caps = self.capabilities(request.model_id)
        if config.get("parallel_tool_calls") and not caps.parallel_tool_calls:
            raise unsupported("parallel_tool_calls is not enabled in the model configuration.")
        if "text" in config and not caps.structured_output:
            raise unsupported("Text output configuration requires structured_output capability.")
        return {"model": request.model_id, "input": self._input(request),
                "tools": [{"type": "function", "name": t.name, "description": t.description,
                           "parameters": t.parameters, "strict": False} for t in request.tools],
                "max_output_tokens": request.output_limit, "stream": True, "store": False,
                "include": ["reasoning.encrypted_content"],
                "parallel_tool_calls": caps.parallel_tool_calls, **config}

    async def _stream_once(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        params = self._params(request)
        buffer: dict[str, dict[str, Any]] = {}
        final: dict[str, Any] | None = None
        stream = await self.client.responses.create(**params, timeout=request.deadline_seconds)
        async with stream:
            async for sdk_event in stream:
                event = sdk_event.model_dump(mode="json", exclude_none=True)
                kind = event.get("type")
                if kind == "response.output_text.delta":
                    yield TextDelta(text=event["delta"])
                elif kind == "response.output_item.added":
                    item = event["item"]
                    if item.get("type") == "function_call":
                        buffer[item["id"]] = {**item, "arguments": item.get("arguments", "")}
                        yield ToolCallDelta(call_id=item["call_id"], name=item["name"])
                elif kind == "response.function_call_arguments.delta":
                    item = buffer.get(event["item_id"])
                    if item is None:
                        raise invalid("Function argument delta arrived before its call.")
                    item["arguments"] += event["delta"]
                    yield ToolCallDelta(call_id=item["call_id"], arguments_delta=event["delta"])
                elif kind == "response.function_call_arguments.done":
                    item = buffer.get(event["item_id"])
                    if item is None:
                        raise invalid("Function argument completion has no matching call.")
                    if item["arguments"] and item["arguments"] != event["arguments"]:
                        raise invalid("Completed function arguments disagree with streamed arguments.")
                    item["arguments"] = event["arguments"]
                elif kind == "response.completed":
                    final = event["response"]
                    break
                elif kind in {"response.failed", "response.incomplete"}:
                    response = event.get("response") or {}
                    if response.get("status") != kind.removeprefix("response."):
                        raise invalid("Terminal event disagrees with its response status.")
                    usage = openai_usage(response.get("usage"))
                    complete = usage.input_tokens is not None and usage.output_tokens is not None
                    if (usage.input_tokens is not None
                            and (usage.cached_input_tokens or 0) + (usage.cache_write_tokens or 0) > usage.input_tokens
                            or usage.output_tokens is not None
                            and (usage.reasoning_tokens or 0) > usage.output_tokens):
                        raise invalid("Terminal usage has inconsistent token subsets.")
                    total = (response.get("usage") or {}).get("total_tokens")
                    if (usage.input_tokens is not None and usage.output_tokens is not None
                            and total is not None and total != usage.input_tokens + usage.output_tokens):
                        raise invalid("Terminal usage total disagrees with input and output counts.")
                    # Terminal billing and executable output are independent.
                    # A truncated/failed response can report its final token use;
                    # a partial or absent usage object cannot release a reserve.
                    yield UsageUpdate(usage=usage, final=complete)
                    error = response.get("error") or {}
                    if error.get("code") == "context_length_exceeded":
                        raise ProviderFailure(ProviderError(code="context_exceeded", message="Provider context limit exceeded."))
                    raise ProviderFailure(ProviderError(code="stream_interrupted", message=f"Response terminated with {kind}; tools withheld."))
                elif kind == "error":
                    code = event.get("code", "")
                    if code == "rate_limit_exceeded":
                        raise ProviderFailure(ProviderError(code="rate_limited", message="Provider rate limited the stream.", retryable=True))
                    raise ProviderFailure(ProviderError(code="stream_interrupted", message="Provider emitted a stream error."))
        if final is None:
            raise ProviderFailure(ProviderError(code="stream_interrupted", message="Response stream ended before response.completed."))
        if final.get("status") != "completed":
            raise invalid("Terminal response is not completed.")
        # Generation is billable even when its tool arguments fail validation.
        usage = openai_usage(final.get("usage"))
        yield UsageUpdate(usage=usage, final=True)
        output = final.get("output", [])
        calls = []
        visible: list[str] = []
        seen: set[str] = set()
        for item in output:
            if item.get("type") == "function_call":
                call = validate_call(item.get("call_id", ""), item.get("name", ""), item.get("arguments", ""), request.tools)
                if call.call_id in seen:
                    raise invalid("Provider repeated a tool call ID.")
                seen.add(call.call_id)
                partial = buffer.get(item.get("id", ""))
                if partial is not None and partial["arguments"] != item.get("arguments"):
                    raise invalid("Terminal function arguments disagree with the stream.")
                calls.append(call)
            elif item.get("type") == "message":
                visible.extend(part.get("text", "") for part in item.get("content", []) if part.get("type") == "output_text")
        if {item["call_id"] for item in buffer.values()} - seen:
            raise invalid("Terminal response omitted a streamed tool call.")
        for call in calls:
            yield ToolCallReady(call=call)
        yield TurnCompleted(turn=ModelTurn(
            provider=self.provider, requested_model=request.model_id, reported_model=final.get("model"),
            response_id=final.get("id"), visible_text="".join(visible), complete_tool_calls=calls,
            usage=usage, finish_reason="tool_calls" if calls else "stop",
            native_state=NativeState(provider=self.provider, model_id=request.model_id,
                                     items=[*params["input"], *output]), raw_response=final,
        ))

    async def compact(self, request: CompactRequest) -> CompactResult:
        validate_native(request.native_state, self.provider, request.model_id)
        if not self.capabilities(request.model_id).native_compaction:
            raise unsupported("Native compaction must be explicitly enabled for this model.")
        # Compaction is a separate paid operation. The caller owns its logical
        # operation ledger and budget; this method performs exactly one attempt.
        try:
            result = await self.client.responses.compact(model=request.model_id, input=cast(ResponseInputParam, self._input(request)),
                                                         instructions=request.instructions)
        except Exception as exc:
            raise ProviderFailure(normalize_error(exc)) from exc
        raw = result.model_dump(mode="json", exclude_none=True)
        return CompactResult(native_state=NativeState(provider=self.provider, model_id=request.model_id,
                                                       items=raw["output"]), usage=openai_usage(raw.get("usage")))

    async def aclose(self) -> None:
        await self.client.close()
