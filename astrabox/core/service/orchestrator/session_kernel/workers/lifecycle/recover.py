"""RecoverSession command: reattach-vs-recreate decision + its helpers.

``_recover_session_direct`` holds the reattach-vs-recreate branching for a
TERMINATED/READY session (its startup counterpart, ``_run_session_startup_direct``,
lives in ``startup.py``). ``_assert_conversation_safe_to_take_offline``
lives in ``commands.py``; the sanitize/state-derivation and
recreate-projection-reset helpers used only by recovery are gathered here as
:class:`_RecoverSessionMixin`, mixed into ``SessionLifecycleWorker``.
"""

from __future__ import annotations

import contextlib
import uuid
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.model import SessionState
from astrabox.core.service.orchestrator.engine.capabilities import require_session_kind
from astrabox.core.service.orchestrator.sandbox_names import keep_name_updates
from astrabox.core.service.orchestrator.engine_kind_utils import (
    resolve_session_engine_kind,
)
from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    needs_turn_recovery,
)
from astrabox.core.service.orchestrator.session_kernel.workers.lifecycle.recovery_ownership import RecoveryOwnership
from astrabox.seams.sandbox_disposal import SandboxDestruction
from astrabox.seams.sandbox import SANDBOX_LIFECYCLE_PROBE_OK

logger = get_logger(__name__)


