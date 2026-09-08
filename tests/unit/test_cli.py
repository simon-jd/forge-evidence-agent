from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from forge.cli import main
from forge.cli.client import ForgeClient


@pytest.mark.parametrize("args,method,path,expected", [
    (["project", "/allowed/repo", "--name", "demo", "--profile", "python-tests"], "POST", "/v1/projects", {"source": "/allowed/repo"}),
    (["run", "project-1", "Fix regression", "--config", "small", "--key", "fixed-key"], "POST", "/v1/runs", {"task": "Fix regression", "config_id": "small"}),
    (["status", "run-1"], "GET", "/v1/runs/run-1", {}),
    (["message", "run-1", "Keep API", "--key", "message-key"], "POST", "/v1/runs/run-1/messages", {"text": "Keep API"}),
    (["approve", "approval-1", "--version", "2"], "POST", "/v1/approvals/approval-1/decision", {"decision": "allow", "version": 2}),
    (["approve", "approval-1", "--version", "2", "--deny"], "POST", "/v1/approvals/approval-1/decision", {"decision": "deny"}),
    (["cancel", "run-1"], "POST", "/v1/runs/run-1/cancel", {}),
    (["resume", "run-1", "--version", "3"], "POST", "/v1/runs/run-1/resume", {"version": 3}),
    (["eval", "submit", "experiment", "--experiment", "paired", "--tasks", "one,two", "--budget", "1.25", "--repeats", "2"], "POST", "/v1/evaluations", {"task_ids": ["one", "two"], "budget_usd": "1.25", "repeats": 2}),
    (["eval", "status", "eval-1"], "GET", "/v1/evaluations/eval-1", {}),
])
def test_commands_use_public_api(monkeypatch: pytest.MonkeyPatch, args: list[str], method: str,
                                 path: str, expected: dict[str, Any]) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == method and request.url.path == path
        assert request.headers["Authorization"] == "Bearer test-token"
        if expected:
            body = json.loads(request.content)
            assert all(body[key] == value for key, value in expected.items())
        return httpx.Response(200, json={"id": "result", "status": "queued"})

    monkeypatch.setattr(main, "ForgeClient", lambda: ForgeClient(token="test-token", transport=httpx.MockTransport(handler)))
    result = CliRunner().invoke(main.app, args)
    assert result.exit_code == 0, (result.output, result.exception)
    assert len(requests) == 1
    if args[0] == "run":
        assert requests[0].headers["Idempotency-Key"] == "fixed-key"
        assert '"idempotency_key": "fixed-key"' in result.output


def test_missing_token_and_api_failures_have_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORGE_API_TOKEN", raising=False)
    missing = CliRunner().invoke(main.app, ["status", "run"])
    assert missing.exit_code == 1 and "FORGE_API_TOKEN" in missing.output
    monkeypatch.setattr(main, "ForgeClient", lambda: ForgeClient(token="test-token", transport=httpx.MockTransport(
        lambda req: httpx.Response(409, json={"detail": "Approval version changed"}))))
    conflict = CliRunner().invoke(main.app, ["approve", "approval", "--version", "2"])
    assert conflict.exit_code == 1 and "Approval version changed" in conflict.output


def test_diff_downloads_latest_owned_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/artifacts"):
            return httpx.Response(200, json=[{"id": "old", "kind": "patch", "details": {"created_at": "2026-01-01"}},
                                            {"id": "new", "kind": "patch", "details": {"created_at": "2026-02-01"}}])
        assert request.url.path == "/v1/artifacts/new"
        return httpx.Response(200, text="diff --git a/a.py b/a.py\n")

    monkeypatch.setattr(main, "ForgeClient", lambda: ForgeClient(token="test-token", transport=httpx.MockTransport(handler)))
    result = CliRunner().invoke(main.app, ["diff", "run-1"])
    assert result.exit_code == 0 and result.output.startswith("diff --git")


class TornSSE(httpx.SyncByteStream):
    def __iter__(self) -> Iterator[bytes]:
        yield b'id: 1\ndata: {"seq":1,"type":"stage.changed","payload":{}}\n\n'
        raise httpx.ReadError("synthetic disconnect")


def test_watch_reconnects_with_cursor_and_deduplicates(monkeypatch: pytest.MonkeyPatch) -> None:
    stream_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            stream_calls.append(request)
            if len(stream_calls) == 1:
                return httpx.Response(200, stream=TornSSE())
            assert request.headers["Last-Event-ID"] == "1"
            return httpx.Response(200, text='id: 1\ndata: {"seq":1}\n\nid: 2\ndata: {"seq":2,"type":"run.finished"}\n\n')
        return httpx.Response(200, json={"status": "completed"})

    monkeypatch.setattr("forge.cli.client.time.sleep", lambda delay: None)
    client = ForgeClient(token="test-token", transport=httpx.MockTransport(handler))
    try:
        assert [event["seq"] for event in client.watch("run")] == [1, 2]
    finally:
        client.close()
    assert len(stream_calls) == 2


def test_invalid_eval_budget_is_rejected_before_http() -> None:
    result = CliRunner().invoke(main.app, ["eval", "submit", "config", "--experiment", "e", "--tasks", "task", "--budget", "NaN"])
    assert result.exit_code == 2
