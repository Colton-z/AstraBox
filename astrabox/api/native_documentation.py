"""Metadata for the SDK-owned protocol routes outside FastAPI's route parser."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

from astrabox.api.documentation import object_schema, request_body

A2A_SPEC = "https://a2a-protocol.org/v1.0.0/specification/"


def _component(title: str) -> str:
    return "A2A" + re.sub(r"[^a-zA-Z0-9]", "", title)


def a2a_components() -> dict[str, Any]:
    """Rebase references in the official schema bundle, preserving native fields."""
    bundle = json.loads((Path(__file__).parent / "schemas" / "a2a.json").read_text())
    names = {
        re.sub(r"[^a-z0-9]", "", title.lower()): _component(title)
        for title in bundle["definitions"]
    }

    def rebase(value: Any) -> Any:
        if isinstance(value, list):
            return [rebase(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {key: rebase(item) for key, item in value.items()}
        if "$ref" in result:
            name = result["$ref"].removesuffix(".jsonschema.json").rsplit(".", 1)[-1].lower()
            result["$ref"] = "#/components/schemas/" + names[name]
        return result

    return {_component(title): rebase(value) for title, value in bundle["definitions"].items()}


def a2a_operation(*, card: bool, assistant: bool) -> dict[str, Any]:
    """Describe a native card or JSON-RPC endpoint, independent of its mount path."""
    common: dict[str, Any] = {
        "tags": ["A2A"],
        "externalDocs": {"url": A2A_SPEC},
        "description": "A2A 1.0, implemented by a2a-sdk 1.2.2. Native protobuf JSON fields and error codes apply. Tenant routing and push notifications are refused. "
        + (
            "Requires the workspace-and-target-bound Assistant capability bearer."
            if assistant
            else "Requires an identity allowed to use the target Agent."
        ),
    }
    if assistant:
        common["security"] = [{"BearerAuth": []}]
    if card:
        return {
            **common,
            "summary": "Discover Agent card",
            "responses": {
                "default": {
                    "description": "Authentication/authorization rejection or native protocol error.",
                    "content": {
                        "application/json": {
                            "schema": {"type": "object", "additionalProperties": True}
                        }
                    },
                },
                "200": {
                    "description": "Native AgentCard; capability fields describe what this Agent supports.",
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/A2AAgentCard"}
                        }
                    },
                },
            },
        }
    from a2a.server.routes.jsonrpc_dispatcher import JsonRpcDispatcher

    # The SDK owns method names and parameter messages. Push-notification
    # methods are advertised as unsupported by this platform's AgentCard.
    names = {key.removeprefix("A2A").lower(): key for key in a2a_components()}
    methods = {
        method: names[cast(Any, model).DESCRIPTOR.name.lower()]
        for method, model in JsonRpcDispatcher.METHOD_TO_MODEL.items()
        if "PushNotification" not in method
    }
    variants = [
        {
            "title": method,
            **object_schema(
                {
                    "jsonrpc": {"const": "2.0"},
                    "id": {"type": ["string", "integer"]},
                    "method": {"const": method},
                    "params": {"$ref": "#/components/schemas/" + title},
                },
                required=("jsonrpc", "id", "method", "params"),
            ),
        }
        for method, title in methods.items()
    ]
    results = ["Send Message Response", "Task", "List Tasks Response", "Agent Card"]
    response = object_schema(
        {
            "jsonrpc": {"const": "2.0"},
            "id": {"type": ["string", "integer", "null"]},
            "result": {
                "anyOf": [
                    {"$ref": "#/components/schemas/" + _component(title)} for title in results
                ]
            },
            "error": object_schema(
                {"code": {"type": "integer"}, "message": {"type": "string"}, "data": {}},
                required=("code", "message"),
            ),
        },
        required=("jsonrpc", "id"),
    )
    return {
        **common,
        "summary": "Call Agent through A2A",
        **request_body({"oneOf": variants}),
        "parameters": [
            {
                "name": "A2A-Version",
                "in": "header",
                "required": True,
                "schema": {"type": "string", "enum": ["1.0"]},
            }
        ],
        "responses": {
            "default": {
                "description": "Native JSON-RPC error or platform authentication/authorization rejection.",
                "content": {
                    "application/json": {
                        "schema": {
                            "anyOf": [response, {"$ref": "#/components/schemas/ErrorResponse"}]
                        }
                    }
                },
            },
            "200": {
                "description": "JSON-RPC result/error, or SSE StreamResponse events for streaming methods.",
                "content": {
                    "application/json": {"schema": response},
                    "text/event-stream": {
                        "schema": {
                            "type": "string",
                            "description": "SSE data contains native JSON-RPC StreamResponse results.",
                        }
                    },
                },
            },
        },
    }


def mcp_components() -> dict[str, Any]:
    """Read protocol parameter/result schemas from the installed MCP SDK."""
    from mcp.types import (
        CallToolRequest,
        ClientNotification,
        InitializeRequest,
        ListToolsRequest,
        PingRequest,
        ServerResult,
    )
    from pydantic import TypeAdapter

    components: dict[str, Any] = {}
    for name, value in {
        "MCPClientCall": InitializeRequest
        | PingRequest
        | ListToolsRequest
        | CallToolRequest
        | ClientNotification,
        "MCPServerResult": ServerResult,
    }.items():
        schema = TypeAdapter(value).json_schema(ref_template="#/components/schemas/MCP{model}")
        for key, definition in schema.pop("$defs", {}).items():
            components["MCP" + key] = definition
        components[name] = schema
    return components
