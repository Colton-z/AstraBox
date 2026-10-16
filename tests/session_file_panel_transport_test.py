"""Session file panel over the box's files face (``SandboxFiles``).

These tests pin the host-side service without an AIO server, port 8080, or an
in-sandbox command runner. The product-level browser journey is covered by the
Playwright file-panel spec, and the files contract itself by the live suite in
``astrabox.testing.sandbox_data_conformance``; this module keeps ownership,
shared-box isolation, symlink containment and error hygiene cheap to diagnose.
"""

from __future__ import annotations

import dataclasses
import io
from contextlib import asynccontextmanager
import posixpath
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.session_file_service import (
    FileDownload,
    SessionFileService,
)
from astrabox.seams.sandbox_data import (
    SandboxDirectorySpec,
    SandboxEntry,
    SandboxPathNotFound,
    SandboxPathRejected,
)


def _entry(path: str, kind: Any, size: int = 0) -> SandboxEntry:
    return SandboxEntry(
        path=path,
        kind=kind,
        size=size,
        mode=0o750 if kind == "directory" else 0o640,
        owner="root",
        group="root",
        modified_at=datetime(2026, 8, 6, tzinfo=timezone.utc),
    )


class _Filesystem:
    """A box's files as the seam shapes them, recording each call."""

    def __init__(self, *roots: str) -> None:
        self.roots = tuple(roots or ("/workspace",))
        self.entries: dict[str, SandboxEntry] = {
            root: _entry(root, "directory") for root in self.roots
        }
        self.content: dict[str, bytes] = {}
        self.calls: list[tuple[str, Any]] = []
        self.last_write_data: Any = None
        self.fail_list_with: Exception | None = None

    async def read_bytes(self, path: str) -> bytes:
        self.calls.append(("read_bytes", path))
        if path not in self.content:
            raise SandboxPathNotFound(path)
        return self.content[path]

    async def read_chunks(self, path: str):
        self.calls.append(("read_chunks", path))
        if path not in self.content:
            raise SandboxPathNotFound(path)
        payload = self.content[path]
        for offset in range(0, len(payload), 3):
            yield payload[offset : offset + 3]

    async def write_bytes(
        self,
        path: str,
        data: Any,
        *,
        mode: int = 0o644,
        owner: str | None = None,
        group: str | None = None,
    ) -> None:
        self.calls.append(
            ("write_bytes", {"path": path, "mode": mode, "owner": owner, "group": group})
        )
        self.last_write_data = data
        body = bytes(data.read() if hasattr(data, "read") else data)
        self.content[path] = body
        self.entries[path] = _entry(path, "file", size=len(body))

    async def make_directories(self, specs: list[SandboxDirectorySpec]) -> None:
        self.calls.append(("make_directories", list(specs)))
        for spec in specs:
            target = posixpath.normpath(spec.path)
            current = "/"
            for part in target.strip("/").split("/"):
                current = posixpath.join(current, part)
                if any(
                    current == root or current.startswith(f"{root}/")
                    for root in self.roots
                ):
                    self.entries.setdefault(current, _entry(current, "directory"))

    async def stat(self, paths: list[str]) -> dict[str, SandboxEntry]:
        self.calls.append(("stat", list(paths)))
        for path in paths:
            if path not in self.entries:
                raise SandboxPathNotFound(path)
        return {path: self.entries[path] for path in paths}

    async def list_directory(self, path: str) -> list[SandboxEntry]:
        self.calls.append(("list_directory", path))
        if self.fail_list_with is not None:
            raise self.fail_list_with
        parent = posixpath.normpath(path)
        return [
            entry
            for found, entry in self.entries.items()
            if found != parent and posixpath.dirname(found) == parent
        ]

    async def search(self, root: str, pattern: str) -> list[str]:
        self.calls.append(("search", (root, pattern)))
        return []

    async def move(self, source: str, destination: str) -> None:
        self.calls.append(("move", (source, destination)))
        affected = [
            path for path in self.entries if path == source or path.startswith(f"{source}/")
        ]
        if not affected:
            raise SandboxPathNotFound(source)
        for old in sorted(affected, key=len):
            new = f"{destination}{old[len(source) :]}"
            self.entries[new] = dataclasses.replace(self.entries.pop(old), path=new)
            if old in self.content:
                self.content[new] = self.content.pop(old)

    async def remove(self, paths: list[str]) -> None:
        self.calls.append(("remove", list(paths)))
        for target in paths:
            for path in list(self.entries):
                if path == target or path.startswith(f"{target}/"):
                    self.entries.pop(path, None)
                    self.content.pop(path, None)


