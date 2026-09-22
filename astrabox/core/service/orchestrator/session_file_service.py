from __future__ import annotations

import copy
import json
import posixpath
import shlex
from pathlib import PurePosixPath
from typing import Any

from opensandbox.models.filesystem import DirectoryListEntry, MoveEntry, WriteEntry

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.engine.capabilities import require_session_kind
from astrabox.core.service.orchestrator.runtime_binding import (
    reconcile_session_runtime_binding,
)
from astrabox.core.service.orchestrator.runtime.conversation_identity import (
    identity_file_root_source_dir,
    identity_file_root_dir,
    normalize_runtime_identity,
    plan_assistant_profile_identity,
)
from astrabox.core.service.orchestrator.runtime.diagnostics import (
    extract_command_log_text,
)
from astrabox.core.service.orchestrator.runtime.sandbox_client import (
    get_underlying_sandbox,
)

logger = get_logger(__name__)

# The public download route still returns one buffered response. Read through
# execd's streaming filesystem face, but stop before one request can consume an
# unbounded amount of API-process memory. Uploads are streamed and have no such
# response-body limit.
_FILESYSTEM_MAX_RESPONSE_BYTES = 64 * 1024 * 1024

# OpenSandbox's Filesystem SDK models Unix modes as integers containing the
# familiar octal digits (for example ``755``), then execd parses those digits
# in base 8. Python's ``0o755`` value is decimal 493 and therefore must not be
# passed to this API; values such as ``0o750`` even serialize with an invalid
# octal digit (488). Keep this wire-format quirk explicit at the adapter edge.
_FILESYSTEM_FILE_MODE = 640
_FILESYSTEM_DIRECTORY_MODE = 750


class _MappedRootFilesystem:
    """Expose one visible root through the box's physical filesystem API."""

    def __init__(self, delegate: Any, *, visible_root: str, source_root: str) -> None:
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

    def _visible_entry(self, entry: Any) -> Any:
        raw_path = str(
            entry.get("path") if isinstance(entry, dict) else getattr(entry, "path", "")
        )
        visible_path = self._to_visible(raw_path)
        if isinstance(entry, dict):
            return {**entry, "path": visible_path}
        copier = getattr(entry, "model_copy", None)
        if callable(copier):
            return copier(update={"path": visible_path})
        try:
            mapped = copy.copy(entry)
            mapped.path = visible_path
            return mapped
        except Exception as exc:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="sandbox filesystem returned an unsupported directory entry",
                status_code=502,
            ) from exc

    async def list_directory(self, entry: DirectoryListEntry) -> list[Any]:
        mapped = entry.model_copy(update={"path": self._to_source(entry.path)})
        return [self._visible_entry(item) for item in await self._delegate.list_directory(mapped)]

    async def get_file_info(self, paths: list[str]) -> dict[str, Any]:
        infos = await self._delegate.get_file_info([self._to_source(path) for path in paths])
        return {
            self._to_visible(path): self._visible_entry(info)
            for path, info in infos.items()
        }

    async def write_file(self, path: str, data: Any, **kwargs: Any) -> None:
        await self._delegate.write_file(self._to_source(path), data, **kwargs)

    async def create_directories(self, entries: list[WriteEntry]) -> None:
        await self._delegate.create_directories(
            [entry.model_copy(update={"path": self._to_source(entry.path)}) for entry in entries]
        )

    async def move_files(self, entries: list[MoveEntry]) -> None:
        await self._delegate.move_files(
            [
                entry.model_copy(
                    update={
                        "src": self._to_source(entry.src),
                        "dest": self._to_source(entry.dest),
                    }
                )
                for entry in entries
            ]
        )

    async def delete_files(self, paths: list[str]) -> None:
        await self._delegate.delete_files([self._to_source(path) for path in paths])

    async def delete_directories(self, paths: list[str]) -> None:
        await self._delegate.delete_directories([self._to_source(path) for path in paths])

    async def read_bytes_stream(self, path: str, **kwargs: Any) -> Any:
        return await self._delegate.read_bytes_stream(self._to_source(path), **kwargs)


