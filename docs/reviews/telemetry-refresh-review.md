# Telemetry failure-isolation independent review

Review date: 2026-09-13, America/Los_Angeles. Scope: `src/forge/telemetry.py`, telemetry settings and worker shutdown wiring, the new worker regression, and `scripts/benchmark_telemetry.py`. This is a review of a personal project on a local workstation; no production deployment or paid model call was performed by the reviewer.

## Findings and fixes

1. **Resolved — real OTLP exporter diagnostics could disclose sensitive text.** The first implementation caught exceptions escaping an exporter, but OpenTelemetry 1.44's HTTP exporter logs request exceptions and collector response reasons internally before returning `FAILURE`. Two independent tests reproduced leaked canary strings through the actual public `OTLPSpanExporter` with an injected `requests.Session`; neither test made network requests. The fix applies a narrow logging filter to the HTTP trace exporter logger, retaining the diagnostic severity while replacing the message and removing formatting arguments, exception data and stack information. The exporter failure counters remain available.
2. **Resolved — a full SDK queue could synchronously flood the producer's logging path.** The SDK issues a warning for each dropped span. Logging handlers execute synchronously and could reintroduce blocking on the event loop during an outage. The fix suppresses the exact span queue-full and post-shutdown producer warning templates. SDK queue metrics still record drops, and unrelated application warnings still reach their handlers. The filtering is explicitly coupled to the installed SDK logger/template behavior and covered by real SDK regressions.

The logging filters are installed once under a process lock and deliberately remain installed after an instance closes, because another instance or an in-flight export may still use the same SDK logger. The tradeoff affects those named SDK loggers throughout the process; it does not disable application logging or replace the global logging factory.

## Independently executed checks

Command:

```bash
.venv/bin/pytest tests/review/test_telemetry_refresh.py -q \
  --junitxml=docs/validation/telemetry-20260913/independent-review.xml
```

Result: **5 passed, 0 failed, 0 skipped** in 0.10 seconds. [JUnit evidence](../validation/telemetry-20260913/independent-review.xml). The two OTLP privacy cases initially failed before the fix; the preserved test scenarios subsequently passed against the reviewed implementation.

The five cases check the two real-SDK log paths, cancellation of an asynchronous close waiter while an export is blocked, cancellation exception privacy, and observable bounded queue overflow without synchronous queue warnings. Cancellation does not falsely terminate the underlying export thread: after release, a second close waiter converges on one shutdown and both sinks finish their work. Tests use temporary files and fake transports/exporters, not cloud services, a real collector deployment or a model provider.

Reviewed telemetry source SHA-256: `85f9a4727cb10cfdea3c3c9e5dbde7a75a5dde1bf1be5a6127bd94bdb8cc8917`. `git diff --check` and Ruff on the independent test file also passed.

## Architecture and measurement review

- Separate SDK batch processors give the local file and remote HTTP sink independent bounded queues and exporter threads. Queue/drop data are read using public metric-reader APIs; optional SDK internal metrics can be unavailable, in which case the corresponding values remain `null`. These readings and wrapper exporter counters are snapshots, not an atomic conservation proof under arbitrary concurrency or an exactly-once collector receipt.
- The actual runtime stage names remain in the fixed span-name allowlist. Run/child trace IDs, parent IDs and timestamp fields remain compatible with the JSONL format. Attribute count and string length are bounded, arbitrary nested payloads are omitted, automatic exception events/status descriptions are disabled, and the resource no longer imports arbitrary environment resource attributes. Newly created local files use mode `0600`; the implementation does not claim to repair permissions of an existing file.
- The worker awaits `aclose()`, which moves SDK shutdown work off the event loop. A Python thread cannot be forcibly stopped by cancelling the waiter. SDK shutdown can time out with an export still in flight, and a custom exporter can block its own shutdown; consequently this is best-effort asynchronous shutdown, not a hard process-exit deadline or guaranteed trace delivery.
- The benchmark uses archived baseline commit `8b923a145a161b95608b95fd38b1d15e2e385618`, fresh subprocesses, randomized paired order and an actual loopback HTTP handler with a fixed delay. Both variants use one span per HTTP request and validate identical local/remote record counts. It separates producer time, drain time and combined time, records raw event-loop lag observations, versions, environment and source hashes. This design supports a narrow claim about responsiveness under the injected collector delay, not a model-latency, repair-quality or application-throughput improvement.

The reviewer has not changed either experience bank, the frozen DeepSeek campaign, its classifications or its accounting. The root task executed the runtime/benchmark runs; the reviewer independently inspected and recomputed the completed artifacts below.


## Final artifact audit

Independently parsed [benchmark.json](../validation/telemetry-20260913/benchmark.json) and recomputed all per-run p95/max values from the raw 25 ticker observations, every producer-plus-drain sum, and all reported medians. All **six pairs / twelve subprocess runs** have one baseline and one candidate, unique order positions, and exactly **24 local records plus 24 HTTP requests per run**. Every baseline source hash matches `git show` of the archived commit; every candidate hash matches the reviewed telemetry source. The report SHA-256 is `1d0d71b43a0a6ef70d797714990f77b29ba4a8f6780f3168aa6ae676d2ba4c88`.

| Median across six runs | Synchronous baseline | Background candidate |
|---|---:|---:|
| Within-run p95 event-loop lag | 56.231284 ms | 0.384879 ms |
| Producer duration | 1.428817 s | 0.126423 s |
| Subsequent drain duration | 0.000709 s | 1.154449 s |
| Combined producer + drain duration | 1.429554 s | 1.281174 s |

The independent medians of producer and drain columns need not sum to the median of per-run combined duration. The large responsiveness difference must retain its **50 ms injected delay, one-span HTTP batches, local loopback and six-pair** context; background export still has a backlog to drain. Environment: Linux 7.0.0-29, Python 3.13.15, Intel Core i9-13900K, OpenTelemetry SDK/HTTP exporter 1.44.0.

Parsed final JUnit evidence:

- [worker-runtime-final.xml](../validation/telemetry-20260913/worker-runtime-final.xml): **1 passed, 0 skipped**, suite time 6.061 s. The test uses actual PostgreSQL and configured Docker, a scripted model, and a deliberately blocked exporter. It asserts renewal of the same lease epoch, cancellation and a cancelled tool receipt before the exporter is released, followed by parent/child local trace correlation. This is a functional liveness assertion, not a measured cancellation-latency distribution.
- [runtime-regression.xml](../validation/telemetry-20260913/runtime-regression.xml): **22 passed, 0 skipped**, suite time 78.120 s. The dedicated liveness test is included in this broader run and must not be counted twice as a distinct test.
- [offline.xml](../validation/telemetry-20260913/offline.xml): **322 collected, 319 passed, 3 skipped**, suite time 24.900 s. The five independent review cases are included; the separate independent-review JUnit is an additional execution, not five additional distinct tests.
- The original [worker-runtime-environment-failure.xml](../validation/telemetry-20260913/worker-runtime-environment-failure.xml) remains a **failed** one-test execution with the initial wait timing out. Its retained failure must not be rewritten as a pass; the successful rerun is a separate evidence file.

Also reviewed the final worker shutdown diagnostic: after `await telemetry.aclose()`, `telemetry.closed` records only the bounded per-sink diagnostics. It adds one terminal log after the active worker loop, not synchronous logs for each produced/dropped span. The source/log privacy and shutdown limits described above remain applicable. No further blocking finding was identified in this final audit.
