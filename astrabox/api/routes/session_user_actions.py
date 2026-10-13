"""Owner-scoped read and explicit deployment operations, independent of a turn."""
from __future__ import annotations

from typing import Awaitable

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from astrabox.api.routes._shared import _resolve_user
from astrabox.api.routes.response_envelope import ApiEnvelope
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.api_response import success_response
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import API_READ_SCOPE, API_WRITE_SCOPE
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.seams.runtime_preparation import (
    SessionUserActionContext, SessionUserActionState, runtime_preparation_provider,
)

router = APIRouter()
logger = get_logger(__name__)


class UserActionView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    message: str
    url: str | None = None


class UserActionCommandView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    label: str


class SessionUserActionView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ready: bool
    user_action: UserActionView | None = None
    actions: list[UserActionCommandView] = Field(default_factory=list)


class PerformUserAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_id: str = Field(min_length=1, max_length=128)


async def _context(request: Request, session_id: str, *, write: bool) -> SessionUserActionContext:
    user = await _resolve_user(request)
    scope = API_WRITE_SCOPE if write else API_READ_SCOPE
    if user.api_scopes is not None and scope not in user.api_scopes:
        raise APIError(code="FORBIDDEN", message="Required API scope is missing", status_code=403)
    session = await SessionRepository().get_owned_session(session_id, user.user_id)
    if session is None:
        raise APIError(code="SESSION_NOT_FOUND", message="Session not found", status_code=404)
    return SessionUserActionContext(
        session_id=session_id, principal_id=user.user_id, agent_id=session.get("agent_id"),
    )


async def _provider_state(result: Awaitable[SessionUserActionState]) -> dict:
    try:
        return (await result).public()
    except Exception:
        logger.exception("session user-action provider failed")
        raise APIError(code="SESSION_USER_ACTION_FAILED", status_code=502,
                       message="Unable to determine the session requirements; refresh to try again") from None


@router.get("/api/v1/sessions/{session_id}/user-action", response_model=ApiEnvelope[SessionUserActionView])
async def read_session_user_action(session_id: str, request: Request):
    context = await _context(request, session_id, write=False)
    provider = runtime_preparation_provider()
    state = await _provider_state(provider.read_user_actions(context=context)) if provider else SessionUserActionState(ready=True).public()
    return success_response(state)


@router.post("/api/v1/sessions/{session_id}/user-action", response_model=ApiEnvelope[SessionUserActionView])
async def perform_session_user_action(session_id: str, request: Request, body: PerformUserAction):
    context = await _context(request, session_id, write=True)
    provider = runtime_preparation_provider()
    if provider is None:
        raise APIError(code="INVALID_REQUEST", message="No user operation is configured", status_code=400)
    current = await _provider_state(provider.read_user_actions(context=context))
    if body.action_id not in {action["id"] for action in current["actions"]}:
        raise APIError(code="INVALID_REQUEST", message="This operation is no longer available; refresh its status", status_code=409)
    state = await _provider_state(provider.perform_user_action(context=context, action_id=body.action_id))
    return success_response(state)
