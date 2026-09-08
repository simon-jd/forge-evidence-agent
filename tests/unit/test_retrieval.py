from pathlib import Path

import pytest

from forge.retrieval import CodeRetriever, RetrievalError, SymbolIndex, read_snapshot


def test_literal_search_filename_lookup_and_line_ranges(tmp_path: Path) -> None:
    (tmp_path / "source.py").write_text("header\nneedle[0]\nfooter\n")
    (tmp_path / "other.py").write_text("needle0\n")
    retriever = CodeRetriever(tmp_path)
    assert retriever.list_files("SOURCE") == ["source.py"]
    matches = retriever.search("needle[0]", context_lines=1)
    assert len(matches) == 1
    assert matches[0].path == "source.py"
    assert matches[0].start_line == 1 and matches[0].end_line == 3
    assert retriever.read_file("source.py", 2, 2).excerpt == "needle[0]\n"
    assert retriever.search("does-not-exist") == []


def test_symbol_index_invalidates_by_bytes_not_file_mtime(tmp_path: Path) -> None:
    source = tmp_path / "a.py"
    source.write_text("class Client:\n    def old(self):\n        return 1\n")
    index = SymbolIndex(tmp_path, snapshot_id="commit123")
    old = index.find("Client.old")[0]
    index.find("Client")
    assert index.parse_count == 1
    source.write_text("class Client:\n    async def fresh(self):\n        return 2\n")
    assert index.find("old") == []
    fresh = index.find("Client.fresh")[0]
    assert fresh.evidence.sha256 != old.evidence.sha256
    assert index.parse_count == 2
    source.unlink()
    assert index.find("Client") == []


def test_nested_symbols_include_scope_and_unicode(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def 外部():\n    def inner():\n        pass\n    return inner\n")
    symbols = SymbolIndex(tmp_path).find("inner")
    assert symbols[0].qualified_name == "外部.inner"
    assert symbols[0].evidence.start_line == 2


def test_non_regular_binary_oversized_and_out_of_range_reads_rejected(tmp_path: Path) -> None:
    (tmp_path / "binary").write_bytes(b"a\x00b")
    (tmp_path / "large").write_text("a" * 100)
    (tmp_path / "directory").mkdir()
    for name in ["binary", "directory"]:
        with pytest.raises(RetrievalError):
            read_snapshot(tmp_path, name)
    with pytest.raises(RetrievalError, match="exceeds"):
        read_snapshot(tmp_path, "large", max_bytes=20)
    with pytest.raises(RetrievalError, match="range"):
        CodeRetriever(tmp_path).read_file("large", 3, 4)


def test_symlink_search_never_returns_external_content(tmp_path: Path) -> None:
    (tmp_path / "linked").symlink_to("/etc/passwd")
    assert CodeRetriever(tmp_path).list_files() == []
    assert CodeRetriever(tmp_path).search("root") == []
