"""Validate Forge's immutable initial migration in a newly created scratch DB.

Run from the Forge repository with its locked Python environment. This script
never runs Alembic against the default application database. Only the generated
scratch database is created, migrated, queried, and dropped.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from forge.settings import Settings

EXPECTED_TABLES = sorted(
    [
        "approvals",
        "artifacts",
        "eval_trials",
        "evaluations",
        "model_attempts",
        "model_turns",
        "projects",
        "run_events",
        "runs",
        "tool_executions",
        "user_messages",
    ]
)
SCRATCH = "forge_migration_check_" + uuid.uuid4().hex[:12]
assert re.fullmatch(r"forge_migration_check_[a-f0-9]{12}", SCRATCH)
assert SCRATCH != "forge"


def schema_snapshot(url: str) -> dict[str, Any]:
    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        names = sorted(inspector.get_table_names(schema="public"))
        business = [name for name in names if name != "alembic_version"]
        with engine.connect() as connection:
            database = connection.execute(text("select current_database()")).scalar_one()
            assert database == SCRATCH, "Migration inspection selected the wrong database"
            revisions = (
                connection.execute(text("select version_num from alembic_version")).scalars().all()
                if "alembic_version" in names
                else []
            )
        details = {}
        for table in business:
            details[table] = {
                "columns": [
                    {
                        "name": col["name"],
                        "type": str(col["type"]),
                        "nullable": col["nullable"],
                        "default": col.get("default"),
                    }
                    for col in inspector.get_columns(table, schema="public")
                ],
                "primary_key": inspector.get_pk_constraint(table, schema="public"),
                "foreign_keys": inspector.get_foreign_keys(table, schema="public"),
                "unique_constraints": inspector.get_unique_constraints(table, schema="public"),
                "indexes": inspector.get_indexes(table, schema="public"),
            }
        encoded = json.dumps(details, sort_keys=True, separators=(",", ":"), default=str).encode()
        return {
            "database": database,
            "all_tables": names,
            "business_tables": business,
            "business_table_count": len(business),
            "revisions": revisions,
            "schema_sha256": hashlib.sha256(encoded).hexdigest(),
            "schema": details,
        }
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--admin-url",
        default=None,
        help="PostgreSQL server credentials; defaults to Forge Settings. Database is forced to postgres.",
    )
    parser.add_argument("--output", type=Path, default=Path("docs/validation/migration.json"))
    args = parser.parse_args()
    base_url = make_url(args.admin_url or Settings().database_url)
    if base_url.drivername not in {"postgresql", "postgresql+psycopg"}:
        parser.error("Migration audit requires PostgreSQL with psycopg")
    admin_url = base_url.set(drivername="postgresql", database="postgres")
    root = Path.cwd()
    migration = root / "migrations/versions/0001_ledger.py"
    assert (root / "alembic.ini").is_file() and migration.is_file(), (
        "Run from the Forge repository root"
    )
    assert "Base.metadata" not in migration.read_text(), (
        "Initial migration must be independent of current model metadata"
    )
    report: dict[str, Any] = {
        "started_at_utc": datetime.now(UTC).isoformat(),
        "database": SCRATCH,
        "result": "in_progress",
        "steps": [],
        "cleanup": "not_created",
        "migration_file": "migrations/versions/0001_ledger.py",
        "migration_sha256": hashlib.sha256(migration.read_bytes()).hexdigest(),
        "audit_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "versions": {
            package: importlib.metadata.version(package)
            for package in ("alembic", "SQLAlchemy", "psycopg")
        },
    }
    admin = psycopg.connect(
        admin_url.render_as_string(hide_password=False),
        autocommit=True,
        connect_timeout=5,
        application_name="forge-migration-audit-admin",
    )
    created = False
    scratch_url = base_url.set(drivername="postgresql+psycopg", database=SCRATCH)
    audit_url = scratch_url.render_as_string(hide_password=False)
    assert make_url(audit_url).database == SCRATCH
    child_env = {**os.environ, "FORGE_DATABASE_URL": audit_url}
    try:
        version_row = admin.execute("select version()").fetchone()
        assert version_row is not None
        report["postgresql_version"] = version_row[0]
        admin.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(SCRATCH))
        )
        created = True
        report["cleanup"] = "pending"
        initial = schema_snapshot(audit_url)
        assert initial["all_tables"] == [], "Generated database was not empty"
        report["initial"] = initial
        first_fingerprint = None
        for argv in (["upgrade", "head"], ["check"], ["downgrade", "base"], ["upgrade", "head"]):
            assert make_url(child_env["FORGE_DATABASE_URL"]).database == SCRATCH
            result = subprocess.run(
                [sys.executable, "-m", "alembic", *argv],
                cwd=root,
                env=child_env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            step = {
                "command": "python -m alembic " + " ".join(argv),
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
            report["steps"].append(step)
            assert result.returncode == 0, f"Migration command failed: {step['command']}"
            snapshot = schema_snapshot(audit_url)
            step["snapshot"] = snapshot
            if argv == ["downgrade", "base"]:
                assert snapshot["business_tables"] == [], "Downgrade left business tables behind"
                assert snapshot["revisions"] == [], "Downgrade left a revision head"
            else:
                assert snapshot["business_tables"] == EXPECTED_TABLES, (
                    "Business table set differs from the 11-table contract"
                )
                assert snapshot["revisions"] == ["0001"], "Unexpected migration revision"
                if first_fingerprint is None:
                    first_fingerprint = snapshot["schema_sha256"]
                else:
                    assert snapshot["schema_sha256"] == first_fingerprint, (
                        "Round-trip changed reflected columns/constraints/indexes"
                    )
            if argv == ["check"]:
                assert "No new upgrade operations detected" in result.stdout, (
                    "Alembic did not confirm metadata parity"
                )
        report["result"] = "passed"
    except Exception as error:
        report["result"] = "failed"
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        if created:
            # No FORCE and no shared-connection termination. This drops only the
            # scratch database created by this process, after closing all engines.
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(SCRATCH)))
            report["cleanup"] = "dropped_scratch_database"
        admin.close()
        report["finished_at_utc"] = datetime.now(UTC).isoformat()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n")
        print(
            json.dumps(
                {key: report[key] for key in ("result", "database", "cleanup", "migration_sha256")},
                indent=2,
            )
        )
        print(f"Evidence: {args.output}")


if __name__ == "__main__":
    main()
