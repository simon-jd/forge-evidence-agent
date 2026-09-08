# 0003: Preserve native state without translating opaque items

Status: accepted.

OpenAI Responses and Anthropic Messages are separate adapters over their native SDK streams. Canonical messages provide a portable transcript; native state preserves the originating provider/model and opaque continuation items. The latter are sent back only to their compatible adapter. A supported native continuation contains the complete previous native transcript, so the runtime appends only the new canonical tail.

Routing happens at a closed tool batch. Provider changes, stale file evidence and portable context compaction rebuild a request from public facts rather than translating opaque reasoning items. Native compaction is capability-gated, separately reserved and recorded as a paid operation. An uncertain compaction response pauses for reconciliation.

SDK automatic retries are disabled. The adapter owns bounded jittered retries before partial output; each attempt has a separate budget reservation. Partial JSON/tool previews never authorize execution. Only a complete durable ModelTurn can create tool operations.

Consequences: portable migration may lose information from native opaque state. We preserve provenance and declare that limitation rather than claiming equivalent reasoning across providers. Model IDs, capability declarations and prices are supplied and frozen by the user, not inferred from a mutable latest alias.
