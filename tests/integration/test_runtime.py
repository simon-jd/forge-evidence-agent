"""Actual PostgreSQL and Docker, with explicitly offline scripted model turns.

These prove execution/recovery semantics, not model repair quality. The dedicated
database must have migrations applied. No test changes global Docker settings.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from openai import AsyncOpenAI
from sqlalchemy import select

from forge.artifacts import LocalArtifactStore
from forge.context.budget import BudgetManager
from forge.domain.types import (
    ModelRequest,
    ModelTurn,
    ProviderError,
    RepoProfile,
    RunConfig,
    TokenUsage,
    ToolCall,
    ToolCallReady,
    canonical_hash,
)
from forge.execution.docker import DockerExecutor
from forge.execution.workspace import WorkspaceManager
from forge.persistence import Conflict, LeaseLost, Repository
from forge.persistence.models import MessageRow, ModelAttemptRow, ModelTurnRow, ToolRow
from forge.providers import OpenAIProvider
from forge.runtime.engine import Runtime
from forge.settings import Settings
from forge.worker import Worker

pytestmark = [pytest.mark.postgres, pytest.mark.docker]
DATABASE_URL = os.environ.get("FORGE_TEST_DATABASE_URL")


@pytest.fixture
async def repository() -> Any:
    if not DATABASE_URL:
        pytest.skip("Set FORGE_TEST_DATABASE_URL to a migrated, dedicated PostgreSQL database")
    repo = Repository(DATABASE_URL)
    yield repo
    await repo.close()


def create_source(root: Path, *, broken: bool = True) -> tuple[Path, str]:
    root.mkdir()
    (root / "maths.py").write_text("def add(a, b):\n    return a - b\n" if broken else "def add(a, b):\n    return a + b\n")
    (root / "test_public.py").write_text("import unittest\nfrom maths import add\n\nclass Acceptance(unittest.TestCase):\n    def test_positive(self):\n        self.assertEqual(add(2, 3), 5)\n    def test_negative(self):\n        self.assertEqual(add(-2, -3), -5)\n")
    for argv in (["git", "init", "-q"], ["git", "add", "."],
                 ["git", "-c", "user.name=Forge Test", "-c", "user.email=forge-test@example.invalid", "commit", "-qm", "Fixed test fixture"]):
        subprocess.run(argv, cwd=root, check=True, capture_output=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    return root, commit


def turn(*calls: ToolCall, text: str = "") -> dict[str, Any]:
    return ModelTurn(provider="fake", requested_model="scripted", visible_text=text,
        complete_tool_calls=list(calls), usage=TokenUsage(input_tokens=100, output_tokens=50),
        finish_reason="tool_use" if calls else "stop").model_dump(mode="json")


def fix_turns(source: Path) -> list[dict[str, Any]]:
    sha = hashlib.sha256((source / "maths.py").read_bytes()).hexdigest()
    patch = "--- a/maths.py\n+++ b/maths.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n"
    return [turn(ToolCall(call_id="read1", name="read_file", arguments={"relative_path": "maths.py"})),
            turn(ToolCall(call_id="patch1", name="apply_patch", arguments={"patch": patch, "expected_file_hashes": {"maths.py": sha}})),
            turn(ToolCall(call_id="finish1", name="finish", arguments={"summary": "Correct arithmetic operator"}))]


async def new_run(repository: Repository, root: Path, *, approval: bool = False,
                  targets: bool = True, scripts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    source, commit = create_source(root / "source")
    profile = RepoProfile(name="arithmetic", image=os.environ.get("FORGE_TEST_IMAGE", "forge-runner:dev"),
        validation_argv=["python", "-m", "unittest", "discover"],
        target_test_argv=["python", "-m", "unittest", "test_public.Acceptance"] if targets else [],
        allowed_patch_paths=["maths.py"], protected_paths=["test_public.py"], command_timeout_seconds=15)
    config = RunConfig(require_write_approval=approval)
    project = await repository.create_project("test-owner", "runtime-test", str(source), profile.model_dump(mode="json"))
    return await repository.create_run(owner="test-owner", project_id=project["id"], idempotency_key=uuid.uuid4().hex,
        request_hash=canonical_hash(project["id"]), task="Fix addition for positive and negative integers",
        base_commit=commit, config=config.model_dump(mode="json"), profile=profile.model_dump(mode="json"),
        manifest={"validation_mode": "offline_scripted", "scripted_turns": scripts if scripts is not None else fix_turns(source)})


async def claim_specific(repository: Repository, run_id: str) -> dict[str, Any]:
    # Claim uses the production transaction. Existing unrelated queued tests
    # would be an invalid shared database configuration, not silently drained.
    claimed = await repository.claim("test-worker", 120)
    assert claimed is not None and claimed["id"] == run_id
    return claimed


def runtime(repository: Repository, root: Path, epoch: int, **kwargs: Any) -> Runtime:
    return Runtime(repository, WorkspaceManager(root / "workspaces"),
        DockerExecutor(docker_host=os.environ.get("FORGE_TEST_DOCKER_HOST"),
                       allow_rootful=os.environ.get("FORGE_ALLOW_ROOTFUL_TESTS") == "1"),
        LocalArtifactStore(root / "artifacts"), epoch=epoch, **kwargs)


@pytest.mark.asyncio
async def test_full_graph_has_independent_verification_and_durable_evidence(repository: Repository, tmp_path: Path) -> None:
    run = await new_run(repository, tmp_path)
    claimed = await claim_specific(repository, run["id"])
    agent = runtime(repository, tmp_path, claimed["lease_epoch"])
    assert DATABASE_URL
    async with AsyncPostgresSaver.from_conn_string(DATABASE_URL.replace("postgresql+psycopg://", "postgresql://")) as saver:
        await saver.setup()
        graph = agent.graph(saver)
        config = {"configurable": {"thread_id": "test:" + run["id"]}, "recursion_limit": 128}
        await graph.ainvoke({"run_id": run["id"], "next": "bootstrap"}, config)
        checkpoint = await graph.aget_state(config)
        assert not checkpoint.next
    final = await repository.get_run(run["id"])
    assert final["status"] == "completed", final["state"].get("stop_reason")
    assert final["verification"]["status"] == "verified"
    assert final["verification"]["baseline_exit_code"] != 0
    assert "a - b" in (Path(run["source"]) / "maths.py").read_text()
    assert "a + b" in agent.workspaces.load(run["id"]).get_diff()
    events = await repository.list_events(run["id"], "test-owner", 0)
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert any(event["kind"] == "context.built" for event in events)
    async with repository.sessions() as session:
        turns = list(await session.scalars(select(ModelTurnRow).where(ModelTurnRow.run_id == run["id"])))
        attempts = list(await session.scalars(select(ModelAttemptRow).where(ModelAttemptRow.run_id == run["id"])))
        assert len(turns) == len(attempts) == 3
        assert all(item.status == "completed" for item in turns)


async def run_stages(agent: Runtime, repository: Repository, run_id: str, until: str | None = None) -> None:
    for _ in range(100):
        run = await repository.get_run(run_id)
        if run["status"] != "running" or run["stage"] == until:
            return
        await agent.step(run_id, run["stage"])
    raise AssertionError("Runtime did not converge")


@pytest.mark.asyncio
async def test_recorded_model_response_is_not_resampled_after_crash(repository: Repository, tmp_path: Path) -> None:
    run = await new_run(repository, tmp_path)
    claimed = await claim_specific(repository, run["id"])
    class InjectedCrash(Exception):
        pass
    async def crash(point: str, run_id: str) -> None:
        if point == "model_recorded_before_checkpoint":
            raise InjectedCrash
    agent = runtime(repository, tmp_path, claimed["lease_epoch"], fault_hook=crash)
    await run_stages(agent, repository, run["id"], until="model")
    with pytest.raises(InjectedCrash):
        await agent.step(run["id"], "model")
    recovered = runtime(repository, tmp_path, claimed["lease_epoch"])
    await recovered.step(run["id"], "model")
    async with repository.sessions() as session:
        attempts = list(await session.scalars(select(ModelAttemptRow).where(ModelAttemptRow.run_id == run["id"])))
        assert len(attempts) == 1
    await run_stages(recovered, repository, run["id"])
    assert (await repository.get_run(run["id"]))["status"] == "completed"


@pytest.mark.asyncio
async def test_patch_effect_before_database_recovers_receipt_without_reapply(repository: Repository, tmp_path: Path) -> None:
    run = await new_run(repository, tmp_path)
    claimed = await claim_specific(repository, run["id"])
    agent = runtime(repository, tmp_path, claimed["lease_epoch"])
    while True:
        current = await repository.get_run(run["id"])
        if current["stage"] == "execute" and current["state"]["iteration"] == 1:
            break
        await agent.step(run["id"], current["stage"])
    async def crash(point: str, run_id: str) -> None:
        if point == "tool_effect_before_database":
            raise RuntimeError("injected crash after persisted patch receipt")
    agent.fault_hook = crash
    with pytest.raises(RuntimeError, match="injected"):
        await agent.step(run["id"], "execute")
    workspace = agent.workspaces.load(run["id"])
    assert workspace.revision == 1
    async with repository.sessions() as session:
        tool = await session.get(ToolRow, f"{run['id']}-t1-c0")
        assert tool is not None and tool.result is None
    recovered = runtime(repository, tmp_path, claimed["lease_epoch"])
    await run_stages(recovered, repository, run["id"])
    assert workspace.revision == 1
    assert (await repository.get_run(run["id"]))["status"] == "completed"


@pytest.mark.asyncio
async def test_approval_survives_worker_and_checkpoint_restart(repository: Repository, tmp_path: Path) -> None:
    run = await new_run(repository, tmp_path, approval=True)
    claimed = await claim_specific(repository, run["id"])
    assert DATABASE_URL
    dsn = DATABASE_URL.replace("postgresql+psycopg://", "postgresql://")
    graph_config = {"configurable": {"thread_id": "approval:" + run["id"]}, "recursion_limit": 128}
    async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
        await saver.setup()
        await runtime(repository, tmp_path, claimed["lease_epoch"]).graph(saver).ainvoke(
            {"run_id": run["id"], "next": "bootstrap"}, graph_config)
    paused = await repository.get_run(run["id"])
    assert paused["status"] == "waiting_approval"
    approval = paused["approvals"][0]
    await repository.decide(approval["id"], "test-owner", "allow", approval["version"])
    claimed = await claim_specific(repository, run["id"])
    async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
        await runtime(repository, tmp_path, claimed["lease_epoch"]).graph(saver).ainvoke(Command(resume=True), graph_config)
    assert (await repository.get_run(run["id"]))["status"] == "completed"


@pytest.mark.asyncio
async def test_regression_only_is_never_completed(repository: Repository, tmp_path: Path) -> None:
    run = await new_run(repository, tmp_path, targets=False)
    claimed = await claim_specific(repository, run["id"])
    await run_stages(runtime(repository, tmp_path, claimed["lease_epoch"]), repository, run["id"])
    result = await repository.get_run(run["id"])
    assert result["status"] == "failed"
    assert result["verification"]["status"] == "regression_only"


@pytest.mark.asyncio
async def test_pg_skip_locked_fencing_messages_and_owner_idempotency(repository: Repository, tmp_path: Path) -> None:
    run = await new_run(repository, tmp_path)
    claims = await asyncio.gather(repository.claim("one", 10), repository.claim("two", 10))
    selected = [item for item in claims if item is not None]
    assert len(selected) == 1 and selected[0]["id"] == run["id"]
    epoch = selected[0]["lease_epoch"]
    first = await repository.add_message(run["id"], "test-owner", "same", "Keep the API unchanged")
    second = await repository.add_message(run["id"], "test-owner", "same", "Keep the API unchanged")
    assert first["id"] == second["id"]
    with pytest.raises(Conflict):
        await repository.add_message(run["id"], "test-owner", "same", "different")
    async with repository.locked(run["id"], epoch=epoch) as (_, row):
        row.lease_expires_at = 0
    claimed = await claim_specific(repository, run["id"])
    assert claimed["lease_epoch"] == epoch + 1
    with pytest.raises(LeaseLost):
        await repository.heartbeat(run["id"], epoch, 30)
    agent = runtime(repository, tmp_path, claimed["lease_epoch"])
    await run_stages(agent, repository, run["id"])
    async with repository.sessions() as session:
        message = await session.get(MessageRow, first["id"])
        assert message is not None and message.applied_iteration == 0
    result = await repository.get_run(run["id"])
    assert result["state"]["constraints"] == ["Keep the API unchanged"]


@pytest.mark.asyncio
async def test_budget_refuses_a_second_model_request(repository: Repository, tmp_path: Path) -> None:
    run = await new_run(repository, tmp_path)
    async with repository.locked(run["id"]) as (_, row):
        config = RunConfig.model_validate(row.config)
        config.budget.max_model_turns = 1
        row.config = config.model_dump(mode="json")
    claimed = await claim_specific(repository, run["id"])
    await run_stages(runtime(repository, tmp_path, claimed["lease_epoch"]), repository, run["id"])
    final = await repository.get_run(run["id"])
    assert final["status"] == "budget_exhausted"
    async with repository.sessions() as session:
        attempts = list(await session.scalars(select(ModelAttemptRow).where(ModelAttemptRow.run_id == run["id"])))
        assert len(attempts) == 1


@pytest.mark.asyncio
async def test_unconfirmed_stream_cannot_execute_a_ready_tool_and_cost_is_unknown(repository: Repository, tmp_path: Path) -> None:
    run = await new_run(repository, tmp_path)
    async with repository.locked(run["id"]) as (_, row):
        config = RunConfig.model_validate(row.config)
        config.models[0].input_usd_per_million = Decimal("1")
        config.models[0].output_usd_per_million = Decimal("2")
        row.config = config.model_dump(mode="json")
    claimed = await claim_specific(repository, run["id"])
    class IncompleteProvider:
        def __init__(self, sink: Any) -> None:
            self.sink = sink
        async def stream(self, request: ModelRequest) -> Any:
            attempt = {"attempt_id": uuid.uuid4().hex, "status": "started"}
            await self.sink(attempt)
            yield ToolCallReady(call=ToolCall(call_id="unconfirmed", name="run_command",
                arguments={"argv": ["python", "-c", "open('marker','w').write('must not execute')"]}))
            await self.sink({**attempt, "status": "failed", "usage": TokenUsage().model_dump()})
            yield ProviderError(code="stream_interrupted", message="Connection lost before complete response")
    agent = runtime(repository, tmp_path, claimed["lease_epoch"],
                    provider_factory=lambda spec, sink, iteration: IncompleteProvider(sink))
    await run_stages(agent, repository, run["id"])
    final = await repository.get_run(run["id"])
    assert final["status"] == "failed"
    reservations = list(final["state"]["budget"]["reservations"].values())
    assert len(reservations) == 1 and reservations[0]["status"] == "unknown"
    assert Decimal(reservations[0]["charged_usd"]) > 0
    assert not (agent.workspaces.load(run["id"]).path / "marker").exists()
    async with repository.sessions() as session:
        assert not list(await session.scalars(select(ToolRow).where(ToolRow.run_id == run["id"])))


@pytest.mark.asyncio
async def test_cancel_running_command_reconciles_receipt_before_terminal(repository: Repository, tmp_path: Path) -> None:
    scripts = [turn(ToolCall(call_id="slow", name="run_command", arguments={
        "argv": ["python", "-c", "import time; print('started',flush=True); time.sleep(12)"], "timeout_seconds": 15}))]
    run = await new_run(repository, tmp_path, scripts=scripts)
    assert DATABASE_URL
    settings = Settings(database_url=DATABASE_URL, data_dir=tmp_path, lease_seconds=12,
                        docker_host=os.environ.get("FORGE_TEST_DOCKER_HOST"),
                        allow_rootful_development=os.environ.get("FORGE_ALLOW_ROOTFUL_TESTS") == "1")
    async with AsyncPostgresSaver.from_conn_string(settings.checkpoint_dsn) as saver:
        await saver.setup()
        worker = Worker(settings, repository, saver)
        running = asyncio.create_task(worker.once())
        operation_id = f"{run['id']}-t0-c0"
        try:
            async with asyncio.timeout(20):
                while True:
                    try:
                        workspace = worker.workspaces.load(run["id"])
                        operation = workspace.operation(operation_id)
                        if operation and operation["state"] == "running":
                            break
                    except FileNotFoundError:
                        pass
                    await asyncio.sleep(0.05)
            await repository.cancel(run["id"], "test-owner")
            await asyncio.wait_for(running, timeout=10)
        finally:
            if not running.done():
                running.cancel()
            worker.telemetry.close()
    final = await repository.get_run(run["id"])
    assert final["status"] == "cancelled", final["state"].get("stop_reason")
    assert workspace.active_operation() is None
    async with repository.sessions() as session:
        tool = await session.get(ToolRow, operation_id)
        assert tool is not None and tool.result is not None
        assert tool.result["status"] == "cancelled"
    assert (tmp_path / "traces.jsonl").is_file()


@pytest.mark.asyncio
async def test_message_arriving_after_validation_prevents_false_completion(repository: Repository, tmp_path: Path) -> None:
    run = await new_run(repository, tmp_path)
    claimed = await claim_specific(repository, run["id"])
    agent = runtime(repository, tmp_path, claimed["lease_epoch"])
    await run_stages(agent, repository, run["id"], until="finalize")
    await repository.add_message(run["id"], "test-owner", "late", "Also preserve bool input semantics")
    await agent.step(run["id"], "finalize")
    current = await repository.get_run(run["id"])
    assert current["status"] == "running" and current["stage"] == "context"
    await repository.cancel(run["id"], "test-owner")
    await agent.step(run["id"], "context")
    assert (await repository.get_run(run["id"]))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_native_invalid_arguments_receive_bounded_feedback_and_route(repository: Repository, tmp_path: Path) -> None:
    run = await new_run(repository, tmp_path)
    async with repository.locked(run["id"]) as (_, row):
        config = RunConfig.model_validate(row.config)
        weak = config.models[0].model_copy(update={"provider": "openai", "model_id": "fixture-weak",
            "input_usd_per_million": Decimal("1"), "output_usd_per_million": Decimal("1"),
            "price_version": "mock-http-price"})
        strong = weak.model_copy(update={"model_id": "fixture-strong"})
        config.models = [weak, strong]
        row.config = config.model_dump(mode="json")
    claimed = await claim_specific(repository, run["id"])
    requests: list[str] = []
    scripts = fix_turns(Path(run["source"]))
    patch_args = scripts[1]["complete_tool_calls"][0]["arguments"]

    def factory(spec: Any, sink: Any, iteration: int) -> OpenAIProvider:
        def respond(request: Any) -> Any:
            body = json.loads(request.content)
            requests.append(body["model"])
            if iteration < 2:
                name, arguments = "read_file", {"relative_path": 123}
            elif iteration == 2:
                name, arguments = "apply_patch", patch_args
            else:
                name, arguments = "finish", {"summary": "Native SDK fixture proposed candidate"}
            response = {"id": f"response-{iteration}", "object": "response", "created_at": 1,
                "status": "completed", "model": spec.model_id,
                "output": [{"type": "function_call", "id": f"item-{iteration}", "call_id": f"call-{iteration}",
                            "name": name, "arguments": json.dumps(arguments), "status": "completed"}],
                "usage": {"input_tokens": 100, "output_tokens": 50}}
            event = {"type": "response.completed", "sequence_number": 0, "response": response}
            return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                                  content=f"event: response.completed\ndata: {json.dumps(event)}\n\n".encode())
        client = AsyncOpenAI(api_key="offline-native-fixture", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)))
        return OpenAIProvider(client=client, capabilities={spec.model_id: spec.capabilities}, attempt_sink=sink)

    agent = runtime(repository, tmp_path, claimed["lease_epoch"], provider_factory=factory)
    await run_stages(agent, repository, run["id"])
    final = await repository.get_run(run["id"])
    assert final["status"] == "completed", final["state"].get("stop_reason")
    assert requests == ["fixture-weak", "fixture-weak", "fixture-strong", "fixture-strong"]
    assert final["state"]["invalid_tool_calls"] == 2
    assert len(final["state"]["budget"]["reservations"]) == 4
    assert all(item["status"] == "settled" for item in final["state"]["budget"]["reservations"].values())
    events = await repository.list_events(run["id"], "test-owner", 0)
    assert len([event for event in events if event["kind"] == "model.switched"]) == 1
    async with repository.sessions() as session:
        executed = list(await session.scalars(select(ToolRow).where(ToolRow.run_id == run["id"])))
        assert len(executed) == 2 and all(item.iteration >= 2 for item in executed)


@pytest.mark.asyncio
async def test_deepseek_factory_reserves_full_wire_input_before_native_tool_cycle(
    repository: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production factory, PostgreSQL ledger and rootless verification; only HTTP is synthetic."""
    run = await new_run(repository, tmp_path)
    async with repository.locked(run["id"]) as (_, row):
        config = RunConfig.model_validate(row.config)
        spec = config.models[0].model_copy(update={
            "provider": "deepseek", "model_id": "offline-deepseek-contract",
            "input_usd_per_million": Decimal("0.30"),
            "cached_input_usd_per_million": Decimal("0.006"),
            "output_usd_per_million": Decimal("1.20"),
            "price_version": "synthetic-tariff-ceiling", "price_basis": "upper_bound",
        })
        spec.capabilities.parallel_tool_calls = True
        config.models = [spec]
        config.context.continuation_mode = "provider_native"
        config.context.input_budget = 64_000
        config.context.compact_threshold = 0.95
        config.sampling_config = {"reasoning": {"effort": "none"}}
        row.config = config.model_dump(mode="json")
        row.manifest = {"validation_mode": "offline_mock_transport"}
    scripts = fix_turns(Path(run["source"]))
    claimed = await claim_specific(repository, run["id"])
    received: list[dict[str, Any]] = []
    in_flight: list[dict[str, Any]] = []

    async def respond(req: httpx2.Request) -> httpx2.Response:
        iteration = len(received)
        assert req.url.host == "api.deepseek.com" and req.url.path == "/responses"
        assert req.headers["authorization"] == "Bearer offline-deepseek-runtime-placeholder"
        body = json.loads(req.content)
        received.append(body)
        # Observe the committed reservation at the HTTP boundary. Merely making
        # the right final ledger row would not prove that spend was reserved first.
        current = await repository.get_run(run["id"])
        reserved = [item for item in current["state"]["budget"]["reservations"].values()
                    if item["status"] == "reserved"]
        assert len(reserved) == 1
        reservation = reserved[0]
        wire_bytes = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode())
        assert reservation["input_tokens"] >= wire_bytes + 1_024
        assert Decimal(reservation["reserved_usd"]) > 0
        assert reservation["charged_usd"] == reservation["reserved_usd"]
        assert reservation["model"]["provider"] == "deepseek"
        assert reservation["model"]["price_basis"] == "upper_bound"
        in_flight.append({"reservation": reservation,
                          "bundle_estimate": current["state"]["context"]["token_estimate"]})
        call = scripts[iteration]["complete_tool_calls"][0]
        item = {"type": "function_call", "id": f"deepseek-item-{iteration}",
                "call_id": call["call_id"], "name": call["name"],
                "arguments": json.dumps(call["arguments"]), "status": "completed"}
        final = {"id": f"deepseek-response-{iteration}", "object": "response", "created_at": 1,
                 "status": "completed", "model": "offline-deepseek-reported", "output": [item],
                 "usage": {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150,
                           "input_tokens_details": {"cached_tokens": 20},
                           "output_tokens_details": {"reasoning_tokens": 5}}}
        events = [
            {"type": "response.output_item.added", "sequence_number": 0, "output_index": 0,
             "item": {**item, "arguments": ""}},
            {"type": "response.function_call_arguments.delta", "sequence_number": 1,
             "output_index": 0, "item_id": item["id"], "delta": item["arguments"]},
            {"type": "response.function_call_arguments.done", "sequence_number": 2,
             "output_index": 0, "item_id": item["id"], "arguments": item["arguments"]},
            {"type": "response.completed", "sequence_number": 3, "response": final},
        ]
        content = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=content.encode())

    def mock_sdk(**kwargs: Any) -> AsyncOpenAI:
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
        return AsyncOpenAI(http_client=client, **kwargs)

    monkeypatch.setattr("forge.providers.deepseek.AsyncOpenAI", mock_sdk)
    settings = Settings(_env_file=None, DEEPSEEK_API_KEY="offline-deepseek-runtime-placeholder")
    agent = runtime(repository, tmp_path, claimed["lease_epoch"], settings=settings)
    assert agent.provider_factory is None  # Exercise runtime -> production factory -> adapter.
    await run_stages(agent, repository, run["id"])
    final = await repository.get_run(run["id"])
    assert final["status"] == "completed", final["state"].get("stop_reason")
    assert final["verification"]["status"] == "verified"
    assert len(received) == 3
    assert all(body["model"] == spec.model_id and body["reasoning"] == {"effort": "none"}
               for body in received)
    assert all(not {"store", "include", "previous_response_id", "parallel_tool_calls"} & set(body)
               for body in received)
    # The extra framing/schema allowance actually changes the reservation,
    # catching a runtime that defines the adapter bound but never uses it.
    assert in_flight[0]["reservation"]["input_tokens"] > in_flight[0]["bundle_estimate"]
    second_input = received[1]["input"]
    assert second_input[:len(received[0]["input"])] == received[0]["input"]
    assert any(item.get("type") == "function_call" and item.get("call_id") == "read1"
               for item in second_input)
    assert any(item.get("type") == "function_call_output" and item.get("call_id") == "read1"
               for item in second_input)
    manager = BudgetManager(config.budget, final["state"]["budget"])
    assert manager.cost_status == "upper_bound"
    # 100 uncached + 20 cached input; 30 output already includes five reasoning
    # tokens. Fixed synthetic usage makes double counting observable.
    assert manager.known_spend_usd == Decimal("0.00019836")
    assert all(item.status == "settled" for item in manager.state.reservations.values())
    async with repository.sessions() as session:
        turns = list(await session.scalars(select(ModelTurnRow).where(ModelTurnRow.run_id == run["id"]).order_by(ModelTurnRow.iteration)))
        attempts = list(await session.scalars(select(ModelAttemptRow).where(ModelAttemptRow.run_id == run["id"])))
        executed = list(await session.scalars(select(ToolRow).where(ToolRow.run_id == run["id"]).order_by(ToolRow.iteration)))
        assert len(turns) == len(attempts) == len(executed) == 3
        assert all(item.response and item.response["provider"] == "deepseek" for item in turns)
        assert turns[1].request["native_state"]["provider"] == "deepseek"
        assert all(item.payload["provider"] == "deepseek" and item.payload["usage_final"] for item in attempts)
        assert [item.call["name"] for item in executed] == ["read_file", "apply_patch", "finish"]


