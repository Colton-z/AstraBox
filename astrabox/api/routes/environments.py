"""The serving Environment's metadata, policy and explicitly granted access."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from astrabox.api.routes._shared import _resolve_user
from astrabox.api.routes.response_envelope import ApiEnvelope
from astrabox.common.utils.api_response import success_response
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import get_asserted_user_context
from astrabox.core.service.environment_access import can_enter_environment
from astrabox.config.settings import get_settings, validate_environment_name
from astrabox.persistence.repository.environment_repository import EnvironmentRepository
from astrabox.seams.scheduler import configured_scheduler, configured_scheduler_name

router = APIRouter()


class EnvironmentView(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    display_name: str
    description: str | None
    version: int


class EnvironmentRuntimePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_prewarm_enabled: bool
    background_execution_enabled: bool
    custody_scope: Literal["environment"]
    scheduler_provider: str | None
    scheduler_worker_enabled: bool


class CurrentEnvironment(EnvironmentView):
    runtime_policy: EnvironmentRuntimePolicy


class AdminEnvironment(EnvironmentView):
    member_user_ids: list[str]


class EnvironmentWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=256)
    description: str | None = None
    member_user_ids: list[str] = Field(default_factory=list)
    version: int = Field(ge=1)

    @field_validator("display_name")
    @classmethod
    def _display_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("display_name cannot be blank")
        return value

    @field_validator("member_user_ids")
    @classmethod
    def _members(cls, values: list[str]) -> list[str]:
        if any(not value or value != value.strip() for value in values):
            raise ValueError("member user ids must be nonempty and unpadded")
        return sorted(set(values))


async def _get(name: str) -> dict:
    row = await EnvironmentRepository().get(name)
    if row is None:
        raise APIError(
            code="ENVIRONMENT_NOT_FOUND", status_code=404, message="Environment not found",
        )
    return row


@router.get("/api/v1/environments", response_model=ApiEnvelope[list[EnvironmentView]])
async def list_accessible_environments(request: Request):
    if not await can_enter_environment(get_asserted_user_context(), operation="list Environments"):
        return success_response([])
    row = await _get(get_settings().environment_name)
    rows = [row]
    return success_response(rows)


@router.get("/api/v1/environments/current", response_model=ApiEnvelope[CurrentEnvironment])
async def get_current_environment(request: Request):
    await _resolve_user(request)
    settings = get_settings()
    row = await _get(settings.environment_name)
    return success_response({**row, "runtime_policy": {
        "agent_prewarm_enabled": settings.agent_prewarm_enabled,
        "background_execution_enabled": settings.background_execution_enabled,
        "custody_scope": "environment",
        "scheduler_provider": configured_scheduler_name(),
        "scheduler_worker_enabled": (
            settings.background_execution_enabled and configured_scheduler() is not None
        ),
    }})


@router.get("/api/v1/admin/environments", response_model=ApiEnvelope[list[AdminEnvironment]])
async def list_admin_environments(request: Request):
    user = await _resolve_user(request)
    repo = EnvironmentRepository()
    rows = await repo.list_accessible(user.user_id, platform_admin=True)
    return success_response([
        {**row, "member_user_ids": await repo.member_user_ids(row["name"])} for row in rows
    ])


@router.get("/api/v1/admin/environments/{name}", response_model=ApiEnvelope[AdminEnvironment])
async def get_admin_environment(request: Request, name: str):
    await _resolve_user(request)
    return success_response({
        **await _get(name), "member_user_ids": await EnvironmentRepository().member_user_ids(name),
    })


@router.put("/api/v1/admin/environments/{name}", response_model=ApiEnvelope[AdminEnvironment])
async def save_admin_environment(request: Request, name: str, payload: EnvironmentWrite):
    user = await _resolve_user(request)
    try:
        validate_environment_name(name)
    except ValueError as exc:
        raise APIError(code="INVALID_REQUEST", status_code=400, message=str(exc)) from exc
    row = await EnvironmentRepository().save(
        name, display_name=payload.display_name, description=payload.description,
        member_user_ids=payload.member_user_ids,
        expected_version=payload.version, actor_id=user.user_id,
    )
    return success_response(row)
