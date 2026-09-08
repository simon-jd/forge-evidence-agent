from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path, PurePosixPath

from forge.evals.contracts import EvalTask, PrivateGraderSpec

FIXTURE_ENV = {
    "GIT_AUTHOR_NAME": "Forge Diagnostic Fixtures",
    "GIT_AUTHOR_EMAIL": "fixtures@forge.invalid",
    "GIT_COMMITTER_NAME": "Forge Diagnostic Fixtures",
    "GIT_COMMITTER_EMAIL": "fixtures@forge.invalid",
    "GIT_AUTHOR_DATE": "2026-09-07T00:00:00+0000",
    "GIT_COMMITTER_DATE": "2026-09-07T00:00:00+0000",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
}


def _relative(value: str) -> Path:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or ".git" in path.parts or "\\" in value:
        raise ValueError("fixture paths must be normal relative paths")
    return Path(*path.parts)


def fixture_git(repository: Path, arguments: list[str]) -> str:
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(repository), *arguments],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **FIXTURE_ENV},
        capture_output=True, text=True, timeout=30, check=True,
    )
    return result.stdout.strip()


class Dataset:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        document = json.loads((self.root / "manifest.json").read_text())
        self.version = document["version"]
        self.tasks = [EvalTask.model_validate(item) for item in document["tasks"]]
        ids = [task.task_id for task in self.tasks]
        if len(set(ids)) != len(ids):
            raise ValueError("dataset task IDs must be unique")
        families: dict[str, str] = {}
        for task in self.tasks:
            if task.family in families and families[task.family] != task.split:
                raise ValueError("a defect family must not span dev and heldout")
            families[task.family] = task.split
            _relative(task.fixture_dir)
            for path in task.file_hashes:
                _relative(path)

    def materialize(self, task: EvalTask, destination: Path) -> Path:
        """Recreate a fixture commit from frozen public source without git history answers."""
        source = self.root / _relative(task.fixture_dir)
        if not source.resolve().is_relative_to(self.root):
            raise ValueError("fixture directory escaped the dataset")
        if destination.exists():
            raise ValueError("fixture destination must be new")
        present = {path.relative_to(source).as_posix() for path in source.rglob("*") if path.is_file()}
        if present != set(task.file_hashes):
            raise ValueError("fixture files differ from the frozen manifest")
        for relative, expected in task.file_hashes.items():
            path = source / _relative(relative)
            if path.is_symlink() or not path.resolve().is_relative_to(source.resolve()):
                raise ValueError("fixture symlinks are not allowed")
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError(f"fixture content hash mismatch: {task.task_id}/{relative}")
        shutil.copytree(source, destination)
        fixture_git(destination, ["init", "--quiet", "--initial-branch=main", "--object-format=sha1"])
        fixture_git(destination, ["add", "--all"])
        fixture_git(destination, ["commit", "--quiet", "-m", f"Frozen diagnostic: {task.task_id}"])
        actual = fixture_git(destination, ["rev-parse", "HEAD"])
        if actual != task.base_commit:
            raise ValueError(f"fixture commit mismatch: expected {task.base_commit}, got {actual}")
        return destination


class PrivateSpecStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ValueError("private grader directory is unavailable")
        if self.root.stat().st_mode & 0o077:
            raise ValueError("private grader directory must use mode 0700")

    def load(self, task: EvalTask) -> PrivateGraderSpec:
        path = self.root / f"{task.task_id}.json"
        if path.is_symlink():
            raise ValueError("private grader spec cannot be a symlink")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != task.private_spec_sha256:
            raise ValueError("private grader spec differs from the frozen hash")
        spec = PrivateGraderSpec.model_validate_json(raw)
        if spec.task_id != task.task_id:
            raise ValueError("private spec task ID mismatch")
        return spec