class SessionFileService:
    _SANDBOX_CACHE_LIMIT = 64

    def __init__(
        self,
        *,
        sessions_repo: Any,
        session_snapshots_repo: Any,
        agent_repo: Any,
        runtime_manager: Any,
        assistant_workspace_service: Any | None = None,
    ) -> None:
        self._sessions_repo = sessions_repo
        self._session_snapshots_repo = session_snapshots_repo
        self._agent_repo = agent_repo
        self._runtime_manager = runtime_manager
        self._assistant_workspace_service = assistant_workspace_service
        self._connected_sandbox_by_key: dict[tuple[str, str], Any] = {}

    async def list_entries(
        self,
        user: UserContext,
        session_id: str,
        *,
        path: str | None = None,
    ) -> dict[str, Any]:
        session, sandbox, root_path = await self._resolve_session_context(user, session_id)
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
        session, sandbox, root_path = await self._resolve_session_context(user, session_id)
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
                filesystem.write_file(
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
        session, sandbox, root_path = await self._resolve_session_context(user, session_id)
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
            filesystem.create_directories(
                [
                    WriteEntry(
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
        session, sandbox, root_path = await self._resolve_session_context(user, session_id)
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
            filesystem.move_files([MoveEntry(source=src_abs, destination=dest_abs)]),
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
        session, sandbox, root_path = await self._resolve_session_context(user, session_id)
        abs_paths = [self._resolve_client_path(root_path, item) for item in paths]
        filesystem = self._resolve_filesystem(sandbox, session.get("runtime_identity"))
        file_paths: list[str] = []
        directory_paths: list[str] = []
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
            if info is None:
                continue
            if self._entry_type(info) == "directory":
                directory_paths.append(abs_path)
            else:
                file_paths.append(abs_path)
        if file_paths:
            await self._filesystem_call(
                "file deletion",
                filesystem.delete_files(list(dict.fromkeys(file_paths))),
            )
        if directory_paths:
            # Deepest-first also handles callers that selected both a directory
            # and one of its descendants.
            directories = sorted(
                dict.fromkeys(directory_paths),
                key=lambda item: item.count("/"),
                reverse=True,
            )
            await self._filesystem_call(
                "directory deletion",
                filesystem.delete_directories(directories),
            )
        return {
            "root_path": root_path,
            "paths": abs_paths,
            "deleted_count": len(set(file_paths + directory_paths)),
        }

    async def download_file(
        self,
        user: UserContext,
        session_id: str,
        *,
        path: str,
    ) -> tuple[bytes, str]:
        if not str(path or "").strip():
            raise APIError(
                code="INVALID_REQUEST",
                message="path is required",
                status_code=400,
            )
        session, sandbox, root_path = await self._resolve_session_context(user, session_id)
        target_path = self._resolve_client_path(root_path, path)
        filesystem = self._resolve_filesystem(sandbox, session.get("runtime_identity"))
        info = await self._assert_path_resolves_within_root(
            filesystem,
            root_path=root_path,
            target_path=target_path,
        )
        if self._entry_type(info) == "directory":
            raise APIError(
                code="INVALID_REQUEST",
                message=f"not a file: {target_path}",
                status_code=400,
            )
        filename = PurePosixPath(target_path).name or "download"
        content = await self._read_file_bytes(
            filesystem,
            target_path,
            known_size=self._entry_size(info),
        )
        return content, filename

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
        session, sandbox, root_path = await self._resolve_session_context(
            None, str(session.get("session_id") or ""), prefetched_session=session
        )
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
    ) -> tuple[bytes, str]:
        """Read-only file download for an already-authorized session doc (share path)."""
        if not str(path or "").strip():
            raise APIError(code="INVALID_REQUEST", message="path is required", status_code=400)
        resolved_session, sandbox, root_path = await self._resolve_session_context(
            None, str(session.get("session_id") or ""), prefetched_session=session
        )
        target_path = self._resolve_client_path(root_path, path)
        filesystem = self._resolve_filesystem(
            sandbox, resolved_session.get("runtime_identity")
        )
        info = await self._assert_path_resolves_within_root(
            filesystem, root_path=root_path, target_path=target_path
        )
        if self._entry_type(info) == "directory":
            raise APIError(
                code="INVALID_REQUEST",
                message=f"not a file: {target_path}",
                status_code=400,
            )
        filename = PurePosixPath(target_path).name or "download"
        content = await self._read_file_bytes(
            filesystem,
            target_path,
            known_size=self._entry_size(info),
        )
        return content, filename

    async def _resolve_session_context(
        self,
        user: UserContext,
        session_id: str,
        *,
        prefetched_session: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], Any, str]:
        # ``prefetched_session`` lets an already-authorized caller (e.g. the
        # share path, which verified a capability token instead of ownership)
        # skip the owner check. Normal callers pass user/session_id and the
        # owner check runs.
        session = prefetched_session or await self._must_get_owned_session(user, session_id)
        session = await self._resolve_effective_runtime_session(session)
        sandbox_id = str(session.get("sandbox_id") or "").strip()
        runtime = self._runtime_manager.get_runtime(session_id, sandbox_id=sandbox_id)
        sandbox = None
        if runtime is not None:
            sandbox = getattr(runtime, "sandbox", None)
            if sandbox is None and getattr(runtime, "agent", None) is not None:
                try:
                    sandbox = runtime.agent.sandbox()
                except Exception:
                    sandbox = None
        if sandbox is None:
            if not sandbox_id:
                raise APIError(
                    code="AGENT_RUNTIME_ERROR",
                    message="sandbox not available for file operations",
                    status_code=409,
                )
            sandbox = await self._get_or_connect_sandbox(session_id, sandbox_id)

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
            return session, sandbox, self._normalize_absolute_path(identity_root)
        engine_root = self._derive_engine_specific_root_path(session)
        if engine_root:
            return session, sandbox, self._normalize_absolute_path(engine_root)
        if session_kind == "agent_chat":
            raise APIError(
                code="CONVERSATION_IDENTITY_REQUIRED",
                message="agent_chat file operations require immutable runtime_identity.file_root_dir",
                status_code=409,
            )
        root_path = await self._resolve_session_root_path(
            session_id,
            sandbox_id=sandbox_id,
            session_kind=session_kind,
            engine_session_key=engine_session_key,
        )
        return session, sandbox, root_path

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

    async def _get_or_connect_sandbox(self, session_id: str, sandbox_id: str) -> Any:
        cache_key = (session_id, sandbox_id)
        cached = self._connected_sandbox_by_key.get(cache_key)
        if cached is not None:
            return cached

        sandbox = await self._runtime_manager.connect_sandbox_only(sandbox_id)
        self._remember_connected_sandbox(cache_key, sandbox)
        return sandbox

    def _remember_connected_sandbox(self, cache_key: tuple[str, str], sandbox: Any) -> None:
        session_id, sandbox_id = cache_key
        stale_keys = [
            key
            for key in self._connected_sandbox_by_key
            if key[0] == session_id and key[1] != sandbox_id
        ]
        for key in stale_keys:
            self._connected_sandbox_by_key.pop(key, None)

        self._connected_sandbox_by_key[cache_key] = sandbox
        while len(self._connected_sandbox_by_key) > self._SANDBOX_CACHE_LIMIT:
            oldest_key = next(iter(self._connected_sandbox_by_key))
            self._connected_sandbox_by_key.pop(oldest_key, None)

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

        snapshot = await self._session_snapshots_repo.get_snapshot(session_id)
        snapshot_cwd = str((snapshot or {}).get("terminal_cwd") or "").strip()
        if snapshot_cwd:
            return self._normalize_absolute_path(snapshot_cwd)

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
    def _resolve_command_runner(sandbox: Any) -> Any:
        command_runner = getattr(sandbox, "commands", None)
        run_fn = getattr(command_runner, "run", None)
        if command_runner is None or not callable(run_fn):
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="sandbox command runner unavailable",
                status_code=502,
            )
        return command_runner

    @staticmethod
    def _resolve_filesystem(
        sandbox: Any,
        runtime_identity: dict[str, Any] | None = None,
    ) -> Any:
        """Return the OpenSandbox execd filesystem capability for ``sandbox``."""
        underlying = get_underlying_sandbox(sandbox)
        filesystem = getattr(underlying, "files", None)
        required = (
            "list_directory",
            "get_file_info",
            "write_file",
            "create_directories",
            "move_files",
            "delete_files",
            "delete_directories",
            "read_bytes_stream",
        )
        if filesystem is None or any(
            not callable(getattr(filesystem, method, None)) for method in required
        ):
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="sandbox does not expose the OpenSandbox filesystem API",
                status_code=502,
            )
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
        if isinstance(exc, APIError):
            return exc
        try:
            status_code = int(getattr(exc, "status_code", 0) or 0)
        except (TypeError, ValueError):
            status_code = 0
        target = f": {path}" if path else ""
        if status_code == 404:
            return APIError(
                code="FILE_NOT_FOUND",
                message=f"path not found{target}",
                status_code=404,
            )
        if status_code == 400:
            return APIError(
                code="INVALID_REQUEST",
                message=f"sandbox filesystem rejected {op_label}{target}",
                status_code=400,
            )
        # The exception's own text, not only its class. One vendor class covers
        # an unreachable box, a refused path and an internal fault, so the class
        # alone cannot say which happened — and the class alone is what this
        # reported, in the log and in the response, leaving a 502 that named the
        # operation and nothing about why it failed.
        detail = str(exc).strip()
        logger.warning(
            "OpenSandbox filesystem operation failed: operation=%s path=%s "
            "status=%s error_type=%s detail=%s",
            op_label,
            path or "",
            status_code or "unknown",
            type(exc).__name__,
            detail[:400] or "<none>",
        )
        # The detail stays in the log. The response is a boundary: a 502 handed
        # to a caller must not carry the transport's own words, which is pinned
        # by its own test — and the operator reading the log is the one who
        # needs them anyway.
        return APIError(
            code="AGENT_RUNTIME_ERROR",
            message=(
                f"sandbox filesystem {op_label} failed"
                + (f": status={status_code}" if status_code else "")
            ),
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

    async def _get_entry_info(self, filesystem: Any, path: str) -> Any:
        infos = await self._filesystem_call(
            "file metadata lookup",
            filesystem.get_file_info([path]),
            path=path,
        )
        if not isinstance(infos, dict):
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="sandbox filesystem returned invalid metadata",
                status_code=502,
            )
        info = infos.get(path)
        if info is None:
            normalized = posixpath.normpath(path)
            for candidate in infos.values():
                candidate_path = str(
                    candidate.get("path")
                    if isinstance(candidate, dict)
                    else getattr(candidate, "path", "")
                )
                if posixpath.normpath(candidate_path) == normalized:
                    info = candidate
                    break
        if info is None:
            raise APIError(
                code="FILE_NOT_FOUND",
                message=f"path not found: {path}",
                status_code=404,
            )
        return info

    @staticmethod
    def _entry_type(info: Any) -> str:
        if isinstance(info, dict):
            value = info.get("entry_type", info.get("type"))
        else:
            value = getattr(info, "entry_type", None)
        return str(value or "").strip().lower()

    @staticmethod
    def _entry_size(info: Any) -> int:
        value = info.get("size") if isinstance(info, dict) else getattr(info, "size", 0)
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    async def _assert_path_resolves_within_root(
        self,
        filesystem: Any,
        *,
        root_path: str,
        target_path: str,
        expect_directory: bool = False,
        allow_missing_leaf: bool = False,
        allow_symlink_leaf: bool = False,
    ) -> Any | None:
        """Reject paths that escape the session root through a symlink.

        The public API already normalizes ``..`` lexically. This second check
        walks the existing path components with execd's lstat-style metadata,
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

        final_info: Any | None = None
        for index, prefix in enumerate(prefixes):
            is_target = index == len(prefixes) - 1
            try:
                info = await self._get_entry_info(filesystem, prefix)
            except APIError as exc:
                if exc.code == "FILE_NOT_FOUND" and allow_missing_leaf and index > 0:
                    return None
                raise

            entry_type = self._entry_type(info)
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
        filesystem: Any,
        *,
        current_path: str,
    ) -> dict[str, Any]:
        raw_entries = await self._filesystem_call(
            "directory listing",
            filesystem.list_directory(DirectoryListEntry(path=current_path, depth=1)),
            path=current_path,
        )
        if not isinstance(raw_entries, list):
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="sandbox filesystem returned an invalid directory listing",
                status_code=502,
            )

        entries: list[dict[str, Any]] = []
        for raw_entry in raw_entries:
            raw_path = str(
                raw_entry.get("path")
                if isinstance(raw_entry, dict)
                else getattr(raw_entry, "path", "")
            ).strip()
            if not raw_path:
                raise APIError(
                    code="AGENT_RUNTIME_ERROR",
                    message="sandbox filesystem returned an invalid directory entry",
                    status_code=502,
                )
            path = self._normalize_absolute_path(raw_path)
            if path == posixpath.normpath(current_path):
                continue
            if not self._is_within_root(path, current_path):
                raise APIError(
                    code="AGENT_RUNTIME_ERROR",
                    message="sandbox filesystem returned an out-of-directory entry",
                    status_code=502,
                )
            modified_at = (
                raw_entry.get("modified_at")
                if isinstance(raw_entry, dict)
                else getattr(raw_entry, "modified_at", None)
            )
            if hasattr(modified_at, "isoformat"):
                modified_at = modified_at.isoformat()
            entries.append(
                {
                    "path": path,
                    "name": PurePosixPath(path).name,
                    "kind": ("directory" if self._entry_type(raw_entry) == "directory" else "file"),
                    "size": self._entry_size(raw_entry),
                    "modified_at": str(modified_at or ""),
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

    async def _read_file_bytes(
        self,
        filesystem: Any,
        path: str,
        *,
        known_size: int,
    ) -> bytes:
        if known_size > _FILESYSTEM_MAX_RESPONSE_BYTES:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=(
                    "sandbox file download exceeds the host cap of "
                    f"{_FILESYSTEM_MAX_RESPONSE_BYTES} bytes"
                ),
                status_code=502,
            )
        stream = await self._filesystem_call(
            "file download",
            filesystem.read_bytes_stream(path),
            path=path,
        )
        chunks: list[bytes] = []
        total = 0
        try:
            async for chunk in stream:
                data = bytes(chunk)
                total += len(data)
                if total > _FILESYSTEM_MAX_RESPONSE_BYTES:
                    raise APIError(
                        code="AGENT_RUNTIME_ERROR",
                        message=(
                            "sandbox file download exceeds the host cap of "
                            f"{_FILESYSTEM_MAX_RESPONSE_BYTES} bytes"
                        ),
                        status_code=502,
                    )
                chunks.append(data)
        except APIError:
            raise
        except Exception as exc:
            raise self._filesystem_api_error(
                exc,
                op_label="file download",
                path=path,
            ) from exc
        finally:
            close = getattr(stream, "aclose", None)
            if callable(close):
                try:
                    await close()
                except Exception:
                    pass
        return b"".join(chunks)

    @classmethod
    def _resolve_chown_identity(cls, session: dict[str, Any]) -> dict[str, Any] | None:
        """Pick the Linux identity that should own files written by the file panel."""
        identity = normalize_runtime_identity(session.get("runtime_identity"))
        if identity:
            return identity
        return cls._planned_assistant_identity(session)

    # Path-safety guard script body. Reads four sys.argv values (target, root,
    # expect_directory, allow_missing_leaf) and prints either ``{ok, resolved_path}``
    # or ``{error: {code, message, status_code}}`` as a single JSON line.
    _PATH_GUARD_SCRIPT = (
        "import json\n"
        "import os\n"
        "import sys\n"
        "\n"
        "target = sys.argv[1]\n"
        "root = sys.argv[2]\n"
        "expect_directory = sys.argv[3] == '1'\n"
        "allow_missing_leaf = sys.argv[4] == '1'\n"
        "\n"
        "def within(path: str, root_path: str) -> bool:\n"
        "    normalized_path = os.path.normpath(path)\n"
        "    normalized_root = os.path.normpath(root_path)\n"
        "    return normalized_path == normalized_root or normalized_path.startswith(normalized_root.rstrip('/') + '/')\n"
        "\n"
        "result_path = sys.argv[5] if len(sys.argv) > 5 else ''\n"
        "\n"
        "def emit(payload: dict) -> None:\n"
        "    text = json.dumps(payload, ensure_ascii=False)\n"
        "    if result_path:\n"
        "        with open(result_path, 'w', encoding='utf-8') as handle:\n"
        "            handle.write(text)\n"
        "    print(text)\n"
        "\n"
        "def fail(code: str, message: str, status_code: int) -> None:\n"
        "    emit({'error': {'code': code, 'message': message, 'status_code': status_code}})\n"
        "    raise SystemExit(0)\n"
        "\n"
        "real_root = os.path.realpath(root)\n"
        "if os.path.islink(root):\n"
        "    fail('INVALID_REQUEST', f'session root must not be a symlink: {root}', 400)\n"
        "probe = target\n"
        "if os.path.lexists(target):\n"
        "    resolved = os.path.realpath(target)\n"
        "else:\n"
        "    if not allow_missing_leaf:\n"
        "        fail('FILE_NOT_FOUND', f'path not found: {target}', 404)\n"
        "    while not os.path.lexists(probe):\n"
        "        parent = os.path.dirname(probe.rstrip('/')) or '/'\n"
        "        if parent == probe:\n"
        "            fail('FILE_NOT_FOUND', f'path not found: {target}', 404)\n"
        "        probe = parent\n"
        "    resolved = os.path.realpath(probe)\n"
        "\n"
        "if not within(resolved, real_root):\n"
        "    fail('INVALID_REQUEST', f'path escapes session root: {target}', 400)\n"
        "\n"
        "if os.path.lexists(target) and expect_directory and not os.path.isdir(target):\n"
        "    fail('NOT_DIRECTORY', f'not a directory: {target}', 400)\n"
        "\n"
        "emit({'ok': True, 'resolved_path': resolved})\n"
    )

    @staticmethod
    def _build_path_guard_command(
        *,
        root_path: str,
        target_path: str,
        expect_directory: bool,
        allow_missing_leaf: bool,
    ) -> str:
        """Heredoc form for path guard checks."""
        return (
            "python - "
            f"{shlex.quote(target_path)} "
            f"{shlex.quote(root_path)} "
            f"{'1' if expect_directory else '0'} "
            f"{'1' if allow_missing_leaf else '0'} "
            "<<'PY'\n" + SessionFileService._PATH_GUARD_SCRIPT + "PY"
        )

    @staticmethod
    def _parse_guard_payload(result: Any, *, target_path: str) -> dict[str, Any]:
        raw_text = extract_command_log_text(result).strip()
        return SessionFileService._parse_guard_payload_text(raw_text, target_path=target_path)

    @staticmethod
    def _parse_guard_payload_text(raw_text: str, *, target_path: str) -> dict[str, Any]:
        if not raw_text:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=f"empty path guard result: {target_path}",
                status_code=502,
            )
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            logger.warning("invalid path guard payload for %s: %s", target_path, raw_text)
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="invalid path guard payload",
                status_code=502,
            ) from exc
        error_payload = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error_payload, dict):
            raise APIError(
                code=str(error_payload.get("code") or "AGENT_RUNTIME_ERROR"),
                message=str(error_payload.get("message") or "path guard failed"),
                status_code=int(error_payload.get("status_code") or 502),
            )
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="path guard payload missing success marker",
                status_code=502,
            )
        return payload
