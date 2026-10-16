from __future__ import annotations

from astrabox.core.service.orchestrator.sandbox_viewer import mint_viewer_url

import contextlib
import dataclasses
import posixpath
from pathlib import PurePosixPath
from typing import IO, Any
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.engine.capabilities import require_session_kind
from astrabox.core.service.orchestrator.expose_port_service import ExposePortService
from astrabox.core.service.orchestrator.runtime_binding import (
    reconcile_session_runtime_binding,
)
from astrabox.core.service.orchestrator.runtime.conversation_identity import (
    identity_file_root_source_dir,
    identity_file_root_dir,
    normalize_runtime_identity,
    plan_assistant_profile_identity,
)
from astrabox.seams.sandbox import SandboxUnreachable
from astrabox.seams.sandbox_data import (
    SandboxDirectorySpec,
    SandboxEntry,
    SandboxFiles,
    SandboxPathNotFound,
    SandboxPathRejected,
)

logger = get_logger(__name__)

# What the panel writes: the conversation's account reads and writes, its group
# reads, and no other account sees the file.
_FILESYSTEM_FILE_MODE = 0o640
_FILESYSTEM_DIRECTORY_MODE = 0o750


class SessionFileStreamBroken(RuntimeError):
    """A file download failed after its response had started.

    The status line and ``Content-Length`` are already on the wire, so no
    error envelope can follow them; the server has to close the connection
    short of the promised length, which the client reads as a failed
    download. This is deliberately not an :class:`APIError`: the app's handler
    for unexpected errors logs it with the request's trace id, and the server
    then aborts the connection.
    """


class FileDownload:
    """One file in a box, opened for streaming to a client.

    Every check that can refuse the download has passed by the time this
    exists: the caller's access to the session, the path's containment and
    symlink walk, that the path names a regular file, and the box's own
    opening of it, whose first chunk is already read. ``size`` comes from the
    same metadata read as those checks.

    Iterating yields the file's bytes as they arrive from the box, once. The
    box connection stays open until :meth:`aclose`, which the response calls
    when the body ends, when the client goes away and when the request is
    cancelled.
    """

    def __init__(
        self,
        *,
        filename: str,
        size: int,
        first: bytes,
        rest: AsyncIterator[bytes],
        resources: contextlib.AsyncExitStack,
        path: str,
    ) -> None:
        self.filename = filename
        self.size = size
        #: Bytes handed to the response so far.
        self.sent = 0
        self._path = path
        self._resources = resources
        self._body = self._stream(first, rest)

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._body

    @property
    def complete(self) -> bool:
        return self.sent == self.size

    async def aclose(self) -> None:
        """Release the box connection; calling it again does nothing."""
        await self._body.aclose()
        await self._resources.aclose()

    async def _stream(
        self, first: bytes, rest: AsyncIterator[bytes]
    ) -> AsyncGenerator[bytes, None]:
        # The newest chunk is held back until the next one arrives or the box
        # ends the file. A body longer or shorter than ``size``, which means
        # the file changed after its metadata was read, then fails before the
        # client has every byte ``Content-Length`` promised, and the client
        # sees a failed download instead of a complete one.
        received = len(first)
        held = first
        if received > self.size:
            raise self._broken(f"the box sent more than the file's {self.size} bytes")
        try:
            async for chunk in rest:
                received += len(chunk)
                if received > self.size:
                    raise self._broken(f"the box sent more than the file's {self.size} bytes")
                if held:
                    yield held
                    self.sent += len(held)
                held = chunk
        except SessionFileStreamBroken:
            raise
        except Exception as exc:
            raise self._broken(f"reading from the box failed: {type(exc).__name__}") from exc
        if received != self.size:
            raise self._broken(f"the box ended the file at {received} of {self.size} bytes")
        if held:
            yield held
            self.sent += len(held)

    def _broken(self, reason: str) -> SessionFileStreamBroken:
        return SessionFileStreamBroken(
            f"file download stopped after {self.sent} of {self.size} bytes: "
            f"{reason}: path={self._path}"
        )


