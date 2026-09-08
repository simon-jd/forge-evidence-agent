"""HTTP control contracts on SQLite; PostgreSQL lease guarantees are tested separately."""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest_asyncio
import yaml
from pydantic import SecretStr

from forge.api import create_app
from forge.api.app import event_frame
from forge.domain.types import RepoProfile, canonical_hash
from forge.persistence.models import ApprovalRow, ArtifactRow, EvaluationRow, ToolRow
from forge.persistence.repository import Repository
from forge.settings import Settings


def write_config(root: Path, group: str, name: str, value: dict[str, Any]) -> None:
    path = root / group / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value))


@pytest_asyncio.fixture
async def api_env(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, Any, Path, Repository]]:
    source = tmp_path / "repositories" / "sample"
    source.mkdir(parents=True)
    (source / "module.py").write_text("VALUE = 1\n")
    for argv in (["git", "init", "-q"], ["git", "add", "module.py"],
                 ["git", "-c", "user.name=Contract Test", "-c", "user.email=contract@example.invalid", "commit", "-qm", "fixture"]):
        await asyncio.to_thread(subprocess.run, argv, cwd=source, check=True, capture_output=True)
    configs = tmp_path / "configs"
    profile = RepoProfile(name="test", image="synthetic-image:fixture", validation_argv=["python", "-m", "pytest"])
    write_config(configs, "repo_profiles", "test", profile.model_dump(mode="json"))
    write_config(configs, "runs", "offline", {"execution_mode": "offline_scripted", "run": {},
        "scripted_turns": [{"provider": "fake", "requested_model": "scripted", "visible_text": "offline",
                            "finish_reason": "stop"}]})
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'contracts.sqlite'}",
                        api_token=SecretStr("contract-token"), owner_id="alice", data_dir=tmp_path / "data",
                        allowed_repo_roots=[source.parent], config_dir=configs)
    repo = Repository(settings.database_url)
    await repo.create_schema_for_tests()
    app = create_app(settings, repo)
    app.state.service.freeze_image = lambda image: "sha256:" + "a" * 64
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "system.txt").write_text("Contract fixture prompt")
    app.state.service.prompt_root = prompts
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://contract.invalid",
                                 headers={"Authorization": "Bearer contract-token"}) as client:
        yield client, app, source, repo
    await repo.close()


async def make_run(client: httpx.AsyncClient, source: Path) -> dict[str, Any]:
    project = await client.post("/v1/projects", json={"name": "sample", "source": str(source), "profile_id": "test"})
    assert project.status_code == 201, project.text
    response = await client.post("/v1/runs", json={"project_id": project.json()["id"], "task": "Fix VALUE", "config_id": "offline"},
                                 headers={"Idempotency-Key": "run-key"})
    assert response.status_code == 202, response.text
    return response.json()


async def test_run_freezes_commit_image_configuration_and_hides_native_state(api_env: Any) -> None:
    client, app, source, repo = api_env
    run = await make_run(client, source)
    assert len(run["base_commit"]) == 40
    assert run["manifest"]["image_id"] == "sha256:" + "a" * 64
    assert run["manifest"]["execution_mode"] == "offline_scripted"
    assert run["manifest"]["tool_schema_hash"]
    stored = await repo.get_run(run["id"], "alice")
    assert stored["manifest"]["scripted_turns"]
    async with repo.locked(run["id"], owner="alice") as (_, row):
        row.state = {"native_state": {"encrypted_content": "private-opaque"}, "raw_response": "secret",
                     "budget": {"cost_usd": "0.2"}, "iteration": 2}
    public = (await client.get(f"/v1/runs/{run['id']}")).json()
    assert public["budget"]["cost_usd"] == "0.2"
    assert public["iteration"] == 2
    assert "private-opaque" not in json.dumps(public)
    assert "scripted_turns" not in public["manifest"]


async def test_run_idempotency_survives_mutable_config_and_rejects_changed_body(api_env: Any) -> None:
    client, app, source, repo = api_env
    run = await make_run(client, source)
    body = {"project_id": run["project_id"], "task": "Fix VALUE", "config_id": "offline"}
    (app.state.service.settings.config_dir / "runs/offline.yaml").write_text("broken: config")
    app.state.service.freeze_image = lambda image: (_ for _ in ()).throw(AssertionError("Idempotent retry must not inspect new image"))
    again = await client.post("/v1/runs", json=body, headers={"Idempotency-Key": "run-key"})
    assert again.status_code == 202 and again.json()["id"] == run["id"]
    conflict = await client.post("/v1/runs", json={**body, "task": "Different"}, headers={"Idempotency-Key": "run-key"})
    assert conflict.status_code == 409
    missing_key = await client.post("/v1/runs", json=body)
    assert missing_key.status_code == 422


