import hashlib
from decimal import Decimal
from pathlib import Path

import pytest

from forge.domain.types import ModelSpec, RepoProfile, RunConfig, TokenUsage
from forge.evals.contracts import (
    AgentTrialOutcome,
    GradeResult,
    PrivateGraderSpec,
    PublicTrialRequest,
)
from forge.evals.dataset import Dataset, PrivateSpecStore
from forge.evals.harness import EvalHarness, standard_conditions
from forge.evals.report import summarize, write_report


class StubGrader:
    async def grade(self, task, repository, patch, spec, profile, trial_id, *, allow_empty=False):
        return GradeResult(status="verified" if patch == "candidate" else "failed",
                           category="none" if patch == "candidate" else "target_failure",
                           target_passed=patch == "candidate", regression_passed=True,
                           target_tests_run=1, regression_tests_run=1, sandbox="fake_contract_only")


def harness(tmp_path: Path, count: int = 2) -> EvalHarness:
    dataset = Dataset(Path("evals"))
    dataset.tasks = [task.model_copy(deep=True) for task in dataset.tasks[:count]]
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    for task in dataset.tasks:
        spec = PrivateGraderSpec(task_id=task.task_id, target_source="HIDDEN_TARGET_SENTINEL",
                                 target_test_count=1, reference_patch="HIDDEN_PATCH_SENTINEL", author_note="contract")
        raw = spec.model_dump_json().encode()
        (private / f"{task.task_id}.json").write_bytes(raw)
        task.private_spec_sha256 = hashlib.sha256(raw).hexdigest()
    return EvalHarness(dataset, PrivateSpecStore(private), StubGrader(), tmp_path / "repos",
                       RepoProfile(name="contract", image="fixture", validation_argv=["true"]))


def fake_config() -> RunConfig:
    return RunConfig(models=[
        ModelSpec(provider="fake", model_id="cheap", input_usd_per_million=Decimal(0), output_usd_per_million=Decimal(0)),
        ModelSpec(provider="fake", model_id="strong", input_usd_per_million=Decimal(0), output_usd_per_million=Decimal(0)),
    ])


@pytest.mark.asyncio
async def test_matrix_three_repeats_public_projection_and_raw_report(tmp_path: Path) -> None:
    evaluation = harness(tmp_path)
    requests = []

    async def callback(request: PublicTrialRequest) -> AgentTrialOutcome:
        requests.append(request)
        raw = request.model_dump_json()
        assert "HIDDEN_" not in raw and "private_spec" not in raw
        assert ".forge_grader" not in raw
        assert not list(Path(request.repository).rglob("*target*test*"))  # noqa: ASYNC240
        return AgentTrialOutcome(execution_kind="fake_control_flow", status="candidate", patch="candidate",
                                 cost_usd=Decimal("0"), cost_status="exact",
                                 usage=TokenUsage(input_tokens=1, output_tokens=1))

    conditions = standard_conditions(fake_config())
    trials = await evaluation.run(evaluation.dataset.tasks, conditions, callback, tmp_path / "trials.jsonl")
    assert len(trials) == len(requests) == 2 * 6 * 3
    assert all(trial.success_within_budget for trial in trials)
    assert len({trial.trial_id for trial in trials}) == len(trials)
    assert len((tmp_path / "trials.jsonl").read_text().splitlines()) == len(trials)
    assert conditions[0].config.context.strategy == "recent"
    assert conditions[3].config.max_escalations == 1
    assert conditions[4].config.initial_model == 1
    assert conditions[5].evidence_guard is False
    reports = write_report(trials, tmp_path / "report")
    assert all(path.is_file() for path in reports.values())
    summary = summarize(trials)
    assert summary["conditions"]["B0"]["tasks"] == 2
    assert summary["conditions"]["B0"]["started_trials"] == 6
    assert summary["paired_comparisons"][0]["paired_tasks"] == 2


@pytest.mark.asyncio
async def test_live_without_authorized_total_is_not_run_and_never_calls_model(tmp_path: Path) -> None:
    evaluation = harness(tmp_path, 1)
    config = fake_config()
    config.models[0].provider = "openai"
    called = False

    async def callback(request: PublicTrialRequest) -> AgentTrialOutcome:
        nonlocal called
        called = True
        raise AssertionError("must not run without authorization")

    trials = await evaluation.run(evaluation.dataset.tasks, standard_conditions(config)[:1], callback,
                                   tmp_path / "trials.jsonl")
    assert not called
    assert all(trial.status == "not_run" for trial in trials)
    assert summarize(trials)["conditions"]["B0"]["success_rate_all_started"] is None


@pytest.mark.asyncio
async def test_unknown_usage_holds_whole_trial_budget_and_stops_later_trials(tmp_path: Path) -> None:
    evaluation = harness(tmp_path, 1)
    config = fake_config()
    config.models[0].provider = "openai"
    calls = 0

    async def callback(request: PublicTrialRequest) -> AgentTrialOutcome:
        nonlocal calls
        calls += 1
        return AgentTrialOutcome(execution_kind="live_model", status="infrastructure_failure",
                                 failure_category="infrastructure", cost_status="unknown")

    trials = await evaluation.run(evaluation.dataset.tasks, standard_conditions(config)[:1], callback,
                                   tmp_path / "trials.jsonl", authorized_total_cost_usd=Decimal("3"))
    assert calls == 1
    assert sum(trial.status == "not_run" for trial in trials) == 2
    assert summarize(trials)["conditions"]["B0"]["unknown_cost_trials"] == 1


@pytest.mark.asyncio
async def test_failure_cost_included_and_unknown_cost_not_reported_as_zero(tmp_path: Path) -> None:
    evaluation = harness(tmp_path, 1)
    outcomes = iter([
        AgentTrialOutcome(execution_kind="fake_control_flow", status="candidate", patch="candidate",
                          cost_usd=Decimal(".2"), cost_status="exact"),
        AgentTrialOutcome(execution_kind="fake_control_flow", status="failed", failure_category="stalled",
                          cost_usd=Decimal(".8"), cost_status="exact"),
        AgentTrialOutcome(execution_kind="fake_control_flow", status="failed", cost_status="unknown"),
    ])

    async def callback(request: PublicTrialRequest) -> AgentTrialOutcome:
        return next(outcomes)

    trials = await evaluation.run(evaluation.dataset.tasks, standard_conditions(fake_config())[:1], callback,
                                   tmp_path / "trials.jsonl")
    known = summarize(trials[:2])["conditions"]["B0"]
    assert known["cost_per_verified_usd"] == "1.0"
    uncertain = summarize(trials)["conditions"]["B0"]
    assert uncertain["cost_per_verified_usd"] is None
    assert uncertain["unknown_cost_trials"] == 1
    assert uncertain["reported_cost_sum_usd"] == "1.0"
