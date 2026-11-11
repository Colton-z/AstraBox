"""Assistant-owned peers for Hermes' bundled outbound A2A tools."""

from __future__ import annotations

import copy
from typing import Any
from urllib.parse import quote

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.assistant_a2a_auth import assistant_a2a_targets
from astrabox.persistence.repository.assistant_catalog_repository import AssistantCatalogRepository
from astrabox.persistence.repository.platform_mcp_binding_repository import PlatformMCPBindingRepository

PEER_PREFIX = "astrabox/"


async def assistant_a2a_peers(
    *, assistant_id: str, binding_id: str | None, proxy_base_url: str,
) -> dict[str, Any]:
    """The Assistant's A2A targets as Hermes' ``a2a_agents`` peers."""
    peers = {}
    for target in await assistant_a2a_targets(
        assistant_id=assistant_id, binding_id=binding_id, proxy_base_url=proxy_base_url,
    ):
        peer_key = f"{PEER_PREFIX}{target['agent_id']}"
        peers[peer_key] = {
            "url": target["url"],
            "auth": {"type": "bearer", "token": target["token"]},
            "capabilities": [
                f"{target['name']}; call exactly "
                f'a2a_call(agent="{peer_key}", message=...)',
            ],
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
