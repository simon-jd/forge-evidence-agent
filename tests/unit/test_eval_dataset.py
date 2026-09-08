import hashlib
import json
from pathlib import Path

import pytest

from forge.evals.contracts import PrivateGraderSpec
from forge.evals.dataset import Dataset, PrivateSpecStore


def test_frozen_diagnostic_split_recreates_all_30_commits(tmp_path: Path) -> None:
    dataset = Dataset(Path("evals"))
    assert len(dataset.tasks) == 30
    assert sum(task.split == "dev" for task in dataset.tasks) == 10
    assert sum(task.split == "heldout" for task in dataset.tasks) == 20
    assert len({task.family for task in dataset.tasks}) == 30
    assert sum("multi_file" in task.tags for task in dataset.tasks) >= 3
    assert any("long_output" in task.tags for task in dataset.tasks)
    for task in dataset.tasks:
        repository = dataset.materialize(task, tmp_path / task.task_id)
        assert (repository / ".git").is_dir()
        assert not list(repository.rglob("*target*test*"))
        assert not list(repository.rglob("*reference*patch*"))
        projection = task.public_projection().model_dump()
        assert set(projection) == {"task_id", "base_commit", "instruction", "constraints", "allowed_patch_paths"}
        assert "split" not in projection and "private_spec_sha256" not in projection


def test_modified_fixture_rejected_before_materialization(tmp_path: Path) -> None:
    dataset = Dataset(Path("evals"))
    task = dataset.tasks[0].model_copy(deep=True)
    task.file_hashes["limiter.py"] = "0" * 64
    with pytest.raises(ValueError, match="hash mismatch"):
        dataset.materialize(task, tmp_path / "repository")


def test_private_specs_require_permission_identity_and_frozen_hash(tmp_path: Path) -> None:
    dataset = Dataset(Path("evals"))
    task = dataset.tasks[0].model_copy(deep=True)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    spec = PrivateGraderSpec(task_id=task.task_id, target_source="private-test-sentinel",
                             target_test_count=1, reference_patch="private-fix-sentinel", author_note="test")
    raw = spec.model_dump_json().encode()
    path = private / f"{task.task_id}.json"
    path.write_bytes(raw)
    task.private_spec_sha256 = hashlib.sha256(raw).hexdigest()
    assert PrivateSpecStore(private).load(task).reference_patch == "private-fix-sentinel"
    path.write_text(json.dumps({"tampered": True}))
    with pytest.raises(ValueError, match="frozen hash"):
        PrivateSpecStore(private).load(task)
    private.chmod(0o755)
    with pytest.raises(ValueError, match="0700"):
        PrivateSpecStore(private)
