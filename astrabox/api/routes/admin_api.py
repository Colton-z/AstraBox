"""``/api/v1/admin-api/*`` — the ops/automation admin surface.

When ``ASTRABOX_ADMIN_API_TOKEN`` is configured, every route requires the
matching bearer credential through :mod:`astrabox.common.utils.admin_api_auth`.
The routes are open when the token is unset.

The ``sessions/all``, ``errors`` and ``process/health`` handlers delegate to the
shared thin-handler helpers in :mod:`astrabox.api.routes.admin`. The
assistant-workspace wake/hibernate handlers have no console-admin twin.

The import edge is one-directional: this module imports admin.py's helpers;
admin.py never imports anything from here.

Registered directly on the passed ``app`` (no ``route_class``);
``register_admin_api_routes(app) -> None`` is idempotent by ``id(app)``.
"""

from __future__ import annotations

from typing import Any

from fastapi import Body
from fastapi import Request
from pydantic import BaseModel, ConfigDict

from astrabox.api.routes.response_envelope import ApiEnvelope
from astrabox.common.utils.admin_api_auth import (
    bootstrap_admin_api_registry,
    require_admin_api_bearer,
)
from astrabox.common.utils.api_response import success_response
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.service_registry import get_assistant_service
from astrabox.api.routes.admin import (
    AdminErrorsPage,
    AdminProcessHealth,
    _errors_payload,
    _process_health_payload,
    _global_sessions_payload,
)

_registered_on: int | None = None


class AdminApiWorkspaceResult(BaseModel):
    """What a wake or hibernate did to one assistant's workspace.

    Only ``assistant_id`` is this surface's own. The rest of the object is the
    workspace state
    (:meth:`~astrabox.core.service.orchestrator.assistant.assistant_service.AssistantService.wake_workspace`
    / ``hibernate_workspace``), which differs per branch — a wake that found a
    ready box reports different facts from one that started a bootstrap — so it
    is carried rather than flattened into one shape that would be wrong for
    most of them.
    """

    model_config = ConfigDict(extra="allow")

    assistant_id: str


def register_admin_api_routes(app) -> None:
    global _registered_on
    if _registered_on == id(app):
        return
    _registered_on = id(app)

    bootstrap_admin_api_registry()
    _assistant_service = get_assistant_service()

    @app.get(
        "/api/v1/admin-api/sessions/all",
        # The unscoped dump is a flat list of sanitized session rows, whose
        # fields belong to the sessions repository. The console's own listing
        # is paged and owner-scoped and is a different route.
        response_model=ApiEnvelope[list[dict[str, Any]]],
        response_model_exclude_unset=True,
    )
    async def admin_api_list_sessions(request: Request):
        await require_admin_api_bearer(request)
        sessions = await _global_sessions_payload(request)
        return success_response(sessions)

    @app.get(
        "/api/v1/admin-api/errors",
        response_model=ApiEnvelope[AdminErrorsPage],
        response_model_exclude_unset=True,
    )
    async def admin_api_list_errors(request: Request):
        await require_admin_api_bearer(request)
        errors = await _errors_payload(request.query_params.get("limit", "200"))
        return success_response(errors)

    @app.get(
        "/api/v1/admin-api/process/health",
        response_model=ApiEnvelope[AdminProcessHealth],
        response_model_exclude_unset=True,
    )
    async def admin_api_process_health(request: Request):
        await require_admin_api_bearer(request)
        return success_response(_process_health_payload())

    @app.post(
        "/api/v1/admin-api/assistants/{assistant_id}/workspace/wake",
        response_model=ApiEnvelope[AdminApiWorkspaceResult],
        response_model_exclude_unset=True,
    )
    async def admin_api_wake_assistant_workspace(
        assistant_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ):
        await require_admin_api_bearer(request)
        user_id = str(payload.get("user_id") or "").strip()
        if not user_id:
            raise APIError(
                code="ADMIN_ASSISTANT_USER_REQUIRED",
                message="user_id is required",
                status_code=400,
            )
        user = UserContext(user_id=user_id)
        result = await _assistant_service.wake_workspace(user, assistant_id)
        return success_response(result)

    @app.post(
        "/api/v1/admin-api/assistants/{assistant_id}/workspace/hibernate",
        response_model=ApiEnvelope[AdminApiWorkspaceResult],
        response_model_exclude_unset=True,
    )
    async def admin_api_hibernate_assistant_workspace(
        assistant_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ):
        await require_admin_api_bearer(request)
        user_id = str(payload.get("user_id") or "").strip()
        if not user_id:
            raise APIError(
                code="ADMIN_ASSISTANT_USER_REQUIRED",
                message="user_id is required",
                status_code=400,
            )
        user = UserContext(user_id=user_id)
        result = await _assistant_service.hibernate_workspace(user, assistant_id)
        return success_response(result)