class _MappedRootFilesystem:
    """Expose one visible root through the box's physical files."""

    def __init__(self, delegate: SandboxFiles, *, visible_root: str, source_root: str) -> None:
        self._delegate = delegate
        self._visible_root = posixpath.normpath(visible_root)
        self._source_root = posixpath.normpath(source_root)

    def _to_source(self, path: str) -> str:
        visible = posixpath.normpath(str(path or ""))
        if not SessionFileService._is_within_root(visible, self._visible_root):
            raise APIError(
                code="INVALID_REQUEST",
                message=f"path escapes session root: {path}",
                status_code=400,
            )
        relative = posixpath.relpath(visible, self._visible_root)
        return (
            self._source_root
            if relative == "."
            else posixpath.normpath(posixpath.join(self._source_root, relative))
        )

    def _to_visible(self, path: str) -> str:
        source = posixpath.normpath(str(path or ""))
        if not SessionFileService._is_within_root(source, self._source_root):
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="sandbox filesystem returned a path outside the workspace source",
                status_code=502,
            )
        relative = posixpath.relpath(source, self._source_root)
        return (
            self._visible_root
            if relative == "."
            else posixpath.normpath(posixpath.join(self._visible_root, relative))
        )

    def _visible_entry(self, entry: SandboxEntry) -> SandboxEntry:
        return dataclasses.replace(entry, path=self._to_visible(entry.path))

    async def read_bytes(self, path: str) -> bytes:
        return await self._delegate.read_bytes(self._to_source(path))

    def read_chunks(self, path: str) -> AsyncGenerator[bytes, None]:
        return self._delegate.read_chunks(self._to_source(path))

    async def write_bytes(
        self,
        path: str,
        data: bytes | IO[bytes],
        *,
        mode: int = 0o644,
        owner: str | None = None,
        group: str | None = None,
    ) -> None:
        await self._delegate.write_bytes(
            self._to_source(path), data, mode=mode, owner=owner, group=group
        )

    async def make_directories(self, specs: Sequence[SandboxDirectorySpec]) -> None:
        await self._delegate.make_directories(
            [dataclasses.replace(spec, path=self._to_source(spec.path)) for spec in specs]
        )

    async def stat(self, paths: Sequence[str]) -> Mapping[str, SandboxEntry]:
        asked = {self._to_source(path): path for path in paths}
        entries = await self._delegate.stat(list(asked))
        return {asked[source]: self._visible_entry(entry) for source, entry in entries.items()}

    async def list_directory(self, path: str) -> Sequence[SandboxEntry]:
        return [
            self._visible_entry(entry)
            for entry in await self._delegate.list_directory(self._to_source(path))
        ]

    async def search(self, root: str, pattern: str) -> Sequence[str]:
        return [
            self._to_visible(found)
            for found in await self._delegate.search(self._to_source(root), pattern)
        ]

    async def move(self, source: str, destination: str) -> None:
        await self._delegate.move(self._to_source(source), self._to_source(destination))

    async def remove(self, paths: Sequence[str]) -> None:
        await self._delegate.remove([self._to_source(path) for path in paths])


