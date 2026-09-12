"""Real installed SDK against synthetic DeepSeek Responses SSE, without paid calls."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from openai import AsyncOpenAI

from forge.domain.types import (
    CanonicalMessage,
    CompactRequest,
    ModelCapabilities,
    ModelRequest,
    NativeState,
    ProviderError,
    ToolCall,
    ToolCallPart,
    ToolCallReady,
    ToolResultPart,
    ToolSpec,
    TurnCompleted,
    UsageUpdate,
)
from forge.providers.base import ProviderFailure
from forge.providers.deepseek import DEEPSEEK_BASE_URL, DeepSeekProvider

MODEL = "frozen-contract-model"
CAPS = {MODEL: ModelCapabilities(parallel_tool_calls=True, native_compaction=True,
                                structured_output=True)}
TOOL = ToolSpec(name="read_file", description="Read a file", read_only=True,
                parameters={"type": "object", "properties": {"path": {"type": "string"}},
                            "required": ["path"], "additionalProperties": False})


def request(**kwargs: Any) -> ModelRequest:
    return ModelRequest(run_id="deepseek-contract", turn_id="turn-contract", model_id=MODEL,
                        tools=[TOOL], canonical_messages=kwargs.pop("canonical_messages", [
                            CanonicalMessage.text("system", "Keep the repository constraints."),
                            CanonicalMessage.text("user", "Find the bug."),
                        ]), **kwargs)


def events(*, final: bool = True, calls: int = 2, arguments: str = '{"path":"module.py"}',
           extra_output: list[dict[str, Any]] | None = None, usage: bool = True) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = [{"type": "response.created", "sequence_number": 0,
                                   "response": {"id": "resp-contract", "status": "in_progress"}}]
    output: list[dict[str, Any]] = [
        {"id": "reason-contract", "type": "reasoning", "summary": [],
         "content": [{"type": "reasoning_text", "text": "Synthetic private reasoning fixture."}]},
        {"id": "message-contract", "type": "message", "role": "assistant", "status": "completed",
         "content": [{"type": "output_text", "text": "Inspecting.", "annotations": []}]},
    ]
    for index in range(calls):
        item = {"id": f"function-{index}", "type": "function_call", "call_id": f"call-{index}",
                "name": TOOL.name, "arguments": ""}
        result.append({"type": "response.output_item.added", "sequence_number": len(result),
                       "output_index": index + 2, "item": dict(item)})
        for chunk in (arguments[:6], arguments[6:]):
            result.append({"type": "response.function_call_arguments.delta", "sequence_number": len(result),
                           "output_index": index + 2, "item_id": item["id"], "delta": chunk})
        item["arguments"] = arguments
        result.append({"type": "response.function_call_arguments.done", "sequence_number": len(result),
                       "output_index": index + 2, "item_id": item["id"], "arguments": arguments})
        output.append(item)
    if final:
        response: dict[str, Any] = {"id": "resp-contract", "object": "response", "created_at": 1,
                                   "model": "reported-contract-model", "status": "completed",
                                   "output": output + (extra_output or [])}
        if usage:
            response["usage"] = {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150,
                                 "input_tokens_details": {"cached_tokens": 20},
                                 "output_tokens_details": {"reasoning_tokens": 5}}
        result.append({"type": "response.completed", "sequence_number": len(result), "response": response})
    return result


def response(**kwargs: Any) -> httpx2.Response:
    content = b"".join(f"event: {item['type']}\ndata: {json.dumps(item)}\n\n".encode()
                       for item in events(**kwargs))
    return httpx2.Response(200, content=content, headers={"content-type": "text/event-stream"})


def adapter(handler: Callable[[httpx2.Request], httpx2.Response], **kwargs: Any) -> DeepSeekProvider:
    client = AsyncOpenAI(api_key="deepseek-contract-placeholder", base_url=DEEPSEEK_BASE_URL,
                         http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
                         max_retries=9)
    return DeepSeekProvider(client=client, capabilities=CAPS, backoff_base_seconds=0, **kwargs)


async def test_sdk_transport_uses_deepseek_identity_and_narrow_request_contract() -> None:
    received: list[httpx2.Request] = []
    attempts = []

    async def sink(record: dict[str, Any]) -> None:
        attempts.append(record)

    def handler(req: httpx2.Request) -> httpx2.Response:
        received.append(req)
        return response()

    model = adapter(handler, attempt_sink=sink)
    stream = [event async for event in model.stream(request())]
    assert isinstance(stream[-1], TurnCompleted), stream
    turn = stream[-1].turn
    assert turn.provider == "deepseek"
    assert turn.requested_model == MODEL
    assert turn.reported_model == "reported-contract-model"
    assert turn.native_state and turn.native_state.provider == "deepseek"
    assert [call.arguments for call in turn.complete_tool_calls] == [{"path": "module.py"}] * 2
    assert len([event for event in stream if isinstance(event, ToolCallReady)]) == 2
    assert turn.usage.input_tokens == 120  # Includes the 20 cached tokens, not 120 + 20.
    assert turn.usage.cached_input_tokens == 20
    assert turn.usage.output_tokens == 30  # Includes the five reasoning tokens.
    assert turn.usage.reasoning_tokens == 5
    assert received[0].url.host == "api.deepseek.com"
    assert received[0].url.path == "/responses"
    assert received[0].headers["Authorization"] == "Bearer deepseek-contract-placeholder"
    body = json.loads(received[0].content)
    assert body["model"] == MODEL and body["reasoning"] == {"effort": "none"}
    assert set(body) == {"model", "input", "tools", "max_output_tokens", "stream", "reasoning"}
    assert model.client.max_retries == 0
    assert attempts[-1]["provider"] == "deepseek" and attempts[-1]["usage_final"] is True
    await model.aclose()


async def test_default_constructor_isolates_openai_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-environment-placeholder")
    monkeypatch.setenv("OPENAI_API_KEY", "wrong-provider-placeholder")
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "wrong-admin-placeholder")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://wrong-provider.invalid")
    monkeypatch.setenv("OPENAI_ORG_ID", "wrong-org-placeholder")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "wrong-project-placeholder")
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "authorization: Bearer wrong-header-placeholder\nX-Private: wrong-private-placeholder")
    received: list[httpx2.Request] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        received.append(req)
        return response(calls=0)

    http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))

    def create_sdk(**kwargs: Any) -> AsyncOpenAI:
        return AsyncOpenAI(http_client=http_client, **kwargs)

    monkeypatch.setattr("forge.providers.deepseek.AsyncOpenAI", create_sdk)
    model = DeepSeekProvider(capabilities=CAPS)
    stream = [event async for event in model.stream(request())]
    assert isinstance(stream[-1], TurnCompleted), stream
    assert received[0].url.host == "api.deepseek.com"
    assert received[0].headers["authorization"] == "Bearer deepseek-environment-placeholder"
    assert not {"openai-organization", "openai-project", "x-private"} & set(received[0].headers)
    assert "wrong-" not in str(received[0].headers)
    await model.aclose()


def test_missing_deepseek_key_never_falls_back_to_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "wrong-provider-placeholder")
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        DeepSeekProvider()
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        DeepSeekProvider(api_key=" ")


async def test_stateless_continuation_resends_complete_history_and_plain_reasoning() -> None:
    bodies = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(req.content))
        return response(calls=1 if len(bodies) == 1 else 0)

    model = adapter(handler)
    first = [event async for event in model.stream(request())]
    assert isinstance(first[-1], TurnCompleted)
    state = first[-1].turn.native_state
    second = [event async for event in model.stream(request(native_state=state, canonical_messages=[
        CanonicalMessage(role="tool", parts=[ToolResultPart(call_id="call-0", content="Observed contents.")]),
    ]))]
    assert isinstance(second[-1], TurnCompleted), second
    body = json.dumps(bodies[1])
    assert body.count("Find the bug.") == 1 and "Keep the repository constraints." in body
    assert "Observed contents." in body and "Synthetic private reasoning fixture." in body
    assert "previous_response_id" not in body and "encrypted_content" not in body
    assert "summary" not in body
    assert state and state.items[2]["summary"] == []  # The persisted source state was not mutated.
    await model.aclose()


@pytest.mark.parametrize("native", [
    NativeState(provider="openai", model_id=MODEL, items=[{"role": "user", "content": "x"}]),
    NativeState(provider="anthropic", model_id=MODEL, items=[{"role": "user", "content": "x"}]),
    NativeState(provider="deepseek", model_id="other-model", items=[{"role": "user", "content": "x"}]),
    NativeState(provider="deepseek", model_id=MODEL, items=[{"role": "user", "content": "x"}], continuation_id="resp-old"),
    NativeState(provider="deepseek", model_id=MODEL, items=[{"type": "compaction", "encrypted_content": "opaque"}]),
    NativeState(provider="deepseek", model_id=MODEL, items=[{"type": "reasoning", "encrypted_content": "opaque"}]),
    NativeState(provider="deepseek", model_id=MODEL, items=[{"type": "reasoning", "summary": [{"type": "summary_text", "text": "x"}]}]),
])
async def test_unsupported_native_state_fails_before_http_or_reservation(native: NativeState) -> None:
    async def forbidden_sink(record: dict[str, Any]) -> None:
        pytest.fail("Invalid state must fail before reserving a billable attempt.")

    model = adapter(lambda req: pytest.fail("Invalid state must not reach HTTP."), attempt_sink=forbidden_sink)
    stream = [event async for event in model.stream(request(native_state=native))]
    assert len(stream) == 1 and isinstance(stream[0], ProviderError)
    assert stream[0].code in {"invalid_request", "unsupported_capability"}
    await model.aclose()


@pytest.mark.parametrize("message", [
    CanonicalMessage(role="assistant", parts=[ToolCallPart(call=ToolCall(call_id="unresolved", name=TOOL.name, arguments={"path": "x"}))]),
    CanonicalMessage(role="tool", parts=[ToolResultPart(call_id="orphan", content="x")]),
])
async def test_responses_pairing_is_validated_as_responses_not_anthropic(message: CanonicalMessage) -> None:
    model = adapter(lambda req: pytest.fail("Unpaired history must not reach HTTP."))
    stream = [event async for event in model.stream(request(canonical_messages=[message]))]
    assert len(stream) == 1 and isinstance(stream[0], ProviderError) and stream[0].code == "invalid_request"
    await model.aclose()


async def test_torn_stream_withholds_complete_arguments_without_retry() -> None:
    requests = []
    attempts = []

    async def sink(record: dict[str, Any]) -> None:
        attempts.append(record)

    def handler(req: httpx2.Request) -> httpx2.Response:
        requests.append(req)
        return response(final=False)

    model = adapter(handler, attempt_sink=sink)
    stream = [event async for event in model.stream(request())]
    assert isinstance(stream[-1], ProviderError) and stream[-1].code == "stream_interrupted"
    assert not any(isinstance(event, (ToolCallReady, TurnCompleted)) for event in stream)
    assert len(requests) == 1 and attempts[-1]["usage_final"] is False
    assert attempts[-1]["usage"]["estimated"] is True
    await model.aclose()


@pytest.mark.parametrize("arguments", ['{"path":', '{"path":42}', '{"path":"x","extra":true}', '[]'])
async def test_schema_feedback_withholds_tools_but_preserves_confirmed_billing(arguments: str) -> None:
    attempts = []

    async def sink(record: dict[str, Any]) -> None:
        attempts.append(record)

    model = adapter(lambda req: response(arguments=arguments), attempt_sink=sink)
    stream = [event async for event in model.stream(request())]
    assert isinstance(stream[-1], ProviderError) and stream[-1].code == "invalid_tool_arguments"
    assert not any(isinstance(event, (ToolCallReady, TurnCompleted)) for event in stream)
    assert attempts[-1]["usage_final"] is True and attempts[-1]["usage"]["estimated"] is False
    assert attempts[-1]["usage"]["input_tokens"] == 120
    assert attempts[-1]["usage"]["cached_input_tokens"] == 20
    assert any(isinstance(event, UsageUpdate) and event.final for event in stream)
    await model.aclose()


async def test_unknown_usage_is_not_reported_as_zero() -> None:
    model = adapter(lambda req: response(usage=False, calls=0))
    stream = [event async for event in model.stream(request())]
    assert isinstance(stream[-1], TurnCompleted)
    assert stream[-1].turn.usage.input_tokens is None and stream[-1].turn.usage.output_tokens is None
    await model.aclose()


async def test_unadvertised_custom_output_withholds_the_entire_tool_batch() -> None:
    model = adapter(lambda req: response(extra_output=[{
        "type": "custom_tool_call", "id": "custom-contract", "call_id": "custom-call",
        "name": "apply_patch", "input": "not a function tool",
    }]))
    stream = [event async for event in model.stream(request())]
    assert isinstance(stream[-1], ProviderError) and stream[-1].code == "invalid_tool_arguments"
    assert not any(isinstance(event, (ToolCallReady, TurnCompleted)) for event in stream)
    await model.aclose()


@pytest.mark.parametrize("config", [
    {"store": False}, {"include": []}, {"previous_response_id": "resp-old"},
    {"parallel_tool_calls": False}, {"reasoning": {"effort": "medium"}},
    {"reasoning": {"effort": "low", "summary": "auto"}}, {"reasoning": "none"},
    {"reasoning": {"effort": "high"}, "temperature": 0}, {"top_p": 1},
    {"text": {"format": {"type": "text"}, "verbosity": "low"}},
    {"reasoning": {"effort": "low"}, "top_p": 0.5}, {"temperature": 3},
    {"temperature": True}, {"tool_choice": {"type": "function", "name": "undeclared"}},
])
async def test_ignored_or_unsupported_options_fail_before_http(config: dict[str, Any]) -> None:
    model = adapter(lambda req: pytest.fail("Unsupported parameters must not reach HTTP."))
    stream = [event async for event in model.stream(request(sampling_config=config))]
    assert len(stream) == 1 and isinstance(stream[0], ProviderError)
    assert stream[0].code in {"invalid_request", "unsupported_capability"}
    await model.aclose()


async def test_supported_sampling_preserves_semantics_without_ignored_parallel_flag() -> None:
    bodies = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(req.content))
        return response(calls=0)

    model = adapter(handler)
    config = {"reasoning": {"effort": "low"}, "top_p": 0.97, "parallel_tool_calls": True,
              "text": {"format": {"type": "json_object"}}, "tool_choice": "none"}
    stream = [event async for event in model.stream(request(sampling_config=config))]
    assert isinstance(stream[-1], TurnCompleted), stream
    assert bodies[0]["reasoning"] == {"effort": "low"} and bodies[0]["top_p"] == 0.97
    assert bodies[0]["text"] == {"format": {"type": "json_object"}}
    assert "parallel_tool_calls" not in bodies[0] and config["parallel_tool_calls"] is True
    await model.aclose()


async def test_native_compaction_is_blocked_even_if_misconfigured() -> None:
    model = adapter(lambda req: pytest.fail("DeepSeek has no native compaction endpoint."))
    assert not model.capabilities(MODEL).native_compaction
    with pytest.raises(ProviderFailure) as error:
        await model.compact(CompactRequest(model_id=MODEL, canonical_messages=[]))
    assert error.value.error.code == "unsupported_capability"
    stream = [event async for event in model.stream(request(requested_features=["native_compaction"]))]
    assert isinstance(stream[0], ProviderError) and stream[0].code == "unsupported_capability"
    await model.aclose()


async def test_input_bound_includes_unicode_schema_tool_result_and_native_history() -> None:
    model = adapter(lambda req: pytest.fail("Input estimation must never make a request."))
    schema = TOOL.model_copy(deep=True)
    schema.parameters["description"] = "schema details " * 2_000
    native = NativeState(provider="deepseek", model_id=MODEL, items=[
        {"role": "user", "content": "多语言上下文🧪" * 1_000},
        {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "private history " * 1_000}]},
        {"type": "function_call", "call_id": "native-call", "name": TOOL.name,
         "arguments": '{"path":"module.py"}'},
    ])
    req = request(native_state=native, canonical_messages=[
        CanonicalMessage(role="tool", parts=[ToolResultPart(call_id="native-call", content="文件内容😀" * 3_000)]),
    ]).model_copy(update={"tools": [schema]})
    bound = model.input_token_bound(req)
    actual_json_bytes = len(json.dumps(model._params(req), ensure_ascii=False, separators=(",", ":")).encode())
    assert bound >= actual_json_bytes + 1_024
    assert bound > 100_000  # Neither Unicode, native items, output text nor schema was omitted.
    assert model.input_token_bound(request()) < bound
    await model.aclose()


async def test_one_retry_layer_and_attempt_usage_are_preserved() -> None:
    received = []
    attempts = []

    async def sink(record: dict[str, Any]) -> None:
        attempts.append(record)

    def handler(req: httpx2.Request) -> httpx2.Response:
        received.append(req)
        return httpx2.Response(429, json={"error": {"code": "rate_limit_exceeded", "message": "synthetic"}},
                              headers={"retry-after": "0"})

    model = adapter(handler, attempt_sink=sink, max_attempts=2)
    stream = [event async for event in model.stream(request())]
    assert isinstance(stream[-1], ProviderError) and stream[-1].code == "rate_limited"
    assert len(received) == 2 and len(attempts) == 4
    assert len({record["attempt_id"] for record in attempts}) == 2
    assert all(record["provider"] == "deepseek" for record in attempts)
    assert all(record["usage"]["input_tokens"] is None for record in attempts)
    await model.aclose()
