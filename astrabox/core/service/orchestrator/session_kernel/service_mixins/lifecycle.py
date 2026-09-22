"""LifecycleCommandsMixin — session lifecycle write commands for
:class:`SessionKernelService`.

create/delete/archive/terminate/end/recover + set-permission-mode,
the shared ``_run_lifecycle_command`` template, session-materialization
wait, runtime-subject rebuild polling, runtime-binding
reconciliation, and lifecycle-snapshot projection."""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import (
    datetime,
    timezone,
)
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.time_utils import parse_iso
from astrabox.common.utils.user_context import UserContext
from astrabox.core.model import SessionState
from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    needs_turn_recovery as _needs_recovery,
)
from astrabox.core.service.orchestrator.session_kernel.permission_lifecycle import (
    PermissionLifecycle,
)
from astrabox.core.service.orchestrator.engine.capabilities import (
    require_engine_for_session_kind,
)
from astrabox.core.service.orchestrator.session_kernel.service_mixins._helpers import (
    _RUNTIME_SUBJECT_REBUILD_READY_BUDGET_SECONDS,
    _RUNTIME_SUBJECT_REBUILD_READY_POLL_SECONDS,
)
from astrabox.core.service.orchestrator.session_kernel.workers import (
    SessionLifecycleWorker,
    WorkerWakeup,
)

# RecoverSession's two success outcomes (``_RecoverSessionMixin`` in
# workers/lifecycle/recover.py) are reattaching to the existing sandbox, or
# recreating a fresh one (status "startup-requested"). Both leave a snapshot
# worth read-repairing; only "manual-recovery-required" (operator intervention
# needed, nothing about the session was touched) is excluded.
_RECOVER_SESSION_RECONCILE_STATUSES = frozenset({"reattached", "startup-requested"})

logger = get_logger(__name__)


