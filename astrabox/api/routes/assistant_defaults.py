"""The deployment's Assistant defaults: the layer every Assistant inherits.

Administrators only (the ``/api/v1/admin`` prefix). The layer is what the
deployment's Assistants can do; see ``assistant_layers`` for how it combines
with an Assistant's own fields.
"""

from __future__ import annotations

from astrabox.seams.skills import SkillDescriptor

from typing import Any

from fastapi import Request
from pydantic import BaseModel, ConfigDict

from astrabox.api.routes.extensions import AgentExtensionItem, SetAgentExtensionsRequest
from astrabox.api.routes.response_envelope import ApiEnvelope
from astrabox.common.utils.api_response import success_response
from astrabox.common.utils.user_context import get_current_user_context
from astrabox.core.service.orchestrator.assistant.assistant_defaults import AssistantDefaultsService

_registered_on: int | None = None


class AssistantDefaultsFields(BaseModel):
    """The layer's authored fields, named as an Assistant's own are."""

    model_config = ConfigDict(extra="forbid")

    #: The RuntimeProfile new Assistants use; unset uses the only one that runs Assistants.
    runtime_profile_name: str | None = None
    #: The model Assistants use unless their own override names one.
    model_name: str | None = None
    #: Instructions every Assistant receives before its own.
    instructions: str | None = None
    skills: list[SkillDescriptor] | None = None
    mcp_servers: dict[str, Any] | None = None
    packages: list[dict[str, Any]] | None = None
    engine_options: dict[str, Any] | None = None
    credential_vault_ids: list[str] | None = None


class AssistantDefaults(AssistantDefaultsFields):
    """The layer as stored, with the RuntimeProfile new Assistants would use now."""

    model_config = ConfigDict(extra="forbid")

    #: What a new Assistant would be created in: the layer's choice, else the
    #: deployment's only RuntimeProfile that runs Assistants, else null.
    effective_runtime_profile_name: str | None = None
    #: The enabled RuntimeProfiles whose engine runs Assistants.
    assistant_runtime_profile_names: list[str]
    #: The layer's MCP servers selected from extension catalogs, as stored:
    #: ``{provider, item_id}`` rows. Written only through ``/extensions``.
    mcp_assignments: list[dict[str, Any]] | None = None
    #: The layer's Skills selected from the extension catalog, as stored:
    #: ``skill_ids`` and the Git ``skills`` they compiled to. Written only
    #: through ``/extensions``.
    extension_catalog: dict[str, Any] | None = None
    updated_at: str | None = None


class AssistantDefaultsExtensionCatalog(BaseModel):
    """The extension catalog with the deployment layer's selection marked."""

    model_config = ConfigDict(extra="allow")

    mcp_servers: list[AgentExtensionItem]
    skills: list[AgentExtensionItem]
    selected_mcp_server_ids: list[str]
    selected_skill_ids: list[str]


def register_assistant_defaults_routes(app: Any) -> None:
    global _registered_on
    if _registered_on == id(app):
        return
    _registered_on = id(app)
    service = AssistantDefaultsService()

    @app.get(
        "/api/v1/admin/assistant-defaults",
        response_model=ApiEnvelope[AssistantDefaults],
        response_model_exclude_unset=True,
    )
    async def get_assistant_defaults(request: Request):
        await get_current_user_context(request)
        return success_response(await service.view())

    @app.put(
        "/api/v1/admin/assistant-defaults",
        response_model=ApiEnvelope[AssistantDefaults],
        response_model_exclude_unset=True,
    )
    async def set_assistant_defaults(request: Request, body: AssistantDefaultsFields):
        user = await get_current_user_context(request)
        return success_response(await service.put(user, body.model_dump()))

    @app.get(
        "/api/v1/admin/assistant-defaults/extensions",
        response_model=ApiEnvelope[AssistantDefaultsExtensionCatalog],
        response_model_exclude_unset=True,
    )
    async def get_assistant_defaults_extensions(request: Request):
        await get_current_user_context(request)
        return success_response(await service.extensions())

    @app.put(
        "/api/v1/admin/assistant-defaults/extensions",
        response_model=ApiEnvelope[AssistantDefaultsExtensionCatalog],
        response_model_exclude_unset=True,
    )
    async def set_assistant_defaults_extensions(request: Request, body: SetAgentExtensionsRequest):
        user = await get_current_user_context(request)
        return success_response(
            await service.set_extensions(
                user, mcp_server_ids=body.mcp_server_ids, skill_ids=body.skill_ids
            )
        )


__all__ = ["register_assistant_defaults_routes"]
