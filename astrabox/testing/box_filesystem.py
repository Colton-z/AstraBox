"""A conformance-test box whose filesystem is a real directory.

Storage-provider tests drive ``prepare`` and ``commit`` against the execd
filesystem contract without coupling those checks to a transport.

Each method behaves as execd documents it — ``list_directory`` returns ABSOLUTE
paths, excludes the root itself, honours ``depth``, and reports links as
``symlink`` without following them. A double that got those wrong would agree
with a provider that fails on a live box, which is the failure a double is
supposed to make impossible rather than comfortable.

It does not stand in for transport; live-sandbox verification remains required
for synchronization behavior.
"""

from __future__ import annotations

import posixpath
from pathlib import Path
from typing import Any


class BoxFilesystemDouble:
    """execd's filesystem capability, backed by ``root`` on the local disk."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        #: Every path read back out, in order — lets a test assert that a
        #: refusal happened BEFORE any file crossed, not part-way through.
        self.reads: list[str] = []

    def local_path(self, box_path: str) -> Path:
        """Where ``box_path`` lands on the local disk."""
        return self.root / posixpath.relpath(box_path, "/")

    async def create_directories(self, entries: list[Any]) -> None:
        for entry in entries:
            self.local_path(entry.path).mkdir(parents=True, exist_ok=True)

    async def write_files(self, entries: list[Any]) -> None:
        for entry in entries:
            self._write(entry.path, entry.data)

    async def write_file(self, path: str, data: Any, **_: Any) -> None:
        self._write(path, data)

    async def read_file(self, path: str, **_: Any) -> str:
        return self.local_path(path).read_text()

    async def read_bytes(self, path: str, **_: Any) -> bytes:
        self.reads.append(path)
        return self.local_path(path).read_bytes()

    async def list_directory(self, entry: Any) -> list[Any]:
        base = self.local_path(entry.path)
        depth = int(entry.depth or 1)
        found = []
        for path in sorted(base.rglob("*")):
            relative = path.relative_to(base)
            if len(relative.parts) > depth:
                continue
            found.append(
                _EntryInfo(
                    path=posixpath.join(entry.path, str(relative)),
                    entry_type=_kind_of(path),
                )
            )
        return found

    def _write(self, path: str, data: Any) -> None:
        target = self.local_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data if isinstance(data, bytes) else str(data).encode("utf-8"))


class BoxDouble:
    """A sandbox handle exposing nothing but its filesystem."""

    def __init__(self, root: Path) -> None:
        self.files = BoxFilesystemDouble(root)


class _EntryInfo:
    def __init__(self, *, path: str, entry_type: str) -> None:
        self.path = path
        self.entry_type = entry_type


def _kind_of(path: Path) -> str:
    if path.is_symlink():
        return "symlink"
    return "directory" if path.is_dir() else "file"


__all__ = ["BoxDouble", "BoxFilesystemDouble"]
