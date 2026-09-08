"""Directory-descriptor file access; no symlinks are followed at any component."""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from forge.execution.errors import UnsafePath

MAX_FILE_BYTES = 8 * 1024 * 1024
IGNORED_DIRS = {".git", ".forge", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"}


def relative_parts(path: str, *, allow_root: bool = False) -> tuple[str, ...]:
    if allow_root and path == ".":
        return ()
    if not path or any(ord(char) < 32 for char in path) or "\\" in path or path.startswith("/"):
        raise UnsafePath("Expected a nonempty POSIX relative path")
    raw = path.split("/")
    if any(part in {"", ".", ".."} for part in raw):
        raise UnsafePath("Path contains empty, dot or parent components")
    parts = PurePosixPath(path).parts
    if any(part in {".git", ".forge"} for part in parts):
        raise UnsafePath("Git and Forge control paths are protected")
    return parts


class SafeRoot:
    def __init__(self, root: Path, *, shared_writes: bool = False) -> None:
        self.root = root
        self.shared_writes = shared_writes

    @contextmanager
    def directory(self, parts: tuple[str, ...], create: bool = False) -> Iterator[int]:
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts:
                if create:
                    try:
                        os.mkdir(part, mode=0o777 if self.shared_writes else 0o755, dir_fd=fd)
                        if self.shared_writes:
                            os.chmod(part, 0o777, dir_fd=fd, follow_symlinks=False)
                    except FileExistsError:
                        pass
                try:
                    child = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd,
                    )
                except FileNotFoundError:
                    raise
                except OSError as error:
                    raise UnsafePath("Parent is not a regular directory (symlinks forbidden)") from error
                os.close(fd)
                fd = child
            yield fd
        finally:
            os.close(fd)

    def read(self, path: str, max_bytes: int = MAX_FILE_BYTES) -> bytes:
        parts = relative_parts(path)
        with self.directory(parts[:-1]) as parent:
            try:
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            except FileNotFoundError:
                raise
            except OSError as error:
                raise UnsafePath("File is not safely readable (symlinks forbidden)") from error
            with os.fdopen(fd, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise UnsafePath("Only ordinary single-link files are supported")
                if metadata.st_size > max_bytes:
                    raise ValueError(f"File exceeds {max_bytes} bytes")
                data = stream.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise ValueError(f"File exceeds {max_bytes} bytes")
                return data

    def digest(self, path: str) -> str | None:
        try:
            return hashlib.sha256(self.read(path)).hexdigest()
        except FileNotFoundError:
            return None

    def write(self, path: str, data: bytes, *, executable: bool = False) -> None:
        parts = relative_parts(path)
        with self.directory(parts[:-1], create=True) as parent:
            temporary = f".forge-write-{uuid.uuid4().hex}"
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o755 if executable else 0o644, dir_fd=parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    if self.shared_writes:
                        os.fchmod(stream.fileno(), 0o777 if executable else 0o666)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                # rename replaces a leaf symlink instead of following it. Parent
                # descriptors remain fixed throughout the operation.
                os.replace(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass

    def remove(self, path: str) -> None:
        parts = relative_parts(path)
        with self.directory(parts[:-1]) as parent:
            info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise UnsafePath("Only ordinary files can be removed")
            os.unlink(parts[-1], dir_fd=parent)
            os.fsync(parent)

    def files(self, *, max_files: int = 20_000) -> list[str]:
        result: list[str] = []

        def walk(fd: int, prefix: str) -> None:
            for name in sorted(os.listdir(fd)):
                if name in IGNORED_DIRS:
                    continue
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                path = f"{prefix}/{name}" if prefix else name
                if stat.S_ISLNK(info.st_mode):
                    # Do not hide a newly created unsafe node from a snapshot.
                    raise UnsafePath(f"Workspace contains unsupported symlink: {path}")
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        walk(child, path)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    relative_parts(path)
                    result.append(path)
                    if len(result) > max_files:
                        raise ValueError(f"Workspace exceeds {max_files} indexed files")
                else:
                    raise UnsafePath(f"Unsupported workspace node: {path}")

        with self.directory(()) as root:
            walk(root, "")
        return result
