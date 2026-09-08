from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from forge.artifacts import ArtifactError, LocalArtifactStore
from forge.domain.types import RepoProfile, ToolCall
from forge.execution.docker import DockerExecutor
from forge.execution.errors import NeedsReconciliation, SandboxUnavailable, StaleEpoch, UnsafePath
from forge.execution.workspace import Workspace, WorkspaceManager
from forge.tools import ToolDispatcher


def git(path: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q")
    git(source, "config", "user.email", "fixture@example.invalid")
    git(source, "config", "user.name", "Forge fixture")
    (source / "a.py").write_text("def answer():\n    return 41\n")
    (source / "b.py").write_text("VALUE = 1\n")
    (source / "tests").mkdir()
    (source / "tests/test_a.py").write_text("def test_truth():\n    assert True\n")
    git(source, "add", ".")
    git(source, "commit", "-qm", "Fixture baseline")
    return source


@pytest.fixture
def workspace(tmp_path: Path, repository: Path) -> Workspace:
    return WorkspaceManager(tmp_path / "workspaces").prepare("run-one", repository, "HEAD")


@pytest.fixture
def dispatcher(workspace: Workspace, tmp_path: Path) -> ToolDispatcher:
    profile = RepoProfile(name="unit", image="unavailable:test", validation_argv=["python", "-m", "pytest"],
                          protected_paths=[".git/**", ".forge/**", "tests/**"])
    return ToolDispatcher(workspace, profile, DockerExecutor(), LocalArtifactStore(tmp_path / "artifacts"))


def patch_call(workspace: Workspace, *, answer: int = 42, call_id: str = "patch") -> ToolCall:
    return ToolCall(call_id=call_id, name="apply_patch", arguments={
        "patch": f"--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n def answer():\n-    return 41\n+    return {answer}\n",
        "expected_file_hashes": {"a.py": workspace.files.digest("a.py")},
    })


def test_snapshot_is_committed_independent_and_private(repository: Path, tmp_path: Path) -> None:
    (repository / "a.py").write_text("uncommitted changes\n")
    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.prepare("one", repository, "HEAD")
    second = manager.prepare("two", repository, "HEAD")
    assert workspace.files.read("a.py") == b"def answer():\n    return 41\n"
    workspace.files.write("a.py", b"changed\n")
    assert second.files.read("a.py") != workspace.files.read("a.py")
    assert (repository / "a.py").read_text() == "uncommitted changes\n"
    assert workspace.root.stat().st_mode & 0o777 == 0o700
    assert workspace.control_path.stat().st_mode & 0o777 == 0o700
    assert workspace.baseline_path.stat().st_mode & 0o777 == 0o700
    assert not (workspace.path / ".git").exists()
    assert manager.prepare("one", repository, "HEAD").base_commit == workspace.base_commit
    with pytest.raises(ValueError, match="Invalid run"):
        manager.prepare("../escape", repository, "HEAD")


def test_git_archive_rejects_repository_symlinks(repository: Path, tmp_path: Path) -> None:
    (repository / "escape").symlink_to("/etc/passwd")
    git(repository, "add", "escape")
    git(repository, "commit", "-qm", "Unsupported link")
    with pytest.raises(UnsafePath, match="symlink"):
        WorkspaceManager(tmp_path / "workspaces").prepare("link", repository, "HEAD")


@pytest.mark.parametrize("path", ["../outside", "/etc/passwd", "a/../../outside", "a//b", "./a.py", ".git/config", "a\\b", "a\nb"])
async def test_untrusted_paths_are_denied(dispatcher: ToolDispatcher, path: str) -> None:
    result = await dispatcher.execute(ToolCall(call_id="read", name="read_file", arguments={"relative_path": path}), "read", 1)
    assert result.status == "denied"


async def test_symlink_read_write_and_fifo_are_rejected(dispatcher: ToolDispatcher, tmp_path: Path) -> None:
    outside = tmp_path / "secret"
    outside.write_text("not public")
    (dispatcher.workspace.path / "link").symlink_to(outside)
    result = await dispatcher.execute(ToolCall(call_id="read", name="read_file", arguments={"relative_path": "link"}), "read", 1)
    assert result.status == "denied"
    call = ToolCall(call_id="write", name="apply_patch", arguments={
        "patch": "--- a/link\n+++ b/link\n@@ -1 +1 @@\n-not public\n+changed\n",
        "expected_file_hashes": {"link": hashlib.sha256(b"not public").hexdigest()},
    })
    assert (await dispatcher.execute(call, "write", 1)).status == "denied"
    assert outside.read_text() == "not public"
    os.mkfifo(dispatcher.workspace.path / "pipe")
    fifo = await dispatcher.execute(ToolCall(call_id="fifo", name="read_file", arguments={"relative_path": "pipe"}), "fifo", 1)
    assert fifo.status == "denied"


async def test_hash_provenance_search_and_complete_artifact(dispatcher: ToolDispatcher) -> None:
    read = await dispatcher.execute(ToolCall(call_id="read", name="read_file", arguments={"relative_path": "a.py", "start_line": 2}), "read", 1)
    assert read.status == "ok"
    assert read.evidence[0].sha256 == dispatcher.workspace.files.digest("a.py")
    assert read.evidence[0].start_line == 2
    assert read.evidence[0].origin_call_id == "read"
    assert "2:     return 41" in read.summary
    assert dispatcher.artifacts.read_text(read.artifact_refs[0]).startswith("def answer")
    search = await dispatcher.execute(ToolCall(call_id="search", name="search_code", arguments={"query": "return", "path_glob": "**/*.py"}), "search", 1)
    assert search.status == "ok"
    assert search.evidence[0].path == "a.py"
    assert search.evidence[0].sha256 == read.evidence[0].sha256
    assert json.loads(search.summary)["matches"][0]["line"] == 2


async def test_read_truncates_but_retains_full_source(dispatcher: ToolDispatcher) -> None:
    content = "x" * 30_000 + "\n" + "second\n" * 1000
    dispatcher.workspace.files.write("large.txt", content.encode())
    result = await dispatcher.execute(ToolCall(call_id="read", name="read_file", arguments={"relative_path": "large.txt"}), "large", 1)
    assert result.truncated
    assert len(result.summary) < 25_000
    assert dispatcher.artifacts.read_text(result.artifact_refs[0]) == content


async def test_retained_logs_can_be_reread_without_cross_run_artifact_access(dispatcher: ToolDispatcher) -> None:
    ref = dispatcher.artifacts.put_text(dispatcher.workspace.run_id, "command_stdout", "first\nsecond\n")
    read = ToolCall(call_id="artifact", name="read_file", arguments={"artifact_ref": ref, "start_line": 2})
    result = await dispatcher.execute(read, "artifact", 1)
    assert result.status == "ok" and "2: second" in result.summary
    assert not result.evidence  # Historical output is never claimed to be current file content.
    private = dispatcher.artifacts.put_text("another-run", "private", "hidden")
    read.arguments["artifact_ref"] = private
    assert (await dispatcher.execute(read, "private", 1)).status == "denied"


async def test_patch_hash_conflict_and_protected_paths_are_preflighted(dispatcher: ToolDispatcher) -> None:
    call = patch_call(dispatcher.workspace)
    call.arguments["expected_file_hashes"]["a.py"] = "0" * 64
    result = await dispatcher.execute(call, "bad-hash", 1)
    assert result.status == "error" and "hash conflict" in result.summary
    assert dispatcher.workspace.revision == 0
    assert "41" in dispatcher.workspace.files.read("a.py").decode()
    protected = ToolCall(call_id="protected", name="apply_patch", arguments={
        "patch": "--- a/tests/test_a.py\n+++ b/tests/test_a.py\n@@ -1,2 +1,2 @@\n def test_truth():\n-    assert True\n+    pass\n",
        "expected_file_hashes": {"tests/test_a.py": dispatcher.workspace.files.digest("tests/test_a.py")},
    })
    assert (await dispatcher.execute(protected, "protected", 1)).status == "denied"


async def test_patch_replay_reuses_result_and_changed_arguments_are_rejected(dispatcher: ToolDispatcher) -> None:
    call = patch_call(dispatcher.workspace)
    result = await dispatcher.execute(call, "same-operation", 1)
    assert result.status == "ok" and result.workspace_revision == 1
    replay = await dispatcher.execute(call, "same-operation", 1)
    assert replay == result
    call.arguments["patch"] += "\n"
    with pytest.raises(NeedsReconciliation, match="changed tool arguments"):
        await dispatcher.execute(call, "same-operation", 1)


async def test_all_patch_hashes_are_checked_before_first_write(dispatcher: ToolDispatcher) -> None:
    call = patch_call(dispatcher.workspace)
    call.arguments["patch"] += "--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n"
    call.arguments["expected_file_hashes"]["b.py"] = "0" * 64
    before = dispatcher.workspace.snapshot()
    assert (await dispatcher.execute(call, "preflight", 1)).status == "error"
    assert dispatcher.workspace.snapshot() == before


async def test_crash_after_patch_write_is_reconciled_by_after_hashes(dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch) -> None:
    call = patch_call(dispatcher.workspace)
    original = dispatcher._finish_patch

    def crash(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated worker exit")

    monkeypatch.setattr(dispatcher, "_finish_patch", crash)
    with pytest.raises(RuntimeError, match="worker exit"):
        await dispatcher.execute(call, "crash", 1)
    assert "42" in dispatcher.workspace.files.read("a.py").decode()
    monkeypatch.setattr(dispatcher, "_finish_patch", original)
    recovered = await dispatcher.execute(call, "crash", 2)
    assert recovered.status == "ok"
    assert json.loads(recovered.summary)["recovered"]
    assert dispatcher.workspace.revision == 1
    assert dispatcher.workspace.active_operation() is None


async def test_partial_patch_is_never_blindly_replayed(dispatcher: ToolDispatcher, monkeypatch: pytest.MonkeyPatch) -> None:
    call = patch_call(dispatcher.workspace)
    call.arguments["patch"] += "--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n"
    call.arguments["expected_file_hashes"]["b.py"] = dispatcher.workspace.files.digest("b.py")
    original = dispatcher.workspace.files.write

    def write(path: str, data: bytes, **kwargs: Any) -> None:
        if path == "b.py":
            raise OSError("simulated disk failure")
        original(path, data, **kwargs)

    monkeypatch.setattr(dispatcher.workspace.files, "write", write)
    with pytest.raises(NeedsReconciliation, match="partially applied"):
        await dispatcher.execute(call, "partial", 1)
    monkeypatch.setattr(dispatcher.workspace.files, "write", original)
    with pytest.raises(NeedsReconciliation, match="refusing replay"):
        await dispatcher.execute(call, "partial", 2)
    assert "42" in dispatcher.workspace.files.read("a.py").decode()
    assert dispatcher.workspace.files.read("b.py") == b"VALUE = 1\n"


async def test_write_lock_serializes_competing_patches(dispatcher: ToolDispatcher) -> None:
    one, two = patch_call(dispatcher.workspace, answer=42), patch_call(dispatcher.workspace, answer=43, call_id="second")
    results = await asyncio.gather(dispatcher.execute(one, "one", 1), dispatcher.execute(two, "two", 1))
    assert sorted(result.status for result in results) == ["error", "ok"]
    assert dispatcher.workspace.revision == 1


async def test_new_epoch_fences_old_worker_and_active_job_requires_reconciliation(dispatcher: ToolDispatcher) -> None:
    dispatcher.workspace.fence(2)
    with pytest.raises(StaleEpoch):
        await dispatcher.execute(patch_call(dispatcher.workspace), "old", 1)
    dispatcher.workspace.set_active("missing-container")
    with pytest.raises(NeedsReconciliation, match="reconciled first"):
        await dispatcher.execute(patch_call(dispatcher.workspace), "new", 2)


async def test_add_delete_and_no_newline_diff_applies_to_clean_checkout(dispatcher: ToolDispatcher, repository: Path, tmp_path: Path) -> None:
    call = ToolCall(call_id="multi", name="apply_patch", arguments={
        "patch": "--- /dev/null\n+++ b/new/note.txt\n@@ -0,0 +1 @@\n+hello\n\\ No newline at end of file\n"
                 "--- a/b.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-VALUE = 1\n",
        "expected_file_hashes": {"new/note.txt": None, "b.py": dispatcher.workspace.files.digest("b.py")},
    })
    result = await dispatcher.execute(call, "multi", 1)
    assert result.status == "ok", result.summary
    diff = dispatcher.workspace.get_diff()
    clean = tmp_path / "clean"
    await asyncio.to_thread(subprocess.run, ["git", "clone", "--quiet", "--no-hardlinks", str(repository), str(clean)], check=True)
    await asyncio.to_thread(subprocess.run, ["git", "-C", str(clean), "apply", "-"], input=diff.encode(), check=True)
    assert (clean / "new/note.txt").read_bytes() == b"hello"
    assert not (clean / "b.py").exists()
    assert "new file mode" in diff and "deleted file mode" in diff


def test_safe_access_rejects_directory_symlinks_and_hardlinks(workspace: Workspace, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("secret")
    (workspace.path / "linked-dir").symlink_to(outside, target_is_directory=True)
    with pytest.raises(UnsafePath):
        workspace.files.write("linked-dir/secret", b"modified")
    os.link(outside / "secret", workspace.path / "hardlink")
    with pytest.raises(UnsafePath):
        workspace.files.read("hardlink")
    assert (outside / "secret").read_text() == "secret"


def test_artifacts_have_owner_digest_and_reject_path_refs(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    ref = store.put_text("run-one", "log", "diagnostic")
    assert store.read_text(ref) == "diagnostic"
    assert store.metadata(ref)["run_id"] == "run-one"
    with pytest.raises(ArtifactError):
        store.read_text("../secret")
    store._path(ref, "data").write_text("tampered")
    with pytest.raises(ArtifactError, match="integrity"):
        store.read_text(ref)


def test_executor_refuses_rootful_without_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        def info(self) -> dict[str, Any]:
            return {"SecurityOptions": ["name=seccomp"], "MemoryLimit": True, "PidsLimit": True, "CpuCfsQuota": True}

        def close(self) -> None:
            pass

    monkeypatch.setattr("forge.execution.docker.DockerClient.from_env", lambda **kwargs: FakeClient())
    with pytest.raises(SandboxUnavailable, match="rootful"):
        DockerExecutor()._client()


def test_executor_refuses_unenforced_resource_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        def info(self) -> dict[str, Any]:
            return {"SecurityOptions": ["name=rootless"], "MemoryLimit": False, "PidsLimit": True, "CpuCfsQuota": False}

        def close(self) -> None:
            pass

    monkeypatch.setattr("forge.execution.docker.DockerClient.from_env", lambda **kwargs: FakeClient())
    with pytest.raises(SandboxUnavailable, match="cannot enforce"):
        DockerExecutor()._client()
