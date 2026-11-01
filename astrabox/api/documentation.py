"""OpenAPI metadata for handlers whose service owns parsing and validation.

These declarations document the existing wire contract. They do not replace a
service's parser, normalize requests, or validate outgoing response objects.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, TypeAdapter

from astrabox.seams.skills import SkillSource


class ErrorDetails(BaseModel):
    """The structured diagnostics emitted by ``APIError.to_error_envelope``."""

    model_config = ConfigDict(extra="allow")

    code: str
    status_code: int
    category: str
    retryable: bool
    owner: str
    user_message: str
    debug_message: str | None = None
    evidence: dict[str, Any] | None = None
    cause_code: str | None = None


class ErrorResponse(BaseModel):
    """The JSON body emitted by the shared API exception handlers."""

    code: str
    message: str
    data: Any
    error: ErrorDetails


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    422: {"model": ErrorResponse, "description": "Request validation failed (INVALID_REQUEST)."},
    "default": {
        "model": ErrorResponse,
        "description": (
            "Request refused or execution failed. Inspect code and error for the reason; "
            "data carries operation-specific evidence. Transport status is authoritative."
        ),
    },
}


def object_schema(
    properties: dict[str, Any],
    *,
    required: tuple[str, ...] = (),
    description: str | None = None,
    additional_properties: bool = True,
) -> dict[str, Any]:
    """Describe a JSON object without changing its existing service validation."""
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": additional_properties,
    }
    if required:
        schema["required"] = list(required)
    if description:
        schema["description"] = description
    return schema


def request_body(schema: dict[str, Any], *, required: bool = True) -> dict[str, Any]:
    """Describe JSON read directly from ``Request`` rather than a body parameter."""
    return {
        "requestBody": {"required": required, "content": {"application/json": {"schema": schema}}}
    }


def query_parameter(
    name: str,
    kind: str,
    description: str,
    *,
    default: Any = None,
    required: bool = False,
    **constraints: Any,
) -> dict[str, Any]:
    """Describe a query value parsed by the handler, retaining its existing errors."""
    schema = {"type": kind, **constraints}
    if default is not None:
        schema["default"] = default
    return {
        "name": name,
        "in": "query",
        "required": required,
        "description": description,
        "schema": schema,
    }


def configure_openapi(app: FastAPI) -> None:
    """Include native routes and use the actual identity policy for auth metadata."""
    original = app.openapi

    def document() -> dict[str, Any]:
        from astrabox.api.native_documentation import a2a_components, mcp_components
        from astrabox.providers.identity_oidc import SESSION_COOKIE
        from astrabox.web.identity_middleware import _is_exempt_path, _required_api_scope

        schema = deepcopy(original())
        schema.setdefault("components", {}).setdefault("securitySchemes", {}).update(
            {
                "BearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "Bearer credential; the operation identifies its issuer, capability binding or configured identity provider. OAuth API token scopes are checked where present.",
                },
                "BrowserSession": {
                    "type": "apiKey",
                    "in": "cookie",
                    "name": SESSION_COOKIE,
                    "description": "Browser session established by the built-in OIDC login flow.",
                },
            }
        )
        schemes = schema["components"]["securitySchemes"]
        schemes["MCPClientToken"] = {
            "type": "http",
            "scheme": "bearer",
            "description": "AstraBox MCP client key, narrowed to its read/converse scope.",
        }
        resolver = app.state.web_identity_resolver
        schema["info"]["x-web-identity"] = type(resolver).__name__
        has_a2a = False
        for route in _routes(app.routes):
            path = route.path_format
            native = getattr(route, "openapi_extra", None)
            if native and not getattr(route, "include_in_schema", True):
                for method in sorted(route.methods or ()):
                    if (
                        method == "HEAD"
                        and "GET" in route.methods
                        and not isinstance(route, APIRoute)
                    ):
                        continue
                    operation = deepcopy(native)
                    operation["operationId"] = re.sub(
                        r"\W", "_", f"{route.name}_{path}_{method.lower()}"
                    )
                    operation.setdefault("parameters", []).extend(
                        [
                            {
                                "name": name,
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            }
                            for name in route.param_convertors
                        ]
                    )
                    schema["paths"].setdefault(path, {})[method.lower()] = operation
                has_a2a |= route.name in {
                    "agent_a2a_rpc",
                    "assistant_a2a_rpc",
                    "agent_card",
                    "assistant_a2a_card",
                }
            for method in route.methods or ():
                operation = schema["paths"].get(path, {}).get(method.lower())
                if operation is None:
                    continue
                module = getattr(route.endpoint, "__module__", "").rsplit(".", 1)[-1]
                operation.setdefault("tags", [module.replace("_", " ").title()])
                scope = _required_api_scope({"method": method}, path)
                if scope:
                    operation["x-required-api-scope"] = scope
                if "security" not in operation and not _is_exempt_path(path):
                    security = _identity_security(resolver, schemes, path)
                    if security is not None:
                        operation["security"] = security
                    else:
                        operation["x-authentication"] = (
                            f"Defined by the installed identity provider {type(resolver).__module__}.{type(resolver).__name__}."
                        )
                if operation.pop("x-mcp-client-token", False):
                    operation.setdefault("security", []).append({"MCPClientToken": []})
        schema["components"]["schemas"].update(mcp_components())
        if has_a2a:
            schema["components"]["schemas"].update(a2a_components())
        return schema

    app.openapi = document  # type: ignore[method-assign]


def _identity_security(
    resolver: Any, schemes: dict[str, Any], path: str
) -> list[dict[str, list[str]]] | None:
    """Describe built-in resolver credentials from their actual configuration."""
    from astrabox.providers.identity_oidc import OidcSessionWebIdentityResolver
    from astrabox.providers.identity_sso import (
        TrustedHeaderWebIdentityResolver,
        VerifiedJwtWebIdentityResolver,
    )
    from astrabox.web.identity_middleware import _is_admin_path

    if getattr(resolver, "is_no_auth_default", False):
        return [{}]
    if isinstance(resolver, OidcSessionWebIdentityResolver):
        return [{"BearerAuth": []}, {"BrowserSession": []}]
    if isinstance(resolver, VerifiedJwtWebIdentityResolver):
        security: list[dict[str, list[str]]] = [{"BearerAuth": []}]
        if not resolver._strict and not _is_admin_path(path):
            security.append({})
        return security
    if isinstance(resolver, TrustedHeaderWebIdentityResolver):
        schemes["TrustedIdentity"] = {
            "type": "apiKey",
            "in": "header",
            "name": resolver._user_header,
            "description": f"Identity asserted by the authenticating gateway. Roles come from {resolver._groups_header}; the ingress owns browser login.",
        }
        gateway: dict[str, list[str]] = {}
        if resolver._gateway_secret:
            schemes["IdentityGatewaySecret"] = {
                "type": "apiKey",
                "in": "header",
                "name": resolver._gateway_secret_header,
                "description": "The configured proxy integrity credential; required independently of user-header strictness.",
            }
            gateway["IdentityGatewaySecret"] = []
        security = [{"TrustedIdentity": [], **gateway}]
        if not resolver._strict and not _is_admin_path(path):
            security.append(gateway)
        return security
    # A plugin's arbitrary credential format cannot be inferred from resolve().
    return None


def _routes(routes: Any) -> Any:
    for route in routes:
        inner = getattr(route, "original_router", None) or getattr(route, "router", None)
        if inner is not None and getattr(inner, "routes", None) is not None:
            yield from _routes(inner.routes)
        elif hasattr(route, "path_format") and hasattr(route, "methods"):
            yield route


def success_schema(data: dict[str, Any]) -> dict[str, Any]:
    """Document the existing success envelope around an operation's data."""
    return object_schema(
        {"code": {"type": "string"}, "message": {"type": "string"}, "data": data},
        required=("code", "message", "data"),
    )


