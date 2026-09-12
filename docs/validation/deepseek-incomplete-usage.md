# DeepSeek terminal-usage accounting postmortem

Audit: **2026-09-12 05:46 UTC / 2026-09-11 America/Los_Angeles**.
Campaign: `deepseek-10usd-20260911-v1`, all **72 heldout trials** recorded.

Five runs ended with a provider-confirmed `response.incomplete` event. Their
persisted attempt records contain complete token counts, but the adapter marked
those counts as nonfinal. This is an accounting classification defect. Withholding
the incomplete tool batch was correct. The original trial records remain
`cost_status: unknown`, and their campaign slots retain **$0.60** in total.
This audit changes neither accounting records nor experiment outcomes.

The [DeepSeek Responses specification](https://api-docs.deepseek.com/guides/responses_api/)
identifies `response.incomplete` as a terminal event carrying the response object.
It differs from an interrupted connection that never delivers a terminal event.
The specification also defines cached input and reasoning output as subsets of
their respective token totals.

## Mechanism and evidence

During the pilot, the [DeepSeek adapter](../../src/forge/providers/deepseek.py) delegated streaming to
[OpenAIProvider._stream_once](../../src/forge/providers/openai.py#L150). That audited
`response.incomplete` branch emitted `UsageUpdate` without `final=True`, then raised
the error `Response terminated with response.incomplete; tools withheld.`
[BaseProvider.stream](../../src/forge/providers/base.py#L253) consequently persisted
`usage_final: false` and `usage.estimated: true`.
[Runtime._attempt](../../src/forge/runtime/engine.py#L275) preserves that treatment;
[BudgetManager.reconcile](../../src/forge/context/budget.py#L172) retains the attempt
reservation. Finally, [CampaignLedger.settle](../../src/forge/evals/campaign.py#L197)
retains the whole original slot allocation for uncertain accounting.

Read-only queries against the dedicated database
`forge_deepseek_10usd_20260911_v1` found exactly one such failed attempt in each
affected run. Every one has input, cached-input, output and reasoning counters;
the cache/reasoning subset checks pass. Each reports **4,096 output tokens**,
matching its recorded output limit, and **zero reasoning tokens**. This supports
output truncation as the explanation, but the precise `incomplete_details` reason
was not retained.

| Task | Condition | Iteration | Input / cached input | Derived incomplete-attempt ceiling | Derived whole-run ceiling |
| --- | --- | ---: | ---: | ---: | ---: |
| `parallel-context` | B1 | 3 | 2,704 / 2,304 | $0.005049024 | $0.005837064 |
| `line-reference` | strong_only | 5 | 6,643 / 1,280 | $0.023355640 | $0.052016096 |
| `patch-preflight` | strong_only | 7 | 4,436 / 1,280 | $0.020442400 | $0.038687880 |
| `utf8-backpressure` | strong_only | 8 | 4,055 / 1,280 | $0.019939480 | $0.033325160 |
| `parallel-context` | strong_only | 2 | 2,403 / 1,152 | $0.017922168 | $0.019339320 |

These are **offline, tariff-derived upper bounds**, conditional on the persisted
provider counters; they are not replacement ledger values or invoice totals.
They use the original reservation's frozen peak prices per million tokens:
Flash input/cache/output **$0.30 / $0.006 / $1.20**; Pro **$1.32 / $0.044 / $3.96**.
For each attempt the calculation is
`((input - cached) * input_rate + cached * cache_rate + output * output_rate) / 1,000,000`.
Reasoning tokens are not added again.

| Scope | Amount or count |
| --- | ---: |
| Five incomplete attempts: input / cached / output / reasoning | 20,241 / 7,296 / 20,480 / 0 |
| Their unchanged attempt reservations | $0.161744340 |
| Their derived usage-based tariff ceiling | $0.086708712 |
| Already-settled earlier attempts within those five runs | $0.062496808 |
| Derived ceiling for all attempts in those five runs | $0.149205520 |
| **Unchanged campaign allocation for those five unknown slots** | **$0.600000000** |

The last row already covers the entire five slots. It must not be added to their
attempt reservations or derived costs when calculating campaign exposure.

## Provenance and limits

Across the 72 heldout runs, the database contains **823 model attempts**:
815 completed and eight failed. Three failures are invalid tool arguments with
`usage_final: true`; five are the incomplete events above with `usage_final: false`.
Thus 818 attempts have final usage recorded. The 815 completed model turns report
681 instances of `deepseek-flash` and 134 of `deepseek-v4-pro`, matching requested
aliases. These returned aliases do not establish an immutable underlying model
checkpoint or independently confirm the price-version label.

The failed [ModelTurn records](../../src/forge/runtime/engine.py#L425) contain only
the normalized error and have `artifact_ref: null`. The original terminal SSE,
response ID and `incomplete_details` were not persisted. The audit therefore rests
on the explicit terminal-event error, normalized usage, frozen prices and the
reviewed code path. It cannot independently replay the provider's raw billing
receipt. The conservative original unknown classification remains the reporting
source of truth; the calculations above explain its likely overstatement.

Run identifiers, in table order:

- `5ded2800ec0549abbebf90defe495a11`
- `7823781e0ef74b4eb8fc2d7f51b2e365`
- `94faf330b0e5472bb3467a6da7ba24f3`
- `a06535294b9643acb0fdba4b84aa6dd3`
- `df1c912139ec4e769638abb0a784ccff`

Local evidence source:
`.forge/campaigns/deepseek-10usd-20260911-v1/trials.jsonl`.
SHA-256 at audit:
`0eab40c8ca55bbea441d8dd79cc84b30c67a5b24430d48aaf80c7a1d1e050345`.
Audited **pre-fix** `src/forge/providers/openai.py` SHA-256:
`7e64a051110360f1e6f6b269ddae69880dc49baa2f2098272be4a69d9fbc0c1a`.
No wallet balance, credentials, model prompts, candidate patches or private grader
answers were included in this audit.

## Follow-up acceptance criteria — not implemented in this experiment

1. A real-SDK MockTransport contract receives terminal `response.incomplete` with
   valid complete usage. It records final billing usage while emitting the error
   and **no** `ToolCallReady` or `TurnCompleted` event.
2. Abrupt EOF, transport failure, cancellation and nonterminal cumulative counters
   retain uncertain accounting even when both input/output counters were observed.
   Partial streams must not trigger an automatic replay.
3. Missing usage or invalid token subsets cannot settle a known bill. Test
   `response.failed` separately against documented terminal-usage semantics rather
   than treating every exception as a final bill.
4. A PostgreSQL runtime test proves a failed generation can settle usage at the
   frozen tariff ceiling without executing a tool batch. Cache/reasoning subsets,
   replay protection and campaign-level spend limits remain enforced.
5. Persist a bounded terminal billing receipt containing event type, response ID,
   usage and incomplete reason, with integrity metadata. Any later reconciliation
   must append an auditable adjustment and preserve these original trials and
   reservations. This completed campaign must not be silently rerun or rewritten.

## Post-campaign fix — 2026-09-12 UTC / 2026-09-11 Pacific

After all 72 trials completed, the shared Responses adapter was corrected for
**future runs**. Terminal `response.incomplete` and `response.failed` events now
settle complete, consistent token usage independently of tool success. Event and
response statuses must agree; invalid subsets or inconsistent totals cannot
release a reservation. Absent or partial usage remains unknown. Neither terminal
failure emits executable tools, `TurnCompleted`, or an implicit retry. This follows
the [OpenAI response schema](https://developers.openai.com/api/reference/python/resources/responses/methods/retrieve)
and the DeepSeek terminal-event contract linked above; usage is optional and must
not be invented when absent.

The normalizer now retains reported `cache_write_tokens`. A complete token receipt
with an unconfigured cache-write price still keeps its cost reservation. Complete
usage at a frozen peak tariff remains `upper_bound`, not invoice-exact.

Validation for this subsequent change:

- [SDK contracts](../../tests/contract/test_providers.py), together with the
  [DeepSeek contracts](../../tests/contract/test_deepseek.py): **124 passed** in
  1.70 seconds. The 38 added cases cover both providers, both terminal failure
  types, full/missing/partial/invalid usage, status mismatch, unknown cache-write
  pricing, EOF and socket interruption. The first restricted-sandbox attempt timed
  out; the successful rerun used MockTransport outside that sandbox and no API.
- [PostgreSQL/rootless runtime regression](../../tests/integration/test_runtime.py):
  the complete-usage case failed before this fix while partial/missing cases passed.
  Afterward **all three passed** in 4.72 seconds: a single synthetic HTTP call,
  failed run, no tool execution; complete usage settled at **$0.00006612** with
  `upper_bound`, and partial/missing usage kept their reservations. See the
  [JUnit record](deepseek-incomplete-runtime-junit.xml).
- Ruff and mypy passed for the changed provider. No paid request or trial rerun
  was performed for the fix.

The five historical unknown slots still reserve **$0.60**. Their original flags,
raw trials, configuration and results were not reclassified. Persisting a bounded
raw terminal billing receipt remains follow-up work; this patch does not recover
the missing original responses.
