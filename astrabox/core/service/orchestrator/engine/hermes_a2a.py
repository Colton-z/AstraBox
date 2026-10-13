"""Assistant-owned peers for Hermes' bundled outbound A2A tools."""

from __future__ import annotations

import copy
from typing import Any
from urllib.parse import quote

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.assistant_a2a_auth import mint_assistant_a2a_capability
from astrabox.persistence.repository.assistant_catalog_repository import AssistantCatalogRepository
from astrabox.persistence.repository.platform_mcp_binding_repository import PlatformMCPBindingRepository

PEER_PREFIX = "astrabox/"


async def assistant_a2a_peers(
    *, assistant_id: str, binding_id: str | None, proxy_base_url: str,
) -> dict[str, Any]:
    """Resolve peers from the durable workspace owner, never model arguments."""
    from astrabox.core.service.orchestrator.service_registry import get_agent_service

    binding = await PlatformMCPBindingRepository().get_binding(binding_id or "")
    assistant = await AssistantCatalogRepository().get_assistant(assistant_id)
    if (
        not binding or binding.get("scope_kind") != "assistant_workspace"
        or binding.get("assistant_id") != assistant_id or not assistant
        or not binding.get("user_id") or assistant.get("owner_id") != binding["user_id"]
    ):
        raise APIError(code="MCP_BINDING_NOT_FOUND", message="Assistant binding not found", status_code=404)
    if not proxy_base_url.strip():
        raise APIError(code="MCP_PROXY_NOT_CONFIGURED", message="Assistant A2A proxy is not configured", status_code=503)
    user = UserContext(str(binding["user_id"]), org_id=assistant.get("org_id"))
    agents = await get_agent_service().list_agents(user)
    peers = {}
    for agent in agents:
        agent_id = str(agent["agent_id"])
        peers[f"{PEER_PREFIX}{agent_id}"] = {
            "url": f"{proxy_base_url.rstrip('/')}/api/v1/assistant-a2a/"
            f"{quote(str(binding_id), safe='')}/agents/{quote(agent_id, safe='')}",
            "auth": {"type": "bearer", "token": mint_assistant_a2a_capability(str(binding_id), agent_id)},
            "capabilities": [f"{agent.get('name') or agent_id} (agent_id: {agent_id})"],
        }
    return peers


def assistant_managed_config(config: dict[str, Any]) -> dict[str, Any]:
    """Keep owner settings while enabling the bundled Assistant collaboration toolset."""
    managed = copy.deepcopy(config)
    peers = managed.get("a2a_agents")
    if peers is not None and not isinstance(peers, dict):
        raise ValueError("a2a_agents must be a mapping")
    if isinstance(peers, dict) and any(str(name).startswith(PEER_PREFIX) for name in peers):
        raise ValueError("a2a_agents names beginning with astrabox/ belong to the platform")
    toolsets = managed.get("platform_toolsets")
    if toolsets is not None:
        if not isinstance(toolsets, dict):
            raise ValueError("platform_toolsets must be a mapping")
        for platform in ("cli", "cron"):
            if platform not in toolsets:
                continue
            names = toolsets[platform]
            if not isinstance(names, list):
                raise ValueError(f"platform_toolsets.{platform} must be a list")
            if "a2a" not in names:
                names.append("a2a")
    return managed
