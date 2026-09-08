from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

import tree_sitter_python
from tree_sitter import Language, Node, Parser

from forge.domain.types import EvidenceAnchor
from forge.retrieval.code import CodeRetriever, FileSnapshot, RetrievalError, read_snapshot


@dataclass(frozen=True)
class Symbol:
    name: str
    qualified_name: str
    kind: str
    evidence: EvidenceAnchor


class SymbolIndex:
    """An incremental Python syntax index; cache identity includes parser version.

    This is deliberately a symbol-definition index, not a type-aware call graph.
    Calls remain discoverable through literal code search.
    """

    def __init__(self, workspace: Path, *, snapshot_id: str = "working-tree") -> None:
        self.workspace = Path(workspace)
        self.snapshot_id = snapshot_id
        self.parser_version = f"tree-sitter/{version('tree-sitter')}:python/{version('tree-sitter-python')}"
        self.parser = Parser(Language(tree_sitter_python.language()))
        self._cache: dict[str, tuple[tuple[str, str, str], list[Symbol]]] = {}
        self.parse_count = 0

    def index_file(self, path: str) -> list[Symbol]:
        if not path.endswith(".py"):
            raise RetrievalError("the initial symbol index supports Python files")
        snapshot = read_snapshot(self.workspace, path)
        key = (self.snapshot_id, snapshot.sha256, self.parser_version)
        previous = self._cache.get(path)
        if previous and previous[0] == key:
            return list(previous[1])
        self.parse_count += 1
        root = self.parser.parse(snapshot.text.encode("utf-8")).root_node
        symbols: list[Symbol] = []
        self._visit(root, snapshot, (), symbols)
        self._cache[path] = (key, symbols)
        return list(symbols)

    def _visit(
        self, node: Node, snapshot: FileSnapshot, parents: tuple[str, ...], symbols: list[Symbol],
    ) -> None:
        if node.type in {"class_definition", "function_definition"}:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                name = (name_node.text or b"").decode("utf-8")
                symbols.append(Symbol(
                    name=name, qualified_name=".".join((*parents, name)),
                    kind="class" if node.type == "class_definition" else "function",
                    evidence=snapshot.anchor(node.start_point.row + 1, node.end_point.row + 1),
                ))
                parents = (*parents, name)
        for child in node.named_children:
            self._visit(child, snapshot, parents, symbols)

    def find(self, name: str, *, limit: int = 20) -> list[Symbol]:
        if not name or not 1 <= limit <= 100:
            raise RetrievalError("name and a limit between 1 and 100 are required")
        paths = [path for path in CodeRetriever(self.workspace).list_files() if path.endswith(".py")]
        self._cache = {path: entry for path, entry in self._cache.items() if path in paths}
        results: list[Symbol] = []
        for path in paths:
            for symbol in self.index_file(path):
                if name.casefold() in symbol.qualified_name.casefold():
                    results.append(symbol)
                    if len(results) >= limit:
                        return results
        return results
