"""Authorize desktop access to a sandbox owned by the caller's task."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.runtime_binding import is_assistant_user_conversation
from astrabox.core.service.orchestrator.sandbox_viewer import mint_viewer_url


class SessionBrowserService:
    def __init__(self, *, session_service: Any, runtime_manager: Any, expose_port_service: Any) -> None:
        self._sessions = session_service
        self._runtime = runtime_manager
        self._ports = expose_port_service

    async def open_browser(self, user: UserContext, session_id: str) -> dict[str, str]:
        session = await self._sessions.must_get_owned_session(user, session_id)
        identity = session.get("runtime_identity") or {}
        assistant_workspace = is_assistant_user_conversation(session)
        if not assistant_workspace and (
            session.get("session_kind") != "agent_chat"
            or identity.get("sandbox_tenancy") != "conversation"
        ):
            raise APIError(
                code="BROWSER_TENANCY_UNSUPPORTED",
                message="Browser requires a conversation-owned sandbox or an owner-only Assistant workspace",
                status_code=409,
            )
        if session.get("sandbox_parked_at"):
            await self._runtime.wake_parked_session(session)
            session = await self._sessions.must_get_owned_session(user, session_id)
        sandbox_id = str(session.get("sandbox_id") or "").strip()
        if not assistant_workspace and not sandbox_id:
            raise APIError(code="BROWSER_UNAVAILABLE", message="sandbox browser is unavailable", status_code=409)
        # Endpoint resolution reads the current workspace, so a conversation's
        # cached sandbox cannot grant access to its previous allocation.
        endpoint = await self._ports.expose_port(deployment_id=session_id, port=8003)
        sandbox_id = endpoint["sandbox_id"]
        sandbox = await self._runtime.connect_sandbox_only(sandbox_id)
        try:
            result = await mint_viewer_url(
                sandbox, endpoint, arguments=["grant-browser"], prefix="b/",
                error_code="BROWSER_UNAVAILABLE",
            )
        finally:
            await sandbox.close()
        target = urlsplit(result["url"])
        # The image's noVNC 1.4 constructs ws(s)://host:port/ + path.
        # Supply connection settings because noVNC persists earlier values.
        websocket = f"{target.path.rsplit('/', 1)[0]}/ws".lstrip("/")
        if target.query:
            websocket += f"?{target.query}"
        host = str(target.hostname or "")
        if ":" in host:
            host = f"[{host}]"
        query = urlencode([
            *parse_qsl(target.query), ("host", host),
            ("port", str(target.port or (443 if target.scheme == "https" else 80))),
            ("encrypt", "true" if target.scheme == "https" else "false"),
            ("autoconnect", "true"), ("resize", "scale"),
            ("reconnect", "false"), ("path", websocket),
        ])
        result["url"] = urlunsplit(target._replace(query=query))
        return result