async def test_auth_owner_allowlist_and_configuration_traversal(api_env: Any) -> None:
    client, app, source, repo = api_env
    run = await make_run(client, source)
    unauthorized = await client.get(f"/v1/runs/{run['id']}", headers={"Authorization": "Bearer wrong"})
    assert unauthorized.status_code == 401
    project = await repo.create_project("bob", "foreign", str(source), {})
    foreign = await client.post("/v1/runs", json={"project_id": project["id"], "task": "x", "config_id": "offline"},
                                headers={"Idempotency-Key": "foreign"})
    assert foreign.status_code == 404
    outside = await client.post("/v1/projects", json={"name": "outside", "source": str(source.parent.parent), "profile_id": "test"})
    assert outside.status_code == 422
    traversal = await client.post("/v1/projects", json={"name": "x", "source": str(source), "profile_id": "../outside"})
    assert traversal.status_code == 422
    target = source.parent.parent / "external.yaml"
    target.write_text("name: leaked")
    (app.state.service.settings.config_dir / "repo_profiles/linked.yaml").symlink_to(target)
    linked = await client.post("/v1/projects", json={"name": "x", "source": str(source), "profile_id": "linked"})
    assert linked.status_code == 422


async def test_message_idempotency_cancel_and_terminal_resume(api_env: Any) -> None:
    client, app, source, repo = api_env
    run = await make_run(client, source)
    path = f"/v1/runs/{run['id']}"
    first = await client.post(path + "/messages", json={"text": "Preserve public API"}, headers={"Idempotency-Key": "message"})
    again = await client.post(path + "/messages", json={"text": "Preserve public API"}, headers={"Idempotency-Key": "message"})
    assert first.status_code == 202 and first.json()["id"] == again.json()["id"]
    changed = await client.post(path + "/messages", json={"text": "changed"}, headers={"Idempotency-Key": "message"})
    assert changed.status_code == 409
    cancelled = await client.post(path + "/cancel")
    assert cancelled.json()["cancel_requested"] is True
    assert cancelled.json()["status"] == "queued"
    async with repo.locked(run["id"], owner="alice") as (_, row):
        row.status = "completed"
    current = await repo.get_run(run["id"])
    rejected = await client.post(path + "/resume", json={"version": current["version"]})
    assert rejected.status_code == 409
    terminal_message = await client.post(path + "/messages", json={"text": "too late"}, headers={"Idempotency-Key": "new"})
    assert terminal_message.status_code == 409


async def test_sse_cursor_replays_persisted_events_without_native_payload(api_env: Any) -> None:
    client, app, source, repo = api_env
    run = await make_run(client, source)
    async with repo.locked(run["id"], owner="alice") as (session, row):
        await repo.event(session, row, "text.delta", {"text": "visible", "native_state": "opaque-private"})
        await repo.event(session, row, "run.finished", {"status": "completed"})
        row.status = "completed"
    response = await client.get(f"/v1/runs/{run['id']}/events", headers={"Last-Event-ID": "1"})
    assert response.status_code == 200
    assert "id: 1\n" not in response.text
    assert "id: 2\n" in response.text and "id: 3\n" in response.text
    assert "opaque-private" not in response.text
    bad = await client.get(f"/v1/runs/{run['id']}/events", headers={"Last-Event-ID": "99"})
    assert bad.status_code == 409
    malformed = await client.get(f"/v1/runs/{run['id']}/events", headers={"Last-Event-ID": "bad"})
    assert malformed.status_code == 422


def test_oversized_event_frame_has_an_explicit_bounded_projection() -> None:
    encoded = event_frame({"seq": 4, "kind": "text.delta", "payload": {"text": "界" * 100_000}, "created_at": 1})
    assert len(encoded) < 65_536
    assert b'"omitted":true' in encoded


