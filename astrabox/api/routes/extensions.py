"""Provider-neutral extension routes for Agents and Assistants.

The deployment's extension provider, its catalog with a record's selection, the
selection itself, and the Agent-scoped management console the provider may
offer. Every route reaches the provider through the extension seam.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from astrabox.api.routes.response_envelope import ApiEnvelope
from astrabox.common.utils.api_response import success_response
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import get_current_user_context
from astrabox.core.service.orchestrator.agent_extension_service import (
    AgentExtensionService,
    configured_extension_provider,
    refuse_catalog_read,
)

_registered_on: int | None = None


class ExtensionProviderView(BaseModel):
    """The extension provider whose catalog this deployment offers.

    ``name`` is the ``ASTRABOX_EXTENSION_PROVIDER`` value that selects it;
    ``display_name`` and ``management_console`` are what the provider declares
    (``ExtensionProviderFeatures``).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    display_name: str
    management_console: bool


class DeploymentExtensionProvider(BaseModel):
    """The deployment's extension provider; ``provider`` is null when it runs no catalog."""

    model_config = ConfigDict(extra="forbid")

    provider: ExtensionProviderView | None


class ExtensionConsoleSessionRequest(BaseModel):
    """Which kind of catalog entry the management console is opened for."""

    model_config = ConfigDict(extra="forbid")

    section: Literal["mcp", "skills"] = "mcp"


class ExtensionConsoleSession(BaseModel):
    """Where to send the browser, and how long the grant lasts.

    The session itself lives in the cookies the route sets; the body carries no
    token, so a client that logs or forwards it discloses nothing. The
    obligations that come with declaring a response model are in
    :mod:`astrabox.api.routes.response_envelope`.
    """

    model_config = ConfigDict(extra="allow")

    open_url: str
    expires_in: int


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


class AssistantExtensionCatalog(BaseModel):
    """The provider's catalog with this Assistant's selection marked, as for an Agent."""

    model_config = ConfigDict(extra="allow")

    assistant_id: str
    mcp_servers: list[AgentExtensionItem]
    skills: list[AgentExtensionItem]
    selected_mcp_server_ids: list[str]
    selected_skill_ids: list[str]


def register_extension_routes(app: Any) -> None:
    """Register the extension provider, catalog, assignment and console endpoints."""

    global _registered_on
    if _registered_on == id(app):
        return
    _registered_on = id(app)
    service = AgentExtensionService()

    @app.get(
        "/api/v1/extension-provider",
        response_model=ApiEnvelope[DeploymentExtensionProvider],
        response_model_exclude_unset=True,
    )
    async def get_extension_provider(request: Request):
        """What the console offers for extension catalogs; reads no catalog."""
        await get_current_user_context(request)
        provider = configured_extension_provider()
        if provider is None:
            return success_response({"provider": None})
        return success_response(
            {
                "provider": {
                    "name": provider.name,
                    "display_name": provider.features.display_name,
                    "management_console": provider.features.management_console,
                }
            }
        )

    # The handler answers a JSONResponse because the session is carried by the
    # cookies it sets, and a Response object bypasses ``response_model``: the
    # declaration below documents the body without validating it, so it has to
    # be read against ``success_response(...)`` at the return statement.
    @app.post(
        "/api/v1/agents/{agent_id}/extension-console/session",
        response_model=ApiEnvelope[ExtensionConsoleSession],
        response_model_exclude_unset=True,
    )
    async def create_extension_console_session(
        agent_id: str,
        request: Request,
        body: ExtensionConsoleSessionRequest,
    ) -> JSONResponse:
        """Open the selected provider's management console for one Agent."""
        user = await get_current_user_context(request)
        agent = await service.assert_can_manage(user, agent_id)
        provider = configured_extension_provider()
        if provider is None:
            raise refuse_catalog_read()
        if not provider.features.management_console:
            raise APIError(
                code="EXTENSION_CONSOLE_UNSUPPORTED",
                message=(
                    f"the {provider.features.display_name} extension catalog offers "
                    "no management console"
                ),
                status_code=409,
            )
        session = await provider.open_management_console(
            user=user,
            agent_id=str(agent.get("agent_id") or agent_id),
            section=body.section,
        )
        response = JSONResponse(
            success_response({"open_url": session.open_url, "expires_in": session.expires_in})
        )
        for cookie in session.cookies:
            response.set_cookie(
                cookie.name,
                cookie.value,
                max_age=cookie.max_age,
                httponly=True,
                secure=request.url.scheme == "https",
                samesite="lax",
                path=cookie.path,
            )
        response.headers["Cache-Control"] = "no-store"
        return response

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


    @app.get(
        "/api/v1/assistants/{assistant_id}/extensions",
        response_model=ApiEnvelope[AssistantExtensionCatalog],
        response_model_exclude_unset=True,
    )
    async def get_assistant_extensions(assistant_id: str, request: Request):
        from astrabox.core.service.orchestrator.service_registry import get_assistant_service

        user = await get_current_user_context(request)
        return success_response(
            await get_assistant_service().get_extension_catalog(user, assistant_id)
        )

    @app.put(
        "/api/v1/assistants/{assistant_id}/extensions",
        response_model=ApiEnvelope[AssistantExtensionCatalog],
        response_model_exclude_unset=True,
    )
    async def set_assistant_extensions(
        assistant_id: str,
        request: Request,
        body: SetAgentExtensionsRequest,
    ):
        from astrabox.core.service.orchestrator.service_registry import get_assistant_service

        user = await get_current_user_context(request)
        return success_response(
            await get_assistant_service().set_extension_assignment(
                user,
                assistant_id,
                mcp_server_ids=body.mcp_server_ids,
                skill_ids=body.skill_ids,
            )
        )


__all__ = ["register_extension_routes"]
