# Immutable initial migration validation

**Passed against PostgreSQL 17.11 on 2026-09-08**, from 07:30:50 to 07:30:52 UTC.
The checked-in audit script created a new empty database, applied revision `0001`,
checked parity with the SQLAlchemy models, downgraded to base, and applied the same
revision again. All four Alembic commands exited successfully. The temporary
database was then dropped.

[Machine-readable evidence](migration.json) contains each command's exit code and
output, the actual table/revision state after every command, and reflected column,
primary key, foreign key, uniqueness and index definitions. This report was
prepared by an independent development Agent from those results. The main Agent
executed the script because the independent Agent's shell could not access local
PostgreSQL; assertions and evidence inspection remained independent.

## Exact observation

The randomly generated database was `forge_migration_check_b7bd10bde451`.
Inspection confirmed it had no tables before migration. The existing `forge`
application database was never an Alembic target.

| Step | Exit | Business tables | Recorded revision | Observation |
| --- | --- | --- | --- | --- |
| `python -m alembic upgrade head` | 0 | 11 | `0001` | Initial schema created |
| `python -m alembic check` | 0 | 11 | `0001` | `No new upgrade operations detected.` |
| `python -m alembic downgrade base` | 0 | 0 | None | Only the empty `alembic_version` table remained |
| `python -m alembic upgrade head` | 0 | 11 | `0001` | Reflected schema matched the first upgrade |
| Scratch database cleanup | Successful | — | — | Dropped only the database created by this audit |

The 11 business tables were:

```text
approvals       artifacts       eval_trials       evaluations
model_attempts  model_turns      projects          run_events
runs            tool_executions user_messages
```

Reflection found 11 primary keys, 10 foreign keys, six unique constraints,
17 indexes including uniqueness backing indexes, and 16 PostgreSQL `JSONB`
columns. The structural comparison includes column types, nullability, defaults,
primary keys, foreign keys, unique constraints and index definitions; it does not
infer equality from the table count alone.

The initial and final reflected schema SHA-256 values were identical:

```text
7ae2792c3112d0d2de91fd2ba53f0e4cb1221fb308ad264cd8fdad003789de3a
```

## Source and environment identity

The initial migration contains explicit `op.create_table` operations rather than
calling the current application's `Base.metadata.create_all`. Alembic's `check`
still compares the migrated database to current model metadata, independently of
how the migration creates the schema.

| Component | Verified value |
| --- | --- |
| Migration | `migrations/versions/0001_ledger.py` |
| Migration SHA-256 | `01d3c50e468291b6a31d7e3991779850e2aac3838fd80c5a35bebeeb6bdeecc3` |
| Audit script | `scripts/check_migration.py` |
| Audit script SHA-256 | `2b7a6f9c737c4b7af4aabd594aef93f27b31bab184a6cb004ac3b0a65ffeca1f` |
| PostgreSQL | 17.11, Linux x86-64 |
| Alembic | 1.19.2 |
| SQLAlchemy | 2.0.52 |
| psycopg | 3.3.5 |

Both recorded source hashes were recomputed against the current files before
this report was written. The audit script passed Ruff and mypy. The saved evidence
contains no database password, model credential, or personal absolute path.

## Reproduce

From the Forge repository root, use the locked environment and an accessible
PostgreSQL server. The configured account needs permission to create a database.
The script reads credentials through Forge `Settings`; it does not embed them.

```bash
.venv/bin/python scripts/check_migration.py \
  --output docs/validation/migration.json
```

To use a separate server or administrative account, pass a PostgreSQL URL through
the optional argument. The URL is an operator-supplied value and is not retained
in the evidence:

```bash
.venv/bin/python scripts/check_migration.py \
  --admin-url "$FORGE_MIGRATION_ADMIN_URL" \
  --output docs/validation/migration.json
```

The script forces the management connection to `postgres`, generates a new
`forge_migration_check_<random>` database, and supplies that exact scratch URL as
`FORGE_DATABASE_URL` to every Alembic subprocess. It verifies the actual selected
database before migration and after every command. Cleanup uses the generated
database identifier only, without `FORCE` or terminating shared connections.
Run the script rather than manually downgrading the application database.

The checked-in CI workflow invokes this same script and retains the JSON evidence.
This report establishes the local execution above; it does not claim a completed
remote CI run.

## Scope

This is an empty-database schema round trip for the immutable initial business
migration. It verifies model/schema parity and reversible table creation on real
PostgreSQL. LangGraph owns its separate checkpoint schema and is outside these
11 business tables. This audit does not establish data-preserving downgrades,
production backup/restore, or behavior under concurrent schema changes.
