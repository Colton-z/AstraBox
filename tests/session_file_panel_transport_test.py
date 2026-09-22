"""Session file panel over OpenSandbox execd's native Filesystem API.

These tests pin the host-side adapter without an AIO server, port 8080, or an
in-sandbox command runner. The product-level browser journey is covered by the
Playwright file-panel spec; this module keeps filesystem mapping, ownership,
binary fidelity, response bounds and symlink containment cheap to diagnose.
"""

from __future__ import annotations

import io
import posixpath
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator import session_file_service as module
from astrabox.core.service.orchestrator.session_file_service import SessionFileService
from astrabox.providers import register_builtin_providers


@dataclass
class _Info:
    path: str
    entry_type: str
    size: int = 0
    modified_at: datetime = datetime(2026, 8, 6, tzinfo=timezone.utc)


class _FilesystemError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"filesystem status {status_code}")
        self.status_code = status_code


class _Filesystem:
    def __init__(self, *roots: str) -> None:
        self.roots = tuple(roots or ("/workspace",))
        self.entries: dict[str, _Info] = {
            root: _Info(root, "directory") for root in self.roots
        }
        self.content: dict[str, bytes] = {}
        self.calls: list[tuple[str, Any]] = []
        self.last_write_data: Any = None
        self.fail_list_with: Exception | None = None

    async def get_file_info(self, paths: list[str]) -> dict[str, _Info]:
        self.calls.append(("get_file_info", list(paths)))
        return {path: self.entries[path] for path in paths if path in self.entries}

    async def list_directory(self, entry: Any) -> list[_Info]:
        self.calls.append(("list_directory", entry))
        if self.fail_list_with is not None:
            raise self.fail_list_with
        parent = posixpath.normpath(entry.path)
        return [
            info
            for path, info in self.entries.items()
            if path != parent and posixpath.dirname(path) == parent
        ]

    async def write_file(
        self,
        path: str,
        data: Any,
        *,
        encoding: str = "utf-8",
        mode: int = 0o755,
        owner: str | None = None,
        group: str | None = None,
    ) -> None:
        self.calls.append(
            (
                "write_file",
                {
                    "path": path,
                    "encoding": encoding,
                    "mode": mode,
                    "owner": owner,
                    "group": group,
                },
            )
        )
        self.last_write_data = data
        payload = data.read() if hasattr(data, "read") else data
        if isinstance(payload, str):
            payload = payload.encode(encoding)
        body = bytes(payload)
        self.content[path] = body
        self.entries[path] = _Info(path, "file", size=len(body))

    async def create_directories(self, entries: list[Any]) -> None:
        self.calls.append(("create_directories", entries))
        for entry in entries:
            target = posixpath.normpath(entry.path)
            current = "/"
            for part in target.strip("/").split("/"):
                current = posixpath.join(current, part)
                if any(
                    current == root or current.startswith(f"{root}/")
                    for root in self.roots
                ):
                    self.entries.setdefault(current, _Info(current, "directory"))

    async def move_files(self, entries: list[Any]) -> None:
        self.calls.append(("move_files", entries))
        for entry in entries:
            src, dest = entry.src, entry.dest
            affected = [path for path in self.entries if path == src or path.startswith(f"{src}/")]
            if not affected:
                raise _FilesystemError(404)
            for old in sorted(affected, key=len):
                new = f"{dest}{old[len(src) :]}"
                info = self.entries.pop(old)
                self.entries[new] = _Info(
                    new,
                    info.entry_type,
                    size=info.size,
                    modified_at=info.modified_at,
                )
                if old in self.content:
                    self.content[new] = self.content.pop(old)

    async def delete_files(self, paths: list[str]) -> None:
        self.calls.append(("delete_files", list(paths)))
        for path in paths:
            self.entries.pop(path, None)
            self.content.pop(path, None)

    async def delete_directories(self, paths: list[str]) -> None:
        self.calls.append(("delete_directories", list(paths)))
        for target in paths:
            for path in list(self.entries):
                if path == target or path.startswith(f"{target}/"):
                    self.entries.pop(path, None)
                    self.content.pop(path, None)

    async def read_bytes_stream(self, path: str, **_kwargs: Any) -> Any:
        self.calls.append(("read_bytes_stream", path))
        if path not in self.content:
            raise _FilesystemError(404)
        payload = self.content[path]

        async def _chunks():
            for offset in range(0, len(payload), 3):
                yield payload[offset : offset + 3]

        return _chunks()


class _Box:
    """A sandbox with Filesystem only: deliberately no AIO endpoint or shell."""

    def __init__(self, filesystem: _Filesystem) -> None:
        self.files = filesystem


class _Wrapper:
    def __init__(self, sandbox: _Box) -> None:
        self.sandbox = sandbox


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
        self.box = _Wrapper(_Box(filesystem))

    async def _resolve_session_context(self, *_args: Any, **_kwargs: Any):
        return self.session, self.box, "/workspace"


