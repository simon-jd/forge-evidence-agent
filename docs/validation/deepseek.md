# DeepSeek implementation evidence

Checked 2026-09-11. These are offline model transports with real local execution
where indicated. No paid DeepSeek request was sent; live quality is unmeasured.

| Check | Observed result | Evidence |
| --- | --- | --- |
| Unit and provider contracts | 202 passed, 0 skipped, 3.50 s | [JUnit](deepseek-offline-junit.xml) |
| PostgreSQL, rootless Docker, private grading, public client and independent review | 42 passed, 0 skipped, 46.99 s | [JUnit](deepseek-integration-junit.xml) |
| Ruff | Passed | `ruff check src scripts tests examples migrations` |
| mypy | Passed, 57 source/script files | `mypy src scripts/demo.py scripts/check_migration.py` |
| Fixed campaign preparation | 74 pending slots, $9.04 allocation, $10 authorization, $0 committed | Local ignored ledger; `--execute` was not used |

The two test suites contain **244 distinct tests**. DeepSeek's 38 SDK contracts,
12 campaign unit cases, four independent budget cases and one actual runtime
integration are included in those counts, not additional runs to add to them.

The integration injects only an SDK MockTransport. The real runtime and provider
factory construct the DeepSeek adapter. At the HTTP boundary the test reads the
already committed PostgreSQL reservation and verifies it covers the serialized
tool/history request plus framing. A three-turn read/patch/finish sequence passes
the independent rootless verifier. Complete synthetic usage settles three
attempts while preserving `price_basis=upper_bound`; cached input and reasoning
output are counted once. Its synthetic dollar amount is not real spending.

Independent review reproduced premature release for a nonterminal run with no
usage. The fix retains the entire remaining run ceiling; a second regression
preserves earlier settled usage while reserving the outstanding balance. Smoke
timeout and nonzero exit each keep $0.20 occupied and prevent subsequent work.
See the [review and its first-failure/final-pass record](../reviews/deepseek-budget-plan.md).

The first broad integration command omitted `FORGE_DOCKER_HOST`, while setting
the separate runtime test socket variable. Two independent-grader tests therefore
correctly refused the default rootful daemon; 40 other cases passed. Supplying
the same rootless socket to both variables produced the 42/42 result above.
No product fallback was enabled to make that check pass.

```bash
uv run --no-sync pytest tests/unit tests/contract -q
env FORGE_TEST_DATABASE_URL="$FORGE_DATABASE_URL" \
  FORGE_DOCKER_HOST="$FORGE_DOCKER_HOST" \
  FORGE_TEST_DOCKER_HOST="$FORGE_DOCKER_HOST" \
  FORGE_TEST_IMAGE=forge-runner:dev \
  FORGE_EVAL_PRIVATE_ROOT=.forge/private-evals \
  FORGE_REVIEW_DOCKER=1 FORGE_REVIEW_DOCKER_HOST="$FORGE_DOCKER_HOST" \
  FORGE_RUN_PUBLIC_CLIENT=1 \
  uv run --no-sync pytest tests/integration tests/review -q
```

Use the dedicated idle test database and the prequalified external grader store.
The verified rootless image remains
`sha256:88ea842ecc6d79db7d22c0cf911ea41b5ebec7c6846298eea1ae7a77dc2c7e15`.
Normal CI empties DeepSeek credentials and uses offline HTTP transports. Remote
rootful CI remains a development check, not evidence of rootless acceptance.
