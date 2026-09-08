from pathlib import Path

from forge.artifacts import LocalArtifactStore
from forge.context import EvidenceLedger
from forge.domain.types import RepoProfile, ToolCall
from forge.execution.docker import DockerExecutor
from forge.execution.workspace import WorkspaceManager
from forge.tools import ToolDispatcher


def repository(root: Path) -> Path:
    import subprocess
    root.mkdir()
    (root / "example.py").write_text("def add(a, b):\n    return a + b\n")
    for argv in (["git", "init", "-q"], ["git", "add", "."],
                 ["git", "-c", "user.name=Forge", "-c", "user.email=forge@example.invalid", "commit", "-qm", "Fixture"]):
        subprocess.run(argv, cwd=root, check=True, capture_output=True)
    return root


async def test_tool_evidence_round_trips_as_current_and_repeat_reads_have_distinct_origins(tmp_path: Path) -> None:
    workspace = WorkspaceManager(tmp_path / "workspaces").prepare("evidence-join", repository(tmp_path / "source"), "HEAD")
    workspace.fence(1)
    tools = ToolDispatcher(workspace, RepoProfile(name="evidence", image="not-used",
        validation_argv=["python", "-m", "unittest"]), DockerExecutor(), LocalArtifactStore(tmp_path / "artifacts"))
    ledger = EvidenceLedger(workspace.path)
    for index in range(2):
        result = await tools.execute(ToolCall(call_id=f"read-{index}", name="read_file",
            arguments={"relative_path": "example.py"}), f"operation-{index}", 1)
        assert result.status == "ok"
        ledger.add(result.evidence[0])
    assert len(ledger.anchors) == 2
    assert all(anchor.status == "current" for anchor in ledger.refresh())
    search = await tools.execute(ToolCall(call_id="search", name="search_code", arguments={"query": "return"}), "search", 1)
    ledger.add(search.evidence[0])
    assert all(anchor.status == "current" for anchor in ledger.refresh())


async def test_symbol_search_uses_real_tree_sitter_and_invalidates_after_an_edit(tmp_path: Path) -> None:
    workspace = WorkspaceManager(tmp_path / "workspaces").prepare("symbols", repository(tmp_path / "source"), "HEAD")
    workspace.fence(1)
    tools = ToolDispatcher(workspace, RepoProfile(name="symbols", image="not-used",
        validation_argv=["python", "-m", "unittest"]), DockerExecutor(), LocalArtifactStore(tmp_path / "artifacts"))
    call = ToolCall(call_id="symbol-1", name="search_code", arguments={"query": "add", "mode": "symbol"})
    first = await tools.execute(call, "symbol-1", 1)
    assert first.status == "ok" and first.evidence[0].path == "example.py"
    assert tools.symbols.parse_count == 1
    second = await tools.execute(call.model_copy(update={"call_id": "symbol-2"}), "symbol-2", 1)
    assert second.status == "ok" and tools.symbols.parse_count == 1
    workspace.files.write("example.py", b"def subtract(a, b):\n    return a - b\n")
    workspace.refresh_revision()
    third = await tools.execute(ToolCall(call_id="symbol-3", name="search_code", arguments={"query": "subtract", "mode": "symbol"}), "symbol-3", 1)
    assert third.status == "ok" and tools.symbols.parse_count == 2
    assert third.evidence[0].sha256 != first.evidence[0].sha256
