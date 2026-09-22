from __future__ import annotations

import contextlib
from typing import Any


from astrabox.persistence.repository.backend import (
    mongo_fail_fast_context,
    mongo_fail_fast_reset,
)
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.retry_utils import (
    build_retry_warning_before_sleep,
    retry_async_call,
)
from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.common.utils.user_context import UserContext

from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    append_settle_terminal_frame,
    build_turn_terminal_snapshot_updates,
    settle_parked_turn,
    normalize_current_turn_remote_anchor as _normalize_current_turn_remote_anchor,
)
from astrabox.core.service.orchestrator.session_kernel.permission_lifecycle import (
    PermissionLifecycle,
)
from astrabox.core.service.orchestrator.session_kernel.workers.base import KernelWorkerBase
from astrabox.core.service.orchestrator.session_kernel.workers.models import (
    WorkerOutcome,
    WorkerWakeup,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn._helpers import (
    _build_current_turn_remote_anchor,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn._latency import (
    _TurnLatencyTrace,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn import (
    bridge_loop,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn.projections import (
    _TurnProjectionMixin,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn.answer_stream import (
    _AnswerStreamMixin,
)

logger = get_logger(__name__)


class TurnWorker(_TurnProjectionMixin, _AnswerStreamMixin, KernelWorkerBase):
    channel = "conversation"

    def __init__(
        self,
        *,
        worker_id: str,
        sessions_repo: Any,
        turn_service: Any,
        runtime_manager: Any,
        broker: Any,
        session_events_repo: Any,
        session_snapshots_repo: Any,
        interaction_snapshots_repo: Any,
        transcript_entries_repo: Any,
        bridge_event_stall_timeout_s: float,
        worker_heartbeat_interval_s: float,
        live_frame_retry_window_s: float,
        live_frame_retry_delay_s: float,
        requested_projection_retry_window_s: float,
        requested_projection_retry_delay_s: float,
        terminal_settle_retry_window_s: float,
        terminal_settle_retry_delay_s: float,
        title_service: Any | None = None,
        redrive_stranded_inputs: Any | None = None,
        spawn_background_task: Any | None = None,
    ) -> None:
        super().__init__(worker_id=worker_id)
        self._sessions_repo = sessions_repo
        self._turn_service = turn_service
        self._runtime_manager = runtime_manager
        self._broker = broker
        self._session_events_repo = session_events_repo
        self._session_snapshots_repo = session_snapshots_repo
        self._interaction_snapshots_repo = interaction_snapshots_repo
        self._transcript_entries_repo = transcript_entries_repo
        self._permission_lifecycle = PermissionLifecycle(
            sessions_repo=sessions_repo,
            apply_engine_permission_mode=turn_service.set_engine_permission_mode,
            session_events_repo=session_events_repo,
            session_snapshots_repo=session_snapshots_repo,
            interaction_snapshots_repo=interaction_snapshots_repo,
        )
        self._session_title_service = title_service
        self._redrive_stranded_inputs = redrive_stranded_inputs
        #: How this worker starts work that must not hold up settling a turn.
        #: A worker built without one schedules nothing, and the browser asks
        #: for the same work when it needs it.
        self._spawn_background_task = spawn_background_task
        self._bridge_event_stall_timeout_s = max(0.01, float(bridge_event_stall_timeout_s or 90.0))
        self._worker_heartbeat_interval_s = max(0.01, float(worker_heartbeat_interval_s or 5.0))
        self._live_frame_retry_window_s = max(0.0, float(live_frame_retry_window_s or 0.0))
        self._live_frame_retry_delay_s = max(0.01, float(live_frame_retry_delay_s or 0.5))
        self._requested_projection_retry_window_s = max(
            0.0,
            float(requested_projection_retry_window_s or 0.0),
        )
        self._requested_projection_retry_delay_s = max(
            0.01,
            float(requested_projection_retry_delay_s or 0.5),
        )
        self._terminal_settle_retry_window_s = max(0.0, float(terminal_settle_retry_window_s or 0.0))
        self._terminal_settle_retry_delay_s = max(0.01, float(terminal_settle_retry_delay_s or 0.5))

    async def run_once(
        self,
        wakeup: WorkerWakeup,
    ) -> WorkerOutcome:
        # Initial phase: read command + session, project snapshot.
        # Fail fast on Mongo errors — retrying on the same machine is
        # pointless if the connection is broken.  The SSE stream will
        # propagate the error to the frontend, which can retry on a
        # different backend machine.
        _ff_token = mongo_fail_fast_context()
        latency_trace: _TurnLatencyTrace | None = None
        try:
            command_event = await self._session_events_repo.get_command_event(
                wakeup.session_id,
                command_id=wakeup.command_id,
            )
            if not isinstance(command_event, dict):
                raise RuntimeError(
                    f"missing command.accepted event session_id={wakeup.session_id} command_id={wakeup.command_id}"
                )
            latency_trace = _TurnLatencyTrace(
                session_id=wakeup.session_id,
                turn_id=str(command_event.get("turn_id") or "").strip() or None,
                command_id=str(command_event.get("causation_id") or "").strip() or None,
                command_type=None,
                accepted_at=command_event.get("occurred_at"),
            )
            latency_trace.mark("turn_worker.command_event_loaded")

            session = await self._sessions_repo.get_session(wakeup.session_id)
            if not isinstance(session, dict):
                raise RuntimeError(f"missing session {wakeup.session_id}")
            latency_trace.mark("turn_worker.session_loaded")

            payload = command_event.get("payload")
            if not isinstance(payload, dict):
                raise RuntimeError(
                    f"command.accepted payload missing session_id={wakeup.session_id} command_id={wakeup.command_id}"
                )

            user = self._build_user_context(session, payload)
            command_type = str(payload.get("command_type") or "").strip()
            latency_trace.command_type = command_type or None
            latency_trace.set_context(user=user, session=session)
            latency_trace.mark("turn_worker.context_built")
            # A handoff continuation reuses the original input's command as
            # the one FIFO root but runs it under a fresh turn: the command
            # event's own turn settled under it, and a second command for the
            # same input would be a second root the engine can never consume.
            wakeup_turn = str(wakeup.turn_id or "").strip()
            event_turn = str(command_event.get("turn_id") or "").strip()
            turn_id_override = (
                wakeup_turn if wakeup_turn and wakeup_turn != event_turn else None
            )
            await self._project_command_accepted(
                session_id=wakeup.session_id,
                turn_id=(turn_id_override or event_turn) or None,
                command_event=command_event,
                command_type=command_type,
                readmit_after_settle=bool(turn_id_override),
            )
            latency_trace.mark("turn_worker.command_projected")

        finally:
            mongo_fail_fast_reset(_ff_token)

        if latency_trace is not None:
            latency_trace.log("worker_setup")

        metadata: dict[str, Any] = {}
        if command_type == "InterruptTurn":
            processed_event_seq, result = await self._run_interrupt_command(
                user=user,
                session_id=wakeup.session_id,
                command_event=command_event,
            )
            metadata["result"] = dict(result)
        else:
            processed_event_seq = await bridge_loop._run_bridge_command(
                self,
                user=user,
                session_id=wakeup.session_id,
                session=session,
                command_event=command_event,
                payload=payload,
                latency_trace=latency_trace,
                turn_id_override=turn_id_override,
            )
            settled_turn_id = turn_id_override or str(
                command_event.get("turn_id") or ""
            ).strip()
            if callable(self._redrive_stranded_inputs) and settled_turn_id:
                # A cancelled settle can leave inputs whose FIFO verdict
                # predates them; the sweep is criterion-gated and idempotent,
                # and its failure must stay loud without failing the settled
                # turn a second time.
                try:
                    await self._redrive_stranded_inputs(
                        wakeup.session_id, turn_id=settled_turn_id
                    )
                except Exception:
                    logger.warning(
                        "stranded-input redrive failed session=%s turn=%s",
                        wakeup.session_id,
                        settled_turn_id,
                        exc_info=True,
                    )
        return WorkerOutcome(
            session_id=wakeup.session_id,
            channel=self.channel,
            status="idle",
            processed_event_seq=processed_event_seq,
            metadata=metadata,
        )

    @staticmethod
    def _build_user_context(session: dict[str, Any], payload: dict[str, Any]) -> UserContext:
        user_id = str(
            payload.get("author_user_id")
            or session.get("user_id")
            or ""
        ).strip()
        return UserContext(user_id=user_id)

    async def _get_max_frame_seq(self, session_id: str, turn_id: str) -> int | None:
        """Return the current max frame_seq for the given session+turn, or None if no frames.

        Reads the turn's watermark, not the session's. Sequence allocation is
        session-scoped, so the session maximum can belong to a concurrent
        background-lane append; this answers "where does this turn end".
        """
        if not turn_id:
            return None
        return await self._session_events_repo.get_max_turn_frame_seq(
            session_id, turn_id=turn_id
        )

    async def _observe_current_turn_remote_anchor(
        self,
        *,
        session_id: str,
        local_turn_id: str,
        sandbox_turn_id: int | None,
        last_sandbox_seq: int | None,
        known_anchor: dict[str, int] | None = None,
    ) -> dict[str, int] | None:
        anchor = _build_current_turn_remote_anchor(
            sandbox_turn_id=sandbox_turn_id,
            last_sandbox_seq=last_sandbox_seq,
        )
        if anchor is None or not local_turn_id:
            return known_anchor

        current_anchor = _normalize_current_turn_remote_anchor(known_anchor)
        if (
            isinstance(current_anchor, dict)
            and int(current_anchor["sandbox_turn_id"]) != int(anchor["sandbox_turn_id"])
        ):
            raise RuntimeError(
                "observed conflicting sandbox turn anchor for the active turn"
            )

        current_seq = (
            int(current_anchor["last_sandbox_seq"])
            if isinstance(current_anchor, dict)
            and isinstance(current_anchor.get("last_sandbox_seq"), int)
            else None
        )
        new_seq = (
            int(anchor["last_sandbox_seq"])
            if isinstance(anchor.get("last_sandbox_seq"), int)
            else None
        )
        if (
            isinstance(current_anchor, dict)
            and (
                new_seq is None
                or (current_seq is not None and current_seq >= new_seq)
            )
        ):
            return current_anchor

        updated = await self._session_snapshots_repo.force_update_fields(
            session_id,
            {"current_turn_remote_anchor": dict(anchor)},
            extra_filter={"current_turn_id": local_turn_id},
        )
        if updated:
            return dict(anchor)

        snapshot = await self._session_snapshots_repo.get_snapshot(session_id)
        stored_anchor = _normalize_current_turn_remote_anchor(
            (snapshot or {}).get("current_turn_remote_anchor")
        )
        snapshot_turn_id = str((snapshot or {}).get("current_turn_id") or "").strip()
        if (
            snapshot_turn_id == local_turn_id
            and isinstance(stored_anchor, dict)
            and int(stored_anchor["sandbox_turn_id"]) != int(anchor["sandbox_turn_id"])
        ):
            raise RuntimeError(
                "session snapshot contains a conflicting sandbox turn anchor for the active turn"
            )
        if isinstance(stored_anchor, dict):
            return stored_anchor
        return current_anchor

    async def _run_interrupt_command(
        self,
        *,
        user: UserContext,
        session_id: str,
        command_event: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        command_id = str(command_event.get("causation_id") or "").strip()
        correlation_id = str(command_event.get("correlation_id") or "").strip() or command_id
        turn_id = str(command_event.get("turn_id") or "").strip() or None
        _ = (user, correlation_id)
        if not turn_id:
            return int(command_event.get("event_seq") or 0), {
                "session_id": session_id,
                "status": "idle",
            }
        snapshot: dict[str, Any] | None = None
        # The interrupt mark lives on the snapshot, CAS-scoped to the active
        # turn: session_snapshots owns current_turn_id, so this is the one
        # store where "mark only the turn being stopped" and the park side's
        # post-commit read meet with no window. The sessions row's
        # current_turn_id is a terminal-time mirror that is empty during a
        # live turn, so a mark scoped against it would never land and would
        # leave the park-vs-interrupt race open.
        interrupt_mark = {
            "interrupt_requested": True,
            "interrupt_requested_at": utcnow_iso(),
        }
        requested = await self._session_snapshots_repo.force_update_fields(
            session_id,
            interrupt_mark,
            extra_filter={"current_turn_id": turn_id},
        )
        if not requested:
            snapshot = await self._session_snapshots_repo.get_snapshot(session_id)
            snapshot_turn_id = str((snapshot or {}).get("current_turn_id") or "").strip()
            if snapshot_turn_id != turn_id:
                return int(command_event.get("event_seq") or 0), {
                    "session_id": session_id,
                    "status": "idle",
                }
            requested = await self._session_snapshots_repo.force_update_fields(
                session_id,
                interrupt_mark,
                extra_filter={"current_turn_id": turn_id},
            )
        if snapshot is None:
            snapshot = await self._session_snapshots_repo.get_snapshot(session_id)
        session = await self._sessions_repo.get_session(session_id)
        if not isinstance(session, dict):
            raise RuntimeError(f"missing session {session_id}")
        await self._turn_service.interrupt_engine_turn(session)

        event = await self._session_events_repo.append_event(
            {
                "session_id": session_id,
                "channel": "conversation",
                "turn_id": turn_id,
                "event_type": "turn.interrupt_requested",
                "causation_id": command_id,
                "correlation_id": correlation_id,
                "payload": {
                    "status": "accepted",
                    "command_id": command_id,
                },
            }
        )
        settle_seq = await self._settle_pre_first_token_interrupt(
            session_id=session_id,
            turn_id=turn_id,
            command_id=command_id,
            snapshot=snapshot,
        )
        return int(settle_seq or event.get("event_seq") or 0), {
            "session_id": session_id,
            "status": "accepted",
        }

    async def _settle_interrupted_waiting_interaction(
        self,
        *,
        session_id: str,
        turn_id: str,
        command_id: str | None,
    ) -> int | None:
        """Settle a turn parked awaiting an interaction under an interrupt.

        A parked turn's worker exited at the interaction boundary, so nothing
        is left to consume the cancelled terminal the runtime interrupt
        produces — the settle happens here: close the turn and deactivate the
        pending interaction, without which the session stays WAITING_INPUT
        forever after an interrupt.

        Called from both sides of the interrupt-vs-park race: the interrupt
        command settles a turn it finds already parked, and a park that lands
        after that check re-reads the session's ``interrupt_requested`` mark
        and settles itself (see the bridge's park commit). Monotonic-CAS'd via
        ``expected_conversation_state``, so a double settle converges — both
        outcomes are "turn stopped".
        """
        return await settle_parked_turn(
            session_events_repo=self._session_events_repo,
            session_snapshots_repo=self._session_snapshots_repo,
            interaction_snapshots_repo=self._interaction_snapshots_repo,
            session_id=session_id,
            turn_id=turn_id,
            command_id=command_id,
            # A user who stops a turn ended it; they did not break it. This
            # platform-side closure observed no engine terminal, so it cannot
            # assign a vendor terminal reason. No failure phase: nothing failed.
            status="COMPLETED",
            failure_phase=None,
            error_text=None,
            causation=f"interrupt-settle:{session_id}:{turn_id}",
        )

    async def _settle_pre_first_token_interrupt(
        self,
        *,
        session_id: str,
        turn_id: str,
        command_id: str | None,
        snapshot: dict[str, Any] | None,
    ) -> int | None:
        """Settle a turn interrupted before it produced any frame.

        The interrupt-settle race: an interrupt landing before the model's
        first token aborts the CLI while the sandbox sidecar suppresses the
        interrupted turn's terminal frames from the bridge, so the bridge never
        settles and the UI hangs "Stopping…". Once frames exist the bridge
        settles normally (and may carry partial content), so this force-settle
        is scoped to the zero-frame case only — it targets exactly the hang and
        never overwrites a turn that produced content.

        Monotonic-CAS'd on the terminal event's seq via ``apply_channel_update``
        with ``expected_conversation_state``: if the bridge does settle
        concurrently, whichever event_seq is higher wins, and both outcomes are
        "turn stopped", so the snapshot converges either way.
        """
        conversation_state = str((snapshot or {}).get("conversation_state") or "").strip()
        if str((snapshot or {}).get("current_turn_id") or "").strip() != turn_id:
            return None
        if conversation_state == "WAITING_FOR_INTERACTION":
            return await self._settle_interrupted_waiting_interaction(
                session_id=session_id,
                turn_id=turn_id,
                command_id=command_id,
            )
        if conversation_state not in ("PROCESSING", "STREAMING"):
            # A non-active state has nothing to settle.
            return None
        existing_frames = await self._session_events_repo.list_frames(
            session_id, turn_id=turn_id, limit=1
        )
        if existing_frames:
            return None  # frames exist -> the bridge owns the terminal settle
        settle_event = await self._session_events_repo.append_event(
            {
                "session_id": session_id,
                "channel": "conversation",
                "turn_id": turn_id,
                # A stop before the first token is the same gesture as a stop
                # anywhere else: the user ended the turn. No engine terminal
                # was observed, so the platform records no vendor reason.
                "event_type": "turn.completed",
                "causation_id": f"interrupt-settle:{session_id}:{turn_id}",
                "correlation_id": f"interrupt-settle:{session_id}:{turn_id}",
                "payload": {
                    "command_id": command_id,
                    "error_text": None,
                    "terminal_reason": None,
                    "failure_phase": None,
                },
            }
        )
        settle_seq = int(settle_event.get("event_seq") or 0)
        # The frame half. Settling only the snapshot leaves every client that
        # is already streaming with no terminal to read: the database calls the
        # session idle while an open page keeps its header running, which is
        # the hang this force-settle exists to end, moved one layer out.
        terminal_frame = await append_settle_terminal_frame(
            session_events_repo=self._session_events_repo,
            session_id=session_id,
            turn_id=turn_id,
            command_id=command_id,
        )
        result = await self._session_snapshots_repo.apply_channel_update(
            session_id,
            channel="conversation",
            event_seq=settle_seq,
            updates=build_turn_terminal_snapshot_updates(
                turn_id=turn_id,
                status="COMPLETED",
                error_text=None,
                command_id=command_id,
                terminal_reason=None,
                terminal_frame=terminal_frame,
            ),
            expected_conversation_state=conversation_state,
        )
        if isinstance(result, dict):
            logger.info(
                "interrupt-settle: pre-first-token turn settled session=%s turn=%s",
                session_id, turn_id,
            )
        return settle_seq
