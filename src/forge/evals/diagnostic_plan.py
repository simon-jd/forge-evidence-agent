"""Explicit imbalanced allocation and descriptive comparison without paid calls."""
from __future__ import annotations

from collections import Counter

from forge.evals.contracts import EvalTrial

CONDITIONS = ("B0", "B1", "B2", "B3", "strong_only", "B2_guard_off")


def imbalanced_plan(task_ids: list[str], first_seed: int = 7) -> list[dict[str, str | int]]:
    """Freeze 10+11+12+13+14+15 slots; extras are planned, never failure retries."""
    if len(task_ids) != 12 or len(set(task_ids)) != 12:
        raise ValueError("Exactly 12 distinct diagnostic task IDs are required")
    slots: list[dict[str, str | int]] = []
    for index, condition in enumerate(CONDITIONS):
        for slot in range(10 + index):
            slots.append({"condition": condition, "task_id": task_ids[slot % 12],
                          "repeat": slot // 12, "seed": first_seed + slot // 12})
    return slots


def common_first_seed(trials: list[EvalTrial]) -> dict:
    """Keep one matching first-seed observation per task across every condition.

    Missing, duplicate or differently seeded cells cannot silently become a fair
    comparison. This is descriptive; no interval or significance claim is made.
    """
    indexed: dict[tuple[str, str, int], EvalTrial] = {}
    if len({trial.trial_id for trial in trials}) != len(trials):
        raise ValueError("Duplicate trial_id")
    for trial in trials:
        if trial.repeat != 0:
            continue
        key = (trial.condition, trial.task_id, trial.seed)
        if key in indexed:
            raise ValueError("Duplicate first-seed observation")
        indexed[key] = trial
    conditions = sorted({trial.condition for trial in trials})
    cells = [{(task, seed) for condition, task, seed in indexed if condition == name}
             for name in conditions]
    common = set.intersection(*cells) if cells else set()
    tasks = [task for task, _ in common]
    if len(tasks) != len(set(tasks)):
        raise ValueError("Multiple first seeds for the same task")
    rows = [indexed[condition, task, seed] for condition in conditions
            for task, seed in sorted(common)]
    return {"scope": "descriptive_common_tasks_first_seed", "trial_count": len(rows),
            "tasks": sorted(tasks), "conditions": conditions,
            "allocation": dict(Counter(row.condition for row in rows)),
            "verified": {name: sum(row.status == "verified" for row in rows
                                    if row.condition == name) for name in conditions},
            "note": "Pooled imbalanced counts cannot rank configurations; no significance conclusion."}
