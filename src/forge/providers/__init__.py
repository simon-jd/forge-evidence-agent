"""Native adapters and an explicitly offline simulator.

Model capability claims come from the frozen ModelSpec, intersected with adapter
implementation support. SDK defaults are never treated as free usage or pricing.
"""

from collections.abc import Sequence

from forge.domain.types import ModelSpec, ModelTurn
from forge.providers.anthropic import AnthropicProvider
from forge.providers.base import AttemptSink, ProviderAdapter, ProviderFailure
from forge.providers.fake import FakeProvider
from forge.providers.openai import OpenAIProvider

__all__ = ["AnthropicProvider", "FakeProvider", "OpenAIProvider", "ProviderAdapter",
           "ProviderFailure", "create_provider"]


def create_provider(spec: ModelSpec, *, scripted_turns: Sequence[ModelTurn] | None = None,
                    attempt_sink: AttemptSink | None = None, api_key: str | None = None) -> ProviderAdapter:
    capabilities = {spec.model_id: spec.capabilities}
    if spec.provider == "fake":
        if scripted_turns is None:
            raise ValueError("FakeProvider requires an explicit scripted_turns sequence.")
        return FakeProvider(scripted_turns, capabilities=capabilities, attempt_sink=attempt_sink)
    if scripted_turns is not None:
        raise ValueError("Scripted turns can only be used with the fake provider.")
    if spec.provider == "deepseek":
        from forge.providers.deepseek import DeepSeekProvider
        return DeepSeekProvider(api_key=api_key, capabilities=capabilities, attempt_sink=attempt_sink)
    if spec.provider == "openai":
        if api_key is not None:
            from openai import AsyncOpenAI
            return OpenAIProvider(client=AsyncOpenAI(api_key=api_key, max_retries=0),
                                  capabilities=capabilities, attempt_sink=attempt_sink)
        return OpenAIProvider(capabilities=capabilities, attempt_sink=attempt_sink)
    if spec.provider != "anthropic":
        raise ValueError("No adapter registered for this provider")
    if api_key is not None:
        from anthropic import AsyncAnthropic
        return AnthropicProvider(client=AsyncAnthropic(api_key=api_key, max_retries=0),
                                 capabilities=capabilities, attempt_sink=attempt_sink)
    return AnthropicProvider(capabilities=capabilities, attempt_sink=attempt_sink)
