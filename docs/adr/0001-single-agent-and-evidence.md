# 0001: A single coding agent with versioned evidence

Status: accepted. Scope: Linux, one trusted user, Python repositories.

The product runs one reasoning loop per task. Parallelism is reserved for read-only tools and independent runs. Adding a planner/developer/reviewer team inside every task would multiply model calls and make attribution harder before the baseline is measured. Independent agents were used to develop and review this project; that development process is separate from the product runtime.

The experimental feature is an Evidence Ledger: code observations carry a file hash, line range, origin call, and current/stale/missing status. Validation observations additionally bind a workspace snapshot and trusted profile hash. Before building the next model context, Forge refreshes these facts, including after arbitrary sandbox commands. Old facts remain available in artifacts but cannot silently return as current code.

This is an engineering hypothesis, not a claim of academic novelty. B2 versus B2_guard_off holds the task, model and budget fixed. We report cost, independent repair success, context size and mechanically observable stale evidence leakage. A reduction in stale prompt content alone does not establish a repair-quality improvement. Negative results and extra read/scan overhead are valid outcomes.

Consequences: full snapshots limit repository size and incur I/O; source annotations can become stale even when their surrounding summary sounds reasonable. The current implementation deliberately favors explicit uncertainty and rereading over unverifiable summary claims.
