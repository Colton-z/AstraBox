from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict

from astrabox.api.routes.response_envelope import ApiEnvelope
from astrabox.api.routes.sessions import (
    DeliveryFailure, SessionHistoryBlockPage, SessionHistoryBlockDetails,
)
from astrabox.api.routes.session_files import SessionFileListing
from astrabox.common.utils.api_response import error_response, success_response
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.http_headers import build_attachment_headers
from astrabox.common.utils.user_context import get_current_user_context
from astrabox.core.service.orchestrator.service_registry import get_platform_service

_registered_on: int | None = None


# The payload half of ``ApiEnvelope[...]``; the obligations that come with
# declaring one are in :mod:`astrabox.api.routes.response_envelope`.


class ShareLink(BaseModel):
    """The share configuration its owner may read back.

    The token is the capability, so it appears here and nowhere the viewer side
    answers. ``expires_at`` is null for a link with no expiry — a distinct state
    from an expired one, which the viewer routes refuse with 404.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    token: str
    expires_at: str | None
    allow_download: bool
    created_at: str | None


class ShareRevoked(BaseModel):
    """Revocation answers the one fact it changed.

    The token is deliberately absent: revoking disables the link without
    discarding the token, so re-sharing keeps links already handed out working,
    and there is nothing here for a client to treat as a live capability.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool


class SharedSession(BaseModel):
    """A session as a link recipient sees it.

    A capability URL grants transcript reading, not access to the owner's
    Session/runtime document. Only fields used by the read-only transcript UI
    cross this boundary.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    delivery_state: str | None = None
    delivery_failure: DeliveryFailure | None = None
    pending_interaction: dict[str, Any] | None = None
    share_allow_download: bool


class SharedMessagePage(BaseModel):
    """One page of a shared conversation, newest page first.

    ``has_more`` drives pagination. The active overlay and pending interaction
    preserve a live read-only transcript without exposing internal cursors.
    """

    model_config = ConfigDict(extra="forbid")

    messages: list[dict[str, Any]]
    has_more: bool
    active_turn_overlay: dict[str, Any] | None = None
    pending_interaction: dict[str, Any] | None = None


def _err(exc: APIError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=error_response(exc))


def register_share_routes(app: Any) -> None:
    global _registered_on
    if _registered_on == id(app):
        return
    _registered_on = id(app)

    _service = get_platform_service()

    # ── Owner side: create / revoke / read share config ──────────────────────
    # These live under /api/v1/sessions/**, which the identity middleware does
    # not exempt, and enforce ownership in the service via
    # must_get_owned_session.
    @app.post(
        "/api/v1/sessions/{session_id}/share",
        response_model=ApiEnvelope[ShareLink],
        response_model_exclude_unset=True,
    )
    async def create_share(session_id: str, request: Request):
        user = await get_current_user_context(request)
        body: dict[str, Any] = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        result = await _service.create_session_share(
            user,
            session_id,
            expires_in_seconds=body.get("expires_in_seconds"),
            allow_download=bool(body.get("allow_download")),
        )
        return success_response(result)

    @app.delete(
        "/api/v1/sessions/{session_id}/share",
        response_model=ApiEnvelope[ShareRevoked],
        response_model_exclude_unset=True,
    )
    async def revoke_share(session_id: str, request: Request):
        user = await get_current_user_context(request)
        return success_response(await _service.revoke_session_share(user, session_id))

    @app.get(
        "/api/v1/sessions/{session_id}/share",
        response_model=ApiEnvelope[ShareLink],
        response_model_exclude_unset=True,
    )
    async def get_share(session_id: str, request: Request):
        user = await get_current_user_context(request)
        return success_response(await _service.get_session_share(user, session_id))

    # ── Viewer side: read-only access by token ───────────────────────────────
    # Access is single-factor: the signed share token is the whole capability,
    # and no login is involved. The identity middleware exempts /api/v1/share/
    # from front-door auth (identity_middleware._DEFAULT_EXEMPT_PREFIXES)
    # precisely so a link recipient without an account can view. The service
    # verifies the token (_resolve_shared_session: enabled + not-expired) and
    # bypasses ownership because the token itself is the grant.
    # The viewer routes answer the envelope on success and a JSONResponse from
    # ``_err`` on a refused token. A Response object bypasses the model, so each
    # declaration below describes the 200 body only — which is the one a client
    # generates against.
    @app.get(
        "/api/v1/share/{token}",
        response_model=ApiEnvelope[SharedSession],
        response_model_exclude_unset=True,
    )
    async def shared_session(token: str, request: Request):
        try:
            return success_response(await _service.get_shared_session(token))
        except APIError as exc:
            return _err(exc)

    @app.get(
        "/api/v1/share/{token}/messages",
        response_model=ApiEnvelope[SharedMessagePage],
        response_model_exclude_unset=True,
    )
    async def shared_messages(token: str, request: Request):
        before = str(request.query_params.get("before", "") or "").strip() or None
        try:
            limit = int(request.query_params.get("limit", "20") or "20")
        except ValueError:
            limit = 20
        try:
            return success_response(
                await _service.get_shared_messages(token, before=before, limit=min(limit, 50))
            )
        except APIError as exc:
            return _err(exc)

    @app.get(
        "/api/v1/share/{token}/history-blocks",
        response_model=ApiEnvelope[SessionHistoryBlockPage],
        response_model_exclude_unset=True,
    )
    async def shared_history_blocks(
        token: str, before: str | None = None, limit: int = 50
    ):
        try:
            return success_response(await _service.get_shared_history_blocks(
                token, before=before, limit=max(1, min(limit, 50))
            ))
        except APIError as exc:
            return _err(exc)

    @app.get(
        "/api/v1/share/{token}/history-blocks/{block_id}",
        response_model=ApiEnvelope[SessionHistoryBlockDetails],
        response_model_exclude_unset=True,
    )
    async def shared_history_block_details(token: str, block_id: str, cursor: str):
        try:
            return success_response(await _service.get_shared_history_block_details(
                token, block_id, cursor=cursor
            ))
        except APIError as exc:
            return _err(exc)

    # The same listing the owner's file panel reads: one model, because the
    # share path calls the same ``SessionFileService`` listing.
    @app.get(
        "/api/v1/share/{token}/files/list",
        response_model=ApiEnvelope[SessionFileListing],
        response_model_exclude_unset=True,
    )
    async def shared_files_list(token: str, request: Request):
        path = str(request.query_params.get("path", "") or "").strip() or None
        try:
            return success_response(await _service.list_shared_files(token, path=path))
        except APIError as exc:
            return _err(exc)

    # No response model: the answer is the file's bytes under an attachment
    # header, not the JSON envelope.
    @app.get("/api/v1/share/{token}/files/download")
    async def shared_file_download(token: str, request: Request, path: str):
        try:
            content, filename = await _service.download_shared_file(token, path=path)
        except APIError as exc:
            return _err(exc)
        headers = build_attachment_headers(filename)
        headers["Content-Length"] = str(len(content))
        return Response(content=content, media_type="application/octet-stream", headers=headers)
