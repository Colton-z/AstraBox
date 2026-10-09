"""Deliver platform-owned MCP endpoints without admitting Agent MCP declarations."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from astrabox.core.service.orchestrator.runtime.mcp_servers import (
    is_platform_mcp_server,
    prepared_slot_mcp_deployment_id,
    runtime_mcp_servers_for_binding,
    template_mcp_servers,
)

PLATFORM_MCP_SERVERS_ENV_VAR = "ASTRABOX_PLATFORM_MCP_SERVERS"


def platform_mcp_servers(
    template: Any, *, deployment_settings: Any, deployment_id: str,
) -> dict[str, Any]:
    """Consume the binding renderer's synthesized and explicitly platform servers."""
    declared = {
        name: config
        for name, config in template_mcp_servers(getattr(template, "mcp_servers", None)).items()
        if is_platform_mcp_server(config)
    }
    return runtime_mcp_servers_for_binding(
        declared,
        proxy_base_url=str(getattr(deployment_settings, "mcp_proxy_base_url", "") or ""),
        deployment_id=deployment_id,
        session_source_type=getattr(template, "session_source_type", None),
        agent_id=getattr(template, "agent_id", None),
        artifacts_enabled=getattr(template, "artifacts_enabled", True),
    )


def platform_mcp_environment(servers: dict[str, Any]) -> dict[str, str]:
    """The image extension's process input, preserving each capability URL verbatim."""
    entries = []
    for name, config in servers.items():
        if config.get("type") != "http" or not isinstance(config.get("url"), str) or not config["url"]:
            raise ValueError(f"platform MCP server {name!r} requires an HTTP URL")
        entries.append({"name": name, "url": config["url"]})
    return {PLATFORM_MCP_SERVERS_ENV_VAR: json.dumps(entries, separators=(",", ":"))}


def startup_mcp_deployment_id(context: Any) -> str:
    """A claimed process retains its slot address; fresh starts use the Session."""
    if context.prepared_manifest is not None:
        return prepared_slot_mcp_deployment_id(context.prepared_manifest.get("slot_id", ""))
    return str(context.platform_mcp_deployment_id or context.session_id)


def request_with_platform_mcp(
    request: Any, *, template: Any, deployment_settings: Any, deployment_id: str,
) -> Any:
    """Compose boot-time MCP input only for images declaring that process contract."""
    if not request.platform_mcp_servers_env:
        return request
    env = platform_mcp_environment(platform_mcp_servers(
        template, deployment_settings=deployment_settings, deployment_id=deployment_id,
    ))
    return replace(request, env={**dict(request.env), **env})
