from __future__ import annotations

import copy
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from forge.domain.types import canonical_hash
from forge.persistence.models import (
    ApprovalRow,
    ArtifactRow,
    Base,
    EventRow,
    MessageRow,
    ProjectRow,
    RunRow,
)

TERMINAL = {"completed", "failed", "cancelled", "budget_exhausted"}


class NotFound(LookupError):
    pass


class Conflict(ValueError):
    pass


class LeaseLost(RuntimeError):
    pass


def row_dict(row: Any) -> dict[str, Any]:
    return {column.key: copy.deepcopy(getattr(row, column.key)) for column in row.__table__.columns}


class Repository:
    def __init__(self, database_url: str) -> None:
        self.engine = create_async_engine(database_url, pool_pre_ping=True, hide_parameters=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def close(self) -> None:
        await self.engine.dispose()

    async def create_schema_for_tests(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def now(self, session: AsyncSession) -> float:
        if self.engine.dialect.name == "postgresql":
            value = await session.scalar(select(func.extract("epoch", func.clock_timestamp())))
            assert value is not None
            return float(value)
        return time.time()

    @asynccontextmanager
    async def locked(
        self, run_id: str, *, epoch: int | None = None, owner: str | None = None,
    ) -> AsyncIterator[tuple[AsyncSession, RunRow]]:
        async with self.sessions() as session, session.begin():
            run = await session.scalar(select(RunRow).where(RunRow.id == run_id).with_for_update())
            if run is None or (owner is not None and run.owner_id != owner):
                raise NotFound("Run not found")
            now = await self.now(session)
            if epoch is not None and (run.lease_epoch != epoch or run.lease_expires_at <= now
                                      or run.status != "running"):
                raise LeaseLost("Worker no longer owns an active run lease")
            yield session, run
            run.updated_at = now
            run.version += 1

    async def event(
        self, session: AsyncSession, run: RunRow, kind: str, payload: dict[str, Any],
    ) -> None:
        run.event_seq += 1
        session.add(EventRow(run_id=run.id, seq=run.event_seq, kind=kind,
                             payload=payload, created_at=await self.now(session)))

    async def create_project(self, owner: str, name: str, source: str,
                             profile: dict[str, Any]) -> dict[str, Any]:
        async with self.sessions() as session, session.begin():
            project = ProjectRow(id=uuid.uuid4().hex, owner_id=owner, name=name, source=source,
                                 profile=profile, created_at=await self.now(session))
            session.add(project)
            await session.flush()
            return row_dict(project)

    async def get_project(self, project_id: str, owner: str) -> dict[str, Any]:
        async with self.sessions() as session:
            row = await session.get(ProjectRow, project_id)
            if row is None or row.owner_id != owner:
                raise NotFound("Project not found")
            return row_dict(row)

    async def create_run(
        self, *, owner: str, project_id: str, idempotency_key: str,
        request_hash: str, task: str, base_commit: str, config: dict[str, Any],
        profile: dict[str, Any], manifest: dict[str, Any],
    ) -> dict[str, Any]:
        async with self.sessions() as session, session.begin():
            # Project lock serializes equal idempotency keys without catching a
            # uniqueness failure after the transaction has already aborted.
            project = await session.scalar(select(ProjectRow).where(
                ProjectRow.id == project_id, ProjectRow.owner_id == owner).with_for_update())
            if project is None:
                raise NotFound("Project not found")
            if self.engine.dialect.name == "postgresql":
                # Key scope is owner-wide, including two different projects.
                await session.execute(select(func.pg_advisory_xact_lock(
                    int(canonical_hash([owner, idempotency_key])[:15], 16))))
            previous = await session.scalar(select(RunRow).where(
                RunRow.owner_id == owner, RunRow.idempotency_key == idempotency_key))
            if previous is not None:
                if previous.request_hash != request_hash:
                    raise Conflict("Idempotency-Key already used for a different request")
                return row_dict(previous)
            now = await self.now(session)
            run = RunRow(id=uuid.uuid4().hex, owner_id=owner, project_id=project_id,
                         idempotency_key=idempotency_key, request_hash=request_hash,
                         task=task, base_commit=base_commit, config=config, profile=profile,
                         manifest=manifest, source=project.source, status="queued", stage="bootstrap",
                         version=1, event_seq=0, lease_epoch=0, lease_expires_at=0,
                         cancel_requested=False, state={}, verification={}, created_at=now,
                         updated_at=now)
            session.add(run)
            await session.flush()
            await self.event(session, run, "run.created", {"status": "queued"})
            await session.flush()
            return row_dict(run)

    async def get_run(self, run_id: str, owner: str | None = None) -> dict[str, Any]:
        async with self.sessions() as session:
            run = await session.get(RunRow, run_id)
            if run is None or (owner is not None and run.owner_id != owner):
                raise NotFound("Run not found")
            result = row_dict(run)
            approvals = await session.scalars(select(ApprovalRow).where(
                ApprovalRow.run_id == run_id).order_by(ApprovalRow.created_at))
            result["approvals"] = [row_dict(row) for row in approvals]
            return result

    async def claim(self, worker_id: str, lease_seconds: int) -> dict[str, Any] | None:
        async with self.sessions() as session, session.begin():
            now = await self.now(session)
            run = await session.scalar(select(RunRow).where(or_(
                RunRow.status == "queued",
                and_(RunRow.status == "running", RunRow.lease_expires_at <= now),
            )).order_by(RunRow.created_at).limit(1).with_for_update(skip_locked=True))
            if run is None:
                return None
            run.status = "running"
            run.worker_id = worker_id
            run.lease_epoch += 1
            run.lease_expires_at = now + lease_seconds
            run.updated_at = now
            run.version += 1
            await self.event(session, run, "run.claimed", {"epoch": run.lease_epoch,
                                                         "worker_id": worker_id})
            await session.flush()
            return row_dict(run)

    async def heartbeat(self, run_id: str, epoch: int, lease_seconds: int) -> None:
        async with self.locked(run_id, epoch=epoch) as (session, run):
            run.lease_expires_at = await self.now(session) + lease_seconds

    async def list_events(self, run_id: str, owner: str, after: int, limit: int = 200) -> list[dict[str, Any]]:
        await self.get_run(run_id, owner)
        async with self.sessions() as session:
            rows = await session.scalars(select(EventRow).where(EventRow.run_id == run_id,
                EventRow.seq > after).order_by(EventRow.seq).limit(min(limit, 500)))
            return [row_dict(row) for row in rows]

    async def add_message(self, run_id: str, owner: str, key: str, text: str) -> dict[str, Any]:
        async with self.locked(run_id, owner=owner) as (session, run):
            previous = await session.scalar(select(MessageRow).where(
                MessageRow.run_id == run_id, MessageRow.idempotency_key == key))
            if previous is not None:
                if previous.request_hash != canonical_hash(text):
                    raise Conflict("Idempotency-Key already used for a different message")
                return row_dict(previous)
            if run.status in TERMINAL:
                raise Conflict("Cannot append messages to a terminal run")
            message = MessageRow(id=uuid.uuid4().hex, run_id=run_id, idempotency_key=key,
                                 text=text, request_hash=canonical_hash(text),
                                 created_at=await self.now(session))
            session.add(message)
            await self.event(session, run, "message.received", {"message_id": message.id})
            await session.flush()
            return row_dict(message)

    async def cancel(self, run_id: str, owner: str) -> dict[str, Any]:
        async with self.locked(run_id, owner=owner) as (session, run):
            if run.status not in TERMINAL and not run.cancel_requested:
                run.cancel_requested = True
                if run.status in {"waiting_approval", "needs_reconciliation"}:
                    # A worker must confirm the external job has stopped.
                    run.status = "queued"
                await self.event(session, run, "run.cancel_requested", {})
        return await self.get_run(run_id, owner)

    async def resume(self, run_id: str, owner: str, version: int) -> dict[str, Any]:
        async with self.locked(run_id, owner=owner) as (session, run):
            if run.version != version:
                raise Conflict("Run version changed; reload before resuming")
            if run.status != "needs_reconciliation" or run.cancel_requested:
                raise Conflict("Only a reconciliation pause can be resumed; approval has its own endpoint")
            # This grants permission to query the existing receipt, never to
            # replay an unknown effect. The executor still rejects uncertainty.
            run.status = "queued"
            await self.event(session, run, "run.resumed", {"mode": "reconcile_existing_operation"})
        return await self.get_run(run_id, owner)

    async def decide(self, approval_id: str, owner: str, decision: str, version: int) -> dict[str, Any]:
        if decision not in {"allow", "deny"}:
            raise ValueError("Expected allow or deny")
        async with self.sessions() as session:
            approval = await session.get(ApprovalRow, approval_id)
            if approval is None:
                raise NotFound("Approval not found")
            run_id = approval.run_id
        async with self.locked(run_id, owner=owner) as (session, run):
            approval = await session.get(ApprovalRow, approval_id)
            assert approval is not None
            if approval.decision is not None:
                if approval.decision != decision:
                    raise Conflict("Approval already has a different decision")
                return row_dict(approval)
            if approval.version != version or run.status != "waiting_approval" or run.cancel_requested:
                raise Conflict("Approval or run state changed")
            if run.state.get("approval_id") != approval_id:
                raise Conflict("Approval no longer refers to the pending operation")
            approval.decision = decision
            approval.version += 1
            run.status = "queued"
            await self.event(session, run, "approval.decided", {"approval_id": approval_id,
                                                              "decision": decision})
            await session.flush()
            return row_dict(approval)

    async def list_artifacts(self, run_id: str, owner: str) -> list[dict[str, Any]]:
        await self.get_run(run_id, owner)
        async with self.sessions() as session:
            rows = await session.scalars(select(ArtifactRow).where(ArtifactRow.run_id == run_id,
                                                                  ArtifactRow.owner_id == owner))
            return [row_dict(row) for row in rows]

    async def get_artifact(self, artifact_id: str, owner: str) -> dict[str, Any]:
        async with self.sessions() as session:
            row = await session.get(ArtifactRow, artifact_id)
            if row is None or row.owner_id != owner:
                raise NotFound("Artifact not found")
            return row_dict(row)
