"""Submission/control use cases. No model calls or repository code execution."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import re
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from docker.client import DockerClient
from docker.errors import DockerException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from forge.context.builder import SYSTEM_INSTRUCTIONS
from forge.domain.types import ModelTurn, RepoProfile, RunConfig, canonical_hash
from forge.persistence.models import EvaluationRow, RunRow, ToolRow
from forge.persistence.repository import Conflict, NotFound, Repository, row_dict
from forge.settings import Settings
from forge.tools.dispatcher import SPECS

PUBLIC_ARTIFACT_KINDS = frozenset({
    "patch", "diff", "command_stdout", "command_stderr", "command_log", "stdout", "stderr",
    "validation", "validation_log", "verification", "evaluation_report", "evaluation_metrics",
    "file_read", "file_listing", "search_results", "test_report", "run_summary",
    "context_snapshot", "transcript", "applied_patch", "baseline_receipt", "validation_receipt",
    "baseline_target_receipt", "target_receipt", "command_receipt",
})


class Unavailable(RuntimeError):
    pass


class RunFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    execution_mode: str
    run: RunConfig
    scripted_turns: list[ModelTurn] = Field(default_factory=list)

    @model_validator(mode="after")
    def mode_is_explicit(self) -> RunFile:
        if self.execution_mode not in {"live", "offline_scripted"}:
            raise ValueError("execution_mode must be live or offline_scripted")
        fake = [model.provider == "fake" for model in self.run.models]
        if self.execution_mode == "offline_scripted":
            if not all(fake) or not self.scripted_turns:
                raise ValueError("offline_scripted requires fake models and explicit turns")
            if any(turn.provider != "fake" for turn in self.scripted_turns):
                raise ValueError("Offline scripted turns must be labeled fake")
        elif any(fake) or self.scripted_turns:
            raise ValueError("Live runs cannot include fake models or scripted turns")
        return self


def frozen_commit(source: Path, reference: str) -> str:
    if not reference or len(reference) > 200 or reference.startswith("-") or "\x00" in reference:
        raise ValueError("Invalid base_commit reference")
    result = subprocess.run(["git", "-C", str(source), "rev-parse", "--verify", "--end-of-options",
                             f"{reference}^{{commit}}"], capture_output=True, text=True, timeout=10, check=False)
    commit = result.stdout.strip()
    if result.returncode or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise ValueError("base_commit must resolve to a commit in the registered repository")
    return commit


def freeze_local_image(image: str, docker_host: str | None = None) -> str:
    try:
        client = DockerClient(base_url=docker_host) if docker_host else DockerClient.from_env()
        try:
            image_id = client.images.get(image).id
        finally:
            client.close()
    except DockerException as exc:
        raise Unavailable("Configured image must already be available in the Docker daemon") from exc
    if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise Unavailable("Docker did not return an immutable image content ID")
    return image_id


class ForgeService:
    def __init__(self, settings: Settings, repository: Repository,
                 freeze_image: Callable[[str], str] | None = None):
        self.settings = settings
        self.repo = repository
        self.freeze_image = freeze_image or (lambda image: freeze_local_image(image, settings.docker_host))
        self.prompt_root = Path(__file__).resolve().parents[3] / "prompts"

    def config_file(self, group: str, config_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", config_id) or ".." in config_id:
            raise ValueError("Configuration ID must be a simple local identifier")
        root = self.settings.config_dir.resolve(strict=True)
        path = (root / group / f"{config_id}.yaml").resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("Configuration must stay inside the configured directory")
        return path

    def load_yaml(self, group: str, config_id: str) -> dict[str, Any]:
        path = self.config_file(group, config_id)
        if path.stat().st_size > 2_097_152:
            raise ValueError("Configuration file is too large")
        content = yaml.safe_load(path.read_text())
        if not isinstance(content, dict):
            raise ValueError("Configuration must be a YAML object")
        return content

    async def register_project(self, name: str, source: str, profile_id: str) -> dict[str, Any]:
        location = self.settings.authorize_source(Path(source))
        await asyncio.to_thread(frozen_commit, location, "HEAD")
        profile = RepoProfile.model_validate(self.load_yaml("repo_profiles", profile_id))
        return await self.repo.create_project(self.settings.owner_id, name, str(location), profile.model_dump(mode="json"))

    async def submit_run(self, *, project_id: str, task: str, base_commit: str,
                         config_id: str, idempotency_key: str) -> dict[str, Any]:
        body = {"project_id": project_id, "task": task, "base_commit": base_commit, "config_id": config_id}
        request_hash = canonical_hash(body)
        # Resolve idempotent retries before touching mutable local config/images.
        async with self.repo.sessions() as session:
            previous = await session.scalar(select(RunRow).where(
                RunRow.owner_id == self.settings.owner_id, RunRow.idempotency_key == idempotency_key))
            if previous is not None:
                if previous.request_hash != request_hash:
                    raise Conflict("Idempotency-Key already used for a different request")
                return row_dict(previous)
        project = await self.repo.get_project(project_id, self.settings.owner_id)
        source = self.settings.authorize_source(Path(project["source"]))
        commit = await asyncio.to_thread(frozen_commit, source, base_commit)
        config = RunFile.model_validate(self.load_yaml("runs", config_id))
        profile = RepoProfile.model_validate(project["profile"])
        image_id = await asyncio.to_thread(self.freeze_image, profile.image)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise ValueError("Image freezer must return an immutable sha256 ID")
        profile.image = image_id
        prompts = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in sorted(self.prompt_root.glob("*")) if path.is_file()}
        if not prompts:
            raise Unavailable("Versioned prompts are missing from this installation")
        packages = ("forge-agent", "openai", "anthropic", "langgraph", "langgraph-checkpoint-postgres",
                    "pydantic", "sqlalchemy", "docker", "tree-sitter", "tree-sitter-python")
        versions = {name: importlib.metadata.version(name) for name in packages}
        snapshot = config.run.model_dump(mode="json")
        manifest = {"base_commit": commit, "image_id": image_id, "config_id": config_id,
                    "config_hash": canonical_hash(snapshot), "profile_hash": canonical_hash(profile),
                    "prompt_hashes": prompts, "tool_schema_hash": canonical_hash([spec.model_dump(mode="json") for spec in SPECS]),
                    "actual_system_prompt_hash": canonical_hash(SYSTEM_INSTRUCTIONS),
                    "dependency_versions": versions, "execution_mode": config.execution_mode,
                    "models": [model.model_dump(mode="json") for model in config.run.models],
                    "model_version_note": "Configured IDs are preserved; aliases may change provider-side.",
                    "scripted_turns": [turn.model_dump(mode="json") for turn in config.scripted_turns]}
        return await self.repo.create_run(owner=self.settings.owner_id, project_id=project_id,
            idempotency_key=idempotency_key, request_hash=request_hash, task=task, base_commit=commit,
            config=snapshot, profile=profile.model_dump(mode="json"), manifest=manifest)

    async def public_run(self, run: dict[str, Any]) -> dict[str, Any]:
        result = {key: run[key] for key in ("id", "project_id", "task", "base_commit", "status", "stage", "version",
                  "event_seq", "cancel_requested", "created_at", "updated_at")}
        state = run.get("state", {})
        result.update({"verification": run.get("verification", {}),
                       "budget": state.get("budget", state.get("budget_state", {})),
                       "iteration": state.get("iteration", 0), "workspace_revision": state.get("workspace_revision", 0),
                       "manifest": {key: value for key, value in run["manifest"].items() if key != "scripted_turns"}})
        approvals = run.get("approvals")
        if approvals is None:
            approvals = (await self.repo.get_run(run["id"], self.settings.owner_id))["approvals"]
        pending = []
        async with self.repo.sessions() as session:
            for approval in approvals:
                if approval["decision"] is None:
                    tool = await session.get(ToolRow, approval["tool_id"])
                    pending.append({**approval, "call": tool.call if tool is not None else None})
        result["pending_approvals"] = pending
        return result

    async def enqueue_evaluation(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.enable_evaluations or self.settings.eval_private_dir is None:
            raise Unavailable("Evaluation submission is disabled on this server")
        config = self.load_yaml("experiments", body["config_id"])
        # Only an administrator's config can reference datasets/private roots.
        if "base_run_config" not in config or "profile_id" not in config or "dataset_id" not in config:
            raise ValueError("Experiment config requires base_run_config, profile_id and dataset_id")
        frozen = RunFile.model_validate(self.load_yaml("runs", config["base_run_config"]))
        if frozen.execution_mode != "live":
            raise ValueError("Quality evaluations require a live configuration; scripted runs are protocol tests")
        profile = RepoProfile.model_validate(self.load_yaml("repo_profiles", config["profile_id"]))
        profile.image = await asyncio.to_thread(self.freeze_image, profile.image)
        payload = {**body, "run_config": frozen.run.model_dump(mode="json"),
                   "profile": profile.model_dump(mode="json"), "dataset_id": config["dataset_id"],
                   "execution_mode": "live"}
        async with self.repo.sessions() as session, session.begin():
            row = EvaluationRow(id=uuid.uuid4().hex, owner_id=self.settings.owner_id,
                                status="queued", config=payload, results={}, created_at=await self.repo.now(session))
            session.add(row)
            await session.flush()
            return {"id": row.id, "status": row.status, "created_at": row.created_at}

    async def get_evaluation(self, evaluation_id: str) -> dict[str, Any]:
        async with self.repo.sessions() as session:
            row = await session.get(EvaluationRow, evaluation_id)
            if row is None or row.owner_id != self.settings.owner_id:
                raise NotFound("Evaluation not found")
            return {"id": row.id, "status": row.status, "created_at": row.created_at, "results": row.results,
                    "config": {key: value for key, value in row.config.items() if key not in {"private_root", "grader_spec"}}}
