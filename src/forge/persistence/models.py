from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JSON_VALUE = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class ProjectRow(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(256))
    source: Mapped[str] = mapped_column(Text)
    profile: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE)
    created_at: Mapped[float] = mapped_column(Float)


class RunRow(Base):
    __tablename__ = "runs"
    __table_args__ = (UniqueConstraint("owner_id", "idempotency_key"),
                     Index("ix_runs_claim", "status", "lease_expires_at", "created_at"))
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    owner_id: Mapped[str] = mapped_column(String(128), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(256))
    request_hash: Mapped[str] = mapped_column(String(64))
    task: Mapped[str] = mapped_column(Text)
    base_commit: Mapped[str] = mapped_column(String(64))
    config: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE)
    profile: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE)
    source: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    stage: Mapped[str] = mapped_column(String(32), default="bootstrap")
    version: Mapped[int] = mapped_column(Integer, default=1)
    event_seq: Mapped[int] = mapped_column(Integer, default=0)
    lease_epoch: Mapped[int] = mapped_column(Integer, default=0)
    lease_expires_at: Mapped[float] = mapped_column(Float, default=0)
    worker_id: Mapped[str | None] = mapped_column(String(128))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    state: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, default=dict)
    verification: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, default=dict)
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)


class EventRow(Base):
    __tablename__ = "run_events"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE)
    created_at: Mapped[float] = mapped_column(Float)


class MessageRow(Base):
    __tablename__ = "user_messages"
    __table_args__ = (UniqueConstraint("run_id", "idempotency_key"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(256))
    request_hash: Mapped[str] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text)
    applied_iteration: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[float] = mapped_column(Float)


class ModelTurnRow(Base):
    __tablename__ = "model_turns"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    iteration: Mapped[int] = mapped_column(Integer, primary_key=True)
    request: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE)
    status: Mapped[str] = mapped_column(String(32))
    response: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE)
    artifact_ref: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[float] = mapped_column(Float)


class ModelAttemptRow(Base):
    __tablename__ = "model_attempts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    iteration: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE)
    created_at: Mapped[float] = mapped_column(Float)


class ToolRow(Base):
    __tablename__ = "tool_executions"
    __table_args__ = (UniqueConstraint("run_id", "iteration", "ordinal"),
                     UniqueConstraint("run_id", "iteration", "call_id"))
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    iteration: Mapped[int] = mapped_column(Integer)
    ordinal: Mapped[int] = mapped_column(Integer)
    call_id: Mapped[str] = mapped_column(String(256))
    call: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE)
    args_hash: Mapped[str] = mapped_column(String(64))
    workspace_revision: Mapped[int] = mapped_column(Integer)
    epoch: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE)
    created_at: Mapped[float] = mapped_column(Float)


class ApprovalRow(Base):
    __tablename__ = "approvals"
    __table_args__ = (UniqueConstraint("tool_id", "workspace_revision"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    tool_id: Mapped[str] = mapped_column(ForeignKey("tool_executions.id"))
    args_hash: Mapped[str] = mapped_column(String(64))
    workspace_revision: Mapped[int] = mapped_column(Integer)
    policy_hash: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    decision: Mapped[str | None] = mapped_column(String(8))
    created_at: Mapped[float] = mapped_column(Float)


class ArtifactRow(Base):
    __tablename__ = "artifacts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    owner_id: Mapped[str] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE)


class EvaluationRow(Base):
    __tablename__ = "evaluations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32))
    config: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE)
    results: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, default=dict)
    created_at: Mapped[float] = mapped_column(Float)


class EvalTrialRow(Base):
    __tablename__ = "eval_trials"
    __table_args__ = (UniqueConstraint("evaluation_id", "task_id", "condition", "repeat"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    evaluation_id: Mapped[str] = mapped_column(ForeignKey("evaluations.id"), index=True)
    task_id: Mapped[str] = mapped_column(String(128))
    condition: Mapped[str] = mapped_column(String(64))
    repeat: Mapped[int] = mapped_column(Integer)
    result: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE)
