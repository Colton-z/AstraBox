"""ReconciliationOrchestrationMixin — the stuck-turn convergence orchestrator
for :class:`SessionKernelService`.

``_reconcile_stuck_turn`` (a thin tier dispatcher) plus its tier helpers —
journal-terminal replay, orphaned AnswerInteraction replay, stale-active
TurnCoordinator wakeup/resolve, and the stale-turn
adjudication tiers. Decides Tier-2
(active-state) convergence and fans out to the recovery mixins."""
from __future__ import annotations

import asyncio
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.core.service.orchestrator.session_kernel.active_turn_projection import (
    build_active_turn_message,
)
from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    AI_SDK_FINISH_REASON_STOP,
    build_turn_terminal_snapshot_updates,
    coerce_int as _coerce_int,
    journal_terminal_for_turn,
    is_terminal_data_result_frame,
    needs_turn_recovery as _needs_recovery,
    normalize_current_turn_engine_anchor as _normalize_current_turn_engine_anchor,
    normalize_current_turn_remote_anchor as _normalize_current_turn_remote_anchor,
    normalize_turn_terminal_frame,
    settle_parked_turn,
)
from astrabox.core.service.orchestrator.session_kernel.engine_emission_projection import (
    project_engine_interaction_opened,
)
from astrabox.core.service.orchestrator.session_kernel.workers import (
    TurnCoordinator,
    WorkerWakeup,
)
from astrabox.core.service.orchestrator.session_kernel.workers.reconcile_worker import (
    DeadSandboxRecoverySettlement,
)
from astrabox.core.service.orchestrator.session_kernel.service_mixins._helpers import (
    _ACTIVE_CONVERSATION_SNAPSHOT_STATES,
    _ORPHANED_ANSWER_REPLAY_GRACE_SECONDS,
    _STALE_NO_ANCHOR_THRESHOLD_SECONDS,
    _has_authoritative_terminal_conversation_state,
    _is_snapshot_stale,
    _journal_event_age_seconds,
)


logger = get_logger(__name__)


