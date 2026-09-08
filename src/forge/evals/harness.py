from __future__ import annotations

import asyncio
import os
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol

from forge.domain.types import RepoProfile, RunConfig, canonical_hash
from forge.evals.contracts import (
    AgentTrialOutcome,
    EvalTask,
    EvalTrial,
    ExperimentCondition,
    GradeResult,
    PrivateGraderSpec,
    PublicTrialRequest,
)
from forge.evals.dataset import Dataset, PrivateSpecStore

RunCallback = Callable[[PublicTrialRequest], Awaitable[AgentTrialOutcome]]


class Grader(Protocol):
    async def grade(
        self, task: EvalTask, repository: Path, patch: str, spec: PrivateGraderSpec,
        profile: RepoProfile, trial_id: str, *, allow_empty: bool = False,
    ) -> GradeResult: ...


def standard_conditions(config: RunConfig) -> list[ExperimentCondition]:
    """Freeze one base config; change only the declared experimental factors."""
    if len(config.models) < 2:
        raise ValueError("B3 versus strong_only needs an explicitly configured second model")
    conditions: list[ExperimentCondition] = []
    for name, strategy, routed, initial, guard in [
        ("B0", "recent", False, 0, True),
        ("B1", "evidence", False, 0, True),
        ("B2", "evidence_compact", False, 0, True),
        ("B3", "evidence_compact", True, 0, True),
        ("strong_only", "evidence_compact", False, len(config.models) - 1, True),
        ("B2_guard_off", "evidence_compact", False, 0, False),
    ]:
        frozen = config.model_copy(deep=True)
        frozen.context = frozen.context.model_copy(update={"strategy": strategy, "evidence_guard": guard})
        frozen.initial_model = initial
        frozen.max_escalations = config.max_escalations if routed else 0
        conditions.append(ExperimentCondition.model_validate({"name": name, "config": frozen, "evidence_guard": guard}))
    return conditions


class EvalHarness:
    def __init__(
        self, dataset: Dataset, private_specs: PrivateSpecStore, grader: Grader,
        repository_root: Path, profile: RepoProfile,
    ) -> None:
        self.dataset, self.private_specs, self.grader = dataset, private_specs, grader
        self.repository_root = Path(repository_root)
        self.repository_root.mkdir(parents=True, exist_ok=True)
        self.profile = profile

    async def run(
        self, tasks: Sequence[EvalTask], conditions: Sequence[ExperimentCondition],
        run_callback: RunCallback, output_path: Path, *, repeats: int = 3,
        seed: int = 1729, experiment_id: str = "experiment",
        authorized_total_cost_usd: Decimal | None = None,
    ) -> list[EvalTrial]:
        if repeats < 1 or repeats > 10:
            raise ValueError("repeats must be between 1 and 10")
        if authorized_total_cost_usd is not None and authorized_total_cost_usd <= 0:
            raise ValueError("authorized total cost must be positive")
        if await asyncio.to_thread(output_path.exists):
            raise ValueError("use a new output file; never silently replace or rerun an existing experiment")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        repositories = {}
        for task in tasks:
            repositories[task.task_id] = await asyncio.to_thread(
                self.dataset.materialize, task, self.repository_root / f"{experiment_id}-{task.task_id}",
            )
        plan = [(task, condition, repeat) for task in tasks for condition in conditions for repeat in range(repeats)]
        random.Random(seed).shuffle(plan)
        trials: list[EvalTrial] = []
        committed = Decimal(0)
        with output_path.open("x") as stream:
            for task, condition, repeat in plan:
                trial_id = f"{experiment_id}-{task.task_id}-{condition.name}-{repeat}"
                profile = self.profile.model_copy(update={"allowed_patch_paths": task.allowed_patch_paths})
                request = PublicTrialRequest(
                    trial_id=trial_id, task=task.public_projection(),
                    repository=str(repositories[task.task_id]), profile=profile,
                    condition=condition, repeat=repeat, seed=seed + repeat,
                )
                started = time.monotonic()
                live = any(model.provider != "fake" for model in condition.config.models)
                affordable = (authorized_total_cost_usd is not None and
                              committed + condition.config.budget.max_cost_usd <= authorized_total_cost_usd)
                try:
                    if live and not affordable:
                        outcome = AgentTrialOutcome(
                            execution_kind="live_model", status="not_run", failure_category="not_run",
                            message="No authorized total budget or insufficient remaining reservation for this live trial.",
                        )
                    else:
                        outcome = await run_callback(request)
                except Exception as error:
                    # No unverifiable zero cost is invented after a callback
                    # failure: the original run ledger may still contain charges.
                    outcome = AgentTrialOutcome(
                        execution_kind="live_model" if live else "fake_control_flow", status="infrastructure_failure",
                        failure_category="infrastructure", message=f"callback failed: {type(error).__name__}",
                    )
                grade = None
                status: Literal["verified", "failed", "infrastructure_failure", "not_run"] = (
                    "failed" if outcome.status == "candidate" else outcome.status
                )
                category = outcome.failure_category
                if outcome.status == "candidate":
                    try:
                        spec = self.private_specs.load(task)
                        grade = await self.grader.grade(task, repositories[task.task_id], outcome.patch,
                                                       spec, profile, trial_id)
                        status, category = grade.status, grade.category
                    except Exception as error:
                        status, category = "infrastructure_failure", "infrastructure"
                        grade = GradeResult(status="infrastructure_failure", category="infrastructure",
                                            sandbox="unknown", message=f"grader failed: {type(error).__name__}")
                within_budget = (outcome.cost_status == "exact" and outcome.cost_usd is not None
                                 and outcome.reserved_unknown_usd == 0
                                 and outcome.cost_usd <= condition.config.budget.max_cost_usd)
                if live and outcome.status != "not_run":
                    if outcome.cost_status == "exact" and outcome.cost_usd is not None:
                        committed += outcome.cost_usd + outcome.reserved_unknown_usd
                    else:
                        committed += max(condition.config.budget.max_cost_usd,
                                         (outcome.cost_usd or Decimal(0)) + outcome.reserved_unknown_usd)
                if status == "verified" and not within_budget:
                    category = "budget_exhausted" if outcome.cost_usd is not None and outcome.cost_usd > condition.config.budget.max_cost_usd else "none"
                trial = EvalTrial(
                    trial_id=trial_id, task_id=task.task_id, family=task.family, split=task.split,
                    condition=condition.name, repeat=repeat, seed=request.seed, base_commit=task.base_commit,
                    config_hash=canonical_hash(condition), execution_kind=outcome.execution_kind,
                    status=status, failure_category=category, success_within_budget=status == "verified" and within_budget,
                    cost_usd=outcome.cost_usd, reserved_unknown_usd=outcome.reserved_unknown_usd,
                    cost_status=outcome.cost_status, usage=outcome.usage,
                    latency_seconds=time.monotonic() - started, phase_seconds={
                        **outcome.phase_seconds, "verification": grade.duration_seconds if grade else 0,
                    }, tool_calls=outcome.tool_calls, compactions=outcome.compactions,
                    invalid_tool_calls=outcome.invalid_tool_calls,
                    stale_current_leaks=outcome.stale_current_leaks, constraint_losses=outcome.constraint_losses,
                    outcome=outcome, grade=grade,
                )
                stream.write(trial.model_dump_json() + "\n")
                stream.flush()
                os.fsync(stream.fileno())
                trials.append(trial)
        return trials
