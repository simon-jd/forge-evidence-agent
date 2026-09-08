from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from forge.domain.types import EvidenceAnchor, canonical_hash


class RetrievalError(ValueError):
    """A repository read is unsafe, too large, or otherwise unavailable."""


@dataclass(frozen=True)
class FileSnapshot:
    path: str
    sha256: str
    text: str

    def anchor(
        self, start_line: int = 1, end_line: int | None = None,
        origin_call_id: str | None = None,
    ) -> EvidenceAnchor:
        lines = self.text.splitlines(keepends=True)
        # Empty files have one empty addressable line.
        length = max(1, len(lines))
        end = length if end_line is None else min(end_line, length)
        if start_line < 1 or start_line > length or end < start_line:
            raise RetrievalError("line range must address an existing, ordered range")
        identity = [self.path, self.sha256, start_line, end, origin_call_id]
        return EvidenceAnchor(
            evidence_id=canonical_hash(identity), path=self.path, sha256=self.sha256,
            start_line=start_line, end_line=end,
            excerpt="".join(lines[start_line - 1:end]), origin_call_id=origin_call_id,
        )


def read_snapshot(workspace: Path, path: str, max_bytes: int = 2 * 1024 * 1024) -> FileSnapshot:
    """Open every path component with O_NOFOLLOW; never follow repository symlinks.

    Directory descriptors also prevent a checked parent from being replaced by a
    symlink before the final file open. Callers must serialize workspace writes
    with reads when they need a multi-file snapshot.
    """
    candidate = PurePosixPath(path)
    if (not path or candidate.is_absolute() or ".." in candidate.parts
            or any(part in {".git", ".forge"} for part in candidate.parts)
            or "\\" in path or "\x00" in path or not candidate.parts):
        raise RetrievalError("path must be an unprotected workspace-relative file")
    descriptors: list[int] = []
    try:
        descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(descriptor)
        for component in candidate.parts[:-1]:
            descriptor = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor,
            )
            descriptors.append(descriptor)
        descriptor = os.open(
            candidate.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=descriptor,
        )
        descriptors.append(descriptor)
        import stat

        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RetrievalError("only regular files can become code evidence")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(65536, max_bytes + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > max_bytes:
                raise RetrievalError(f"file exceeds {max_bytes} byte read limit")
        data = b"".join(chunks)
        if b"\x00" in data:
            raise RetrievalError("binary files cannot become text evidence")
        return FileSnapshot(str(candidate), hashlib.sha256(data).hexdigest(), data.decode("utf-8"))
    except (OSError, UnicodeError) as exc:
        raise RetrievalError(f"cannot read {path}: {exc.__class__.__name__}") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


class CodeRetriever:
    def __init__(self, workspace: Path, *, max_files: int = 10_000) -> None:
        self.workspace = Path(workspace).absolute()
        self.max_files = max_files

    def read_file(
        self, path: str, start_line: int = 1, end_line: int | None = None,
        origin_call_id: str | None = None,
    ) -> EvidenceAnchor:
        return read_snapshot(self.workspace, path).anchor(start_line, end_line, origin_call_id)

    def list_files(self, query: str = "") -> list[str]:
        found: list[str] = []
        visited = 0
        for directory, directories, names in os.walk(self.workspace, followlinks=False):
            directories[:] = sorted(
                name for name in directories if name not in {
                    ".git", ".forge", ".venv", "node_modules", "__pycache__",
                } and not (Path(directory) / name).is_symlink()
            )
            for name in sorted(names):
                visited += 1
                if visited > self.max_files:
                    raise RetrievalError(f"repository exceeds {self.max_files} file listing limit")
                path = Path(directory) / name
                if path.is_symlink():
                    continue
                relative = path.relative_to(self.workspace).as_posix()
                if query.casefold() in relative.casefold():
                    found.append(relative)
        return found

    def search(
        self, query: str, *, limit: int = 20, context_lines: int = 3,
        origin_call_id: str | None = None,
    ) -> list[EvidenceAnchor]:
        """Literal ripgrep hits, reread into current hashed line ranges.

        Feed an explicit bounded file list to rg in small batches, then stop
        requesting batches once enough matches have arrived. A timeout covers
        each batch. rg does not run through a shell.
        """
        if not query or "\n" in query or len(query) > 4096:
            raise RetrievalError("search query must be a nonempty single line of at most 4096 chars")
        if not 1 <= limit <= 100 or not 0 <= context_lines <= 50:
            raise RetrievalError("invalid search result or context limit")
        files = self.list_files()
        if not files:
            return []
        # max-count bounds matches per file; file-count and max-filesize bound
        # subprocess memory. Search in small batches rather than an unbounded
        # argument list or a whole-repository JSON capture.
        anchors: list[EvidenceAnchor] = []
        for offset in range(0, len(files), 32):
            try:
                result = subprocess.run(
                    ["rg", "--json", "--fixed-strings", "--max-count", str(limit),
                     "--max-filesize", "2M", "--", query, *files[offset:offset + 32]],
                    cwd=self.workspace, capture_output=True, timeout=5, check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RetrievalError("ripgrep is unavailable or search timed out") from exc
            if result.returncode not in (0, 1):
                raise RetrievalError("ripgrep failed while reading the repository")
            for row in result.stdout.splitlines():
                event = json.loads(row)
                if event.get("type") != "match":
                    continue
                data = event["data"]
                path = data["path"].get("text")
                if path is None:
                    continue
                number = data["line_number"]
                try:
                    snapshot = read_snapshot(self.workspace, path)
                    lines = snapshot.text.splitlines()
                    if number > len(lines) or query not in lines[number - 1]:
                        continue  # A concurrent edit invalidated this rg hit.
                    anchors.append(snapshot.anchor(
                        max(1, number - context_lines), number + context_lines, origin_call_id,
                    ))
                except RetrievalError:
                    continue
                if len(anchors) >= limit:
                    return anchors
        return anchors
