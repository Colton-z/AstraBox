from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.common.utils.time_utils import parse_iso
from astrabox.core.model import SessionState

logger = get_logger(__name__)

CALLBACK_SUBJECT_SESSION = "session"

_TERMINAL_STATUSES = {"terminated", "error", "paused", "pausing"}

_OWNER_TERMINAL_STATES = frozenset(
    {SessionState.TERMINATED.value, SessionState.DELETED.value}
)


@dataclass(frozen=True)
class SandboxOwnerConvergence:
    """Every durable owner affected by one terminal sandbox fact."""

    sandbox_id: str
    converged_sessions: tuple[str, ...] = ()
    ignored_sessions: dict[str, str] = field(default_factory=dict)
    converged_agents: tuple[str, ...] = ()
    ignored_agents: dict[str, str] = field(default_factory=dict)
    converged_assistant_workspaces: tuple[str, ...] = ()
    ignored_assistant_workspaces: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SandboxOwnerRealignment:
    """Durable owners refreshed by one control-plane-confirmed live fact."""

    sandbox_id: str
    realigned_sessions: tuple[str, ...] = ()
    realigned_agents: tuple[str, ...] = ()
    realigned_assistant_workspaces: tuple[str, ...] = ()


def planned_session_teardown_reason(session: dict[str, Any]) -> str | None:
    """Name the owner intent that makes a box self-notice non-actionable."""
    if str(session.get("sandbox_parked_at") or "").strip():
        return "session_parked"
    if str(session.get("state") or "").strip() in _OWNER_TERMINAL_STATES:
        return "session_ended"
    return None


def _session_assistant_workspace_owner_id(session: dict[str, Any]) -> str:
    workspace_ref = session.get("workspace_ref")
    if (
        isinstance(workspace_ref, dict)
        and str(workspace_ref.get("kind") or "").strip() == "assistant"
    ):
        return str(workspace_ref.get("assistant_id") or "").strip()
    if str(session.get("owner_type") or "").strip() == "assistant_workspace":
        return str(session.get("owner_id") or "").strip()
    return ""


def new_sandbox_callback_fields() -> tuple[str, str]:
    return str(uuid.uuid4()), str(uuid.uuid4())


def build_sandbox_callback_url(
    *,
    subject_type: str,
    subject_id: str,
    generation: str,
    token: str,
) -> str:
    settings = load_astrabox_settings()
    base_url = str(settings.mcp_proxy_base_url or "").strip()
    if not base_url:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message="sandbox callback base url is not configured",
            status_code=500,
        )
    return (
        f"{base_url.rstrip('/')}/api/v1/sandbox-callback/"
        f"{quote(subject_type, safe='')}/"
        f"{quote(subject_id, safe='')}/"
        f"{quote(generation, safe='')}/"
        f"{quote(token, safe='')}"
    )


def build_sandbox_callback_url_from_record(
    *,
    subject_type: str,
    subject_id: str,
    record: dict[str, Any] | None,
) -> str:
    generation = str((record or {}).get("sandbox_generation") or "").strip()
    token = str((record or {}).get("sandbox_callback_token") or "").strip()
    if not generation or not token:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=f"sandbox callback context missing for {subject_type} {subject_id}",
            status_code=500,
        )
    return build_sandbox_callback_url(
        subject_type=subject_type,
        subject_id=subject_id,
        generation=generation,
        token=token,
    )


def normalize_sandbox_callback_status(value: Any) -> str:
    return str(value or "").strip().lower()


def is_terminal_sandbox_callback_status(status: str) -> bool:
    return normalize_sandbox_callback_status(status) in _TERMINAL_STATUSES


