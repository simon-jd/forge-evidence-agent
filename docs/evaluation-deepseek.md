# DeepSeek pilot under one USD 10 authorization

This pilot implements the user's 2026-09-11 budget constraint. It is a small,
preselected comparison: 12 heldout diagnostic tasks, six conditions and one
repeat. Two separate public cursor repairs check the real Flash and Pro paths
before any heldout task. It does not complete the original 540-trial study.

## Frozen models and experimental controls

The [official price page](https://api-docs.deepseek.com/quick_start/pricing/) was
checked on 2026-09-11. The current Flash ID is `deepseek-flash` (V4.1 Flash);
the Pro ID is `deepseek-v4-pro` (V4 Pro 0813). The old Flash ID is an alias.
The adapter uses the official [stateless Responses API](https://api-docs.deepseek.com/guides/responses_api/),
including full local history, terminal usage and function tools.

| Model | Input miss / 1M | Input cache hit / 1M | Output / 1M |
| --- | ---: | ---: | ---: |
| deepseek-flash | $0.30 | $0.006 | $1.20 |
| deepseek-v4-pro | $1.32 | $0.044 | $3.96 |

These are the published **peak USD tariffs**, frozen as conservative upper bounds.
Off-peak charges may be lower. Reports keep `cost_exact_usd` empty for these runs
and expose `cost_upper_bound_usd`; complete usage does not make the tariff an
exact invoice. Cached input and reasoning output are subsets, counted once.

`configs/runs/deepseek-pilot.yaml` fixes reasoning effort to `none`, 4,096 maximum
output tokens per attempt, 24 attempts including retries, 40 tool calls and at
most one model escalation. B0/B1/B2 compare context handling, B3 enables bounded
escalation, `strong_only` starts on Pro, and `B2_guard_off` removes the evidence
guard under the B2 configuration. The task IDs, seed and ordering inputs are
frozen in `configs/campaigns/deepseek-10usd.json` before observing model results.

## Cumulative accounting

| Allocation | Runs | Per-run ceiling | Total |
| --- | ---: | ---: | ---: |
| Public Flash / Pro smoke, including two retained development failures | 4 | $0.20 | $0.80 |
| 12 heldout tasks × 6 conditions | 72 | $0.12 | $8.64 |
| Scheduled total | 76 | | **$9.44** |
| Unallocated margin | | | **$0.56** |

Two independently reviewed development amendments precede every heldout trial.
The first Flash run stopped at 16,147 mandatory context bytes against a 16K
allowance after two requests. The second used a 32K allowance and produced a
passing patch, but exhausted 12 requests before `finish`. Both remain failed
runs with complete usage; their combined peak-tariff cost upper bound is
$0.016993608. Each amendment added one distinct $0.20 development slot from the
original unallocated margin. The frozen heldout configuration uses 32K context
and 24 maximum attempts with unchanged per-run dollar, output and tool limits.
No heldout task was observed, replaced or rerun during these changes.

Before enqueueing each run, the campaign claims its complete allocation in a
private, atomically written, file-locked ledger. The runtime separately reserves
every API attempt before transmission using the complete serialized request
bound and output cap. Only confirmed final usage permits reconciliation.
An interrupted smoke, unknown usage or nonterminal run retains its original
allocation; restarting the command cannot replay claimed slots. An observed
overrun stops subsequent paid work and remains visible.

This is a local campaign ceiling, not an account-wide billing limit. Other
programs using the same API account are outside this ledger. Byte-based token
estimates and frozen tariffs are conservative controls, not provider-enforced
billing guarantees; an account spending limit or limited account balance is the
outer control for a strict invoice ceiling. No unused allocation is automatically
reinvested in more trials or retries.

## Prepare and execute

Complete the [local setup](quickstart.md): a rootless Docker socket, the trusted
`forge-runner:dev` image, and the dedicated local PostgreSQL service are required.
The campaign creates its own database named `forge_deepseek_10usd_20260911_v1`
using that service's credentials; it never drops or resets a database. The
prequalified private evaluator store must exist under `.forge/private-evals`
or the configured `FORGE_EVAL_PRIVATE_DIR`.

Put `DEEPSEEK_API_KEY` in the local, ignored `.env` file. Keep it out of chat,
commits, config YAML and shared logs. Also configure `FORGE_DOCKER_HOST` and
`FORGE_DATABASE_URL` from the local setup.

```bash
# Prepares/inspects the fixed ledger; makes no network or model requests.
uv run --no-sync python -m forge.evals.deepseek_campaign

# Executes the previously authorized campaign, at most once per claimed slot.
uv run --no-sync python -m forge.evals.deepseek_campaign --execute
```

For an existing campaign, `--execute` refuses prior progress. A reviewed
`--amend-plan` operation preserves old plan/hash/history and every original slot,
permits only pending configuration changes and new development smoke slots, and
retains the original $10 authorization. It refuses unresolved calls or any
already-started heldout trial. After original runs are reconciled, `--resume`
continues only pending development work and checks current-configuration verified
smoke evidence before admitting heldout trials. A settled bill is not a pass.

Execution checks the rootless daemon, frozen image, private-spec hashes and the
account's model listing before generation. Each smoke uses the public CLI/API,
a separate worker, the public cursor task and independent read-only validation.
Only after both verify does the normal evaluation harness enqueue the 72 trials.

The private directory `.forge/campaigns/deepseek-10usd-20260911-v1/` contains
`ledger.json`, smoke observations, worker logs, trial JSONL and HTML/CSV/JSON
reports. Do not publish raw trial patches, private grader material or session
files. Publish only reviewed, sanitized aggregates. If execution stops, inspect
the original run IDs and reconcile their usage; do not delete the ledger, invent
a new campaign ID or rerun the standalone live demo against this authorization.

## Evidence and interpretation

The [provider contract](providers/deepseek.md) and
[independent budget review](reviews/deepseek-budget-plan.md) document offline
verification. The machine-readable [live status](../evals/deepseek-live-status.json)
records the completed 72-trial pilot: 51 independently verified, 21 failed, with five unresolved cost classifications. The [results and limitations](validation/deepseek-live.md), [sanitized report](../evals/reports/deepseek-10usd/report.html) and [independent audit](reviews/deepseek-live-review.md) preserve every outcome. Campaign accounting is $1.030805844 in settled peak-tariff upper bounds plus $0.60 of full unresolved run allocations, totaling $1.630805844 under the original authorization. No unused budget funded additional trials.

One repeat and a preselected 12-task subset cannot establish robust gains or an
industry benchmark score. Report per-condition verified repairs, failure counts,
paired task outcomes and upper-bound spending. Keep unsuccessful and
infrastructure-failed trials in the report; do not silently replace them.