async def _read_all(download: FileDownload) -> bytes:
    try:
        return b"".join([chunk async for chunk in download])
    finally:
        await download.aclose()


class _Box:
    """A sandbox handle with files only: deliberately no AIO endpoint or shell."""

    def __init__(self, filesystem: _Filesystem) -> None:
        self.files = filesystem
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _Upload:
    def __init__(self, filename: str, content: bytes) -> None:
        self.filename = filename
        self.content_type = "application/octet-stream"
        self.file = io.BytesIO(content)


class _Service(SessionFileService):
    def __init__(
        self,
        filesystem: _Filesystem,
        *,
        session_id: str = "s-1",
        workspace_source_dir: str = "/workspace",
    ) -> None:
        super().__init__(
            sessions_repo=None,
            agent_repo=None,
            runtime_manager=None,
        )
        self.filesystem = filesystem
        self.session = {
            "session_id": session_id,
            "session_kind": "agent_chat",
            "runtime_identity": {
                "linux_user": (
                    "conv_s1"
                    if session_id == "s-1"
                    else f"conv_{session_id.replace('-', '_')}"
                ),
                "home_dir": (
                    "/home/agent"
                    if workspace_source_dir == "/workspace"
                    else posixpath.dirname(workspace_source_dir)
                ),
                "workspace_dir": "/workspace",
                "workspace_source_dir": workspace_source_dir,
                "file_root_dir": "/workspace",
                "file_root_source_dir": workspace_source_dir,
                "sandbox_tenancy": (
                    "conversation"
                    if workspace_source_dir == "/workspace"
                    else "agent"
                ),
            },
        }
        self.box = _Box(filesystem)

    @asynccontextmanager
    async def _resolve_session_context(self, *_args: Any, **_kwargs: Any):
        yield self.session, self.box, "/workspace"


async def test_shared_conversations_expose_one_root_without_sharing_backing_files() -> None:
    first_root = "/home/conversations/conv_first/workspace"
    second_root = "/home/conversations/conv_second/workspace"
    filesystem = _Filesystem(first_root, second_root)
    first = _Service(
        filesystem,
        session_id="first",
        workspace_source_dir=first_root,
    )
    second = _Service(
        filesystem,
        session_id="second",
        workspace_source_dir=second_root,
    )

    for service, payload in ((first, b"first"), (second, b"second")):
        uploaded = await service.upload_files(
            object(),
            str(service.session["session_id"]),
            path=None,
            files=[_Upload("same.txt", payload)],
        )
        assert uploaded["root_path"] == "/workspace"
        assert uploaded["entries"][0]["path"] == "/workspace/same.txt"

    assert filesystem.content[f"{first_root}/same.txt"] == b"first"
    assert filesystem.content[f"{second_root}/same.txt"] == b"second"
    first_body = await _read_all(await first.download_file(object(), "first", path="same.txt"))
    second_body = await _read_all(await second.download_file(object(), "second", path="same.txt"))
    assert first_body == b"first"
    assert second_body == b"second"

    for service, session_id in ((first, "first"), (second, "second")):
        listing = await service.list_entries(object(), session_id)
        assert listing["root_path"] == "/workspace"
        assert [entry["path"] for entry in listing["entries"]] == [
            "/workspace/same.txt"
        ]


async def test_symlink_cannot_redirect_reads_but_can_be_renamed_or_deleted() -> None:
    filesystem = _Filesystem()
    filesystem.entries["/workspace/outside"] = _entry("/workspace/outside", "symlink")
    service = _Service(filesystem)

    with pytest.raises(APIError) as raised:
        await service.list_entries(object(), "s-1", path="outside")
    assert raised.value.code == "INVALID_REQUEST"
    assert "symlink" in raised.value.message

    await service.move_path(
        object(),
        "s-1",
        src_path="outside",
        dest_path="renamed-link",
    )
    assert filesystem.entries["/workspace/renamed-link"].kind == "symlink"
    await service.delete_paths(object(), "s-1", paths=["renamed-link"])
    assert "/workspace/renamed-link" not in filesystem.entries