async def test_full_file_journey_uses_only_execd_filesystem() -> None:
    filesystem = _Filesystem()
    service = _Service(filesystem)
    user = object()

    empty = await service.list_entries(user, "s-1")
    assert empty["entries"] == []
    list_call = next(value for name, value in filesystem.calls if name == "list_directory")
    assert list_call.path == "/workspace"
    assert list_call.depth == 1

    created = await service.create_directory(user, "s-1", path="artifacts")
    assert created["path"] == "/workspace/artifacts"
    mkdir_entry = next(value[0] for name, value in filesystem.calls if name == "create_directories")
    assert mkdir_entry.model_dump(exclude={"data"}) == {
        "path": "/workspace/artifacts",
        # OpenSandbox transports the octal digits as an integer, not Python's
        # decimal value for an octal literal.
        "mode": 750,
        "owner": "conv_s1",
        "group": "conv_s1",
        "encoding": "utf-8",
    }

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
    write_call = next(value for name, value in filesystem.calls if name == "write_file")
    assert write_call == {
        "path": "/workspace/artifacts/source.bin",
        "encoding": "utf-8",
        "mode": 640,
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
    downloaded, filename = await service.download_file(
        user,
        "s-1",
        path="artifacts/renamed.bin",
    )
    assert filename == "renamed.bin"
    assert downloaded == body

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
    first_body, _ = await first.download_file(object(), "first", path="same.txt")
    second_body, _ = await second.download_file(object(), "second", path="same.txt")
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
    filesystem.entries[f"{source_root}/notes.txt"] = _Info(
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
                runtime_manager=SimpleNamespace(get_runtime=lambda *_a, **_k: None),
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

        async def _get_or_connect_sandbox(self, *_args: Any, **_kwargs: Any):
            return _Wrapper(_Box(filesystem))

    listing = await AssistantService().list_entries(object(), "assistant-session")

    assert listing["root_path"] == "/workspace"
    assert [entry["path"] for entry in listing["entries"]] == [
        "/workspace/notes.txt"
    ]
    list_call = next(value for name, value in filesystem.calls if name == "list_directory")
    assert list_call.path == source_root


async def test_symlink_cannot_redirect_reads_but_can_be_renamed_or_deleted() -> None:
    filesystem = _Filesystem()
    filesystem.entries["/workspace/outside"] = _Info("/workspace/outside", "symlink")
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
    assert filesystem.entries["/workspace/renamed-link"].entry_type == "symlink"
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


async def test_download_is_capped_while_streaming() -> None:
    filesystem = _Filesystem()
    filesystem.entries["/workspace/growing.bin"] = _Info("/workspace/growing.bin", "file", size=0)
    filesystem.content["/workspace/growing.bin"] = b"123456789"
    service = _Service(filesystem)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(module, "_FILESYSTEM_MAX_RESPONSE_BYTES", 8)
        with pytest.raises(APIError) as raised:
            await service.download_file(object(), "s-1", path="growing.bin")
    assert raised.value.status_code == 502
    assert "host cap" in raised.value.message


async def test_filesystem_statuses_are_mapped_without_exposing_transport_details() -> None:
    filesystem = _Filesystem()
    filesystem.fail_list_with = _FilesystemError(500)
    service = _Service(filesystem)
    with pytest.raises(APIError) as raised:
        await service.list_entries(object(), "s-1")
    assert raised.value.code == "AGENT_RUNTIME_ERROR"
    assert raised.value.status_code == 502
    assert "filesystem status 500" not in raised.value.message


def test_a_handle_without_the_native_filesystem_fails_loudly() -> None:
    with pytest.raises(APIError) as raised:
        SessionFileService._resolve_filesystem(object())
    assert raised.value.status_code == 502
    assert "OpenSandbox filesystem API" in raised.value.message


def test_a_filesystem_failure_logs_the_vendors_own_words(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One vendor class covers several different failures.

    `SandboxInternalException` is raised for an unreachable box, a refused path
    and an internal fault alike, so the class alone cannot say which happened.
    It went to the log without its text, leaving an operator with a 502 that
    named the operation and nothing about why — which is what a live
    `files/list` produced.

    The log, not the response: the response is a boundary and keeps it.
    """

    class SandboxInternalException(Exception):
        pass

    with caplog.at_level("WARNING"):
        error = SessionFileService._filesystem_api_error(
            SandboxInternalException(
                "Network connectivity error: All connection attempts failed"
            ),
            op_label="file metadata lookup",
            path="/workspace",
        )

    assert error.status_code == 502
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
        SimpleNamespace(status_code=404),  # type: ignore[arg-type]
        op_label="file metadata lookup",
        path="/workspace/absent.txt",
    )

    assert missing.status_code == 404
    assert missing.code == "FILE_NOT_FOUND"
    assert "/workspace/absent.txt" in missing.message
