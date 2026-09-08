# Independent evaluation and evidence experiments

Forge evaluates a submitted patch in a separate checkout of a frozen commit. The Agent receives a public task projection; a private evaluator supplies independent target tests after the Agent run finishes. A `finish` call, model text, a green self-authored test, or a successful command exit does not decide the evaluation label.

The current recorded result is **30 of 30 diagnostic tasks qualified** in real Docker: each original snapshot fails at least one target assertion while passing its public smoke test; each private reference patch passes both target tests and the public smoke test. This is fixture validation. **Live model quality experiments have not run.** See `evals/qualification.json` and `evals/live-status.json` for the actual evidence and outstanding dependency.

The measured 60 baseline/reference observations are also exported in `evals/reports/qualification/` as JSONL, CSV, a JSON summary and `report.html`. Their execution kind is `reference_validation`; recorded model cost and tokens are zero because these checks invoke no model. Private patches and assertions are omitted from the public export. Do not use that report as a coding-model score.

## Dataset and validity

`evals/manifest.json` freezes 30 tasks: 10 development and 20 heldout, with one defect family per split. Cases include interleaved tool streams, Unicode byte boundaries, cancellation cleanup, leases, billing, cross-module configuration and evidence-coordinate contracts, long logs, and ordinary graph/range defects. They are small, manually specified diagnostic programs, not a claim to represent the difficulty or diversity of a production repository benchmark. Three cases require edits across actual modules; the long-log case includes 1,600 source log lines. Repeats are multiple observations of the same task.

Each task records the source file hashes, reproducible Git commit, instructions, source edit allowlist, split/family, and private specification hash. `Dataset.materialize()` verifies every public byte and reconstructs that exact commit in a new repository. Its Git history contains only the broken starting snapshot. It never contains the private reference fix.

The development/heldout split is frozen for this project. Its source tasks are public. The author necessarily knows the designed defects. Do not describe these tasks as a secret external benchmark or make claims of generalization from this dataset alone. After examining heldout quality results, record any subsequent change as a new dataset version. An eventual SWE-bench run should use the official harness and be reported separately.

## Private material and container boundary

The persistent local store is `.forge/private-evals`, mode `0700`, under the already ignored `.forge/` directory. Each `<task_id>.json`, mode `0600`, contains `PrivateGraderSpec`:

```text
task_id
target_source          evaluator-owned unittest module
target_test_count      exact required number of executed target tests
reference_patch        fixture qualification only
author_note
```

`PrivateSpecStore.load()` compares the file bytes with the SHA-256 frozen in the public manifest. The public task projection excludes target source, reference patches, their storage paths, final labels, split and private specification hashes. A hash is an integrity anchor, not a substitute for independent test design.

Private specs are not included in a public clone. Copy the preserved private directory into another trusted evaluator environment using a secure transfer, set mode `0700`, and validate all manifest hashes before accepting the transfer. The original authoring material is also retained privately for dataset maintenance; never publish it as a convenient regeneration script while calling the corresponding answers hidden. If the private store is unavailable, evaluation fails explicitly. It never substitutes the Agent's tests for missing target tests.

The evaluator creates a new `Workspace`, applies only permitted source changes, and then installs its target tests under `.forge_grader/` in that evaluator workspace. An Agent container mounts only its own separate workspace. Neither the private store nor the parent data directory is mounted into Agent jobs. Public regression tests and validation configuration cannot be changed by a candidate patch. The evaluator rejects missing target executions, skips, expected failures, malformed reports, and mismatched target counts.

The current qualification used image `sha256:88ea842ecc6d79db7d22c0cf911ea41b5ebec7c6846298eea1ae7a77dc2c7e15`, Python 3.13 with pytest 8.4.2, under actual `rootless_docker`. After resolving the host UID mapping setup, all 60 baseline/reference checks were repeated with the grader workspace mounted read-only. The container has a non-root UID, disabled network, dropped capabilities, read-only root filesystem and resource limits. `python -I -B` prevents bytecode writes; test temporary files go to `/tmp`. Every grader input, including hidden directories, is hashed before and after execution. A changed or unreadable input produces a failed protected-path result. Default execution rejects a rootful daemon unless development mode is explicitly enabled; such receipts remain labeled `rootful_docker_dev`. There is no host subprocess fallback for grading submitted code.

The grader boundary prevents accidental answer leakage and common test-file deletion/skip shortcuts. It does not claim to securely grade arbitrary malicious Python that attacks the interpreter executing its tests. A hostile-code service needs a stronger execution and grading design.

An independent review found that a candidate could rewrite the public regression file during module import when the grader used a writable workspace. The fix introduced the read-only grader mount and full input seals. A real rootless Docker regression test now submits that attack: the write is blocked, the original public assertion still fails, and the candidate is rejected even though its target assertion passes. All 30 qualification cases were then rerun on the corrected boundary.

## Experiments and cost policy

`standard_conditions(base_config)` creates all conditions from one frozen `RunConfig`:

| Condition | Context | Model choice | Evidence guard |
| --- | --- | --- | --- |
| B0 | recent complete interactions | fixed first model | on |
| B1 | current evidence plus recent interactions | same fixed model | on |
| B2 | evidence plus portable compaction | same fixed model | on |
| B3 | same as B2 | bounded rules for escalation | on |
| strong_only | same as B2 | fixed final model | on |
| B2_guard_off | same as B2 | same fixed first model | off |

