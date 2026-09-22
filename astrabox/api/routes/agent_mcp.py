"""Controller for the platform-level Agent MCP facade (Streamable HTTP)."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from fastapi import Body, Request
from fastapi.responses import JSONResponse, Response
from mcp_types.version import LATEST_PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS

from astrabox import __version__
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext, get_asserted_user_context
from astrabox.core.service.orchestrator.agent.agent_mcp_service import AgentMCPService
from astrabox.core.service.orchestrator.mcp_client_token_service import (
    SECRET_PREFIX,
    MCPClientTokenService,
)
from astrabox.core.service.orchestrator.service_registry import get_agent_service

logger = get_logger(__name__)

_registered_on: int | None = None


def _jsonrpc_result(request_id: Any, result: Any) -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content={"jsonrpc": "2.0", "id": request_id, "result": result},
    )


def _jsonrpc_error(
    request_id: Any,
    code: int,
    message: str,
    *,
    data: dict[str, Any] | None = None,
) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if data:
        error["data"] = data
    return JSONResponse(
        status_code=200,
        content={"jsonrpc": "2.0", "id": request_id, "error": error},
    )


def _tool_result(payload: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    """Return the MCP CallToolResult shape understood by current clients."""

    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False, default=str),
            }
        ],
        "structuredContent": payload,
        "isError": is_error,
    }


def _origin_is_allowed(request: Request) -> bool:
    """Implement the Streamable HTTP Origin check required by the MCP spec."""

    origin = str(request.headers.get("origin") or "").strip()
    if not origin:
        return True
    parsed = urlsplit(origin)
    request_host = str(request.headers.get("host") or "").strip().lower()
    return parsed.scheme in {"http", "https"} and parsed.netloc.lower() == request_host


def _protocol_header_is_supported(request: Request) -> bool:
    """Accept an omitted compatibility header or a supported protocol version."""

    version = str(request.headers.get("mcp-protocol-version") or "").strip()
    return not version or version in SUPPORTED_PROTOCOL_VERSIONS



def _mcp_auth_error_response(exc: APIError) -> JSONResponse:
    """Serve an auth rejection with the Streamable HTTP discovery challenge.

    The identity middleware exempts this path, so the facade — the layer
    that actually judges the credential — answers the challenge itself.
    """
    headers = (
        {"WWW-Authenticate": 'Bearer realm="astrabox-mcp"'}
        if int(getattr(exc, "status_code", 401) or 401) == 401
        else None
    )
    return JSONResponse(
        status_code=int(getattr(exc, "status_code", 401) or 401),
        content={"code": exc.code, "message": str(exc)},
        headers=headers,
    )


def register_agent_mcp_routes(
    app: Any,
    *,
    agent_service: Any | None = None,
) -> None:
    global _registered_on
    if _registered_on == id(app):
        return
    _registered_on = id(app)

    resolved_agent_service = agent_service or get_agent_service()
    mcp_service = AgentMCPService(agent_service=resolved_agent_service)

    token_service = MCPClientTokenService()

    async def _caller(request: Request) -> tuple[UserContext, str | None]:
        """The identity behind this call, and the scope narrowing it.

        Two credentials reach this endpoint and the prefix tells them apart, so
        neither is tried against the other: a secret starting with the AstraBox
        prefix is one this deployment issued, and anything else is left to the
        identity middleware, which has already verified a browser session or a
        provider access token by the time a handler runs.

        A scope of None is not "no restriction applied" — it is the full
        identity of someone who authenticated as themselves.
        """
        presented = str(request.headers.get("authorization") or "").strip()
        if presented.lower().startswith("bearer "):
            secret = presented[7:].strip()
            if secret.startswith(SECRET_PREFIX):
                return await token_service.resolve(secret)
        # This path is exempt from the front-door reject (the middleware
        # cannot judge which credential kind a bearer is), so only an identity
        # the middleware actually stamped counts here. The never-raising
        # default would serve the deployment's local administrator to any
        # unrecognized bearer token.
        user = get_asserted_user_context()
        if user is None:
            raise APIError(
                code="AUTH_REQUIRED",
                message="sign-in or an AstraBox MCP key is required",
                status_code=401,
            )
        return user, None

    @app.get("/api/v1/mcp", include_in_schema=False)
    async def agent_mcp_event_stream(request: Request) -> Response:
        """Decline the optional standalone SSE stream as Streamable HTTP specifies."""

        if not _origin_is_allowed(request):
            return JSONResponse(
                status_code=403,
                content={"code": "MCP_ORIGIN_FORBIDDEN", "message": "Origin is not allowed"},
            )
        try:
            await _caller(request)
        except APIError as exc:
            return _mcp_auth_error_response(exc)
        return Response(status_code=405, headers={"Allow": "POST"})

    @app.post("/api/v1/mcp")
    async def agent_mcp_endpoint(
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> Response:
        if not _origin_is_allowed(request):
            return JSONResponse(
                status_code=403,
                content={"code": "MCP_ORIGIN_FORBIDDEN", "message": "Origin is not allowed"},
            )

        try:
            user, scope = await _caller(request)
        except APIError as exc:
            return _mcp_auth_error_response(exc)

        method = str(payload.get("method") or "")
        raw_params = payload.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}
        request_id = payload.get("id")

        if method != "initialize" and not _protocol_header_is_supported(request):
            return JSONResponse(
                status_code=400,
                content={
                    "code": "MCP_PROTOCOL_VERSION_UNSUPPORTED",
                    "message": "MCP-Protocol-Version is not supported",
                },
            )

        # Notifications never receive a JSON-RPC response. The facade is
        # stateless, so initialized/cancelled notifications need no side effect.
        if request_id is None and method.startswith("notifications/"):
            return Response(status_code=202)

        try:
            if method == "initialize":
                requested_version = str(params.get("protocolVersion") or "")
                protocol_version = (
                    requested_version
                    if requested_version in SUPPORTED_PROTOCOL_VERSIONS
                    else LATEST_PROTOCOL_VERSION
                )
                result = {
                    "protocolVersion": protocol_version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": "astrabox-agents",
                        "version": __version__,
                    },
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = await mcp_service.handle_tools_list()
            elif method == "tools/call":
                tool_name = str(params.get("name") or "")
                raw_arguments = params.get("arguments")
                if raw_arguments is not None and not isinstance(raw_arguments, dict):
                    return _jsonrpc_error(request_id, -32602, "arguments must be an object")
                arguments = raw_arguments or {}
                tool_payload = await mcp_service.handle_tool_call(
                    user,
                    tool_name,
                    arguments,
                    scope=scope,
                )
                result = _tool_result(tool_payload)
            else:
                return _jsonrpc_error(
                    request_id,
                    -32601,
                    f"unknown method: {method}",
                )

            return _jsonrpc_result(request_id, result)

        except APIError as exc:
            if method == "tools/call":
                return _jsonrpc_result(
                    request_id,
                    _tool_result(
                        {"error": {"code": exc.code, "message": exc.message}},
                        is_error=True,
                    ),
                )
            return _jsonrpc_error(
                request_id,
                -32000,
                exc.message,
                data={"code": exc.code},
            )
        except Exception:
            logger.exception("AstraBox MCP request failed")
            if method == "tools/call":
                return _jsonrpc_result(
                    request_id,
                    _tool_result(
                        {
                            "error": {
                                "code": "INTERNAL_ERROR",
                                "message": "internal server error",
                            }
                        },
                        is_error=True,
                    ),
                )
            return _jsonrpc_error(request_id, -32603, "internal server error")


__all__ = ["register_agent_mcp_routes"]
