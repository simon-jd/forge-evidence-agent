"""Real CLI -> HTTP -> separate worker -> Docker; no in-process app transport."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

pytestmark = [pytest.mark.postgres, pytest.mark.docker]


def test_public_cli_demo_verifies_cursor_fix_after_observer_disconnect(tmp_path: Path) -> None:
    if os.environ.get("FORGE_RUN_PUBLIC_CLIENT") != "1":
        pytest.skip("Set FORGE_RUN_PUBLIC_CLIENT=1 with a dedicated migrated database and no other queued work")
    database = os.environ.get("FORGE_TEST_DATABASE_URL")
    host = os.environ.get("FORGE_TEST_DOCKER_HOST") or os.environ.get("FORGE_DOCKER_HOST")
    if not database or not host:
        pytest.fail("Explicit public-client integration requires FORGE_TEST_DATABASE_URL and FORGE_TEST_DOCKER_HOST")
    project = Path(__file__).resolve().parents[2]
    output = Path(os.environ.get("FORGE_PUBLIC_CLIENT_EVIDENCE", str(tmp_path / "public-client.json")))
    argv = [sys.executable, str(project / "scripts/demo.py"), "--root", str(tmp_path / "demo"),
            "--output", str(output), "--database-url", database, "--docker-host", host,
            "--image", os.environ.get("FORGE_TEST_IMAGE", "forge-runner:dev"),
            "--port", os.environ.get("FORGE_PUBLIC_CLIENT_PORT", "18765")]
    rootful = os.environ.get("FORGE_ALLOW_ROOTFUL_TESTS") == "1"
    if rootful:
        argv.append("--allow-rootful-development")
    result = subprocess.run(argv, cwd=project, capture_output=True, text=True, timeout=240)
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(output.read_text())
    assert evidence["execution_mode"] == "offline_scripted"
    assert evidence["purpose"] == "public_client_control_flow" and not evidence["model_quality_claim"]
    assert evidence["status"] == "completed" and evidence["verification"]["status"] == "verified"
    checks = evidence["verification"]["checks"]
    assert checks["workspace_unchanged"]
    assert checks["regression"]["passed"] and checks["target"]["passed"]
    assert len(checks["regression"]["record"]["executed"]) == 6
    assert len(checks["target"]["record"]["executed"]) == 3
    assert evidence["tool_call_ids"] == ["demo-read", "demo-patch", "demo-focused-test", "demo-finish"]
    assert evidence["submission_idempotent"] and evidence["message_idempotent"]
    assert evidence["watch_disconnected_before_execution"] and evidence["separate_api_worker_processes"]
    assert evidence["sse_contiguous_after_resume"] and evidence["sse_final_cursor_empty"]
    assert evidence["source_unchanged"]
    assert "(event.timestamp_ms, event.sequence)" in evidence["patch"]
    assert "test_cursor.py" not in evidence["patch"]
    assert any(receipt["kind"] == "baseline_target_receipt" and receipt["exit_code"] != 0 for receipt in evidence["receipts"])
    assert any(receipt["kind"] == "target_receipt" and receipt["exit_code"] == 0 for receipt in evidence["receipts"])
    assert all(receipt["rootless"] for receipt in evidence["receipts"]) or rootful
    reservations = evidence["budget"]["reservations"].values()
    assert len(evidence["budget"]["reservations"]) == 4
    assert all(item["status"] == "settled" and Decimal(item["charged_usd"]) == 0 for item in reservations)
