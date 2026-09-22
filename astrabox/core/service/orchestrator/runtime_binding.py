from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from astrabox.common.logger.logger_factory import get_logger
from astrabox.core.service.orchestrator.engine.capabilities import (
    engine_allowed_for_session_kind,
    require_session_kind,
)
from astrabox.core.service.orchestrator.engine_kind_utils import (
    resolve_session_engine_kind,
)

logger = get_logger(__name__)

BindingStatus = Literal[
    "READY",
    "PROVISIONING",
    "HIBERNATING",
    "RECOVERY_REQUIRED",
    "DELETED",
    "UNAVAILABLE",
]
RuntimeSubjectKind = Literal["session", "assistant_workspace"]


@dataclass(frozen=True)
class RuntimeBindingResolution:
    session_id: str
    session_kind: str
    authority_kind: str
    authority_id: str
    authority_state: str | None
    engine_kind: str
    sandbox_id: str | None
    expires_at: str | None
    owner_runtime_identity: dict[str, Any] | None
    status: BindingStatus
    reason_code: str | None
    reason_message: str | None
    routing_updates: dict[str, Any]

    @property
    def can_dispatch(self) -> bool:
        return self.status == "READY" and bool(self.sandbox_id)

    def as_projection(self) -> dict[str, Any]:
        return {
            "authority_kind": self.authority_kind,
            "authority_id": self.authority_id,
            "state": self.status,
            "can_dispatch": self.can_dispatch,
            "sandbox_id": self.sandbox_id,
            "expires_at": self.expires_at,
            "reason_code": self.reason_code,
            "reason_message": self.reason_message,
        }


def _clean(value: Any) -> str | None:
    return str(value or "").strip() or None


def is_assistant_workspace_bootstrap(session: dict[str, Any] | None) -> bool:
    return bool((session or {}).get("hidden")) and (
        str((session or {}).get("owner_type") or "").strip() == "assistant_workspace"
    )


def runtime_subject_kind(session: dict[str, Any] | None) -> RuntimeSubjectKind:
    """Return the runtime-startup authority encoded by one Session."""

    workspace_ref = (session or {}).get("workspace_ref")
    ref_kind = (
        str(workspace_ref.get("kind") or "").strip()
        if isinstance(workspace_ref, dict)
        else ""
    )
    session_kind = str((session or {}).get("session_kind") or "").strip()
    if session_kind == "agent_chat" and ref_kind == "agent":
        return "session"
    if session_kind == "assistant_chat" and ref_kind == "assistant":
        return "assistant_workspace"
    raise ValueError(
        "Session has no runtime subject: "
        f"session_kind={session_kind!r} workspace_kind={ref_kind!r}"
    )


def is_assistant_user_conversation(session: dict[str, Any] | None) -> bool:
    try:
        subject_kind = runtime_subject_kind(session)
    except ValueError:
        return False
    return subject_kind == "assistant_workspace" and not is_assistant_workspace_bootstrap(
        session
    )


def _build_non_shared_resolution(session: dict[str, Any]) -> RuntimeBindingResolution:
    session_id = _clean(session.get("session_id")) or ""
    raw_session_kind = _clean(session.get("session_kind")) or ""
    authority_state = _clean(session.get("state"))
    sandbox_id = _clean(session.get("sandbox_id"))
    owner_runtime_identity = (
        dict(session.get("runtime_identity"))
        if isinstance(session.get("runtime_identity"), dict)
        else None
    )
    try:
        session_kind = require_session_kind(raw_session_kind)
        engine_kind = resolve_session_engine_kind(session)
        if not engine_allowed_for_session_kind(engine_kind, session_kind):
            raise ValueError(
                f"engine_kind={engine_kind!r} is not installed with support for "
                f"session_kind={session_kind!r}"
            )
    except (KeyError, TypeError, ValueError) as exc:
        return RuntimeBindingResolution(
            session_id=session_id,
            session_kind=raw_session_kind,
            authority_kind="session",
            authority_id=session_id,
            authority_state=authority_state,
            engine_kind=_clean(session.get("engine_kind")) or "",
            sandbox_id=None,
            expires_at=None,
            owner_runtime_identity=owner_runtime_identity,
            status="UNAVAILABLE",
            reason_code="SESSION_RUNTIME_IDENTITY_INVALID",
            reason_message=str(exc),
            routing_updates={},
        )
    status: BindingStatus = "UNAVAILABLE"
    reason_code: str | None = None
    reason_message: str | None = None

    if authority_state != "READY":
        reason_code = "SESSION_NOT_READY"
        reason_message = f"session is not ready state={authority_state!r}"
    elif not sandbox_id:
        reason_code = "SESSION_SANDBOX_MISSING"
        reason_message = "session sandbox_id is missing"
    else:
        status = "READY"

    return RuntimeBindingResolution(
        session_id=session_id,
        session_kind=session_kind,
        authority_kind="session",
        authority_id=session_id,
        authority_state=authority_state,
        engine_kind=engine_kind,
        sandbox_id=sandbox_id if status == "READY" else None,
        expires_at=_clean(session.get("expires_at")) if status == "READY" else None,
        owner_runtime_identity=owner_runtime_identity,
        status=status,
        reason_code=reason_code,
        reason_message=reason_message,
        routing_updates={},
    )


