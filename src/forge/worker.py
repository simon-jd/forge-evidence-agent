from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import time
import uuid
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command

from forge.artifacts import LocalArtifactStore
from forge.context.budget import BudgetExceeded, BudgetManager
from forge.domain.types import RunConfig
from forge.execution.docker import DockerExecutor
from forge.execution.errors import NeedsReconciliation
from forge.execution.workspace import WorkspaceManager
from forge.persistence import LeaseLost, Repository
from forge.runtime.engine import Runtime
from forge.settings import Settings
from forge.telemetry import Telemetry, log_event


class Worker:
    def __init__(self, settings: Settings, repository: Repository, checkpointer: Any,
                 *, runtime_factory: Any = Runtime) -> None:
        self.settings, self.repository, self.checkpointer = settings, repository, checkpointer
        self.runtime_factory = runtime_factory
        self.worker_id = "worker-" + uuid.uuid4().hex[:16]
        self.workspaces = WorkspaceManager(settings.data_dir / "workspaces")
        self.artifacts = LocalArtifactStore(settings.data_dir / "artifacts")
        self.executor = DockerExecutor(settings.docker_host, settings.allow_rootful_development)
        self.telemetry = Telemetry(settings.data_dir / "traces.jsonl", settings.otlp_endpoint)

    async def once(self) -> bool:
        run = await self.repository.claim(self.worker_id, self.settings.lease_seconds)
        if run is None:
            return False
        runtime = self.runtime_factory(self.repository, self.workspaces, self.executor, self.artifacts,
                                       epoch=run["lease_epoch"], telemetry=self.telemetry, settings=self.settings)
        graph = runtime.graph(self.checkpointer)
        graph_config = {"configurable": {"thread_id": "forge:" + run["id"]}, "recursion_limit": 2048}
        snapshot = await graph.aget_state(graph_config)
        if snapshot.next:
            has_interrupt = any(task.interrupts for task in snapshot.tasks)
            graph_input: Any = Command(resume=True) if has_interrupt else None
        else:
            graph_input = {"run_id": run["id"], "next": run["stage"]}
        with self.telemetry.span("run", run_id=run["id"], epoch=run["lease_epoch"]):
            task = asyncio.create_task(graph.ainvoke(graph_input, graph_config))
            try:
                await self._observe(task, run, runtime)
            except LeaseLost:
                log_event("worker.lease_lost", run_id=run["id"], epoch=run["lease_epoch"])
            except asyncio.CancelledError:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, LeaseLost, NeedsReconciliation):
                    await task
                # Shutdown releases no false terminal status. The next worker
                # takes the expired lease and reconciles persisted operations.
                raise
            except Exception as error:
                log_event("worker.failed", run_id=run["id"], error_type=type(error).__name__)
                current = await self.repository.get_run(run["id"])
                if current["status"] == "running":
                    await runtime._stop(run["id"], "needs_reconciliation", f"Worker stopped after {type(error).__name__}; inspect run artifacts")
        return True

    async def _observe(self, task: asyncio.Task[Any], run: dict[str, Any], runtime: Runtime) -> None:
        next_heartbeat = time.monotonic() + self.settings.lease_seconds / 3
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=0.5)
            if done:
                break
            current = await self.repository.get_run(run["id"])
            if current["status"] != "running":
                continue  # gate is persisting its interrupt
            if current["lease_epoch"] != run["lease_epoch"]:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, LeaseLost, NeedsReconciliation):
                    await task
                raise LeaseLost("Another worker owns this run")
            expired = False
            if current["state"].get("budget"):
                try:
                    BudgetManager(RunConfig.model_validate(current["config"]).budget,
                                  current["state"]["budget"]).check_runtime()
                except BudgetExceeded:
                    expired = True
            if expired:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                current = await self.repository.get_run(run["id"])
                if current["status"] == "running":
                    try:
                        await runtime._cancel(current, terminal_status="budget_exhausted",
                                              reason="Total runtime budget expired; active jobs were stopped")
                    except NeedsReconciliation as error:
                        await runtime._stop(run["id"], "needs_reconciliation", str(error))
                return
            if current["cancel_requested"]:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                current = await self.repository.get_run(run["id"])
                try:
                    await runtime._cancel(current)
                except NeedsReconciliation as error:
                    await runtime._stop(run["id"], "needs_reconciliation", str(error))
                return
            if time.monotonic() >= next_heartbeat:
                try:
                    await self.repository.heartbeat(run["id"], run["lease_epoch"], self.settings.lease_seconds)
                except BaseException:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, LeaseLost, NeedsReconciliation):
                        await task
                    raise
                next_heartbeat = time.monotonic() + self.settings.lease_seconds / 3
        await task


async def serve(settings: Settings, *, once: bool = False) -> None:
    if not settings.database_url.startswith("postgresql+psycopg://"):
        raise ValueError("The production worker requires PostgreSQL; test doubles are not a scheduler")
    repository = Repository(settings.database_url)
    try:
        async with AsyncPostgresSaver.from_conn_string(settings.checkpoint_dsn) as checkpointer:
            await checkpointer.setup()
            worker = Worker(settings, repository, checkpointer)
            try:
                while True:
                    claimed = await worker.once()
                    if once:
                        return
                    if not claimed:
                        await asyncio.sleep(settings.worker_poll_seconds)
            finally:
                worker.telemetry.close()
    finally:
        await repository.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Forge PostgreSQL/LangGraph worker")
    parser.add_argument("--once", action="store_true", help="Process at most one queued run")
    options = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    async def run() -> None:
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        assert task is not None
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, task.cancel)
        with contextlib.suppress(asyncio.CancelledError):
            await serve(Settings(), once=options.once)
    asyncio.run(run())


if __name__ == "__main__":
    main()
