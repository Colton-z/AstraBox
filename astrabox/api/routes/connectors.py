"""Connector routes: a user's connections, the connectors an Agent or Assistant offers, set-up.

``/api/v1/connectors`` is each signed-in user's own: listing connectors,
starting a consent and disconnecting. The vendor returns the browser to
``/connector-hub/oauth/callback``, which is handed to the connector hub; the hub
then returns it to the console.
``/api/v1/assistants/{id}/connectors`` turns a connector on for one of the
caller's Assistants, which uses its owner's connections in all its
conversations. ``/api/v1/agents/{id}/connectors`` turns one on for an Agent the
caller manages, whose callers each use their own connections in conversations
they start. ``/api/v1/admin/connectors`` is where an administrator
registers a connector with the hub, connects their own account so the hub can
list its tools, and publishes them.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request, Response
from pydantic import BaseModel, ConfigDict

from astrabox.api.routes.response_envelope import ApiEnvelope
from astrabox.common.utils.api_response import success_response
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import get_current_user_context
from astrabox.core.service.connectors.service import (
    AgentConnectors,
    AssistantConnectors,
    ConnectorService,
)
from astrabox.core.service.orchestrator.service_registry import (
    get_agent_service,
    get_assistant_service,
)

_registered_on: int | None = None
#: Where the connector hub's browser-facing callback is served on the console's
#: origin; ``ASTRABOX_CONTEXTFORGE_PUBLIC_URL`` is the origin followed by it.
HUB_PUBLIC_PATH = "/connector-hub"


class Connector(BaseModel):
    """One work app: whether it is set up, and the caller's connection to it."""

    model_config = ConfigDict(extra="forbid")

    connector_id: str
    name: str
    registration: str
    docs_url: str | None = None
    #: User view: set up and published, so it can be connected.
    available: bool | None = None
    #: User view: ``connected``, ``needs_reauth`` or ``disconnected``.
    status: str | None = None
    #: Administrator view.
    deployed: bool | None = None
    configured: bool | None = None
    published: bool | None = None
    redirect_uri: str | None = None
    server_id: str | None = None


class AuthorizeResponse(BaseModel):
    authorize_url: str


class ConfigureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The deployment's OAuth app, for a vendor without self-registration.
    client_id: str | None = None
    client_secret: str | None = None


class AssistantConnectorState(BaseModel):
    assistant_id: str
    enabled: list[str]


class AgentConnectorState(BaseModel):
    agent_id: str
    enabled: list[str]


class AdoptedConnector(BaseModel):
    """An app just connected, and the caller's Assistants it was turned on in."""

    connector_id: str
    assistant_ids: list[str]


