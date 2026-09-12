# What the first live runs exposed

These observations come from the public cursor demonstration, before any heldout
trial. They explain two recorded development revisions; they are not a benchmark
score or a claim about all DeepSeek workloads.

**A conservative context estimator needs a usable configured envelope.** The
initial 16K allowance counts UTF-8 bytes, tool schemas, mandatory instructions,
the latest complete tool transaction and the output reserve. After two real model
responses, the mandatory total was 16,147. The runtime correctly stopped before
another request, but the initial allowance was too small for this tool cycle.
Replaying the same saved transcript through the deterministic builder reproduced
the failure at 16K; 32K produced 18,249 estimated input tokens plus the 4,096 output
reserve. No model resampling was needed to diagnose that configuration error.

**Request limits and dollar limits answer different questions.** With 32K context,
the next Flash run repeatedly produced invalid unified-diff headers/counts, then
applied a correct patch and passed the focused and full public suites. It consumed
12 requests before calling `finish`. The system therefore retained
`budget_exhausted` and `unverified`, even though its last command passed tests.
Its peak-tariff cost upper bound was only $0.015467496: the active constraint was
the attempt count, not dollars. The final pre-heldout configuration raises the
attempt ceiling to 24 while retaining the per-run dollar, tool and output limits.
The tool parser and acceptance rules were not relaxed to obtain a pass.

**A development rerun must have a new identity and an accounted allocation.** Both
unsuccessful runs retain their original IDs, definitions, usage and failure
evidence. Two new $0.20 development slots use previously unallocated money under
the same $10 authorization. The campaign records the complete prior plans and
hashes in an atomic revision log, and refuses changes after a heldout trial has
started. The eventual successful Flash/Pro runs are separate observations, not
replacements for the failures.

**Accounted does not mean verified.** Reconciliation can mark a slot's money
settled while its run remains unsuccessful. The continuation gate checks a live
`completed` run, independent `verified` result, current configuration hash,
matching run ID, rootless receipts and unchanged source. Treating a settled slot
as success would have admitted the first failed Flash run into the evaluation.

The final public demonstrations both met that gate. Their costs, including the
two preserved failures, are independently audited in
[the live review](../reviews/deepseek-live-review.md). Claims about comparative
repair quality require the separately frozen 72-trial dataset and its retained
failure rows.