async def _persist_updates(
    sessions_repo: Any,
    session_id: str,
    updates: dict[str, Any],
) -> None:
    if not updates:
        return
    try:
        await sessions_repo.update_session(
            session_id,
            updates,
            touch_updated_at=False,
        )
    except TypeError:
        await sessions_repo.update_session(session_id, updates)


async def resolve_assistant_workspace_binding(
    *,
    session: dict[str, Any],
    assistant_workspace_service: Any,
) -> RuntimeBindingResolution:
    normalized = dict(session)
    session_id = _clean(normalized.get("session_id")) or ""
    workspace_ref = normalized.get("workspace_ref")
    workspace_ref = workspace_ref if isinstance(workspace_ref, dict) else {}
    assistant_id = _clean(workspace_ref.get("assistant_id"))
    user_id = _clean(workspace_ref.get("user_id")) or _clean(normalized.get("user_id"))
    try:
        engine_kind = resolve_session_engine_kind(normalized)
        if not engine_allowed_for_session_kind(engine_kind, "assistant_chat"):
            raise ValueError(
                f"engine_kind={engine_kind!r} is not installed with support for "
                "session_kind='assistant_chat'"
            )
    except (KeyError, TypeError, ValueError) as exc:
        return RuntimeBindingResolution(
            session_id=session_id,
            session_kind="assistant_chat",
            authority_kind="assistant_workspace",
            authority_id=f"assistant_workspace:{assistant_id or ''}",
            authority_state=None,
            engine_kind="",
            sandbox_id=None,
            expires_at=None,
            owner_runtime_identity=None,
            status="UNAVAILABLE",
            reason_code="SESSION_RUNTIME_IDENTITY_INVALID",
            reason_message=str(exc),
            routing_updates={},
        )

    if not assistant_id:
        return RuntimeBindingResolution(
            session_id=session_id,
            session_kind="assistant_chat",
            authority_kind="assistant_workspace",
            authority_id="",
            authority_state=None,
            engine_kind=engine_kind,
            sandbox_id=None,
            expires_at=None,
            owner_runtime_identity=None,
            status="UNAVAILABLE",
            reason_code="ASSISTANT_WORKSPACE_REF_INVALID",
            reason_message="assistant workspace_ref missing assistant_id",
            routing_updates={},
        )

    workspace = await assistant_workspace_service.get_workspace(
        user_id=user_id or "",
        assistant_id=assistant_id,
    )
    if not isinstance(workspace, dict):
        return RuntimeBindingResolution(
            session_id=session_id,
            session_kind="assistant_chat",
            authority_kind="assistant_workspace",
            authority_id=f"assistant_workspace:{assistant_id}",
            authority_state=None,
            engine_kind=engine_kind,
            sandbox_id=None,
            expires_at=None,
            owner_runtime_identity=None,
            status="UNAVAILABLE",
            reason_code="ASSISTANT_WORKSPACE_NOT_FOUND",
            reason_message=f"assistant workspace not found assistant={assistant_id}",
            routing_updates=_assistant_binding_updates(
                normalized,
                authoritative_sandbox_id=None,
                authoritative_expires_at=None,
                authoritative_runtime_identity=None,
                engine_kind=engine_kind,
                # There is no workspace row at all, so nothing else can be
                # naming a box: this row's copy is kept.
                workspace_names_a_sandbox=False,
            ),
        )

    workspace_state = _clean(workspace.get("state")) or "UNAVAILABLE"
    authoritative_sandbox_id = _clean(workspace.get("current_sandbox_id"))
    authoritative_expires_at = _clean(workspace.get("current_sandbox_expires_at"))
    owner_runtime_identity = (
        dict(workspace.get("runtime_identity"))
        if isinstance(workspace.get("runtime_identity"), dict)
        else None
    )
    workspace_engine_kind = _clean(workspace.get("engine_kind"))
    if workspace_engine_kind and workspace_engine_kind != engine_kind:
        return RuntimeBindingResolution(
            session_id=session_id,
            session_kind="assistant_chat",
            authority_kind="assistant_workspace",
            authority_id=f"assistant_workspace:{assistant_id}",
            authority_state=workspace_state,
            engine_kind=engine_kind,
            sandbox_id=None,
            expires_at=None,
            owner_runtime_identity=owner_runtime_identity,
            status="UNAVAILABLE",
            reason_code="ASSISTANT_WORKSPACE_ENGINE_MISMATCH",
            reason_message=(
                "assistant session and workspace engine_kind disagree: "
                f"session={engine_kind!r} workspace={workspace_engine_kind!r}"
            ),
            routing_updates={},
        )
    authoritative_engine_kind = workspace_engine_kind or engine_kind
    if not engine_allowed_for_session_kind(
        authoritative_engine_kind, "assistant_chat"
    ):
        return RuntimeBindingResolution(
            session_id=session_id,
            session_kind="assistant_chat",
            authority_kind="assistant_workspace",
            authority_id=f"assistant_workspace:{assistant_id}",
            authority_state=workspace_state,
            engine_kind=authoritative_engine_kind,
            sandbox_id=None,
            expires_at=None,
            owner_runtime_identity=owner_runtime_identity,
            status="UNAVAILABLE",
            reason_code="SESSION_RUNTIME_IDENTITY_INVALID",
            reason_message=(
                f"engine_kind={authoritative_engine_kind!r} is not installed with "
                "support for session_kind='assistant_chat'"
            ),
            routing_updates={},
        )
    status: BindingStatus
    reason_code: str | None = None
    reason_message: str | None = None
    if workspace_state == "READY" and authoritative_sandbox_id:
        status = "READY"
    elif workspace_state == "READY":
        status = "UNAVAILABLE"
        reason_code = "ASSISTANT_WORKSPACE_SANDBOX_MISSING"
        reason_message = "assistant workspace READY has no current sandbox"
    elif workspace_state == "MATERIALIZING":
        status = "PROVISIONING"
        reason_code = "ASSISTANT_WORKSPACE_PROVISIONING"
        reason_message = "assistant workspace is provisioning"
    elif workspace_state == "HIBERNATING":
        status = "HIBERNATING"
        reason_code = "ASSISTANT_WORKSPACE_HIBERNATING"
        reason_message = "assistant workspace is hibernating"
    elif workspace_state == "RECOVERY_REQUIRED":
        status = "RECOVERY_REQUIRED"
        reason_code = "ASSISTANT_WORKSPACE_RECOVERY_REQUIRED"
        reason_message = _clean(workspace.get("last_error")) or "assistant workspace requires recovery"
    elif workspace_state == "DELETED":
        status = "DELETED"
        reason_code = "ASSISTANT_WORKSPACE_DELETED"
        reason_message = "assistant workspace is deleted"
    else:
        status = "UNAVAILABLE"
        reason_code = "ASSISTANT_WORKSPACE_NOT_READY"
        reason_message = (
            _clean(workspace.get("last_error"))
            or f"assistant workspace is not ready state={workspace_state!r}"
        )

    return RuntimeBindingResolution(
        session_id=session_id,
        session_kind="assistant_chat",
        authority_kind="assistant_workspace",
        authority_id=f"assistant_workspace:{assistant_id}",
        authority_state=workspace_state,
        engine_kind=authoritative_engine_kind,
        sandbox_id=authoritative_sandbox_id if status == "READY" else None,
        expires_at=authoritative_expires_at if status == "READY" else None,
        owner_runtime_identity=owner_runtime_identity,
        status=status,
        reason_code=reason_code,
        reason_message=reason_message,
        routing_updates=_assistant_binding_updates(
            normalized,
            authoritative_sandbox_id=authoritative_sandbox_id if status == "READY" else None,
            authoritative_expires_at=authoritative_expires_at if authoritative_sandbox_id else None,
            authoritative_runtime_identity=owner_runtime_identity,
            engine_kind=authoritative_engine_kind,
            workspace_names_a_sandbox=authoritative_sandbox_id is not None,
        ),
    )