class ReconciliationOrchestrationMixin:
    """Convergence orchestrator for stuck turns, mixed into
    :class:`SessionKernelService`."""

    async def _recover_stuck_turn(
        self,
        *,
        session: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> dict[str, Any] | DeadSandboxRecoverySettlement | None:
        """Repair a journaled terminal before asking an engine to resume work."""
        session_id = str(session.get("session_id") or "").strip()
        turn_id = str(snapshot.get("current_turn_id") or "").strip()
        terminal_event = await journal_terminal_for_turn(
            self._session_events_repo, session_id=session_id, turn_id=turn_id,
        )
        if isinstance(terminal_event, dict) and terminal_event.get("event_type") in {
            "turn.completed", "turn.failed",
        }:
            repaired = await self._replay_projection_from_journal_terminal(
                session_id=session_id, session=session, snapshot=snapshot,
                turn_id=turn_id, terminal_event=terminal_event,
            )
            repaired_state = str((repaired or {}).get("conversation_state") or "")
            if repaired_state in _ACTIVE_CONVERSATION_SNAPSHOT_STATES:
                return None
            return repaired
        return await self._recover_engine_via_anchor(session=session, snapshot=snapshot)

    def _wakeup_turn_coordinator(self, session_id: str) -> None:
        """Fire-and-forget: schedule background recovery for an unresolved turn.

        Deduplicates by session_id within this process. Cross-process
        deduplication is handled by ``try_claim_event(turn.recovery_claimed)``
        inside TurnCoordinator.
        """
        active = self._recovery_tasks.get(session_id)
        if isinstance(active, asyncio.Task) and not active.done():
            return
        task = self._spawn_background_task(
            self._run_turn_coordinator(session_id),
            name=f"turn-coordinator-{session_id}",
        )
        self._recovery_tasks[session_id] = task

    async def _run_turn_coordinator(self, session_id: str) -> None:
        try:
            snapshot = await self._session_snapshots_repo.get_snapshot(session_id)
            if not isinstance(snapshot, dict):
                return
            session = await self._sessions_repo.get_session(session_id)
            if not isinstance(session, dict):
                # The row is invisible because the user deleted the session —
                # which is precisely when its orphaned turn has nobody else.
                # Deletion tears the sandbox down, so it is the durable
                # "compute is gone" verdict: the stub says so, the coordinator
                # recovers the turn from the mirror when the mirror is
                # complete and settles it unrecoverable when it is not, and
                # the lifecycle channel (DELETED) is untouched either way.
                if not self._is_unresolved_turn(snapshot):
                    return
                session = {
                    "session_id": session_id,
                    "deleted": True,
                    "runtime_unavailable": True,
                }
            await self._turn_coordinator.try_resolve_turn(session_id, session, snapshot)
        except Exception:
            logger.exception(
                "turn coordinator: background resolve failed session=%s", session_id,
            )
        finally:
            self._recovery_tasks.pop(session_id, None)

    @staticmethod
    def _is_unresolved_turn(snapshot: dict[str, Any] | None) -> bool:
        return TurnCoordinator._get_unresolved_turn_id(snapshot or {}) is not None

    async def _attach_parked_runtime(self, session_id: str) -> None:
        """Keep a live approval attached; settle it when its box is gone.

        The box asks, then blocks its PreToolUse hook holding the answer slot,
        and gives up once the host has been absent longer than its budget. A
        platform restart can outlast that budget, so without this the approval
        expires while the session sits perfectly healthy — the reconciler's
        parked fence is right to refuse recovery here, and refusing recovery is
        not the same as staying away.
        """
        session = await self._sessions_repo.get_session(session_id)
        if not isinstance(session, dict):
            return
        if session.get("runtime_unavailable") and not session.get("sandbox_id"):
            # Death convergence already removed the confirmed-dead binding.
            # The parked worker has exited, so it cannot retire its approval.
            # Use the same settlement as explicit reclaim, not anchor recovery
            # or a fabricated answer to the supplier's old request.
            snapshot = await self._session_snapshots_repo.get_snapshot(session_id)
            turn_id = str((snapshot or {}).get("current_turn_id") or "").strip()
            if (
                not turn_id
                or (snapshot or {}).get("conversation_state")
                != "WAITING_FOR_INTERACTION"
            ):
                return
            interaction_id = str(
                (snapshot or {}).get("active_interaction_id") or ""
            ).strip()
            command_id = await self._turn_coordinator._resolve_recovery_command_id(
                session_id=session_id, turn_id=turn_id, snapshot=snapshot,
            )
            await settle_parked_turn(
                session_events_repo=self._session_events_repo,
                session_snapshots_repo=self._session_snapshots_repo,
                interaction_snapshots_repo=self._interaction_snapshots_repo,
                session_id=session_id,
                turn_id=turn_id,
                command_id=command_id,
                status="FAILED",
                failure_phase="sandbox_reclaimed",
                error_text="sandbox unavailable while awaiting interaction",
                causation=f"reclaim-settle:{session_id}:{turn_id}",
            )
            if interaction_id:
                await self._sessions_repo.clear_pending_interaction(
                    session_id, interaction_id=interaction_id,
                )
            return
        if self._runtime_manager.get_runtime(session_id) is not None:
            return
        ensure_runtime = getattr(
            self._turn_service, "_ensure_runtime_lightweight_for_session", None
        )
        if not callable(ensure_runtime):
            return
        runtime = await ensure_runtime(session)
        if runtime is not None:
            logger.info(
                "reconcile: re-attached a parked session's runtime session=%s",
                session_id,
            )

    async def _resolve_pending_turn_for_resume(
        self,
        *,
        session_id: str,
        session: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        if not _needs_recovery(snapshot):
            return snapshot, False
        turn_id = str(snapshot.get("last_turn_id") or "").strip()
        if not turn_id:
            return snapshot, False

        try:
            result = await self._turn_coordinator.try_resolve_turn(
                session_id, session, snapshot,
            )
        except Exception:
            logger.exception(
                "resume pending turn recovery failed session=%s turn=%s",
                session_id,
                turn_id,
            )
            raise
        latest = result
        if not isinstance(latest, dict):
            latest_snapshot = await self._session_snapshots_repo.get_snapshot(session_id)
            latest = latest_snapshot if isinstance(latest_snapshot, dict) else snapshot

        terminalized = TurnCoordinator._snapshot_has_completed_terminal_proof(
            latest, turn_id,
        )
        return latest, terminalized

    async def _resume_orphaned_answer_command(
        self,
        *,
        session_id: str,
        session: dict[str, Any],
        snapshot: dict[str, Any] | None,
    ) -> bool:
        """Finish AnswerInteraction journal projection before pending adjudication.

        Command journal order is the protocol boundary here: once an
        AnswerInteraction has ``dispatch.confirmed``, the sandbox has accepted
        the answer and the interaction is not pending.  Reconcile must
        complete the missing ``interaction.answer_persisted`` projection before
        any later pending/recovery decision can trust ``active=true``.
        """
        if not isinstance(session, dict):
            return False
        current_snapshot = await self._session_snapshots_repo.get_snapshot(session_id)
        if isinstance(current_snapshot, dict):
            snapshot = current_snapshot
        if not isinstance(snapshot, dict):
            return False
        conversation_state = str(snapshot.get("conversation_state") or "").strip()
        recovery_pending = _needs_recovery(snapshot)
        if (
            conversation_state not in _ACTIVE_CONVERSATION_SNAPSHOT_STATES
            and not recovery_pending
        ):
            return False

        turn_id = str(
            snapshot.get("current_turn_id")
            or (snapshot.get("last_turn_id") if recovery_pending else "")
            or ""
        ).strip()
        if not turn_id:
            return False

        active_interaction = await self._interaction_snapshots_repo.get_active_interaction(session_id)
        if not isinstance(active_interaction, dict):
            return False
        interaction_id = str(active_interaction.get("interaction_id") or "").strip()
        if not interaction_id:
            return False
        if str(active_interaction.get("turn_id") or "").strip() != turn_id:
            return False

        command_events = await self._session_events_repo.list_events(
            session_id,
            after_seq=0,
            channel="command",
            turn_id=turn_id,
            event_type="command.accepted",
            limit=50,
        )
        candidates: list[dict[str, Any]] = []
        for event in reversed(command_events):
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            if str(payload.get("command_type") or "").strip() != "AnswerInteraction":
                continue
            candidate_interaction_id = str(
                payload.get("interaction_id")
                or (payload.get("interaction_response") or {}).get("interaction_id")
                or ""
            ).strip()
            if candidate_interaction_id != interaction_id:
                continue
            candidates.append(event)
        if not candidates:
            return False

        answer_already_persisted = False
        latest_candidate = candidates[0]
        latest_command_id = str(latest_candidate.get("causation_id") or "").strip()
        for candidate in candidates:
            command_id = str(candidate.get("causation_id") or "").strip()
            if not command_id:
                continue
            after_seq = max(int(candidate.get("event_seq") or 0) - 1, 0)
            answer_events = await self._session_events_repo.list_events(
                session_id,
                after_seq=after_seq,
                event_type="interaction.answer_persisted",
                causation_id=command_id,
                limit=1,
            )
            if answer_events:
                answer_already_persisted = True
                continue
            dispatch_events = await self._session_events_repo.list_events(
                session_id,
                after_seq=after_seq,
                event_type="dispatch.confirmed",
                causation_id=command_id,
                limit=1,
            )
            if dispatch_events:
                logger.warning(
                    "projecting answered interaction after dispatch confirmation catch-up session=%s turn=%s interaction=%s command_id=%s",
                    session_id,
                    turn_id,
                    interaction_id,
                    command_id,
                )
                await self._build_turn_worker()._project_answer_persisted(
                    session_id=session_id,
                    turn_id=turn_id,
                    command_event=candidate,
                    payload=dict(candidate.get("payload") or {}),
                )
                if recovery_pending:
                    self._wakeup_turn_coordinator(session_id)
                return True
        if answer_already_persisted:
            return False

        if conversation_state != "WAITING_FOR_INTERACTION":
            return False

        if not latest_command_id:
            return False

        command_age_s = _journal_event_age_seconds(latest_candidate)
        if (
            command_age_s is not None
            and command_age_s < _ORPHANED_ANSWER_REPLAY_GRACE_SECONDS
        ):
            logger.info(
                "skip orphaned answer replay for fresh command session=%s turn=%s interaction=%s command_id=%s age_s=%.2f",
                session_id,
                turn_id,
                interaction_id,
                latest_command_id,
                command_age_s,
            )
            return False

        logger.warning(
            "replaying orphaned answer command session=%s turn=%s interaction=%s command_id=%s",
            session_id,
            turn_id,
            interaction_id,
            latest_command_id,
        )
        self._spawn_background_task(
            self._build_turn_worker().run(
                WorkerWakeup(
                    session_id=session_id,
                    channel="conversation",
                    command_id=latest_command_id,
                    turn_id=turn_id,
                    reason="reconcile_orphaned_answer_command",
                )
            ),
            name=f"session-kernel-answer-replay-{session_id}",
        )
        return True

    async def _replay_projection_from_journal_terminal(
        self,
        *,
        session_id: str,
        session: dict[str, Any],
        snapshot: dict[str, Any],
        turn_id: str,
        terminal_event: dict[str, Any],
    ) -> dict[str, Any] | None:
        event_type = str(terminal_event.get("event_type") or "").strip()
        event_seq = int(terminal_event.get("event_seq") or 0)
        payload = terminal_event.get("payload") or {}
        if event_seq <= 0 or not isinstance(payload, dict):
            return None

        command_id = (
            str(payload.get("command_id") or "").strip()
            or str(terminal_event.get("causation_id") or "").strip()
            or None
        )
        if event_type == "turn.completed":
            status = "COMPLETED"
            error_text = None
            terminal_frame = await self._find_existing_turn_terminal_frame(
                session_id=session_id,
                turn_id=turn_id,
                command_id=command_id,
                expected_type="finish",
                expected_finish_reason=AI_SDK_FINISH_REASON_STOP,
            )
        elif event_type == "turn.failed":
            status = "FAILED"
            error_text = str(payload.get("error_text") or "").strip() or None
            terminal_frame = await self._find_existing_turn_terminal_frame(
                session_id=session_id,
                turn_id=turn_id,
                command_id=command_id,
            )
        else:
            return None

        result = await self._session_snapshots_repo.apply_channel_update(
            session_id,
            channel="conversation",
            event_seq=event_seq,
            updates=build_turn_terminal_snapshot_updates(
                turn_id=turn_id,
                status=status,
                error_text=error_text,
                command_id=command_id,
                failure_phase=str(payload.get("failure_phase") or "").strip() or None,
                terminal_frame=terminal_frame,
            ),
            expected_conversation_state=(
                str(snapshot.get("conversation_state") or "").strip() or None
            ),
        )
        if isinstance(result, dict):
            logger.info(
                "reconcile replayed terminal journal projection session=%s turn=%s event=%s seq=%d",
                session_id,
                turn_id,
                event_type,
                event_seq,
            )
            return result
        latest = await self._session_snapshots_repo.get_snapshot(session_id)
        return latest if isinstance(latest, dict) else snapshot

    async def _reconcile_stuck_turn(
        self,
        *,
        session_id: str,
        session: dict[str, Any],
        snapshot: dict[str, Any] | None,
        worker_heartbeat_stale: bool = False,
    ) -> dict[str, Any] | None:
        if not isinstance(snapshot, dict):
            return snapshot
        conversation_state = str(snapshot.get("conversation_state") or "").strip()

        # A transcript-pending turn already left the live-writer state and has
        # one designated resolver: TurnCoordinator. Resolve it synchronously
        # on the write/read-repair path before applying the ordinary active
        # turn ladder. The periodic reconciler remains the crash backstop, not
        # a ten-second prerequisite for the next user input.
        #
        # No remote anchor is required. A bridge can die before observing its
        # first mirror coordinate; dispatch.confirmed still carries the
        # turn-local sandbox and the engine adapter slices the durable mirror
        # from the accepted input. Requiring an anchor here would make a state
        # produced with force_transcript_pending invisible to its resolver.
        if _needs_recovery(snapshot):
            resolved = await self._turn_coordinator.try_resolve_turn(
                session_id,
                session,
                snapshot,
            )
            if isinstance(resolved, dict):
                return resolved
            latest = await self._session_snapshots_repo.get_snapshot(session_id)
            return latest if isinstance(latest, dict) else snapshot

        # Tier 2: active conversation state (PROCESSING/STREAMING/WAITING_FOR_INTERACTION)
        # Worker may still be running or may have died.
        if conversation_state not in _ACTIVE_CONVERSATION_SNAPSHOT_STATES:
            return snapshot
        turn_id = str(snapshot.get("current_turn_id") or "").strip()
        if not turn_id:
            return snapshot

        terminal_event = await journal_terminal_for_turn(
            self._session_events_repo,
            session_id=session_id,
            turn_id=turn_id,
        )
        if isinstance(terminal_event, dict):
            result = await self._replay_projection_from_journal_terminal(
                session_id=session_id,
                session=session,
                snapshot=snapshot,
                turn_id=turn_id,
                terminal_event=terminal_event,
            )
            result_state = str((result or {}).get("conversation_state") or "").strip()
            if result_state not in _ACTIVE_CONVERSATION_SNAPSHOT_STATES:
                return result
            # Replay failed to converge (event_seq stale). Fall through to
            # the remaining tiers below.

        _awaiting = await self._reconcile_awaiting_interaction_replay(
            session_id=session_id,
            session=session,
            snapshot=snapshot,
            conversation_state=conversation_state,
            turn_id=turn_id,
        )
        if _awaiting is not None:
            return _awaiting

        _waiting = await self._reconcile_waiting_message_repair(
            session_id=session_id,
            session=session,
            snapshot=snapshot,
            conversation_state=conversation_state,
            turn_id=turn_id,
        )
        if _waiting is not None:
            return _waiting

        _no_anchor = await self._adjudicate_no_anchor_delivery(
            session_id=session_id,
            session=session,
            snapshot=snapshot,
            conversation_state=conversation_state,
            turn_id=turn_id,
        )
        if _no_anchor is not None:
            return _no_anchor

        return await self._reconcile_has_anchor_stale(
            session_id=session_id,
            session=session,
            snapshot=snapshot,
            conversation_state=conversation_state,
            turn_id=turn_id,
            worker_heartbeat_stale=worker_heartbeat_stale,
        )

    async def _reconcile_awaiting_interaction_replay(
        self,
        *,
        session_id: str,
        session: dict[str, Any],
        snapshot: dict[str, Any],
        conversation_state: str,
        turn_id: str,
    ) -> dict[str, Any] | None:
        # Tier 2a: PROCESSING/STREAMING with turn.awaiting_interaction in journal.
        # Worker died after committing the authority event but before projections.
        # The terminalizer fence blocked turn.failed, so snapshot is stuck.
        # Replay projections from the event payload to converge.
        if conversation_state in ("PROCESSING", "STREAMING"):
            awaiting_events = await self._session_events_repo.list_events(
                session_id,
                channel="conversation",
                turn_id=turn_id,
                event_type="turn.awaiting_interaction",
            )
            # A single turn can open multiple interactions across resume
            # cycles.  Scan from newest to oldest and replay the latest
            # one whose interaction is still unresolved.
            awaiting_event = None
            for _candidate in reversed(awaiting_events):
                _c_payload = _candidate.get("payload") or {}
                _c_iid = str(_c_payload.get("interaction_id") or "").strip()
                if not _c_iid:
                    continue
                _c_existing = await self._interaction_snapshots_repo.get_interaction(
                    session_id, _c_iid
                )
                _c_resolved = (
                    isinstance(_c_existing, dict)
                    and (
                        str(_c_existing.get("interaction_state") or "") == "ANSWERED"
                        or not _c_existing.get("active")
                    )
                )
                if not _c_resolved:
                    awaiting_event = _candidate
                    break
            if awaiting_event is not None:
                event_seq = int(awaiting_event.get("event_seq") or 0)
                payload = awaiting_event.get("payload") or {}
                interaction_id = str(payload.get("interaction_id") or "").strip()
                if interaction_id and event_seq:
                    logger.info(
                        "tier2 journal replay: turn.awaiting_interaction found for "
                        "PROCESSING/STREAMING session=%s turn=%s interaction=%s",
                        session_id, turn_id, interaction_id,
                    )
                    projection = await project_engine_interaction_opened(
                        session_events_repo=self._session_events_repo,
                        session_snapshots_repo=self._session_snapshots_repo,
                        interaction_snapshots_repo=self._interaction_snapshots_repo,
                        session_id=session_id,
                        turn_id=turn_id,
                        command_id=str(payload.get("command_id") or "").strip(),
                        correlation_id=str(
                            awaiting_event.get("correlation_id") or ""
                        ).strip(),
                        pending={
                            key: value
                            for key, value in payload.items()
                            if key != "command_id"
                        },
                        tool_name=str(payload.get("tool_name") or "").strip(),
                        source_event_seq_applied=event_seq,
                        current_turn_remote_anchor=(
                            snapshot.get("current_turn_remote_anchor")
                            if isinstance(
                                snapshot.get("current_turn_remote_anchor"), dict
                            )
                            else None
                        ),
                        current_turn_engine_anchor=(
                            snapshot.get("current_turn_engine_anchor")
                            if isinstance(
                                snapshot.get("current_turn_engine_anchor"), dict
                            )
                            else None
                        ),
                    )
                    if projection.waiting:
                        return projection.snapshot or snapshot
                    return snapshot
        return None

    async def _reconcile_waiting_message_repair(
        self,
        *,
        session_id: str,
        session: dict[str, Any],
        snapshot: dict[str, Any],
        conversation_state: str,
        turn_id: str,
    ) -> dict[str, Any] | None:
        if conversation_state == "WAITING_FOR_INTERACTION":
            active_interaction_id = str(snapshot.get("active_interaction_id") or "").strip()
            if active_interaction_id:
                interaction = await self._interaction_snapshots_repo.get_interaction(
                    session_id,
                    active_interaction_id,
                )
                if (
                    isinstance(interaction, dict)
                    and bool(interaction.get("active"))
                    and str(interaction.get("turn_id") or "").strip() == turn_id
                ):
                    return snapshot
            active_interaction = await self._interaction_snapshots_repo.get_active_interaction(
                session_id
            )
            if (
                isinstance(active_interaction, dict)
                and str(active_interaction.get("turn_id") or "").strip() == turn_id
            ):
                return snapshot
        return None

    async def _adjudicate_no_anchor_delivery(
        self,
        *,
        session_id: str,
        session: dict[str, Any],
        snapshot: dict[str, Any],
        conversation_state: str,
        turn_id: str,
    ) -> dict[str, Any] | None:
        # No anchor means the worker never talked to the engine (pre-write
        # only). The snapshot age is the sole death observation here. Workers
        # fence writes by turn id; consulting a separate lease would introduce
        # a second, contradictory ownership authority.
        no_anchor_dead = _is_snapshot_stale(
            snapshot,
            threshold_seconds=_STALE_NO_ANCHOR_THRESHOLD_SECONDS,
        )
        # Age is not the only death observation. ``runtime_unavailable`` carries
        # control-plane evidence that the box is absent; when the turn
        # also has no engine anchor, no worker can still write it. Settle that
        # state immediately instead of waiting for the age threshold.
        if not no_anchor_dead and bool(session.get("runtime_unavailable")):
            no_anchor_dead = (
                _normalize_current_turn_engine_anchor(
                    snapshot.get("current_turn_engine_anchor")
                )
                is None
            )
        if no_anchor_dead:
            # Delivery adjudication: exhaust available evidence before
            # declaring NOT_RECEIVED.
            # 1) Check journal for dispatch.confirmed
            _dispatch_events = await self._session_events_repo.list_events(
                session_id,
                after_seq=0,
                channel="conversation",
                turn_id=turn_id,
                event_type="dispatch.confirmed",
                limit=1,
            )
            _has_dispatch = bool(_dispatch_events)

            # 2) Check canonical engine-frame events for any evidence.
            _turn_frames = await self._session_events_repo.list_frames(
                session_id, turn_id=turn_id, after_seq=-1,
            )
            _has_frames = bool(_turn_frames)

            if _has_dispatch or _has_frames:
                _rf = await self._adjudicate_received_failed(
                    session_id=session_id,
                    session=session,
                    conversation_state=conversation_state,
                    turn_id=turn_id,
                    has_dispatch=_has_dispatch,
                    has_frames=_has_frames,
                    turn_frames=_turn_frames,
                )
                if _rf is not None:
                    return _rf
            else:
                _nr = await self._adjudicate_not_received(
                    session_id=session_id,
                    conversation_state=conversation_state,
                    turn_id=turn_id,
                )
                if _nr is not None:
                    return _nr
        return snapshot
        return None

    async def _adjudicate_received_failed(
        self,
        *,
        session_id: str,
        session: dict[str, Any],
        conversation_state: str,
        turn_id: str,
        has_dispatch: bool,
        has_frames: bool,
        turn_frames: Any,
    ) -> dict[str, Any] | None:
        _has_dispatch = has_dispatch
        _has_frames = has_frames
        _turn_frames = turn_frames
        # Sandbox did receive (or at least process) this turn,
        # but the worker died before establishing a remote anchor.
        # Treat as RECEIVED + FAILED.
        await self._session_events_repo.try_claim_event(
            {
                "session_id": session_id,
                "channel": "conversation",
                "turn_id": turn_id,
                "event_type": "turn.stale_detected",
                "causation_id": f"stale:{session_id}:{turn_id}",
                "correlation_id": f"stale:{session_id}:{turn_id}",
                "payload": {
                    "detected_state": conversation_state,
                    "frame_count": len(_turn_frames) if isinstance(_turn_frames, list) else 0,
                    "reason": "no_anchor_but_evidence_found",
                    "has_dispatch_confirmed": _has_dispatch,
                    "has_frames": _has_frames,
                },
            }
        )
        _stale_error = "turn stale: sandbox evidence found but worker lost anchor"
        if _has_frames and isinstance(_turn_frames, list) and _turn_frames:
            _projected = build_active_turn_message(
                session_id=session_id,
                turn_id=turn_id,
                message_id=turn_id,
                frames=_turn_frames,
                existing_message=None,
                default_message_seq=0,
            )
            _base_blocks = list((_projected or {}).get("blocks") or [])
            _base_content = str((_projected or {}).get("content") or "")
        else:
            _base_blocks = []
            _base_content = ""
        failed_event, _created = await self._session_events_repo.try_claim_event(
            {
                "session_id": session_id,
                "channel": "conversation",
                "turn_id": turn_id,
                "event_type": "turn.failed",
                "causation_id": f"stale-failed:{session_id}:{turn_id}",
                "correlation_id": f"stale:{session_id}:{turn_id}",
                "payload": {
                    "assistant_text": _base_content or None,
                    "blocks": _base_blocks,
                    "error_text": _stale_error,
                    "failure_phase": "post_dispatch",
                    "reason": "no_anchor_but_evidence_found",
                },
            }
        )
        _failed_event_seq = int(failed_event.get("event_seq") or 0)
        result = await self._session_snapshots_repo.apply_channel_update(
            session_id,
            channel="conversation",
            event_seq=_failed_event_seq,
            updates=build_turn_terminal_snapshot_updates(
                turn_id=turn_id,
                status="FAILED",
                error_text=_stale_error,
                command_id=None,
                delivery_state="RECEIVED",
                failure_phase="post_dispatch",
            ),
            expected_conversation_state=conversation_state,
        )
        if isinstance(result, dict):
            logger.info(
                "reconcile no-anchor stale with evidence: session=%s turn=%s → RECEIVED+FAILED",
                session_id, turn_id,
            )
            return result
        return None

    async def _adjudicate_not_received(
        self,
        *,
        session_id: str,
        conversation_state: str,
        turn_id: str,
    ) -> dict[str, Any] | None:
        _accepted_commands = await self._session_events_repo.list_events(
            session_id,
            after_seq=0,
            channel="command",
            turn_id=turn_id,
            event_type="command.accepted",
            limit=1,
        )
        _accepted_command = (
            _accepted_commands[-1] if isinstance(_accepted_commands, list) and _accepted_commands else None
        )
        _accepted_command_id = str(
            (_accepted_command or {}).get("causation_id") or ""
        ).strip() or None
        # No evidence of sandbox receipt at all.
        # Adjudication: NOT_RECEIVED.
        event = await self._session_events_repo.append_event(
            {
                "session_id": session_id,
                "channel": "conversation",
                "turn_id": turn_id,
                "event_type": "turn.stale_detected",
                "causation_id": f"stale:{session_id}:{turn_id}",
                "correlation_id": f"stale:{session_id}:{turn_id}",
                "payload": {
                    "detected_state": conversation_state,
                    "frame_count": 0,
                    "reason": "no_anchor",
                },
            }
        )
        if _accepted_command_id:
            _stale_error = "turn failed before dispatch"
            failed_event = await self._session_events_repo.append_event(
                {
                    "session_id": session_id,
                    "channel": "conversation",
                    "turn_id": turn_id,
                    "event_type": "turn.failed",
                    "causation_id": _accepted_command_id,
                    "correlation_id": _accepted_command_id,
                    "payload": {
                        "command_id": _accepted_command_id,
                        "final_state": "FAILED",
                        "assistant_text": None,
                        "error_text": _stale_error,
                        "failure_phase": "pre_dispatch",
                    },
                }
            )
            result = await self._session_snapshots_repo.apply_channel_update(
                session_id,
                channel="conversation",
                event_seq=int(failed_event.get("event_seq") or 0),
                updates=build_turn_terminal_snapshot_updates(
                    turn_id=turn_id,
                    status="FAILED",
                    error_text=_stale_error,
                    command_id=_accepted_command_id,
                    delivery_state="NOT_RECEIVED",
                    failure_phase="pre_dispatch",
                ),
                expected_conversation_state=conversation_state,
            )
            if isinstance(result, dict):
                logger.info(
                    "reconcile no-anchor stale with accepted command: session=%s turn=%s → FAILED(pre_dispatch)+NOT_RECEIVED",
                    session_id, turn_id,
                )
                return result

        # last_turn_status is left None here: no accepted command was ever
        # found for this turn, so there is no evidence to classify a status
        # from. The frontend delivery-failure contract keys off
        # delivery_state=NOT_RECEIVED + last_turn_id alone and never reads
        # last_turn_status:
        #   * backend: SessionReadMixin._build_delivery_failure (session_read.py)
        #     synthesizes `delivery_failure` from `delivery_state == "NOT_RECEIVED"`
        #     + `last_turn_id` alone.
        #   * frontend: chatHelpers.suppressNotReceivedUserMessages,
        #     messageSettlement.getTransportIdleTerminalTurnId, outboxAuthority,
        #     and useSessionChat's delivery-failure-materialization effect all
        #     branch on `delivery_state === 'NOT_RECEIVED'` (+ turn_id /
        #     delivery_failure.turn_id matching) and never read last_turn_status.
        # last_turn_id is preserved (turn_id=turn_id below) so the frontend can
        # match the failed delivery message by turn_id; the pre-write user
        # accepted command remains in the event log so it can be shown as failed.
        result = await self._session_snapshots_repo.apply_channel_update(
            session_id,
            channel="conversation",
            event_seq=int(event.get("event_seq") or 0),
            updates=build_turn_terminal_snapshot_updates(
                turn_id=turn_id,
                status=None,
                error_text=None,
                command_id=None,
                delivery_state="NOT_RECEIVED",
            ),
            expected_conversation_state=conversation_state,
        )
        if isinstance(result, dict):
            logger.info(
                "reconcile no-anchor stale: session=%s turn=%s → NOT_RECEIVED",
                session_id, turn_id,
            )
            return result
        return None

    async def _reconcile_has_anchor_stale(
        self,
        *,
        session_id: str,
        session: dict[str, Any],
        snapshot: dict[str, Any],
        conversation_state: str,
        turn_id: str,
        worker_heartbeat_stale: bool,
    ) -> dict[str, Any] | None:
        existing_frames = await self._session_events_repo.list_frames(
            session_id, turn_id=turn_id, after_seq=-1,
        )

        # Check if persisted frames contain a definitive turn-completion signal.
        # `data-result` is written by the worker only when the sandbox turn
        # truly completes (contains usage/cost/duration).  Interaction-pause
        # `finish` frames never produce `data-result`, so this check has no
        # false positives.
        # For error-only failures (no `data-result`), stale detection (300s)
        # is the safety net — acceptable for a degraded path.
        has_terminal_frame = False
        if conversation_state in {"PROCESSING", "STREAMING"}:
            has_terminal_frame = any(
                is_terminal_data_result_frame(f.get("payload"))
                for f in existing_frames
            )
            if has_terminal_frame:
                # Terminal frame exists but worker died before settling.
                # Delegate to TurnCoordinator for authoritative recovery.
                self._wakeup_turn_coordinator(session_id)

        # Stale detection: a dead worker heartbeat is authoritative owner-death
        # evidence; updated_at age remains only a degraded-path detector.
        active_owner_dead = bool(worker_heartbeat_stale) or _is_snapshot_stale(snapshot)
        if active_owner_dead:
            terminal_event = await journal_terminal_for_turn(
                self._session_events_repo,
                session_id=session_id,
                turn_id=turn_id,
            )
            if isinstance(terminal_event, dict):
                return await self._replay_projection_from_journal_terminal(
                    session_id=session_id,
                    session=session,
                    snapshot=snapshot,
                    turn_id=turn_id,
                    terminal_event=terminal_event,
                )
            # Single owner: hand the coordinator the owner-death verdict and
            # let it settle from durable evidence — mirror complete →
            # COMPLETED, box gone → FAILED, box alive and still working → stay
            # PROCESSING. An interim IDLE+FAILED projection would expose a
            # terminal state while recovery is still deciding. Uncertainty is
            # not evidence, so this branch never settles directly.
            resolved = await self._turn_coordinator.try_resolve_turn(
                session_id,
                session,
                snapshot,
                owner_dead=True,
            )
            if isinstance(resolved, dict):
                logger.info(
                    "reconcile stale: coordinator resolved session=%s turn=%s state=%s",
                    session_id, turn_id, conversation_state,
                )
                return resolved
            logger.info(
                "reconcile stale: owner dead, turn unresolved (mirror incomplete, "
                "box alive) session=%s turn=%s state=%s frames=%d — retrying later",
                session_id, turn_id, conversation_state, len(existing_frames),
            )
            return snapshot

        # Fresh + has frames + no terminal → worker likely still running.
        if existing_frames:
            return snapshot

        # Fresh + no frames → delegate to TurnCoordinator.
        # The stale detection above will eventually settle to IDLE+FAILED,
        # at which point the coordinator picks it up.
        self._wakeup_turn_coordinator(session_id)
        return snapshot
