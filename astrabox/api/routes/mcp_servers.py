"""Administrator API for registered MCP servers and Agent assignments."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

from astrabox.api.routes.response_envelope import ApiEnvelope
from astrabox.common.utils.api_response import success_response
from astrabox.common.utils.user_context import get_current_user_context
from astrabox.core.service.orchestrator.mcp_registry_service import (
    MCPRegistryService,
)

_registered_on: int | None = None


class CreateMCPServerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    url: str
    transport: str = "streamable_http"
    description: str | None = None
    enabled: bool = True


class UpdateMCPServerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    url: str | None = None
    transport: str | None = None
    description: str | None = None
    enabled: bool | None = None


class SetAgentMCPServersRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mcp_server_ids: list[str]


# ── response payloads ───────────────────────────────────────────────────────
#
# The payload half of ``ApiEnvelope[...]``; the obligations that come with
# declaring a response model are documented in
# :mod:`astrabox.api.routes.response_envelope`.


class MCPServer(BaseModel):
    """One registered remote MCP server.

    ``MCPRegistryService._view`` returns the stored document minus its private
    keys, so the fields and their order are the document's own.
    """

    model_config = ConfigDict(extra="allow")

    mcp_server_id: str
    created_at: str
    updated_at: str
    org_id: str
    name: str
    description: str | None
    url: str
    transport: str
    enabled: bool
    created_by: str | None
    updated_by: str | None


class MCPServerList(BaseModel):
    """Every MCP server registered in the organization."""

    model_config = ConfigDict(extra="allow")

    mcp_servers: list[MCPServer]


class AgentMCPServerAssignment(BaseModel):
    """The built-in-catalog MCP servers selected for one Agent.

    ``missing_mcp_server_ids`` names assignments whose server is gone, so the
    console can show the gap rather than a silently shorter list.
    """

    model_config = ConfigDict(extra="allow")

    agent_id: str
    mcp_server_ids: list[str]
    mcp_servers: list[MCPServer]
    missing_mcp_server_ids: list[str]


def register_mcp_server_routes(app: Any) -> None:
    global _registered_on
    if _registered_on == id(app):
        return
    _registered_on = id(app)

    service = MCPRegistryService()

    @app.post(
        "/api/v1/admin/mcp-servers",
        response_model=ApiEnvelope[MCPServer],
        response_model_exclude_unset=True,
    )
    async def create_mcp_server(request: Request, body: CreateMCPServerRequest):
        user = await get_current_user_context(request)
        return success_response(
            await service.create_server(
                user,
                name=body.name,
                url=body.url,
                transport=body.transport,
                description=body.description,
                enabled=body.enabled,
            )
        )

    @app.get(
        "/api/v1/admin/mcp-servers",
        response_model=ApiEnvelope[MCPServerList],
        response_model_exclude_unset=True,
    )
    async def list_mcp_servers(request: Request):
        user = await get_current_user_context(request)
        return success_response({"mcp_servers": await service.list_servers(user)})

    @app.get(
        "/api/v1/admin/mcp-servers/{mcp_server_id}",
        response_model=ApiEnvelope[MCPServer],
        response_model_exclude_unset=True,
    )
    async def get_mcp_server(mcp_server_id: str, request: Request):
        user = await get_current_user_context(request)
        return success_response(await service.get_server(user, mcp_server_id))

    @app.patch(
        "/api/v1/admin/mcp-servers/{mcp_server_id}",
        response_model=ApiEnvelope[MCPServer],
        response_model_exclude_unset=True,
    )
    async def update_mcp_server(
        mcp_server_id: str, request: Request, body: UpdateMCPServerRequest
    ):
        user = await get_current_user_context(request)
        updates = body.model_dump(exclude_unset=True)
        return success_response(
            await service.update_server(user, mcp_server_id, updates)
        )

    # 204 is declared, not defaulted: the handler answers 204 with no body, and
    # the default 200 documents a body no caller ever receives.
    @app.delete("/api/v1/admin/mcp-servers/{mcp_server_id}", status_code=204)
    async def delete_mcp_server(mcp_server_id: str, request: Request):
        user = await get_current_user_context(request)
        await service.delete_server(user, mcp_server_id)
        return Response(status_code=204)

    @app.get(
        "/api/v1/admin/agents/{agent_id}/mcp-servers",
        response_model=ApiEnvelope[AgentMCPServerAssignment],
        response_model_exclude_unset=True,
    )
    async def get_agent_mcp_servers(agent_id: str, request: Request):
        user = await get_current_user_context(request)
        return success_response(await service.get_agent_assignment(user, agent_id))

    @app.put(
        "/api/v1/admin/agents/{agent_id}/mcp-servers",
        response_model=ApiEnvelope[AgentMCPServerAssignment],
        response_model_exclude_unset=True,
    )
    async def set_agent_mcp_servers(
        agent_id: str, request: Request, body: SetAgentMCPServersRequest
    ):
        user = await get_current_user_context(request)
        return success_response(
            await service.set_agent_assignment(
                user, agent_id, body.mcp_server_ids
            )
        )


__all__ = ["register_mcp_server_routes"]