class SessionFileService:
    def __init__(
        self,
        *,
        sessions_repo: Any,
        agent_repo: Any,
        runtime_manager: Any,
        assistant_workspace_service: Any | None = None,
    ) -> None:
        self._sessions_repo = sessions_repo
        self._agent_repo = agent_repo
        self._runtime_manager = runtime_manager
        self._assistant_workspace_service = assistant_workspace_service

    async def list_entries(
        self,
        user: UserContext,
        session_id: str,
        *,
        path: str | None = None,
    ) -> dict[str, Any]:
        async with self._resolve_session_context(user, session_id) as (session, sandbox, root_path):
            current_path = self._resolve_client_path(root_path, path)
            filesystem = self._resolve_filesystem(sandbox, session.get("runtime_identity"))
            await self._assert_path_resolves_within_root(
                filesystem,
                root_path=root_path,
                target_path=current_path,
                expect_directory=True,
            )
            payload = await self._list_entries_via_filesystem(
                filesystem,
                current_path=current_path,
            )
            return {
                "root_path": root_path,
                "current_path": current_path,
                "parent_path": self._parent_path(root_path, current_path),
                "entries": payload["entries"],
                "session_kind": require_session_kind(session.get("session_kind")),
            }

    async def upload_files(
        self,
        user: UserContext,
        session_id: str,
        *,
        path: str | None,
        files: list[Any],
    ) -> dict[str, Any]:
        if not files:
            raise APIError(
                code="INVALID_REQUEST",
                message="files are required",
                status_code=400,
            )
        async with self._resolve_session_context(user, session_id) as (session, sandbox, root_path):
            target_dir = self._resolve_client_path(root_path, path)
            filesystem = self._resolve_filesystem(sandbox, session.get("runtime_identity"))
            await self._assert_path_resolves_within_root(
                filesystem,
                root_path=root_path,
                target_path=target_dir,
                expect_directory=True,
            )
            uploaded: list[dict[str, Any]] = []
            for upload in files:
                filename = PurePosixPath(str(getattr(upload, "filename", "") or "").strip()).name
                if not filename:
                    raise APIError(
                        code="INVALID_REQUEST",
                        message="upload filename is required",
                        status_code=400,
                    )
                destination = self._join_and_validate(target_dir, filename, root_path=root_path)
                await self._assert_path_resolves_within_root(
                    filesystem,
                    root_path=root_path,
                    target_path=destination,
                    allow_missing_leaf=True,
                )
                file_obj = getattr(upload, "file", None)
                if file_obj is None:
                    raise APIError(
                        code="INVALID_REQUEST",
                        message=f"upload file handle missing: {filename}",
                        status_code=400,
                    )
                try:
                    file_obj.seek(0)
                except Exception:
                    pass
                ownership = self._filesystem_ownership(session)
                await self._filesystem_call(
                    "file upload",
                    filesystem.write_bytes(
                        destination,
                        file_obj,
                        mode=_FILESYSTEM_FILE_MODE,
                        owner=ownership.get("owner"),
                        group=ownership.get("group"),
                    ),
                    path=destination,
                )
                uploaded.append(
                    {
                        "path": destination,
                        "name": filename,
                        "kind": "file",
                    }
                )
            return {
                "root_path": root_path,
                "current_path": target_dir,
                "parent_path": self._parent_path(root_path, target_dir),
                "entries": uploaded,
                "uploaded_count": len(uploaded),
            }

    async def create_directory(
        self,
        user: UserContext,
        session_id: str,
        *,
        path: str,
    ) -> dict[str, Any]:
        if not str(path or "").strip():
            raise APIError(
                code="INVALID_REQUEST",
                message="path is required",
                status_code=400,
            )
        async with self._resolve_session_context(user, session_id) as (session, sandbox, root_path):
            target_path = self._resolve_client_path(root_path, path)
            filesystem = self._resolve_filesystem(sandbox, session.get("runtime_identity"))
            await self._assert_path_resolves_within_root(
                filesystem,
                root_path=root_path,
                target_path=target_path,
                expect_directory=True,
                allow_missing_leaf=True,
            )
            ownership = self._filesystem_ownership(session)
            await self._filesystem_call(
                "directory creation",
                filesystem.make_directories(
                    [
                        SandboxDirectorySpec(
                            path=target_path,
                            mode=_FILESYSTEM_DIRECTORY_MODE,
                            owner=ownership.get("owner"),
                            group=ownership.get("group"),
                        )
                    ]
                ),
                path=target_path,
            )
            return {
                "root_path": root_path,
                "current_path": target_path,
                "parent_path": self._parent_path(root_path, target_path),
                "path": target_path,
            }

    async def move_path(
        self,
        user: UserContext,
        session_id: str,
        *,
        src_path: str,
        dest_path: str,
    ) -> dict[str, Any]:
        if not str(src_path or "").strip() or not str(dest_path or "").strip():
            raise APIError(
                code="INVALID_REQUEST",
                message="src_path and dest_path are required",
                status_code=400,
            )
        async with self._resolve_session_context(user, session_id) as (session, sandbox, root_path):
            src_abs = self._resolve_client_path(root_path, src_path)
            dest_abs = self._resolve_client_path(root_path, dest_path)
            self._assert_not_session_root(src_abs, root_path, operation="move")
            self._assert_not_session_root(dest_abs, root_path, operation="move")
            filesystem = self._resolve_filesystem(sandbox, session.get("runtime_identity"))
            await self._assert_path_resolves_within_root(
                filesystem,
                root_path=root_path,
                target_path=src_abs,
                allow_symlink_leaf=True,
            )
            await self._assert_path_resolves_within_root(
                filesystem,
                root_path=root_path,
                target_path=dest_abs,
                allow_missing_leaf=True,
            )
            await self._filesystem_call(
                "file move",
                filesystem.move(src_abs, dest_abs),
                path=src_abs,
            )
            return {
                "root_path": root_path,
                "src_path": src_abs,
                "dest_path": dest_abs,
            }

    async def delete_paths(
        self,
        user: UserContext,
        session_id: str,
        *,
        paths: list[str],
    ) -> dict[str, Any]:
        if not paths:
            raise APIError(
                code="INVALID_REQUEST",
                message="paths are required",
                status_code=400,
            )
        async with self._resolve_session_context(user, session_id) as (session, sandbox, root_path):
            abs_paths = [self._resolve_client_path(root_path, item) for item in paths]
            filesystem = self._resolve_filesystem(sandbox, session.get("runtime_identity"))
            present: list[str] = []
            for abs_path in abs_paths:
                self._assert_not_session_root(abs_path, root_path, operation="delete")
                info = await self._assert_path_resolves_within_root(
                    filesystem,
                    root_path=root_path,
                    target_path=abs_path,
                    allow_missing_leaf=True,
                    allow_symlink_leaf=True,
                )
                # Deletion is a convergence command: a retry after a lost response
                # succeeds once every requested target is absent. Existing parent
                # components were still walked above, so a symlink cannot turn the
                # missing leaf into an escape from the session root.
                if info is not None:
                    present.append(abs_path)
            targets = list(dict.fromkeys(present))
            if targets:
                # Removal is recursive and unlinks a symlink without following
                # it, so a selection holding a directory and one of its
                # descendants needs no ordering.
                await self._filesystem_call("deletion", filesystem.remove(targets))
            return {
                "root_path": root_path,
                "paths": abs_paths,
                "deleted_count": len(targets),
            }

    async def download_file(
        self,
        user: UserContext,
        session_id: str,
        *,
        path: str,
    ) -> FileDownload:
        """Open a file of the owner's session for streaming; see :class:`FileDownload`."""
        return await self._open_download(user, session_id, path=path)

    async def preview_file(
        self,
        user: UserContext,
        session_id: str,
        *,
        path: str,
        expose_port: ExposePortService,
    ) -> dict[str, Any]:
        """Authorize the Files root and mint a short-lived static resource URL."""
        async with self._resolve_session_context(user, session_id) as (session, sandbox, root):
            target = self._resolve_client_path(root, path)
            filesystem = self._resolve_filesystem(sandbox, session.get("runtime_identity"))
            info = await self._assert_path_resolves_within_root(
                filesystem, root_path=root, target_path=target,
            )
            if info is None or info.kind != "file":
                raise APIError(code="INVALID_REQUEST", message="preview requires a file", status_code=400)
            endpoint = await expose_port.expose_port(deployment_id=session_id, port=8002)
            source_root = identity_file_root_source_dir(session.get("runtime_identity")) or root
            return await mint_viewer_url(
                sandbox, endpoint,
                arguments=["grant", "--", source_root, posixpath.relpath(target, root)],
                prefix="p/", error_code="FILE_PREVIEW_UNAVAILABLE",
            )

    async def list_entries_for_session(
        self,
        session: dict[str, Any],
        *,
        path: str | None = None,
    ) -> dict[str, Any]:
        """Read-only directory listing for an already-authorized session doc.

        Used by the share path: the caller verified a capability token and the
        session's share config, so no owner check is performed here.
        """
        async with self._resolve_session_context(
            None, str(session.get("session_id") or ""), prefetched_session=session
        ) as (session, sandbox, root_path):
            current_path = self._resolve_client_path(root_path, path)
            filesystem = self._resolve_filesystem(sandbox, session.get("runtime_identity"))
            await self._assert_path_resolves_within_root(
                filesystem,
                root_path=root_path,
                target_path=current_path,
                expect_directory=True,
            )
            payload = await self._list_entries_via_filesystem(filesystem, current_path=current_path)
            return {
                "root_path": root_path,
                "current_path": current_path,
                "parent_path": self._parent_path(root_path, current_path),
                "entries": payload["entries"],
                "session_kind": require_session_kind(session.get("session_kind")),
            }

    async def download_file_for_session(
        self,
        session: dict[str, Any],
        *,
        path: str,
    ) -> FileDownload:
        """Read-only file download for an already-authorized session doc (share path)."""
        return await self._open_download(
            None, str(session.get("session_id") or ""), path=path, prefetched_session=session
        )

    async def _open_download(
        self,
        user: UserContext | None,
        session_id: str,
        *,
        path: str,
        prefetched_session: dict[str, Any] | None = None,
    ) -> FileDownload:
        """Run every check a download can fail, then open the file in the box.

        The box connection the session context opens is handed to the returned
        download rather than closed here, so it outlives this call; a refusal
        closes it before raising.
        """
        if not str(path or "").strip():
            raise APIError(
                code="INVALID_REQUEST",
                message="path is required",
                status_code=400,
            )
        async with contextlib.AsyncExitStack() as stack:
            session, sandbox, root_path = await stack.enter_async_context(
                self._resolve_session_context(
                    user, session_id, prefetched_session=prefetched_session
                )
            )
            target_path = self._resolve_client_path(root_path, path)
            filesystem = self._resolve_filesystem(sandbox, session.get("runtime_identity"))
            info = await self._assert_path_resolves_within_root(
                filesystem,
                root_path=root_path,
                target_path=target_path,
            )
            if info is None or info.kind != "file":
                raise APIError(
                    code="INVALID_REQUEST",
                    message=f"not a file: {target_path}",
                    status_code=400,
                )
            chunks = await stack.enter_async_context(
                contextlib.aclosing(filesystem.read_chunks(target_path))
            )
            # The box reports a failure to open the file, such as the file
            # being removed after the metadata read, only once iteration
            # starts. Reading the first chunk here turns that into a refusal
            # the client still receives as an error response.
            try:
                first = await anext(chunks, b"")
            except Exception as exc:
                raise self._filesystem_api_error(
                    exc, op_label="file download", path=target_path
                ) from exc
            return FileDownload(
                filename=PurePosixPath(target_path).name or "download",
                size=info.size,
                first=bytes(first),
                rest=chunks,
                resources=stack.pop_all(),
                path=target_path,
            )

    @asynccontextmanager
    async def _resolve_session_context(
        self,
        user: UserContext,
        session_id: str,
        *,
        prefetched_session: dict[str, Any] | None = None,
    ) -> AsyncIterator[tuple[dict[str, Any], Any, str]]:
        # ``prefetched_session`` lets an already-authorized caller (e.g. the
        # share path, which verified a capability token instead of ownership)
        # skip the owner check. Normal callers pass user/session_id and the
        # owner check runs.
        session = prefetched_session or await self._must_get_owned_session(user, session_id)
        session = await self._resolve_effective_runtime_session(session)
        sandbox_id = str(session.get("sandbox_id") or "").strip()
        if not sandbox_id:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="sandbox not available for file operations",
                status_code=409,
            )
        if session.get("sandbox_parked_at"):
            await self._runtime_manager.wake_parked_session(session)

        session_kind = require_session_kind(session.get("session_kind"))
        engine_session_key = str(session.get("engine_session_key") or "").strip() or None
        runtime_identity = session.get("runtime_identity")
        if not isinstance(runtime_identity, dict):
            runtime_identity = self._planned_assistant_identity(session)
            if isinstance(runtime_identity, dict):
                # Assistant workspace identity belongs to the owner row and is
                # intentionally not persisted on each conversation. File
                # operations still need its logical-to-physical path mapping.
                session = {**session, "runtime_identity": runtime_identity}
        identity_root = identity_file_root_dir(runtime_identity)
        if identity_root:
            root_path = self._normalize_absolute_path(identity_root)
        elif engine_root := self._derive_engine_specific_root_path(session):
            root_path = self._normalize_absolute_path(engine_root)
        elif session_kind == "agent_chat":
            raise APIError(
                code="CONVERSATION_IDENTITY_REQUIRED",
                message="agent_chat file operations require immutable runtime_identity.file_root_dir",
                status_code=409,
            )
        else:
            root_path = await self._resolve_session_root_path(
                session_id,
                sandbox_id=sandbox_id,
                session_kind=session_kind,
                engine_session_key=engine_session_key,
            )
        # A resume keeps the ID but replaces the endpoint. Resolve it for each
        # operation, including when another worker or a turn resumed the box.
        sandbox = await self._runtime_manager.connect_sandbox_only(sandbox_id)
        try:
            yield session, sandbox, root_path
        finally:
            await sandbox.close()

    @staticmethod
    def _planned_assistant_identity(
        session: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Plan a shared Assistant profile from the selected engine declaration."""

        if str(session.get("session_kind") or "").strip() != "assistant_chat":
            return None
        workspace_ref = session.get("workspace_ref") or {}
        if not isinstance(workspace_ref, dict) or str(
            workspace_ref.get("kind") or ""
        ).strip() != "assistant":
            raise APIError(
                code="SESSION_RUNTIME_IDENTITY_INVALID",
                message="assistant session has no assistant workspace reference",
                status_code=500,
            )
        kind = str(workspace_ref.get("kind") or "").strip()
        engine_kind = str(workspace_ref.get("engine_kind") or "").strip()
        user_id = str(workspace_ref.get("user_id") or "").strip()
        assistant_id = str(workspace_ref.get("assistant_id") or "").strip()
        if kind != "assistant" or not engine_kind or not user_id or not assistant_id:
            raise APIError(
                code="SESSION_RUNTIME_IDENTITY_INVALID",
                message="assistant workspace identity is incomplete",
                status_code=500,
            )
        try:
            return plan_assistant_profile_identity(
                engine_kind=engine_kind,
                user_id=user_id,
                assistant_id=assistant_id,
                sandbox_id=str(workspace_ref.get("sandbox_id") or "").strip() or None,
            )
        except (KeyError, ValueError) as exc:
            raise APIError(
                code="SESSION_RUNTIME_IDENTITY_INVALID",
                message=str(exc),
                status_code=500,
            ) from exc

    @classmethod
    def _derive_engine_specific_root_path(cls, session: dict[str, Any]) -> str | None:
        """Declared file root when a shared profile has no persisted identity."""

        identity = cls._planned_assistant_identity(session)
        return identity_file_root_dir(identity) if isinstance(identity, dict) else None

    async def _resolve_effective_runtime_session(self, session: dict[str, Any]) -> dict[str, Any]:
        reconciled, _ = await reconcile_session_runtime_binding(
            session=session,
            sessions_repo=self._sessions_repo,
            agent_repo=self._agent_repo,
            assistant_workspace_service=self._assistant_workspace_service,
            persist=False,
        )
        return reconciled

    async def _must_get_owned_session(
        self,
        user: UserContext,
        session_id: str,
    ) -> dict[str, Any]:
        session = await self._sessions_repo.get_session(session_id)
        if session is None or str(session.get("user_id") or "") != user.user_id:
            raise APIError(code="SESSION_NOT_FOUND", message="session not found", status_code=404)
        return session

    async def _resolve_session_root_path(
        self,
        session_id: str,
        *,
        sandbox_id: str | None,
        session_kind: str,
        engine_session_key: str | None,
    ) -> str:
        runtime = self._runtime_manager.get_runtime(session_id, sandbox_id=sandbox_id)
        runtime_cwd = (
            str(getattr(runtime, "terminal_cwd", "") or "").strip() if runtime is not None else ""
        )
        if runtime_cwd:
            return self._normalize_absolute_path(runtime_cwd)

        session = await self._sessions_repo.get_session(session_id)
        stored_cwd = str((session or {}).get("terminal_cwd") or "").strip()
        if stored_cwd:
            return self._normalize_absolute_path(stored_cwd)

        planned = self._runtime_manager.resolve_session_terminal_cwd(
            session_id,
            sandbox_id=sandbox_id,
            session_kind=session_kind,
            engine_session_key=engine_session_key,
        )
        planned_cwd = str(planned or "").strip()
        if not planned_cwd:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="session cwd unavailable for file operations",
                status_code=409,
            )
        return self._normalize_absolute_path(planned_cwd)

    @staticmethod
    def _normalize_absolute_path(path: str) -> str:
        normalized = posixpath.normpath(str(path or "").strip() or "/")
        if not normalized.startswith("/"):
            normalized = f"/{normalized.lstrip('/')}"
        return normalized or "/"

    def _resolve_client_path(self, root_path: str, raw_path: str | None) -> str:
        root = self._normalize_absolute_path(root_path)
        value = str(raw_path or "").strip().replace("\\", "/")
        if not value or value == ".":
            return root
        if value.startswith("/"):
            resolved = self._normalize_absolute_path(value)
        else:
            normalized_rel = posixpath.normpath(value)
            if normalized_rel in {".."} or normalized_rel.startswith("../"):
                raise APIError(
                    code="INVALID_REQUEST",
                    message=f"path escapes session root: {raw_path}",
                    status_code=400,
                )
            resolved = self._normalize_absolute_path(posixpath.join(root, normalized_rel))
        if not self._is_within_root(resolved, root):
            raise APIError(
                code="INVALID_REQUEST",
                message=f"path escapes session root: {raw_path}",
                status_code=400,
            )
        return resolved

    def _join_and_validate(self, base_path: str, name: str, *, root_path: str) -> str:
        clean_name = PurePosixPath(name).name
        if not clean_name:
            raise APIError(
                code="INVALID_REQUEST",
                message="path name is required",
                status_code=400,
            )
        return self._resolve_client_path(root_path, posixpath.join(base_path, clean_name))

    @staticmethod
    def _is_within_root(path: str, root_path: str) -> bool:
        normalized_path = posixpath.normpath(path)
        normalized_root = posixpath.normpath(root_path)
        return normalized_path == normalized_root or normalized_path.startswith(
            f"{normalized_root.rstrip('/')}/"
        )

    @staticmethod
    def _parent_path(root_path: str, current_path: str) -> str | None:
        normalized_root = posixpath.normpath(root_path)
        normalized_current = posixpath.normpath(current_path)
        if normalized_current == normalized_root:
            return None
        candidate = posixpath.dirname(normalized_current)
        if candidate == normalized_root or candidate.startswith(f"{normalized_root.rstrip('/')}/"):
            return candidate
        return None

    @staticmethod
    def _resolve_filesystem(
        sandbox: Any,
        runtime_identity: dict[str, Any] | None = None,
    ) -> SandboxFiles:
        """The box's files, seen through the conversation's visible root.

        ``sandbox`` is a provider's :class:`astrabox.seams.sandbox.SandboxHandle`.
        A conversation whose visible root differs from where its files live
        gets a mapping that translates every path both ways.
        """
        filesystem: SandboxFiles = sandbox.files
        visible_root = identity_file_root_dir(runtime_identity)
        source_root = identity_file_root_source_dir(runtime_identity)
        if visible_root and source_root and visible_root != source_root:
            return _MappedRootFilesystem(
                filesystem,
                visible_root=visible_root,
                source_root=source_root,
            )
        return filesystem

    @staticmethod
    def _filesystem_api_error(
        exc: BaseException,
        *,
        op_label: str,
        path: str | None = None,
    ) -> APIError:
        """Answer a files failure in the API's own terms.

        The box's and the transport's words go to the log only. The response is
        a boundary: they can name the box's physical paths, which a mapped
        conversation root keeps from its user, and the operator reading the log
        is the one who needs them.
        """
        target = f": {path}" if path else ""
        if isinstance(exc, SandboxPathNotFound):
            return APIError(
                code="FILE_NOT_FOUND",
                message=f"path not found{target}",
                status_code=404,
            )
        if isinstance(exc, SandboxUnreachable):
            logger.warning(
                "sandbox filesystem operation could not reach the box: operation=%s "
                "path=%s detail=%s",
                op_label,
                path or "",
                exc.message[:400],
            )
            return APIError(
                code="SANDBOX_UNREACHABLE",
                message=f"sandbox filesystem {op_label} could not reach the box",
                status_code=503,
            )
        if isinstance(exc, APIError):
            return exc
        detail = str(exc).strip()
        if isinstance(exc, SandboxPathRejected):
            logger.warning(
                "sandbox filesystem operation refused: operation=%s path=%s reason=%s",
                op_label,
                path or "",
                detail[:400] or "<none>",
            )
            return APIError(
                code="AGENT_RUNTIME_ERROR",
                message=f"sandbox filesystem {op_label} was refused{target}",
                status_code=502,
            )
        logger.warning(
            "sandbox filesystem operation failed: operation=%s path=%s "
            "error_type=%s detail=%s",
            op_label,
            path or "",
            type(exc).__name__,
            detail[:400] or "<none>",
        )
        return APIError(
            code="AGENT_RUNTIME_ERROR",
            message=f"sandbox filesystem {op_label} failed",
            status_code=502,
        )

    async def _filesystem_call(
        self,
        op_label: str,
        operation: Any,
        *,
        path: str | None = None,
    ) -> Any:
        try:
            return await operation
        except Exception as exc:
            raise self._filesystem_api_error(
                exc,
                op_label=op_label,
                path=path,
            ) from exc

    async def _get_entry_info(self, filesystem: SandboxFiles, path: str) -> SandboxEntry:
        entries = await self._filesystem_call(
            "file metadata lookup",
            filesystem.stat([path]),
            path=path,
        )
        entry = entries.get(path)
        if entry is None:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=f"sandbox filesystem answered without the entry for {path}",
                status_code=502,
            )
        return entry

    async def _assert_path_resolves_within_root(
        self,
        filesystem: SandboxFiles,
        *,
        root_path: str,
        target_path: str,
        expect_directory: bool = False,
        allow_missing_leaf: bool = False,
        allow_symlink_leaf: bool = False,
    ) -> SandboxEntry | None:
        """Reject paths that escape the session root through a symlink.

        The public API already normalizes ``..`` lexically. This second check
        walks the existing path components with the box's lstat metadata,
        so an in-root component cannot redirect a later filesystem operation
        outside the session directory. A missing tail is allowed for create,
        upload and move destinations; its last existing parent is still checked.
        """
        root = self._normalize_absolute_path(root_path)
        target = self._normalize_absolute_path(target_path)
        if not self._is_within_root(target, root):
            raise APIError(
                code="INVALID_REQUEST",
                message=f"path escapes session root: {target_path}",
                status_code=400,
            )

        relative = posixpath.relpath(target, root)
        prefixes = [root]
        if relative != ".":
            current = root
            for part in PurePosixPath(relative).parts:
                current = self._normalize_absolute_path(posixpath.join(current, part))
                prefixes.append(current)

        final_info: SandboxEntry | None = None
        for index, prefix in enumerate(prefixes):
            is_target = index == len(prefixes) - 1
            try:
                info = await self._get_entry_info(filesystem, prefix)
            except APIError as exc:
                if exc.code == "FILE_NOT_FOUND" and allow_missing_leaf and index > 0:
                    return None
                raise

            entry_type = info.kind
            if entry_type == "symlink":
                if is_target and prefix != root and allow_symlink_leaf:
                    return info
                raise APIError(
                    code="INVALID_REQUEST",
                    message=f"path contains a symlink: {prefix}",
                    status_code=400,
                )
            if (not is_target or prefix == root) and entry_type != "directory":
                raise APIError(
                    code="NOT_DIRECTORY",
                    message=f"not a directory: {prefix}",
                    status_code=400,
                )
            if is_target and expect_directory and entry_type != "directory":
                raise APIError(
                    code="NOT_DIRECTORY",
                    message=f"not a directory: {prefix}",
                    status_code=400,
                )
            final_info = info
        return final_info

    @staticmethod
    def _assert_not_session_root(path: str, root_path: str, *, operation: str) -> None:
        if posixpath.normpath(path) == posixpath.normpath(root_path):
            raise APIError(
                code="INVALID_REQUEST",
                message=f"cannot {operation} the session root",
                status_code=400,
            )

    async def _list_entries_via_filesystem(
        self,
        filesystem: SandboxFiles,
        *,
        current_path: str,
    ) -> dict[str, Any]:
        listed = await self._filesystem_call(
            "directory listing",
            filesystem.list_directory(current_path),
            path=current_path,
        )

        entries: list[dict[str, Any]] = []
        for entry in listed:
            path = self._normalize_absolute_path(entry.path)
            if path == posixpath.normpath(current_path):
                continue
            if not self._is_within_root(path, current_path):
                raise APIError(
                    code="AGENT_RUNTIME_ERROR",
                    message="sandbox filesystem returned an out-of-directory entry",
                    status_code=502,
                )
            entries.append(
                {
                    "path": path,
                    "name": PurePosixPath(path).name,
                    "kind": "directory" if entry.kind == "directory" else "file",
                    "size": entry.size,
                    "modified_at": entry.modified_at.isoformat() if entry.modified_at else "",
                }
            )
        entries.sort(key=lambda item: (item["kind"] != "directory", item["name"].lower()))
        return {"entries": entries}

    @staticmethod
    def _filesystem_ownership(session: dict[str, Any]) -> dict[str, str | None]:
        identity = SessionFileService._resolve_chown_identity(session)
        linux_user = str((identity or {}).get("linux_user") or "").strip()
        if not linux_user:
            return {"owner": None, "group": None}
        # Both identity bootstraps create a same-name primary group. Execd's
        # filesystem API accepts account names, not numeric uid/gid values.
        return {"owner": linux_user, "group": linux_user}

    @classmethod
    def _resolve_chown_identity(cls, session: dict[str, Any]) -> dict[str, Any] | None:
        """Pick the Linux identity that should own files written by the file panel."""
        identity = normalize_runtime_identity(session.get("runtime_identity"))
        if identity:
            return identity
        return cls._planned_assistant_identity(session)