def parse_sandbox_callback_expire_time(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1_000_000_000_000:
            ts /= 1000.0
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.isdigit():
            ts = float(text)
            if ts > 1_000_000_000_000:
                ts /= 1000.0
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        else:
            try:
                dt = datetime.fromisoformat(text)
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _should_apply_callback_expire_time(
    *,
    current_expire_time: Any,
    callback_expire_time: str | None,
) -> bool:
    if not callback_expire_time:
        return False
    current_text = str(current_expire_time or "").strip()
    if not current_text:
        return True
    try:
        current_dt = parse_iso(current_text)
        callback_dt = parse_iso(callback_expire_time)
    except (TypeError, ValueError):
        return True
    return callback_dt > current_dt


def _session_callback_error_text(status: str) -> str:
    normalized = normalize_sandbox_callback_status(status)
    if normalized == "error":
        return "sandbox terminated abnormally"
    if normalized in {"paused", "pausing"}:
        return "sandbox unavailable"
    return "sandbox terminated"


def terminal_session_updates(
    *,
    session: dict[str, Any],
    last_error: str,
) -> dict[str, Any]:
    """Canonical durable updates for a session whose sandbox is confirmed gone.

    This is the single shape every convergence path writes — the status
    callback (push), the expiration-watcher probe reconciler (pull) and the
    bootstrap expired-session reclaim all end here, so a dead binding always
    converges the same way.

    Product invariant: users never perceive the sandbox. A conversation whose
    sandbox died stays READY with the binding cleared — the next message
    re-provisions a fresh sandbox and restores history. TERMINATED is reserved
    for explicit end/delete and must never be produced by sandbox death, for
    any session kind.

    Busy locks and current-turn markers are cleared: a session whose sandbox
    is gone cannot have a live turn.
    """
    _ = session  # one shape for every session kind — the sandbox is invisible to users
    return {
        "state": SessionState.READY.value,
        "runtime_unavailable": True,
        "last_error": last_error,
        "sandbox_id": None,
        "sandbox_endpoint": None,
        "expires_at": None,
        "current_turn_id": None,
        "current_turn_seq": None,
        "current_turn_partial": None,
        "startup_progress": None,
    }


class SandboxLifecycleService:
    def __init__(self, *, platform_service: Any) -> None:
        self._platform = platform_service
        self._sessions_repo = platform_service._sessions_repo
        self._agent_repo = platform_service._agent_repo
        self._assistant_workspace_service = (
            platform_service._assistant_workspace_service
        )

    async def list_dead_sandbox_probe_candidates(
        self,
        *,
        now_iso: str,
        limit: int,
    ) -> dict[str, int]:
        """Return suspicious boxes from every durable owner, fairly deduplicated.

        The value is the number of owner rows that nominated the box. The
        watcher probes each key once and keeps its existing row-based summary
        counters, while the round-robin merge prevents a busy owner collection
        from starving the others under the per-tick sandbox budget.
        """
        page_limit = max(1, int(limit or 1))
        session_rows = await self._sessions_repo.list_dead_binding_probe_candidates(
            now_iso=now_iso,
            limit=page_limit,
        )
        agent_rows = await self._agent_repo.list_dead_binding_probe_candidates(
            now_iso=now_iso,
            limit=page_limit,
        )
        workspace_rows = (
            await self._assistant_workspace_service.list_dead_binding_probe_candidates(
                now_iso=now_iso,
                limit=page_limit,
            )
        )
        batches = (
            (session_rows, "sandbox_id"),
            (agent_rows, "sandbox_id"),
            (workspace_rows, "current_sandbox_id"),
        )
        candidates: dict[str, int] = {}
        max_rows = max((len(rows) for rows, _sandbox_field in batches), default=0)
        for index in range(max_rows):
            for rows, sandbox_field in batches:
                if index >= len(rows):
                    continue
                sandbox_id = str(
                    (rows[index] or {}).get(sandbox_field) or ""
                ).strip()
                if not sandbox_id:
                    continue
                if sandbox_id not in candidates and len(candidates) >= page_limit:
                    continue
                candidates[sandbox_id] = candidates.get(sandbox_id, 0) + 1
        return candidates

    async def realign_live_sandbox_owners(
        self,
        sandbox_id: str,
        *,
        expires_at: str | None,
    ) -> SandboxOwnerRealignment:
        """Refresh every owner still naming one confirmed-live sandbox."""
        target = str(sandbox_id or "").strip()
        if not target:
            raise ValueError("sandbox_id is required")
        resolved_expires_at = str(expires_at or "").strip() or None

        realigned_sessions: list[str] = []
        sessions = await self._sessions_repo.list_sessions_by_sandbox_id(target)
        for session in sessions:
            session_id = str((session or {}).get("session_id") or "").strip()
            if not session_id:
                continue
            updates: dict[str, Any] = {
                "sandbox_liveness_suspect_at": "",
                "expires_at": resolved_expires_at,
            }
            if await self._sessions_repo.compare_and_update_session(
                session_id,
                expected={"sandbox_id": target},
                updates=updates,
                touch_updated_at=False,
            ):
                realigned_sessions.append(session_id)

        realigned_agents: list[str] = []
        agents = await self._agent_repo.list_agents_by_sandbox_id(target)
        for agent in agents:
            agent_id = str((agent or {}).get("agent_id") or "").strip()
            if not agent_id:
                continue
            if await self._agent_repo.compare_and_update_agent(
                agent_id,
                expected={"sandbox_id": target},
                updates={"expires_at": resolved_expires_at},
            ):
                realigned_agents.append(agent_id)

        realigned_workspaces: list[str] = []
        workspaces = (
            await self._assistant_workspace_service.list_workspaces_by_sandbox_id(
                target
            )
        )
        for workspace in workspaces:
            assistant_id = str(
                (workspace or {}).get("assistant_id") or ""
            ).strip()
            if not assistant_id:
                continue
            if await self._assistant_workspace_service.realign_live_sandbox(
                workspace=workspace,
                sandbox_id=target,
                expires_at=resolved_expires_at,
            ):
                realigned_workspaces.append(assistant_id)

        return SandboxOwnerRealignment(
            sandbox_id=target,
            realigned_sessions=tuple(realigned_sessions),
            realigned_agents=tuple(realigned_agents),
            realigned_assistant_workspaces=tuple(realigned_workspaces),
        )

    async def handle_callback(
        self,
        *,
        subject_type: str,
        subject_id: str,
        generation: str,
        token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        normalized_type = str(subject_type or "").strip().lower()
        if normalized_type == CALLBACK_SUBJECT_SESSION:
            return await self._handle_session_callback(
                session_id=subject_id,
                generation=generation,
                token=token,
                payload=payload,
            )
        raise APIError(
            code="INVALID_REQUEST",
            message=f"unsupported sandbox callback subject: {subject_type}",
            status_code=400,
        )

    async def _handle_session_callback(
        self,
        *,
        session_id: str,
        generation: str,
        token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        current = await self._sessions_repo.get_session(session_id)
        if not isinstance(current, dict):
            return {"handled": False, "ignored": "session_not_found"}

        expected_generation = str(current.get("sandbox_generation") or "").strip()
        expected_token = str(current.get("sandbox_callback_token") or "").strip()
        if expected_generation != generation or expected_token != token:
            return {"handled": False, "ignored": "stale_callback"}

        status = normalize_sandbox_callback_status(payload.get("status"))
        if not status:
            raise APIError(
                code="INVALID_REQUEST",
                message="sandbox callback missing status",
                status_code=400,
            )

        if is_terminal_sandbox_callback_status(status):
            sandbox_id = str(current.get("sandbox_id") or "").strip()
            if not sandbox_id:
                return {"handled": False, "ignored": "binding_already_cleared"}
            await self.converge_dead_sandbox_owners(
                sandbox_id,
                last_error=_session_callback_error_text(status),
                reason=f"sandbox_callback_{status}",
                preserve_planned_teardowns=status in {"paused", "pausing"},
            )
            return {
                "handled": True,
                "subject_type": CALLBACK_SUBJECT_SESSION,
                "subject_id": session_id,
                "status": status,
            }

        updates: dict[str, Any] = {}
        expires_at = parse_sandbox_callback_expire_time(payload.get("expireTime"))
        if _should_apply_callback_expire_time(
            current_expire_time=current.get("expires_at"),
            callback_expire_time=expires_at,
        ):
            updates["expires_at"] = expires_at

        if not updates:
            return {"handled": True, "subject_type": CALLBACK_SUBJECT_SESSION, "subject_id": session_id, "status": status}

        updated = await self._sessions_repo.compare_and_update_session(
            session_id,
            expected={
                "sandbox_generation": generation,
                "sandbox_callback_token": token,
            },
            updates=updates,
            touch_updated_at=False,
        )
        if not updated:
            return {"handled": False, "ignored": "stale_race"}

        with contextlib.suppress(Exception):
            await self._platform._sync_lifecycle_projection_from_session(
                session=current,
                session_id=session_id,
                updates=updates,
                reason=f"sandbox_callback_{status}",
            )

        return {
            "handled": True,
            "subject_type": CALLBACK_SUBJECT_SESSION,
            "subject_id": session_id,
            "status": status,
        }

    async def converge_dead_sandbox(
        self,
        session: dict[str, Any],
        *,
        last_error: str,
        reason: str,
    ) -> bool:
        """Single convergence entry for a session whose sandbox is confirmed gone.

        Push (status callback) and pull (probe reconciler) both end here so the
        durable convergence happens once, one way: the canonical
        ``terminal_session_updates`` dict, runtime eviction, and the kernel
        lifecycle projection. The exact ``sandbox_id`` predicate is
        the idempotency and ABA fence: a repeated or late fact cannot match an
        already-cleared binding or a replacement sandbox.

        Callers must hold control-plane-confirmed evidence that the sandbox is
        terminal (a terminal status callback, or a lifecycle probe returning
        NOT_FOUND / a terminal state). A transient probe failure is not such
        evidence and must not reach this method.
        """
        session_id = str((session or {}).get("session_id") or "").strip()
        if not session_id:
            return False
        sandbox_id = str((session or {}).get("sandbox_id") or "").strip()
        if not sandbox_id:
            return False
        updates = terminal_session_updates(session=session, last_error=last_error)
        applied = await self._sessions_repo.compare_and_update_session(
            session_id,
            expected={"sandbox_id": sandbox_id},
            updates=updates,
            touch_updated_at=False,
        )
        if not applied:
            return False
        await self._apply_terminal_side_effects(
            session=session,
            session_id=session_id,
            updates=updates,
            reason=reason,
        )
        logger.warning(
            "sandbox death convergence applied session=%s reason=%s last_error=%s",
            session_id,
            reason,
            last_error,
        )
        return bool(applied)

    async def project_session_runtime_ready(
        self,
        session: dict[str, Any],
        *,
        reason: str,
    ) -> None:
        """Project a newly usable Session sandbox through the lifecycle channel."""

        session_id = str((session or {}).get("session_id") or "").strip()
        sandbox_id = str((session or {}).get("sandbox_id") or "").strip()
        if not session_id or not sandbox_id or bool(session.get("runtime_unavailable")):
            raise ValueError("a ready Session runtime requires session_id and sandbox_id")
        await self._platform._sync_lifecycle_projection_from_session(
            session=session,
            session_id=session_id,
            updates={},
            reason=reason,
        )

    async def converge_dead_sandbox_owners(
        self,
        sandbox_id: str,
        *,
        last_error: str,
        reason: str,
        preserve_planned_teardowns: bool = False,
    ) -> SandboxOwnerConvergence:
        """Converge every durable owner of one confirmed-dead sandbox.

        The terminal fact and the fencing rules are global. What the fact
        means remains owner-specific: conversations retain their identity,
        Agents drop their replaceable shared-box pointer, and Assistant
        workspaces enter recovery with every marker derived from the old box
        invalidated.

        ``preserve_planned_teardowns`` is only for a box's self-notice. A Pod
        also exits while a session is intentionally parked or an Assistant
        workspace is intentionally released; each owner writes that intent
        before asking the provider to remove compute. Control-plane NOT_FOUND
        evidence must leave this false because a retained binding is invalid
        when its resource is confirmed absent.
        """
        target = str(sandbox_id or "").strip()
        if not target:
            raise ValueError("sandbox_id is required")

        converged_sessions: list[str] = []
        ignored_sessions: dict[str, str] = {}
        workspaces = (
            await self._assistant_workspace_service.list_workspaces_by_sandbox_id(
                target
            )
        )
        releasing_workspace_ids = {
            str((workspace or {}).get("assistant_id") or "").strip()
            for workspace in workspaces
            if preserve_planned_teardowns
            and str((workspace or {}).get("state") or "").strip()
            == "RECOVERY_REQUIRED"
            and str((workspace or {}).get("hibernated_at") or "").strip()
        }
        releasing_workspace_ids.discard("")
        sessions = await self._sessions_repo.list_sessions_by_sandbox_id(target)
        parked_agent_ids = {
            str((session or {}).get("agent_id") or "").strip()
            for session in sessions
            if preserve_planned_teardowns
            and str((session or {}).get("sandbox_parked_at") or "").strip()
        }
        parked_agent_ids.discard("")
        converged_agents: list[str] = []
        ignored_agents: dict[str, str] = {}
        agents = await self._agent_repo.list_agents_by_sandbox_id(target)
        for agent in agents:
            agent_id = str((agent or {}).get("agent_id") or "").strip()
            if not agent_id:
                continue
            if agent_id in parked_agent_ids:
                ignored_agents[agent_id] = "agent_box_parked"
                continue
            applied = await self._agent_repo.compare_and_update_agent(
                agent_id,
                expected={"sandbox_id": target},
                updates={
                    "sandbox_id": None,
                    "sandbox_backend": None,
                    "_resident_sandbox_generation": None,
                    "expires_at": None,
                },
            )
            if applied:
                converged_agents.append(agent_id)

        converged_workspaces: list[str] = []
        ignored_workspaces: dict[str, str] = {}
        for workspace in workspaces:
            assistant_id = str(
                (workspace or {}).get("assistant_id") or ""
            ).strip()
            if not assistant_id:
                continue
            release_pending = bool(
                str((workspace or {}).get("hibernated_at") or "").strip()
                and str((workspace or {}).get("state") or "").strip()
                == "RECOVERY_REQUIRED"
            )
            if preserve_planned_teardowns and release_pending:
                ignored_workspaces[assistant_id] = "workspace_release_pending"
                continue
            applied = await self._assistant_workspace_service.converge_dead_sandbox(
                workspace=workspace,
                sandbox_id=target,
                last_error=last_error,
            )
            if applied:
                converged_workspaces.append(assistant_id)

        # Parent owners are fenced first. Runtime-binding reconciliation reads
        # those records to project a box onto a Session; clearing a Session
        # first would leave a window in which the confirmed-dead id could be
        # projected back from an Agent or Assistant workspace.
        for session in sessions:
            session_id = str((session or {}).get("session_id") or "").strip()
            if not session_id:
                continue
            planned = (
                planned_session_teardown_reason(session)
                if preserve_planned_teardowns
                else None
            )
            if planned:
                ignored_sessions[session_id] = planned
                continue
            assistant_owner_id = _session_assistant_workspace_owner_id(session)
            if assistant_owner_id in releasing_workspace_ids:
                ignored_sessions[session_id] = "workspace_release_pending"
                continue
            if await self.converge_dead_sandbox(
                session,
                last_error=last_error,
                reason=reason,
            ):
                converged_sessions.append(session_id)

        result = SandboxOwnerConvergence(
            sandbox_id=target,
            converged_sessions=tuple(converged_sessions),
            ignored_sessions=ignored_sessions,
            converged_agents=tuple(converged_agents),
            ignored_agents=ignored_agents,
            converged_assistant_workspaces=tuple(converged_workspaces),
            ignored_assistant_workspaces=ignored_workspaces,
        )
        logger.warning(
            "sandbox owner convergence applied sandbox=%s reason=%s "
            "sessions=%s agents=%s assistant_workspaces=%s ignored_sessions=%s "
            "ignored_agents=%s "
            "ignored_assistant_workspaces=%s",
            target,
            reason,
            result.converged_sessions,
            result.converged_agents,
            result.converged_assistant_workspaces,
            result.ignored_sessions or {},
            result.ignored_agents or {},
            result.ignored_assistant_workspaces or {},
        )
        return result

    async def _apply_terminal_side_effects(
        self,
        *,
        session: dict[str, Any],
        session_id: str,
        updates: dict[str, Any],
        reason: str,
    ) -> None:
        with contextlib.suppress(Exception):
            await self._platform._runtime_manager.evict_runtime(session_id)
        with contextlib.suppress(Exception):
            await self._platform._sync_lifecycle_projection_from_session(
                session=session,
                session_id=session_id,
                updates=updates,
                reason=reason,
            )
