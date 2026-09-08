"""Native Anthropic Messages SSE; thinking signatures are preserved as native data.

Verified protocol: https://platform.claude.com/docs/en/build-with-claude/streaming
NativeState contains full native messages, including a system envelope when used.
With NativeState, canonical_messages contains only appended messages.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

from anthropic import AsyncAnthropic

from forge.domain.types import (
    CanonicalMessage,
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
    unsupported,
    validate_call,
)


def anthropic_items(messages: list[CanonicalMessage]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for message in messages:
        blocks: list[dict[str, Any]] = []
        for part in message.parts:
            if part.type in {"text", "file_reference"}:
                if message.role == "tool":
                    raise invalid("Tool messages must contain correlated tool_result parts.")
                text = part.text
                if part.type == "file_reference":
                    text = f"[{part.path}:{part.start_line}-{part.end_line or '?'} sha256={part.sha256}]\n{text}"
                blocks.append({"type": "text", "text": text})
            elif part.type == "tool_call":
                if message.role != "assistant":
                    raise invalid("Only assistant messages can contain tool calls.")
                blocks.append({"type": "tool_use", "id": part.call.call_id, "name": part.call.name,
                               "input": part.call.arguments})
            elif part.type == "tool_result":
                if message.role not in {"tool", "user"}:
                    raise invalid("Tool results require tool or user role.")
                blocks.append({"type": "tool_result", "tool_use_id": part.call_id,
                               "content": part.content, "is_error": part.is_error})
        role = "user" if message.role == "tool" else message.role
        if result and result[-1]["role"] == role:
            result[-1]["content"].extend(blocks)
        else:
            result.append({"role": role, "content": blocks})
    return result


def anthropic_usage(data: dict[str, Any]) -> TokenUsage:
    # Anthropic input_tokens excludes cache reads/writes. Canonical input_tokens
    # is total input, so pricing can consistently subtract separately priced cache.
    base = data.get("input_tokens")
    read = data.get("cache_read_input_tokens")
    write = data.get("cache_creation_input_tokens")
    total = None if base is None else base + (read or 0) + (write or 0)
    return TokenUsage(input_tokens=total, output_tokens=data.get("output_tokens"),
                      cached_input_tokens=read, cache_write_tokens=write,
                      reasoning_tokens=(data.get("output_tokens_details") or {}).get("thinking_tokens"))


class AnthropicProvider(BaseProvider):
    provider = "anthropic"
    implemented_features = frozenset({"tool_calling", "parallel_tool_calls", "structured_output"})

    def __init__(self, *, client: AsyncAnthropic | None = None,
                 capabilities: Mapping[str, ModelCapabilities] | None = None,
                 max_attempts: int = 3, attempt_sink: AttemptSink | None = None,
                 backoff_base_seconds: float = 0.25, backoff_cap_seconds: float = 8):
        super().__init__(capabilities=capabilities, max_attempts=max_attempts, attempt_sink=attempt_sink,
                         backoff_base_seconds=backoff_base_seconds, backoff_cap_seconds=backoff_cap_seconds)
        self.client = (client or AsyncAnthropic(max_retries=0)).with_options(max_retries=0)

    def _params(self, request: ModelRequest) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        items = list(request.native_state.items) if request.native_state else []
        additions = anthropic_items(request.canonical_messages)
        for item in additions:
            if items and items[-1]["role"] == item["role"]:
                items[-1] = {**items[-1], "content": [*items[-1]["content"], *item["content"]]}
            else:
                items.append(item)
        check_pairing(items, self.provider)
        # Anthropic requires all tool_result blocks before new user text.
        for item in items:
            blocks = item.get("content", [])
            if item.get("role") == "user" and any(block.get("type") == "tool_result" for block in blocks):
                found_text = False
                for block in blocks:
                    if block.get("type") != "tool_result":
                        found_text = True
                    elif found_text:
                        raise invalid("Anthropic tool results must precede other user content.")
        config = dict(request.sampling_config)
        # The locked 1.4 SDK intentionally removed legacy temperature/top_p/top_k.
        allowed = {"stop_sequences", "thinking", "tool_choice", "output_config"}
        if set(config) - allowed:
            raise unsupported(f"Unsupported Anthropic sampling options: {sorted(set(config) - allowed)}")
        if "output_config" in config and not self.capabilities(request.model_id).structured_output:
            raise unsupported("output_config requires the structured_output capability.")
        params: dict[str, Any] = {"model": request.model_id, "max_tokens": request.output_limit,
                                  "messages": [item for item in items if item["role"] != "system"], "stream": True, **config}
        system = [block for item in items if item["role"] == "system" for block in item["content"]]
        if system:
            params["system"] = system
        if request.tools:
            params["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in request.tools]
            choice = params.setdefault("tool_choice", {"type": "auto"})
            choice = dict(choice)
            choice["disable_parallel_tool_use"] = not self.capabilities(request.model_id).parallel_tool_calls
            params["tool_choice"] = choice
        return params, items

    async def _stream_once(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        params, items = self._params(request)
        stream = await self.client.messages.create(**params, timeout=request.deadline_seconds)
        blocks: dict[int, dict[str, Any]] = {}
        arguments: dict[int, str] = {}
        closed: set[int] = set()
        final: dict[str, Any] | None = None
        usage_data: dict[str, Any] = {}
        stopped = False
        async with stream:
            async for sdk_event in stream:
                event = sdk_event.model_dump(mode="json", exclude_none=True)
                kind = event.get("type")
                if kind == "message_start":
                    if final is not None:
                        raise invalid("Duplicate message_start.")
                    final = event["message"]
                    usage_data.update(final.get("usage") or {})
                    yield UsageUpdate(usage=anthropic_usage(usage_data))
                elif kind == "content_block_start":
                    index, block = event["index"], event["content_block"]
                    if index in blocks:
                        raise invalid("Duplicate content block index.")
                    blocks[index] = block
                    if block["type"] == "tool_use":
                        arguments[index] = ""
                        yield ToolCallDelta(call_id=block["id"], name=block["name"])
                elif kind == "content_block_delta":
                    index, delta = event["index"], event["delta"]
                    if index not in blocks or index in closed:
                        raise invalid("Content delta references a missing or closed block.")
                    block = blocks[index]
                    if delta["type"] == "text_delta":
                        block["text"] = block.get("text", "") + delta["text"]
                        yield TextDelta(text=delta["text"])
                    elif delta["type"] == "input_json_delta":
                        if index not in arguments:
                            raise invalid("JSON delta does not belong to a tool block.")
                        arguments[index] += delta["partial_json"]
                        yield ToolCallDelta(call_id=block["id"], arguments_delta=delta["partial_json"])
                    elif delta["type"] == "thinking_delta":
                        block["thinking"] = block.get("thinking", "") + delta["thinking"]
                    elif delta["type"] == "signature_delta":
                        block["signature"] = block.get("signature", "") + delta["signature"]
                    elif delta["type"] == "citations_delta":
                        block.setdefault("citations", []).append(delta["citation"])
                elif kind == "content_block_stop":
                    if event["index"] not in blocks or event["index"] in closed:
                        raise invalid("Content stop references a missing or closed block.")
                    closed.add(event["index"])
                elif kind == "message_delta":
                    if final is None:
                        raise invalid("Message delta arrived before message_start.")
                    final.update(event["delta"])
                    usage_data.update(event.get("usage") or {})
                    yield UsageUpdate(usage=anthropic_usage(usage_data))
                elif kind == "message_stop":
                    stopped = True
                    break
        if not stopped or final is None or closed != set(blocks):
            raise ProviderFailure(ProviderError(code="stream_interrupted", message="Messages stream ended before all blocks and message were confirmed."))
        reason = final.get("stop_reason")
        if reason not in {"end_turn", "tool_use", "stop_sequence", "refusal", "max_tokens"}:
            raise ProviderFailure(ProviderError(code="stream_interrupted", message="Unsupported or missing message stop reason."))
        # message_delta usage remains provisional until message_stop confirms
        # the response. A schema-invalid generation can still have exact billing.
        usage = anthropic_usage(usage_data)
        yield UsageUpdate(usage=usage, final=True)
        if reason == "max_tokens":
            raise ProviderFailure(ProviderError(code="stream_interrupted", message="Output token limit interrupted the response; tools withheld."))
        calls = []
        visible: list[str] = []
        seen: set[str] = set()
        for index in sorted(blocks):
            block = blocks[index]
            if block["type"] == "tool_use":
                call = validate_call(block.get("id", ""), block.get("name", ""), arguments[index] or block.get("input", {}), request.tools)
                if call.call_id in seen:
                    raise invalid("Provider repeated a tool call ID.")
                seen.add(call.call_id)
                block["input"] = call.arguments
                calls.append(call)
            elif block["type"] == "text":
                visible.append(block.get("text", ""))
        if calls and reason != "tool_use":
            raise invalid("Tool blocks were returned without a tool_use stop reason.")
        final["content"] = [blocks[index] for index in sorted(blocks)]
        final["usage"] = usage_data
        for call in calls:
            yield ToolCallReady(call=call)
        yield TurnCompleted(turn=ModelTurn(
            provider=self.provider, requested_model=request.model_id, reported_model=final.get("model"),
            response_id=final.get("id"), visible_text="".join(visible), complete_tool_calls=calls,
            usage=usage, finish_reason=reason,
            native_state=NativeState(provider=self.provider, model_id=request.model_id,
                                     items=[*items, {"role": "assistant", "content": final["content"]}]),
            raw_response=final,
        ))

    async def aclose(self) -> None:
        await self.client.close()
