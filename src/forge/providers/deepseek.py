"""DeepSeek's stateless Responses API, using the installed native OpenAI SDK.

Wire compatibility is narrower than OpenAI's API; it is not a base-URL override.
https://api-docs.deepseek.com/guides/responses_api/ (checked 2026-09-11)
"""

from __future__ import annotations

import copy
import json
import math
import os
from collections.abc import AsyncIterator, Mapping
from typing import Any, cast

from openai import AsyncOpenAI, Omit

from forge.domain.types import (
    CompactRequest,
    CompactResult,
    ModelCapabilities,
    ModelEvent,
    ModelRequest,
    ToolCallReady,
    TurnCompleted,
)
from forge.providers.base import (
    AttemptSink,
    check_pairing,
    invalid,
    invalid_tool,
    unsupported,
    validate_native,
)
from forge.providers.openai import OpenAIProvider, openai_items

DEEPSEEK_BASE_URL = "https://api.deepseek.com"


class DeepSeekProvider(OpenAIProvider):
    provider = "deepseek"
    implemented_features = frozenset({"tool_calling", "parallel_tool_calls", "structured_output"})

    def __init__(self, *, api_key: str | None = None, client: AsyncOpenAI | None = None,
                 capabilities: Mapping[str, ModelCapabilities] | None = None,
                 max_attempts: int = 3, attempt_sink: AttemptSink | None = None,
                 backoff_base_seconds: float = 0.25, backoff_cap_seconds: float = 8):
        if client is None:
            key = api_key if api_key is not None else os.environ.get("DEEPSEEK_API_KEY")
            if not key or not key.strip():
                raise ValueError("Set DEEPSEEK_API_KEY or supply a DeepSeek API key explicitly.")
            # OpenAI's SDK also reads ambient headers and account identifiers.
            # Explicit omission prevents those credentials reaching DeepSeek.
            headers: dict[str, Any] = {
                line.partition(":")[0].strip(): Omit()
                for line in os.environ.get("OPENAI_CUSTOM_HEADERS", "").splitlines()
                if ":" in line and line.partition(":")[0].strip()
            }
            headers.update({"Authorization": f"Bearer {key}",
                            "OpenAI-Organization": Omit(), "OpenAI-Project": Omit()})
            client = AsyncOpenAI(api_key=key, base_url=DEEPSEEK_BASE_URL, max_retries=0,
                                 organization="", project="", admin_api_key="", webhook_secret="",
                                 default_headers=cast(Mapping[str, str], headers))
        super().__init__(client=client, capabilities=capabilities, max_attempts=max_attempts,
                         attempt_sink=attempt_sink, backoff_base_seconds=backoff_base_seconds,
                         backoff_cap_seconds=backoff_cap_seconds)

    def validate(self, request: ModelRequest) -> None:
        super().validate(request)
        # Reject unsupported/ignored knobs before opening a billable attempt.
        self._params(request)

    def input_token_bound(self, request: ModelRequest) -> int:
        """Conservative byte estimate including complete history and tool schemas.

        This is not a provider tokenizer result. The 1,024-byte allowance covers
        request framing; the billing ledger retains its separate spending cap.
        """
        return len(json.dumps(self._params(request), ensure_ascii=False).encode("utf-8")) + 1_024

    def _input(self, request: ModelRequest | CompactRequest) -> list[dict[str, Any]]:
        validate_native(request.native_state, self.provider, request.model_id)
        state = request.native_state
        if state is not None and state.continuation_id is not None:
            raise unsupported("DeepSeek continuation requires full local history, not response IDs.")
        items = copy.deepcopy(state.items) if state is not None else []
        items.extend(openai_items(request.canonical_messages))
        for item in items:
            kind = item.get("type", "message")
            if kind not in {"message", "function_call", "function_call_output", "reasoning"}:
                raise unsupported("This DeepSeek adapter accepts text, reasoning and function-tool history only.")
            if "encrypted_content" in item:
                raise unsupported("DeepSeek does not support encrypted reasoning or compaction items.")
            if kind == "reasoning":
                if item.get("summary"):
                    raise unsupported("DeepSeek reasoning continuation requires content, not summaries.")
                item.pop("summary", None)
        # This helper selects a wire format, not the adapter/account identity.
        check_pairing(items, "openai")
        return items

    def _params(self, request: ModelRequest) -> dict[str, Any]:
        config = copy.deepcopy(request.sampling_config)
        allowed = {"temperature", "top_p", "reasoning", "text", "tool_choice", "parallel_tool_calls"}
        if set(config) - allowed:
            raise unsupported(f"Unsupported DeepSeek sampling options: {sorted(set(config) - allowed)}")
        reasoning = config.setdefault("reasoning", {"effort": "none"})
        if not isinstance(reasoning, dict) or set(reasoning) != {"effort"}:
            raise unsupported("DeepSeek reasoning configuration accepts only an explicit effort.")
        effort = reasoning["effort"]
        if effort not in ("none", "low", "high", "max"):
            raise unsupported("DeepSeek reasoning effort must be none, low, high or max.")
        if "parallel_tool_calls" in config:
            if config.pop("parallel_tool_calls") is not True:
                raise unsupported("DeepSeek always permits multiple tool calls; disable concurrency in the executor.")
        if "temperature" in config:
            if effort != "none":
                raise unsupported("DeepSeek ignores temperature in thinking mode; omit it.")
            self._number(config["temperature"], "temperature", 0, 2)
        if "top_p" in config:
            if effort == "none":
                raise unsupported("DeepSeek ignores top_p when thinking is disabled; omit it.")
            self._number(config["top_p"], "top_p", 0.95, 1)
        if "text" in config:
            if not self.capabilities(request.model_id).structured_output:
                raise unsupported("Text output configuration requires structured_output capability.")
            text = config["text"]
            if not isinstance(text, dict) or set(text) != {"format"} or not isinstance(text["format"], dict):
                raise unsupported("DeepSeek text configuration supports format only.")
        if "tool_choice" in config:
            choice = config["tool_choice"]
            if isinstance(choice, dict):
                if set(choice) != {"type", "name"} or choice.get("type") != "function" or not any(
                    tool.name == choice.get("name") for tool in request.tools
                ):
                    raise invalid("DeepSeek tool_choice must name a declared function tool.")
            elif choice not in ("none", "auto", "required"):
                raise unsupported("Unsupported DeepSeek tool_choice.")
        return {"model": request.model_id, "input": self._input(request),
                "tools": [{"type": "function", "name": tool.name, "description": tool.description,
                           "parameters": tool.parameters, "strict": False} for tool in request.tools],
                "max_output_tokens": request.output_limit, "stream": True, **config}

    @staticmethod
    def _number(value: Any, name: str, minimum: float, maximum: float) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
            raise invalid(f"DeepSeek {name} must be a finite number between {minimum} and {maximum}.")

    async def _stream_once(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        ready: list[ToolCallReady] = []
        async for event in super()._stream_once(request):
            if isinstance(event, ToolCallReady):
                ready.append(event)
            elif isinstance(event, TurnCompleted):
                for item in event.turn.raw_response.get("output", []):
                    if item.get("type") not in {"message", "reasoning", "function_call"}:
                        raise invalid_tool("DeepSeek returned an output item outside the declared function-tool protocol.")
                    if "encrypted_content" in item:
                        raise invalid("DeepSeek returned unsupported encrypted reasoning.")
                for call in ready:
                    yield call
                yield event
            else:
                yield event

    async def compact(self, request: CompactRequest) -> CompactResult:
        validate_native(request.native_state, self.provider, request.model_id)
        raise unsupported("DeepSeek has no native compaction endpoint; use portable context compaction.")
