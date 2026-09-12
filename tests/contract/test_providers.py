"""Native SDKs against synthetic SSE over MockTransport; no live model calls.

Fixtures model the published wire protocol and deliberately invalid/truncated
variants. They are contract/fault evidence, never evidence of agent quality.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from forge.context.budget import BudgetManager
from forge.domain.types import (
    BudgetLimits,
    CanonicalMessage,
    CompactRequest,
    ModelCapabilities,
    ModelRequest,
    ModelSpec,
    ModelTurn,
    NativeState,
    ProviderError,
    TokenUsage,
    ToolCall,
    ToolCallPart,
    ToolCallReady,
    ToolResultPart,
    ToolSpec,
    TurnCompleted,
    UsageUpdate,
)
from forge.providers import (
    AnthropicProvider,
    FakeProvider,
    OpenAIProvider,
    ProviderFailure,
    create_provider,
)
from forge.providers.deepseek import DeepSeekProvider

CAPS = {"test-model": ModelCapabilities(parallel_tool_calls=True, native_compaction=True,
                                        structured_output=True)}
TOOLS = [ToolSpec(name="read_file", description="Read a file", read_only=True,
                  parameters={"type": "object", "properties": {"path": {"type": "string", "minLength": 1}},
                              "required": ["path"], "additionalProperties": False})]


def request(**kwargs: Any) -> ModelRequest:
    return ModelRequest(run_id="run-test", turn_id="turn-test", model_id="test-model",
                        canonical_messages=kwargs.pop("canonical_messages", [CanonicalMessage.text("user", "Find bug.")]),
                        tools=TOOLS, **kwargs)


def encode_sse(events: list[dict[str, Any]]) -> bytes:
    return b"".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode() for event in events)


def openai_events(*, final: bool = True, usage: bool = True, calls: int = 2,
                  arguments: str = '{"path":"module.py"}') -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = [
        {"type": "response.created", "sequence_number": 0, "response": {"id": "resp-test", "status": "in_progress"}},
        {"type": "response.output_text.delta", "sequence_number": 1, "item_id": "msg-test", "output_index": 0,
         "content_index": 0, "delta": "Inspecting."},
    ]
    output: list[dict[str, Any]] = [
        {"id": "msg-test", "type": "message", "role": "assistant", "status": "completed",
         "content": [{"type": "output_text", "text": "Inspecting.", "annotations": []}]},
        {"id": "rs-test", "type": "reasoning", "summary": [], "encrypted_content": "opaque-test"},
    ]
    for i in range(calls):
        item = {"id": f"fc-{i}", "type": "function_call", "call_id": f"call-{i}", "name": "read_file", "arguments": ""}
        events.append({"type": "response.output_item.added", "sequence_number": len(events), "output_index": i + 2, "item": dict(item)})
        for chunk in (arguments[:5], arguments[5:12], arguments[12:]):
            events.append({"type": "response.function_call_arguments.delta", "sequence_number": len(events),
                           "item_id": item["id"], "output_index": i + 2, "delta": chunk})
        item["arguments"] = arguments
        events.append({"type": "response.function_call_arguments.done", "sequence_number": len(events),
                       "item_id": item["id"], "output_index": i + 2, "arguments": arguments})
        events.append({"type": "response.output_item.done", "sequence_number": len(events), "output_index": i + 2, "item": dict(item)})
        output.append(item)
    if final:
        response: dict[str, Any] = {"id": "resp-test", "object": "response", "created_at": 1,
                                   "status": "completed", "model": "test-model-reported", "output": output}
        if usage:
            response["usage"] = {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150,
                                  "input_tokens_details": {"cached_tokens": 20},
                                  "output_tokens_details": {"reasoning_tokens": 5}}
        events.append({"type": "response.completed", "sequence_number": len(events), "response": response})
    return events


def anthropic_events(*, final: bool = True, usage: bool = True, calls: int = 2,
                     arguments: str = '{"path":"module.py"}', reason: str | None = None) -> list[dict[str, Any]]:
    message: dict[str, Any] = {"id": "msg-test", "type": "message", "role": "assistant", "content": [],
                               "model": "test-model-reported", "stop_reason": None, "stop_sequence": None}
    if usage:
        message["usage"] = {"input_tokens": 90, "output_tokens": 1,
                            "cache_read_input_tokens": 20, "cache_creation_input_tokens": 10}
    events: list[dict[str, Any]] = [
        {"type": "message_start", "message": message},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Inspecting."}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "signature_delta", "signature": "signed-test"}},
        {"type": "content_block_stop", "index": 1},
    ]
    for i in range(calls):
        events.append({"type": "content_block_start", "index": i + 2,
                       "content_block": {"type": "tool_use", "id": f"call-{i}", "name": "read_file", "input": {}}})
        for chunk in (arguments[:5], arguments[5:12], arguments[12:]):
            events.append({"type": "content_block_delta", "index": i + 2,
                           "delta": {"type": "input_json_delta", "partial_json": chunk}})
        events.append({"type": "content_block_stop", "index": i + 2})
    delta: dict[str, Any] = {"type": "message_delta", "delta": {"stop_reason": reason or ("tool_use" if calls else "end_turn"), "stop_sequence": None}}
    if usage:
        delta["usage"] = {"output_tokens": 30}
    events.append(delta)
    if final:
        events.append({"type": "message_stop"})
    return events


def adapter(provider: str, handler: Callable[[httpx2.Request], httpx2.Response], **kwargs: Any) -> OpenAIProvider | AnthropicProvider:
    http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    if provider in {"openai", "deepseek"}:
        client = AsyncOpenAI(api_key="contract-placeholder", base_url="https://contract.invalid/v1", http_client=http_client, max_retries=9)
        if provider == "deepseek":
            return DeepSeekProvider(client=client, capabilities=CAPS, backoff_base_seconds=0, **kwargs)
        return OpenAIProvider(client=client, capabilities=CAPS, backoff_base_seconds=0, **kwargs)
    client_a = AsyncAnthropic(api_key="contract-placeholder", base_url="https://contract.invalid", http_client=http_client, max_retries=9)
    return AnthropicProvider(client=client_a, capabilities=CAPS, backoff_base_seconds=0, **kwargs)


def events_for(provider: str, **kwargs: Any) -> list[dict[str, Any]]:
    return (openai_events if provider == "openai" else anthropic_events)(**kwargs)


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_complete_stream_assembles_multiple_calls_usage_native_state(provider: str) -> None:
    received = []
    attempts = []

    async def sink(value: dict[str, Any]) -> None:
        attempts.append(value)

    def handler(req: httpx2.Request) -> httpx2.Response:
        received.append(json.loads(req.content))
        return httpx2.Response(200, content=encode_sse(events_for(provider)), headers={"content-type": "text/event-stream"})

    model = adapter(provider, handler, attempt_sink=sink)
    stream = [event async for event in model.stream(request())]
    assert isinstance(stream[-1], TurnCompleted), stream
    turn = stream[-1].turn
    assert turn.visible_text == "Inspecting."
    assert [call.arguments for call in turn.complete_tool_calls] == [{"path": "module.py"}] * 2
    assert len([event for event in stream if isinstance(event, ToolCallReady)]) == 2
    assert turn.usage.input_tokens == 120
    assert turn.usage.output_tokens == 30
    assert turn.usage.cached_input_tokens == 20
    assert turn.native_state is not None and turn.native_state.provider == provider
    native_json = turn.native_state.model_dump_json()
    assert ("opaque-test" if provider == "openai" else "signed-test") in native_json
    assert received[0]["stream"] is True
    assert model.client.max_retries == 0
    assert [attempt["status"] for attempt in attempts] == ["started", "completed"]
    assert attempts[-1]["usage"]["input_tokens"] == 120
    assert attempts[-1]["usage_final"] is True
    assert [event.final for event in stream if isinstance(event, UsageUpdate)].count(True) == 1
    await model.aclose()


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_torn_stream_withholds_even_closed_tool_calls(provider: str) -> None:
    calls = 0
    attempts = []

    async def sink(value: dict[str, Any]) -> None:
        attempts.append(value)

    def handler(req: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(200, content=encode_sse(events_for(provider, final=False)), headers={"content-type": "text/event-stream"})

    model = adapter(provider, handler, attempt_sink=sink)
    events = [event async for event in model.stream(request())]
    assert isinstance(events[-1], ProviderError) and events[-1].code == "stream_interrupted"
    assert not any(isinstance(event, (TurnCompleted, ToolCallReady)) for event in events)
    assert calls == 1
    assert attempts[-1]["usage_final"] is False
    assert attempts[-1]["usage"]["estimated"] is True
    assert not any(isinstance(event, UsageUpdate) and event.final for event in events)
    if provider == "anthropic":
        # Cumulative message_delta counters do not settle a missing message_stop.
        assert attempts[-1]["usage"]["input_tokens"] == 120
        assert attempts[-1]["usage"]["output_tokens"] == 30
    await model.aclose()


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("arguments", ['{"path":', '{"path":42}', '{"path":"x","unexpected":true}', '[]'])
async def test_invalid_arguments_never_become_executable(provider: str, arguments: str) -> None:
    model = adapter(provider, lambda req: httpx2.Response(200, content=encode_sse(events_for(provider, arguments=arguments)), headers={"content-type": "text/event-stream"}))
    events = [event async for event in model.stream(request())]
    assert isinstance(events[-1], ProviderError) and events[-1].code == "invalid_tool_arguments"
    assert not any(isinstance(event, (ToolCallReady, TurnCompleted)) for event in events)
    await model.aclose()


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_missing_usage_remains_unknown(provider: str) -> None:
    model = adapter(provider, lambda req: httpx2.Response(200, content=encode_sse(events_for(provider, usage=False, calls=0)), headers={"content-type": "text/event-stream"}))
    events = [event async for event in model.stream(request())]
    assert isinstance(events[-1], TurnCompleted), events
    assert events[-1].turn.usage.input_tokens is None
    assert events[-1].turn.usage.output_tokens is None
    await model.aclose()


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("status,expected,attempt_count", [(401, "authentication_error", 1), (400, "invalid_request", 1), (429, "rate_limited", 3), (503, "transient_provider_error", 3)])
async def test_retries_are_bounded_and_every_attempt_accounted(provider: str, status: int, expected: str, attempt_count: int) -> None:
    requests = []
    records = []

    async def sink(value: dict[str, Any]) -> None:
        records.append(value)

    def handler(req: httpx2.Request) -> httpx2.Response:
        requests.append(req)
        return httpx2.Response(status, json={"error": {"type": "api_error", "message": "synthetic"}}, headers={"retry-after": "0"})

    model = adapter(provider, handler, attempt_sink=sink)
    events = [event async for event in model.stream(request())]
    assert isinstance(events[-1], ProviderError) and events[-1].code == expected, events
    assert len(requests) == attempt_count
    assert len(records) == attempt_count * 2
    assert len({record["attempt_id"] for record in records}) == attempt_count
    assert all(record["usage"]["input_tokens"] is None for record in records)
    await model.aclose()


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_native_state_cannot_cross_provider_or_model(provider: str) -> None:
    def forbidden(req: httpx2.Request) -> httpx2.Response:
        raise AssertionError("Invalid continuation must be rejected before HTTP.")

    model = adapter(provider, forbidden)
    for state in [NativeState(provider="other", model_id="test-model", items=[{"type": "opaque"}]),
                  NativeState(provider=provider, model_id="different-model", items=[{"type": "opaque"}])]:
        events = [event async for event in model.stream(request(native_state=state))]
        assert len(events) == 1 and isinstance(events[0], ProviderError)
        assert events[0].code == "invalid_request"
    await model.aclose()


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_unsupported_capability_and_sampling_fail_explicitly(provider: str) -> None:
    def forbidden(req: httpx2.Request) -> httpx2.Response:
        raise AssertionError("Unsupported capability must be rejected before HTTP.")

    model = adapter(provider, forbidden)
    for req in [request(requested_features=["native_async_tools"]), request(sampling_config={"invented_knob": 1})]:
        events = [event async for event in model.stream(req)]
        assert isinstance(events[-1], ProviderError) and events[-1].code == "unsupported_capability"
    await model.aclose()


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_native_continuation_sends_tool_results_and_preserves_opaque_blocks(provider: str) -> None:
    received = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        received.append(json.loads(req.content))
        return httpx2.Response(200, content=encode_sse(events_for(provider, calls=1 if len(received) == 1 else 0)), headers={"content-type": "text/event-stream"})

    model = adapter(provider, handler)
    first = [event async for event in model.stream(request())]
    assert isinstance(first[-1], TurnCompleted)
    state = first[-1].turn.native_state
    second = [event async for event in model.stream(request(native_state=state, canonical_messages=[
        CanonicalMessage(role="tool", parts=[ToolResultPart(call_id="call-0", content="file contents")])]))]
    assert isinstance(second[-1], TurnCompleted), second
    body = json.dumps(received[-1])
    assert ("opaque-test" if provider == "openai" else "signed-test") in body
    assert "file contents" in body
    assert body.count("Find bug.") == 1
    await model.aclose()


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_pending_or_orphan_tool_transcript_rejected(provider: str) -> None:
    def forbidden(req: httpx2.Request) -> httpx2.Response:
        raise AssertionError("Invalid transcript must be rejected before HTTP.")

    model = adapter(provider, forbidden)
    for message in [CanonicalMessage(role="assistant", parts=[ToolCallPart(call=ToolCall(call_id="call-missing", name="read_file", arguments={"path": "file.py"}))]),
                    CanonicalMessage(role="tool", parts=[ToolResultPart(call_id="call-missing", content="orphan")])]:
        events = [event async for event in model.stream(request(canonical_messages=[message]))]
        assert isinstance(events[-1], ProviderError) and events[-1].code == "invalid_request"
    await model.aclose()


async def test_compaction_retains_opaque_output_window() -> None:
    bodies = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        assert req.url.path == "/v1/responses/compact"
        bodies.append(json.loads(req.content))
        return httpx2.Response(200, json={"id": "cmp-test", "object": "response.compaction", "created_at": 1,
                                          "output": [{"type": "compaction", "id": "cmp-item", "encrypted_content": "opaque-compact"}],
                                          "usage": {"input_tokens": 300, "output_tokens": 30, "total_tokens": 330}})

    model = adapter("openai", handler)
    result = await model.compact(CompactRequest(model_id="test-model", canonical_messages=[CanonicalMessage.text("user", "Keep constraints")]))
    assert result.native_state.items[0]["encrypted_content"] == "opaque-compact"
    assert result.usage.input_tokens == 300
    assert bodies[0]["instructions"]
    await model.aclose()


async def test_anthropic_native_compaction_is_explicitly_unsupported() -> None:
    model = adapter("anthropic", lambda req: httpx2.Response(500))
    assert not model.capabilities("test-model").native_compaction
    with pytest.raises(ProviderFailure) as error:
        await model.compact(CompactRequest(model_id="test-model", canonical_messages=[]))
    assert error.value.error.code == "unsupported_capability"
    await model.aclose()


async def test_fake_requires_explicit_script_and_never_invents_completion() -> None:
    with pytest.raises(ValueError, match="explicit"):
        create_provider(ModelSpec(provider="fake", model_id="test-model"))
    turn = ModelTurn(provider="fake", requested_model="test-model", finish_reason="stop",
                     visible_text="Scripted", usage=TokenUsage(input_tokens=0, output_tokens=0))
    fake = FakeProvider([turn])
    first = [event async for event in fake.stream(request())]
    assert isinstance(first[-1], TurnCompleted)
    assert first[-1].turn.raw_response["execution_mode"] == "offline_scripted"
    second = [event async for event in fake.stream(request())]
    assert isinstance(second[-1], ProviderError) and second[-1].code == "invalid_request"
    assert len(fake.requests) == 1


class InterruptedBody(httpx2.AsyncByteStream):
    def __init__(self, payload: bytes):
        self.payload = payload

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.payload
        raise httpx2.ReadError("synthetic socket reset")


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_socket_reset_during_tool_stream_is_not_retried(provider: str) -> None:
    calls = 0

    def handler(req: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(200, stream=InterruptedBody(encode_sse(events_for(provider, final=False))), headers={"content-type": "text/event-stream"})

    model = adapter(provider, handler)
    events = [event async for event in model.stream(request())]
    assert isinstance(events[-1], ProviderError) and events[-1].code == "stream_interrupted"
    assert not any(isinstance(event, ToolCallReady) for event in events)
    assert calls == 1
    await model.aclose()


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_invalid_generated_call_still_records_billable_usage(provider: str) -> None:
    attempts = []

    async def sink(value: dict[str, Any]) -> None:
        attempts.append(value)

    model = adapter(provider, lambda req: httpx2.Response(200, content=encode_sse(events_for(provider, arguments='{"path":42}')),
                    headers={"content-type": "text/event-stream"}), attempt_sink=sink)
    events = [event async for event in model.stream(request())]
    assert isinstance(events[-1], ProviderError)
    assert attempts[-1]["status"] == "failed"
    assert attempts[-1]["usage"]["input_tokens"] == 120
    assert attempts[-1]["usage"]["output_tokens"] == 30
    assert attempts[-1]["usage"]["estimated"] is False
    assert attempts[-1]["usage_final"] is True
    assert events[-1].code == "invalid_tool_arguments"
    terminal_usage = [event for event in events if isinstance(event, UsageUpdate) and event.final]
    assert len(terminal_usage) == 1
    assert terminal_usage[0].usage.input_tokens == 120
    assert not any(isinstance(event, ToolCallReady) for event in events)
    await model.aclose()


async def test_anthropic_token_limit_withholds_tool_calls_and_retains_usage() -> None:
    model = adapter("anthropic", lambda req: httpx2.Response(200, content=encode_sse(anthropic_events(reason="max_tokens")),
                    headers={"content-type": "text/event-stream"}))
    events = [event async for event in model.stream(request())]
    assert isinstance(events[-1], ProviderError) and events[-1].code == "stream_interrupted"
    assert not any(isinstance(event, ToolCallReady) for event in events)
    await model.aclose()


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_rate_limit_then_success_has_two_separate_attempts(provider: str) -> None:
    calls = 0
    attempts = []

    async def sink(value: dict[str, Any]) -> None:
        attempts.append(value)

    def handler(req: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx2.Response(429, json={"error": {"type": "rate_limit_error", "message": "synthetic"}}, headers={"retry-after": "0"})
        return httpx2.Response(200, content=encode_sse(events_for(provider)), headers={"content-type": "text/event-stream"})

    model = adapter(provider, handler, attempt_sink=sink)
    events = [event async for event in model.stream(request())]
    assert isinstance(events[-1], TurnCompleted)
    assert [item["status"] for item in attempts] == ["started", "failed", "started", "completed"]
    assert attempts[1]["usage"]["input_tokens"] is None
    assert attempts[3]["usage"]["input_tokens"] == 120
    assert calls == 2
    await model.aclose()


class HangingBody(httpx2.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        await asyncio.Event().wait()
        yield b""  # pragma: no cover

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_deadline_closes_stream_and_keeps_usage_unknown(provider: str) -> None:
    body = HangingBody()
    attempts = []

    async def sink(value: dict[str, Any]) -> None:
        attempts.append(value)

    model = adapter(provider, lambda req: httpx2.Response(200, stream=body, headers={"content-type": "text/event-stream"}), attempt_sink=sink)
    events = [event async for event in model.stream(request(deadline_seconds=0.05))]
    assert isinstance(events[-1], ProviderError)
    assert attempts[-1]["usage"]["input_tokens"] is None
    assert attempts[-1]["usage_final"] is False
    assert body.closed
    await model.aclose()


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_cancel_propagates_and_records_unknown_attempt(provider: str) -> None:
    body = HangingBody()
    attempts = []
    entered = asyncio.Event()

    async def sink(value: dict[str, Any]) -> None:
        attempts.append(value)

    def handler(req: httpx2.Request) -> httpx2.Response:
        entered.set()
        return httpx2.Response(200, stream=body, headers={"content-type": "text/event-stream"})

    model = adapter(provider, handler, attempt_sink=sink)

    async def consume() -> list[Any]:
        return [event async for event in model.stream(request())]

    task = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert attempts[-1]["status"] == "cancelled"
    assert attempts[-1]["usage"]["input_tokens"] is None
    assert attempts[-1]["usage_final"] is False
    assert body.closed
    await model.aclose()


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_undeclared_generated_tool_is_feedback_error_with_final_usage(provider: str) -> None:
    attempts = []
    http_calls = 0

    async def sink(value: dict[str, Any]) -> None:
        attempts.append(value)

    frames = events_for(provider, calls=1)
    for event in frames:
        if provider == "openai":
            if "item" in event and event["item"].get("type") == "function_call":
                event["item"]["name"] = "undeclared_tool"
            for item in event.get("response", {}).get("output", []):
                if item.get("type") == "function_call":
                    item["name"] = "undeclared_tool"
        elif event.get("content_block", {}).get("type") == "tool_use":
            event["content_block"]["name"] = "undeclared_tool"

    def handler(req: httpx2.Request) -> httpx2.Response:
        nonlocal http_calls
        http_calls += 1
        return httpx2.Response(200, content=encode_sse(frames), headers={"content-type": "text/event-stream"})

    model = adapter(provider, handler, attempt_sink=sink)
    events = [event async for event in model.stream(request())]
    assert isinstance(events[-1], ProviderError)
    assert events[-1].code == "invalid_tool_arguments"
    assert events[-1].retryable is False
    assert http_calls == 1  # runtime, not the adapter, decides the next generation
    assert attempts[-1]["usage_final"] is True
    assert attempts[-1]["usage"]["estimated"] is False
    assert not any(isinstance(event, (ToolCallReady, TurnCompleted)) for event in events)
    await model.aclose()


def terminal_frames(kind: str, usage: dict[str, Any] | None, *, status: str | None = None) -> list[dict[str, Any]]:
    frames = openai_events(calls=1)
    final = frames[-1]["response"]
    final.update(status=status or kind.removeprefix("response."), usage=usage)
    # Both providers support these declared function items; no encrypted fixture
    # is needed to exercise the independent billing decision.
    final["output"] = [item for item in final["output"] if item["type"] != "reasoning"]
    frames[-1]["type"] = kind
    if kind == "response.failed":
        final["error"] = {"code": "server_error", "message": "Synthetic terminal failure"}
    else:
        final["incomplete_details"] = {"reason": "max_output_tokens"}
    return frames


def terminal_accounting_adapter(
    provider: str, frames: list[dict[str, Any]], *, disconnect: bool = False,
) -> tuple[Any, BudgetManager, list[dict[str, Any]], list[httpx2.Request]]:
    budget = BudgetManager(BudgetLimits(max_cost_usd=Decimal("1")))
    spec = ModelSpec(provider=provider, model_id="test-model",
        input_usd_per_million=Decimal(".30"), cached_input_usd_per_million=Decimal(".006"),
        output_usd_per_million=Decimal("1.20"), price_basis="upper_bound")
    attempts: list[dict[str, Any]] = []
    requests: list[httpx2.Request] = []

    async def sink(payload: dict[str, Any]) -> None:
        attempts.append(payload)
        if payload["status"] == "started":
            budget.reserve(payload["attempt_id"], spec, 2_000, 4_096)
        else:
            budget.reconcile(payload["attempt_id"], TokenUsage.model_validate(payload["usage"]))

    def handler(req: httpx2.Request) -> httpx2.Response:
        requests.append(req)
        payload = encode_sse(frames)
        if disconnect:
            return httpx2.Response(200, stream=InterruptedBody(payload), headers={"content-type": "text/event-stream"})
        return httpx2.Response(200, content=payload, headers={"content-type": "text/event-stream"})

    return adapter(provider, handler, attempt_sink=sink), budget, attempts, requests


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
@pytest.mark.parametrize("kind", ["response.incomplete", "response.failed"])
async def test_terminal_failure_with_complete_usage_settles_without_tools_or_retry(provider: str, kind: str) -> None:
    usage = {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150,
             "input_tokens_details": {"cached_tokens": 20},
             "output_tokens_details": {"reasoning_tokens": 5}}
    model, budget, attempts, requests = terminal_accounting_adapter(provider, terminal_frames(kind, usage))
    stream = [event async for event in model.stream(request())]
    assert isinstance(stream[-1], ProviderError) and stream[-1].code == "stream_interrupted"
    assert stream[-1].retryable is False
    assert not any(isinstance(event, (ToolCallReady, TurnCompleted)) for event in stream)
    assert len(requests) == 1 and [row["status"] for row in attempts] == ["started", "failed"]
    assert attempts[-1]["usage_final"] is True and attempts[-1]["usage"]["estimated"] is False
    assert budget.cost_status == "upper_bound" and budget.known_spend_usd == Decimal(".00006612")
    assert len(budget.state.reservations) == 1
    assert next(iter(budget.state.reservations.values())).status == "settled"
    await model.aclose()


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
@pytest.mark.parametrize("kind", ["response.incomplete", "response.failed"])
@pytest.mark.parametrize("usage", [None, {"input_tokens": 120}, {"output_tokens": 30},
                                    {"input_tokens_details": {"cached_tokens": 20}}])
async def test_terminal_failure_without_complete_usage_keeps_reservation(
    provider: str, kind: str, usage: dict[str, Any] | None,
) -> None:
    model, budget, attempts, requests = terminal_accounting_adapter(provider, terminal_frames(kind, usage))
    stream = [event async for event in model.stream(request())]
    assert isinstance(stream[-1], ProviderError) and stream[-1].code == "stream_interrupted"
    assert len(requests) == 1 and attempts[-1]["usage_final"] is False
    assert not any(isinstance(event, (ToolCallReady, TurnCompleted)) for event in stream)
    reservation = next(iter(budget.state.reservations.values()))
    assert budget.cost_status == "unknown" and budget.known_spend_usd == 0
    assert reservation.charged_usd == reservation.reserved_usd and reservation.status != "settled"
    await model.aclose()


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
@pytest.mark.parametrize("usage", [
    {"input_tokens": -1, "output_tokens": 30},
    {"input_tokens": 120, "output_tokens": 30, "input_tokens_details": {"cached_tokens": 121}},
    {"input_tokens": 120, "output_tokens": 30, "input_tokens_details": {"cached_tokens": 100, "cache_write_tokens": 21}},
    {"input_tokens": 120, "output_tokens": 30, "output_tokens_details": {"reasoning_tokens": 31}},
    {"input_tokens": 120, "output_tokens": 30, "total_tokens": 151},
])
async def test_terminal_invalid_usage_never_releases_a_reservation(provider: str, usage: dict[str, Any]) -> None:
    model, budget, attempts, requests = terminal_accounting_adapter(provider, terminal_frames("response.incomplete", usage))
    stream = [event async for event in model.stream(request())]
    assert isinstance(stream[-1], ProviderError)
    assert not any(isinstance(event, (ToolCallReady, TurnCompleted)) for event in stream)
    assert len(requests) == 1 and attempts[-1]["usage_final"] is False
    reservation = next(iter(budget.state.reservations.values()))
    assert reservation.charged_usd == reservation.reserved_usd and budget.cost_status == "unknown"
    await model.aclose()


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
async def test_terminal_event_with_nonterminal_status_cannot_finalize_billing(provider: str) -> None:
    frames = terminal_frames("response.incomplete", {"input_tokens": 120, "output_tokens": 30}, status="in_progress")
    model, budget, attempts, requests = terminal_accounting_adapter(provider, frames)
    stream = [event async for event in model.stream(request())]
    assert isinstance(stream[-1], ProviderError) and stream[-1].code == "invalid_request"
    assert len(requests) == 1 and attempts[-1]["usage_final"] is False
    assert budget.cost_status == "unknown"
    assert not any(isinstance(event, (ToolCallReady, TurnCompleted)) for event in stream)
    await model.aclose()


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
async def test_terminal_cache_write_usage_with_unknown_tariff_remains_reserved(provider: str) -> None:
    frames = terminal_frames("response.incomplete", {
        "input_tokens": 120, "output_tokens": 30, "total_tokens": 150,
        "input_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 10},
    })
    model, budget, attempts, requests = terminal_accounting_adapter(provider, frames)
    stream = [event async for event in model.stream(request())]
    assert isinstance(stream[-1], ProviderError)
    assert len(requests) == 1 and attempts[-1]["usage_final"] is True
    assert attempts[-1]["usage"]["cache_write_tokens"] == 10
    reservation = next(iter(budget.state.reservations.values()))
    assert reservation.status == "unknown" and reservation.charged_usd == reservation.reserved_usd
    assert budget.cost_status == "unknown" and budget.known_spend_usd == 0
    await model.aclose()


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
@pytest.mark.parametrize("disconnect", [False, True])
async def test_nonterminal_counters_and_closed_call_before_eof_do_not_finalize_billing(provider: str, disconnect: bool) -> None:
    frames = openai_events(final=False, calls=1)
    frames[0]["response"]["usage"] = {"input_tokens": 120, "output_tokens": 30}
    model, budget, attempts, requests = terminal_accounting_adapter(provider, frames, disconnect=disconnect)
    stream = [event async for event in model.stream(request())]
    assert isinstance(stream[-1], ProviderError) and stream[-1].code == "stream_interrupted"
    assert not any(isinstance(event, (ToolCallReady, TurnCompleted)) for event in stream)
    assert len(requests) == 1 and attempts[-1]["usage_final"] is False
    assert budget.cost_status == "unknown"
    reservation = next(iter(budget.state.reservations.values()))
    assert reservation.charged_usd == reservation.reserved_usd
    await model.aclose()


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_missing_call_identity_stays_a_protocol_error(provider: str) -> None:
    frames = events_for(provider, calls=1)
    for event in frames:
        if provider == "openai":
            for item in event.get("response", {}).get("output", []):
                if item.get("type") == "function_call":
                    item["call_id"] = ""
        elif event.get("content_block", {}).get("type") == "tool_use":
            event["content_block"]["id"] = ""
    model = adapter(provider, lambda req: httpx2.Response(200, content=encode_sse(frames),
                                                       headers={"content-type": "text/event-stream"}))
    events = [event async for event in model.stream(request())]
    assert isinstance(events[-1], ProviderError)
    assert events[-1].code == "invalid_request"
    assert not any(isinstance(event, (ToolCallReady, TurnCompleted)) for event in events)
    await model.aclose()
