"""A separate evaluation scheduler; normal Forge workers execute its child runs.

PostgreSQL advisory locks identify a live evaluator process. After a crash,
unfinished experiments move to needs_reconciliation instead of replaying possibly
billed trials. Run IDs and trial IDs remain in the evaluation's progress record.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func, select

from forge.application.services import ForgeService
from forge.artifacts.store import LocalArtifactStore, atomic_write
from forge.context.budget import BudgetManager
from forge.domain.types import RepoProfile, RunConfig, TokenUsage, canonical_hash
from forge.evals.contracts import AgentTrialOutcome, FailureCategory, PublicTrialRequest
from forge.evals.dataset import Dataset, PrivateSpecStore
from forge.evals.grader import DockerGrader
from forge.evals.harness import EvalHarness, RunCallback, standard_conditions
from forge.evals.report import summarize, write_report
from forge.execution.docker import DockerExecutor
from forge.persistence.models import EvalTrialRow, EvaluationRow
from forge.persistence.repository import TERMINAL, Repository, row_dict
from forge.settings import Settings

Progress = Callable[[dict[str, Any]], Awaitable[None]]


class ServiceRunCallback:
    def __init__(
        self, settings: Settings, repository: Repository, evaluation_id: str,
        progress: Progress, *, poll_seconds: float = 0.5,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.evaluation_id = evaluation_id
        self.progress = progress
        self.poll_seconds = poll_seconds
        self.generated = settings.config_dir / "generated" / evaluation_id
        self.service = ForgeService(settings.model_copy(update={"config_dir": self.generated}), repository)
        self.artifacts = LocalArtifactStore(settings.data_dir / "artifacts")
        self.projects: dict[str, str] = {}

    def _configuration(self, request: PublicTrialRequest) -> tuple[str, str]:
        if any(model.provider == "fake" for model in request.condition.config.models):
            raise ValueError("the quality-evaluation worker requires real providers; inject a contract callback for offline tests")
        config_id = "condition-" + canonical_hash(request.condition)[:24]
        profile_id = "profile-" + canonical_hash(request.profile)[:24]
        run_data = {"execution_mode": "live", "run": request.condition.config.model_dump(mode="json")}
        profile = request.profile.model_copy(update={"target_test_argv": []})
        for group, identifier, payload in [("runs", config_id, run_data),
                                            ("repo_profiles", profile_id, profile.model_dump(mode="json"))]:
            path = self.generated / group / f"{identifier}.yaml"
            encoded = yaml.safe_dump(payload, sort_keys=True).encode()
            if path.exists() and path.read_bytes() != encoded:
                raise ValueError("frozen generated configuration changed")
            atomic_write(path, encoded)
        return config_id, profile_id

    async def __call__(self, request: PublicTrialRequest) -> AgentTrialOutcome:
        config_id, profile_id = await asyncio.to_thread(self._configuration, request)
        await self.progress({"active_trial_id": request.trial_id, "active_run_id": None})
        project_key = canonical_hash([request.repository, request.profile.model_dump(mode="json")])
        if project_key not in self.projects:
            project = await self.service.register_project(
                "Eval " + request.task.task_id, request.repository, profile_id,
            )
            self.projects[project_key] = project["id"]
        run = await self.service.submit_run(
            project_id=self.projects[project_key],
            task=request.task.instruction + "\n\nConstraints:\n" + "\n".join(request.task.constraints),
            base_commit=request.task.base_commit, config_id=config_id,
            idempotency_key="evaluation:" + request.trial_id,
        )
        await self.progress({"active_trial_id": request.trial_id, "active_run_id": run["id"]})
        deadline = time.monotonic() + request.condition.config.budget.max_runtime_seconds + 300
        timed_out = False
        while run["status"] not in TERMINAL | {"needs_reconciliation"}:
            if run["status"] == "waiting_approval" or time.monotonic() >= deadline:
                await self.repository.cancel(run["id"], self.settings.owner_id)
                timed_out = True
                # Cancellation requests are not proof that a running job ended.
                cancellation_deadline = time.monotonic() + 60
                while run["status"] not in TERMINAL | {"needs_reconciliation"} and time.monotonic() < cancellation_deadline:
                    await asyncio.sleep(self.poll_seconds)
                    run = await self.repository.get_run(run["id"], self.settings.owner_id)
                break
            await asyncio.sleep(self.poll_seconds)
            run = await self.repository.get_run(run["id"], self.settings.owner_id)
        records = await self.repository.list_artifacts(run["id"], self.settings.owner_id)
        patches = [record for record in records if record["kind"] == "patch"]
        patch = self.artifacts.read_text(patches[-1]["id"]) if patches else ""
        state = run.get("state", {})
        manager = BudgetManager(request.condition.config.budget, state.get("budget"))
        reservations = list(manager.state.reservations.values())
        unknown = [item for item in reservations if item.status != "settled"]
        usage_rows = [item.usage for item in reservations]
        terminal_confirmed = run["status"] in TERMINAL
        reserved_unknown = sum((item.charged_usd for item in unknown), Decimal(0))
        if not terminal_confirmed:
            # A cancellation request or a stale checkpoint cannot establish the
            # final bill. Retain the run's remaining ceiling until reconciled.
            reserved_unknown = max(reserved_unknown,
                request.condition.config.budget.max_cost_usd - manager.known_spend_usd)

        def tokens(field: str) -> int | None:
            if any(usage is None or getattr(usage, field) is None for usage in usage_rows):
                return None
            return sum(getattr(usage, field) for usage in usage_rows)

        phase_seconds: dict[str, float] = {"run": max(0, run["updated_at"] - run["created_at"])}
        execution_durations = []
        for record in records:
            if record["kind"] == "command_receipt":
                receipt = json.loads(self.artifacts.read_text(record["id"]))
                if "duration_seconds" in receipt:
                    execution_durations.append(receipt["duration_seconds"])
        if execution_durations:
            phase_seconds["execution"] = sum(execution_durations)
        events = []
        cursor = 0
        while True:
            page = await self.repository.list_events(run["id"], self.settings.owner_id, cursor, 200)
            events.extend(page)
            if not page or len(page) < 200:
                break
            cursor = page[-1]["seq"]
        candidate = (bool(patch) and run["status"] in {"completed", "failed"}
                     and run.get("verification", {}).get("status") in {"verified", "regression_only", "failed"})
        category: FailureCategory = "none"
        status = "candidate" if candidate else "failed"
        if run["status"] not in TERMINAL or timed_out:
            status, category = "infrastructure_failure", "infrastructure"
        elif run["status"] == "budget_exhausted":
            category = "budget_exhausted"
        elif not candidate:
            reason = str(state.get("failure_class") or state.get("stop_reason", "")).lower()
            category = "stalled" if "stall" in reason else "permission_denied" if "denied" in reason else "agent_failure"
        return AgentTrialOutcome.model_validate({
            "run_id": run["id"], "patch": patch, "status": status, "failure_category": category,
            "execution_kind": "live_model", "message": state.get("stop_reason", ""),
            "cost_usd": manager.known_spend_usd,
            "reserved_unknown_usd": reserved_unknown,
            "cost_status": manager.cost_status if terminal_confirmed else "unknown",
            "usage": TokenUsage(input_tokens=tokens("input_tokens"), output_tokens=tokens("output_tokens"),
                                cached_input_tokens=tokens("cached_input_tokens"),
                                estimated=bool(unknown) or not terminal_confirmed),
            "tool_calls": manager.state.tool_calls,
            "compactions": sum(event["kind"] == "context.compacted" for event in events),
            "invalid_tool_calls": state.get("invalid_tool_calls", 0),
            "phase_seconds": phase_seconds, "artifact_refs": [record["id"] for record in records],
        })


class EvaluationWorker:
    def __init__(self, settings: Settings, repository: Repository, *, dataset_root: Path | None = None,
                 callback_factory: Callable[[str, Progress], RunCallback] | None = None) -> None:
        self.settings, self.repository = settings, repository
        self.dataset_root = dataset_root or Path(__file__).resolve().parents[3] / "evals"
        self.callback_factory = callback_factory
        self.worker_id = "evaluator-" + uuid.uuid4().hex[:16]

    async def _progress(self, evaluation_id: str, update: dict[str, Any]) -> None:
        async with self.repository.sessions() as session, session.begin():
            row = await session.get(EvaluationRow, evaluation_id, with_for_update=True)
            if row is None or row.status != "running" or row.results.get("worker_id") != self.worker_id:
                raise ValueError("evaluation ownership is no longer active")
            merged = {**row.results, **update}
            if update.get("active_run_id") and update.get("active_trial_id"):
                merged["submitted_runs"] = {**row.results.get("submitted_runs", {}),
                                             update["active_trial_id"]: update["active_run_id"]}
            row.results = merged

    async def once(self) -> bool:
        if self.repository.engine.dialect.name != "postgresql":
            raise ValueError("evaluation scheduling requires PostgreSQL advisory locks")
        if not self.settings.enable_evaluations or self.settings.eval_private_dir is None:
            raise ValueError("enable evaluations and configure an external private-spec directory")
        async with self.repository.sessions() as session:
            candidates = list(await session.scalars(select(EvaluationRow).where(
                EvaluationRow.owner_id == self.settings.owner_id,
                EvaluationRow.status.in_(["queued", "running"]),
            ).order_by(EvaluationRow.created_at).limit(20)))
        for candidate in candidates:
            key = int(hashlib.sha256(("evaluation:" + candidate.id).encode()).hexdigest()[:15], 16)
            async with self.repository.engine.connect() as connection:
                locked = await connection.scalar(select(func.pg_try_advisory_lock(key)))
                if not locked:
                    continue
                try:
                    async with self.repository.sessions() as session, session.begin():
                        row = await session.get(EvaluationRow, candidate.id, with_for_update=True)
                        if row is None or row.status not in {"queued", "running"}:
                            continue
                        if row.status == "running":
                            row.status = "needs_reconciliation"
                            row.results = {**row.results, "reason": "Previous evaluator exited; inspect recorded trials and active run before resuming. No trial was automatically replayed."}
                            return True
                        row.status = "running"
                        row.results = {"worker_id": self.worker_id, "started_at": time.time()}
                        evaluation = row_dict(row)
                    try:
                        await self._execute(evaluation)
                    except asyncio.CancelledError:
                        await self._progress(evaluation["id"], {"interrupted": True})
                        raise
                    except Exception as error:
                        async with self.repository.sessions() as session, session.begin():
                            row = await session.get(EvaluationRow, evaluation["id"], with_for_update=True)
                            assert row is not None
                            row.status = "needs_reconciliation"
                            row.results = {**row.results, "reason": f"Evaluator stopped after {type(error).__name__}; inspect existing run and trial artifacts before retrying."}
                    return True
                finally:
                    await connection.execute(select(func.pg_advisory_unlock(key)))
        return False

    async def _execute(self, evaluation: dict[str, Any]) -> None:
        config = evaluation["config"]
        if config["dataset_id"] not in {"forge-diagnostics-v1", "diagnostics-v1"}:
            raise ValueError("unknown server-side dataset ID")
        dataset = Dataset(self.dataset_root)
        selected_ids = set(config.get("task_ids", []))
        if selected_ids - {task.task_id for task in dataset.tasks}:
            raise ValueError("unknown task ID in evaluation")
        tasks = [task for task in dataset.tasks if not selected_ids or task.task_id in selected_ids]
        all_conditions = standard_conditions(RunConfig.model_validate(config["run_config"]))
        selected_conditions = set(config.get("conditions", []))
        if selected_conditions - {condition.name for condition in all_conditions}:
            raise ValueError("unknown experimental condition")
        conditions = [condition for condition in all_conditions if not selected_conditions or condition.name in selected_conditions]
        private_dir = self.settings.eval_private_dir
        assert private_dir is not None
        executor = DockerExecutor(self.settings.docker_host, self.settings.allow_rootful_development)
        root = self.settings.data_dir / "evaluations" / evaluation["id"]
        profile = RepoProfile.model_validate(config["profile"])
        harness = EvalHarness(dataset, PrivateSpecStore(private_dir),
                              DockerGrader(private_dir / "workspaces" / evaluation["id"], executor),
                              root / "repositories", profile)

        async def progress(update: dict[str, Any]) -> None:
            await self._progress(evaluation["id"], update)

        callback = (self.callback_factory(evaluation["id"], progress) if self.callback_factory else
                    ServiceRunCallback(self.settings, self.repository, evaluation["id"], progress))
        await progress({"raw_trials": str(root / "trials.jsonl")})
        trials = await harness.run(
            tasks, conditions, callback, root / "trials.jsonl", repeats=config.get("repeats", 3),
            seed=config.get("seed", 1729), experiment_id=evaluation["id"],
            authorized_total_cost_usd=Decimal(str(config["budget_usd"])),
        )
        reports = await asyncio.to_thread(write_report, trials, root / "report")
        async with self.repository.sessions() as session, session.begin():
            row = await session.get(EvaluationRow, evaluation["id"], with_for_update=True)
            assert row is not None
            if row.status != "running" or row.results.get("worker_id") != self.worker_id:
                raise ValueError("evaluation ownership changed before finalization")
            for trial in trials:
                session.add(EvalTrialRow(id=uuid.uuid4().hex, evaluation_id=row.id,
                                        task_id=trial.task_id, condition=trial.condition, repeat=trial.repeat,
                                        result=trial.model_dump(mode="json")))
            row.status = "completed" if all(trial.status not in {"not_run", "infrastructure_failure"} for trial in trials) else "completed_with_gaps"
            row.results = {**row.results, "active_trial_id": None, "active_run_id": None,
                           "summary": summarize(trials), "reports": {key: str(value) for key, value in reports.items()},
                           "raw_trials": str(root / "trials.jsonl")}
        if executor._docker is not None:
            await asyncio.to_thread(executor._docker.close)


async def serve(settings: Settings, *, once: bool = False) -> None:
    repository = Repository(settings.database_url)
    try:
        worker = EvaluationWorker(settings, repository)
        while True:
            did_work = await worker.once()
            if once:
                return
            if not did_work:
                await asyncio.sleep(settings.worker_poll_seconds)
    finally:
        await repository.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Forge independent evaluation scheduler")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve(Settings(), once=args.once))


if __name__ == "__main__":
    main()
