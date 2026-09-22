"""``astrabox mcp serve`` — the same command layer, spoken as MCP over stdio.

An MCP-native client (an assistant's desktop app, an agent framework) cannot
run a shell command and read its exit code. This serves the client subcommands
as MCP tools instead, calling the very same functions the argv façade calls —
:func:`astrabox.cli.resources.plan_and_run`, :func:`astrabox.cli.scaffold.
export_document`, :func:`astrabox.cli.run.stream_turn`. There is one
implementation of what these commands do; this module only changes who is
asking.

This is the *platform administration* surface: schemas, resources, convergence,
and starting a task. It is distinct from the deployment's own ``/api/v1/mcp``
endpoint (``docs/agent-mcp.md``), which lets a client *use* the Agents a
deployment already has.

The transport is line-delimited JSON-RPC on stdin/stdout, which is what MCP's
stdio transport is. stdout therefore carries protocol messages and nothing
else; every diagnostic goes to stderr.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from astrabox.cli.client import ApiClient, Endpoint, as_items, resolve_endpoint
from astrabox.cli.document import parse_document
from astrabox.cli.output import CliError
from astrabox.cli.resources import GET_ROUTES, SCHEMA_ROUTES, plan_and_run
from astrabox.cli.run import start_conversation, stream_turn
from astrabox.cli.scaffold import export_document
from astrabox.cli.stack import probe

#: JSON-RPC error codes this server returns, from the JSON-RPC 2.0 spec.
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603


def register(subparsers: Any) -> None:
    """Add ``astrabox mcp`` to the top-level parser."""
    from astrabox.cli.flags import connection_flags

    mcp = subparsers.add_parser(
        "mcp",
        help="Serve the client commands as MCP tools over stdio.",
        description=(
            "Expose schema, get, diff, apply, export, status and run as MCP "
            "tools on stdin/stdout, for clients that speak MCP rather than "
            "argv. Administers a deployment; the deployment's own /api/v1/mcp "
            "endpoint is for using its Agents."
        ),
    )
    serve = mcp.add_subparsers(dest="mcp_command", required=True).add_parser(
        "serve",
        parents=[connection_flags()],
        help="Run the stdio MCP server until stdin closes.",
    )
    serve.set_defaults(func=_cmd_mcp_serve)


def tool_definitions() -> list[dict[str, Any]]:
    """The MCP tool list, one entry per client command this server exposes."""
    return [
        {
            "name": "astrabox_schema",
            "description": (
                "The deployment's authoring contract for a resource kind: every "
                "field's key, type, whether it is required, and its candidate "
                "set. Read this before writing an astrabox.yaml resource — it "
                "is the authoritative field list and it is versioned with the "
                "deployment."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": sorted(SCHEMA_ROUTES)},
                },
                "required": ["kind"],
            },
        },
        {
            "name": "astrabox_get",
            "description": "List what the deployment holds for one collection.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": sorted(GET_ROUTES)},
                },
                "required": ["kind"],
            },
        },
        {
            "name": "astrabox_export",
            "description": (
                "Project the deployment's environments and Agents into an "
                "astrabox.yaml document body. Server-managed state is excluded, "
                "so the result is a document astrabox_apply accepts."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "astrabox_diff",
            "description": (
                "Report what applying this document would change, sending no "
                "writes. Same reads as astrabox_apply."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "document": {
                        "type": "object",
                        "description": "An astrabox.yaml body, as an object.",
                    },
                },
                "required": ["document"],
            },
        },
        {
            "name": "astrabox_apply",
            "description": (
                "Create and update every resource the document declares. Never "
                "deletes: a resource the document stops mentioning is left "
                "alone. Refuses rather than guessing when a name is ambiguous."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "document": {
                        "type": "object",
                        "description": "An astrabox.yaml body, as an object.",
                    },
                },
                "required": ["document"],
            },
        },
        {
            "name": "astrabox_status",
            "description": "Whether the deployment is up and serving traffic.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "astrabox_run",
            "description": (
                "Start a conversation with an Agent, send one task, and return "
                "the reply once the session's stream closes. Reports a pending "
                "interaction when the Agent stopped to ask something."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "description": "Agent name or id."},
                    "task": {"type": "string"},
                    "session_id": {
                        "type": "string",
                        "description": "Continue this session instead of starting one.",
                    },
                    "timeout_seconds": {"type": "integer"},
                },
                "required": ["agent", "task"],
            },
        },
    ]


def call_tool(endpoint: Endpoint, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Run one tool against a deployment and return its payload.

    Every branch delegates to the function the argv façade uses, so a tool and
    its command can never drift into two behaviours.
    """
    with ApiClient(endpoint) as client:
        if name == "astrabox_schema":
            return dict(client.get(SCHEMA_ROUTES[_enum_arg(arguments, "kind", SCHEMA_ROUTES)]))
        if name == "astrabox_get":
            route, _columns = GET_ROUTES[_enum_arg(arguments, "kind", GET_ROUTES)]
            return {"items": [dict(item) for item in as_items(client.get(route))]}
        if name == "astrabox_export":
            return export_document(client)
        if name in ("astrabox_diff", "astrabox_apply"):
            document = parse_document(arguments.get("document"))
            write = name == "astrabox_apply"
            return {"dry_run": not write, "resources": plan_and_run(client, document, write=write)}
        if name == "astrabox_status":
            healthy, health_detail = probe(endpoint.base_url, "/healthz")
            ready, ready_detail = probe(endpoint.base_url, "/readyz")
            return {
                "endpoint": endpoint.base_url,
                "healthy": healthy,
                "ready": ready,
                "detail": health_detail if not healthy else ready_detail,
            }
        if name == "astrabox_run":
            session_id = str(arguments.get("session_id") or "") or start_conversation(
                client, str(arguments.get("agent") or "")
            )
            result = stream_turn(
                client,
                session_id,
                str(arguments.get("task") or ""),
                deadline_seconds=int(arguments.get("timeout_seconds") or 900),
                live=False,
            )
            result["session_id"] = session_id
            return result
    raise CliError(f"unknown tool: {name}")


