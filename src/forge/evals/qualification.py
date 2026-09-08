"""Qualify task defects and evaluator validity with private reference patches.

This is not an Agent quality experiment. Both variants run in the independent
Docker grader; only redacted test counts and boundaries are written publicly.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from forge.domain.types import RepoProfile, TokenUsage, canonical_hash
from forge.evals.contracts import AgentTrialOutcome, EvalTrial, GradeResult
from forge.evals.dataset import Dataset, PrivateSpecStore
from forge.evals.grader import DockerGrader
from forge.evals.report import write_report


async def qualify_dataset(
    dataset: Dataset, private_specs: PrivateSpecStore, grader: DockerGrader,
    repository_root: Path, profile: RepoProfile, output_path: Path, *, qualification_id: str,
) -> dict[str, Any]:
    await asyncio.to_thread(repository_root.mkdir, parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for task in dataset.tasks:
        repository = await asyncio.to_thread(
            dataset.materialize, task, repository_root / f"{qualification_id}-{task.task_id}",
        )
        spec = private_specs.load(task)
        baseline = await grader.grade(task, repository, "", spec, profile,
                                      f"{qualification_id}-{task.task_id}-baseline", allow_empty=True)
        reference = await grader.grade(task, repository, spec.reference_patch, spec, profile,
                                       f"{qualification_id}-{task.task_id}-reference")
        qualified = (baseline.status == "failed" and not baseline.target_passed
                     and baseline.regression_passed and reference.status == "verified"
                     and baseline.target_tests_run == spec.target_test_count
                     and reference.target_tests_run == spec.target_test_count)
        rows.append({
            "task_id": task.task_id, "split": task.split, "base_commit": task.base_commit,
            "qualified": qualified, "private_spec_sha256": task.private_spec_sha256,
            "baseline": baseline.model_dump(mode="json", exclude={"message", "artifact_refs", "grader_workspace"}),
            "reference": reference.model_dump(mode="json", exclude={"message", "artifact_refs", "grader_workspace"}),
        })
        # Persist after every task; failed qualification remains inspectable.
        report = {"kind": "fixture_qualification_not_model_quality", "dataset_version": dataset.version,
                  "tasks_checked": len(rows), "qualified": sum(row["qualified"] for row in rows),
                  "rows": rows}
        output_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(output_path.write_text, json.dumps(report, indent=2) + "\n")
    return report


def export_qualification_report(dataset: Dataset, qualification: dict[str, Any], output: Path) -> None:
    """Publish measured qualification rows without exposing private fixes/tests."""
    tasks = {task.task_id: task for task in dataset.tasks}
    trials = []
    for row in qualification["rows"]:
        task = tasks[row["task_id"]]
        for variant in ("baseline", "reference"):
            grade = GradeResult.model_validate(row[variant])
            outcome = AgentTrialOutcome(
                execution_kind="reference_validation", status="candidate", cost_status="exact",
                cost_usd=Decimal(0), usage=TokenUsage(input_tokens=0, output_tokens=0),
                message="No model calls. Private target assertions and reference patch are intentionally omitted from this public fixture qualification export.",
            )
            trials.append(EvalTrial(
                trial_id=f"qualification-{task.task_id}-{variant}", task_id=task.task_id,
                family=task.family, split=task.split, condition=variant, repeat=0, seed=0,
                base_commit=task.base_commit, config_hash=canonical_hash({"private_spec": task.private_spec_sha256,
                                                                          "image": grade.image_id, "variant": variant}),
                execution_kind="reference_validation", status=grade.status, failure_category=grade.category,
                success_within_budget=grade.status == "verified", cost_usd=Decimal(0), reserved_unknown_usd=Decimal(0),
                cost_status="exact", usage=outcome.usage, latency_seconds=grade.duration_seconds,
                phase_seconds={"verification": grade.duration_seconds}, tool_calls=0, compactions=0,
                invalid_tool_calls=0, stale_current_leaks=None, constraint_losses=None, outcome=outcome, grade=grade,
            ))
    output.mkdir(parents=True, exist_ok=True)
    (output / "trials.jsonl").write_text("".join(trial.model_dump_json() + "\n" for trial in trials))
    write_report(trials, output)
