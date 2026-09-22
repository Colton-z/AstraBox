"""Provider-neutral Agent extension assignment routes."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from pydantic import BaseModel, ConfigDict

from astrabox.api.routes.response_envelope import ApiEnvelope
from astrabox.common.utils.api_response import success_response
from astrabox.common.utils.user_context import get_current_user_context
from astrabox.core.service.orchestrator.agent_extension_service import (
    AgentExtensionService,
)

_registered_on: int | None = None


class SetAgentExtensionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mcp_server_ids: list[str]
    skill_ids: list[str]


class AgentExtensionItem(BaseModel):
    """One selectable catalog entry, as ``ExtensionCatalogItem.public_view``
    exposes it. MCP servers and Skills share this shape; ``transport`` is set
    on the entries whose provider declares one."""

    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    description: str | None = None
    transport: str | None = None


class AgentExtensionCatalog(BaseModel):
    """The provider's catalog with this Agent's selection marked.

    The selected ids are read back from the stored assignments rather than from
    the request, so what the form shows ticked is what Session start resolves.
    """

    model_config = ConfigDict(extra="allow")

    agent_id: str
    mcp_servers: list[AgentExtensionItem]
    skills: list[AgentExtensionItem]
    selected_mcp_server_ids: list[str]
    selected_skill_ids: list[str]


def register_extension_routes(app: Any) -> None:
    """Register the Agent-scoped catalog read and assignment endpoints."""

    global _registered_on
    if _registered_on == id(app):
        return
    _registered_on = id(app)
    service = AgentExtensionService()

    @app.get(
        "/api/v1/agents/{agent_id}/extensions",
        response_model=ApiEnvelope[AgentExtensionCatalog],
        response_model_exclude_unset=True,
    )
    async def get_agent_extensions(agent_id: str, request: Request):
        user = await get_current_user_context(request)
        return success_response(await service.get_catalog(user, agent_id))

    @app.put(
        "/api/v1/agents/{agent_id}/extensions",
        response_model=ApiEnvelope[AgentExtensionCatalog],
        response_model_exclude_unset=True,
    )
    async def set_agent_extensions(
        agent_id: str,
        request: Request,
        body: SetAgentExtensionsRequest,
    ):
        user = await get_current_user_context(request)
        return success_response(
            await service.set_assignment(
                user,
                agent_id,
                mcp_server_ids=body.mcp_server_ids,
                skill_ids=body.skill_ids,
            )
        )


__all__ = ["register_extension_routes"]
