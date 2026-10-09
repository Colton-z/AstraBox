from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict

from astrabox.api.routes.response_envelope import ApiEnvelope
from astrabox.common.utils.api_response import error_response, success_response
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import get_current_user_context
from astrabox.core.service.orchestrator.runtime.mcp_servers import CONNECTOR_PLATFORM_SERVER
from astrabox.core.service.orchestrator.service_registry import get_platform_service
from astrabox.core.service.orchestrator.transcript_capability import verify_platform_mcp_capability

class ExposedPortUrl(BaseModel):
    """A freshly minted browser-reachable URL for one sandbox port.

    The URL is short-lived and carries its own bearer, so a caller re-requests
    it rather than caching it.
    """

    model_config = ConfigDict(extra="allow")

    url: str
    port: int


def _json_api_error_response(exc: APIError) -> JSONResponse:
    return JSONResponse(
        content=error_response(exc),
        status_code=exc.status_code,
    )


def register_platform_mcp_routes(app: Any) -> None:
    if getattr(app.state, "platform_mcp_routes_registered", False):
        return
    app.state.platform_mcp_routes_registered = True

    _service = get_platform_service()

    @app.post("/api/v1/platform-mcp/{deployment_id}/{server_name}/{capability}/mcp")
    async def platform_mcp_streamable_http(
        deployment_id: str,
        server_name: str,
        capability: str,
        request: Request,
    ):
        if not verify_platform_mcp_capability(deployment_id, server_name, capability):
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": "not found"}}, status_code=404)
        try:
            body = await request.json()
        except Exception as exc:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"invalid json body: {exc}"},
                }
            )
        if not isinstance(body, dict):
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "invalid JSON-RPC request body"},
                }
            )
        target = await _service.resolve_platform_mcp_server(deployment_id, server_name)
        if not target:
            return JSONResponse(
                error_response(
                    "NOT_FOUND",
                    f"platform MCP server '{server_name}' not configured for binding",
                ),
                status_code=404,
            )
        if target.get("platform_server") == CONNECTOR_PLATFORM_SERVER:
            try:
                answer = await _service.platform_mcp.relay_connector(
                    target, body,
                    session_id=request.headers.get("mcp-session-id"),
                    protocol_version=request.headers.get("mcp-protocol-version"),
                )
            except APIError as exc:
                return JSONResponse(
                    {"jsonrpc": "2.0", "id": body.get("id"),
                     "error": {"code": -32603, "message": f"{exc.code}: {exc.message}"}},
                    status_code=502,
                )
            headers = {"Mcp-Session-Id": answer.session_id} if answer.session_id else None
            if answer.message is None:
                return Response(status_code=answer.status, headers=headers)
            return JSONResponse(answer.message, status_code=answer.status, headers=headers)
        response = await _service.platform_mcp.handle_jsonrpc(
            deployment_id,
            server_name,
            body,
        )
        if "id" not in body:
            return Response(status_code=202)
        return JSONResponse(response)

    @app.get(
        "/api/v1/exposed-ports/{deployment_id}/{port}/url",
        response_model=ApiEnvelope[ExposedPortUrl],
        response_model_exclude_unset=True,
    )
    async def refresh_exposed_port_url(
        deployment_id: str,
        port: int,
        request: Request,
    ):
        """Return a fresh browser-reachable URL for a sandbox port.

        The request itself is authenticated and ownership-checked before the
        platform mints a new short-lived bearer URL.

        The success answer is the envelope dict, not a ``JSONResponse`` holding
        it: FastAPI sends a response object through untouched, which would
        leave ``response_model`` describing this route without checking it.
        """
        try:
            user = await get_current_user_context(request)
            url = await _service.refresh_exposed_port_url(
                deployment_id=deployment_id,
                port=port,
                user=user,
            )
            return success_response({"url": url, "port": port})
        except APIError as exc:
            return _json_api_error_response(exc)
