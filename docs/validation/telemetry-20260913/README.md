# Telemetry isolation verification — 2026-09-13

Forge now exports local JSONL and remote OTLP traces through separate bounded SDK queues. This removes exporter I/O from span completion on the async worker. PostgreSQL remains the authoritative action/usage ledger: trace delivery is best effort. See the [market/dependency review](../../research/market-2026-09-13.md), [design decision](../../adr/0007-isolate-telemetry-export.md), [independent review](../../reviews/telemetry-refresh-review.md), and machine-readable [verification manifest](verification.json).

## Completed validation

| Check | Actual result | Boundary / evidence |
| --- | --- | --- |
| Offline unit, contract and review suite | 319 passed, 3 skipped | [JUnit](offline.xml); three container-only review cases require explicit opt-in |
| Blocked trace sink vs worker control path | 1 passed | [JUnit](worker-runtime-final.xml); real PostgreSQL and rootless Docker, scripted model |
| Runtime, public CLI and independent findings | 22 passed, 0 skipped | [JUnit](runtime-regression.xml); includes the blocked-sink case and the three previously skipped container cases |
| Independent telemetry regressions | 5 passed | [JUnit](independent-review.xml); included in offline suite; real SDK with controlled exporter/transport doubles |
| Static checks | Ruff passed; mypy passed for 58 files | Commands below, including benchmark, demo and migration scripts |

These sets overlap and must not be added together. The runtime regression observes a command still running while the exporter is held, confirms the same lease epoch gets a later PostgreSQL expiry, requests cancellation, and confirms the container operation and persisted tool result are cancelled before the exporter is released. It also checks local parent/child trace correlation after drain. This is a functional liveness test, not a heartbeat-latency percentile measurement.

The first runtime attempt failed because the previous Forge Docker socket was absent; [its failed JUnit](worker-runtime-environment-failure.xml) is retained. Restarting with a `/tmp` runtime directory hit a host quota; a project path containing spaces exposed unquoted state-directory expansion in the installed `dockerd-rootless.sh`. The dedicated service was restored under `/run/user/1000/forge-rootless`, retaining the original image/data root. The startup script now defaults to the user's runtime directory and rejects whitespace in that runtime path. No other project's daemon, containers or data were removed.

## Reproducible slow-collector comparison

Baseline is the exact `src/forge/telemetry.py` from commit `8b923a145a161b95608b95fd38b1d15e2e385618` (synchronous `SimpleSpanProcessor`). Candidate source SHA-256 is `85f9a4727cb10cfdea3c3c9e5dbde7a75a5dde1bf1be5a6127bd94bdb8cc8917`. [Raw results](benchmark.json) contain source hashes, individual ticker samples, per-run measurements and environment. No model API is called.

- Six paired repetitions, fresh subprocess for each variant; order randomized with seed `20260913`.
- Twenty-four spans per run; **one span per actual HTTP request in both variants**. The loopback collector sleeps 50 ms per request. The candidate queue holds 64 spans per sink; batch size is 1 for this comparison, rather than the default 32.
- A 5 ms asyncio ticker samples event-loop scheduling lateness during production. Per-run p95 uses sorted index `floor((n−1) × .95)`; the reported aggregate is the **median of six per-run p95 values**, not the percentile of all samples pooled together.
- Each run asserts 24 local JSONL records and 24 completed collector requests. Producer time and shutdown/drain time are recorded separately.

| Metric: median across six runs | Synchronous baseline | Background candidate |
| --- | ---: | ---: |
| Per-run p95 event-loop lateness | 56.231 ms | 0.385 ms |
| Span production interval | 1.429 s | 0.126 s |
| Shutdown/drain interval | 0.000709 s | 1.154 s |
| Production plus drain | 1.430 s | 1.281 s |

Per-run p95 ranges were 55.320–56.693 ms and 0.278–0.585 ms respectively. Export work still has to finish: the queue moves waiting off the producer and does not make the remote collector faster. These measurements establish responsiveness under a controlled slow-collector fault. They do **not** establish lower LLM latency, higher repair success, production throughput, or an SLA.

Environment: shared Linux 7.0.0-29 x86-64 workstation, Intel Core i9-13900K, Python 3.13.15, OpenTelemetry SDK/OTLP HTTP 1.44.0. No CPU pinning or workload isolation. Runtime validation used a dedicated PostgreSQL database and rootless Docker with image `sha256:88ea842ecc6d79db7d22c0cf911ea41b5ebec7c6846298eea1ae7a77dc2c7e15`. Model turns were scripted; the collector was a real loopback HTTP fault fixture, not a cloud collector.

```bash
uv run --no-sync ruff check src scripts tests examples migrations
uv run --no-sync mypy src scripts/demo.py scripts/check_migration.py scripts/benchmark_telemetry.py
uv run --no-sync pytest tests/unit tests/contract tests/review -q
uv run --no-sync python scripts/benchmark_telemetry.py \
  --output results/telemetry-new-measurement.json --pairs 6 --spans 24 --delay-seconds 0.05

# Use an already migrated, dedicated database and explicitly configured rootless socket.
# Export FORGE_TEST_DATABASE_URL, FORGE_TEST_DOCKER_HOST and FORGE_TEST_IMAGE.
FORGE_RUN_PUBLIC_CLIENT=1 FORGE_REVIEW_DOCKER=1 \
  FORGE_REVIEW_DOCKER_HOST="$FORGE_TEST_DOCKER_HOST" \
  uv run --no-sync pytest tests/integration/test_runtime.py \
  tests/integration/test_public_client.py tests/review/test_independent_findings.py -q
```

## Failure, privacy and compatibility boundaries

Tests cover independent sinks, bounded queue overflow, exporter return failures/exceptions, local filesystem failure, exporter initialization failure, explicit OTLP request timeout, invalid configuration, nested trace correlation, cancellation, and close lifecycle. With SDK internal metrics explicitly enabled in the process environment, the controlled saturation test observes 8 remote drops out of 13 offered spans with one in flight and queue capacity 4; local delivery remains 13/13. This is a deterministic test condition, not a production loss-rate metric. With metrics disabled, queue size/drop fields remain `null` rather than falsely zero.

The independent reviewer reproduced OTLP SDK logs containing injected transport/collector text and synchronous queue-full warnings running on the producer. Both paths were fixed and retested. Narrow filters persist on two named SDK loggers across Telemetry instances; that process-wide effect and SDK 1.44 logger/template coupling are intentional tradeoffs. Application loggers are untouched. Span attributes/names are allowlisted and bounded; automatic exception payloads and ambient resource attributes are omitted. This does not sanitize arbitrary application logs or prevent secrets deliberately supplied under allowed identifiers.

Worker shutdown awaits `aclose()` off the event loop and emits bounded sink diagnostics. OTLP has an explicit request timeout, but cancelling a waiter cannot forcibly terminate a Python export thread. SDK shutdown is best effort; arbitrary custom exporters, abrupt process death and trace delivery after shutdown deadlines are not hard guarantees. Queues are in memory and may drop spans; JSONL is not the authoritative recovery ledger.

No dependency upgrade was needed: inspected stable releases matched the lockfile. This refresh made zero paid model API calls and did not alter or rerun the frozen DeepSeek campaign. Its 72 diagnostic trials and conservative cost accounting remain historical evidence with their original limitations.