class _RecoverSessionMixin:
    """RecoverSession command + its helpers, mixed into :class:`SessionLifecycleWorker`."""

    async def _recover_session_direct(
        self,
        *,
        user: UserContext,
        session: dict[str, Any],
        session_id: str,
        command_event: dict[str, Any],
    ) -> dict[str, Any]:
        observed_session = dict(session)
        observed_runtime = self._runtime_manager.get_runtime(session_id)
        observed_snapshot = await self._session_snapshots_repo.get_snapshot(session_id)
        sanitized_session = self._session_service._sanitize_session(session)
        state_fix_updates: dict[str, Any] = {}
        for key in ("state", "runtime_unavailable", "last_error", "recovery_policy", "recovery_reason"):
            if sanitized_session.get(key) != session.get(key):
                state_fix_updates[key] = sanitized_session.get(key)
        if state_fix_updates:
            session = {**session, **state_fix_updates}
            sanitized_session = {**sanitized_session, **state_fix_updates}

        state = await self._derive_effective_session_state_for_recover(
            session_id=session_id,
            session=sanitized_session,
        )

        if state == SessionState.CREATING.value or observed_session.get("state") == SessionState.CREATING.value:
            current = await self._sessions_repo.get_session(session_id)
            if current and current.get("state") == SessionState.CREATING.value and current.get("_runtime_recovery_owner"):
                return {"session_id": session_id, "state": SessionState.CREATING.value,
                        "status": "recovery-in-progress", "sandbox_id": current.get("sandbox_id")}

        if state not in {SessionState.TERMINATED.value, SessionState.READY.value}:
            raise APIError(
                code="INVALID_REQUEST",
                message=(
                    f"session state is {state}, recovery only applies to "
                    "TERMINATED or READY sessions"
                ),
                status_code=409,
            )

        owner = str(command_event.get("causation_id") or "").strip()
        if not owner:
            raise RuntimeError("Recovery command has no durable identity")
        claimed = await self._sessions_repo.compare_and_update_session(
            session_id,
            expected={
                "state": observed_session.get("state"),
                "sandbox_generation": observed_session.get("sandbox_generation"),
                "_runtime_recovery_owner": observed_session.get("_runtime_recovery_owner"),
            },
            updates={"state": SessionState.CREATING.value, "_runtime_recovery_owner": owner},
        )
        if not claimed:
            current = await self._sessions_repo.get_session(session_id)
            if not current:
                raise APIError(code="SESSION_NOT_FOUND", message="session not found", status_code=404)
            if current.get("state") != SessionState.CREATING.value:
                raise APIError(
                    code="RUNTIME_RECOVERY_SUPERSEDED",
                    message=f"concurrent recovery changed the Session to {current.get('state')}",
                    status_code=409,
                )
            return {"session_id": session_id, "state": current.get("state"),
                    "status": "recovery-in-progress", "sandbox_id": current.get("sandbox_id")}
        ownership = RecoveryOwnership(self._sessions_repo, session_id, owner)
        startup_requested = False
        try:
            with ownership.bind():
                result = await self._recover_owned_session(
                    user=user, session=session, session_id=session_id,
                    command_event=command_event, sanitized_session=sanitized_session,
                    state=state, ownership=ownership, observed_runtime=observed_runtime,
                    observed_snapshot=observed_snapshot,
                )
            startup_requested = result.get("status") == "startup-requested"
            return result
        finally:
            if not startup_requested:
                # A live reattach writes READY itself. A failed probe leaves the
                # prior state; it must not strand a CREATING owner with no startup.
                await self._sessions_repo.compare_and_update_session(
                    session_id, expected=ownership.expected,
                    updates={"state": session.get("state")},
                )

    async def _recover_owned_session(
        self, *, user: UserContext, session: dict[str, Any], session_id: str,
        command_event: dict[str, Any], sanitized_session: dict[str, Any],
        state: str, ownership: RecoveryOwnership, observed_runtime: Any,
        observed_snapshot: dict[str, Any] | None,
    ) -> dict[str, Any]:
        template = await self._session_service._agent_config.resolve_session_harness(session)
        if not template:
            raise APIError(
                code="TEMPLATE_NOT_ALLOWED",
                message="agent configuration not found",
                status_code=403,
            )

        permission_mode = str(session.get("permission_mode") or "").strip() or None
        engine_session_key = str(session.get("engine_session_key") or "").strip() or None
        session_kind = require_session_kind(session.get("session_kind"))
        previous_sandbox_id = str(session.get("sandbox_id") or "").strip() or None
        recovery_gate_session = dict(session)
        recovery_policy = str(sanitized_session.get("recovery_policy") or "").strip().lower() or None

        async def _require_manual_recovery(reason: str, message: str) -> dict[str, Any]:
            # Session state is left untouched here; degradation is expressed
            # via the snapshot's conversation_state. The resolved reason is
            # persisted so the read projection can surface it: without this
            # write it would live only in this return value and never reach a
            # client. Successful recovery paths clear it.
            with contextlib.suppress(Exception):
                await ownership.update({"recovery_reason": reason})
            return {
                "session_id": session_id,
                "state": SessionState.READY.value,
                "status": "manual-recovery-required",
                "recovery_reason": reason,
                "sandbox_id": session.get("sandbox_id"),
                "permission_mode": str(session.get("permission_mode") or permission_mode or "").strip() or None,
            }

        recovery_action = self._runtime_subjects.recovery_action_for(session)
        if recovery_action == "restart_session_on_subject":
            return await self._request_runtime_subject_startup(
                user=user,
                session=session,
                session_id=session_id,
                command_event=command_event,
                permission_mode=permission_mode,
                engine_session_key=engine_session_key,
                destruction=None,
                clear_session_binding=False,
                ownership=ownership, observed_snapshot=observed_snapshot,
            )
        if recovery_action != "recover_session_allocation":
            raise RuntimeError(f"unknown runtime recovery action: {recovery_action!r}")

        # Agent chat recovers from the binding on its Session. The provider
        # interprets that binding as a dedicated box or an isolated placement
        # in the Agent's shared box; the lifecycle worker does not add a second
        # Agent-wake state machine.
        snapshot_for_recover = await self._session_snapshots_repo.get_snapshot(session_id)
        conversation_degraded = needs_turn_recovery(snapshot_for_recover)
        resume_agent_chat = session_kind == "agent_chat" and bool(engine_session_key)
        expired_binding = False
        if previous_sandbox_id:
            # Explicit recovery must validate provider liveness even when a
            # resident client and the stored expiry still look healthy.
            probe = await self._runtime_manager.get_sandbox_lifecycle_probe(previous_sandbox_id)
            expired_binding = self._runtime_manager._is_terminal_sandbox_lifecycle_probe(probe)
            if not expired_binding and probe.probe_status != SANDBOX_LIFECYCLE_PROBE_OK:
                raise RuntimeError(
                    f"cannot determine bound sandbox liveness before recovery "
                    f"session={session_id} sandbox={previous_sandbox_id}: {probe.error_text}"
                )

        if conversation_degraded and recovery_policy == "auto" and not resume_agent_chat:
            if not previous_sandbox_id:
                return await _require_manual_recovery(
                    "sandbox_missing",
                    "original sandbox info is missing; cannot auto-recover — manual session recovery required",
                )
            if expired_binding:
                return await _require_manual_recovery(
                    "sandbox_expired",
                    "original sandbox has expired; cannot auto-recover — manual session recovery required",
                )

        if (
            (conversation_degraded or resume_agent_chat)
            and previous_sandbox_id
            # A reclaimed sandbox (terminate_sandbox or lease expiry) leaves
            # previous_sandbox_id dead — attempting reattach there only times out
            # for ~2 minutes before falling back to recreate, blowing the resumed-
            # message turn budget. Reclaim keeps the session READY, not TERMINATED,
            # so runtime_unavailable — not the TERMINATED check — is the signal
            # that the sandbox is gone. Skip reattach and go straight to recreate;
            # the new runner restores Claude's transcript through SessionStore,
            # using engine_session_key as the exact vendor resume identity.
            and state != SessionState.TERMINATED.value
            and not bool(recovery_gate_session.get("runtime_unavailable"))
            and not expired_binding
        ):
            try:
                workspace_plan = self._runtime_manager.plan_runtime_attach(
                    agent_id=str(session.get("agent_id") or ""),
                    session_id=session_id,
                    session_kind=session_kind,
                    sandbox_id=previous_sandbox_id,
                    engine_session_key=engine_session_key,
                    existing_terminal_cwd=str(session.get("terminal_cwd") or "").strip() or None,
                    engine_kind=resolve_session_engine_kind(session),
                    runtime_identity=session.get("runtime_identity") if isinstance(session.get("runtime_identity"), dict) else None,
                )
                runtime = await self._runtime_manager.ensure_runtime(
                    session_id,
                    template,
                    sandbox_id=previous_sandbox_id,
                    user_id=user.user_id,
                    engine_session_key=engine_session_key,
                    permission_mode=permission_mode,
                    session_kind=session_kind,
                    workspace_plan=workspace_plan,
                    runtime_identity=session.get("runtime_identity") if isinstance(session.get("runtime_identity"), dict) else None,
                    startup_guard=ownership.require_current,
                )
                effective_engine_session_key = engine_session_key or (
                    getattr(runtime, "engine_session_key", None) if runtime is not None else None
                )
                # Attachment can renew the box; read its resulting expiry.
                # Query failure must not enter destructive reattach cleanup.
                try:
                    real_expires_at = await self._runtime_manager.get_sandbox_expires_at(
                        previous_sandbox_id
                    )
                except Exception as exc:
                    return await _require_manual_recovery(
                        "sandbox_expiry_unavailable",
                        f"cannot read attached sandbox expiry: {exc}",
                    )
                updates: dict[str, Any] = {
                    "state": SessionState.READY.value,
                    "sandbox_id": getattr(runtime, "sandbox_id", None) or previous_sandbox_id,
                    "sandbox_endpoint": None,
                    "runtime_unavailable": False,
                    "last_error": None,
                    "startup_progress": None,
                    "model_name": self._runtime_manager.resolve_template_model_name(template),
                    "recovery_policy": None,
                    "recovery_reason": None,
                }
                if effective_engine_session_key:
                    updates["engine_session_key"] = effective_engine_session_key
                updates["expires_at"] = (
                    real_expires_at.isoformat() if real_expires_at is not None else None
                )
                updates.update(
                    await self._fetch_runtime_initialization_metadata(
                        session_id, runtime
                    )
                )
                await ownership.update(updates)
                latest = await self._sessions_repo.get_session(session_id)
                current = latest if isinstance(latest, dict) else {**session, **updates}
                return {
                    "session_id": session_id,
                    "state": str(current.get("state") or SessionState.READY.value),
                    "status": "reattached",
                "sandbox_id": current.get("sandbox_id"),
                "permission_mode": str(current.get("permission_mode") or permission_mode or "").strip() or None,
            }
            except Exception:
                # Failed attachment does not authorize deleting a live box.
                # In particular a stale owner must not sweep its successor's
                # runtime or allocation through session-scoped cleanup.
                if observed_runtime is not None:
                    await self._runtime_manager.evict_runtime_if_current(session_id, observed_runtime)
                raise

        await ownership.require_current()
        if observed_runtime is not None:
            await self._runtime_manager.evict_runtime_if_current(session_id, observed_runtime)
        # Provider absence is already authoritative. No destructive supplier
        # action is needed to replace compute that is confirmed gone; keeping
        # cleanup out of this path also prevents deleting a successor's box.
        destruction = None
        if previous_sandbox_id:
            destruction = (
                SandboxDestruction.confirmed_gone(
                    previous_sandbox_id,
                    detail="explicit recovery confirmed supplier death",
                )
                if expired_binding
                else SandboxDestruction.refused(
                    previous_sandbox_id,
                    detail="recovery retains the observed box for scoped reconciliation",
                )
            )
        return await self._request_runtime_subject_startup(
            user=user,
            session=session,
            session_id=session_id,
            command_event=command_event,
            permission_mode=permission_mode,
            engine_session_key=engine_session_key,
            destruction=destruction,
            clear_session_binding=True,
            ownership=ownership, observed_snapshot=observed_snapshot,
        )

    async def _request_runtime_subject_startup(
        self,
        *,
        user: UserContext,
        session: dict[str, Any],
        session_id: str,
        command_event: dict[str, Any],
        permission_mode: str | None,
        engine_session_key: str | None,
        destruction: SandboxDestruction | None,
        clear_session_binding: bool,
        ownership: RecoveryOwnership,
        observed_snapshot: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Reset one conversation and re-enter the common startup authority.

        Session-owned recovery retains an unconfirmed old sandbox on its
        disposal ledger before moving the binding. A shared-subject Session keeps its routing
        copy until startup publishes the owner's replacement: the Session is
        not allowed to sever or destroy the owner's last sandbox name.
        """

        await ownership.require_current()
        await self._reset_conversation_projection_for_recreate_recovery(session_id, observed_snapshot)
        updates: dict[str, Any] = {
            "state": SessionState.CREATING.value,
            "runtime_unavailable": True,
            "last_error": None,
            "startup_progress": "creating_sandbox",
            "pending_interaction": None,
            "current_turn_id": None,
            "interrupt_requested": False,
            "sandbox_generation": str(uuid.uuid4()),
            "sandbox_callback_token": str(uuid.uuid4()),
            "recovery_policy": None,
            "recovery_reason": None,
        }
        if clear_session_binding:
            updates.update(keep_name_updates(destruction, row=session))
            updates.update(
                {
                    "sandbox_id": None,
                    "sandbox_endpoint": None,
                    "expires_at": None,
                }
            )
        await ownership.update(updates)

        startup_command_id = str(uuid.uuid4())
        correlation_id = str(command_event.get("correlation_id") or "").strip() or None
        startup_command = await self._session_events_repo.append_event(
            {
                "session_id": session_id,
                "channel": "command",
                "event_type": "command.accepted",
                "causation_id": startup_command_id,
                "correlation_id": correlation_id,
                "payload": {
                    "command_type": "StartSessionStartup",
                    "sandbox_generation": updates["sandbox_generation"],
                    "author_user_id": user.user_id,
                    "template_name": str(session.get("template_name") or "").strip(),
                    "permission_mode": permission_mode,
                    "resume_session_id": engine_session_key,
                    "recovery_owner": ownership.owner,
                },
            }
        )
        latest = await self._sessions_repo.get_session(session_id)
        current = latest if isinstance(latest, dict) else {**session, **updates}
        return {
            "session_id": session_id,
            "state": str(current.get("state") or SessionState.CREATING.value),
            "status": "startup-requested",
            "sandbox_id": current.get("sandbox_id"),
            "permission_mode": str(
                current.get("permission_mode") or permission_mode or ""
            ).strip()
            or None,
            "startup_command_id": startup_command_id,
            "startup_command_event_seq": int(startup_command.get("event_seq") or 0),
        }

    async def _reset_conversation_projection_for_recreate_recovery(
        self, session_id: str, observed: dict[str, Any] | None,
    ) -> None:
        if not observed:
            return
        # The original row version and exact turn bind this reset. A new
        # owner/turn cannot be cleared even if it wins while the write awaits.
        expected = {key: observed.get(key) for key in (
            "updated_at", "current_turn_id", "active_interaction_id",
            "conversation_state", "last_turn_id",
        )}
        applied = await self._session_snapshots_repo.force_update_fields(
            session_id,
            {"conversation_state": "IDLE", "current_turn_id": None,
             "active_interaction_id": None, "current_turn_remote_anchor": None},
            extra_filter=expected,
        )
        if not applied:
            raise APIError(code="RUNTIME_RECOVERY_SUPERSEDED",
                           message="conversation changed before recovery reset", status_code=409)
        turn_id = str(observed.get("current_turn_id") or "").strip()
        if turn_id:
            await self._interaction_snapshots_repo.deactivate_active_for_turn(session_id, turn_id)

    # This worker does not drive the conversation channel: turn recovery and
    # conversation state are owned by ``TurnWorker`` and ``ReconcileWorker``.

    async def _derive_effective_session_state_for_recover(
        self,
        *,
        session_id: str,
        session: dict[str, Any],
    ) -> str:
        base_state = str((session or {}).get("state") or "").strip()
        snapshot = await self._session_snapshots_repo.get_snapshot(session_id)
        if not isinstance(snapshot, dict):
            return base_state

        lifecycle_state = str(snapshot.get("session_lifecycle_state") or "").strip()
        conversation_state = str(snapshot.get("conversation_state") or "").strip()
        terminal_state = str(snapshot.get("terminal_state") or "").strip()
        active_interaction_id = str(snapshot.get("active_interaction_id") or "").strip()
        active_interaction = await self._interaction_snapshots_repo.get_active_interaction(session_id)

        if lifecycle_state == "CREATING":
            return SessionState.CREATING.value
        if lifecycle_state == "TERMINATED":
            return SessionState.TERMINATED.value
        if lifecycle_state == "DELETED":
            return SessionState.DELETED.value
        if terminal_state == "INTERRUPTING" or conversation_state == "INTERRUPTING":
            return SessionState.INTERRUPTING.value
        # A turn is only in flight while something is flying it. When the bound
        # sandbox is gone the platform stamps runtime_unavailable on the row,
        # and the snapshot still reads RUNNING because the runtime that would
        # have closed the turn went with the box. Reporting PROCESSING then
        # refuses the one operation that can move the conversation on — and it
        # refuses it to a caller the platform itself told to retry, since the
        # gone-sandbox answer says a replacement is being prepared. Recovery is
        # how the conversation reaches a runtime that can judge its own turn;
        # the judgement belongs there, and this gate only decides whether the
        # conversation gets that far.
        runtime_gone = bool((session or {}).get("runtime_unavailable"))
        if not runtime_gone and (
            terminal_state == "RUNNING" or conversation_state in {"PROCESSING", "STREAMING"}
        ):
            return "PROCESSING"
        if conversation_state == "WAITING_FOR_INTERACTION":
            return "WAITING_INPUT"
        if isinstance(active_interaction, dict) or active_interaction_id:
            return "WAITING_INPUT"
        # DEGRADED/LOST runtime or IDLE+FAILED conversation → still READY
        # (degradation is surfaced via runtime_warning, not as a blocking state)
        return SessionState.READY.value
