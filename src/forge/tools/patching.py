"""Unified patches with all-file preflight and durable before/after manifests."""

from __future__ import annotations

import fnmatch
import hashlib
from dataclasses import dataclass

from unidiff import PatchSet
from unidiff.errors import UnidiffParseError

from forge.domain.types import RepoProfile
from forge.execution.errors import UnsafePath
from forge.execution.safe_files import SafeRoot, relative_parts


@dataclass(frozen=True)
class FileChange:
    path: str
    before: bytes | None
    after: bytes | None

    @property
    def before_hash(self) -> str | None:
        return hashlib.sha256(self.before).hexdigest() if self.before is not None else None

    @property
    def after_hash(self) -> str | None:
        return hashlib.sha256(self.after).hexdigest() if self.after is not None else None


def path_allowed(path: str, profile: RepoProfile) -> bool:
    relative_parts(path)
    for pattern in profile.protected_paths:
        if fnmatch.fnmatchcase(path, pattern) or (pattern.endswith("/**") and path == pattern[:-3]):
            return False
    return any(fnmatch.fnmatchcase(path, pattern) or
               (pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:]))
               for pattern in profile.allowed_patch_paths)


def _patch_path(value: str, prefix: str) -> str | None:
    if value == "/dev/null":
        return None
    if not value.startswith(prefix):
        raise ValueError("Unified patch paths must start with a/ and b/")
    path = value[len(prefix):]
    relative_parts(path)
    return path


def prepare_patch(
    root: SafeRoot, patch: str, expected_file_hashes: dict[str, str | None], profile: RepoProfile,
) -> list[FileChange]:
    if not patch.strip() or len(patch.encode()) > 2 * 1024 * 1024:
        raise ValueError("Patch must contain changes and fit within 2 MiB")
    try:
        parsed = PatchSet(patch.splitlines(keepends=True))
    except (UnidiffParseError, ValueError) as error:
        raise ValueError(f"Invalid unified patch: {error}") from error
    if not parsed or len(parsed) > 100:
        raise ValueError("Patch must affect between 1 and 100 files")
    changes: list[FileChange] = []
    touched: set[str] = set()
    for file_patch in parsed:
        source = _patch_path(file_patch.source_file, "a/")
        target = _patch_path(file_patch.target_file, "b/")
        if source is not None and target is not None and source != target:
            raise ValueError("Rename patches must be represented as delete plus add")
        path = target or source
        if path is None or path in touched or file_patch.is_binary_file:
            raise ValueError("Patch includes duplicate paths, binary data or an invalid path")
        if not path_allowed(path, profile):
            raise UnsafePath(f"Repository profile protects this path: {path}")
        touched.add(path)
        if path not in expected_file_hashes:
            raise ValueError(f"Missing expected hash for {path}")
        try:
            before = root.read(path)
        except FileNotFoundError:
            before = None
        actual_hash = hashlib.sha256(before).hexdigest() if before is not None else None
        if actual_hash != expected_file_hashes[path]:
            raise ValueError(f"Workspace hash conflict for {path}; read current content before editing")
        if (source is None) != (before is None):
            raise ValueError(f"Patch existence precondition failed for {path}")
        try:
            original = (before or b"").decode().splitlines(keepends=True)
        except UnicodeDecodeError as error:
            raise ValueError(f"Binary patch target unsupported: {path}") from error
        if before is not None and b"\x00" in before:
            raise ValueError(f"Binary patch target unsupported: {path}")
        output: list[str] = []
        cursor = 0
        for hunk in file_patch:
            offset = hunk.source_start - 1 if hunk.source_start else 0
            if offset < cursor or offset > len(original):
                raise ValueError(f"Overlapping or out-of-range patch hunk for {path}")
            output.extend(original[cursor:offset])
            if hunk.target_start != (len(output) + 1 if hunk.target_length else len(output)):
                raise ValueError(f"Incorrect target hunk offset for {path}")
            cursor = offset
            normalized: list[tuple[str, str]] = []
            for line in hunk:
                if line.line_type == "\\":
                    if not normalized or "No newline at end of file" not in line.value:
                        raise ValueError("Unsupported patch line annotation")
                    kind, value = normalized[-1]
                    normalized[-1] = (kind, value.removesuffix("\n"))
                else:
                    normalized.append((line.line_type, line.value))
            for kind, value in normalized:
                if kind in {" ", "-"}:
                    if cursor >= len(original) or original[cursor] != value:
                        raise ValueError(f"Patch context does not match current file {path}")
                    cursor += 1
                if kind in {" ", "+"}:
                    output.append(value)
                if kind not in {" ", "+", "-"}:
                    raise ValueError("Unsupported patch line")
        output.extend(original[cursor:])
        after = "".join(output).encode() if target is not None else None
        if target is None and output:
            raise ValueError(f"Deleted-file patch leaves content in {path}")
        if after is not None and len(after) > 8 * 1024 * 1024:
            raise ValueError("Patched file exceeds workspace file limit")
        if before == after:
            raise ValueError(f"Patch does not change {path}")
        changes.append(FileChange(path, before, after))
    if set(expected_file_hashes) != touched:
        raise ValueError("Expected hashes must match exactly the files affected by the patch")
    return changes