The guard flag is copied into the actual runtime `ContextConfig`, not only the experiment label. Budget limits remain identical across conditions. Plan three repeats per task/condition; all six conditions over 30 tasks produce 540 planned trials. Randomized execution order reduces a simple time-order effect; request seeds are recorded but do not promise provider determinism.

`EvalHarness.run(..., authorized_total_cost_usd=Decimal(...))` reserves the full per-run limit before permitting each live callback. It subsequently accounts for actual known costs. Missing or uncertain usage retains at least that trial's reserved limit when deciding whether another trial can start. No authorized total, insufficient remaining reservation, or an unconfigured live run produces `not_run`; the callback is not invoked. Limits constrain future requests and cannot reverse already billed requests. No paid model call was made during the implementation and fixture qualification.

The report distinguishes live model runs, offline control-flow fixtures, and reference-validation runs. These execution kinds must never be pooled into a claimed model success rate. Exact cost per successful verification includes failed-attempt costs and becomes undefined for zero successes or any unaccounted cost. Unknown usage is recorded separately, never silently converted into free usage. A verified patch with uncertain cost is not asserted to be a within-budget success.

## Integration contracts

```python
from forge.evals.dataset import Dataset, PrivateSpecStore
from forge.evals.grader import DockerGrader
from forge.evals.harness import EvalHarness, standard_conditions

harness = EvalHarness(
    dataset=Dataset(dataset_root),
    private_specs=PrivateSpecStore(private_root),
    grader=DockerGrader(private_workspace_root, docker_executor),
    repository_root=repository_cache_root,
    profile=pinned_profile,
)
trials = await harness.run(
    tasks, standard_conditions(base_run_config), run_callback,
    output_path, repeats=3, seed=1729, experiment_id=experiment_id,
    authorized_total_cost_usd=authorized_budget,
)
```

`run_callback(PublicTrialRequest) -> Awaitable[AgentTrialOutcome]` is injected. Its request contains `trial_id`, public `task`, generated `repository`, pinned `profile`, `condition`, `repeat`, and `seed`. It contains no grader data. The returned outcome includes the patch, run ID, execution kind, candidate/failure status, Decimal cost and its certainty, token usage, tool counts, compaction counts, phase durations and trace/artifact references. The evaluator derives the final target/regression result independently.

`ServiceRunCallback` implements the real path through `ForgeService`. It writes frozen public condition/profile YAML only under `settings.config_dir/generated/<evaluation_id>`, registers the public repository, submits a normal run with a trial-specific idempotency key, polls that run, and retrieves its patch and usage ledger. Repository authorization remains active: `FORGE_ALLOWED_REPO_ROOTS` must include the evaluator's generated repositories. Separate normal workers execute the submitted runs, so an evaluator waiting on a trial does not block the only task worker.

`forge.evals.worker:main` is the separate evaluation worker entry point. It consumes API-enqueued `EvaluationRow` records. PostgreSQL advisory locks prevent two live evaluators from claiming one experiment. If the evaluator dies, an unfinished experiment becomes `needs_reconciliation`; existing trial files and submitted run IDs are preserved and no potentially billed trial is automatically replayed. Inspect any still-running task before restarting an experiment. The task runtime has its own durable recovery semantics.

The API payload uses an administrator-owned `config_id`, `experiment_id`, `task_ids`, `conditions`, `repeats`, `budget_usd`, and `seed`. The administrator's experiment config chooses the dataset, base run config and profile. Client requests cannot supply grader code or private paths. Use `FORGE_ENABLE_EVALUATIONS=true` and `FORGE_EVAL_PRIVATE_DIR=.forge/private-evals` when enabling this feature.

## Reproduce verification and reports

To requalify all fixtures without a model, choose a prebuilt image and configure the appropriate Docker daemon:

```bash
uv run python -m forge.evals.cli qualify \
  --dataset evals --private .forge/private-evals \
  --image forge-runner:dev --output .forge/qualification-new.json
```

The command uses new repository/workspace IDs and preserves the prior report. Development use on a rootful daemon requires the explicit `FORGE_ALLOW_ROOTFUL_DEVELOPMENT=true` setting; its result remains labeled accordingly.

For scheduler integration tests, use the dedicated migrated PostgreSQL test database. Docker/private-spec integration additionally requires `FORGE_TEST_IMAGE`, `FORGE_DOCKER_HOST`, and `FORGE_EVAL_PRIVATE_ROOT`; rootful tests require `FORGE_ALLOW_ROOTFUL_TESTS=1`:

```bash
uv run pytest tests/unit/test_eval_dataset.py tests/unit/test_eval_harness.py tests/unit/test_eval_grader.py
uv run pytest tests/integration/test_eval_worker.py tests/integration/test_eval_docker.py
```

Raw `EvalTrial` JSONL is flushed and synchronized after every completed observation. It may include model-produced patches, so keep raw experiments private while the heldout protocol remains active. CSV and HTML exports contain outcomes/metrics without private target source or reference fixes:

```bash
uv run python -m forge.evals.cli report .forge/evaluations/EXPERIMENT/trials.jsonl \
  --output .forge/evaluations/EXPERIMENT/report
```

The report contains per-trial values, exact/uncertain cost, failure categories, all-started and infrastructure-excluded success denominators, median/p95 latency, token/phase measurement gaps, and task-paired comparisons. Bootstrap intervals resample tasks after averaging each task's repeated observations. Unmeasured stale-fact dependence or phase timing remains missing; the reporter does not invent zero measurements. Mechanical evidence-guard tests quantify known source leaks; conclusions about model reasoning require actual model experiments and error analysis.