class LifecycleCommandsMixin:
    """Session lifecycle write commands for :class:`SessionKernelService`."""

    async def create_session(
        self,
        user: UserContext,
        template_name: str,
        *,
        permission_mode: str | None = None,
        session_kind: str = "agent_chat",
        workspace_ref: dict[str, Any] | None = None,
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
        # Validate that the Agent or Assistant runtime configuration resolves by
        # identity (agent_id / workspace_ref) before accepting the command.
        template = await self._session_service._agent_config.resolve_session_harness(
            {"workspace_ref": workspace_ref, "agent_id": agent_id, "owner_id": owner_id}
        )
        if template is None:
            raise APIError(
                code="TEMPLATE_NOT_ALLOWED",
                message=f"agent '{agent_id or template_name}' not found or not permitted",
                status_code=403,
            )
        normalized_idempotency_key = str(idempotency_key or "").strip() or None
        if session_id is not None and normalized_idempotency_key is not None:
            raise ValueError("session_id and idempotency_key are mutually exclusive")
        idempotency_subject = str(
            agent_id
            or owner_id
            or (workspace_ref or {}).get("agent_id")
            or (workspace_ref or {}).get("assistant_id")
            or template_name
        ).strip()
        resolved_session_id = str(
            session_id
            or (
                uuid.UUID(
                    hashlib.sha256(
                        (
                            "conversation-create\0"
                            f"{user.user_id}\0{idempotency_subject}\0"
                            f"{normalized_idempotency_key}"
                        ).encode("utf-8")
                    ).hexdigest()[:32]
                )
                if normalized_idempotency_key is not None
                else uuid.uuid4()
            )
        )
        if normalized_idempotency_key is not None:
            existing = await self._sessions_repo.get_session_including_deleted(
                resolved_session_id
            )
            if isinstance(existing, dict):
                same_owner = str(existing.get("user_id") or "") == str(user.user_id or "")
                existing_workspace = existing.get("workspace_ref")
                if not isinstance(existing_workspace, dict):
                    existing_workspace = {}
                existing_subject = str(
                    existing.get("agent_id")
                    or existing.get("owner_id")
                    or existing_workspace.get("agent_id")
                    or existing_workspace.get("assistant_id")
                    or existing.get("template_name")
                    or ""
                ).strip()
                same_subject = existing_subject == idempotency_subject
                if not same_owner or not same_subject:
                    raise APIError(
                        code="IDEMPOTENCY_KEY_CONFLICT",
                        message="Idempotency-Key is already bound to another conversation",
                        status_code=409,
                    )
                if bool(existing.get("deleted")):
                    raise APIError(
                        code="IDEMPOTENCY_KEY_RETIRED",
                        message="Idempotency-Key belongs to a deleted conversation",
                        status_code=409,
                    )
                return await self.get_session(user, resolved_session_id, session=existing)
        engine_kind = require_engine_for_session_kind(
            str(getattr(template, "engine_kind", "") or "").strip(),
            session_kind,
        )
        normalized_permission_mode = PermissionLifecycle.resolve_initial_mode(
            permission_mode,
            session_kind=session_kind,
            engine_kind=engine_kind,
        )
        command_id, _ = await self._append_command_accepted(
            user=user,
            session_id=resolved_session_id,
            command_type="CreateSession",
            payload={
                "template_name": template_name,
                "permission_mode": normalized_permission_mode,
                "session_kind": session_kind,
                "engine_kind": engine_kind,
                "workspace_ref": dict(workspace_ref) if workspace_ref else None,
                "hidden": bool(hidden),
                "owner_type": str(owner_type or "").strip() or None,
                "owner_id": str(owner_id or "").strip() or None,
                "source_type": str(source_type or "").strip() or None,
                "agent_id": str(agent_id or "").strip() or None,
                "deployment_name": str(deployment_name or "").strip() or None,
                "title": str(title or "").strip() or None,
            },
        )
        worker_task = self._spawn_background_task(
            self._build_lifecycle_worker().run(
                WorkerWakeup(
                    session_id=resolved_session_id,
                    channel="lifecycle",
                    command_id=command_id,
                )
            ),
            name=f"session-kernel-lifecycle-worker-{resolved_session_id}",
        )
        outcome = await worker_task
        metadata = outcome.metadata if isinstance(outcome.metadata, dict) else {}
        session = metadata.get("result") if isinstance(metadata.get("result"), dict) else None
        if not isinstance(session, dict):
            session = await self._wait_for_session_materialization(
                resolved_session_id,
                worker_task=worker_task,
            )
        startup_command_id = str(metadata.get("startup_command_id") or "").strip() or None
        response = await self.get_session(user, resolved_session_id, session=session)
        if startup_command_id:
            self._spawn_background_task(
                self._build_lifecycle_worker().run(
                    WorkerWakeup(
                        session_id=resolved_session_id,
                        channel="lifecycle",
                        command_id=startup_command_id,
                    )
                ),
                name=f"session-kernel-startup-worker-{resolved_session_id}",
            )
        return response

    async def update_session_permission_mode(
        self,
        user: UserContext,
        session_id: str,
        permission_mode: str,
    ) -> dict[str, Any]:
        session = await self._must_get_projection_backed_session(
            user,
            session_id,
            reconcile_conversation=False,
            internal_wiring=True,
        )
        self._require_turn_eligible(session, channel="lifecycle")
        command_id, _ = await self._append_command_accepted(
            user=user,
            session_id=session_id,
            command_type="SetPermissionMode",
            payload={"permission_mode": permission_mode},
        )
        outcome = await self._build_lifecycle_worker().run(
            WorkerWakeup(
                session_id=session_id,
                channel="lifecycle",
                command_id=command_id,
            )
        )
        result = outcome.metadata.get("result") if isinstance(outcome.metadata, dict) else None
        if isinstance(result, dict):
            return dict(result)
        return {
            "session_id": session_id,
            "permission_mode": permission_mode,
            "applied": False,
        }

    async def delete_session(self, user: UserContext, session_id: str) -> dict[str, Any]:
        return await self._run_lifecycle_command(
            user=user,
            session_id=session_id,
            command_type="DeleteSession",
            payload={},
            ensure_kwargs={
                "channel": "lifecycle",
                "reject_creating": False,
                "reject_terminated": False,
                "reject_deleted": False,
            },
            default_result={"session_id": session_id, "deleted": True},
        )

    async def archive_session(self, user: UserContext, session_id: str) -> dict[str, Any]:
        return await self._run_lifecycle_command(
            user=user,
            session_id=session_id,
            command_type="ArchiveSession",
            payload={},
            ensure_kwargs={
                "channel": "lifecycle",
                "reject_creating": False,
                "reject_terminated": False,
                "reject_deleted": True,
            },
            default_result={"session_id": session_id, "archived": True},
        )

    async def terminate_sandbox(self, user: UserContext, session_id: str) -> dict[str, Any]:
        return await self._run_lifecycle_command(
            user=user,
            session_id=session_id,
            command_type="TerminateSession",
            payload={},
            ensure_kwargs={
                "channel": "lifecycle",
                "reject_creating": False,
                "reject_terminated": False,
                "reject_deleted": True,
            },
            default_result={"session_id": session_id, "status": "terminated"},
            reconcile_agent_binding=False,
        )

    async def end_conversation(self, user: UserContext, session_id: str) -> dict[str, Any]:
        return await self._run_lifecycle_command(
            user=user,
            session_id=session_id,
            command_type="EndConversation",
            payload={},
            ensure_kwargs={
                "channel": "lifecycle",
                "reject_creating": False,
                "reject_terminated": False,
                "reject_deleted": True,
            },
            default_result={"session_id": session_id, "status": "conversation-ended"},
            reconcile_agent_binding=False,
        )

    async def recover_session(self, user: UserContext, session_id: str) -> dict[str, Any]:
        # Explicit lifecycle recovery must not consume projection read-repair
        # first; otherwise `_reconcile_stuck_turn()` may settle the
        # conversation snapshot before the lifecycle worker sees the original
        # degraded evidence and decides whether to reattach or recreate.
        session = await self._must_get_owned_session(user, session_id)
        # Admission: recovery cold-recreates the sandbox, so it is a sandbox
        # provisioning point too — gate it like create so a user cannot bypass
        # a per-user sandbox rate/quota by looping recover (no-op by default).
        from astrabox.seams.admission import (
            ADMISSION_KIND_SANDBOX,
            AdmissionRequest,
            enforce_admission,
        )

        await enforce_admission(
            AdmissionRequest(
                kind=ADMISSION_KIND_SANDBOX,
                user_id=str(getattr(user, "user_id", "") or ""),
                session_id=session_id,
                agent_id=str(session.get("agent_id") or "").strip() or None,
            )
        )
        session = await self._reconcile_runtime_binding(session)
        if session.get("state") == SessionState.CREATING.value and session.get("_runtime_recovery_owner"):
            return await self.get_session(user, session_id)
        self._require_turn_eligible(
            session,
            channel="lifecycle",
            reject_creating=True,
            reject_terminated=False,
            reject_deleted=True,
        )
        command_id, _ = await self._append_command_accepted(
            user=user,
            session_id=session_id,
            command_type="RecoverSession",
            payload={},
        )
        outcome = await self._build_lifecycle_worker().run(
            WorkerWakeup(
                session_id=session_id,
                channel="lifecycle",
                command_id=command_id,
            )
        )
        result = outcome.metadata.get("result") if isinstance(outcome.metadata, dict) else None
        # Both reattach and recreate (status "startup-requested") can leave a
        # snapshot worth read-repairing, so both success outcomes get the same
        # reconcile treatment here.
        if (
            isinstance(result, dict)
            and str(result.get("status") or "").strip() in _RECOVER_SESSION_RECONCILE_STATUSES
        ):
            snapshot = await self._session_snapshots_repo.get_snapshot(session_id)
            if isinstance(snapshot, dict) and _needs_recovery(snapshot):
                await self._reconcile_stuck_turn(
                    session_id=session_id,
                    session=session,
                    snapshot=snapshot,
                )
        startup_command_id = (
            str((result or {}).get("startup_command_id") or "").strip() or None
            if isinstance(result, dict)
            else None
        )
        response = await self.get_session(user, session_id)
        if startup_command_id:
            self._spawn_background_task(
                self._build_lifecycle_worker().run(
                    WorkerWakeup(
                        session_id=session_id,
                        channel="lifecycle",
                        command_id=startup_command_id,
                    )
                ),
                name=f"session-kernel-recover-startup-worker-{session_id}",
            )
        return response

    async def project_lifecycle_snapshot_from_session(
        self,
        *,
        session_id: str,
        event_seq: int,
        session: dict[str, Any],
        fallback_permission_mode: str | None,
    ) -> None:
        """Public kernel API: project a lifecycle snapshot from a session doc.

        Called by the platform-level bootstrap reconciliation sweep (a repo-wide
        startup concern, not a per-session kernel one) from outside the kernel,
        to keep the lifecycle projection in sync after it durably rewrites a
        stale session's state.
        """
        state = str((session or {}).get("state") or "").strip()
        permission_mode = str(
            (session or {}).get("permission_mode")
            or fallback_permission_mode
            or ""
        ).strip() or None
        updates: dict[str, Any] = {
            "session_lifecycle_state": SessionLifecycleWorker._derive_lifecycle_state(session),
            "runtime_connectivity_state": SessionLifecycleWorker._derive_runtime_connectivity_state(session),
            "permission_mode": permission_mode,
        }
        agent_id = str((session or {}).get("agent_id") or "").strip()
        if agent_id:
            updates["agent_binding"] = {
                "agent_id": agent_id,
                "session_kind": str((session or {}).get("session_kind") or "").strip() or None,
            }
        await self._session_snapshots_repo.apply_channel_update(
            session_id,
            channel="lifecycle",
            event_seq=event_seq,
            updates=updates,
        )

    async def _converge_lifecycle_projection(
        self,
        *,
        session: dict[str, Any] | None,
        snapshot: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Republish a lifecycle outcome the session row holds and the projection missed.

        Startup settles in two writes: the fenced ``sessions`` row first, then
        the journal event and the lifecycle projection that every read derives
        state from (``workers/lifecycle/startup.py``). A process that dies
        between them leaves a row that has left CREATING behind a projection
        that still says CREATING, and the conversation stays CREATING for good:
        ``_require_turn_eligible`` refuses input as "still creating",
        ``_derive_effective_session_state_for_recover`` reads the same
        projection and refuses recovery as well, and
        :class:`~astrabox.core.service.orchestrator.bootstrap_reconciler.BootstrapReconciler`
        selects only rows whose own state is CREATING. Every startup path ends
        in that pair — cold create, prepared-slot claim, attach and recovery —
        so this converges all of them.

        The lifecycle channel is a pure function of the row
        (``SessionLifecycleWorker._derive_lifecycle_state``), so republishing it
        is derivation, not repair by guesswork. The journal append is the
        ordering point: the row is read after it, so a lifecycle write racing
        this one appends later, projects at a higher watermark and wins. The
        caller's ``session`` only triggers the check and names that observation
        in the journal; the published state is read back from the database, so
        the read path's in-memory binding overlay never becomes stored
        lifecycle state.
        """
        if not isinstance(snapshot, dict) or not isinstance(session, dict):
            return snapshot
        projected_state = str(snapshot.get("session_lifecycle_state") or "").strip()
        if projected_state != "CREATING":
            return snapshot
        if SessionLifecycleWorker._derive_lifecycle_state(session) == "CREATING":
            return snapshot
        session_id = str(session.get("session_id") or "").strip()
        if not session_id:
            return snapshot

        reason = "unpublished_startup_outcome"
        event = await self._session_events_repo.append_event(
            {
                "session_id": session_id,
                "channel": "lifecycle",
                "event_type": "session.lifecycle_reconciled",
                "causation_id": f"lifecycle:{session_id}:{reason}",
                "correlation_id": f"lifecycle:{session_id}:{reason}",
                "payload": {
                    "reason": reason,
                    "previous_state": projected_state,
                    "observed_session_state": str(session.get("state") or "").strip() or None,
                },
            }
        )
        durable = await self._sessions_repo.get_session(session_id)
        if not isinstance(durable, dict):
            return snapshot
        logger.warning(
            "lifecycle projection republished from its session row: "
            "session=%s row_state=%s projected_state=%s",
            session_id,
            str(durable.get("state") or "").strip() or None,
            projected_state,
        )
        await self.project_lifecycle_snapshot_from_session(
            session_id=session_id,
            event_seq=int(event.get("event_seq") or 0),
            session=durable,
            fallback_permission_mode=(
                str(session.get("permission_mode") or "").strip() or None
            ),
        )
        latest = await self._session_snapshots_repo.get_snapshot(session_id)
        return latest if isinstance(latest, dict) else snapshot

    async def _run_lifecycle_command(
        self,
        *,
        user: UserContext,
        session_id: str,
        command_type: str,
        payload: dict[str, Any],
        ensure_kwargs: dict[str, Any],
        default_result: dict[str, Any],
        reconcile_agent_binding: bool = True,
    ) -> dict[str, Any]:
        session = await self._must_get_projection_backed_session(
            user,
            session_id,
            reconcile_conversation=False,
            reconcile_agent_binding=reconcile_agent_binding,
            internal_wiring=True,
        )
        self._require_turn_eligible(session, **ensure_kwargs)
        command_id, _ = await self._append_command_accepted(
            user=user,
            session_id=session_id,
            command_type=command_type,
            payload=payload,
        )
        outcome = await self._build_lifecycle_worker().run(
            WorkerWakeup(
                session_id=session_id,
                channel="lifecycle",
                command_id=command_id,
            )
        )
        result = outcome.metadata.get("result") if isinstance(outcome.metadata, dict) else None
        return dict(result) if isinstance(result, dict) else dict(default_result)

    async def _wait_for_session_materialization(
        self,
        session_id: str,
        *,
        worker_task: asyncio.Task,
    ) -> dict[str, Any]:
        for _ in range(40):
            session = await self._sessions_repo.get_session(session_id)
            if isinstance(session, dict):
                return session
            if worker_task.done():
                await worker_task
                break
            await asyncio.sleep(0.05)

        session = await self._sessions_repo.get_session(session_id)
        if isinstance(session, dict):
            return session
        if worker_task.done():
            await worker_task
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message="create session failed: lifecycle worker did not materialize session",
            status_code=502,
        )

    @staticmethod
    def _bound_runtime_lease_expired(session: dict[str, Any]) -> bool:
        """True when this conversation's bound sandbox lease has lapsed.

        The stored lease can lag the supplier. Expiry triggers recovery's live
        probe; it does not prove that the sandbox backend reclaimed the instance.
        Only an actually-past, parseable expiry on a
        still-bound sandbox counts; a missing/unparseable expiry or no sandbox_id is
        never treated as expired (the missing-sandbox path handles those).
        """
        if not str(session.get("sandbox_id") or "").strip():
            return False
        raw = session.get("expires_at")
        if not raw:
            return False
        try:
            exp = parse_iso(str(raw))
        except (ValueError, TypeError):
            return False
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp <= datetime.now(timezone.utc)

    async def _await_runtime_subject_rebuild_ready(
        self, user: UserContext, session_id: str
    ) -> dict[str, Any]:
        """Poll a recovering Session until its runtime subject is dispatchable.

        recover_session recreates asynchronously, so the conversation message
        path must wait for the fresh sandbox to materialize before dispatching
        the turn. Bounded by the cold-rebuild budget; a non-CREATING terminal
        state (rebuild failed) returns immediately so the turn guard reports it.
        """
        session = await self._must_get_projection_backed_session(
            user, session_id, reconcile_conversation=False
        )
        deadline = self._loop_time() + _RUNTIME_SUBJECT_REBUILD_READY_BUDGET_SECONDS

        def _still_rebuilding(s: dict[str, Any]) -> bool:
            state = str(s.get("state") or "")
            if state in (SessionState.TERMINATED.value, SessionState.DELETED.value):
                return False  # terminal rebuild failure — let the turn guard report it
            # Wait while the rebuild is in flight. After a sandbox reclaim the session
            # starts at READY+runtime_unavailable (not CREATING), so this must also
            # treat "runtime still unavailable" as in-flight: the async rebuild flips
            # CREATING -> READY and clears runtime_unavailable only once the fresh
            # sandbox is dispatchable.
            if state == SessionState.CREATING.value:
                return True
            return bool(s.get("runtime_unavailable"))

        while _still_rebuilding(session):
            if self._loop_time() >= deadline:
                break
            await asyncio.sleep(_RUNTIME_SUBJECT_REBUILD_READY_POLL_SECONDS)
            session = await self._must_get_projection_backed_session(
                user, session_id, reconcile_conversation=False
            )
        return session