def _assistant_binding_updates(
    session: dict[str, Any],
    *,
    authoritative_sandbox_id: str | None,
    authoritative_expires_at: str | None,
    authoritative_runtime_identity: dict[str, Any] | None,
    engine_kind: str,
    workspace_names_a_sandbox: bool = False,
) -> dict[str, Any]:
    """Align a conversation row with its workspace authority.

    ``authoritative_sandbox_id`` is None whenever the workspace is not READY,
    because a non-READY workspace cannot be dispatched to. That is a routing
    fact, and copying it onto ``sandbox_id`` unconditionally also severs the
    box's last name: the session row is the only place
    ``_resolve_sandbox_backend`` can learn a box's backend, so nulling it makes
    the box undestroyable by the by-id path at the same moment the workspace
    stops naming it. Two reasonable writes composing into "alive and
    unaddressable" — the guard below skips the write whenever it would do
    that.

    ``workspace_names_a_sandbox`` is what separates them. When the workspace
    still holds a ``current_sandbox_id`` — hibernating with an unconfirmed
    kill, recovery pending, materializing — the box has an authority naming it
    and this row's copy is redundant, so clearing it is safe. When the
    workspace names no sandbox, this row's copy may be the last one there is, and
    it is left alone; ``runtime_binding`` already reports the session as
    undispatchable through its own status field, which is what callers read.
    """
    updates: dict[str, Any] = {}
    current_sandbox_id = _clean(session.get("sandbox_id"))
    current_endpoint = _clean(session.get("sandbox_endpoint"))
    severs_last_name = (
        authoritative_sandbox_id is None
        and current_sandbox_id is not None
        and not workspace_names_a_sandbox
    )
    if authoritative_sandbox_id != current_sandbox_id and not severs_last_name:
        updates["sandbox_id"] = authoritative_sandbox_id
    if current_endpoint:
        updates["sandbox_endpoint"] = None
    if authoritative_sandbox_id and authoritative_expires_at != _clean(session.get("expires_at")):
        updates["expires_at"] = authoritative_expires_at
    if authoritative_sandbox_id is None and _clean(session.get("expires_at")):
        updates["expires_at"] = None

    workspace_ref = session.get("workspace_ref")
    if isinstance(workspace_ref, dict):
        updated_ref = dict(workspace_ref)
        ref_severs_last_name = (
            authoritative_sandbox_id is None
            and _clean(updated_ref.get("sandbox_id")) is not None
            and not workspace_names_a_sandbox
        )
        if (
            _clean(updated_ref.get("sandbox_id")) != authoritative_sandbox_id
            and not ref_severs_last_name
        ):
            updated_ref["sandbox_id"] = authoritative_sandbox_id
        if _clean(updated_ref.get("engine_kind")) != engine_kind:
            updated_ref["engine_kind"] = engine_kind
        # workspace runtime_identity is owner-level metadata. Do not copy it into
        # workspace_ref; per-user profile identity is derived by the workspace plan.
        if updated_ref != workspace_ref:
            updates["workspace_ref"] = updated_ref
    # Assistant workspace runtime_identity is owner/bootstrap metadata. Per-user
    # profile identity is derived by the assistant workspace planner, so do not
    # copy workspace identity into a user conversation session.
    if isinstance(session.get("runtime_identity"), dict):
        updates["runtime_identity"] = None
    return updates


