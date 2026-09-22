"""Receivers for sandbox-death evidence supplied by an external emitter.

``/api/v1/sandbox-callback/...`` is the control plane's channel: a provider that
can call back on termination hits it, fenced by the session's generation and
callback token. OpenSandbox has no such field on its create contract, so on that
backend nothing calls it.

``.../sandbox/{sandbox_id}/terminating`` accepts an authenticated box-scoped
notice from a provider or deployment termination trigger. It is scoped to the
box because one box can have Session, Agent, and Assistant-workspace owners; the
platform, not the emitter, resolves which durable owners the notification
affects. These routes receive notices but do not arrange their delivery. The
OpenSandbox standard create has no termination callback or ``preStop`` hook and
invokes neither route automatically; its built-in recovery
path is the expiration watcher's lifecycle probe. See
``docs/maintainers/sandbox-death-notification.md``.
"""

from __future__ import annotations

from typing import Any

from fastapi import Body
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from astrabox.api.routes.response_envelope import ApiEnvelope
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.api_response import error_response, success_response
from astrabox.core.service.orchestrator.service_registry import (
    get_platform_service,
)
from astrabox.core.service.orchestrator.transcript_capability import (
    verify_sandbox_box_capability_token,
)
from astrabox.api.routes.transcript import CAPABILITY_PATH_PREFIX

logger = get_logger(__name__)

_registered_on: int | None = None

# The payload half of ``ApiEnvelope[...]``; the obligations that come with
# declaring one are in :mod:`astrabox.api.routes.response_envelope`. The caller
# is a control plane or a dying box, so both models answer the same question:
# what the platform did with the news.


class SandboxCallbackResult(BaseModel):
    """The disposition of one control-plane callback.

    ``handled`` false carries ``ignored`` naming why — a callback for an unknown
    session, or one fenced out by a newer sandbox generation. ``handled`` true
    carries the subject it applied to and the status it applied. The fields of
    the arm that did not run are absent rather than null, so a caller reads the
    outcome from which keys are present.
    """

    model_config = ConfigDict(extra="allow")

    handled: bool
    ignored: str | None = None
    subject_type: str | None = None
    subject_id: str | None = None
    status: str | None = None


class SandboxTerminatingResult(BaseModel):
    """Which Session projections converged, and which were expected to pause.

    Agent and Assistant-workspace owners are converged by the same operation but
    are not exposed in this in-box compatibility response. A notice is always
    ``handled``: a box with no owner is normal for unclaimed prepared capacity.
    ``ignored`` maps a Session id to the planned pause that preserved its box.
    """

    model_config = ConfigDict(extra="allow")

    handled: bool
    converged: list[str]
    ignored: dict[str, str]


def register_sandbox_callback_routes(app: Any) -> None:
    global _registered_on
    if _registered_on == id(app):
        return
    _registered_on = id(app)

    _service = get_platform_service()._sandbox_lifecycle_service

    @app.post(
        "/api/v1/sandbox-callback/{subject_type}/{subject_id}/{generation}/{token}",
        response_model=ApiEnvelope[SandboxCallbackResult],
        response_model_exclude_unset=True,
    )
    async def sandbox_lifecycle_callback(
        subject_type: str,
        subject_id: str,
        generation: str,
        token: str,
        payload: dict[str, Any] = Body(...),
    ):
        result = await _service.handle_callback(
            subject_type=subject_type,
            subject_id=subject_id,
            generation=generation,
            token=token,
            payload=payload,
        )
        return success_response(result)

    @app.post(
        CAPABILITY_PATH_PREFIX + "/{cap_token}/api/v1/sandbox/{sandbox_id}/terminating",
        response_model=ApiEnvelope[SandboxTerminatingResult],
        response_model_exclude_unset=True,
    )
    async def sandbox_terminating_notice(
        cap_token: str,
        sandbox_id: str,
        payload: dict[str, Any] = Body(...),
    ):
        """The box says it is going away. The platform decides who that affects.

        Scoped to the box, not to a conversation, because the box is what knows
        and a box can have Session, Agent, and Assistant-workspace owners. It
        reports one fact — "I am being torn down" — and the platform resolves
        every durable owner by ``sandbox_id``. The box asserts none of those
        relationships; they are platform state.

        A Pod also exits during an intentional lifecycle transition. The owner
        records that intent before touching the box, so the convergence
        operation preserves:

        * a parked Session and its Agent-owned shared-box pointer;
        * an Assistant workspace whose files committed and whose old box is
          being released, plus every Session attached to it;
        * a Session already being ended or deleted.

        A notice from a box with no durable owner is not an error — it is normal
        for a box whose owners moved on and for unclaimed prepared capacity.
        """
        if not verify_sandbox_box_capability_token(sandbox_id, cap_token):
            return JSONResponse(
                status_code=403,
                content=error_response(
                    "FORBIDDEN", "invalid or missing sandbox capability token"
                ),
            )
        result = await _service.converge_dead_sandbox_owners(
            sandbox_id,
            last_error="sandbox terminated",
            reason="sandbox_terminating_notice",
            preserve_planned_teardowns=True,
        )
        logger.info(
            "box terminating notice sandbox=%s sessions=%s agents=%s "
            "assistant_workspaces=%s ignored_sessions=%s "
            "ignored_agents=%s ignored_assistant_workspaces=%s",
            sandbox_id,
            result.converged_sessions,
            result.converged_agents,
            result.converged_assistant_workspaces,
            result.ignored_sessions or {},
            result.ignored_agents or {},
            result.ignored_assistant_workspaces or {},
        )
        return success_response(
            {
                "handled": True,
                "converged": list(result.converged_sessions),
                "ignored": result.ignored_sessions or {},
            }
        )
