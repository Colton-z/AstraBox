"""Lifecycle command executors: delete / archive / end / terminate.

The "_direct" implementations ``_execute_command`` switches to.
Delete/archive/end-conversation/terminate share a "tear down the runtime,
then write a terminal-ish session shape" pattern, branching heavily on
``_is_assistant_user_conversation`` (``assistant_workspace.py``). Gathered
here as :class:`_LifecycleCommandsMixin`, mixed into
``SessionLifecycleWorker``. ``recover`` lives in a separate module
(``recover.py``).

``_terminate_session_direct`` writes, inline and twice, a "READY + sandbox
cleared + runtime_unavailable=True" update shape belonging to the same
"dead/reclaimed sandbox binding" family that ``sandbox_lifecycle.py``'s
``terminal_session_updates()`` / ``converge_dead_sandbox()`` owns for a
Session. Confirmed box death enters the owner-wide
``converge_dead_sandbox_owners()`` method, which delegates its Session
transition to that writer. This path stays separate because its trigger is an
explicit user ``terminate_sandbox`` command: it clears ``last_error`` instead
of setting a death message and skips the
``expires_at``/``busy_*``/``current_turn_*`` fields. Keep the two Session
writers in sync when either changes.
"""

from __future__ import annotations

from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.model import SessionState
from astrabox.core.service.orchestrator.conversation_offline_guard import (
    describe_offline_blocking_turn,
)
from astrabox.core.service.orchestrator.sandbox_names import (
    keep_name_updates,
    release_name_updates,
)
from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    settle_parked_turn,
)
from astrabox.seams.sandbox_disposal import (
    SANDBOX_DESTRUCTION_REFUSED,
    SANDBOX_DESTRUCTION_RETAINED,
    SANDBOX_DESTRUCTION_UNCONFIRMED,
)

logger = get_logger(__name__)


