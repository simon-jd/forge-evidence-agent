#!/usr/bin/env python3
"""An actual public-client/process/Docker demonstration, with zero paid calls by default.

Lifecycle commands use the installed Forge CLI or public HTTP only. This script
does not call Repository, Runtime, a provider, or a private verifier. Use a
dedicated migrated database without other workers or queued tasks: the production
worker's --once option claims the next eligible job, not a requested job ID.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import select
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import yaml

from forge.settings import Settings

PROJECT = Path(__file__).resolve().parents[1]
TASK = (
    "Fix event replay pagination so reconnecting with the complete (timestamp_ms, sequence) "
    "cursor never drops later events sharing the same timestamp. Keep ordering stable, "
    "exclude the exact boundary event, preserve the public API, and do not change public tests. "
    "Read the source, apply the correction, run the focused ResumeCursorTests, then propose completion."
)
TERMINAL = {"completed", "failed", "cancelled", "budget_exhausted", "needs_reconciliation"}


def command(argv: list[str], *, env: dict[str, str], cwd: Path = PROJECT, timeout: int = 30) -> str:
    completed = subprocess.run(argv, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout)
    if completed.returncode:
        raise RuntimeError(f"{Path(argv[0]).name} failed ({completed.returncode}): {completed.stderr[-2000:]}")
    return completed.stdout


def cli(*args: str, env: dict[str, str], timeout: int = 30) -> str:
    return command([str(Path(sys.executable).with_name("forge")), *args], env=env, timeout=timeout)


def materialize_source(root: Path, env: dict[str, str]) -> tuple[Path, str]:
    source = root / "source"
    fixture = PROJECT / "examples/demo-repository"
    if source.exists():
        # Documentation-only template updates do not rewrite an existing frozen
        # fixture's Git history. Source/tests must still match, and any local
        # change (including README edits) is rejected by git status below.
        for file in fixture.glob("*.py"):
            if (source / file.name).read_bytes() != file.read_bytes():
                raise ValueError("Existing demo source changed; choose a new --root to preserve it")
        if command(["git", "status", "--porcelain"], env=env, cwd=source).strip():
            raise ValueError("Existing demo source is dirty; choose a new --root")
    else:
        shutil.copytree(fixture, source, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        git_env = {**env, "GIT_AUTHOR_NAME": "Forge Demo", "GIT_AUTHOR_EMAIL": "demo@example.invalid",
                   "GIT_COMMITTER_NAME": "Forge Demo", "GIT_COMMITTER_EMAIL": "demo@example.invalid",
                   "GIT_AUTHOR_DATE": "2026-09-07T00:00:00+00:00", "GIT_COMMITTER_DATE": "2026-09-07T00:00:00+00:00"}
        command(["git", "init", "--quiet"], env=git_env, cwd=source)
        command(["git", "add", "README.md", "replay.py", "test_cursor.py"], env=git_env, cwd=source)
        command(["git", "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "Public cursor reproduction"], env=git_env, cwd=source)
    return source, command(["git", "rev-parse", "HEAD"], env=env, cwd=source).strip()


def offline_configuration(source: Path) -> dict[str, Any]:
    before = (source / "replay.py").read_text()
    old = "        ordered = [event for event in ordered if event.timestamp_ms > after.timestamp_ms]\n"
    new = ("        ordered = [event for event in ordered\n"
           "                   if (event.timestamp_ms, event.sequence) > (after.timestamp_ms, after.sequence)]\n")
    if before.count(old) != 1:
        raise ValueError("The public demonstration source changed; review its scripted fixture")
    import difflib
    patch = "".join(difflib.unified_diff(before.splitlines(keepends=True), before.replace(old, new).splitlines(keepends=True),
                                       fromfile="a/replay.py", tofile="b/replay.py"))
    calls = [
        {"call_id": "demo-read", "name": "read_file", "arguments": {"relative_path": "replay.py"}},
        {"call_id": "demo-patch", "name": "apply_patch", "arguments": {"patch": patch,
            "expected_file_hashes": {"replay.py": hashlib.sha256(before.encode()).hexdigest()}}},
        {"call_id": "demo-focused-test", "name": "run_command", "arguments": {
            "argv": ["python", "-m", "unittest", "test_cursor.ResumeCursorTests"], "timeout_seconds": 15}},
        {"call_id": "demo-finish", "name": "finish", "arguments": {
            "summary": "Offline scripted correction of the composite cursor; independent validation must decide success.",
            "claimed_changes": ["Compare both timestamp and sequence when resuming event pages."]}},
    ]
    return {"execution_mode": "offline_scripted", "run": {
        "models": [{"provider": "fake", "model_id": "scripted", "input_usd_per_million": "0",
                    "output_usd_per_million": "0", "cached_input_usd_per_million": "0",
                    "cache_write_usd_per_million": "0", "price_version": "offline-zero"}],
        "max_escalations": 0, "require_write_approval": False,
        "context": {"strategy": "evidence_compact", "input_budget": 24000},
        "budget": {"max_cost_usd": "0.01", "max_model_turns": 4, "max_tool_calls": 4,
                   "max_repair_rounds": 0, "max_runtime_seconds": 120}},
        "scripted_turns": [{"provider": "fake", "requested_model": "scripted",
            "visible_text": f"Offline control-flow fixture step {index + 1}; no model inference.",
            "complete_tool_calls": [call], "finish_reason": "tool_calls",
            "usage": {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0,
                      "cache_write_tokens": 0}} for index, call in enumerate(calls)]}


def stop(process: subprocess.Popen[str] | None) -> None:
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def demonstrate(args: argparse.Namespace) -> dict[str, Any]:
    settings = Settings()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    session_id = uuid.uuid4().hex
    session = root / "sessions" / session_id
    session.mkdir(parents=True, mode=0o700)
    env = dict(os.environ)
    source, base_commit = materialize_source(root, env)
    source_hash = hashlib.sha256((source / "replay.py").read_bytes()).hexdigest()
    config = offline_configuration(source)
    if args.live_config is not None:
        config = yaml.safe_load(args.live_config.read_text())
        if config.get("execution_mode") != "live" or config.get("scripted_turns"):
            raise ValueError("--live-config must contain execution_mode: live and no scripted turns")
        if args.authorized_budget_usd is None:
            raise ValueError("Live demo requires an explicit --authorized-budget-usd amount")
        declared = Decimal(str(config["run"]["budget"]["max_cost_usd"]))
        authorized = Decimal(args.authorized_budget_usd)
        if not declared.is_finite() or not authorized.is_finite() or declared <= 0 or declared > authorized:
            raise ValueError("The live configuration must fit the explicitly authorized positive budget")
    elif args.authorized_budget_usd is not None:
        raise ValueError("Budget authorization is meaningful only with --live-config")
    config_dir = session / "config"
    for group in ("runs", "repo_profiles"):
        (config_dir / group).mkdir(parents=True)
    (config_dir / "runs/demo.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    profile = {"name": "public-composite-cursor", "image": args.image,
               "validation_argv": ["python", "-m", "unittest", "test_cursor"],
               "target_test_argv": ["python", "-m", "unittest", "test_cursor.ResumeCursorTests"],
               "allowed_patch_paths": ["replay.py"], "protected_paths": ["test_cursor.py", ".git/**", ".forge/**"],
               "command_timeout_seconds": 15}
    (config_dir / "repo_profiles/demo.yaml").write_text(yaml.safe_dump(profile, sort_keys=False))
    token = secrets.token_urlsafe(32)
    env.update({"FORGE_DATABASE_URL": args.database_url or settings.database_url,
                "FORGE_API_TOKEN": token, "FORGE_OWNER_ID": "demo-" + session_id,
                "FORGE_API_URL": f"http://127.0.0.1:{args.port}",
                "FORGE_DATA_DIR": str(session / "data"), "FORGE_CONFIG_DIR": str(config_dir),
                "FORGE_ALLOWED_REPO_ROOTS": json.dumps([str(source)]),
                "FORGE_DOCKER_HOST": args.docker_host or settings.docker_host or "",
                "FORGE_ALLOW_ROOTFUL_DEVELOPMENT": "true" if args.allow_rootful_development else "false",
                "FORGE_ENABLE_EVALUATIONS": "false", "FORGE_WORKER_POLL_SECONDS": "0.1",
                "PYTHONUNBUFFERED": "1"})
    if not env["FORGE_DOCKER_HOST"]:
        raise ValueError("Set FORGE_DOCKER_HOST or --docker-host to an explicit Docker endpoint")
    # Offline processes receive no model credentials, including those in .env;
    # environment values override .env entries read by Settings.
    if config["execution_mode"] == "offline_scripted":
        for key in ("OPENAI_API_KEY", "FORGE_OPENAI_API_KEY", "ANTHROPIC_API_KEY", "FORGE_ANTHROPIC_API_KEY",
                    "DEEPSEEK_API_KEY", "FORGE_DEEPSEEK_API_KEY"):
            env[key] = ""
    session_env = session / "client.env"
    session_env.write_text("\n".join(f"export {key}={shlex.quote(env[key])}" for key in ("FORGE_API_URL", "FORGE_API_TOKEN")) + "\n")
    session_env.chmod(0o600)
    api_process: subprocess.Popen[str] | None = None
    worker: subprocess.Popen[str] | None = None
    watcher: subprocess.Popen[str] | None = None
    run_id: str | None = None
    worker_pids: list[int] = []
    started = time.monotonic()
    with (session / "api.log").open("w") as api_log, (session / "worker.log").open("w") as worker_log:
        try:
            api_process = subprocess.Popen([sys.executable, "-m", "uvicorn", "forge.api.main:app",
                "--host", "127.0.0.1", "--port", str(args.port), "--log-level", "warning"],
                cwd=PROJECT, env=env, text=True, stdout=api_log, stderr=subprocess.STDOUT)
            with httpx.Client(base_url=env["FORGE_API_URL"], headers={"Authorization": f"Bearer {token}"}, timeout=10) as http:
                deadline = time.monotonic() + 30
                while True:
                    if api_process.poll() is not None:
                        raise RuntimeError(f"API startup failed; see private log {session / 'api.log'}")
                    try:
                        if http.get("/openapi.json").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    if time.monotonic() > deadline:
                        raise TimeoutError("API did not become ready")
                    time.sleep(0.1)
                project = json.loads(cli("project", str(source), "--name", "Composite cursor demo", "--profile", "demo", env=env))
                submission = ["run", project["id"], TASK, "--config", "demo", "--base-commit", base_commit, "--key", "demo-" + session_id]
                submitted = json.loads(cli(*submission, env=env))
                run_id = submitted["id"]
                repeated = json.loads(cli(*submission, env=env))
                if repeated["id"] != run_id:
                    raise AssertionError("Idempotent CLI submission created a second run")
                notice = "Retain all same-timestamp events; preserve public acceptance tests."
                message = json.loads(cli("message", run_id, notice, "--key", "message-" + session_id, env=env))
                repeated_message = json.loads(cli("message", run_id, notice, "--key", "message-" + session_id, env=env))
                if repeated_message["id"] != message["id"]:
                    raise AssertionError("Idempotent CLI message created a second instruction")
                watcher = subprocess.Popen([str(Path(sys.executable).with_name("forge")), "watch", run_id],
                    cwd=PROJECT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                assert watcher.stdout is not None
                ready, _, _ = select.select([watcher.stdout], [], [], 10)
                if not ready:
                    raise TimeoutError("Public CLI event stream did not produce a durable event")
                first_event = json.loads(watcher.stdout.readline())
                stop(watcher)
                watcher = None
                # The observer has disconnected. A separate production worker
                # now performs the queued work without a watching client.
                worker = subprocess.Popen([sys.executable, "-m", "forge.worker", "--once"],
                    cwd=PROJECT, env=env, text=True, stdout=worker_log, stderr=subprocess.STDOUT)
                worker_pids.append(worker.pid)
                deadline = time.monotonic() + args.timeout
                approval_announced = False
                while True:
                    final = json.loads(cli("status", run_id, env=env))
                    if final["status"] in TERMINAL:
                        break
                    if final["status"] == "waiting_approval" and not approval_announced:
                        print(f"Review run {run_id}: in another terminal source {session_env}, then forge status {run_id}.", file=sys.stderr)
                        approval_announced = True
                    if worker.poll() is not None:
                        if final["status"] == "queued" and approval_announced:
                            worker = subprocess.Popen([sys.executable, "-m", "forge.worker", "--once"],
                                cwd=PROJECT, env=env, text=True, stdout=worker_log, stderr=subprocess.STDOUT)
                            worker_pids.append(worker.pid)
                            approval_announced = False
                        elif final["status"] != "waiting_approval":
                            raise RuntimeError(f"Worker exited before the demo finished; inspect {session / 'worker.log'} and any other queued tasks")
                    if time.monotonic() > deadline:
                        raise TimeoutError("Demo deadline reached; inspect retained session logs")
                    time.sleep(0.15)
                rest = [json.loads(line) for line in cli("watch", run_id, "--after", str(first_event["seq"]), env=env).splitlines() if line]
                events = [first_event, *rest]
                sequences = [event["seq"] for event in events]
                if sequences != list(range(1, final["event_seq"] + 1)):
                    raise AssertionError("Public CLI resume lost or duplicated durable event sequences")
                empty_replay = cli("watch", run_id, "--after", str(final["event_seq"]), env=env)
                if empty_replay.strip():
                    raise AssertionError("Replaying the final cursor repeated an event")
                patch = cli("diff", run_id, env=env)
                artifacts_response = http.get(f"/v1/runs/{run_id}/artifacts")
                artifacts_response.raise_for_status()
                receipts = []
                for artifact in artifacts_response.json():
                    if artifact["kind"].endswith("receipt"):
                        response = http.get(f"/v1/artifacts/{artifact['id']}")
                        response.raise_for_status()
                        receipt = response.json()
                        receipts.append({"kind": artifact["kind"], **{key: receipt[key] for key in (
                            "status", "exit_code", "rootless", "image_id", "workspace_revision", "limits") if key in receipt}})
                if hashlib.sha256((source / "replay.py").read_bytes()).hexdigest() != source_hash:
                    raise AssertionError("Original source repository changed")
                if final["status"] != "completed" or final["verification"].get("status") != "verified":
                    raise AssertionError(f"Demo did not independently verify: {final['status']} {final['verification']}")
                if not args.allow_rootful_development and (not receipts or not all(receipt["rootless"] for receipt in receipts)):
                    raise AssertionError("A default demo command did not use verified rootless execution")
                evidence = {
                    "observed_at": datetime.now(UTC).isoformat(), "execution_mode": config["execution_mode"],
                    "purpose": "public_client_control_flow" if args.live_config is None else "single_live_public_demo",
                    "model_quality_claim": False, "run_id": run_id, "base_commit": base_commit,
                    "status": final["status"], "verification": final["verification"], "budget": final["budget"],
                    "manifest": final["manifest"], "elapsed_seconds": round(time.monotonic() - started, 3),
                    "event_count": len(events), "event_types": [event["type"] for event in events],
                    "tool_call_ids": [event["payload"]["call_id"] for event in events if event["type"] == "tool.finished"],
                    "source_unchanged": True, "submission_idempotent": True, "message_idempotent": True,
                    "watch_disconnected_before_execution": True, "sse_contiguous_after_resume": True,
                    "sse_final_cursor_empty": True, "separate_api_worker_processes": api_process.pid not in worker_pids,
                    "receipts": receipts, "patch": patch,
                }
                (session / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
                return evidence
        except BaseException:
            if run_id is not None and api_process is not None and api_process.poll() is None:
                try:
                    cli("cancel", run_id, env=env)
                except (RuntimeError, subprocess.TimeoutExpired):
                    pass
            raise
        finally:
            stop(watcher)
            stop(worker)
            stop(api_process)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PROJECT / ".forge/demo")
    parser.add_argument("--output", type=Path, default=PROJECT / ".forge/demo/latest.json")
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--database-url", help="Dedicated migrated PostgreSQL URL; otherwise Settings/.env")
    parser.add_argument("--docker-host", help="Explicit Docker socket; otherwise Settings/.env")
    parser.add_argument("--image", default="forge-runner:dev")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--live-config", type=Path, help="Manually prepared live RunFile; default is zero-cost offline_scripted")
    parser.add_argument("--authorized-budget-usd", help="Explicit authorization cap for --live-config")
    parser.add_argument("--allow-rootful-development", action="store_true", help="Explicit development mode, recorded as rootful; never the default")
    args = parser.parse_args()
    try:
        evidence = demonstrate(args)
        print(json.dumps({key: evidence[key] for key in ("execution_mode", "status", "run_id", "event_count", "elapsed_seconds")}, indent=2))
        print(f"Evidence: {args.output}")
    except (ValueError, RuntimeError, TimeoutError, subprocess.SubprocessError, httpx.HTTPError) as error:
        print(f"Demo failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
