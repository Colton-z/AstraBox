from __future__ import annotations

import hashlib
import json
import posixpath
import re
import shlex
from pathlib import PurePosixPath
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.engine.capabilities import require_session_kind
from astrabox.core.service.orchestrator.runtime.diagnostics import (
    extract_command_log_text,
)
from astrabox.core.service.orchestrator.runtime.conversation_identity import (
    identity_file_root_dir,
    identity_file_root_source_dir,
    identity_path_from_source,
    identity_path_to_source,
    normalize_runtime_identity,
)
from astrabox.core.service.orchestrator.runtime_binding import (
    reconcile_session_runtime_binding,
)
from astrabox.core.service.orchestrator.session_file_service import (
    SessionFileService,
)

logger = get_logger(__name__)

_ARTIFACT_TYPE = "html_preview"
_ARTIFACT_PREFIX = "html-preview:"
_NGINX_CONF = "/opt/gem/nginx.conf"
_NGINX_PREVIEW_CONF_DIR = "/opt/gem/nginx"
_NGINX_PREVIEW_MOUNT_ROOT = "/opt/gem/astrabox-previews"
_AIO_PREVIEW_PORT = 8080


class HtmlPreviewService:
    def __init__(
        self,
        *,
        sessions_repo: Any,
        session_snapshots_repo: Any,
        artifacts_repo: Any,
        runtime_manager: Any,
        binding_repo: Any,
        assistant_workspace_service: Any | None = None,
        agent_repo: Any | None = None,
    ) -> None:
        self._sessions_repo = sessions_repo
        self._session_snapshots_repo = session_snapshots_repo
        self._artifacts_repo = artifacts_repo
        self._runtime_manager = runtime_manager
        self._binding_repo = binding_repo
        self._assistant_workspace_service = assistant_workspace_service
        self._agent_repo = agent_repo

    async def publish_html_preview(
        self,
        *,
        deployment_id: str,
        path: str,
        title: str | None = None,
    ) -> dict[str, Any]:
        context = await self._resolve_binding_context(deployment_id)
        root_path = context["root_path"]
        target_path = self._resolve_absolute_file_path(root_path, path)
        runtime_identity = context.get("runtime_identity")
        source_root_path = context.get("source_root_path") or root_path
        source_target_path = identity_path_to_source(runtime_identity, target_path)
        command_runner = SessionFileService._resolve_command_runner(context["sandbox"])
        guard_result = await command_runner.run(
            SessionFileService._build_path_guard_command(
                root_path=source_root_path,
                target_path=source_target_path,
                expect_directory=False,
                allow_missing_leaf=False,
            )
        )
        guard_payload = SessionFileService._parse_guard_payload(
            guard_result,
            target_path=source_target_path,
        )
        resolved_source_file = str(guard_payload.get("resolved_path") or "").strip()
        if not resolved_source_file:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="path guard did not return resolved file path",
                status_code=502,
            )
        resolved_file = identity_path_from_source(runtime_identity, resolved_source_file)
        suffix = PurePosixPath(resolved_file).suffix.lower()
        if suffix not in {".html", ".htm"}:
            raise APIError(
                code="INVALID_REQUEST",
                message="html preview path must point to a .html or .htm file",
                status_code=400,
            )
        source_dir = posixpath.dirname(resolved_file) or root_path
        source_storage_dir = posixpath.dirname(resolved_source_file) or source_root_path
        entrypoint = PurePosixPath(resolved_file).name
        preview_id = self._preview_id(
            context["deployment_id"],
            source_dir=source_dir,
            entrypoint=entrypoint,
        )
        await self._install_nginx_preview_mapping(
            command_runner,
            source_dir=source_storage_dir,
            preview_id=preview_id,
        )
        artifact = await self._artifacts_repo.upsert_artifact(
            {
                "session_id": context["deployment_id"],
                "artifact_id": f"{_ARTIFACT_PREFIX}{preview_id}",
                "artifact_type": _ARTIFACT_TYPE,
                "deployment_id": context["deployment_id"],
                "scope_kind": context["scope_kind"],
                "user_id": context["user_id"],
                "assistant_id": context.get("assistant_id"),
                "source_dir": source_dir,
                "entrypoint": entrypoint,
                "title": str(title or "").strip() or entrypoint,
                "sandbox_id": context["sandbox_id"],
                "created_by_agent_at": utcnow_iso(),
            }
        )
        url_path = f"/api/v1/html-previews/{context['deployment_id']}/{preview_id}/{entrypoint}"
        return {
            "preview_id": preview_id,
            "title": artifact.get("title") or entrypoint,
            "entrypoint": entrypoint,
            "path": resolved_file,
            "url_path": url_path,
            "url": self._public_url(url_path),
        }

    async def resolve_preview_redirect(
        self,
        *,
        deployment_id: str,
        preview_id: str,
        asset_path: str,
        user: UserContext | None = None,
    ) -> str:
        normalized_deployment_id = str(deployment_id or "").strip()
        normalized_preview_id = str(preview_id or "").strip()
        if not normalized_deployment_id or not re.fullmatch(r"[a-f0-9]{24}", normalized_preview_id):
            raise APIError(code="PREVIEW_NOT_FOUND", message="preview not found", status_code=404)
        artifact = await self._artifacts_repo.get_artifact(
            normalized_deployment_id,
            f"{_ARTIFACT_PREFIX}{normalized_preview_id}",
        )
        if not artifact or artifact.get("artifact_type") != _ARTIFACT_TYPE:
            raise APIError(code="PREVIEW_NOT_FOUND", message="preview not found", status_code=404)
        if user is not None and str(artifact.get("user_id") or "").strip() != user.user_id:
            raise APIError(code="PREVIEW_NOT_FOUND", message="preview not found", status_code=404)
        clean_asset_path = self._clean_asset_path(asset_path)
        context = await self._resolve_binding_context(normalized_deployment_id)
        command_runner = SessionFileService._resolve_command_runner(context["sandbox"])
        source_dir = str(artifact.get("source_dir") or "").strip()
        source_storage_dir = identity_path_to_source(
            context.get("runtime_identity"), source_dir
        )
        await self._assert_source_dir_still_within_root(
            command_runner,
            root_path=context.get("source_root_path") or context["root_path"],
            source_dir=source_storage_dir,
        )
        await self._install_nginx_preview_mapping(
            command_runner,
            source_dir=source_storage_dir,
            preview_id=normalized_preview_id,
        )
        endpoint = await self._runtime_manager.resolve_enhanced_server_endpoint(
            sandbox_id=context["sandbox_id"],
            port=_AIO_PREVIEW_PORT,
        )
        endpoint = str(endpoint or "").strip()
        if not endpoint:
            raise APIError(
                code="HTML_PREVIEW_ENDPOINT_UNAVAILABLE",
                message="sandbox preview endpoint unavailable",
                status_code=502,
            )
        return f"https://{endpoint}/astrabox-preview/{normalized_preview_id}/{clean_asset_path}"

    async def _resolve_binding_context(self, deployment_id: str) -> dict[str, Any]:
        normalized_deployment_id = str(deployment_id or "").strip()
        if not normalized_deployment_id:
            raise APIError(code="MCP_BINDING_NOT_FOUND", message="MCP binding not found", status_code=404)
        binding = await self._binding_repo.get_binding(normalized_deployment_id)
        if isinstance(binding, dict):
            scope_kind = str(binding.get("scope_kind") or "").strip()
            if scope_kind == "assistant_workspace":
                return await self._resolve_assistant_workspace_context(binding)
            if scope_kind == "session":
                session_id = str(binding.get("session_id") or normalized_deployment_id).strip()
                return await self._resolve_session_context(
                    deployment_id=normalized_deployment_id,
                    session_id=session_id,
                )
            raise APIError(
                code="MCP_BINDING_INVALID",
                message=f"unsupported MCP binding scope_kind={scope_kind!r}",
                status_code=500,
            )
        return await self._resolve_session_context(
            deployment_id=normalized_deployment_id,
            session_id=normalized_deployment_id,
        )

    async def _resolve_session_context(self, *, deployment_id: str, session_id: str) -> dict[str, Any]:
        session = await self._sessions_repo.get_session(session_id)
        if not session:
            raise APIError(code="MCP_BINDING_NOT_FOUND", message="MCP binding not found", status_code=404)
        session, resolution = await reconcile_session_runtime_binding(
            session=session,
            sessions_repo=self._sessions_repo,
            agent_repo=self._agent_repo,
            assistant_workspace_service=self._assistant_workspace_service,
            persist=False,
        )
        if resolution.authority_kind != "session" and not resolution.can_dispatch:
            raise APIError(
                code=resolution.reason_code or "HTML_PREVIEW_SANDBOX_UNAVAILABLE",
                message=resolution.reason_message or "session sandbox is unavailable for html preview",
                status_code=409,
            )
        sandbox_id = str(session.get("sandbox_id") or "").strip()
        if not sandbox_id:
            raise APIError(
                code="HTML_PREVIEW_SANDBOX_UNAVAILABLE",
                message="session sandbox is unavailable for html preview",
                status_code=409,
            )
        sandbox = await self._resolve_sandbox(session_id=session_id, sandbox_id=sandbox_id)
        runtime_identity = normalize_runtime_identity(session.get("runtime_identity"))
        if (
            require_session_kind(session.get("session_kind")) == "agent_chat"
            and runtime_identity is None
        ):
            raise APIError(
                code="CONVERSATION_IDENTITY_REQUIRED",
                message="agent_chat html preview requires a complete runtime identity",
                status_code=409,
            )
        identity_root = identity_file_root_dir(runtime_identity)
        root_path = identity_root or await self._resolve_session_root_path(session)
        source_root_path = identity_file_root_source_dir(runtime_identity) or root_path
        return {
            "deployment_id": deployment_id,
            "scope_kind": "session",
            "session_id": session_id,
            "user_id": str(session.get("user_id") or "").strip(),
            "sandbox_id": sandbox_id,
            "sandbox": sandbox,
            "root_path": SessionFileService._normalize_absolute_path(root_path),
            "source_root_path": SessionFileService._normalize_absolute_path(
                source_root_path
            ),
            "runtime_identity": runtime_identity,
        }

    async def _resolve_assistant_workspace_context(self, binding: dict[str, Any]) -> dict[str, Any]:
        user_id = str(binding.get("user_id") or "").strip()
        assistant_id = str(binding.get("assistant_id") or "").strip()
        if not user_id or not assistant_id:
            raise APIError(
                code="MCP_BINDING_INVALID",
                message="assistant workspace MCP binding missing user_id or assistant_id",
                status_code=500,
            )
        workspace = await self._assistant_workspace_service.get_workspace(
            user_id=user_id,
            assistant_id=assistant_id,
        ) if self._assistant_workspace_service is not None else None
        workspace_state = str((workspace or {}).get("state") or "").strip()
        sandbox_id = str((workspace or {}).get("current_sandbox_id") or "").strip()
        if workspace_state != "READY" or not sandbox_id:
            raise APIError(
                code="HTML_PREVIEW_SANDBOX_UNAVAILABLE",
                message="assistant workspace sandbox is unavailable for html preview",
                status_code=409,
            )
        sandbox = await self._resolve_sandbox(session_id=None, sandbox_id=sandbox_id)
        root_path = str(binding.get("root_path") or "").strip()
        if not root_path:
            # Rendered from the Assistant's own runtime profile rather than
            # composed here. The hand-built copy this replaces was a second
            # source of truth for one directory, and it went wrong the moment
            # the profile's `home_template` did — invisibly, because a preview
            # rooted at a path that does not exist reads as an empty workspace.
            from astrabox.core.service.orchestrator.runtime.conversation_identity import (
                assistant_workspace_dir,
            )

            root_path = assistant_workspace_dir(user_id, assistant_id)
        return {
            "deployment_id": str(binding.get("deployment_id") or "").strip(),
            "scope_kind": "assistant_workspace",
            "assistant_id": assistant_id,
            "user_id": user_id,
            "sandbox_id": sandbox_id,
            "sandbox": sandbox,
            "root_path": SessionFileService._normalize_absolute_path(root_path),
            "source_root_path": SessionFileService._normalize_absolute_path(root_path),
            "runtime_identity": None,
        }

    async def _resolve_sandbox(self, *, session_id: str | None, sandbox_id: str) -> Any:
        runtime = self._runtime_manager.get_runtime(session_id, sandbox_id=sandbox_id) if session_id else None
        sandbox = getattr(runtime, "sandbox", None) if runtime is not None else None
        if sandbox is not None:
            return sandbox
        return await self._runtime_manager.connect_sandbox_only(sandbox_id)

    async def _resolve_session_root_path(self, session: dict[str, Any]) -> str:
        session_id = str(session.get("session_id") or "").strip()
        sandbox_id = str(session.get("sandbox_id") or "").strip()
        runtime = self._runtime_manager.get_runtime(session_id, sandbox_id=sandbox_id)
        runtime_cwd = str(getattr(runtime, "terminal_cwd", "") or "").strip() if runtime else ""
        if runtime_cwd:
            return SessionFileService._normalize_absolute_path(runtime_cwd)
        snapshot = await self._session_snapshots_repo.get_snapshot(session_id)
        snapshot_cwd = str((snapshot or {}).get("terminal_cwd") or "").strip()
        if snapshot_cwd:
            return SessionFileService._normalize_absolute_path(snapshot_cwd)
        planned = self._runtime_manager.resolve_session_terminal_cwd(
            session_id,
            sandbox_id=sandbox_id,
            session_kind=require_session_kind(session.get("session_kind")),
            engine_session_key=str(session.get("engine_session_key") or "").strip() or None,
        )
        if not str(planned or "").strip():
            raise APIError(
                code="HTML_PREVIEW_ROOT_UNAVAILABLE",
                message="session root unavailable for html preview",
                status_code=409,
            )
        return SessionFileService._normalize_absolute_path(str(planned))

    def _resolve_absolute_file_path(self, root_path: str, raw_path: str) -> str:
        text = str(raw_path or "").strip()
        if not text.startswith("/"):
            raise APIError(
                code="INVALID_REQUEST",
                message="html preview path must be an absolute sandbox path",
                status_code=400,
            )
        root = SessionFileService._normalize_absolute_path(root_path)
        resolved = SessionFileService._normalize_absolute_path(text)
        if not SessionFileService._is_within_root(resolved, root):
            raise APIError(
                code="INVALID_REQUEST",
                message=f"path escapes session root: {raw_path}",
                status_code=400,
            )
        return resolved

    async def _assert_source_dir_still_within_root(
        self,
        command_runner: Any,
        *,
        root_path: str,
        source_dir: str,
    ) -> None:
        result = await command_runner.run(
            SessionFileService._build_path_guard_command(
                root_path=root_path,
                target_path=source_dir,
                expect_directory=True,
                allow_missing_leaf=False,
            )
        )
        SessionFileService._parse_guard_payload(result, target_path=source_dir)

    async def _install_nginx_preview_mapping(
        self,
        command_runner: Any,
        *,
        source_dir: str,
        preview_id: str,
    ) -> None:
        result = await command_runner.run(
            self._build_nginx_mapping_command(
                source_dir=source_dir,
                preview_id=preview_id,
            )
        )
        output = extract_command_log_text(result).strip()
        if getattr(result, "error", None):
            raise APIError(
                code="HTML_PREVIEW_NGINX_CONFIG_FAILED",
                message=f"failed to configure html preview nginx route: {output or result.error}",
                status_code=502,
            )
        try:
            payload = json.loads(output.splitlines()[-1] if output else "")
        except Exception as exc:
            raise APIError(
                code="HTML_PREVIEW_NGINX_CONFIG_FAILED",
                message="invalid html preview nginx configure output",
                status_code=502,
            ) from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise APIError(
                code="HTML_PREVIEW_NGINX_CONFIG_FAILED",
                message=f"html preview nginx configure failed: {payload!r}",
                status_code=502,
            )

    @staticmethod
    def _build_nginx_mapping_command(*, source_dir: str, preview_id: str) -> str:
        return (
            "python - "
            f"{shlex.quote(source_dir)} "
            f"{shlex.quote(preview_id)} "
            f"{shlex.quote(_NGINX_PREVIEW_CONF_DIR)} "
            f"{shlex.quote(_NGINX_CONF)} "
            f"{shlex.quote(_NGINX_PREVIEW_MOUNT_ROOT)} "
            "<<'PY'\n"
            "import json\n"
            "import os\n"
            "import re\n"
            "import subprocess\n"
            "import sys\n"
            "\n"
            "source_dir, preview_id, conf_dir, nginx_conf, mount_root = sys.argv[1:6]\n"
            "if not re.fullmatch(r'[a-f0-9]{24}', preview_id):\n"
            "    print(json.dumps({'ok': False, 'error': 'invalid preview id'}))\n"
            "    raise SystemExit(0)\n"
            "if not os.path.isdir(source_dir):\n"
            "    print(json.dumps({'ok': False, 'error': f'source dir not found: {source_dir}'}))\n"
            "    raise SystemExit(0)\n"
            "if not os.path.isfile(nginx_conf):\n"
            "    print(json.dumps({'ok': False, 'error': f'nginx config not found: {nginx_conf}'}))\n"
            "    raise SystemExit(0)\n"
            "def fail(error):\n"
            "    print(json.dumps({'ok': False, 'error': str(error)[-1000:]}))\n"
            "    raise SystemExit(0)\n"
            "def run_checked(args):\n"
            "    result = subprocess.run(args, text=True, capture_output=True)\n"
            "    if result.returncode != 0:\n"
            "        fail(result.stderr[-1000:] or result.stdout[-1000:] or f'command failed: {args!r}')\n"
            "    return result\n"
            "real_source_dir = os.path.realpath(source_dir)\n"
            "mount_dir = os.path.join(mount_root, preview_id)\n"
            "os.makedirs(mount_root, exist_ok=True)\n"
            "if os.path.exists(mount_dir) and not os.path.isdir(mount_dir):\n"
            "    fail(f'preview mount path is not a directory: {mount_dir}')\n"
            "os.makedirs(mount_dir, exist_ok=True)\n"
            "mounted = subprocess.run(['mountpoint', '-q', mount_dir])\n"
            "if mounted.returncode == 0:\n"
            "    run_checked(['umount', mount_dir])\n"
            "run_checked(['mount', '--bind', real_source_dir, mount_dir])\n"
            "run_checked(['find', mount_dir, '-type', 'd', '-exec', 'chmod', 'a+rx', '{}', '+'])\n"
            "run_checked(['find', mount_dir, '-type', 'f', '-exec', 'chmod', 'a+r', '{}', '+'])\n"
            "run_checked(['mount', '-o', 'remount,bind,ro', mount_dir])\n"
            "def nginx_quote(value):\n"
            "    return '\"' + value.replace('\\\\', '\\\\\\\\').replace('\"', '\\\\\"') + '\"'\n"
            "alias_path = mount_dir.rstrip('/') + '/'\n"
            "conf = (\n"
            "    f'location ^~ /astrabox-preview/{preview_id}/ {{\\n'\n"
            "    f'    alias {nginx_quote(alias_path)};\\n'\n"
            "    '    autoindex off;\\n'\n"
            "    '    disable_symlinks on;\\n'\n"
            "    '}\\n'\n"
            ")\n"
            "os.makedirs(conf_dir, exist_ok=True)\n"
            "target = os.path.join(conf_dir, f'astrabox-preview-{preview_id}.conf')\n"
            "tmp = target + '.tmp'\n"
            "with open(tmp, 'w', encoding='utf-8') as fh:\n"
            "    fh.write(conf)\n"
            "os.replace(tmp, target)\n"
            "run_checked(['nginx', '-t', '-c', nginx_conf])\n"
            "run_checked(['nginx', '-s', 'reload', '-c', nginx_conf])\n"
            "print(json.dumps({'ok': True, 'conf': target}))\n"
            "PY"
        )

    @staticmethod
    def _preview_id(deployment_id: str, *, source_dir: str, entrypoint: str) -> str:
        raw = f"{deployment_id}|{source_dir}|{entrypoint}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:24]

    @staticmethod
    def _clean_asset_path(asset_path: str) -> str:
        text = str(asset_path or "").strip().replace("\\", "/")
        normalized = posixpath.normpath(text or "index.html").lstrip("/")
        if normalized in {"", "."} or normalized == ".." or normalized.startswith("../"):
            raise APIError(code="INVALID_REQUEST", message="invalid preview asset path", status_code=400)
        return normalized

    @staticmethod
    def _public_url(path: str) -> str:
        base = str(load_astrabox_settings().mcp_proxy_base_url or "").strip().rstrip("/")
        if not base:
            return path
        return f"{base}{path}"
