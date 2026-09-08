"""Explicit scripted protocol simulator. Its output is never model-quality evidence."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Mapping, Sequence

from forge.domain.types import (
    ModelCapabilities,
    ModelEvent,
    ModelRequest,
    ModelTurn,
    TextDelta,
    ToolCallReady,
    TurnCompleted,
    UsageUpdate,
)
from forge.providers.base import AttemptSink, BaseProvider, invalid, validate_call


class FakeProvider(BaseProvider):
    provider = "fake"

    def __init__(self, turns: Sequence[ModelTurn], *,
                 capabilities: Mapping[str, ModelCapabilities] | None = None,
                 attempt_sink: AttemptSink | None = None):
        super().__init__(capabilities=capabilities, max_attempts=1, attempt_sink=attempt_sink)
        if any(turn.provider != "fake" for turn in turns):
            raise ValueError("Scripted turns must be labeled provider='fake'.")
        self._turns = deque(turn.model_copy(deep=True) for turn in turns)
        self._lock = asyncio.Lock()
        self.requests: list[ModelRequest] = []

    async def _stream_once(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        async with self._lock:
            if not self._turns:
                raise invalid("Offline scripted turn queue exhausted; no model was invoked.")
            turn = self._turns.popleft()
            self.requests.append(request.model_copy(deep=True))
        if turn.requested_model != request.model_id:
            raise invalid("Scripted turn model does not match the requested model.")
        calls = [validate_call(call.call_id, call.name, call.arguments, request.tools)
                 for call in turn.complete_tool_calls]
        if len({call.call_id for call in calls}) != len(calls):
            raise invalid("Scripted turn repeats a call ID.")
        turn.raw_response["execution_mode"] = "offline_scripted"
        if turn.visible_text:
            yield TextDelta(text=turn.visible_text)
        yield UsageUpdate(usage=turn.usage, final=True)
        for call in calls:
            yield ToolCallReady(call=call)
        yield TurnCompleted(turn=turn)
