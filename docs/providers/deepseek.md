# DeepSeek provider

The adapter uses DeepSeek's stateless Responses endpoint through the installed
OpenAI SDK. Provider identity, model configuration, credentials and accounting
remain `deepseek`. The adapter contains no model aliases or prices.

The [official Responses compatibility guide](https://api-docs.deepseek.com/guides/responses_api/)
was checked on 2026-09-11. DeepSeek requires the complete input history, does not
support server continuation or encrypted reasoning, and always permits multiple
tool calls. Forge resends local native items, checks call/result pairing and keeps
execution concurrency under the runtime's control. Native compaction is disabled;
portable compaction remains available.

The default `reasoning.effort` is `none` for the bounded experiment. Explicit
`low`, `high` and `max` are accepted. Ignored settings are rejected locally rather
than silently changing the experiment: temperature with thinking enabled,
top_p with thinking disabled, reasoning summaries, text verbosity and disabling
parallel calls. See the [official thinking-mode semantics](https://api-docs.deepseek.com/guides/thinking_mode/).

Use `DEEPSEEK_API_KEY` or pass a key explicitly. The production constructor fixes
the origin to `https://api.deepseek.com`; OpenAI account identifiers and ambient
custom headers are omitted. An explicitly injected SDK client is supported for
transport tests. It remains the caller's responsibility to supply a trusted client.

Final usage is settled only after the provider's terminal confirmation. Cached
input is a subset of input tokens; reasoning is a subset of output tokens, so
neither is added twice. A torn stream keeps its reservation unresolved. Confirmed
but invalid function arguments retain final usage and become bounded model
feedback, with no executable tool batch. These rules use the same reviewed
attempt and execution ledgers as the other adapters.

`input_token_bound()` estimates the complete serialized request in UTF-8 bytes
plus 1,024 bytes for framing, including tool schemas and native history. This is
a conservative estimate, not an exact tokenizer count or a billing guarantee.

`tests/contract/test_deepseek.py` uses the real installed `AsyncOpenAI` and
`httpx2.MockTransport`. It checks the host, payload, credential isolation, complete
history, usage, schema failures, interrupted streams and unsupported capabilities.
It makes no live API requests and cannot establish model quality or live service
availability. The experiment runner owns the separate cumulative dollar cap.
