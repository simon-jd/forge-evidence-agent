from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class ArtifactError(ValueError):
    pass


def atomic_write(path: Path, data: bytes) -> None:
    """Persist bytes and their directory entry before returning to the ledger."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class LocalArtifactStore:
    """The API must authorize ``metadata(ref)['run_id']`` before returning data.

    A reference is an opaque ID, never a path supplied by the caller. Artifacts
    are append-only; reads verify their recorded byte count and digest.
    """

    def __init__(self, root: Path | str, max_bytes: int = 64 * 1024 * 1024) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.max_bytes = max_bytes

    def _path(self, ref: str, suffix: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", ref):
            raise ArtifactError("Invalid artifact reference")
        return self.root / ref[:2] / f"{ref}.{suffix}"

    def put_bytes(
        self, run_id: str, kind: str, data: bytes,
        content_type: str = "application/octet-stream",
    ) -> str:
        if len(data) > self.max_bytes:
            raise ArtifactError(f"Artifact exceeds {self.max_bytes} bytes")
        ref = uuid.uuid4().hex
        metadata = {
            "id": ref, "run_id": run_id, "kind": kind,
            "sha256": hashlib.sha256(data).hexdigest(), "byte_size": len(data),
            "content_type": content_type, "created_at": datetime.now(UTC).isoformat(),
        }
        atomic_write(self._path(ref, "data"), data)
        atomic_write(self._path(ref, "json"), json.dumps(metadata).encode())
        return ref

    def put_text(self, run_id: str, kind: str, text: str) -> str:
        return self.put_bytes(run_id, kind, text.encode(), "text/plain; charset=utf-8")

    def metadata(self, ref: str) -> dict[str, Any]:
        try:
            with self._path(ref, "json").open("rb") as stream:
                result: dict[str, Any] = json.load(stream)
            if result["id"] != ref:
                raise ArtifactError("Artifact metadata identity mismatch")
            return result
        except (OSError, KeyError, ValueError) as error:
            raise ArtifactError(f"Artifact {ref} is unavailable") from error

    def read_bytes(self, ref: str) -> bytes:
        metadata = self.metadata(ref)
        try:
            with self._path(ref, "data").open("rb") as stream:
                data = stream.read(self.max_bytes + 1)
        except OSError as error:
            raise ArtifactError(f"Artifact {ref} is unavailable") from error
        if (len(data) != metadata["byte_size"] or
                hashlib.sha256(data).hexdigest() != metadata["sha256"]):
            raise ArtifactError("Artifact failed integrity verification")
        return data

    def read_text(self, ref: str) -> str:
        return self.read_bytes(ref).decode("utf-8")