async def test_mutations_cannot_remove_or_move_the_session_root() -> None:
    service = _Service(_Filesystem())
    with pytest.raises(APIError) as delete_error:
        await service.delete_paths(object(), "s-1", paths=["/workspace"])
    assert delete_error.value.code == "INVALID_REQUEST"
    assert "session root" in delete_error.value.message

    with pytest.raises(APIError) as move_error:
        await service.move_path(object(), "s-1", src_path="/workspace", dest_path="other")
    assert move_error.value.code == "INVALID_REQUEST"


async def test_filesystem_statuses_are_mapped_without_exposing_transport_details() -> None:
    filesystem = _Filesystem()
    filesystem.fail_list_with = SandboxPathRejected("filesystem status 500")
    service = _Service(filesystem)
    with pytest.raises(APIError) as raised:
        await service.list_entries(object(), "s-1")
    assert raised.value.code == "AGENT_RUNTIME_ERROR"
    assert raised.value.status_code == 502
    assert "filesystem status 500" not in raised.value.message


class _FileSessions:
    def __init__(self, session: dict[str, Any]) -> None:
        self.session = dict(session)
        self.updates: list[dict[str, Any]] = []
        self.notices: list[str | None] = []
        self.fail_write = False

    async def get_session(self, _session_id: str) -> dict[str, Any]:
        return dict(self.session)

    async def compare_and_update_session(
        self, _session_id: str, *, expected: dict[str, Any],
        updates: dict[str, Any], touch_updated_at: bool = True, notify: str | None = None,
    ) -> bool:
        if self.fail_write:
            raise RuntimeError('database unavailable')
        if not all(self.session.get(key) == value for key, value in expected.items()):
            return False
        assert touch_updated_at is False
        self.session.update(updates)
        self.updates.append(dict(updates))
        self.notices.append(notify)
        return True


def _file_transport(*, parked: bool = False):
    from astrabox.core.service.orchestrator.runtime_manager import RemoteAgentRuntimeManager

    filesystem = _Filesystem()
    session = {
        **_Service(filesystem).session,
        'user_id': 'owner',
        'sandbox_id': 'same-box',
        'sandbox_parked_at': 'park-1' if parked else None,
        'state': 'READY',
        'runtime_unavailable': False,
    }
    repo = _FileSessions(session)

    class Manager(RemoteAgentRuntimeManager):
        def __init__(self) -> None:
            self._sessions_repo = repo
            self._session_locks = {}
            self.filesystem = filesystem
            self.handles: list[_Box] = []
            self.resumes: list[str] = []
            self.resume_result = True
            self.during_resume = None

        async def resume_sandbox_by_id(self, sandbox_id: str) -> bool:
            self.resumes.append(sandbox_id)
            if self.during_resume:
                await self.during_resume()
            return self.resume_result

        async def connect_sandbox_only(self, sandbox_id: str):
            assert sandbox_id == 'same-box'
            assert repo.session['sandbox_parked_at'] is None
            handle = _Box(self.filesystem)
            self.handles.append(handle)
            return handle

        def get_runtime(self, *_args: Any, **_kwargs: Any):
            raise AssertionError('file operations must not reuse runtime transport')

    class Service(SessionFileService):
        async def _resolve_effective_runtime_session(self, session: dict[str, Any]):
            return session

    manager = Manager()
    service = Service(
        sessions_repo=repo, agent_repo=None,
        runtime_manager=manager,
    )
    return service, manager, repo, SimpleNamespace(user_id='owner')


async def test_file_owner_check_precedes_wake_or_connection() -> None:
    service, manager, repo, _user = _file_transport(parked=True)
    with pytest.raises(APIError) as raised:
        await service.list_entries(SimpleNamespace(user_id='other-owner'), 's-1')
    assert raised.value.status_code == 404
    assert manager.resumes == [] and manager.handles == []
    assert repo.session['sandbox_parked_at'] == 'park-1'
