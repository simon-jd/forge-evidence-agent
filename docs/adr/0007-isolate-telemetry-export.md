# 0007: Isolate trace export from the async worker control path

Status: accepted. Decision date: 2026-09-13.

The original `SimpleSpanProcessor` invokes JSONL and OTLP export during span completion. A slow collector can therefore occupy the same event-loop thread used by worker lease renewal and cancellation. The [dated market and ecosystem review](../research/market-2026-09-13.md) identifies recovery and reliable backend execution as relevant to this project's early-career AI Engineer positioning. This decision addresses the concrete coupling in Forge; it does not establish a market-wide framework requirement.

## Decision

Use the installed OpenTelemetry `BatchSpanProcessor`, with an independent bounded queue and exporter thread for each sink. JSONL remains enabled when OTLP is absent or fails. Default capacity is 256 queued spans per sink, with at most 32 spans in a batch; queued and in-flight spans are separate. Scheduling defaults to 200 ms. Settings reject invalid limits and a batch size larger than its queue. The SDK owns batching and overflow; Forge neither replaces its queue nor reads private queue attributes.

Pass the configured timeout directly to `OTLPSpanExporter(timeout=...)`, defaulting to two seconds. `BatchSpanProcessor.export_timeout_millis` does not enforce that network timeout in the installed SDK. The exporter governs its HTTP requests and retries; this setting is not a hard deadline for arbitrary injected exporters, DNS, local filesystem calls or process shutdown. Export failures and initialization failures are counted without propagating exception text into the worker. A local path failure and an unavailable remote sink remain independent failure states.

`Telemetry.close()` preserves the synchronous API and drains SDK queues on a best-effort basis. Worker lifecycle code awaits `aclose()`, which delegates that work to `asyncio.to_thread`. Waiting for shutdown therefore occurs outside the event-loop thread. Cancelling the awaiter does not terminate a running thread; no arbitrary exporter thread-kill or hard shutdown guarantee is made. Graceful worker exit records a final `telemetry.closed` snapshot.

## Measurements and privacy boundary

Forge records exported/failed span and batch counts, in-flight spans, and initialization/shutdown failures separately for `local` and `otlp`. Export success describes the exporter's return value, not durable exactly-once delivery. SDK processed-item counters alone are insufficient: version 1.44 does not classify an exporter returning `FAILURE` the same way as an exception.

For queue size and drops, each processor receives its own public `MeterProvider` and `InMemoryMetricReader`. The documented `OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED=true` flag must be exported in the launching shell; loading Forge's `.env` with Pydantic Settings does not set SDK environment variables. When SDK queue measurements are absent or unreadable, their values are `null`, never an assumed zero. Diagnostics expose bounded sink labels rather than run IDs or raw SDK component identifiers. Concurrent snapshots are not atomic across the SDK and exporter counters.

Trace names and attribute keys are allowlisted, string lengths and attribute counts are bounded, and parent trace/span correlation remains available. Automatic exception events and status descriptions are disabled. Resource construction does not import arbitrary resource environment attributes or host detectors.

The real OTLP SDK logs transport exceptions and collector response reasons before returning to Forge's exporter adapter. A process-wide logging filter on `opentelemetry.exporter.otlp.proto.http.trace_exporter` replaces diagnostic text with a constant message and removes arguments, exception information and stack text. A second filter on `opentelemetry.sdk._shared_internal` suppresses only the installed SDK's span queue-full and post-shutdown producer message templates with the `Span` argument. This prevents those records from reaching potentially blocking logging handlers on the producer path; queue drops remain available through the opt-in SDK measurements. Other SDK messages and application loggers are unaffected.

These filters use public Python logging APIs, install once under a lock, and remain after one instance closes so another instance stays protected. Their scope covers other users of these exact SDK loggers in the same process. Logger names and message templates are version-specific integration points covered by real-exporter and saturation regressions; future SDK upgrades must recheck them. No global logging factory or SDK-private queue API is modified.

## Dependency and operational consequences

Retain the tested lockfile versions: OpenTelemetry 1.44.0, FastAPI 0.141.1, LangGraph 1.2.11 and checkpoint-postgres 3.1.2. The official release and maintenance review found that the required stable processor/exporter interfaces already exist. A version bump would not move the synchronous network call off the worker's event loop. Framework or checkpoint migration would add API, replay and database compatibility work without addressing this fault, so this change introduces no dependency upgrade.

Bounded buffering can lose traces and adds background threads. JSONL is also best effort and is not a durable audit ledger. PostgreSQL action/usage records and execution receipts continue to own recovery decisions. A stalled custom exporter can still delay shutdown, and unrelated synchronous worker operations retain their existing behavior. The [validation record](../validation/telemetry-20260913/README.md) owns actual results and reproducible measurements. A controlled local collector experiment does not establish production throughput, cloud operation or faster model inference.