async def reconcile_session_runtime_binding(
    *,
    session: dict[str, Any],
    sessions_repo: Any,
    agent_repo: Any | None = None,
    assistant_workspace_service: Any | None = None,
    persist: bool = False,
) -> tuple[dict[str, Any], RuntimeBindingResolution]:
    normalized = dict(session)
    # A Session-owned allocation records its resolved sandbox_id on the Session.
    # The provider may place it in an Agent-shared physical sandbox, but there is
    # no separate runtime-subject row from which to reconcile this binding.
    if is_assistant_user_conversation(normalized) and assistant_workspace_service is not None:
        resolution = await resolve_assistant_workspace_binding(
            session=normalized,
            assistant_workspace_service=assistant_workspace_service,
        )
        status_updates = _assistant_binding_status_updates(normalized, resolution)
        updates = {**resolution.routing_updates, **status_updates}
        if updates:
            normalized.update(updates)
            if persist:
                await _persist_updates(
                    sessions_repo,
                    resolution.session_id,
                    updates,
                )
        return normalized, resolution
    return normalized, _build_non_shared_resolution(normalized)


def _assistant_binding_status_updates(
    session: dict[str, Any],
    resolution: RuntimeBindingResolution,
) -> dict[str, Any]:
    updates: dict[str, Any] = {
        "runtime_binding": resolution.as_projection(),
    }
    if resolution.status in {"UNAVAILABLE", "RECOVERY_REQUIRED", "HIBERNATING"}:
        updates["runtime_unavailable"] = True
        if resolution.reason_message:
            updates["last_error"] = resolution.reason_message
        return updates
    if resolution.status in {"READY", "DELETED"}:
        updates["runtime_unavailable"] = False
        updates["last_error"] = None
        if (
            resolution.status == "READY"
            and _clean(session.get("state")) == "TERMINATED"
            and bool(session.get("runtime_unavailable"))
        ):
            updates["state"] = "READY"
    return updates