def register_connector_routes(app: Any) -> None:
    global _registered_on
    if _registered_on == id(app):
        return
    _registered_on = id(app)

    service = ConnectorService()
    assistants = AssistantConnectors(service_getter=get_assistant_service)
    agents = AgentConnectors(service_getter=get_agent_service)

    @app.get("/api/v1/connectors", response_model=ApiEnvelope[list[Connector]])
    async def list_connectors(request: Request):
        user = await get_current_user_context(request)
        return success_response(await service.list_for_user(user))

    @app.post(
        "/api/v1/connectors/{connector_id}/authorize",
        response_model=ApiEnvelope[AuthorizeResponse],
    )
    async def authorize_connector(connector_id: str, request: Request):
        user = await get_current_user_context(request)
        return success_response(await service.start(user, connector_id))

    @app.get(f"{HUB_PUBLIC_PATH}/oauth/callback", include_in_schema=False)
    async def connector_hub_callback(request: Request):
        # The hub's public address is this path on the console's origin, so
        # the vendor's redirect lands here; only this one hub path is exposed.
        answer = await service.hub_callback(str(request.url.query))
        headers = {
            name: value
            for name, value in answer.headers.items()
            if name.lower() in {"location", "content-type"}
        }
        return Response(content=answer.content, status_code=answer.status_code, headers=headers)

    @app.post(
        "/api/v1/connectors/{connector_id}/assistants",
        response_model=ApiEnvelope[AdoptedConnector],
    )
    async def offer_connector_in_assistants(connector_id: str, request: Request):
        # The console calls this when the vendor returns the browser after a
        # consent: an app its owner connects is offered by their Assistants,
        # except those where the owner turned it off.
        user = await get_current_user_context(request)
        if connector_id not in await service.connected(user):
            raise APIError(
                code="CONNECTOR_NOT_CONNECTED",
                message=f"{connector_id} is not connected for this user; connect it first",
                status_code=409,
            )
        changed = await assistants.adopt(user, [connector_id])
        return success_response({"connector_id": connector_id, "assistant_ids": changed})

    @app.delete("/api/v1/connectors/{connector_id}/connection")
    async def disconnect_connector(connector_id: str, request: Request):
        user = await get_current_user_context(request)
        removed = await service.disconnect(user, connector_id)
        return success_response({"connector_id": connector_id, "removed": removed})

    @app.get(
        "/api/v1/assistants/{assistant_id}/connectors",
        response_model=ApiEnvelope[AssistantConnectorState],
    )
    async def assistant_connectors(assistant_id: str, request: Request):
        user = await get_current_user_context(request)
        enabled = await assistants.enabled(user, assistant_id)
        return success_response({"assistant_id": assistant_id, "enabled": enabled})

    @app.put(
        "/api/v1/assistants/{assistant_id}/connectors/{connector_id}",
        response_model=ApiEnvelope[AssistantConnectorState],
    )
    async def enable_assistant_connector(assistant_id: str, connector_id: str, request: Request):
        user = await get_current_user_context(request)
        enabled = await assistants.set_enabled(user, assistant_id, connector_id, enabled=True)
        return success_response({"assistant_id": assistant_id, "enabled": enabled})

    @app.delete(
        "/api/v1/assistants/{assistant_id}/connectors/{connector_id}",
        response_model=ApiEnvelope[AssistantConnectorState],
    )
    async def disable_assistant_connector(assistant_id: str, connector_id: str, request: Request):
        user = await get_current_user_context(request)
        enabled = await assistants.set_enabled(user, assistant_id, connector_id, enabled=False)
        return success_response({"assistant_id": assistant_id, "enabled": enabled})

    @app.get(
        "/api/v1/agents/{agent_id}/connectors",
        response_model=ApiEnvelope[AgentConnectorState],
    )
    async def agent_connectors(agent_id: str, request: Request):
        user = await get_current_user_context(request)
        enabled = await agents.enabled(user, agent_id)
        return success_response({"agent_id": agent_id, "enabled": enabled})

    @app.put(
        "/api/v1/agents/{agent_id}/connectors/{connector_id}",
        response_model=ApiEnvelope[AgentConnectorState],
    )
    async def enable_agent_connector(agent_id: str, connector_id: str, request: Request):
        user = await get_current_user_context(request)
        enabled = await agents.set_enabled(user, agent_id, connector_id, enabled=True)
        return success_response({"agent_id": agent_id, "enabled": enabled})

    @app.delete(
        "/api/v1/agents/{agent_id}/connectors/{connector_id}",
        response_model=ApiEnvelope[AgentConnectorState],
    )
    async def disable_agent_connector(agent_id: str, connector_id: str, request: Request):
        user = await get_current_user_context(request)
        enabled = await agents.set_enabled(user, agent_id, connector_id, enabled=False)
        return success_response({"agent_id": agent_id, "enabled": enabled})

    @app.get("/api/v1/admin/connectors", response_model=ApiEnvelope[list[Connector]])
    async def admin_list_connectors(request: Request):
        await get_current_user_context(request)
        return success_response(await service.list_for_admin())

    @app.put("/api/v1/admin/connectors/{connector_id}", response_model=ApiEnvelope[Connector])
    async def admin_configure_connector(
        connector_id: str, body: ConfigureRequest, request: Request
    ):
        await get_current_user_context(request)
        return success_response(
            await service.configure(
                connector_id, client_id=body.client_id, client_secret=body.client_secret
            )
        )

    @app.post(
        "/api/v1/admin/connectors/{connector_id}/verify",
        response_model=ApiEnvelope[AuthorizeResponse],
    )
    async def admin_verify_connector(connector_id: str, request: Request):
        await get_current_user_context(request)
        return success_response(await service.verify(connector_id))

    @app.post(
        "/api/v1/admin/connectors/{connector_id}/publish", response_model=ApiEnvelope[Connector]
    )
    async def admin_publish_connector(connector_id: str, request: Request):
        await get_current_user_context(request)
        return success_response(await service.publish(connector_id))

    @app.delete("/api/v1/admin/connectors/{connector_id}")
    async def admin_remove_connector(connector_id: str, request: Request):
        await get_current_user_context(request)
        await service.remove(connector_id)
        return success_response({"connector_id": connector_id, "configured": False})
