# Forge control API and CLI

The API queues work and reads durable facts. It does not call models, execute
repository programs, or keep a task alive through a client connection. The worker
owns execution. API and CLI contracts are tested without live model calls; the
SQLite HTTP tests do not establish PostgreSQL lease or Docker isolation guarantees.

## Start the control plane

Use the repository's locked environment and database migration instructions. Set
`FORGE_API_TOKEN` to a random local secret and configure allowed repository roots.
The API refuses authenticated routes without a configured token and fails startup
if the token is empty. It is intended to bind to loopback:

```bash
uv run uvicorn forge.api.main:app --host 127.0.0.1 --port 8765
```

The CLI reads `FORGE_API_TOKEN` and `FORGE_API_URL` from its process environment.
The server additionally loads `.env` through `Settings`. A `.env` file is not
automatically exported into an unrelated CLI shell. `forge status RUN_ID` is a
convenient authenticated connectivity check.

## Trusted configuration and frozen submissions

Register a repository using a profile from `configs/repo_profiles/ID.yaml`. The
HTTP request accepts a profile ID rather than inline commands or mount paths.
The source must resolve inside `FORGE_ALLOWED_REPO_ROOTS`. The API only uses
`git rev-parse` to inspect the commit, and the image must already exist in Docker;
submission does not pull or build images.

Run configurations live in `configs/runs/ID.yaml`:

```yaml
execution_mode: live
run:
  models:
    - provider: openai
      model_id: YOUR_ENABLED_MODEL_ID
      price_version: YOUR_PRICE_SOURCE_AND_DATE
      input_usd_per_million: null
      output_usd_per_million: null
  budget:
    max_cost_usd: "1.00"
```

The `YOUR_*` values and null prices above are configuration placeholders, not a
working model/pricing recommendation. Copy an example, fill the account's enabled
model ID, capabilities, and versioned prices, and use its new configuration ID.
Unknown usage or pricing is never reported as zero. Runtime budget checks may
reject a configuration whose request cost cannot be bounded.

Submission freezes the resolved commit, Docker `sha256:` content ID, complete
`RunConfig`, profile, model price/capability records, installed dependency versions,
tool schema hash, versioned prompt file hashes, and the actual context builder's
system instruction hash. A provider model alias is preserved as configured and
explicitly identified as potentially mutable upstream.

`offline_scripted` configurations require explicitly provided `FakeProvider`
turns. `configs/runs/offline-protocol.yaml` exercises completion/verification
wiring and does not generate a repair. It must not be counted as AI quality or a
real-model demonstration. The raw script is retained in the internal manifest and
omitted from public run responses.

`python-tests` deliberately has no target acceptance command. Passing regression
tests alone must produce `regression_only`, not `verified`. Adapt the trusted
profile to include an independent target test and protect all validation entry
points before registering a project intended for verified repairs.

## Request contract

Every endpoint requires `Authorization: Bearer TOKEN`. Object access uses the
configured owner. Missing/incorrect credentials return 401; another owner's
object is indistinguishable from a missing object (404). Invalid input/config is
422, conflicting state/idempotency is 409, and an unavailable image or disabled
evaluation capability is 503. JSON request bodies reject unknown fields.

| Method | Path | JSON input / response |
| --- | --- | --- |
| POST | `/v1/projects` | `{name, source, profile_id}` → project, 201 |
| POST | `/v1/runs` | `{project_id, task, base_commit:"HEAD", config_id}` + `Idempotency-Key` → queued run, 202 |
| GET | `/v1/runs/{id}` | Status, stage, version, budget, iteration, verification, manifest, reviewable pending approvals |
| GET | `/v1/runs/{id}/events` | SSE; `Last-Event-ID` or `?after=N`; `?follow=false` for finite replay |
| POST | `/v1/runs/{id}/messages` | `{text}` + `Idempotency-Key` → appended message, 202 |
| POST | `/v1/runs/{id}/cancel` | Empty body → cancellation request state |
| POST | `/v1/runs/{id}/resume` | `{version}` → reconciliation queued, 202 |
| POST | `/v1/approvals/{id}/decision` | `{decision:"allow"\|"deny", version}` → decision |
| GET | `/v1/runs/{id}/artifacts` | Authorized artifact metadata |
| GET | `/v1/artifacts/{id}` | Authorized, integrity-checked bytes as an attachment |
| POST | `/v1/evaluations` | Bounded server-configured experiment → queued evaluation, 202 |
| GET | `/v1/evaluations/{id}` | Evaluation state and results/report references |