def json_response(data: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Describe JSON output without applying response validation or coercion."""
    return {200: {"content": {"application/json": {"schema": success_schema(data)}}}}


BINARY_RESPONSE = {
    200: {
        "description": "Raw attachment bytes; Content-Disposition supplies the filename and Content-Length the byte count.",
        "headers": {
            "Content-Disposition": {"schema": {"type": "string"}},
            "Content-Length": {"schema": {"type": "integer"}},
        },
        "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
    }
}


def form_schema(fields: list[dict[str, Any]], *, update: bool = False) -> dict[str, Any]:
    """Expose the fields the existing form validator owns as JSON Schema."""
    scalar_types = {
        "string": "string",
        "text": "string",
        "env_ref": "string",
        "enum": "string",
        "boolean": "boolean",
        "integer": "integer",
    }
    properties: dict[str, Any] = {}
    required: list[str] = []
    for field in fields:
        name = str(field.get("path") or field["key"])
        kind = str(field["type"])
        item_schema = field.get("item_schema")
        if kind in scalar_types:
            value: dict[str, Any] = {"type": scalar_types[kind]}
            if field.get("enum"):
                value["enum"] = field["enum"]
        elif kind in {"string_list", "skill_list", "object_list"}:
            item = (
                {"type": "string"}
                if kind != "object_list"
                else {"type": "object", "additionalProperties": True}
            )
            if kind == "skill_list":
                item = {"anyOf": [{"type": "string"}, TypeAdapter(SkillSource).json_schema()]}
            if item_schema:
                item = form_schema(item_schema)
            value = {"type": "array", "items": item}
        elif kind == "key_value":
            value = {"type": "object", "additionalProperties": {"type": "string"}}
        elif kind == "object":
            value = (
                form_schema(item_schema)
                if item_schema
                else {"type": "object", "additionalProperties": True}
            )
        else:
            raise ValueError(f"undocumented form field type: {kind}")
        if not field.get("required"):
            value = {"anyOf": [value, {"type": "null"}]}
        if "default" in field:
            value["default"] = field["default"]
        parent = properties
        segments = name.split(".")
        for segment in segments[:-1]:
            parent = parent.setdefault(segment, object_schema({}))["properties"]
        parent[segments[-1]] = value
        if field.get("required") and not update and len(segments) == 1:
            required.append(name)
    return object_schema(properties, required=tuple(required), additional_properties=False)
