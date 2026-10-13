"""Authenticated entry point for the conversation sandbox's browser."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from pydantic import BaseModel, ConfigDict

from astrabox.api.routes.response_envelope import ApiEnvelope
from astrabox.common.utils.api_response import success_response
from astrabox.common.utils.user_context import get_current_user_context
from astrabox.core.service.orchestrator.service_registry import get_platform_service

_registered_on: int | None = None


class SessionBrowserAccess(BaseModel):
    model_config = ConfigDict(extra="allow")

    url: str
    expires_at: str


def register_session_browser_routes(app: Any) -> None:
    global _registered_on
    if _registered_on == id(app):
        return
    _registered_on = id(app)
    service = get_platform_service()

    @app.post(
        "/api/v1/sessions/{session_id}/browser",
        response_model=ApiEnvelope[SessionBrowserAccess],
        response_model_exclude_unset=True,
    )
    async def open_session_browser(session_id: str, request: Request):
        user = await get_current_user_context(request)
        return success_response(await service.open_session_browser(user, session_id))
