"""The serving process's execution scope and worker policy."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from astrabox.api.routes._shared import _resolve_user
from astrabox.api.routes.response_envelope import ApiEnvelope
from astrabox.common.utils.api_response import success_response
from astrabox.config.settings import get_settings
from astrabox.seams.scheduler import configured_scheduler, configured_scheduler_name

router = APIRouter()


class ExecutionPolicy(BaseModel):
    """Policy for new automatic work, distinct from custody of accepted runtime work."""

    model_config = ConfigDict(extra="forbid")

    execution_target: str
    agent_prewarm_enabled: bool
    background_execution_enabled: bool
    runtime_recovery_scope: Literal["owned_target"]
    scheduler_provider: str | None
    scheduler_control_configured: bool
    scheduler_worker_enabled: bool


@router.get("/api/v1/execution-policy", response_model=ApiEnvelope[ExecutionPolicy])
async def get_execution_policy(request: Request):
    """Read this process's policy without starting workers or touching runtime resources."""
    await _resolve_user(request)
    settings = get_settings()
    scheduler_configured = configured_scheduler() is not None
    return success_response(ExecutionPolicy(
        execution_target=settings.execution_target,
        agent_prewarm_enabled=settings.agent_prewarm_enabled,
        background_execution_enabled=settings.background_execution_enabled,
        runtime_recovery_scope="owned_target",
        scheduler_provider=configured_scheduler_name(),
        scheduler_control_configured=scheduler_configured,
        scheduler_worker_enabled=settings.background_execution_enabled and scheduler_configured,
    ).model_dump())
