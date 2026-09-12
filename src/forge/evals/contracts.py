"""Evaluation requests expose public task data, never grader material."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import Field

from forge.domain.types import Contract, RepoProfile, RunConfig, TokenUsage


class EvalTask(Contract):
    task_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,80}$")
    split: Literal["dev", "heldout"]
    family: str
    tags: list[str] = Field(default_factory=list)
    fixture_dir: str
    base_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    file_hashes: dict[str, str]
    instruction: str
    constraints: list[str] = Field(default_factory=list)
    allowed_patch_paths: list[str]
    private_spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    def public_projection(self) -> PublicTask:
        return PublicTask(
            task_id=self.task_id, base_commit=self.base_commit, instruction=self.instruction,
            constraints=self.constraints, allowed_patch_paths=self.allowed_patch_paths,
        )


class PublicTask(Contract):
    task_id: str
    base_commit: str
    instruction: str
    constraints: list[str]
    allowed_patch_paths: list[str]


class PrivateGraderSpec(Contract):
    task_id: str
    target_source: str
    target_test_count: int = Field(gt=0)
    reference_patch: str
    author_note: str


class ExperimentCondition(Contract):
    name: Literal["B0", "B1", "B2", "B3", "strong_only", "B2_guard_off"]
    config: RunConfig
    evidence_guard: bool = True


class PublicTrialRequest(Contract):
    trial_id: str
    task: PublicTask
    repository: str
    profile: RepoProfile
    condition: ExperimentCondition
    repeat: int = Field(ge=0)
    seed: int


FailureCategory = Literal[
    "none", "invalid_patch", "target_failure", "regression_failure", "protected_path",
    "budget_exhausted", "permission_denied", "stalled", "constraint_lost",
    "wrong_location", "infrastructure", "not_run", "agent_failure",
]


class AgentTrialOutcome(Contract):
    """The callback's completion claim does not decide the evaluator result."""

    run_id: str | None = None
    patch: str = ""
    execution_kind: Literal["live_model", "fake_control_flow", "reference_validation"]
    status: Literal["candidate", "failed", "infrastructure_failure", "not_run"]
    failure_category: FailureCategory = "none"
    message: str = ""
    cost_usd: Decimal | None = Field(default=None, ge=0)
    reserved_unknown_usd: Decimal = Field(default=Decimal(0), ge=0)
    cost_status: Literal["exact", "upper_bound", "estimated", "unknown"] = "unknown"
    usage: TokenUsage = Field(default_factory=TokenUsage)
    tool_calls: int = Field(default=0, ge=0)
    compactions: int = Field(default=0, ge=0)
    invalid_tool_calls: int = Field(default=0, ge=0)
    stale_current_leaks: int | None = Field(default=None, ge=0)
    constraint_losses: int | None = Field(default=None, ge=0)
    phase_seconds: dict[str, float] = Field(default_factory=dict)
    artifact_refs: list[str] = Field(default_factory=list)


class GradeResult(Contract):
    status: Literal["verified", "failed", "infrastructure_failure"]
    category: FailureCategory
    target_passed: bool = False
    regression_passed: bool = False
    target_tests_run: int = Field(default=0, ge=0)
    regression_tests_run: int = Field(default=0, ge=0)
    sandbox: str
    image_id: str | None = None
    grader_workspace: str | None = None
    artifact_refs: list[str] = Field(default_factory=list)
    message: str = ""
    duration_seconds: float = Field(default=0, ge=0)
    snapshot_before: str | None = None
    snapshot_after: str | None = None
    tampered_paths: list[str] = Field(default_factory=list)


class EvalTrial(Contract):
    trial_id: str
    task_id: str
    family: str
    split: Literal["dev", "heldout"]
    condition: str
    repeat: int
    seed: int
    base_commit: str
    config_hash: str
    execution_kind: Literal["live_model", "fake_control_flow", "reference_validation"]
    status: Literal["verified", "failed", "infrastructure_failure", "not_run"]
    failure_category: FailureCategory
    success_within_budget: bool
    cost_usd: Decimal | None
    reserved_unknown_usd: Decimal
    cost_status: Literal["exact", "upper_bound", "estimated", "unknown"]
    usage: TokenUsage
    latency_seconds: float
    phase_seconds: dict[str, float]
    tool_calls: int
    compactions: int
    invalid_tool_calls: int
    stale_current_leaks: int | None
    constraint_losses: int | None
    outcome: AgentTrialOutcome
    grade: GradeResult | None = None
