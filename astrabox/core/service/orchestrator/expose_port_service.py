"""Expose a sandbox port as a browser-reachable URL.

The sandbox backend already provides a browser-reachable URL per port (some backends
expose a bare per-port host; others return a signed, time-bound gateway URL, the same
approach E2B / Daytona / Modal take), so the platform just resolves it and hands it to
the agent / browser.

The agent calls the ``expose_port`` MCP tool with a port number; the platform
resolves the backend-neutral endpoint URL via ``sandbox_for_name`` and
returns it. The browser accesses the URL directly (WebSocket included — Streamlit /
Next.js hot-reload works), time-bound on gateway backends (~20min, refreshable).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.common.utils.user_context import UserContext
from astrabox.seams.sandbox import (
    SandboxBrowserEndpoint,
    sandbox_for_name,
)

logger = get_logger(__name__)


class ExposePortService:
    def __init__(
        self,
        *,
        sessions_repo: Any,
        binding_repo: Any,
        assistant_workspace_service: Any | None = None,
    ) -> None:
        self._sessions_repo = sessions_repo
        self._binding_repo = binding_repo
        self._assistant_workspace_service = assistant_workspace_service

    async def expose_port(
        self,
        *,
        deployment_id: str,
        port: int,
        title: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a browser-reachable URL for a sandbox port.

        Called by the agent via the ``expose_port`` MCP tool. The URL points at
        the sandbox backend's gateway and supports both HTTP and WebSocket.

        On a Kubernetes Secure Access deployment this is a short-lived signed
        URL. On local Docker it is execd's native ``/proxy/{port}`` URL. AstraBox
        never relays the application bytes itself.
        """
        context, endpoint = await self._resolve_browser_endpoint(
            deployment_id=deployment_id,
            port=port,
        )
        logger.info(
            "expose_port: binding=%s sandbox=%s port=%d url_len=%d",
            deployment_id,
            context["sandbox_id"],
            int(port),
            len(endpoint.endpoint),
        )
        return {
            "port": int(port),
            "url": endpoint.endpoint,
            "title": str(title or "").strip() or f"Port {port}",
            "sandbox_id": context["sandbox_id"],
            "access": "signed" if endpoint.signed else "native",
            "expires_at": (endpoint.expires_at.isoformat() if endpoint.expires_at else None),
        }

    async def refresh_port_url(
        self,
        *,
        deployment_id: str,
        port: int,
        user: UserContext,
    ) -> str:
        """Re-resolve and return a fresh URL for a sandbox port.

        Called by an authenticated browser when a signed URL has expired. The
        binding/session owner is checked before a new bearer URL is minted.
        """
        _context, endpoint = await self._resolve_browser_endpoint(
            deployment_id=deployment_id,
            port=port,
            user=user,
        )
        return endpoint.endpoint

    async def _resolve_browser_endpoint(
        self,
        *,
        deployment_id: str,
        port: int,
        user: UserContext | None = None,
    ) -> tuple[dict[str, Any], SandboxBrowserEndpoint]:
        """Resolve the native browser endpoint and reject unusable URL shapes."""
        normalized_port = int(port)
        if normalized_port < 1 or normalized_port > 65535:
            raise APIError(
                code="INVALID_REQUEST",
                message="port must be between 1 and 65535",
                status_code=400,
            )
        context = await self._resolve_binding_context(deployment_id)
        if user is not None:
            self._assert_user_can_refresh(context, user)
        sandbox_id = context["sandbox_id"]
        backend = context.get("sandbox_backend")
        settings = load_astrabox_settings()
        expires_at: datetime | None = None
        if bool(getattr(settings, "sandbox_secure_access_enabled", False)):
            expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=int(settings.sandbox_endpoint_url_ttl_seconds)
            )
        endpoint = await sandbox_for_name(backend).resolve_browser_endpoint(
            sandbox_id,
            normalized_port,
            expires_at=expires_at,
        )
        if endpoint is None or not str(endpoint.endpoint or "").strip():
            raise APIError(
                code="EXPOSE_PORT_ENDPOINT_UNAVAILABLE",
                message=(
                    f"failed to resolve endpoint for sandbox={sandbox_id} port={normalized_port}"
                ),
                status_code=502,
            )
        if endpoint.headers:
            header_names = ", ".join(sorted(str(name) for name in endpoint.headers))
            raise APIError(
                code="SANDBOX_BROWSER_ENDPOINT_REQUIRES_HEADERS",
                message=(
                    "OpenSandbox returned an endpoint that requires request headers "
                    f"({header_names}). Browser links require ingress route mode "
                    "'uri' or 'wildcard'; header mode cannot be opened directly."
                ),
                status_code=502,
            )
        url = str(endpoint.endpoint).strip()
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        return context, SandboxBrowserEndpoint(
            endpoint=url,
            headers={},
            expires_at=endpoint.expires_at,
            signed=endpoint.signed,
        )

    @staticmethod
    def _assert_user_can_refresh(context: dict[str, Any], user: UserContext) -> None:
        allowed = {
            str(context.get("user_id") or "").strip(),
            str(context.get("conversation_user_id") or "").strip(),
        }
        allowed.discard("")
        if str(user.user_id or "").strip() not in allowed:
            # Do not reveal whether another user's session/binding exists.
            raise APIError(
                code="EXPOSE_PORT_NOT_FOUND",
                message="exposed port not found",
                status_code=404,
            )

    async def _resolve_binding_context(self, deployment_id: str) -> dict[str, Any]:
        """Resolve the sandbox_id + backend from a binding or session id."""
        normalized = str(deployment_id or "").strip()
        if not normalized:
            raise APIError(
                code="BINDING_NOT_FOUND", message="deployment_id is required", status_code=400
            )
        # Assistant-workspace deployments are owned by binding records, so
        # resolve that authority before accepting a direct session id.
        binding = await self._binding_repo.get_binding(normalized)
        if isinstance(binding, dict):
            # A binding that names a Session defers to the Session's LIVE
            # placement: a prepared-slot binding freezes sandbox_id at
            # prepare, and recovery can move the claiming Session to a new
            # box while the row still names the dead one.
            bound_session = str(binding.get("session_id") or "").strip()
            if bound_session:
                session = await self._sessions_repo.get_session(bound_session)
                if isinstance(session, dict):
                    sandbox_id = str(session.get("sandbox_id") or "").strip()
                    if sandbox_id:
                        return {
                            "deployment_id": normalized,
                            "sandbox_id": sandbox_id,
                            "sandbox_backend": str(
                                session.get("sandbox_backend") or ""
                            ).strip()
                            or None,
                            "user_id": str(session.get("user_id") or "").strip(),
                            "conversation_user_id": "",
                        }
            sandbox_id = str(binding.get("sandbox_id") or "").strip()
            if sandbox_id:
                return {
                    "deployment_id": normalized,
                    "sandbox_id": sandbox_id,
                    "sandbox_backend": str(binding.get("sandbox_backend") or "").strip() or None,
                    "user_id": str(binding.get("user_id") or "").strip(),
                    "conversation_user_id": str(binding.get("conversation_user_id") or "").strip(),
                }
        # Agent-conversation deployments may address the session directly.
        session = await self._sessions_repo.get_session(normalized)
        if not session:
            raise APIError(
                code="BINDING_NOT_FOUND",
                message=f"no binding or session found for id={normalized!r}",
                status_code=404,
            )
        sandbox_id = str(session.get("sandbox_id") or "").strip()
        if not sandbox_id:
            raise APIError(
                code="SANDBOX_NOT_AVAILABLE",
                message="session has no active sandbox",
                status_code=502,
            )
        return {
            "deployment_id": normalized,
            "sandbox_id": sandbox_id,
            "sandbox_backend": str(session.get("sandbox_backend") or "").strip() or None,
            "user_id": str(session.get("user_id") or "").strip(),
            "conversation_user_id": "",
        }
