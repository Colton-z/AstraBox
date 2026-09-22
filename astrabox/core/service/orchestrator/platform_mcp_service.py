from __future__ import annotations

import json
from typing import Any

from mcp_types.version import LATEST_PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.runtime.mcp_servers import (
    HTML_PREVIEW_PLATFORM_SERVER,
    is_platform_mcp_server,
    mcp_server_enabled,
    platform_mcp_server_id,
    template_mcp_servers,
)

logger = get_logger(__name__)

_EXPOSE_PORT_TOOL = "expose_port"
# Keep accepting the old tool name so existing templates/agents don't 404.
_LEGACY_PUBLISH_HTML_PREVIEW_TOOL = "publish_html_preview"
_ACCEPTED_TOOLS = {_EXPOSE_PORT_TOOL, _LEGACY_PUBLISH_HTML_PREVIEW_TOOL}


class PlatformMCPService:
    def __init__(
        self,
        *,
        sessions_repo: Any,
        agent_config: Any,
        binding_repo: Any,
        expose_port_service: Any,
    ) -> None:
        self._sessions_repo = sessions_repo
        self._agent_config = agent_config
        self._binding_repo = binding_repo
        self._expose_port_service = expose_port_service

    async def resolve_server(self, deployment_id: str, server_name: str) -> dict[str, Any] | None:
        template = await self._resolve_template_for_mcp_binding(deployment_id)
        if not template:
            return None
        servers = template_mcp_servers(getattr(template, "mcp_servers", None))
        config = servers.get(str(server_name or "").strip())
        if not isinstance(config, dict):
            return None
        if not mcp_server_enabled(config):
            return None
        if not is_platform_mcp_server(config):
            return None
        platform_server = platform_mcp_server_id(server_name, config)
        if platform_server != HTML_PREVIEW_PLATFORM_SERVER:
            return None
        tools = config.get("tools")
        if isinstance(tools, list):
            allowed_tools = {str(item).strip() for item in tools if str(item or "").strip()}
        else:
            allowed_tools = set(_ACCEPTED_TOOLS)
        return {
            "deployment_id": str(deployment_id or "").strip(),
            "server_name": str(server_name or "").strip(),
            "platform_server": platform_server,
            "allowed_tools": allowed_tools,
        }

    async def handle_jsonrpc(
        self,
        deployment_id: str,
        server_name: str,
        body: dict[str, Any],
    ) -> dict[str, Any] | None:
        request_id = body.get("id")
        method = str(body.get("method") or "").strip()
        server = await self.resolve_server(deployment_id, server_name)
        if not server:
            return self._error(request_id, -32601, "platform MCP server not configured")

        try:
            if method == "initialize":
                params = body.get("params")
                params = params if isinstance(params, dict) else {}
                requested_version = str(params.get("protocolVersion") or "")
                protocol_version = (
                    requested_version
                    if requested_version in SUPPORTED_PROTOCOL_VERSIONS
                    else LATEST_PROTOCOL_VERSION
                )
                return self._result(
                    request_id,
                    {
                        "protocolVersion": protocol_version,
                        "capabilities": {"tools": {}},
                        "serverInfo": {
                            "name": f"astrabox-platform:{server_name}",
                            "version": "2.0.0",
                        },
                    },
                )
            if method in {"notifications/initialized", "initialized"}:
                return None
            if method == "ping":
                return self._result(request_id, {})
            if method == "tools/list":
                return self._result(request_id, {"tools": self._tools(server)})
            if method == "tools/call":
                return await self._handle_tools_call(request_id, server, body)
            return self._error(request_id, -32601, f"unknown method: {method}")
        except APIError as exc:
            return self._error(request_id, -32603, f"{exc.code}: {exc.message}")
        except Exception as exc:
            logger.exception("platform MCP request failed: %s", exc)
            return self._error(request_id, -32603, str(exc))

    async def _handle_tools_call(
        self,
        request_id: Any,
        server: dict[str, Any],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        params = body.get("params") if isinstance(body.get("params"), dict) else {}
        tool_name = str(params.get("name") or "").strip()
        if tool_name not in server["allowed_tools"] and tool_name not in _ACCEPTED_TOOLS:
            return self._error(request_id, -32602, f"tool not allowed: {tool_name}")
        arguments = params.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}
        # Both expose_port and publish_html_preview route through here.
        if tool_name in _ACCEPTED_TOOLS:
            port = arguments.get("port")
            if port is None and "path" in arguments:
                # No in-box nginx to serve a bare path; a path-only call
                # implies the agent's dev server is on the default port.
                port = 8080
            result = await self._expose_port_service.expose_port(
                deployment_id=server["deployment_id"],
                port=int(port or 8080),
                title=str(arguments.get("title") or "").strip() or None,
            )
            return self._tool_result(request_id, result)
        return self._error(request_id, -32601, f"unknown tool: {tool_name}")

    async def _resolve_template_name(self, deployment_id: str) -> str | None:
        normalized = str(deployment_id or "").strip()
        binding = await self._binding_repo.get_binding(normalized)
        if isinstance(binding, dict):
            return str(binding.get("template_name") or "").strip() or None
        session = await self._sessions_repo.get_session(normalized)
        if isinstance(session, dict):
            return str(session.get("template_name") or "").strip() or None
        return None

    # ── Session-scoped MCP server-target resolution ──────────────────────
    # (transport/service-code extraction from a session's template MCP config;
    # distinct from resolve_server's platform-hosted binding lookup above.)
    async def _resolve_template_for_mcp_binding(self, deployment_id: str) -> Any | None:
        normalized = str(deployment_id or "").strip()
        session = await self._sessions_repo.get_session(normalized)
        if isinstance(session, dict):
            resolver = getattr(self._agent_config, "resolve_session_harness", None)
            if callable(resolver):
                resolved = await resolver(session)
                if resolved is not None:
                    return resolved

        binding = await self._binding_repo.get_binding(normalized)
        template_name = ""
        if isinstance(binding, dict):
            template_name = str(binding.get("template_name") or "").strip()
        if not template_name:
            if not session:
                return None
            template_name = str(session.get("template_name") or "").strip()
        if not template_name:
            return None
        agent_resolver = getattr(self._agent_config, "resolve_agent_harness", None)
        if callable(agent_resolver):
            resolved = await agent_resolver(template_name)
            if resolved is not None:
                return resolved
        legacy_resolver = getattr(self._agent_config, "get_template", None)
        if callable(legacy_resolver):
            return await legacy_resolver(template_name)
        return None

    @staticmethod
    def _tools(server: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "name": _EXPOSE_PORT_TOOL,
                "description": (
                    "Expose a sandbox port as a browser-reachable URL. "
                    "Use when you start a web server (e.g. Streamlit, Next.js, "
                    "python -m http.server) and want to give the user a live preview link. "
                    "WebSocket is supported (hot-reload works)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "port": {
                            "type": "integer",
                            "description": "The port number the web server is listening on inside the sandbox.",
                        },
                        "title": {
                            "type": "string",
                            "description": "Optional display title for the preview.",
                        },
                    },
                    "required": ["port"],
                    "additionalProperties": False,
                },
            }
        ]

    @staticmethod
    def _tool_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return PlatformMCPService._result(
            request_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(result, ensure_ascii=False),
                    }
                ],
                "structuredContent": result,
            },
        )

    @staticmethod
    def _result(request_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }
