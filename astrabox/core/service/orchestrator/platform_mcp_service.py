from __future__ import annotations

import json
from typing import Any

from mcp_types.version import LATEST_PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.agent_schema import agent_artifacts_enabled
from astrabox.core.service.orchestrator.runtime.mcp_servers import (
    ARTIFACTS_PLATFORM_SERVER,
    artifact_tools_enabled,
    binds_artifact_tools,
    EXPOSE_PORT_PLATFORM_SERVER,
    is_platform_mcp_server,
    mcp_server_enabled,
    platform_mcp_server_id,
    template_mcp_servers,
)

from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.artifacts.service import invalid
from astrabox.core.service.artifacts.tools import GUIDE, artifact_tools

logger = get_logger(__name__)

_EXPOSE_PORT_TOOL = "expose_port"


class PlatformMCPService:
    def __init__(
        self,
        *,
        sessions_repo: Any,
        agent_config: Any,
        binding_repo: Any,
        expose_port_service: Any,
        artifact_service: Any = None,
    ) -> None:
        self._sessions_repo = sessions_repo
        self._agent_config = agent_config
        self._binding_repo = binding_repo
        self._expose_port_service = expose_port_service
        self._artifacts = artifact_service
        self._servers = {
            EXPOSE_PORT_PLATFORM_SERVER: (self._expose_tools, self._handle_expose_tool),
            ARTIFACTS_PLATFORM_SERVER: (artifact_tools, self._handle_artifact_tool),
        }

    async def resolve_server(self, deployment_id: str, server_name: str) -> dict[str, Any] | None:
        if server_name == ARTIFACTS_PLATFORM_SERVER:
            binding = await self._binding_repo.get_binding(deployment_id)
            sid = (binding or {}).get("session_id") or (deployment_id if not binding else None)
            session = await self._sessions_repo.get_session(sid) if sid else None
            if not artifact_tools_enabled((session or binding or {}).get("source_type")):
                return None
            if not binding and not session:
                return None
            if not await self._origin_agent_artifacts_enabled(binding, session):
                return None
            return {"deployment_id": deployment_id, "server_name": server_name,
                    "platform_server": ARTIFACTS_PLATFORM_SERVER,
                    "allowed_tools": {t["name"] for t in artifact_tools()}}
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
        if platform_server not in self._servers:
            return None
        if platform_server == ARTIFACTS_PLATFORM_SERVER and not binds_artifact_tools(
            getattr(template, "session_source_type", None),
            agent_artifacts_enabled=getattr(template, "artifacts_enabled", True),
        ):
            return None
        tools = config.get("tools")
        if isinstance(tools, list):
            allowed_tools = {str(item).strip() for item in tools if str(item or "").strip()}
        else:
            allowed_tools = {t["name"] for t in self._servers[platform_server][0]()}
        return {
            "deployment_id": str(deployment_id or "").strip(),
            "server_name": str(server_name or "").strip(),
            "platform_server": platform_server,
            "allowed_tools": allowed_tools,
        }

    async def _origin_agent_artifacts_enabled(
        self, binding: dict[str, Any] | None, session: dict[str, Any] | None,
    ) -> bool:
        """The binding's Agent switch, read at call time; Assistants have none.

        Reading the Agent on every call makes turning the switch off refuse
        the tools at once, including in conversations whose engine received
        the server before the change. An unclaimed prepared-slot binding names
        its Agent in ``template_name``. A binding whose Agent cannot be read
        has no switch to consult, so it is refused.
        """

        ref = (session or {}).get("workspace_ref") or {}
        if ref.get("kind") == "assistant" or ref.get("assistant_id") or (binding or {}).get("assistant_id"):
            return True
        agent_id = str(
            ref.get("agent_id")
            or (session or {}).get("agent_id")
            or (binding or {}).get("template_name")
            or ""
        ).strip()
        if not agent_id:
            return False
        agent = await self._agent_config.get_agent_access_doc(agent_id)
        return isinstance(agent, dict) and agent_artifacts_enabled(agent)

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
        if tool_name not in server["allowed_tools"]:
            return self._error(request_id, -32602, f"tool not allowed: {tool_name}")
        arguments = params.get("arguments")
        if not isinstance(arguments, dict):
            return self._error(request_id, -32602, "arguments must be an object")
        server_id = server.get("platform_server", EXPOSE_PORT_PLATFORM_SERVER)
        tools, handler = self._servers[server_id]
        schema = next(t["inputSchema"] for t in tools() if t["name"] == tool_name)
        if server_id == EXPOSE_PORT_PLATFORM_SERVER and type(arguments.get("port")) is not int:
            return self._error(request_id, -32602, "port is required: start your web server on a port of its own and pass that port")
        if set(arguments) - set(schema["properties"]) or set(schema["required"]) - set(arguments):
            return self._error(request_id, -32602, "missing or unknown tool arguments")
        if server_id == EXPOSE_PORT_PLATFORM_SERVER and (type(arguments.get("port")) is not int or not 1 <= arguments["port"] <= 65535):
            return self._error(request_id, -32602, "port is required: start your web server on a port of its own and pass that port")
        result = await handler(server, tool_name, arguments)
        return self._tool_result(request_id, result)

    async def _handle_expose_tool(self, server: dict[str, Any], tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        port = arguments.get("port")
        if type(port) is not int or not 1 <= port <= 65535:
            raise APIError(code="INVALID_REQUEST", message="port is required: start your web server on a port of its own and pass that port", status_code=400)
        return await self._expose_port_service.expose_port(deployment_id=server["deployment_id"], port=port, title=arguments.get("title"))

    async def _artifact_binding(self, deployment_id: str) -> dict[str, Any]:
        binding = await self._binding_repo.get_binding(deployment_id)
        sid = str((binding or {}).get("session_id") or (deployment_id if not binding else ""))
        if sid:
            session = await self._sessions_repo.get_session(sid)
            if not session:
                raise APIError(code="MCP_BINDING_NOT_FOUND", message="MCP binding not found", status_code=404)
            ref = session.get("workspace_ref") or {}
            return {**(binding or {}), **session, "session_id": sid,
                    "assistant_id": ref.get("assistant_id"), "artifact_grant": (binding or {}).get("artifact_grant")}
        if not binding or not binding.get("user_id"):
            raise APIError(code="MCP_BINDING_NOT_FOUND", message="MCP binding not found", status_code=404)
        return binding

    async def _handle_artifact_tool(self, server: dict[str, Any], name: str, args: dict[str, Any]) -> dict[str, Any]:
        binding = await self._artifact_binding(server["deployment_id"])
        if not binding.get("user_id"):
            raise APIError(code="MCP_BINDING_NOT_FOUND", message="MCP binding has no user", status_code=404)
        if not artifact_tools_enabled(binding.get("source_type")):
            raise APIError(code="MCP_BINDING_NOT_FOUND", message="channel Sessions have no artifact tools", status_code=404)
        ref = binding.get("workspace_ref") or {}
        assistant_id = binding.get("assistant_id") or ref.get("assistant_id")
        agent_id = binding.get("agent_id") or ref.get("agent_id")
        scope = {"assistant_id": assistant_id} if assistant_id else {"agent_id": agent_id}
        if not (assistant_id or agent_id):
            raise APIError(code="MCP_BINDING_NOT_FOUND", message="artifact binding has no origin identity", status_code=404)
        user = UserContext(binding["user_id"])
        if name == "artifact_guide":
            return {"guide": GUIDE}
        if self._artifacts is None:
            raise APIError(code="ARTIFACT_STORE_UNAVAILABLE", message="artifact service unavailable", status_code=503)
        grant = binding.get("artifact_grant")
        if grant is not None:
            if name == "create_artifact":
                raise APIError(code="ARTIFACT_NOT_WRITER", message="artifact runs cannot create artifacts", status_code=403)
            if name != "list_artifacts" and args.get("artifact_id") != grant.get("artifact_id"):
                raise self._artifacts._not_found()
            granted_record = await self._artifacts.records.get(grant.get("artifact_id"))
            if not granted_record or any(granted_record["origin"].get(k) != v for k, v in scope.items()):
                raise self._artifacts._not_found()
            granted_viewer = await self._artifacts.resolve_viewer(user=user, artifact_id=grant.get("artifact_id"), grant=grant)
        if name == "create_artifact":
            origin = {**scope, "session_kind": binding.get("session_kind") or "assistant_chat"}
            for key in ("session_id",):
                if binding.get(key):
                    origin[key] = binding[key]
            created = await self._artifacts.create(user, args, origin=origin, binding=binding)
            return self._artifacts.marker(created)
        if name == "list_artifacts":
            if grant is not None:
                runtime = await self._artifacts.runtime(granted_viewer)
                return {"items": [self._artifacts.marker(runtime["artifact"])], "next_cursor": None}
            listing = await self._artifacts.list(user, q=args.get("query"), limit=args.get("limit", 50), **scope)
            return {**listing, "items": [self._artifacts.marker(item) for item in listing["items"]]}
        aid = args.get("artifact_id")
        if not isinstance(aid, str) or not aid:
            raise invalid("artifact_id is required")
        record = await self._artifacts.records.get(aid)
        if not record or any(record["origin"].get(k) != v for k, v in scope.items()):
            raise self._artifacts._not_found()
        viewer = await self._artifacts.resolve_viewer(user=user, artifact_id=aid, grant=grant)
        if name == "update_artifact":
            if viewer.level != "owner":
                raise APIError(code="ARTIFACT_NOT_WRITER", message="only the artifact owner may update it", status_code=403)
            updated = await self._artifacts.update(user, aid, {k: v for k, v in args.items() if k != "artifact_id"}, binding=binding)
            return self._artifacts.marker(updated)
        if name == "get_artifact":
            if "version" in args and (type(args["version"]) is not int or args["version"] < 1):
                raise invalid("version must be positive")
            if viewer.level == "owner":
                fetched = await self._artifacts.get(user, aid, args.get("version"))
                version = fetched["version"]
            else:
                viewer, record, version = await self._artifacts.refresh(viewer)
                if "version" in args and args["version"] != version["version"]:
                    raise self._artifacts._not_found()
            return {"description": record["description"], "manifest": version["manifest"],
                    "content": version["content"], "viewer": viewer.projection(), **self._artifacts.marker(version)}
        action = args.get("action")
        if action == "get":
            rows = await self._artifacts.data_get(viewer, args.get("path"))
        elif action in {"list", "query"}:
            rows = await self._artifacts.data_query(viewer, args.get("collection"), where=args.get("where"), order_by=args.get("order_by"), limit=args.get("limit", 100))
        elif action in {"set", "update", "delete"}:
            rows = await self._artifacts.data_write(viewer, [{"action": action, "path": args.get("path"), "data": args.get("data")}])
        elif action == "batch":
            rows = await self._artifacts.data_write(viewer, args.get("writes"))
        else:
            raise invalid("invalid artifact_data action")
        _, _, version = await self._artifacts.refresh(viewer)
        return {**rows, **self._artifacts.marker(version)}

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
        if isinstance(binding, dict) and binding.get("assistant_id"):
            return await self._agent_config.resolve_session_harness(
                {"workspace_ref": {"kind": "assistant", "assistant_id": binding["assistant_id"]}}
            )
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
        return None

    @staticmethod
    def _tools(server: dict[str, Any]) -> list[dict[str, Any]]:
        tools = artifact_tools() if server.get("platform_server") == ARTIFACTS_PLATFORM_SERVER else PlatformMCPService._expose_tools()
        return [t for t in tools if t["name"] in server["allowed_tools"]]

    @staticmethod
    def _expose_tools() -> list[dict[str, Any]]:
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
