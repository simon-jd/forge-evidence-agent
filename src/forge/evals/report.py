from __future__ import annotations

import csv
import html
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from forge.evals.contracts import EvalTrial


def _percentile(values: list[float], probability: float) -> float | None:
    return sorted(values)[max(0, math.ceil(probability * len(values)) - 1)] if values else None


def summarize(trials: list[EvalTrial]) -> dict[str, Any]:
    if len({trial.execution_kind for trial in trials}) > 1:
        raise ValueError("live, fake-control-flow and reference-validation trials require separate reports")
    by_condition: dict[str, list[EvalTrial]] = defaultdict(list)
    for trial in trials:
        by_condition[trial.condition].append(trial)
    conditions: dict[str, dict[str, Any]] = {}
    for condition, rows in sorted(by_condition.items()):
        started = [row for row in rows if row.status != "not_run"]
        usable = [row for row in started if row.status != "infrastructure_failure"]
        unknown = [row for row in started if row.cost_status not in {"exact", "upper_bound"}
                   or row.cost_usd is None or row.reserved_unknown_usd > 0]
        upper_bound = [row for row in started if row.cost_status == "upper_bound"]
        known_cost = sum((row.cost_usd or Decimal(0) for row in started), Decimal(0))
        exact_cost = known_cost if not unknown and not upper_bound else None
        ceiling_cost = known_cost if not unknown else None
        verified = sum(row.status == "verified" for row in started)
        succeeded = sum(row.success_within_budget for row in started)
        latencies = [row.latency_seconds for row in started]
        conditions[condition] = {
            "tasks": len({row.task_id for row in rows}), "planned_trials": len(rows),
            "started_trials": len(started), "not_run_trials": len(rows) - len(started),
            "usable_trials": len(usable), "infrastructure_failures": len(started) - len(usable),
            "verified_trials": verified, "success_within_budget": succeeded,
            "success_rate_all_started": succeeded / len(started) if started else None,
            "success_rate_usable": succeeded / len(usable) if usable else None,
            "cost_exact_usd": str(exact_cost) if exact_cost is not None else None,
            "cost_upper_bound_usd": str(ceiling_cost) if ceiling_cost is not None else None,
            "upper_bound_cost_trials": len(upper_bound),
            "reported_cost_sum_usd": str(known_cost), "unknown_cost_trials": len(unknown),
            "reserved_unknown_usd": str(sum((row.reserved_unknown_usd for row in started), Decimal(0))),
            "cost_per_verified_usd": str(exact_cost / verified) if verified and exact_cost is not None else None,
            "cost_per_verified_upper_bound_usd": str(ceiling_cost / verified) if verified and ceiling_cost is not None else None,
            "latency_median_seconds": statistics.median(latencies) if latencies else None,
            "latency_p95_seconds": _percentile(latencies, 0.95),
            "failures": dict(Counter(row.failure_category for row in started if row.failure_category != "none")),
            "execution_kinds": dict(Counter(row.execution_kind for row in rows)),
            "tool_calls": sum(row.tool_calls for row in started),
            "compactions": sum(row.compactions for row in started),
            "invalid_tool_calls": sum(row.invalid_tool_calls for row in started),
            "input_tokens_reported": sum(row.usage.input_tokens or 0 for row in started),
            "output_tokens_reported": sum(row.usage.output_tokens or 0 for row in started),
            "cached_input_tokens_reported": sum(row.usage.cached_input_tokens or 0 for row in started),
            "input_usage_unknown_trials": sum(row.usage.input_tokens is None for row in started),
            "output_usage_unknown_trials": sum(row.usage.output_tokens is None for row in started),
            "stale_current_leaks": sum(row.stale_current_leaks or 0 for row in started),
            "stale_leak_measurement_missing_trials": sum(row.stale_current_leaks is None for row in started),
            "constraint_losses": sum(row.constraint_losses or 0 for row in started),
            "constraint_measurement_missing_trials": sum(row.constraint_losses is None for row in started),
            "phase_seconds": {
                phase: sum(row.phase_seconds.get(phase, 0) for row in started)
                for phase in sorted({phase for row in started for phase in row.phase_seconds})
            },
            "phase_measurement_missing_trials": {
                phase: sum(phase not in row.phase_seconds for row in started)
                for phase in ["model", "retrieval", "execution", "verification"]
            },
        }
    comparisons = []
    for left, right in [("B0", "B1"), ("B1", "B2"), ("strong_only", "B3"), ("B2_guard_off", "B2")]:
        if left in by_condition and right in by_condition:
            comparisons.append(paired_comparison(trials, left, right))
    return {
        "conditions": conditions, "paired_comparisons": comparisons,
        "interpretation": [
            "Repeats are observations of the same tasks, not independent tasks or best-of-N success.",
            "Infrastructure failures are separate; both all-started and usable success denominators are shown.",
            "Unknown cost is not zero; exact cost per success is unavailable if any started trial has uncertain cost.",
            "Fake and reference-validation results test plumbing and fixtures, not model repair quality.",
            "The diagnostic heldout split is small and is not an official SWE-bench score.",
        ],
    }


def paired_comparison(trials: list[EvalTrial], baseline: str, treatment: str) -> dict[str, Any]:
    grouped: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in trials:
        if row.condition in {baseline, treatment} and row.status not in {"not_run", "infrastructure_failure"}:
            grouped[row.condition][row.task_id].append(float(row.success_within_budget))
    shared = sorted(set(grouped[baseline]) & set(grouped[treatment]))
    differences = [statistics.mean(grouped[treatment][task]) - statistics.mean(grouped[baseline][task])
                   for task in shared]
    interval = None
    if len(differences) >= 2:
        rng = random.Random(1729)
        bootstrap = [statistics.mean(rng.choices(differences, k=len(differences))) for _ in range(2000)]
        interval = [_percentile(bootstrap, 0.025), _percentile(bootstrap, 0.975)]
    return {
        "baseline": baseline, "treatment": treatment, "paired_tasks": len(shared),
        "task_mean_success_delta": statistics.mean(differences) if differences else None,
        "task_cluster_bootstrap_95_percent": interval,
        "task_deltas": dict(zip(shared, differences, strict=True)),
        "note": "Each task contributes one average across its repeats. Interval resamples tasks.",
    }


def write_report(trials: list[EvalTrial], destination: Path) -> dict[str, Path]:
    destination.mkdir(parents=True, exist_ok=True)
    summary = summarize(trials)
    summary_path = destination / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    csv_path = destination / "trials.csv"
    fields = ["trial_id", "task_id", "split", "condition", "repeat", "execution_kind", "status",
              "failure_category", "success_within_budget", "cost_usd", "cost_status",
              "reserved_unknown_usd", "latency_seconds", "tool_calls", "compactions", "invalid_tool_calls"]
    with csv_path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for trial in trials:
            writer.writerow({field: getattr(trial, field) for field in fields})
    def escape(value: Any) -> str:
        return html.escape("—" if value is None else str(value))
    condition_rows = "".join(
        "<tr>" + "".join(f"<td>{escape(value)}</td>" for value in [
            condition, values["tasks"], values["started_trials"], values["not_run_trials"],
            values["success_within_budget"], values["infrastructure_failures"],
            values["cost_per_verified_usd"], values["cost_per_verified_upper_bound_usd"], values["unknown_cost_trials"],
        ]) + "</tr>" for condition, values in summary["conditions"].items()
    )
    trial_rows = "".join(
        "<tr>" + "".join(f"<td>{escape(value)}</td>" for value in [
            row.task_id, row.condition, row.repeat, row.execution_kind, row.status,
            row.failure_category, row.cost_usd, row.cost_status,
        ]) + "</tr>" for row in trials
    )
    page = """<!doctype html><html lang="en"><meta charset="utf-8"><title>Forge diagnostic evaluation</title>
<style>body{font:15px/1.55 system-ui;margin:40px auto;max-width:1250px;padding:0 24px;color:#192b37;background:#f6f8fa}
h1,h2{font-weight:650}table{border-collapse:collapse;width:100%;background:white;margin:24px 0;font-size:13px}
th,td{text-align:left;padding:10px;border-bottom:1px solid #dce3e8}th{background:#e8eff2}pre{white-space:pre-wrap;background:#e8eff2;padding:20px}
.note{border-left:4px solid #99743b;padding:14px;background:#fff8e8}</style>
<h1>Forge · diagnostic evaluation</h1><p class="note">Read execution_kind before interpreting a score. Offline control-flow and reference-patch runs are not live-model results.</p>
<h2>Conditions</h2><table><thead><tr><th>Condition</th><th>Tasks</th><th>Started</th><th>Not run</th><th>Within-budget successes</th><th>Infra failures</th><th>Exact $ / verified</th><th>Upper-bound $ / verified</th><th>Unknown cost trials</th></tr></thead><tbody>"""
    page += condition_rows + "</tbody></table><h2>Paired task comparisons</h2><pre>"
    page += escape(json.dumps(summary["paired_comparisons"], indent=2))
    page += "</pre><h2>Interpretation</h2><ul>" + "".join(f"<li>{escape(note)}</li>" for note in summary["interpretation"])
    page += "</ul><h2>Every trial</h2><table><thead><tr><th>Task</th><th>Condition</th><th>Repeat</th><th>Execution</th><th>Status</th><th>Category</th><th>Reported $</th><th>Cost status</th></tr></thead><tbody>"
    page += trial_rows + "</tbody></table></html>"
    if trials and all(trial.execution_kind == "reference_validation" for trial in trials):
        page = page.replace("Forge · diagnostic evaluation", "Forge · fixture qualification (no model calls)")
    html_path = destination / "report.html"
    html_path.write_text(page)
    return {"summary": summary_path, "csv": csv_path, "html": html_path}