@pytest.mark.asyncio
@pytest.mark.parametrize("usage_kind", ["complete", "partial", "missing"])
async def test_deepseek_incomplete_generation_accounts_usage_without_executing_tools(
    repository: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, usage_kind: str,
) -> None:
    """Terminal billing and successful tool completion are distinct runtime facts."""
    run = await new_run(repository, tmp_path)
    original = (Path(run["source"]) / "maths.py").read_bytes()
    async with repository.locked(run["id"]) as (_, row):
        config = RunConfig.model_validate(row.config)
        spec = config.models[0].model_copy(update={
            "provider": "deepseek", "model_id": "offline-deepseek-contract",
            "input_usd_per_million": Decimal("0.30"),
            "cached_input_usd_per_million": Decimal("0.006"),
            "output_usd_per_million": Decimal("1.20"),
            "price_version": "synthetic-tariff-ceiling", "price_basis": "upper_bound",
        })
        config.models = [spec]
        config.context.input_budget = 64_000
        config.sampling_config = {"reasoning": {"effort": "none"}}
        row.config = config.model_dump(mode="json")
        row.manifest = {"validation_mode": "offline_mock_transport"}
    usage: dict[str, Any] | None = {
        "input_tokens": 120, "output_tokens": 30, "total_tokens": 150,
        "input_tokens_details": {"cached_tokens": 20},
        "output_tokens_details": {"reasoning_tokens": 5},
    }
    if usage_kind == "partial":
        usage.pop("output_tokens")
    elif usage_kind == "missing":
        usage = None
    call = fix_turns(Path(run["source"]))[1]["complete_tool_calls"][0]
    item = {"type": "function_call", "id": "incomplete-patch", "call_id": call["call_id"],
            "name": call["name"], "arguments": json.dumps(call["arguments"]), "status": "completed"}
    response = {"id": "synthetic-incomplete", "object": "response", "created_at": 1,
                "status": "incomplete", "model": spec.model_id, "output": [item],
                "incomplete_details": {"reason": "max_output_tokens"}, "usage": usage}
    events = [
        {"type": "response.output_item.added", "sequence_number": 0, "output_index": 0,
         "item": {**item, "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "sequence_number": 1,
         "output_index": 0, "item_id": item["id"], "delta": item["arguments"]},
        {"type": "response.function_call_arguments.done", "sequence_number": 2,
         "output_index": 0, "item_id": item["id"], "arguments": item["arguments"]},
        {"type": "response.incomplete", "sequence_number": 3, "response": response},
    ]
    content = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
    requests = 0

    async def respond(req: httpx2.Request) -> httpx2.Response:
        nonlocal requests
        requests += 1
        current = await repository.get_run(run["id"])
        assert len(current["state"]["budget"]["reservations"]) == 1
        assert req.url.host == "api.deepseek.com"
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=content.encode())

    def mock_sdk(**kwargs: Any) -> AsyncOpenAI:
        return AsyncOpenAI(http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)), **kwargs)

    monkeypatch.setattr("forge.providers.deepseek.AsyncOpenAI", mock_sdk)
    claimed = await claim_specific(repository, run["id"])
    agent = runtime(repository, tmp_path, claimed["lease_epoch"],
                    settings=Settings(_env_file=None, DEEPSEEK_API_KEY="offline-incomplete-placeholder"))
    await run_stages(agent, repository, run["id"])
    final = await repository.get_run(run["id"])
    assert final["status"] == "failed"
    assert "response.incomplete" in final["state"]["stop_reason"]
    assert requests == 1
    assert (Path(run["source"]) / "maths.py").read_bytes() == original
    manager = BudgetManager(config.budget, final["state"]["budget"])
    reservation, = manager.state.reservations.values()
    if usage_kind == "complete":
        assert reservation.status == "settled"
        assert manager.cost_status == "upper_bound"
        assert manager.known_spend_usd == Decimal("0.00006612")
    else:
        assert reservation.status in {"unknown", "estimated"}
        assert manager.cost_status == "unknown"
        assert reservation.charged_usd >= reservation.reserved_usd > 0
    async with repository.sessions() as session:
        attempts = list(await session.scalars(select(ModelAttemptRow).where(ModelAttemptRow.run_id == run["id"])))
        turns = list(await session.scalars(select(ModelTurnRow).where(ModelTurnRow.run_id == run["id"])))
        executed = list(await session.scalars(select(ToolRow).where(ToolRow.run_id == run["id"])))
        assert len(attempts) == len(turns) == 1 and not executed
        assert attempts[0].status == turns[0].status == "failed"
        assert attempts[0].payload["usage_final"] is (usage_kind == "complete")
