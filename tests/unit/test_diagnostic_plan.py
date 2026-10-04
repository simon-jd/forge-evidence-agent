from collections import Counter
from types import SimpleNamespace

import pytest

from forge.evals.diagnostic_plan import common_first_seed, imbalanced_plan


def trials():
    return [SimpleNamespace(trial_id=f"diagnostic-{i}", status="verified", **slot)
            for i, slot in enumerate(imbalanced_plan([f"T{i}" for i in range(1, 13)]))]


def test_imbalanced_allocation_preserves_common_sixty_without_calling_models():
    rows = trials()
    assert len(rows) == 75
    assert sorted(Counter(row.condition for row in rows).values()) == [10, 11, 12, 13, 14, 15]
    assert len({(row.condition, row.task_id, row.seed) for row in rows}) == 75
    comparison = common_first_seed(rows)
    assert comparison["trial_count"] == 60
    assert set(comparison["allocation"].values()) == {10}
    assert set(comparison["tasks"]) == {f"T{i}" for i in range(1, 11)}


def test_extra_repeats_do_not_change_common_first_seed():
    rows = trials()
    original = common_first_seed(rows)
    for row in rows:
        if row.repeat:
            row.status = "failed"
    assert common_first_seed(rows) == original


def test_duplicate_and_mismatched_seed_are_not_counted_as_independent_trials():
    rows = trials()
    with pytest.raises(ValueError, match="Duplicate"):
        common_first_seed(rows + [rows[0]])
    rows[0].seed += 100
    assert common_first_seed(rows)["trial_count"] == 54


def test_plan_rejects_duplicate_task_ids():
    with pytest.raises(ValueError):
        imbalanced_plan(["T1"] * 12)


def test_not_run_placeholder_is_not_a_common_observation():
    rows = trials()
    rows[0].status = "not_run"
    comparison = common_first_seed(rows)
    assert comparison["trial_count"] == 54
    assert "T1" not in comparison["tasks"]
    assert set(comparison["allocation"].values()) == {9}