async def test_artifact_owner_raw_data_boundary_and_integrity(api_env: Any) -> None:
    client, app, source, repo = api_env
    run = await make_run(client, source)
    store = app.state.artifacts
    patch = store.put_text(run["id"], "patch", "a patch")
    raw = store.put_text(run["id"], "model_response", "opaque-private")
    foreign = store.put_text(run["id"], "patch", "bob-private")
    async with repo.sessions() as session, session.begin():
        for ref, owner, kind in [(patch, "alice", "patch"), (raw, "alice", "model_response"), (foreign, "bob", "patch")]:
            session.add(ArtifactRow(id=ref, owner_id=owner, run_id=run["id"], kind=kind, details=store.metadata(ref)))
    listed = (await client.get(f"/v1/runs/{run['id']}/artifacts")).json()
    assert [row["id"] for row in listed] == [patch]
    downloaded = await client.get(f"/v1/artifacts/{patch}")
    assert downloaded.status_code == 200 and downloaded.text == "a patch"
    assert "attachment" in downloaded.headers["content-disposition"]
    assert (await client.get(f"/v1/artifacts/{raw}")).status_code == 404
    assert (await client.get(f"/v1/artifacts/{foreign}")).status_code == 404
    store._path(patch, "data").write_text("tampered")
    assert (await client.get(f"/v1/artifacts/{patch}")).status_code == 409


async def test_approval_displays_exact_call_and_requires_current_version(api_env: Any) -> None:
    client, app, source, repo = api_env
    run = await make_run(client, source)
    call = {"call_id": "call-1", "name": "apply_patch", "arguments": {"patch": "reviewed diff"}}
    async with repo.locked(run["id"], owner="alice") as (session, row):
        session.add(ToolRow(id="tool-test", run_id=row.id, iteration=1, ordinal=0, call_id="call-1", call=call,
                            args_hash=canonical_hash(call), workspace_revision=0, epoch=1, status="planned", created_at=1))
        await session.flush()
        session.add(ApprovalRow(id="approval-test", run_id=row.id, tool_id="tool-test", args_hash=canonical_hash(call),
                                workspace_revision=0, policy_hash="p", version=1, created_at=1))
        row.state = {"approval_id": "approval-test"}
        row.status = "waiting_approval"
    pending = (await client.get(f"/v1/runs/{run['id']}")).json()["pending_approvals"]
    assert pending[0]["call"] == call
    stale = await client.post("/v1/approvals/approval-test/decision", json={"decision": "allow", "version": 2})
    assert stale.status_code == 409
    allowed = await client.post("/v1/approvals/approval-test/decision", json={"decision": "allow", "version": 1})
    assert allowed.status_code == 200
    again = await client.post("/v1/approvals/approval-test/decision", json={"decision": "allow", "version": 1})
    assert again.status_code == 200
    assert (await repo.get_run(run["id"]))["status"] == "queued"


async def test_evaluation_api_only_enqueues_server_owned_configuration(api_env: Any) -> None:
    client, app, source, repo = api_env
    settings = app.state.service.settings
    body = {"experiment_id": "protocol-contract", "task_ids": ["task-1"], "conditions": ["B0"],
            "config_id": "diagnostic", "budget_usd": "0.10"}
    disabled = await client.post("/v1/evaluations", json=body)
    assert disabled.status_code == 503
    settings.enable_evaluations = True
    settings.eval_private_dir = source.parent.parent / "private"
    write_config(settings.config_dir, "runs", "live-test", {"execution_mode": "live", "run": {"models": [
        {"provider": "openai", "model_id": "contract-model", "price_version": "synthetic-unit-test"}]}})
    write_config(settings.config_dir, "experiments", "diagnostic", {"base_run_config": "live-test", "profile_id": "test", "dataset_id": "diagnostic-test"})
    response = await client.post("/v1/evaluations", json=body)
    assert response.status_code == 202, response.text
    result = (await client.get(f"/v1/evaluations/{response.json()['id']}")).json()
    assert result["status"] == "queued" and result["results"] == {}
    assert result["config"]["profile"]["image"] == "sha256:" + "a" * 64
    async with repo.sessions() as session, session.begin():
        session.add(EvaluationRow(id="foreign-eval", owner_id="bob", status="queued", config={}, results={}, created_at=1))
    assert (await client.get("/v1/evaluations/foreign-eval")).status_code == 404
    injected = await client.post("/v1/evaluations", json={**body, "private_root": "/tmp/secret"})
    assert injected.status_code == 422


async def test_reconciliation_resume_requeues_only_current_state_version(api_env: Any) -> None:
    client, app, source, repo = api_env
    run = await make_run(client, source)
    async with repo.locked(run["id"], owner="alice") as (_, row):
        row.status = "needs_reconciliation"
    current = await repo.get_run(run["id"])
    stale = await client.post(f"/v1/runs/{run['id']}/resume", json={"version": current["version"] - 1})
    assert stale.status_code == 409
    resumed = await client.post(f"/v1/runs/{run['id']}/resume", json={"version": current["version"]})
    assert resumed.status_code == 202 and resumed.json()["status"] == "queued"