`Idempotency-Key` is required for run and message submission. The same key/body
returns the same object; a different body conflicts. Run retries are resolved
before rereading changed configuration or inspecting an image. Use a new key for
a new task. The CLI prints the key it used so a caller can retain it.

An approval response displays the complete pending tool call, parameter hash,
workspace revision, policy hash, and approval version. Inspect it with
`forge status RUN_ID` before deciding. A stale version conflicts. User messages are
append-only and apply at a safe execution boundary; they do not grant permissions.
Cancelling sets `cancel_requested`; the API does not claim a container has stopped.
Resume only queues reconciliation of an existing uncertain operation. A terminal
task cannot be resumed; create a new run.

## Durable event replay and artifacts

Each event has its persisted run sequence as SSE `id`, an event type, and a JSON
envelope containing `seq`, `type`, `payload`, and `created_at`. The API queries at
most 100 records per page and never buffers an entire run. The worker persists
text blocks rather than every token. Slow readers backpressure their own HTTP
response, without cancelling the worker. A disconnected CLI reconnects with the
last processed sequence and ignores duplicates.

The API sends a keepalive comment during quiet periods. Event projections strip
native model state and credential-shaped keys. Frames are bounded at 64 KiB; an
unexpectedly oversized record becomes an explicitly marked omitted payload whose
full content belongs in a retained artifact. The sequence remains replayable.

Run responses omit worker state and model-native/raw responses. Owner-authorized
artifact access includes patches, command/validation receipts, visible canonical
transcripts, context snapshots, and code references. Raw `model_response` artifacts
remain in controlled storage and are not public download endpoints. Artifact
content is read only after both database ownership and local metadata identity
checks; byte count/hash mismatches fail the request. Code-containing artifacts
remain private project data and should not be published with benchmark results.

## CLI workflow

```bash
forge project /absolute/allowed/repository --name example --profile python-tests
forge run PROJECT_ID "Fix the reported parser regression" --config configured-model --base-commit COMMIT --key repair-001
forge watch RUN_ID
forge watch RUN_ID --after 42
forge status RUN_ID
forge message RUN_ID "Keep the existing public API" --key instruction-001
forge approve APPROVAL_ID --version 1
forge approve APPROVAL_ID --version 1 --deny
forge cancel RUN_ID
forge resume RUN_ID --version 7
forge diff RUN_ID
```

Commands produce JSON, except `watch` emits one JSON event per line and `diff`
prints patch bytes as text. API/transport failures return nonzero exit codes and a
concise error. CLI commands call only the public HTTP API, so they can run in a
separate process without database credentials or access to the worker's files.

## Bounded evaluation submission

Evaluation submission is disabled unless `FORGE_ENABLE_EVALUATIONS=true` and an
administrator supplies `FORGE_EVAL_PRIVATE_DIR`. A trusted
`configs/experiments/ID.yaml` resolves `base_run_config`, `profile_id`, and
`dataset_id`. The public request never includes grader paths or hidden tests:

```json
{
  "experiment_id": "paired-context-v1",
  "task_ids": ["task-a", "task-b"],
  "repeats": 3,
  "conditions": ["B0", "B1", "B2", "B3", "strong_only"],
  "config_id": "configured-experiment",
  "budget_usd": "5.00",
  "seed": 1729
}
```

One base run configuration produces the matched conditions; the API freezes the
base model configuration and profile image. The evaluation worker must enforce
the experiment-wide budget across all trials, validate frozen task IDs and
dataset/split metadata, and retain failure costs. The API only enqueues this
request; a successful HTTP submission does not mean an experiment has run.

```bash
forge eval submit configured-experiment --experiment paired-context-v1 --tasks task-a,task-b --budget 5.00 --repeats 3
forge eval status EVALUATION_ID
```

Native provider protocol contract tests use the locked SDKs and synthetic SSE
through `httpx2.MockTransport`. HTTP/CLI tests use local transports. These tests
never require model API keys and never invoke a paid model. Two-provider live
smokes and quality experiments are separate evidence.
