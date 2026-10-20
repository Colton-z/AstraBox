"""A conversation's lifecycle: create, recover, take offline, end, and its permission mode.

The platform owns the box a conversation runs in; the agent runtime owns
whether it is working. These operations act on the box and on the session row
only, and consult the runtime's own reported status (``runtime_status``)
where taking a conversation offline could cut off its work.

Startups and recoveries run in background tasks, each under a claim on the
session row (:mod:`.ownership`). Writes that make a runtime usable, or declare
it gone, send the session's ``reader`` notice from the same statement; a
permission-mode change sends ``permission``.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.model import SessionState
from astrabox.core.service.orchestrator.runtime.execution_target import require_execution_target
from astrabox.core.service.orchestrator.runtime.preparation import require_session_user_prerequisites
from astrabox.core.service.orchestrator.engine_kind_utils import resolve_session_engine_kind
from astrabox.core.service.orchestrator.runtime_binding import (
    is_assistant_user_conversation,
    reconcile_session_runtime_binding,
)
from astrabox.core.service.orchestrator.sandbox_names import (
    keep_name_updates,
    release_name_updates,
)
from astrabox.core.service.orchestrator.session_lifecycle.permission_modes import (
    resolve_initial_mode,
    validate_mode_for_session,
)
from astrabox.core.service.orchestrator.session_lifecycle.recovery import SessionRecovery
from astrabox.core.service.orchestrator.session_lifecycle.startup import SessionStartup
from astrabox.core.service.orchestrator.session_service import SessionService
from astrabox.persistence.provider import get_persistence
from astrabox.seams.admission import (
    ADMISSION_KIND_SANDBOX,
    AdmissionRequest,
    enforce_admission,
)
from astrabox.seams.runtime_preparation import (
    validated_user_action,
)
from astrabox.seams.sandbox_disposal import (
    SANDBOX_DESTRUCTION_REFUSED,
    SANDBOX_DESTRUCTION_RETAINED,
    SANDBOX_DESTRUCTION_UNCONFIRMED,
)

logger = get_logger(__name__)

#: The runtime status during which a conversation may not be taken offline.
_WORKING = "working"


class SessionLifecycleService:
    def __init__(
        self,
        *,
        sessions_repo: Any,
        session_service: SessionService,
        agent_config: Any,
        runtime_manager: Any,
        runtime_subjects: Any,
        assistant_workspace_service: Any,
        spawn_background_task: Callable[..., asyncio.Task[Any]],
    ) -> None:
        self._sessions = sessions_repo
        self._session_service = session_service
        self._agent_config = agent_config
        self._runtime_manager = runtime_manager
        self._assistant_workspace_service = assistant_workspace_service
        self._spawn_background_task = spawn_background_task
        self._startup = SessionStartup(
            sessions_repo=sessions_repo,
            agent_config=agent_config,
            runtime_manager=runtime_manager,
            runtime_subjects=runtime_subjects,
        )
        self._recovery = SessionRecovery(
            sessions_repo=sessions_repo,
            agent_config=agent_config,
            runtime_manager=runtime_manager,
            runtime_subjects=runtime_subjects,
            startup=self._startup,
        )

    # -- create ------------------------------------------------------------

    async def create_session(
        self,
        user: UserContext,
        *,
        session_kind: str,
        workspace_ref: dict[str, Any],
        permission_mode: str | None = None,
        session_id: str | None = None,
        hidden: bool = False,
        owner_type: str | None = None,
        owner_id: str | None = None,
        source_type: str | None = None,
        agent_id: str | None = None,
        deployment_name: str | None = None,
        title: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a conversation and start its runtime; the session as its owner sees it.

        The row is inserted ``CREATING`` and its startup runs in the
        background. With ``idempotency_key`` the session id is derived from the
        owner, the subject and the key, so a retried create answers with the
        conversation the first one made. Whichever create claims the row's
        startup starts the one runtime it gets.
        """
        subject = str(
            agent_id
            or owner_id
            or workspace_ref.get("agent_id")
            or workspace_ref.get("assistant_id")
            or ""
        ).strip()
        template = await self._agent_config.resolve_session_harness(
            {"workspace_ref": workspace_ref, "agent_id": agent_id, "owner_id": owner_id}
        )
        if template is None:
            raise APIError(
                code="TEMPLATE_NOT_ALLOWED",
                message=f"agent '{subject}' not found or not permitted",
                status_code=403,
            )
        await self._session_service.record_user_profile(user)
        key = str(idempotency_key or "").strip() or None
        if session_id is not None and key is not None:
            raise ValueError("session_id and idempotency_key are mutually exclusive")
        resolved_session_id = session_id or (
            _idempotent_session_id(user.user_id, subject, key) if key is not None else str(uuid.uuid4())
        )
        if key is not None:
            existing = await self._sessions.get_session_including_deleted(resolved_session_id)
            if existing is not None:
                _require_same_create(existing, user=user, subject=subject)
                await self._start(existing)
                return _owner_view(existing)
        created = await self._session_service.create_session_record(
            user,
            permission_mode=permission_mode,
            session_id=resolved_session_id,
            session_kind=session_kind,
            workspace_ref=workspace_ref,
            hidden=hidden,
            owner_type=owner_type,
            owner_id=owner_id,
            source_type=source_type,
            agent_id=agent_id,
            deployment_name=deployment_name,
            title=title,
        )
        await self._start(created)
        return _owner_view(created)

    async def _start(self, session: dict[str, Any]) -> None:
        """Claim a new row's startup and run it in the background.

        A row whose startup another create already claimed, or that left
        ``CREATING``, is not started again.
        """
        require_execution_target(session)
        session_id = str(session["session_id"])
        generation = str(session.get("sandbox_generation") or "")
        if str(session.get("state") or "") != SessionState.CREATING.value or not generation:
            return
        owner = str(uuid.uuid4())
        if not await self._sessions.compare_and_update_session(
            session_id,
            expected={
                "state": SessionState.CREATING.value,
                "runtime_recovery_owner": None,
                "sandbox_generation": generation,
            },
            updates={"runtime_recovery_owner": owner},
        ):
            return
        self._spawn(
            f"session-startup-{session_id}",
            session_id,
            lambda: self._startup.run(
                session_id=session_id,
                owner=owner,
                sandbox_generation=generation,
                resume_engine_session_key=None,
            ),
        )

    # -- recover -------------------------------------------------------------

    async def recover_session(self, user: UserContext, session_id: str) -> dict[str, Any]:
        """Recover the owner's conversation; the session as it stands once recovery started.

        Recovery provisions a box, so it passes the same sandbox admission as
        a create: looping it must not get around a per-user cap.
        """
        session = await self._session_service.must_get_owned_session(user, session_id)
        await enforce_admission(
            AdmissionRequest(
                kind=ADMISSION_KIND_SANDBOX,
                user_id=str(user.user_id or ""),
                session_id=session_id,
                agent_id=str(session.get("agent_id") or "").strip() or None,
            )
        )
        await self._recover(session)
        current = await self._sessions.get_session(session_id)
        if current is None:
            raise APIError(code="SESSION_NOT_FOUND", message="session not found", status_code=404)
        return _owner_view(current)

    async def replace_runtime(self, session_id: str) -> None:
        """Start the recovery of a session whose runtime is gone, for the platform itself.

        Returns once the recovery task runs; ``READY`` arrives later with the
        session's ``reader`` notice. Safe to call concurrently: one call
        claims the recovery and the others find it in progress. Raises the
        user-action refusal while the startup waits on something only the
        user can do.
        """
        session = await self._sessions.get_session(session_id)
        if session is None:
            raise APIError(code="SESSION_NOT_FOUND", message="session not found", status_code=404)
        await self._recover(session)

    async def _recover(self, session: dict[str, Any]) -> None:
        require_execution_target(session)
        session_id = str(session["session_id"])
        session, _binding = await reconcile_session_runtime_binding(
            session=session,
            sessions_repo=self._sessions,
            assistant_workspace_service=self._assistant_workspace_service,
            persist=True,
        )
        state = str(session.get("state") or "")
        observed = state
        if state == SessionState.CREATING.value:
            # A startup or recovery drives this session and its READY write
            # announces the runtime, unless it has been creating past the
            # creation timeout: the session then reads as terminated, and its
            # startup, likely lost with the process that ran it, is superseded.
            if SessionService._sanitize_session(session).get("state") == SessionState.CREATING.value:
                return
            observed = SessionState.TERMINATED.value
        elif state not in {SessionState.READY.value, SessionState.TERMINATED.value}:
            raise APIError(
                code="INVALID_REQUEST",
                message=f"session state is {state}, recovery only applies to TERMINATED or READY sessions",
                status_code=409,
            )
        await self._refuse_while_startup_waits_on_user(session)
        owner = str(uuid.uuid4())
        claimed = await self._sessions.compare_and_update_session(
            session_id,
            expected={
                "state": state,
                "sandbox_generation": session.get("sandbox_generation"),
                "runtime_recovery_owner": session.get("runtime_recovery_owner"),
            },
            updates={
                "state": SessionState.CREATING.value,
                "runtime_recovery_owner": owner,
                "startup_user_action": None,
            },
        )
        if not claimed:
            current = await self._sessions.get_session(session_id)
            if current is None:
                raise APIError(code="SESSION_NOT_FOUND", message="session not found", status_code=404)
            if current.get("state") == SessionState.CREATING.value:
                return
            raise APIError(
                code="RUNTIME_RECOVERY_SUPERSEDED",
                message=f"concurrent recovery changed the Session to {current.get('state')}",
                status_code=409,
            )
        observed_session = {**session, "state": observed}
        self._spawn(
            f"session-recovery-{session_id}",
            session_id,
            lambda: self._recovery.run(session=observed_session, owner=owner),
        )

    async def _refuse_while_startup_waits_on_user(self, session: dict[str, Any]) -> None:
        """Answer with the action the last startup is waiting on, before taking a box.

        The deployment's provider is asked first, so a recovery requested
        after the user acted starts the runtime, and one requested before
        does not take and release a box for a startup bound to stop again.
        """
        if validated_user_action(session.get("startup_user_action")) is None:
            return
        await require_session_user_prerequisites(session)

    # -- take offline and end -----------------------------------------------

    async def delete_session(self, user: UserContext, session_id: str) -> dict[str, Any]:
        session = await self._session_service.must_get_owned_session(user, session_id)
        require_execution_target(session)
        _refuse_while_working(session, "delete session")
        if is_assistant_user_conversation(session):
            sandbox_id = str(session.get("sandbox_id") or "").strip() or None
            await self._dispose_conversation_runtime(session_id, sandbox_id=sandbox_id)
            await self._sessions.soft_delete(session_id, user.user_id)
            return {
                "session_id": session_id,
                "deleted": True,
                "sandbox_id": sandbox_id,
                "status": "conversation-deleted",
                "killed": False,
            }
        sandbox_id = str(session.get("sandbox_id") or "").strip() or None
        destruction = await self._runtime_manager.terminate_runtime(
            session_id, fallback_sandbox_id=sandbox_id
        )
        leaked_sandbox_id = destruction.leaked_sandbox_id
        if leaked_sandbox_id is not None:
            # A deleted row is hidden from every cleanup path that finds a box
            # through its session. Keep the row and the box's name visible so
            # the same DELETE can be retried.
            keep = keep_name_updates(destruction, row=session)
            logger.error(
                "session delete refused to soft-delete session=%s because sandbox=%s "
                "may still be running: %s",
                session_id,
                leaked_sandbox_id,
                destruction.detail,
            )
            if keep:
                await self._sessions.update_session(session_id, keep)
            error_data = {
                "failed_operations": ["destroy sandbox"],
                "sandbox_id": leaked_sandbox_id,
                "destruction_outcome": destruction.outcome,
            }
            if destruction.outcome == SANDBOX_DESTRUCTION_UNCONFIRMED:
                raise APIError(
                    code="SESSION_SANDBOX_DESTRUCTION_UNCONFIRMED",
                    message="sandbox destruction was attempted but not confirmed",
                    status_code=502,
                    data=error_data,
                )
            if destruction.outcome == SANDBOX_DESTRUCTION_REFUSED:
                raise APIError(
                    code="SESSION_SANDBOX_DESTRUCTION_REFUSED",
                    message="sandbox destruction was refused before it was attempted",
                    status_code=502,
                    data=error_data,
                )
            raise RuntimeError(
                "a leaking sandbox destruction must be UNCONFIRMED or REFUSED, "
                f"not {destruction.outcome!r}"
            )
        await self._sessions.soft_delete(session_id, user.user_id)
        return {"session_id": session_id, "deleted": True}

    async def archive_session(self, user: UserContext, session_id: str) -> dict[str, Any]:
        async with get_persistence().coordination.runtime_lock(session_id) as check_lock:
            session = await self._session_service.must_get_owned_session(user, session_id)
            require_execution_target(session)
            if is_assistant_user_conversation(session):
                _refuse_while_working(session, "archive session")
                sandbox_id = str(session.get("sandbox_id") or "").strip() or None
                await self._dispose_conversation_runtime(session_id, sandbox_id=sandbox_id)
                result: dict[str, Any] = {
                    "session_id": session_id,
                    "sandbox_id": sandbox_id,
                    "status": "conversation-archived",
                    "killed": False,
                }
            else:
                # An Agent conversation owns its runtime allocation, so archiving
                # releases it: a dedicated box is destroyed, an isolated placement
                # in the Agent's shared box is released.
                result = await self._reclaim_sandbox(session, before_publish=check_lock)
            await check_lock()
            await self._sessions.archive_session(session_id, user.user_id)
            return {**result, "session_id": session_id, "archived": True}

    async def terminate_sandbox(self, user: UserContext, session_id: str) -> dict[str, Any]:
        session = await self._session_service.must_get_owned_session(user, session_id)
        return await self._reclaim_sandbox(session)

    async def end_conversation(self, user: UserContext, session_id: str) -> dict[str, Any]:
        async with get_persistence().coordination.runtime_lock(session_id) as check_lock:
            session = await self._session_service.must_get_owned_session(user, session_id)
            require_execution_target(session)
            if not is_assistant_user_conversation(session):
                raise APIError(
                    code="INVALID_REQUEST",
                    message="end conversation is only supported for assistant conversations",
                    status_code=409,
                )
            _refuse_while_working(session, "end conversation")
            sandbox_id = str(session.get("sandbox_id") or "").strip() or None
            await self._dispose_conversation_runtime(session_id, sandbox_id=sandbox_id)
            await check_lock()
            await self._sessions.update_session(
                session_id,
                {
                    "state": SessionState.TERMINATED.value,
                    "runtime_unavailable": False,
                    "last_error": "assistant conversation ended by user",
                },
            )
            return {
                "session_id": session_id,
                "sandbox_id": sandbox_id,
                "status": "conversation-ended",
                "killed": False,
            }

    async def kill_session(self, session: dict[str, Any]) -> dict[str, Any]:
        """End a session for an administrator, destroying its box.

        ``TERMINATED`` takes the row out of the dead-binding probe, so a box
        whose destruction is not confirmed goes onto the row's undestroyed
        ledger in the same write and is reported to the operator.
        """
        require_execution_target(session)
        session_id = str(session["session_id"])
        if is_assistant_user_conversation(session):
            raise APIError(
                code="ASSISTANT_WORKSPACE_SHARED_RUNTIME",
                message=(
                    "assistant user conversations share an assistant workspace sandbox; "
                    "kill the workspace owner runtime instead"
                ),
                status_code=409,
            )
        sandbox_id = str(session.get("sandbox_id") or "").strip() or None
        try:
            destruction = await self._runtime_manager.terminate_runtime(
                session_id, fallback_sandbox_id=sandbox_id
            )
        except Exception as exc:
            logger.warning("admin kill failed session=%s err=%s", session_id, exc)
            raise APIError(
                code="ADMIN_KILL_FAILED",
                message=f"failed to terminate session runtime: {exc}",
                status_code=502,
            ) from exc
        keep = keep_name_updates(destruction, row=session)
        if keep:
            logger.error(
                "admin kill could not confirm the destruction of session=%s sandbox=%s; "
                "it is recorded as undestroyed: %s",
                session_id,
                sandbox_id,
                destruction.detail,
            )
        await self._sessions.update_session(
            session_id,
            {
                **keep,
                "state": SessionState.TERMINATED.value,
                "runtime_unavailable": True,
                "last_error": "killed by admin",
            },
            notify="session",
        )
        return {
            "session_id": session_id,
            "sandbox_id": sandbox_id,
            "killed": destruction.confirmed,
            "destruction": destruction.outcome,
            "destruction_detail": destruction.detail,
        }

    async def _reclaim_sandbox(
        self,
        session: dict[str, Any],
        *,
        before_publish: Callable[[], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Release an Agent conversation's box; the conversation stays ``READY``.

        The conversation keeps its history and is woken by its next message
        on a new box, which resumes the engine's session. Only archive and
        delete take a conversation out of the active set.
        """
        require_execution_target(session)
        session_id = str(session["session_id"])
        _refuse_while_working(session, "terminate sandbox")
        if is_assistant_user_conversation(session):
            raise APIError(
                code="INVALID_REQUEST",
                message=(
                    "assistant conversation cannot terminate the shared sandbox; "
                    "use the end conversation API"
                ),
                status_code=409,
            )
        sandbox_id = str(session.get("sandbox_id") or "").strip() or None
        runtime = self._runtime_manager.get_runtime(session_id, sandbox_id=sandbox_id)
        if not sandbox_id and runtime is not None:
            sandbox_id = str(getattr(runtime, "sandbox_id", "") or "").strip() or None
        if not sandbox_id and runtime is None:
            # No pointer and no runtime here: nothing to destroy or sever. A
            # runtime missing from this process alone proves nothing (another
            # replica may serve the session), so both are required.
            if before_publish is not None:
                await before_publish()
            await self._sessions.update_session(
                session_id,
                {
                    "state": SessionState.READY.value,
                    "runtime_unavailable": True,
                    "sandbox_id": None,
                    "last_error": None,
                },
                notify="reader",
            )
            return {
                "session_id": session_id,
                "sandbox_id": None,
                "status": "sandbox-reclaimed",
                "killed": False,
            }
        try:
            destruction = await self._runtime_manager.terminate_runtime(
                session_id, fallback_sandbox_id=sandbox_id
            )
        except Exception as exc:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=f"failed to terminate sandbox {sandbox_id}: {exc}",
                status_code=502,
            ) from exc
        killed = destruction.confirmed
        # RETAINED is a release: this call closed the placement it owned and
        # left the box running for a longer-lived owner, such as an Agent's
        # shared box carrying other conversations.
        released = killed or destruction.outcome == SANDBOX_DESTRUCTION_RETAINED
        if sandbox_id and not released:
            # The pointer stays: nobody established this box's death.
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=(
                    f"failed to terminate sandbox {sandbox_id}: "
                    f"{destruction.outcome} — {destruction.detail}"
                ),
                status_code=502,
            )
        binding = (
            # RETAINED: clear this conversation's binding, not the box's
            # identity, which its longer-lived owner still holds.
            {"sandbox_id": None}
            if destruction.outcome == SANDBOX_DESTRUCTION_RETAINED
            and destruction.sandbox_id == sandbox_id
            else release_name_updates(destruction, sandbox_id=sandbox_id, row=session)
        )
        if before_publish is not None:
            await before_publish()
        await self._sessions.update_session(
            session_id,
            {
                **binding,
                "state": SessionState.READY.value,
                "runtime_unavailable": True,
                "last_error": None,
            },
            notify="reader",
        )
        return {
            "session_id": session_id,
            "sandbox_id": sandbox_id,
            "status": "sandbox-reclaimed",
            "killed": bool(killed),
        }

    async def _dispose_conversation_runtime(self, session_id: str, *, sandbox_id: str | None) -> None:
        """End an Assistant conversation's processes; the shared workspace box stays.

        Ordinary eviction only disconnects a client so another process can
        resume it. Ending a conversation for good closes its engine session
        through its client or the adapter's recorded native identity, and its
        terminal shell through the shell the terminal recorded. A supplier
        failure in that finalizer refuses completion.
        """
        from astrabox.persistence.repository.terminal_repository import TerminalRepository

        failed_operations: list[str] = []
        try:
            session = await self._sessions.get_session(session_id)
            if session is None:
                raise RuntimeError("conversation record is unavailable for final disposal")
            await self._runtime_manager.dispose_runtime_session(
                session_id,
                sandbox_id=sandbox_id,
                engine_kind=resolve_session_engine_kind(session),
                engine_session_key=session.get("engine_session_key"),
                runtime_cursor=session.get("reader_cursor"),
                workspace_ref=session.get("workspace_ref"),
            )
        except Exception as exc:
            failed_operations.append("stop agent process")
            logger.error(
                "resident engine process disposal failed: session=%s sandbox=%s err=%s",
                session_id,
                sandbox_id,
                exc,
            )
        shell = await TerminalRepository().shell(session_id)
        try:
            await self._runtime_manager.dispose_terminal_session(
                session_id, sandbox_id=sandbox_id, pty_session_id=shell
            )
        except Exception as exc:
            failed_operations.append("stop terminal process")
            logger.error(
                "conversation terminal PTY disposal failed: session=%s sandbox=%s pty=%s err=%s",
                session_id,
                sandbox_id,
                shell,
                exc,
            )
        if failed_operations:
            # The success state is not written. Both disposals are idempotent,
            # so the caller can retry and finish whichever did not complete.
            raise APIError(
                code="SESSION_PROCESS_CLEANUP_FAILED",
                message="one or more session processes could not be stopped",
                status_code=502,
                data={"failed_operations": failed_operations},
            )

    # -- permission mode -------------------------------------------------------

    async def update_session_permission_mode(
        self, user: UserContext, session_id: str, permission_mode: str
    ) -> dict[str, Any]:
        """Store the mode the owner chose; the session's reader applies it to the runtime."""
        session = await self._session_service.must_get_owned_session(user, session_id)
        state = str(session.get("state") or "")
        if state == SessionState.CREATING.value:
            raise APIError(
                code="SESSION_BUSY",
                message="session is still creating, please retry",
                status_code=409,
            )
        if state in {SessionState.TERMINATED.value, SessionState.DELETED.value}:
            raise APIError(
                code="INVALID_REQUEST",
                message=f"cannot update permission mode when session state is {state}",
                status_code=409,
            )
        mode = validate_mode_for_session(permission_mode, session=session)
        previous = str(session.get("permission_mode") or "").strip() or None
        if not await self._sessions.set_permission_mode(session_id, mode):
            raise APIError(code="SESSION_NOT_FOUND", message="session not found", status_code=404)
        return {
            "session_id": session_id,
            "permission_mode": mode,
            "previous_permission_mode": previous,
            "changed": mode != previous,
        }

    # -- tasks -------------------------------------------------------------------

    def _spawn(self, name: str, session_id: str, work: Callable[[], Awaitable[Any]]) -> None:
        async def run() -> None:
            try:
                await work()
            except asyncio.CancelledError:
                raise
            except APIError as exc:
                if exc.code == "RUNTIME_RECOVERY_SUPERSEDED":
                    logger.info("%s superseded session=%s: %s", name, session_id, exc.message)
                else:
                    logger.error("%s failed session=%s: %s %s", name, session_id, exc.code, exc.message)
            except Exception:
                logger.exception("%s failed session=%s", name, session_id)

        self._spawn_background_task(run(), name=name)


def _idempotent_session_id(user_id: str, subject: str, key: str) -> str:
    digest = hashlib.sha256(f"conversation-create\0{user_id}\0{subject}\0{key}".encode()).hexdigest()
    return str(uuid.UUID(digest[:32]))


def _require_same_create(existing: dict[str, Any], *, user: UserContext, subject: str) -> None:
    """A key replays only the create it was first used for, and only while it lives."""
    existing_subject = str(
        existing.get("agent_id") or existing.get("owner_id") or existing.get("assistant_id") or ""
    ).strip()
    if str(existing.get("user_id") or "") != str(user.user_id or "") or existing_subject != subject:
        raise APIError(
            code="IDEMPOTENCY_KEY_CONFLICT",
            message="Idempotency-Key is already bound to another conversation",
            status_code=409,
        )
    if existing.get("deleted"):
        raise APIError(
            code="IDEMPOTENCY_KEY_RETIRED",
            message="Idempotency-Key belongs to a deleted conversation",
            status_code=409,
        )


def _refuse_while_working(session: dict[str, Any], operation: str) -> None:
    """Refuse to take a conversation offline while its runtime reports it is working.

    A runtime waiting for an answer is not working: nothing runs that taking
    the conversation offline would cut off. Neither is a runtime the platform
    has confirmed gone (``runtime_unavailable``): its last report describes a
    process that died with its box, and honouring it would keep the
    conversation from ever being taken offline.
    """
    if str(session.get("runtime_status") or "") != _WORKING or session.get("runtime_unavailable"):
        return
    logger.info("reject %s while the runtime is working session=%s", operation, session.get("session_id"))
    raise APIError(
        code="SESSION_BUSY",
        message=f"cannot {operation} while the agent is working",
        status_code=409,
        data={"runtime_status": _WORKING},
    )


def _owner_view(session: dict[str, Any]) -> dict[str, Any]:
    from astrabox.core.service.orchestrator.session_runtime.views import session_detail_fields

    return session_detail_fields(SessionService._sanitize_session(session))
