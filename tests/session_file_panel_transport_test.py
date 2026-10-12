"""Session file panel over the box's files face (``SandboxFiles``).

These tests pin the host-side service without an AIO server, port 8080, or an
in-sandbox command runner. The product-level browser journey is covered by the
Playwright file-panel spec, and the files contract itself by the live suite in
``astrabox.testing.sandbox_data_conformance``; this module keeps filesystem
mapping, ownership, binary fidelity, download streaming and symlink containment
cheap to diagnose.
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
    SessionFileStreamBroken,
)
from astrabox.providers import register_builtin_providers
from astrabox.seams.sandbox import SandboxUnreachable
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
            session_snapshots_repo=None,
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


async def test_full_file_journey_uses_only_the_box_files() -> None:
    filesystem = _Filesystem()
    service = _Service(filesystem)
    user = object()

    empty = await service.list_entries(user, "s-1")
    assert empty["entries"] == []
    list_call = next(value for name, value in filesystem.calls if name == "list_directory")
    assert list_call == "/workspace"

    created = await service.create_directory(user, "s-1", path="artifacts")
    assert created["path"] == "/workspace/artifacts"
    mkdir_spec = next(value[0] for name, value in filesystem.calls if name == "make_directories")
    assert mkdir_spec == SandboxDirectorySpec(
        path="/workspace/artifacts", mode=0o750, owner="conv_s1", group="conv_s1"
    )

    body = b"\x00binary\r\nwith a trailing newline\n"
    upload = _Upload("source.bin", body)
    uploaded = await service.upload_files(
        user,
        "s-1",
        path="artifacts",
        files=[upload],
    )
    assert uploaded["uploaded_count"] == 1
    assert uploaded["entries"][0]["path"] == "/workspace/artifacts/source.bin"
    assert filesystem.last_write_data is upload.file
    write_call = next(value for name, value in filesystem.calls if name == "write_bytes")
    assert write_call == {
        "path": "/workspace/artifacts/source.bin",
        "mode": 0o640,
        "owner": "conv_s1",
        "group": "conv_s1",
    }

    listing = await service.list_entries(user, "s-1", path="artifacts")
    assert listing["entries"] == [
        {
            "path": "/workspace/artifacts/source.bin",
            "name": "source.bin",
            "kind": "file",
            "size": len(body),
            "modified_at": "2026-08-06T00:00:00+00:00",
        }
    ]

    moved = await service.move_path(
        user,
        "s-1",
        src_path="artifacts/source.bin",
        dest_path="artifacts/renamed.bin",
    )
    assert moved["dest_path"] == "/workspace/artifacts/renamed.bin"
    download = await service.download_file(
        user,
        "s-1",
        path="artifacts/renamed.bin",
    )
    assert (download.filename, download.size) == ("renamed.bin", len(body))
    assert await _read_all(download) == body

    deleted_file = await service.delete_paths(
        user,
        "s-1",
        paths=["artifacts/renamed.bin"],
    )
    assert deleted_file["deleted_count"] == 1
    await service.delete_paths(user, "s-1", paths=["artifacts"])
    assert set(filesystem.entries) == {"/workspace"}

    repeated = await service.delete_paths(user, "s-1", paths=["artifacts"])
    assert repeated["deleted_count"] == 0
    assert set(filesystem.entries) == {"/workspace"}


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


async def test_assistant_file_api_maps_the_owner_profile_without_persisting_it_on_session() -> None:
    register_builtin_providers()
    # Rendered from the Assistant's profile rather than spelled: the home is one
    # segment now (the account), and a fixture that spells a path stops
    # exercising the mapping the moment the template moves.
    from astrabox.core.service.orchestrator.runtime.conversation_identity import (
        assistant_workspace_dir,
    )

    source_root = assistant_workspace_dir("owner-1", "assistant-1")
    filesystem = _Filesystem(source_root)
    filesystem.entries[f"{source_root}/notes.txt"] = _entry(
        f"{source_root}/notes.txt",
        "file",
        size=5,
    )

    class AssistantService(SessionFileService):
        def __init__(self) -> None:
            super().__init__(
                sessions_repo=None,
                session_snapshots_repo=None,
                agent_repo=None,
                runtime_manager=SimpleNamespace(connect_sandbox_only=self.connect),
            )
            self.session = {
                "session_id": "assistant-session",
                "session_kind": "assistant_chat",
                "sandbox_id": "sandbox-1",
                "workspace_ref": {
                    "kind": "assistant",
                    "engine_kind": "assistant",
                    "user_id": "owner-1",
                    "assistant_id": "assistant-1",
                    "sandbox_id": "sandbox-1",
                },
            }

        async def _must_get_owned_session(self, *_args: Any, **_kwargs: Any):
            return self.session

        async def _resolve_effective_runtime_session(self, session: dict[str, Any]):
            return session

        async def connect(self, *_args: Any, **_kwargs: Any):
            return _Box(filesystem)

    listing = await AssistantService().list_entries(object(), "assistant-session")

    assert listing["root_path"] == "/workspace"
    assert [entry["path"] for entry in listing["entries"]] == [
        "/workspace/notes.txt"
    ]
    list_call = next(value for name, value in filesystem.calls if name == "list_directory")
    assert list_call == source_root


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


@pytest.mark.parametrize(
    ("listed_size", "served", "lost"),
    [(9, b"123456789abc", False), (12, b"123456789", False), (9, b"123", True)],
    ids=["file-grew", "file-shrank", "box-lost"],
)
async def test_a_download_that_breaks_after_its_headers_never_reads_as_complete(
    listed_size: int, served: bytes, lost: bool
) -> None:
    """The response already promised ``listed_size`` bytes when these fail.

    The file changes after its metadata was read, or the box goes away after
    the first chunk. No error response can follow the headers any more, so the
    stream must fail before it has handed over every promised byte: a client
    that received ``Content-Length`` bytes would take the download as complete.
    """

    filesystem = _Filesystem()
    filesystem.entries["/workspace/f.bin"] = _entry("/workspace/f.bin", "file", size=listed_size)
    filesystem.content["/workspace/f.bin"] = served
    if lost:
        async def served_then_lost(_path: str):
            yield served
            raise SandboxUnreachable("sandbox-1", "connection reset by peer")

        filesystem.read_chunks = served_then_lost  # type: ignore[method-assign]
    download = await _Service(filesystem).download_file(object(), "s-1", path="f.bin")
    delivered = b""
    with pytest.raises(SessionFileStreamBroken) as raised:
        async for chunk in download:
            delivered += chunk
    await download.aclose()

    assert len(delivered) < listed_size
    assert not download.complete
    if lost:
        assert isinstance(raised.value.__cause__, SandboxUnreachable)


async def test_filesystem_statuses_are_mapped_without_exposing_transport_details() -> None:
    filesystem = _Filesystem()
    filesystem.fail_list_with = SandboxPathRejected("filesystem status 500")
    service = _Service(filesystem)
    with pytest.raises(APIError) as raised:
        await service.list_entries(object(), "s-1")
    assert raised.value.code == "AGENT_RUNTIME_ERROR"
    assert raised.value.status_code == 502
    assert "filesystem status 500" not in raised.value.message


async def test_a_box_lost_mid_download_answers_without_transport_words() -> None:
    """The unreachable error is an APIError too; the stream reader still maps it."""

    filesystem = _Filesystem()
    filesystem.entries["/workspace/file.bin"] = _entry("/workspace/file.bin", "file", size=3)

    async def unreachable(_path: str):
        raise SandboxUnreachable("sandbox-1", "All connection attempts failed")
        yield b""

    filesystem.read_chunks = unreachable  # type: ignore[method-assign]
    service = _Service(filesystem)
    with pytest.raises(APIError) as raised:
        await service.download_file(object(), "s-1", path="file.bin")
    assert (raised.value.code, raised.value.status_code) == ("SANDBOX_UNREACHABLE", 503)
    assert "All connection attempts failed" not in raised.value.message


@pytest.mark.parametrize(
    ("failure", "status"),
    [
        (
            SandboxUnreachable(
                "sandbox-1", "Network connectivity error: All connection attempts failed"
            ),
            503,
        ),
        (RuntimeError("Network connectivity error: All connection attempts failed"), 502),
    ],
    ids=["unreachable", "unexpected"],
)
def test_a_filesystem_failure_logs_the_vendors_own_words(
    caplog: pytest.LogCaptureFixture, failure: Exception, status: int
) -> None:
    """A failure's own words go to the log, and only there.

    An operator needs the text to tell an unreachable box from an internal
    fault, which a response naming only the operation cannot do. The response
    is a boundary and keeps the words out.
    """

    with caplog.at_level("WARNING"):
        error = SessionFileService._filesystem_api_error(
            failure,
            op_label="file metadata lookup",
            path="/workspace",
        )

    assert error.status_code == status
    assert "All connection attempts failed" not in error.message, (
        "the transport's words must not cross the response boundary"
    )
    assert "All connection attempts failed" in caplog.text, (
        "the operator's copy must carry them"
    )
    assert "path=/workspace" in caplog.text


def test_a_recognised_status_still_answers_in_its_own_terms() -> None:
    """The control: the fallback is where the detail goes, and the cases that
    already say something precise are left alone."""

    missing = SessionFileService._filesystem_api_error(
        SandboxPathNotFound("/workspace/absent.txt"),
        op_label="file metadata lookup",
        path="/workspace/absent.txt",
    )

    assert missing.status_code == 404
    assert missing.code == "FILE_NOT_FOUND"
    assert "/workspace/absent.txt" in missing.message


class _FileSessions:
    def __init__(self, session: dict[str, Any]) -> None:
        self.session = dict(session)
        self.updates: list[dict[str, Any]] = []
        self.fail_write = False

    async def get_session(self, _session_id: str) -> dict[str, Any]:
        return dict(self.session)

    async def compare_and_update_session(
        self, _session_id: str, *, expected: dict[str, Any],
        updates: dict[str, Any], touch_updated_at: bool = True,
    ) -> bool:
        if self.fail_write:
            raise RuntimeError('database unavailable')
        if not all(self.session.get(key) == value for key, value in expected.items()):
            return False
        assert touch_updated_at is False
        self.session.update(updates)
        self.updates.append(dict(updates))
        return True


def _file_transport(*, parked: bool = False):
    from astrabox.core.service.orchestrator.runtime_manager import RemoteAgentRuntimeManager

    filesystem = _Filesystem()
    session = {
        **_Service(filesystem).session,
        'user_id': 'owner',
        'sandbox_id': 'same-box',
        'sandbox_endpoint': 'old-endpoint',
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
        sessions_repo=repo, session_snapshots_repo=None, agent_repo=None,
        runtime_manager=manager,
    )
    return service, manager, repo, SimpleNamespace(user_id='owner')


async def test_file_listing_wakes_original_box_and_preserves_ready_state() -> None:
    service, manager, repo, user = _file_transport(parked=True)
    manager.filesystem.entries['/workspace/kept.txt'] = _entry('/workspace/kept.txt', 'file')

    listing = await service.list_entries(user, 's-1')

    assert [entry['name'] for entry in listing['entries']] == ['kept.txt']
    assert manager.resumes == ['same-box']
    assert repo.session['sandbox_id'] == 'same-box'
    assert repo.session['sandbox_endpoint'] is None
    assert repo.session['sandbox_parked_at'] is None
    assert datetime.fromisoformat(repo.session['sandbox_resumed_at']).tzinfo is not None
    assert repo.session['state'] == 'READY'
    assert repo.session['runtime_unavailable'] is False
    assert len(manager.handles) == 1 and manager.handles[0].closed


async def test_file_request_resolves_new_endpoint_after_another_reader_wakes_same_id() -> None:
    service, manager, repo, user = _file_transport()
    await service.list_entries(user, 's-1')
    manager.filesystem = _Filesystem()
    manager.filesystem.entries['/workspace/new.txt'] = _entry('/workspace/new.txt', 'file', 3)
    manager.filesystem.content['/workspace/new.txt'] = b'new'
    # Another worker already completed resume: neither the ID nor the parked
    # mark tells this reader that its previous connection is now obsolete.
    download = await service.download_file_for_session(repo.session, path='new.txt')
    assert manager.resumes == []
    assert len(manager.handles) == 2
    assert manager.handles[0] is not manager.handles[1]
    # The body is still to be sent, so the connection it reads from stays open
    # until the download is closed.
    assert not manager.handles[1].closed
    assert (await _read_all(download), download.filename) == (b'new', 'new.txt')
    assert all(handle.closed for handle in manager.handles)


async def test_file_connection_closes_after_filesystem_failure() -> None:
    service, manager, _repo, user = _file_transport()
    manager.filesystem.fail_list_with = SandboxPathRejected("filesystem status 503")
    with pytest.raises(APIError):
        await service.list_entries(user, 's-1')
    assert len(manager.handles) == 1 and manager.handles[0].closed


async def test_file_owner_check_precedes_wake_or_connection() -> None:
    service, manager, repo, _user = _file_transport(parked=True)
    with pytest.raises(APIError) as raised:
        await service.list_entries(SimpleNamespace(user_id='other-owner'), 's-1')
    assert raised.value.status_code == 404
    assert manager.resumes == [] and manager.handles == []
    assert repo.session['sandbox_parked_at'] == 'park-1'


@pytest.mark.parametrize('failure', ['refused', 'provider-error', 'database-error', 'binding-change'])
async def test_file_wake_failure_does_not_replace_box_or_erase_park_record(failure: str) -> None:
    service, manager, repo, user = _file_transport(parked=True)
    if failure == 'refused':
        manager.resume_result = False
    elif failure == 'database-error':
        repo.fail_write = True
    else:
        async def change_or_fail():
            if failure == 'provider-error':
                raise RuntimeError('provider unavailable')
            repo.session.update(sandbox_id='replacement', sandbox_parked_at='park-2')
        manager.during_resume = change_or_fail
    with pytest.raises((APIError, RuntimeError)):
        await service.list_entries(user, 's-1')
    assert manager.handles == []
    assert repo.updates == []
    assert repo.session['sandbox_parked_at'] == ('park-2' if failure == 'binding-change' else 'park-1')
    assert repo.session['state'] == 'READY'
    assert repo.session['runtime_unavailable'] is False


async def test_concurrent_file_and_turn_wake_share_one_resume() -> None:
    import asyncio

    service, manager, repo, user = _file_transport(parked=True)
    turn_session = dict(repo.session)
    entered = asyncio.Event()
    released = asyncio.Event()

    async def hold_resume():
        entered.set()
        await released.wait()

    manager.during_resume = hold_resume
    listing = asyncio.create_task(service.list_entries(user, 's-1'))
    await asyncio.wait_for(entered.wait(), timeout=1)
    turn = asyncio.create_task(manager.wake_parked_session(turn_session))
    released.set()
    await asyncio.gather(listing, turn)
    assert manager.resumes == ['same-box']
    assert turn_session['sandbox_parked_at'] is None
    assert turn_session['sandbox_endpoint'] is None
    assert all(handle.closed for handle in manager.handles)
