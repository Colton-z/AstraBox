"""Routes for finding Agents: categories, tags and each user's favorites.

Reading categories and tags, and favoriting, is open to any signed-in user;
defining categories is under ``/api/v1/admin``, which the identity middleware
reserves for platform administrators.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field

from astrabox.api.routes.response_envelope import ApiEnvelope
from astrabox.common.utils.api_response import success_response
from astrabox.common.utils.user_context import get_current_user_context
from astrabox.core.service.orchestrator.agent import agent_catalog
from astrabox.core.service.orchestrator.service_registry import get_agent_service

_registered_on: int | None = None


class AgentCategory(BaseModel):
    """One Agent category, in the administrator's order (``position``, from 0)."""

    category_id: str
    name: str
    position: int


class AdminAgentCategory(AgentCategory):
    """A category with the number of Agents it holds."""

    agents: int


class AgentCategoryName(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)


class AgentCategoryOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_ids: list[str]


class DeletedAgentCategory(BaseModel):
    category_id: str
    #: How many Agents the category was taken off.
    unbound_agents: int


class AgentTag(BaseModel):
    tag: str
    #: How many Agents the caller may see carry it.
    agents: int


class AgentFavorite(BaseModel):
    agent_id: str
    favorited: bool


def register_agent_catalog_routes(app: Any) -> None:
    global _registered_on
    if _registered_on == id(app):
        return
    _registered_on = id(app)

    @app.get(
        "/api/v1/agent-categories",
        response_model=ApiEnvelope[list[AgentCategory]],
    )
    async def list_agent_categories(request: Request):
        await get_current_user_context(request)
        return success_response(await agent_catalog.list_categories())

    @app.get(
        "/api/v1/agent-tags",
        response_model=ApiEnvelope[list[AgentTag]],
    )
    async def list_agent_tags(request: Request):
        user = await get_current_user_context(request)
        return success_response(await get_agent_service().list_agent_tags(user))

    @app.put(
        "/api/v1/agents/{agent_id}/favorite",
        response_model=ApiEnvelope[AgentFavorite],
    )
    async def favorite_agent(agent_id: str, request: Request):
        user = await get_current_user_context(request)
        result = await get_agent_service().set_favorite(user, agent_id, favorite=True)
        return success_response(result)

    @app.delete(
        "/api/v1/agents/{agent_id}/favorite",
        response_model=ApiEnvelope[AgentFavorite],
    )
    async def unfavorite_agent(agent_id: str, request: Request):
        user = await get_current_user_context(request)
        result = await get_agent_service().set_favorite(user, agent_id, favorite=False)
        return success_response(result)

    @app.get(
        "/api/v1/admin/agent-categories",
        response_model=ApiEnvelope[list[AdminAgentCategory]],
    )
    async def list_admin_agent_categories(request: Request):
        await get_current_user_context(request)
        counts = await get_agent_service().category_agent_counts()
        rows = await agent_catalog.list_categories()
        return success_response(
            [{**row, "agents": counts.get(row["category_id"], 0)} for row in rows]
        )

    @app.post(
        "/api/v1/admin/agent-categories",
        response_model=ApiEnvelope[AgentCategory],
    )
    async def create_agent_category(payload: AgentCategoryName, request: Request):
        await get_current_user_context(request)
        return success_response(await agent_catalog.create_category(payload.name))

    @app.put(
        "/api/v1/admin/agent-categories/order",
        response_model=ApiEnvelope[list[AgentCategory]],
    )
    async def reorder_agent_categories(payload: AgentCategoryOrder, request: Request):
        await get_current_user_context(request)
        return success_response(await agent_catalog.reorder_categories(payload.category_ids))

    @app.put(
        "/api/v1/admin/agent-categories/{category_id}",
        response_model=ApiEnvelope[AgentCategory],
    )
    async def rename_agent_category(
        category_id: str, payload: AgentCategoryName, request: Request
    ):
        await get_current_user_context(request)
        return success_response(await agent_catalog.rename_category(category_id, payload.name))

    @app.delete(
        "/api/v1/admin/agent-categories/{category_id}",
        response_model=ApiEnvelope[DeletedAgentCategory],
    )
    async def delete_agent_category(category_id: str, request: Request):
        await get_current_user_context(request)
        await agent_catalog.require_category(category_id)
        # Agents first: an Agent left naming a deleted category could not be
        # saved again until its author removed the id.
        unbound = await get_agent_service().unbind_category(category_id)
        await agent_catalog.delete_category(category_id)
        return success_response({"category_id": category_id, "unbound_agents": unbound})