def _enum_arg(arguments: dict[str, Any], key: str, allowed: Any) -> str:
    """One required enum argument, refused by name when it is not a member."""
    value = str(arguments.get(key) or "").strip()
    if value not in allowed:
        raise CliError(f"{key} must be one of {sorted(allowed)}, not {value!r}")
    return value


def _cmd_mcp_serve(args: Any) -> int:
    """Handle ``astrabox mcp serve`` — read JSON-RPC until stdin closes."""
    endpoint = resolve_endpoint(endpoint=args.endpoint, token=args.token)
    for line in sys.stdin:
        message = line.strip()
        if not message:
            continue
        response = _handle(endpoint, message)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


def _handle(endpoint: Endpoint, message: str) -> dict[str, Any] | None:
    """Turn one JSON-RPC line into its response, or ``None`` for a notification."""
    try:
        request = json.loads(message)
    except ValueError:
        return _error(None, _INVALID_PARAMS, "request is not JSON")
    if not isinstance(request, dict):
        return _error(None, _INVALID_PARAMS, "request is not an object")

    request_id = request.get("id")
    method = str(request.get("method") or "")
    params = request.get("params") if isinstance(request.get("params"), dict) else {}

    # A notification carries no id and receives no response. This server holds
    # no per-connection state, so initialized/cancelled need no side effect.
    if request_id is None and method.startswith("notifications/"):
        return None

    if method == "initialize":
        return _result(request_id, _initialize(params))
    if method == "ping":
        return _result(request_id, {})
    if method == "tools/list":
        return _result(request_id, {"tools": tool_definitions()})
    if method == "tools/call":
        return _call(endpoint, request_id, params)
    return _error(request_id, _METHOD_NOT_FOUND, f"unknown method: {method}")


def _initialize(params: dict[str, Any]) -> dict[str, Any]:
    """The initialize result, echoing a protocol version the vendor supports."""
    from mcp_types.version import LATEST_PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS

    from astrabox import __version__

    requested = str(params.get("protocolVersion") or "")
    return {
        "protocolVersion": (
            requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        ),
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "astrabox-platform", "version": __version__},
    }


def _call(endpoint: Endpoint, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Dispatch tools/call, reporting a tool failure as a tool result.

    MCP distinguishes a protocol error from a tool that ran and failed: the
    second is a result with ``isError``, so the model sees why rather than the
    client seeing a transport fault.
    """
    name = str(params.get("name") or "")
    raw = params.get("arguments")
    if raw is not None and not isinstance(raw, dict):
        return _error(request_id, _INVALID_PARAMS, "arguments must be an object")
    try:
        payload = call_tool(endpoint, name, raw or {})
    except CliError as exc:
        body: dict[str, Any] = {"error": exc.message, **exc.details}
        if exc.code:
            body["code"] = exc.code
        return _result(request_id, _tool_result(body, is_error=True))
    except Exception as exc:  # noqa: BLE001 - reported to the caller, not swallowed
        return _error(request_id, _INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
    return _result(request_id, _tool_result(payload))


def _tool_result(payload: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    """The MCP CallToolResult shape current clients understand."""
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str)}],
        "structuredContent": payload,
        "isError": is_error,
    }


def _result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


__all__ = ["call_tool", "register", "tool_definitions"]
