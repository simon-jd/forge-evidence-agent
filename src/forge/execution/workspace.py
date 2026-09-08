from __future__ import annotations

import asyncio
import difflib
import fcntl
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from forge.artifacts.store import atomic_write
from forge.execution.errors import NeedsReconciliation, StaleEpoch, UnsafePath
from forge.execution.safe_files import MAX_FILE_BYTES, SafeRoot, relative_parts


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "worktree"
        self.baseline_path = root / "baseline"
        self.control_path = root / "control"
        self.files = SafeRoot(self.path, shared_writes=True)

    @property
    def metadata(self) -> dict[str, Any]:
        return json.loads((self.control_path / "workspace.json").read_text())  # type: ignore[no-any-return]

    @property
    def run_id(self) -> str:
        return str(self.metadata["run_id"])

    @property
    def base_commit(self) -> str:
        return str(self.metadata["base_commit"])

    @property
    def revision(self) -> int:
        return int(self.metadata["revision"])

    def snapshot(self) -> dict[str, str]:
        result: dict[str, str] = {}
        total = 0
        for path in self.files.files():
            data = self.files.read(path)
            total += len(data)
            if total > 128 * 1024 * 1024:
                raise ValueError("Workspace exceeds 128 MiB snapshot budget")
            result[path] = hashlib.sha256(data).hexdigest()
        return result

    def refresh_revision(self) -> int:
        metadata = self.metadata
        current = self.snapshot()
        if current != metadata["snapshot"]:
            metadata["revision"] += 1
            metadata["snapshot"] = current
            atomic_write(self.control_path / "workspace.json", json.dumps(metadata).encode())
        return int(metadata["revision"])

    def get_diff(self, paths: list[str] | None = None) -> str:
        baseline = SafeRoot(self.baseline_path)
        base_files, current_files = set(baseline.files()), set(self.files.files())
        selected = base_files | current_files
        if paths is not None:
            for path in paths:
                relative_parts(path)
            selected &= set(paths)
        output: list[str] = []
        for path in sorted(selected):
            before = baseline.read(path) if path in base_files else b""
            after = self.files.read(path) if path in current_files else b""
            if before == after and (path in base_files) == (path in current_files):
                continue
            try:
                left, right = before.decode(), after.decode()
            except UnicodeDecodeError as error:
                raise ValueError(f"Binary patch export is unsupported: {path}") from error
            if "\x00" in left or "\x00" in right:
                raise ValueError(f"Binary patch export is unsupported: {path}")
            output.append(f"diff --git a/{path} b/{path}\n")
            if path not in base_files:
                output.append("new file mode 100644\n")
            elif path not in current_files:
                output.append("deleted file mode 100644\n")
            lines = difflib.unified_diff(
                left.splitlines(keepends=True), right.splitlines(keepends=True),
                fromfile=f"a/{path}" if path in base_files else "/dev/null",
                tofile=f"b/{path}" if path in current_files else "/dev/null",
            )
            for line in lines:
                output.append(line)
                if not line.endswith("\n"):
                    output.append("\n\\ No newline at end of file\n")
        return "".join(output)

    @asynccontextmanager
    async def lock(self, *, shared: bool = False) -> AsyncIterator[None]:
        """flock covers independent worker processes as well as async callers."""
        fd = os.open(self.control_path / "workspace.lock", os.O_RDWR | os.O_CREAT, 0o600)
        operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        try:
            while True:
                try:
                    fcntl.flock(fd, operation | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.02)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def fence(self, epoch: int) -> None:
        """Record new ownership even while an old command holds the write lock.

        The worker calls this after a successful database lease claim. It does
        not imply the previous command has stopped; active-operation checking
        remains mandatory before a new command or patch.
        """
        if epoch < 1:
            raise StaleEpoch("Workspace epochs begin at one")
        with (self.control_path / "epoch.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = self.control_path / "epoch.json"
            previous = int(json.loads(path.read_text())["epoch"]) if path.exists() else 0
            if epoch < previous:
                raise StaleEpoch(f"Epoch {epoch} is older than workspace epoch {previous}")
            if epoch > previous:
                atomic_write(path, json.dumps({"epoch": epoch}).encode())

    def assert_epoch(self, epoch: int) -> None:
        self.fence(epoch)

    def operation_path(self, operation_id: str) -> Path:
        identifier = hashlib.sha256(operation_id.encode()).hexdigest()
        return self.control_path / "operations" / f"{identifier}.json"

    def operation(self, operation_id: str) -> dict[str, Any] | None:
        path = self.operation_path(operation_id)
        return json.loads(path.read_text()) if path.exists() else None

    def save_operation(self, operation_id: str, value: dict[str, Any]) -> None:
        atomic_write(self.operation_path(operation_id), json.dumps(value).encode())

    def active_operation(self) -> str | None:
        path = self.control_path / "active.json"
        return json.loads(path.read_text()).get("operation_id") if path.exists() else None

    def require_idle(self, operation_id: str) -> None:
        active = self.active_operation()
        if active is not None and active != operation_id:
            raise NeedsReconciliation(f"Previous operation {active} must be reconciled first")

    def set_active(self, operation_id: str | None) -> None:
        atomic_write(self.control_path / "active.json", json.dumps({"operation_id": operation_id}).encode())


class WorkspaceManager:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def load(self, run_id: str) -> Workspace:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
            raise ValueError("Invalid run ID")
        workspace = Workspace(self.root / run_id)
        if workspace.metadata["run_id"] != run_id:
            raise ValueError("Workspace identity mismatch")
        return workspace

    def prepare(self, run_id: str, source: Path, base_commit: str) -> Workspace:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
            raise ValueError("Invalid run ID")
        source = source.resolve(strict=True)
        if not base_commit or base_commit.startswith("-"):
            raise ValueError("Expected a commit, not a Git option")
        resolved = self._git(source, ["rev-parse", "--verify", f"{base_commit}^{{commit}}"])
        commit = resolved.decode().strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
            raise ValueError("Repository did not resolve an immutable commit")
        target = self.root / run_id
        if target.exists():
            workspace = self.load(run_id)
            if workspace.base_commit != commit or workspace.metadata["source"] != str(source):
                raise ValueError("Run ID already refers to a different repository snapshot")
            return workspace
        # git archive reads committed objects, never uncommitted files or hooks.
        archive = self._git(source, ["archive", "--format=tar", commit], max_bytes=128 * 1024 * 1024)
        staging = Path(tempfile.mkdtemp(prefix=".preparing-", dir=self.root))
        for directory in ("worktree", "baseline", "control", "control/operations", "control/logs"):
            (staging / directory).mkdir(parents=True, mode=0o700)
        (staging / "worktree").chmod(0o777)
        work_files = SafeRoot(staging / "worktree", shared_writes=True)
        base_files = SafeRoot(staging / "baseline")
        count = total = 0
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            for member in tar:
                path = member.name.rstrip("/")
                relative_parts(path)
                if member.isdir():
                    continue
                if not member.isfile():
                    raise UnsafePath(f"Git archive includes a symlink or unsupported entry: {path}")
                count += 1
                total += member.size
                if count > 20_000 or total > 128 * 1024 * 1024 or member.size > MAX_FILE_BYTES:
                    raise ValueError("Repository exceeds workspace snapshot limits")
                stream = tar.extractfile(member)
                if stream is None:
                    raise ValueError("Git archive omitted file contents")
                data = stream.read(MAX_FILE_BYTES + 1)
                work_files.write(path, data, executable=bool(member.mode & 0o111))
                base_files.write(path, data, executable=bool(member.mode & 0o111))
        workspace = Workspace(staging)
        metadata = {
            "run_id": run_id, "source": str(source), "base_commit": commit,
            "revision": 0, "snapshot": workspace.snapshot(), "snapshot_kind": "committed",
        }
        atomic_write(staging / "control/workspace.json", json.dumps(metadata).encode())
        try:
            staging.rename(target)
        except FileExistsError:
            other = self.load(run_id)
            if other.base_commit != commit or other.metadata["source"] != str(source):
                raise ValueError("Concurrent preparation used different source") from None
            return other
        return Workspace(target)

    @staticmethod
    def _git(source: Path, args: list[str], max_bytes: int = 4096) -> bytes:
        # Use a file rather than capture_output so a huge archive cannot exhaust
        # memory before its size limit is checked.
        with tempfile.TemporaryFile() as stream:
            result = subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "-C", str(source), *args],
                stdout=stream, stderr=subprocess.PIPE, timeout=60, check=False,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_CONFIG_NOSYSTEM": "1",
                     "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"},
            )
            if result.returncode:
                raise ValueError(f"Git snapshot failed: {result.stderr.decode(errors='replace')[:2000]}")
            if stream.tell() > max_bytes:
                raise ValueError("Git snapshot exceeds size limit")
            stream.seek(0)
            return stream.read()
