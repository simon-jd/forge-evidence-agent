# 0005: Verify an exported patch in a clean workspace

Status: accepted.

The model's finish call only proposes a candidate. Runtime verification checks out the fixed base commit separately, applies the exported patch through the same path/hash policy, and runs the trusted public regression and acceptance commands. Regression-only results remain explicitly limited; they never become a verified completed task.

Benchmark grading is a separate process with separately stored target tests and reference patches. Its public projection includes the task, repository commit and permitted change paths. The task container mounts only its own worktree; private grader data does not enter that mount, prompt or retrieval root. The grader verifies original regression tests and independent target tests with count/skip checks.

The diagnostic dataset has 30 distinct tasks and a frozen 10/20 development/heldout split. Published task source is not a secret benchmark, and a small diagnostic heldout result does not establish broad production coding ability. Qualification proves that original and reference code exercise different outcomes; it does not measure LLM performance.

Consequences: supported patch paths and test runners are deliberately constrained. Judge-model opinions and an agent-created passing test cannot replace the independent checks. Reference-validation results, fake protocol tests and real model trials remain separate categories in every report.
