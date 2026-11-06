"""Owner-scoped read and explicit deployment operations, independent of a turn."""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from astrabox.api.routes._shared import _resolve_user
from astrabox.api.routes.response_envelope import ApiEnvelope
from astrabox.common.utils.api_response import success_response
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import API_READ_SCOPE, API_WRITE_SCOPE
from astrabox.core.model import AgentState, SessionState
from astrabox.core.service.orchestrator.runtime.environment_ownership import require_environment_ownership
from astrabox.core.service.orchestrator.runtime.preparation import preparation_subject
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.seams.runtime_preparation import (
    SessionUserActionContext,
)

from astrabox.core.service.orchestrator.user_actions import user_action_state

router = APIRouter()


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
    applicable: bool = Field(description="Whether execution prerequisites apply; false for read-only history")
    ready: bool
    user_action: UserActionView | None = None
    actions: list[UserActionCommandView] = Field(default_factory=list)


class PerformUserAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_id: str = Field(min_length=1, max_length=128)


async def _context(request: Request, session_id: str, *, write: bool) -> SessionUserActionContext | None:
    user = await _resolve_user(request)
    scope = API_WRITE_SCOPE if write else API_READ_SCOPE
    if user.api_scopes is not None and scope not in user.api_scopes:
        raise APIError(code="FORBIDDEN", message="Required API scope is missing", status_code=403)
    session = await SessionRepository().get_owned_session(session_id, user.user_id)
    if session is None:
        raise APIError(code="SESSION_NOT_FOUND", message="Session not found", status_code=404)
    if write:
        require_environment_ownership(session)
    if (
        session.get("state") in {SessionState.TERMINATED.value, SessionState.DELETED.value}
        or session.get("agent_state") in {AgentState.DELETING.value, AgentState.DELETED.value}
    ):
        return None
    return SessionUserActionContext(
        session_id=session_id, principal_id=user.user_id, subject=preparation_subject(session),
    )


@router.get("/api/v1/sessions/{session_id}/user-action", response_model=ApiEnvelope[SessionUserActionView])
async def read_session_user_action(session_id: str, request: Request):
    context = await _context(request, session_id, write=False)
    if context is None:
        return success_response({"applicable": False, "ready": False, "user_action": None, "actions": []})
    return success_response(await user_action_state(context))


@router.post("/api/v1/sessions/{session_id}/user-action", response_model=ApiEnvelope[SessionUserActionView])
async def perform_session_user_action(session_id: str, request: Request, body: PerformUserAction):
    context = await _context(request, session_id, write=True)
    if context is None:
        raise APIError(code="SESSION_READ_ONLY", status_code=409,
                       message="This conversation is read-only; no preparation operation is available")
    return success_response(await user_action_state(context, action_id=body.action_id))
