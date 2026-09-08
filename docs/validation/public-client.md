# Public CLI and independent process demonstration

Independent development Agent: `sandbox_development`. Observed 2026-09-07
America/Los_Angeles. This is **offline scripted control-flow evidence**, with no
paid model requests and no claim of model-generated code or model quality.

## Actual path exercised

```text
installed forge CLI (separate command processes)
    -> authenticated HTTP API (independent uvicorn process, loopback:18765)
    -> PostgreSQL queue and durable event log
    -> forge.worker --once (independent worker process)
    -> rootless Docker task workspace
    -> separate read-only validation workspace
    -> public HTTP receipts / forge diff / resumed forge watch
```

`scripts/demo.py` does not construct an in-process ASGI client, invoke Runtime,
query Repository, call a provider, or call a verifier directly. It creates only
public fixture/configuration input, controls its own child processes, and uses
the installed CLI/public HTTP endpoints to submit and observe work. The existing
dedicated PostgreSQL queue was checked idle and the main development Agent
reserved an exclusive window before these observations. The script itself cannot
reserve an arbitrary shared queue: `worker --once` claims the next eligible job.

The public fixture has a real, intentionally small cursor defect. A page ending
inside one millisecond is followed by a filter that considers only timestamp,
dropping later events at the same timestamp. The scripted correction compares
`(timestamp_ms, sequence)`. The tool trace contains exactly these calls:

1. `demo-read`: read the current `replay.py` and its content hash.
2. `demo-patch`: apply the explicitly authored unified diff with that hash.
3. `demo-focused-test`: run the public `ResumeCursorTests` inside Docker.
4. `demo-finish`: propose completion and enter independent validation.

Six public unittest cases cover stable ordering, page bounds, cursor construction,
exclusive boundaries, same-timestamp completeness and repeated pagination. Three
of them are the pinned acceptance target. Tests remain protected and unchanged.
The demo includes no private diagnostic grader or private reference patch.

## Reproduction and observed results

The standard runner image was already built in the separately managed rootless
daemon; migrations were already applied to the dedicated local database.

```bash
.venv/bin/python scripts/demo.py \
  --docker-host unix:///tmp/forge-rootless-1000/docker.sock \
  --output docs/validation/public-client.json
```

Observed at 17:07 local time:

| Check | Observation |
| --- | --- |
| Run ID | `49181324d3c64a08aae6465d7f47b8b1` |
| Execution mode | `offline_scripted` |
| Fixture base commit | `edd3860ef04bd7c72ce3a40fa27a543cd0965c93` |
| Final state | `completed`, verification `verified` |
| Baseline regression / target | Exit code 1 / 1 |
| Independent final regression / target | Exit code 0 / 0 |
| Actual completed tests | 6 regression, 3 target; no missing/skip shortcuts |
| End-to-end observed wall time | 7.685 seconds, one local observation |
| Durable events | 50 contiguous sequences |
| Model calls | Four explicit fake-provider turns, zero paid inference |
| Model charge | Four settled zero-dollar fixture reservations |
| Receipts | Five, all `rootless=true`; four verifier mounts read-only |
| Original source | Unchanged |

The observer was deliberately disconnected after the first durable event and
before the separate worker started. The worker still finished. A new CLI process
resumed after that event, obtained every remaining sequence exactly once, and a
replay from the final cursor returned no events. Repeated submissions and repeated
messages reused their original IDs under the same idempotency keys.

The code/input image identity was frozen as:

```text
sha256:88ea842ecc6d79db7d22c0cf911ea41b5ebec7c6846298eea1ae7a77dc2c7e15
```

The [sanitized public-client JSON](public-client.json) retains the actual patch,
public test completion records, per-attempt budget ledger, manifest/schema/prompt
hashes and receipt policy fields. It excludes bearer tokens, model keys, process
logs, local source paths and the private session environment file. Debug logs and
client token remain only under the ignored, private `.forge/demo/sessions/` tree.

The generated repository's fixed author/committer timestamp is a reproducible
fixture input. It is not a dated project development commit, work record or
employment-history claim. Documentation updates do not rewrite an existing
frozen demo source repository; source/test changes require a new demo root.

## Independent test invocation

The public-client integration test then launched the full script again with its
own source, API/worker processes, session owner, run ID and workspace:

```bash
env FORGE_TEST_DATABASE_URL=postgresql+psycopg://forge:forge_local@127.0.0.1:55471/forge \
  FORGE_TEST_DOCKER_HOST=unix:///tmp/forge-rootless-1000/docker.sock \
  FORGE_TEST_IMAGE=forge-runner:dev FORGE_RUN_PUBLIC_CLIENT=1 \
  FORGE_ALLOW_ROOTFUL_TESTS=0 \
  FORGE_PUBLIC_CLIENT_EVIDENCE=docs/validation/public-client-test.json \
  .venv/bin/pytest tests/integration/test_public_client.py -q \
  --junitxml=docs/validation/public-client-junit.xml
```

Observed: **1 passed in 6.74 seconds**, zero skips.
[JUnit result](public-client-junit.xml), [second independent run](public-client-test.json).
The test asserts the concrete public completion records, four tool identities,
rootless receipt policies, original-source preservation, duplicate-request behavior,
SSE sequence continuity, and settled zero fixture charges. It is not satisfied
by a final success string alone.

Before execution, Ruff passed for the demo script, fixture and integration test;
mypy passed for the typed demo script. The workflow YAML parsed with its two
separate jobs and read-only GitHub permissions.

## CI pins and limits

`.github/workflows/checks.yml` has an offline unit/provider-contract job and a
separate PostgreSQL/Docker integration job. Both use **uv 0.12.10**, **Python 3.13**
and `uv sync --frozen`, with empty model-key environment variables. The integration
job explicitly uses a hosted runner's rootful development daemon and labels its
result accordingly; that job cannot establish rootless acceptance. It uploads
only JUnit and the public fixture's sanitized result. Private `.forge` logs,
environment files and evaluation inputs are excluded from uploads.

The initial checkout/setup-uv pins were read from the
[official uv Actions guide](https://docs.astral.sh/uv/guides/integration/github/).
All three tags were then checked with `gh api repos/OWNER/REPO/git/ref/tags/TAG
--jq .object` against GitHub's official API on 2026-09-07:

| Action/tag | Verified commit SHA |
| --- | --- |
| [actions/checkout v7.0.1](https://github.com/actions/checkout/tree/3d3c42e5aac5ba805825da76410c181273ba90b1) | `3d3c42e5aac5ba805825da76410c181273ba90b1` |
| [astral-sh/setup-uv v9.0.0](https://github.com/astral-sh/setup-uv/tree/c771a70e6277c0a99b617c7a806ffedaca235ff9) | `c771a70e6277c0a99b617c7a806ffedaca235ff9` |
| [actions/upload-artifact v7](https://github.com/actions/upload-artifact/tree/043fb46d1a93c77aae656e7c1c64a875d1fc6a0a) | `043fb46d1a93c77aae656e7c1c64a875d1fc6a0a` |

Each API object reported `type=commit`; all workflow action references are now
fixed to those commit SHAs. The initial network-restricted command could not
connect; the explicitly approved API lookup succeeded. Remote GitHub Actions has
not been run as part of this report. A locally parsed workflow and passing local
tests are not a claimed green remote build.

The manual `--live-config` path requires an explicit budget authorization and
preserves runtime write approvals. It was not exercised here. Two offline
observations do not establish general model effectiveness, throughput, production
adoption or a performance distribution. See [quickstart](../quickstart.md) for
configuration, deployment and the separate evaluator's prerequisites.