class _LifecycleCommandsMixin:
    """Lifecycle command executors (delete/archive/end/terminate), mixed into
    :class:`SessionLifecycleWorker`."""

    async def _dispose_conversation_runtime(
        self, session_id: str, *, sandbox_id: str | None
    ) -> None:
        """Tear down a conversation that is ending for good (delete/archive/end).

        A shared Assistant sandbox outlives any one conversation. Normal
        runtime eviction therefore only disconnects the platform client so a
        turn can recover after a process restart. Final conversation disposal
        is stronger: close the engine session, terminate its resident process,
        and keep the shared sandbox alive for other conversations.

        The latest dispatch journal entry retains the engine's opaque process
        anchor after a completed turn's active snapshot is cleared. Supplying
        it lets the engine adapter finish cleanup when the in-memory client that
        dispatched the turn is unavailable.
        """
        engine_kind: str | None = None
        engine_turn_id: str | None = None
        terminal_pty_session_id: str | None = None
        snapshot: dict[str, Any] = {}
        try:
            snapshot = await self._session_snapshots_repo.get_snapshot(session_id) or {}
            terminal_pty_session_id = str(
                snapshot.get("terminal_pty_session_id") or ""
            ).strip() or None
            last_turn_id = str((snapshot or {}).get("last_turn_id") or "").strip()
            if last_turn_id:
                events = await self._session_events_repo.list_events(
                    session_id,
                    channel="conversation",
                    turn_id=last_turn_id,
                    event_type="dispatch.confirmed",
                    limit=10,
                )
                for event in reversed(events):
                    payload = event.get("payload")
                    recovery_context = (
                        payload.get("recovery_context")
                        if isinstance(payload, dict)
                        else None
                    )
                    anchor = (
                        recovery_context.get("engine_anchor")
                        if isinstance(recovery_context, dict)
                        else None
                    )
                    if not isinstance(anchor, dict):
                        continue
                    candidate_kind = str(anchor.get("engine_kind") or "").strip()
                    candidate_turn_id = str(
                        anchor.get("engine_turn_id") or ""
                    ).strip()
                    if candidate_kind and candidate_turn_id:
                        engine_kind = candidate_kind
                        engine_turn_id = candidate_turn_id
                        break
        except Exception as exc:
            logger.error(
                "could not recover the last engine process anchor before "
                "conversation disposal: session=%s err=%s",
                session_id,
                exc,
            )
            raise APIError(
                code="SESSION_CLEANUP_STATE_READ_FAILED",
                message="could not read the repository state required for session cleanup",
                status_code=503,
                data={"failed_operations": ["read cleanup state"]},
            ) from exc

        failed_operations: list[str] = []
        try:
            await self._runtime_manager.dispose_runtime_session(
                session_id,
                sandbox_id=sandbox_id,
                engine_kind=engine_kind,
                engine_turn_id=engine_turn_id,
            )
        except Exception as exc:
            failed_operations.append("stop agent process")
            logger.error(
                "resident engine process disposal failed: "
                "session=%s sandbox=%s err=%s",
                session_id,
                sandbox_id,
                exc,
            )
        try:
            await self._runtime_manager.dispose_terminal_session(
                session_id,
                sandbox_id=sandbox_id,
                pty_session_id=terminal_pty_session_id,
            )
        except Exception as exc:
            failed_operations.append("stop terminal process")
            logger.error(
                "conversation terminal PTY disposal failed: "
                "session=%s sandbox=%s pty=%s err=%s",
                session_id,
                sandbox_id,
                terminal_pty_session_id,
                exc,
            )
        if failed_operations:
            # Do not let delete/archive/end write their success state. Both
            # process-disposal operations are idempotent, so the caller can
            # retry after a transient runtime failure and finish whichever
            # operation did not complete the first time.
            raise APIError(
                code="SESSION_PROCESS_CLEANUP_FAILED",
                message="one or more session processes could not be stopped",
                status_code=502,
                data={"failed_operations": failed_operations},
            )

        # The durable id above handles backend restarts; this clears the
        # process-local lookup used by subsequent terminal commands only after
        # every requested remote cleanup operation was confirmed.
        from astrabox.core.service.orchestrator.terminal_service import (
            forget_terminal_session,
        )

        forget_terminal_session(session_id)
    async def _delete_session_direct(
        self,
        *,
        user: UserContext,
        session: dict[str, Any],
        session_id: str,
    ) -> dict[str, Any]:
        await self._assert_conversation_safe_to_take_offline(
            session_id=session_id,
            operation="delete session",
        )
        if self._is_assistant_user_conversation(session):
            sandbox_id = self._assistant_workspace_sandbox_id(session)
            await self._dispose_conversation_runtime(session_id, sandbox_id=sandbox_id)
            await self._sessions_repo.soft_delete(session_id, user.user_id)
            return {
                "session_id": session_id,
                "deleted": True,
                "sandbox_id": sandbox_id,
                "status": "conversation-deleted",
                "killed": False,
            }

        sandbox_id = str(session.get("sandbox_id") or "").strip() or None
        destruction = await self._runtime_manager.terminate_runtime(
            session_id,
            fallback_sandbox_id=sandbox_id,
        )
        leaked_sandbox_id = destruction.leaked_sandbox_id
        if leaked_sandbox_id is not None:
            # Soft-delete hides this row from every cleanup path that resolves a
            # box through its owning session. Keep both the row and the failed
            # destruction's durable name visible so the same DELETE can retry.
            keep = keep_name_updates(destruction, row=session)
            logger.error(
                "session delete refused to soft-delete session=%s because "
                "sandbox=%s may still be running: %s",
                session_id,
                leaked_sandbox_id,
                destruction.detail,
            )
            if keep:
                await self._sessions_repo.update_session(session_id, keep)
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
        await self._sessions_repo.soft_delete(session_id, user.user_id)
        return {"session_id": session_id, "deleted": True}

    async def _archive_session_direct(
        self,
        *,
        user: UserContext,
        session: dict[str, Any],
        session_id: str,
    ) -> dict[str, Any]:
        if self._is_assistant_user_conversation(session):
            await self._assert_conversation_safe_to_take_offline(
                session_id=session_id,
                operation="archive session",
            )
            sandbox_id = self._assistant_workspace_sandbox_id(session)
            await self._dispose_conversation_runtime(session_id, sandbox_id=sandbox_id)
            terminate_result = {
                "session_id": session_id,
                "sandbox_id": sandbox_id,
                "status": "conversation-archived",
                "killed": False,
            }
        else:
            # The Agent conversation owns its runtime allocation, so archive
            # releases it. The provider either destroys a dedicated box or
            # releases the isolated placement in an Agent-shared box.
            terminate_result = await self._terminate_session_direct(
                session=session,
                session_id=session_id,
            )
        await self._sessions_repo.archive_session(session_id, user.user_id)
        return {
            **terminate_result,
            "session_id": session_id,
            "archived": True,
        }

    async def _end_conversation_direct(
        self,
        *,
        session: dict[str, Any],
        session_id: str,
    ) -> dict[str, Any]:
        state = str(session.get("state") or "")
        if state == SessionState.DELETED.value:
            raise APIError(
                code="INVALID_REQUEST",
                message="session already deleted",
                status_code=409,
            )
        if not self._is_assistant_user_conversation(session):
            raise APIError(
                code="INVALID_REQUEST",
                message="end conversation is only supported for assistant conversations",
                status_code=409,
            )
        await self._assert_conversation_safe_to_take_offline(
            session_id=session_id,
            operation="end conversation",
        )
        sandbox_id = self._assistant_workspace_sandbox_id(session)
        await self._dispose_conversation_runtime(session_id, sandbox_id=sandbox_id)
        await self._sessions_repo.update_session(
            session_id,
            {
                "state": SessionState.TERMINATED.value,
                "runtime_unavailable": False,
                "last_error": "assistant conversation ended by user",
            },
        )
        result = {
            "session_id": session_id,
            "sandbox_id": sandbox_id,
            "status": "conversation-ended",
            "killed": False,
        }
        return result

    async def _terminate_session_direct(
        self,
        *,
        session: dict[str, Any],
        session_id: str,
    ) -> dict[str, Any]:
        state = str(session.get("state") or "")
        if state == SessionState.DELETED.value:
            raise APIError(
                code="INVALID_REQUEST",
                message="session already deleted",
                status_code=409,
            )
        await self._assert_conversation_safe_to_take_offline(
            session_id=session_id,
            operation="terminate sandbox",
        )

        if self._is_assistant_user_conversation(session):
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
            # Per-session lifecycle separation: reclaiming the (already-absent) sandbox
            # does not terminate the conversation. The session stays READY (derives to
            # lifecycle ACTIVE = wakeable); the lost sandbox is surfaced as degradation
            # via runtime_unavailable, and the next message rebuilds a fresh sandbox and
            # restores history from the mirror. Only explicit archive (hidden) / delete
            # take a conversation out of the active set; TERMINATED is reserved for those.
            # Same dead-binding-convergence shape as
            # sandbox_lifecycle.terminal_session_updates()/converge_dead_sandbox() (see
            # module docstring) but kept as a separate writer here.
            #
            # There is no name to sever here and no box to judge: this row holds
            # no sandbox_id, so the write below clears a field that is already
            # empty. "This process has no runtime for the session" is not on its
            # own evidence of anything — on a multi-replica deployment it says
            # only that another replica serves the session — so this branch
            # requires both the absent runtime and the empty pointer and acts on
            # neither alone.
            await self._settle_parked_turn_on_reclaim(session_id)
            await self._sessions_repo.update_session(
                session_id,
                {
                    "state": SessionState.READY.value,
                    "runtime_unavailable": True,
                    "sandbox_id": None,
                    "sandbox_endpoint": None,
                    "pending_interaction": None,
                    "last_error": None,
                },
            )
            return {
                "session_id": session_id,
                "sandbox_id": None,
                "status": "sandbox-reclaimed",
                "killed": False,
            }

        try:
            destruction = await self._runtime_manager.terminate_runtime(
                session_id,
                fallback_sandbox_id=sandbox_id,
            )
        except BaseException as exc:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=f"failed to terminate sandbox {sandbox_id}: {exc}",
                status_code=502,
            ) from exc
        killed = destruction.confirmed
        # RETAINED is a release, not a failure. It says this call closed the
        # child resource it owned inside the box and left the box running
        # because a longer-lived owner still owns it — an agent's pooled box
        # carrying isolated sessions. The seam states it (`leaked_sandbox_id`
        # is None for RETAINED, so nothing is unaddressed), and the runtime
        # manager's two startup-cleanup readers already judge it this way.
        # Reading it as "not killed, therefore failed" turned a correct
        # disposal into a 502 for every conversation on a shared box.
        released = killed or destruction.outcome == SANDBOX_DESTRUCTION_RETAINED
        if sandbox_id and not released:
            # The pointer is deliberately left as it is: this raise aborts
            # before the clearing write below, so the row keeps naming a box
            # whose death nobody established.
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=(
                    f"failed to terminate sandbox {sandbox_id}: "
                    f"{destruction.outcome} — {destruction.detail}"
                ),
                status_code=502,
            )

        # Sandbox reclaimed, conversation stays wakeable (see above): READY +
        # runtime_unavailable, not TERMINATED. The next message rebuilds a fresh sandbox
        # and restores history from the mirror. Explicit archive/delete are the only
        # paths that take a conversation out of the active set. Clear sandbox_id/endpoint:
        # the conversation truthfully has no sandbox now, and a lingering dead sandbox_id
        # makes concurrent resolvers (file panel, background continuation, agent-port
        # dispatch on port 44772) call get_endpoint on the just-Terminated sandbox — the
        # vendored SDK then dumps a GetEndpoint-on-Terminated traceback even though the
        # caller handles the miss gracefully. No sandbox_id -> resolvers short-circuit.
        # Same dead-binding-convergence shape as
        # sandbox_lifecycle.terminal_session_updates()/converge_dead_sandbox() (see
        # module docstring) but kept as a separate writer here.
        await self._settle_parked_turn_on_reclaim(session_id)
        await self._sessions_repo.update_session(
            session_id,
            {
                **(
                    # Clear this conversation's binding, not the box's durable
                    # identity. RETAINED says a longer-lived owner keeps the box
                    # after this conversation releases it. The seam cannot infer
                    # that ownership transfer generically, and no leak ledger
                    # entry is owed because `leaked_sandbox_id` is None.
                    {"sandbox_id": None, "sandbox_endpoint": None}
                    if destruction.outcome == SANDBOX_DESTRUCTION_RETAINED
                    and destruction.sandbox_id == sandbox_id
                    else release_name_updates(
                        destruction,
                        sandbox_id=sandbox_id,
                        row=session,
                        also_clear=("sandbox_endpoint",),
                    )
                ),
                "state": SessionState.READY.value,
                "runtime_unavailable": True,
                "pending_interaction": None,
                "last_error": None,
            },
        )
        return {
            "session_id": session_id,
            "sandbox_id": sandbox_id,
            "status": "sandbox-reclaimed",
            "killed": bool(killed),
        }

    async def _settle_parked_turn_on_reclaim(self, session_id: str) -> None:
        """Close a turn parked on an approval whose compute was just reclaimed.

        The approval wait was in-memory in the reclaimed box — there is
        nothing left to answer, and nobody left to produce the turn's
        terminal (the parked turn's worker exited at the interaction
        boundary by design). Settling here keeps the defer contract honest:
        the answer endpoint refuses (nothing pending), the conversation is
        immediately sendable, and the next message resumes the session — the
        engine re-requests the approval.
        """
        snapshot = await self._session_snapshots_repo.get_snapshot(session_id)
        turn_id = str((snapshot or {}).get("current_turn_id") or "").strip()
        if not turn_id:
            return
        if str((snapshot or {}).get("conversation_state") or "").strip() != "WAITING_FOR_INTERACTION":
            return
        await settle_parked_turn(
            session_events_repo=self._session_events_repo,
            session_snapshots_repo=self._session_snapshots_repo,
            interaction_snapshots_repo=self._interaction_snapshots_repo,
            session_id=session_id,
            turn_id=turn_id,
            command_id=None,
            status="FAILED",
            failure_phase="sandbox_reclaimed",
            error_text="sandbox reclaimed while awaiting interaction",
            causation=f"reclaim-settle:{session_id}:{turn_id}",
        )

    async def _assert_conversation_safe_to_take_offline(
        self,
        *,
        session_id: str,
        operation: str,
    ) -> None:
        snapshot = await self._session_snapshots_repo.get_snapshot(session_id)
        blocker = describe_offline_blocking_turn(snapshot)
        if blocker is None:
            return
        logger.info(
            "reject %s while conversation turn is active "
            "session=%s turn=%s state=%s reason=%s recovery_phase=%s",
            operation,
            session_id,
            blocker.get("turn_id"),
            blocker.get("conversation_state"),
            blocker.get("reason"),
            blocker.get("recovery_phase"),
        )
        raise APIError(
            code="SESSION_BUSY",
            message=f"cannot {operation} while a conversation turn is active",
            status_code=409,
            data=blocker,
        )
