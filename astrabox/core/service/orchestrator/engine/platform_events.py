"""Platform persistence behind the engine event-reporting capability."""

from __future__ import annotations

from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.core.service.orchestrator.engine.base import (
    ENGINE_MESSAGE_EVENT_TYPE,
    ResidentOutputCheckpoint,
    ResidentResponseHandle,
)
from astrabox.core.service.orchestrator.engine.emissions import (
    BackgroundTasksOpened,
    ChildResourceFact,
    EngineTurnEmission,
    PrivateDiagnostic,
    PublicUIFrame,
    TurnTerminal,
)
from astrabox.core.service.orchestrator.engine.frame_scope import (
    mark_engine_public_ui_frame,
    pop_engine_frame_scope,
)
from astrabox.core.service.orchestrator.engine.frame_translator import (
    BLOCK_INDEX_FIELD,
)
from astrabox.core.service.orchestrator.engine.input_delivery import (
    confirm_engine_input_consumed,
)
from astrabox.core.service.orchestrator.engine.interaction_contract import (
    build_pending_interaction_record,
    build_tool_approval_request_frame,
    validate_interaction_contract,
)
from astrabox.core.service.orchestrator.message_blocks import (
    canonicalize_terminal_message_blocks,
    normalize_message_blocks,
)
from astrabox.core.service.orchestrator.session_kernel.active_turn_projection import (
    build_active_turn_message,
)
from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    AI_SDK_FINISH_REASON_STOP,
    AI_SDK_FINISH_REASON_TOOL_CALLS,
    build_turn_active_snapshot_updates,
    build_turn_terminal_snapshot_updates,
    resident_engine_turn_id,
)
from astrabox.core.service.orchestrator.session_kernel.engine_emission_projection import (
    project_engine_interaction_opened,
    record_engine_background_tasks_opened,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn._replay import (
    _DurableSemanticFrameCoalescer,
)
from astrabox.persistence.repository.interaction_snapshot_repository import (
    InteractionSnapshotRepository,
)
from astrabox.persistence.repository.session_event_repository import (
    SessionEventRepository,
)
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.repository.session_snapshot_repository import (
    SessionSnapshotRepository,
)

logger = get_logger(__name__)

#: How a resident frame row names its writer. Read nowhere as a branch; it is
#: the operator's answer to "which lane wrote this row" beside the worker's
#: ``sandbox_transcript`` / ``transcript_mirror`` and recovery's
#: ``turn_recovery``.
RESIDENT_OUTPUT_SOURCE_KIND = "resident_engine_output"

_TERMINAL_EVENT_TYPES = frozenset({"turn.completed", "turn.failed", "turn.recovered"})
#: Frames whose meaning is their durable position: a reload closes the
#: browser's response at the cursor that follows them, so only the journaled
#: copy carries them. Same set the turn worker keeps off its live lane.
_LIVE_SUPPRESSED_FRAME_TYPES = frozenset(
    {"finish", "error", "data-result", "data-session-store-reload"}
)
_PLATFORM_ACTIVE_CONVERSATION_STATES = frozenset(
    {"PROCESSING", "STREAMING", "INTERRUPTING", "WAITING_FOR_INTERACTION"}
)
_FRAME_PAGE_SIZE = 500


class PlatformEngineEventSink:
    """Persist one Session's adapter-classified durable engine facts."""

    def __init__(self, session_id: str, *, journal_repo: Any | None = None) -> None:
        target = str(session_id or "").strip()
        if not target:
            raise ValueError("engine event sink requires a session id")
        self._session_id = target
        self._journal = (
            journal_repo if journal_repo is not None else SessionEventRepository()
        )

    async def confirm_input_consumed(
        self,
        *,
        input_id: str,
        content: str,
        consumer_carrier: str | None,
    ) -> None:
        await confirm_engine_input_consumed(
            self._journal,
            session_id=self._session_id,
            input_id=input_id,
            response_message_id=None,
            content=content,
            consumer_carrier=consumer_carrier,
        )

    async def persist_event(
        self,
        *,
        engine_kind: str,
        causation_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        event_doc = {
            "session_id": self._session_id,
            "channel": "conversation",
            "event_type": ENGINE_MESSAGE_EVENT_TYPE,
            "causation_id": str(causation_id),
            "payload": {
                "engine_kind": str(engine_kind),
                **dict(payload),
            },
        }
        persisted, _created = await self._journal.try_claim_event(event_doc)
        return persisted


class PlatformResidentOutputSink:
    """Publish output an engine produced with no platform input in flight.

    One response at a time per Session, addressed by the vendor's own id for
    the prompt that opened it. Every effect lands on the paths platform turns
    already use — ``session_events`` frame rows, the conversation snapshot the
    active overlay and header read, the terminal event the message projection
    settles from, and the broker the Session follower listens to — so a cold
    GET, an attached page and a later reload all read one journal.

    The snapshot slot is taken only from IDLE and released only while this
    response still holds it. A platform turn that starts meanwhile takes the
    slot over through its own admission; the resident output keeps landing
    durably under its own address and settles through its own terminal event.
    """

    def __init__(
        self,
        session_id: str,
        *,
        broker: Any | None,
        journal_repo: Any | None = None,
        snapshots_repo: Any | None = None,
        sessions_repo: Any | None = None,
        interactions_repo: Any | None = None,
    ) -> None:
        target = str(session_id or "").strip()
        if not target:
            raise ValueError("resident output sink requires a session id")
        self._session_id = target
        self._journal = (
            journal_repo if journal_repo is not None else SessionEventRepository()
        )
        self._snapshots = (
            snapshots_repo if snapshots_repo is not None else SessionSnapshotRepository()
        )
        self._sessions = sessions_repo if sessions_repo is not None else SessionRepository()
        self._interactions = (
            interactions_repo
            if interactions_repo is not None
            else InteractionSnapshotRepository()
        )
        self._broker = broker
        if broker is None:
            logger.warning(
                "resident output: no session event broker for session=%s; "
                "attached readers learn of engine-owned output only through "
                "durable polling",
                target,
            )
        self._coalescer = _DurableSemanticFrameCoalescer()
        #: Per-response live frame counter. The turn worker numbers its live
        #: frames the same way; the Session follower keys a live frame and
        #: its later durable row on ``(command_id, live_seq)`` and emits the
        #: payload once.
        self._live_seq = 0
        #: The adapter this Session's resident stream belongs to, learned from
        #: the first restore/open. Stamped on every row this sink writes.
        self._engine_kind = ""

    def _bind_engine_kind(self, engine_kind: str) -> str:
        kind = str(engine_kind or "").strip()
        if not kind:
            raise ValueError("resident output requires the adapter's engine kind")
        if self._engine_kind and self._engine_kind != kind:
            raise RuntimeError(
                "resident output sink is bound to another engine: "
                f"session={self._session_id} bound={self._engine_kind!r} "
                f"requested={kind!r}"
            )
        self._engine_kind = kind
        return kind

    # ── restore ──────────────────────────────────────────────────────────

    async def restore_resident_output(
        self,
        *,
        engine_kind: str,
    ) -> ResidentOutputCheckpoint:
        engine_kind = self._bind_engine_kind(engine_kind)
        snapshot = await self._snapshots.get_snapshot(self._session_id)
        response_id = resident_engine_turn_id(snapshot)
        conversation_state = str((snapshot or {}).get("conversation_state") or "").strip()
        if response_id is None:
            return ResidentOutputCheckpoint(
                external_turn_active=(
                    conversation_state in _PLATFORM_ACTIVE_CONVERSATION_STATES
                ),
            )
        anchor = (snapshot or {}).get("current_turn_engine_anchor") or {}
        anchor_kind = str(anchor.get("engine_kind") or "").strip()
        if anchor_kind != engine_kind:
            raise RuntimeError(
                "resident response is anchored on another engine: "
                f"session={self._session_id} response={response_id} "
                f"anchor={anchor_kind!r} adapter={engine_kind!r}"
            )
        frames = await self._turn_frames(response_id)
        boundary_sequence: int | None = None
        after_sequence: int | None = None
        live_sequence_after: int | None = None
        committed: list[dict[str, Any]] = []
        for frame in frames:
            sequence = frame.get("engine_sequence_number")
            if isinstance(sequence, int) and not isinstance(sequence, bool):
                boundary_sequence = (
                    sequence if boundary_sequence is None else min(boundary_sequence, sequence)
                )
                after_sequence = (
                    sequence if after_sequence is None else max(after_sequence, sequence)
                )
            live_seq = frame.get("live_seq")
            if isinstance(live_seq, int) and not isinstance(live_seq, bool):
                live_sequence_after = (
                    live_seq if live_sequence_after is None else max(live_sequence_after, live_seq)
                )
            payload = frame.get("payload")
            if not isinstance(payload, dict):
                continue
            row: dict[str, Any] = {
                "frame_seq": frame.get("frame_seq"),
                "payload": dict(payload),
            }
            for key in ("engine_sequence_number", "engine_block_index", "live_seq"):
                if key in frame:
                    row[key] = frame[key]
            committed.append(row)
        # Live sequences continue where the journaled rows stop: a follower
        # keyed on ``(command_id, live_seq)`` treats the same key as the same
        # frame, and this response's command id survives the reconnect.
        self._live_seq = (live_sequence_after + 1) if live_sequence_after is not None else 0
        return ResidentOutputCheckpoint(
            open_response_id=response_id,
            boundary_sequence=boundary_sequence,
            after_sequence=after_sequence,
            committed_frames=tuple(committed),
            external_turn_active=False,
        )

    # ── open ─────────────────────────────────────────────────────────────

    async def open_resident_response(
        self,
        *,
        engine_kind: str,
        response_id: str,
        engine_session_key: str | None,
        causation_id: str,
        native_message: dict[str, Any],
        runner_sequence: int,
    ) -> ResidentResponseHandle | None:
        clean_response_id = str(response_id or "").strip()
        if not clean_response_id:
            raise ValueError("resident response requires the engine's response id")
        engine_kind = self._bind_engine_kind(engine_kind)
        # The native boundary is durable before anything is published under
        # it. The runner marks the same message for persistence, so this claim
        # usually finds the persister's row; on a replay it always does.
        await self._journal.try_claim_event(
            {
                "session_id": self._session_id,
                "channel": "conversation",
                "event_type": ENGINE_MESSAGE_EVENT_TYPE,
                "causation_id": str(causation_id),
                "payload": {
                    "engine_kind": str(engine_kind),
                    "runner_sequence": int(runner_sequence),
                    "message": dict(native_message),
                    "engine_boundary": True,
                },
            }
        )
        settled = await self._journal.list_events(
            self._session_id,
            turn_id=clean_response_id,
            event_types=_TERMINAL_EVENT_TYPES,
            limit=1,
        )
        if settled:
            logger.info(
                "resident output: response already settled, replay skipped "
                "session=%s response=%s",
                self._session_id,
                clean_response_id,
            )
            return None
        self._coalescer = _DurableSemanticFrameCoalescer()
        self._live_seq = 0
        start_seq = await self._append_frames(
            clean_response_id,
            [
                {
                    "type": "start",
                    "messageId": clean_response_id,
                    "messageMetadata": {"turn_id": clean_response_id},
                }
            ],
            engine_sequence_number=runner_sequence,
        )
        updates = build_turn_active_snapshot_updates(
            conversation_state="STREAMING",
            turn_id=clean_response_id,
            worker_command_id=None,
            current_turn_remote_anchor=None,
            current_turn_engine_anchor={
                "engine_kind": str(engine_kind),
                "engine_turn_id": clean_response_id,
                "engine_session_key": engine_session_key,
                "engine_sequence_number": int(runner_sequence),
            },
            delivery_state=None,
        )
        updates["worker_heartbeat_at"] = utcnow_iso()
        snapshot = await self._snapshots.apply_channel_update(
            self._session_id,
            channel="conversation",
            event_seq=start_seq,
            updates=updates,
            expected_conversation_state="IDLE",
        )
        owns_slot = isinstance(snapshot, dict)
        if not owns_slot:
            current = await self._snapshots.get_snapshot(self._session_id)
            owns_slot = resident_engine_turn_id(current) == clean_response_id
            if not owns_slot:
                logger.warning(
                    "resident output: conversation slot is held by a platform "
                    "turn; publishing engine-owned response durably without the "
                    "overlay session=%s response=%s state=%s current_turn=%s",
                    self._session_id,
                    clean_response_id,
                    str((current or {}).get("conversation_state") or ""),
                    str((current or {}).get("current_turn_id") or ""),
                )
        return ResidentResponseHandle(
            response_id=clean_response_id,
            owns_slot=owns_slot,
        )

    # ── publish ──────────────────────────────────────────────────────────

    async def publish_resident_output(
        self,
        handle: ResidentResponseHandle,
        emissions: list[EngineTurnEmission],
        *,
        engine_sequence_number: int,
    ) -> None:
        engine_kind = self._engine_kind
        if not engine_kind:
            raise RuntimeError("resident output published before the sink was bound")
        ready: list[dict[str, Any]] = []
        for emission in emissions:
            frame = emission.as_frame()
            if isinstance(emission, PublicUIFrame):
                frame = mark_engine_public_ui_frame(frame)
                # Live and durable are two deliveries, as for a platform
                # turn: every public frame reaches an attached follower at
                # chunk cadence through the broker, while the journal keeps
                # the coalesced record a reload or cold GET reads. The row
                # carries the live sequence so a follower that already
                # rendered the chunk emits only the durable cursor for it.
                live_seq = self._live_seq
                self._live_seq += 1
                frame["__live_seq"] = live_seq
                if (
                    self._broker is not None
                    and str(frame.get("type") or "") not in _LIVE_SUPPRESSED_FRAME_TYPES
                ):
                    await self._broker.publish(
                        self._session_id,
                        {
                            "type": "ai_sdk_live_frame",
                            "command_id": handle.response_id,
                            "turn_id": handle.response_id,
                            "live_seq": live_seq,
                            "payload": {
                                key: value
                                for key, value in frame.items()
                                if not str(key).startswith("__")
                            },
                            "source_cursor": {"live_seq": live_seq},
                        },
                    )
                ready.extend(self._coalescer.ingest(frame))
                continue
            if isinstance(emission, ChildResourceFact):
                ready.extend(self._coalescer.ingest(frame))
                continue
            if isinstance(emission, PrivateDiagnostic):
                await self._journal.append_event(
                    {
                        "session_id": self._session_id,
                        "channel": "engine",
                        "turn_id": handle.response_id,
                        "event_type": "engine.diagnostic",
                        "causation_id": handle.response_id,
                        "correlation_id": handle.response_id,
                        "payload": {
                            "engine_kind": engine_kind,
                            "engine_turn_id": handle.response_id,
                            "event_type": emission.event_type,
                            "subtype": emission.subtype,
                            "raw": emission.raw,
                            "source": RESIDENT_OUTPUT_SOURCE_KIND,
                        },
                    }
                )
                continue
            if isinstance(emission, BackgroundTasksOpened):
                await record_engine_background_tasks_opened(
                    session_events_repo=self._journal,
                    session_id=self._session_id,
                    turn_id=handle.response_id,
                    command_id=handle.response_id,
                    correlation_id=handle.response_id,
                    engine_kind=engine_kind,
                    manifest=dict(emission.manifest),
                )
                continue
            raise ValueError(
                "resident output cannot publish "
                f"{type(emission).__name__}: the adapter owns that boundary"
            )
        if ready:
            await self._append_frames(
                handle.response_id,
                ready,
                engine_sequence_number=engine_sequence_number,
            )

    # ── liveness ─────────────────────────────────────────────────────────

    async def heartbeat_resident_response(
        self,
        handle: ResidentResponseHandle,
    ) -> bool:
        if not handle.owns_slot:
            return False
        return bool(
            await self._snapshots.force_update_fields(
                self._session_id,
                {"worker_heartbeat_at": utcnow_iso()},
                extra_filter={
                    "current_turn_id": handle.response_id,
                    "current_turn_worker_command_id": None,
                },
            )
        )

    # ── interaction ──────────────────────────────────────────────────────

    async def open_resident_interaction(
        self,
        handle: ResidentResponseHandle,
        *,
        interaction_id: str,
        contract: dict[str, Any],
        engine_session_key: str | None,
        engine_sequence_number: int,
    ) -> bool:
        engine_kind = self._engine_kind
        if not engine_kind:
            raise RuntimeError("resident interaction opened before the sink was bound")
        response_id = handle.response_id
        clean_interaction_id = str(interaction_id or "").strip()
        if not clean_interaction_id:
            raise ValueError("resident interaction requires the engine's interaction id")
        declared = dict(contract)
        gate_tool_use_id = str(declared.pop("tool_use_id", "") or "").strip()
        validate_interaction_contract(declared)
        tool_name = str(declared.get("tool_name") or "")
        pending = build_pending_interaction_record(
            contract=declared,
            session_id=self._session_id,
            turn_id=response_id,
            interaction_id=clean_interaction_id,
            tool_call_id=gate_tool_use_id or None,
        )
        pending["engine_kind"] = engine_kind
        pending["engine_turn_id"] = response_id
        if engine_session_key:
            pending["engine_session_key"] = engine_session_key
        if not handle.owns_slot:
            logger.warning(
                "resident output: engine-owned response asked for an interaction "
                "while a platform turn holds the conversation slot; it stays "
                "with the runner's own wait budget session=%s response=%s "
                "interaction=%s tool=%s",
                self._session_id,
                response_id,
                clean_interaction_id,
                tool_name,
            )
            return False
        # Durable authority first, then the stream. Commit-before-emit is the
        # same order the turn worker keeps.
        pending_frames = self._coalescer.flush()
        if pending_frames:
            await self._append_frames(
                response_id,
                pending_frames,
                engine_sequence_number=engine_sequence_number,
            )
        await self._sessions.update_session(
            self._session_id,
            {
                "pending_interaction": pending,
                "last_error": None,
                "runtime_unavailable": False,
            },
        )
        projection = await project_engine_interaction_opened(
            session_events_repo=self._journal,
            session_snapshots_repo=self._snapshots,
            interaction_snapshots_repo=self._interactions,
            session_id=self._session_id,
            turn_id=response_id,
            command_id=response_id,
            correlation_id=response_id,
            pending=pending,
            tool_name=tool_name,
            source_event_seq_applied=0,
            current_turn_remote_anchor=None,
            current_turn_engine_anchor={
                "engine_kind": engine_kind,
                "engine_turn_id": response_id,
                "engine_session_key": engine_session_key,
                "engine_sequence_number": int(engine_sequence_number),
            },
        )
        if projection.fenced or not projection.waiting:
            # Fenced: the authority event landed but the snapshot CAS on this
            # response's slot missed, so a platform turn owns the conversation
            # now and this response is not the one the platform waits on —
            # the same verdict the turn worker treats as fenced out. Not
            # waiting: the interaction was already answered. Either way the
            # row written above is withdrawn — only that row: a record another
            # owner wrote during these awaits is theirs — and nothing is
            # published for it.
            await self._sessions.compare_and_update_session(
                self._session_id,
                expected={"pending_interaction": pending},
                updates={"pending_interaction": None},
            )
            logger.warning(
                "resident output: interaction did not park the conversation "
                "session=%s response=%s interaction=%s fenced=%s waiting=%s",
                self._session_id,
                response_id,
                clean_interaction_id,
                projection.fenced,
                projection.waiting,
            )
            return False
        if self._broker is not None:
            await self._broker.publish(
                self._session_id,
                {
                    "type": "pending_interaction",
                    "turn_id": response_id,
                    "interaction_id": clean_interaction_id,
                    "tool_name": tool_name,
                    "pending_interaction": pending,
                },
            )
        control_frames: list[dict[str, Any]] = []
        approval_request_frame = build_tool_approval_request_frame(pending)
        if approval_request_frame is not None:
            control_frames.append(approval_request_frame)
        control_frames.append({"type": "data-interaction", "data": dict(pending)})
        control_frames.append(
            {"type": "finish", "finishReason": AI_SDK_FINISH_REASON_TOOL_CALLS}
        )
        await self._append_frames(
            response_id,
            control_frames,
            engine_sequence_number=engine_sequence_number,
        )
        return True

    # ── close ────────────────────────────────────────────────────────────

    async def close_resident_response(
        self,
        handle: ResidentResponseHandle,
        *,
        terminal: TurnTerminal,
        engine_sequence_number: int,
    ) -> None:
        response_id = handle.response_id
        pending = self._coalescer.flush()
        if pending:
            await self._append_frames(
                response_id,
                pending,
                engine_sequence_number=engine_sequence_number,
            )
        frames = await self._turn_frames(response_id)
        projected = build_active_turn_message(
            session_id=self._session_id,
            turn_id=response_id,
            message_id=response_id,
            frames=frames,
            existing_message=None,
            default_message_seq=0,
        )
        blocks = canonicalize_terminal_message_blocks(
            [
                block
                for block in normalize_message_blocks((projected or {}).get("blocks"))
                if str(block.get("type") or "").strip() != "subagent"
            ]
        )
        assistant_text = str((projected or {}).get("content") or "")

        error_text: str | None = None
        if terminal.outcome == "failed":
            error = terminal.error or {}
            error_text = (
                str(error.get("message") or "").strip()
                or str(error.get("code") or "").strip()
                or "engine reported finishReason=error without message"
            )
            terminal_payload: dict[str, Any] = {"type": "error", "errorText": error_text}
            event_type = "turn.failed"
            status = "FAILED"
        else:
            terminal_payload = {
                "type": "finish",
                "finishReason": AI_SDK_FINISH_REASON_STOP,
            }
            event_type = "turn.completed"
            status = "COMPLETED"
        finish_seq = await self._append_frames(
            response_id,
            [terminal_payload],
            engine_sequence_number=engine_sequence_number,
        )
        terminal_frame: dict[str, Any] = {
            "turn_id": response_id,
            "command_id": response_id,
            "frame_seq": finish_seq,
            "type": terminal_payload["type"],
        }
        if "finishReason" in terminal_payload:
            terminal_frame["finish_reason"] = terminal_payload["finishReason"]
        event, _created = await self._journal.try_claim_event(
            {
                "session_id": self._session_id,
                "channel": "conversation",
                "turn_id": response_id,
                "event_type": event_type,
                "causation_id": f"{response_id}:resident-terminal",
                "correlation_id": response_id,
                "payload": {
                    "command_id": response_id,
                    "final_state": "READY",
                    "assistant_text": assistant_text or None,
                    "blocks": blocks,
                    "error_text": error_text,
                    "failure_phase": None,
                    "terminal_reason": terminal.native_reason,
                    "finish_reason": terminal.finish_reason,
                    "usage": dict(terminal.usage) if terminal.usage else None,
                    "engine_turn_id": response_id,
                    "engine_sequence_number": int(engine_sequence_number),
                    "source": RESIDENT_OUTPUT_SOURCE_KIND,
                },
            }
        )
        event_seq = int(event.get("event_seq") or 0)
        if not event_seq:
            raise RuntimeError(
                "resident terminal event has no journal sequence "
                f"session={self._session_id} response={response_id}"
            )
        if not handle.owns_slot:
            return
        result = await self._snapshots.apply_channel_update(
            self._session_id,
            channel="conversation",
            event_seq=event_seq,
            updates=build_turn_terminal_snapshot_updates(
                turn_id=response_id,
                status=status,
                error_text=error_text,
                command_id=response_id,
                terminal_reason=terminal.native_reason,
                terminal_frame=terminal_frame,
            ),
            extra_filter={
                "current_turn_id": response_id,
                "current_turn_worker_command_id": None,
            },
        )
        if not isinstance(result, dict):
            logger.warning(
                "resident output: terminal journaled but the conversation slot "
                "moved on before it closed session=%s response=%s event_seq=%s",
                self._session_id,
                response_id,
                event_seq,
            )

    # ── journal plumbing ─────────────────────────────────────────────────

    async def _turn_frames(self, turn_id: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        after_seq = -1
        while True:
            batch = await self._journal.list_frames(
                self._session_id,
                turn_id=turn_id,
                scope="turn",
                after_seq=after_seq,
                limit=_FRAME_PAGE_SIZE,
            )
            if not batch:
                return rows
            rows.extend(dict(row) for row in batch)
            next_seq = max(int(row.get("frame_seq") or 0) for row in batch)
            if next_seq <= after_seq:
                raise RuntimeError("resident output frame scan did not advance")
            after_seq = next_seq
            if len(batch) < _FRAME_PAGE_SIZE:
                return rows

    async def _append_frames(
        self,
        response_id: str,
        payloads: list[dict[str, Any]],
        *,
        engine_sequence_number: int,
    ) -> int:
        """Journal frames in one contiguous block and tell the broker.

        Returns the first sequence of the block. The rows carry the same
        fields the turn worker writes, plus the runner sequence that produced
        them, which is the watermark a reconnecting observer resumes from. A
        Session-scoped child fact rides in the same block with no turn id, as
        it does when a platform turn observes it.
        """

        if not payloads:
            raise ValueError("resident output append requires at least one frame")
        starting_seq = int(
            await self._journal.allocate_session_frame_seq(
                self._session_id,
                count=len(payloads),
            )
        )
        docs: list[dict[str, Any]] = []
        broker_events: list[dict[str, Any]] = []
        for offset, payload in enumerate(payloads):
            frame_payload = dict(payload)
            scope = pop_engine_frame_scope(frame_payload)
            turn_id = None if scope == "session" else response_id
            frame_seq = starting_seq + offset
            live_seq = frame_payload.pop("__live_seq", None)
            block_index = frame_payload.pop(BLOCK_INDEX_FIELD, None)
            doc: dict[str, Any] = {
                "session_id": self._session_id,
                "turn_id": turn_id,
                "scope": scope,
                "command_id": response_id,
                "frame_seq": frame_seq,
                "payload": frame_payload,
                "created_at": utcnow_iso(),
                "source_kind": RESIDENT_OUTPUT_SOURCE_KIND,
                "engine_kind": self._engine_kind,
                "engine_turn_id": response_id,
                "engine_sequence_number": int(engine_sequence_number),
            }
            broker_event: dict[str, Any] = {
                "type": "ai_sdk_frame",
                "command_id": response_id,
                "turn_id": turn_id,
                "scope": scope,
                "frame_seq": frame_seq,
                "payload": dict(frame_payload),
            }
            if isinstance(live_seq, int) and not isinstance(live_seq, bool):
                doc["live_seq"] = live_seq
                broker_event["live_seq"] = live_seq
                broker_event["source_cursor"] = {"live_seq": live_seq}
            if isinstance(block_index, int) and not isinstance(block_index, bool):
                doc["engine_block_index"] = block_index
            docs.append(doc)
            broker_events.append(broker_event)
        await self._journal.append_frames(docs)
        if self._broker is not None:
            for event in broker_events:
                await self._broker.publish(self._session_id, event)
        return starting_seq


__all__ = [
    "PlatformEngineEventSink",
    "PlatformResidentOutputSink",
    "RESIDENT_OUTPUT_SOURCE_KIND",
]
