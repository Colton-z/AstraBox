from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.time_utils import utcnow_iso as _utcnow_iso

logger = get_logger(__name__)

#: The ``turn_failure`` error text a turn carries when the user stopped it.
#: Every settlement path that records a user stop writes this exact string, and
#: readers that distinguish a stop from a real failure compare against it, so
#: the two sides must name the same constant rather than two equal literals.
USER_STOP_FAILURE_TEXT = "Request interrupted by user"

TURN_RECOVERY_PHASE_TRANSCRIPT_PENDING = "TRANSCRIPT_PENDING"
_VALID_TURN_RECOVERY_PHASES = frozenset({TURN_RECOVERY_PHASE_TRANSCRIPT_PENDING})
_UNSET = object()

TURN_TERMINAL_ACTOR_LIVE_WORKER = "live_worker"
TURN_TERMINAL_ACTOR_READ_RESUME = "read_resume"
TURN_TERMINAL_ACTOR_RECOVERY_COORDINATOR = "recovery_coordinator"
TURN_TERMINAL_ACTOR_JOURNAL_REPLAY = "journal_replay"
TURN_TERMINAL_ACTOR_CHECKPOINT_RECONCILE = "checkpoint_reconcile"
TURN_TERMINAL_ACTOR_STALE_RECONCILE = "stale_reconcile"
TURN_TERMINAL_ACTOR_ENGINE_RECOVERY = "engine_recovery"

_ACTIVE_TURN_TERMINAL_STATES = frozenset(
    {"PROCESSING", "STREAMING", "WAITING_FOR_INTERACTION"}
)
_TERMINAL_LAST_TURN_STATUSES = frozenset({"COMPLETED", "FAILED"})
_ACTIVE_TERMINAL_ACTORS = frozenset(
    {
        TURN_TERMINAL_ACTOR_LIVE_WORKER,
        TURN_TERMINAL_ACTOR_JOURNAL_REPLAY,
        TURN_TERMINAL_ACTOR_CHECKPOINT_RECONCILE,
        TURN_TERMINAL_ACTOR_STALE_RECONCILE,
        TURN_TERMINAL_ACTOR_ENGINE_RECOVERY,
    }
)
_RECOVERY_TERMINAL_ACTORS = frozenset(
    {
        TURN_TERMINAL_ACTOR_RECOVERY_COORDINATOR,
        TURN_TERMINAL_ACTOR_JOURNAL_REPLAY,
        TURN_TERMINAL_ACTOR_ENGINE_RECOVERY,
    }
)

# AI SDK `finish` frames are overloaded: `tool-calls` closes the current
# model step / stream segment, while only `stop` is the normal turn terminal.
AI_SDK_FINISH_REASON_STOP = "stop"
AI_SDK_FINISH_REASON_TOOL_CALLS = "tool-calls"
AI_SDK_TURN_TERMINAL_FINISH_REASONS = frozenset({AI_SDK_FINISH_REASON_STOP})
SDK_RESPONSE_RESULT_BOUNDARY = "__sdk_response_boundary"


def is_terminal_data_result_frame(frame: Any) -> bool:
    """Distinguish a whole platform turn result from one SDK response result."""

    return (
        isinstance(frame, dict)
        and str(frame.get("type") or "").strip() == "data-result"
        and frame.get(SDK_RESPONSE_RESULT_BOUNDARY) is not True
    )


@dataclass(frozen=True)
class TurnTerminalAuthority:
    actor: str
    allowed: bool
    turn_id: str | None = None
    expected_conversation_state: str | None = None
    extra_filter: dict[str, Any] | None = None
    reason: str = ""

    def snapshot_update_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if self.expected_conversation_state is not None:
            kwargs["expected_conversation_state"] = self.expected_conversation_state
        if self.extra_filter is not None:
            kwargs["extra_filter"] = dict(self.extra_filter)
        return kwargs


#: Every event type that ends a turn, most direct evidence first. A turn the
#: coordinator settled from the mirror is as finished as one the engine closed
#: itself — ``turn.recovered`` is written through ``try_claim_event`` as durable
#: truth, so a reader that does not ask for it answers "not finished yet" about a
#: turn that finished, permanently. The order matters only when more than one
#: exists: a terminal the engine wrote outranks one reconstructed from the
#: mirror.
#:
#: Adding a terminal event type anywhere means adding it here: there is no
#: second place that answers this question.
_TERMINAL_EVENT_TYPES = ("turn.completed", "turn.failed", "turn.recovered")


async def journal_terminal_for_turn(
    journal_repo: Any,
    *,
    session_id: str,
    turn_id: str,
) -> dict[str, Any] | None:
    if not session_id or not turn_id:
        return None
    for event_type in _TERMINAL_EVENT_TYPES:
        events = await journal_repo.list_events(
            session_id,
            after_seq=0,
            channel="conversation",
            turn_id=turn_id,
            event_type=event_type,
            limit=50,
        )
        if events:
            return dict(events[-1])
    return None


def coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def normalize_current_turn_remote_anchor(raw: Any) -> dict[str, int] | None:
    if not isinstance(raw, dict):
        return None
    sandbox_turn_id = coerce_int(raw.get("sandbox_turn_id"))
    if sandbox_turn_id is None or sandbox_turn_id < 0:
        return None
    normalized: dict[str, int] = {
        "sandbox_turn_id": int(sandbox_turn_id),
    }
    last_sandbox_seq = coerce_int(raw.get("last_sandbox_seq"))
    if last_sandbox_seq is not None and last_sandbox_seq >= 0:
        normalized["last_sandbox_seq"] = int(last_sandbox_seq)
    return normalized


def normalize_current_turn_engine_anchor(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    engine_kind = str(raw.get("engine_kind") or "").strip()
    engine_turn_id = str(raw.get("engine_turn_id") or "").strip()
    if not engine_kind or not engine_turn_id:
        return None
    normalized: dict[str, Any] = {
        "engine_kind": engine_kind,
        "engine_turn_id": engine_turn_id,
    }
    engine_session_key = str(raw.get("engine_session_key") or "").strip()
    if engine_session_key:
        normalized["engine_session_key"] = engine_session_key
    sequence_number = coerce_int(
        raw["sequence_number"] if "sequence_number" in raw else raw.get("engine_sequence_number")
    )
    if sequence_number is not None and sequence_number >= 0:
        normalized["engine_sequence_number"] = int(sequence_number)
    return normalized


def resident_engine_turn_id(snapshot: dict[str, Any] | None) -> str | None:
    """The engine-owned response holding the conversation slot, if any.

    A platform turn in PROCESSING/STREAMING is owned by a worker command and
    anchored on the engine's own turn id, which is a different value from the
    platform turn id. A response the engine started on its own — a queued task
    notification answered with no input in flight — is projected with no
    worker command and with the response id as both the current turn and the
    engine anchor. That shape is the only one the platform reads as "active,
    but not admission": the dispatch gate lets a new input start its own turn
    beside it instead of refusing with SESSION_BUSY, and the resident observer
    that restores after a reconnect knows which response is still open.
    """

    if not isinstance(snapshot, dict):
        return None
    if _clean_text(snapshot.get("conversation_state")) not in {"PROCESSING", "STREAMING"}:
        return None
    turn_id = _clean_text(snapshot.get("current_turn_id"))
    if not turn_id or _clean_text(snapshot.get("current_turn_worker_command_id")):
        return None
    anchor = normalize_current_turn_engine_anchor(snapshot.get("current_turn_engine_anchor"))
    if anchor is None or anchor["engine_turn_id"] != turn_id:
        return None
    return turn_id


def normalize_turn_recovery_phase(raw: Any) -> str | None:
    text = str(raw or "").strip().upper()
    if not text:
        return None
    if text not in _VALID_TURN_RECOVERY_PHASES:
        return None
    return text


def normalize_turn_terminal_frame(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    turn_id = str(raw.get("turn_id") or "").strip()
    command_id = str(raw.get("command_id") or "").strip()
    frame_type = str(raw.get("type") or "").strip()
    if not turn_id or not command_id or frame_type not in {"finish", "error"}:
        return None
    finish_reason = str(raw.get("finish_reason") or raw.get("finishReason") or "").strip()
    if frame_type == "finish" and finish_reason not in AI_SDK_TURN_TERMINAL_FINISH_REASONS:
        return None
    frame_seq = coerce_int(raw.get("frame_seq"))
    if frame_seq is None or frame_seq < 0:
        return None
    normalized: dict[str, Any] = {
        "turn_id": turn_id,
        "command_id": command_id,
        "frame_seq": int(frame_seq),
        "type": frame_type,
    }
    if finish_reason:
        normalized["finish_reason"] = finish_reason
    return normalized


def normalize_live_source_cursor(raw: Any) -> dict[str, int] | None:
    if not isinstance(raw, dict):
        return None
    live_seq = coerce_int(raw["live_seq"] if "live_seq" in raw else raw.get("liveSeq"))
    sandbox_turn_id = coerce_int(
        raw["sandbox_turn_id"] if "sandbox_turn_id" in raw else raw.get("sandboxTurnId")
    )
    sandbox_seq = coerce_int(
        raw["sandbox_seq"] if "sandbox_seq" in raw else raw.get("sandboxSeq")
    )
    mirror_seq = coerce_int(
        raw["mirror_seq"] if "mirror_seq" in raw else raw.get("mirrorSeq")
    )
    normalized: dict[str, int] = {}
    if live_seq is not None and live_seq >= 0:
        normalized["live_seq"] = int(live_seq)
    if sandbox_turn_id is not None and sandbox_turn_id >= 0:
        normalized["sandbox_turn_id"] = int(sandbox_turn_id)
    if sandbox_seq is not None and sandbox_seq >= 0:
        normalized["sandbox_seq"] = int(sandbox_seq)
    if mirror_seq is not None and mirror_seq >= 0:
        normalized["mirror_seq"] = int(mirror_seq)
    return normalized or None


def turn_terminal_frame_matches(
    raw: Any,
    *,
    turn_id: str | None,
    command_id: str | None,
    frame_type: str = "finish",
    finish_reason: str | None = None,
) -> bool:
    proof = normalize_turn_terminal_frame(raw)
    if not isinstance(proof, dict):
        return False
    if proof.get("turn_id") != (str(turn_id or "").strip() or None):
        return False
    if proof.get("command_id") != (str(command_id or "").strip() or None):
        return False
    if proof.get("type") != frame_type:
        return False
    if finish_reason is not None and proof.get("finish_reason") != finish_reason:
        return False
    return True


async def find_recovery_finish_frame(
    session_events_repo: Any,
    *,
    session_id: str,
    turn_id: str,
    command_id: str,
    page_size: int = 500,
) -> dict[str, Any] | None:
    after_seq = -1
    while True:
        frames = await session_events_repo.list_frames(
            session_id,
            command_id=command_id,
            turn_id=turn_id,
            after_seq=after_seq,
            limit=page_size,
        )
        if not frames:
            return None

        max_seq = after_seq
        for frame in frames:
            frame_seq = coerce_int(frame.get("frame_seq"))
            if frame_seq is not None:
                max_seq = max(max_seq, frame_seq)
            payload = frame.get("payload")
            if not isinstance(payload, dict):
                continue
            proof = normalize_turn_terminal_frame(
                {
                    "turn_id": str(frame.get("turn_id") or "").strip(),
                    "command_id": command_id,
                    "frame_seq": frame.get("frame_seq"),
                    "type": payload.get("type"),
                    "finish_reason": payload.get("finishReason") or payload.get("finish_reason"),
                }
            )
            if turn_terminal_frame_matches(
                proof,
                turn_id=turn_id,
                command_id=command_id,
                frame_type="finish",
                finish_reason=AI_SDK_FINISH_REASON_STOP,
            ):
                return proof

        if len(frames) < page_size or max_seq <= after_seq:
            return None
        after_seq = max_seq


def derive_turn_recovery_phase(snapshot: dict[str, Any] | None) -> str | None:
    if not isinstance(snapshot, dict):
        return None
    return normalize_turn_recovery_phase(snapshot.get("turn_recovery_phase"))


def _clean_text(raw: Any) -> str:
    return str(raw or "").strip()


def _deny_terminal_authority(
    *,
    actor: str,
    reason: str,
    turn_id: str | None = None,
) -> TurnTerminalAuthority:
    return TurnTerminalAuthority(
        actor=actor,
        allowed=False,
        turn_id=_clean_text(turn_id) or None,
        reason=reason,
    )


def _allow_terminal_authority(
    *,
    actor: str,
    turn_id: str,
    expected_conversation_state: str | None,
    extra_filter: dict[str, Any] | None,
    reason: str,
) -> TurnTerminalAuthority:
    return TurnTerminalAuthority(
        actor=actor,
        allowed=True,
        turn_id=_clean_text(turn_id) or None,
        expected_conversation_state=_clean_text(expected_conversation_state) or None,
        extra_filter=dict(extra_filter) if isinstance(extra_filter, dict) else None,
        reason=reason,
    )


def active_turn_terminal_extra_filter(turn_id: str | None) -> dict[str, Any] | None:
    clean_turn_id = _clean_text(turn_id)
    if not clean_turn_id:
        return None
    return {"current_turn_id": clean_turn_id}


def resolve_live_worker_terminal_authority(
    *,
    turn_id: str | None,
) -> TurnTerminalAuthority:
    clean_turn_id = _clean_text(turn_id)
    if not clean_turn_id:
        return _deny_terminal_authority(
            actor=TURN_TERMINAL_ACTOR_LIVE_WORKER,
            reason="missing_turn_id",
        )
    return _allow_terminal_authority(
        actor=TURN_TERMINAL_ACTOR_LIVE_WORKER,
        turn_id=clean_turn_id,
        expected_conversation_state=None,
        extra_filter=active_turn_terminal_extra_filter(clean_turn_id),
        reason="live_worker_current_turn",
    )


def resolve_active_turn_terminal_authority(
    snapshot: dict[str, Any] | None,
    *,
    turn_id: str | None,
    actor: str,
) -> TurnTerminalAuthority:
    clean_actor = _clean_text(actor)
    clean_turn_id = _clean_text(turn_id)
    if clean_actor == TURN_TERMINAL_ACTOR_READ_RESUME:
        return _deny_terminal_authority(
            actor=clean_actor,
            turn_id=clean_turn_id,
            reason="read_path_has_no_terminal_write_authority",
        )
    if clean_actor not in _ACTIVE_TERMINAL_ACTORS:
        return _deny_terminal_authority(
            actor=clean_actor,
            turn_id=clean_turn_id,
            reason="actor_cannot_settle_active_turn",
        )
    if not isinstance(snapshot, dict):
        return _deny_terminal_authority(
            actor=clean_actor,
            turn_id=clean_turn_id,
            reason="missing_snapshot",
        )
    if not clean_turn_id:
        return _deny_terminal_authority(
            actor=clean_actor,
            reason="missing_turn_id",
        )
    conversation_state = _clean_text(snapshot.get("conversation_state"))
    if conversation_state not in _ACTIVE_TURN_TERMINAL_STATES:
        return _deny_terminal_authority(
            actor=clean_actor,
            turn_id=clean_turn_id,
            reason="snapshot_not_active_turn",
        )
    snapshot_turn_id = _clean_text(snapshot.get("current_turn_id"))
    if snapshot_turn_id != clean_turn_id:
        return _deny_terminal_authority(
            actor=clean_actor,
            turn_id=clean_turn_id,
            reason="current_turn_id_mismatch",
        )
    return _allow_terminal_authority(
        actor=clean_actor,
        turn_id=clean_turn_id,
        expected_conversation_state=conversation_state,
        extra_filter=active_turn_terminal_extra_filter(clean_turn_id),
        reason="active_current_turn",
    )


def resolve_transcript_recovery_terminal_authority(
    snapshot: dict[str, Any] | None,
    *,
    actor: str = TURN_TERMINAL_ACTOR_RECOVERY_COORDINATOR,
    turn_id: str | None = None,
    owner_dead: bool = False,
) -> TurnTerminalAuthority:
    clean_actor = _clean_text(actor)
    requested_turn_id = _clean_text(turn_id)
    if clean_actor == TURN_TERMINAL_ACTOR_READ_RESUME:
        return _deny_terminal_authority(
            actor=clean_actor,
            turn_id=requested_turn_id,
            reason="read_path_has_no_terminal_write_authority",
        )
    if clean_actor not in _RECOVERY_TERMINAL_ACTORS:
        return _deny_terminal_authority(
            actor=clean_actor,
            turn_id=requested_turn_id,
            reason="actor_cannot_create_recovery_terminal",
        )
    if not isinstance(snapshot, dict):
        return _deny_terminal_authority(
            actor=clean_actor,
            turn_id=requested_turn_id,
            reason="missing_snapshot",
        )
    if owner_dead:
        # The reconcile lane's owner-death verdict (stale worker heartbeat)
        # grants authority over the active turn directly: the writer that the
        # IDLE-lane shape below waits on is gone and will never settle it.
        # The terminal CAS still fences a zombie — it filters on the exact
        # PROCESSING/STREAMING state and turn observed here, so a revived
        # writer that settled first makes this write miss, not double-settle.
        conv_state = _clean_text(snapshot.get("conversation_state"))
        active_turn_id = _clean_text(snapshot.get("current_turn_id"))
        if (
            conv_state in ("PROCESSING", "STREAMING")
            and active_turn_id
            and (not requested_turn_id or requested_turn_id == active_turn_id)
        ):
            return _allow_terminal_authority(
                actor=clean_actor,
                turn_id=active_turn_id,
                expected_conversation_state=conv_state,
                extra_filter={
                    "current_turn_id": active_turn_id,
                },
                reason="owner_dead_active_turn",
            )
        return _deny_terminal_authority(
            actor=clean_actor,
            turn_id=requested_turn_id,
            reason="owner_dead_snapshot_not_active_on_turn",
        )
    if _clean_text(snapshot.get("conversation_state")) != "IDLE":
        return _deny_terminal_authority(
            actor=clean_actor,
            turn_id=requested_turn_id,
            reason="snapshot_not_idle",
        )
    if _clean_text(snapshot.get("last_turn_status")) != "FAILED":
        return _deny_terminal_authority(
            actor=clean_actor,
            turn_id=requested_turn_id,
            reason="last_turn_not_failed",
        )
    if derive_turn_recovery_phase(snapshot) != TURN_RECOVERY_PHASE_TRANSCRIPT_PENDING:
        return _deny_terminal_authority(
            actor=clean_actor,
            turn_id=requested_turn_id,
            reason="turn_recovery_phase_not_transcript_pending",
        )
    snapshot_turn_id = _clean_text(snapshot.get("last_turn_id"))
    if not snapshot_turn_id:
        return _deny_terminal_authority(
            actor=clean_actor,
            turn_id=requested_turn_id,
            reason="missing_last_turn_id",
        )
    if requested_turn_id and requested_turn_id != snapshot_turn_id:
        return _deny_terminal_authority(
            actor=clean_actor,
            turn_id=requested_turn_id,
            reason="last_turn_id_mismatch",
        )
    return _allow_terminal_authority(
        actor=clean_actor,
        turn_id=snapshot_turn_id,
        expected_conversation_state="IDLE",
        extra_filter={
            "current_turn_id": None,
            "last_turn_id": snapshot_turn_id,
        },
        reason="idle_failed_transcript_pending",
    )


def resolve_journal_terminal_replay_authority(
    snapshot: dict[str, Any] | None,
    *,
    turn_id: str | None,
) -> TurnTerminalAuthority:
    clean_turn_id = _clean_text(turn_id)
    active = resolve_active_turn_terminal_authority(
        snapshot,
        turn_id=clean_turn_id,
        actor=TURN_TERMINAL_ACTOR_JOURNAL_REPLAY,
    )
    if active.allowed:
        return active
    if not isinstance(snapshot, dict) or not clean_turn_id:
        return _deny_terminal_authority(
            actor=TURN_TERMINAL_ACTOR_JOURNAL_REPLAY,
            turn_id=clean_turn_id,
            reason=active.reason or "missing_snapshot_or_turn_id",
        )
    if (
        _clean_text(snapshot.get("conversation_state")) == "IDLE"
        and _clean_text(snapshot.get("last_turn_id")) == clean_turn_id
        and _clean_text(snapshot.get("last_turn_status")) in _TERMINAL_LAST_TURN_STATUSES
    ):
        return _allow_terminal_authority(
            actor=TURN_TERMINAL_ACTOR_JOURNAL_REPLAY,
            turn_id=clean_turn_id,
            expected_conversation_state="IDLE",
            extra_filter={
                "current_turn_id": None,
                "last_turn_id": clean_turn_id,
            },
            reason="idle_last_turn_terminal",
        )
    return _deny_terminal_authority(
        actor=TURN_TERMINAL_ACTOR_JOURNAL_REPLAY,
        turn_id=clean_turn_id,
        reason=active.reason or "journal_terminal_not_owned_by_snapshot_turn",
    )


def resolve_checkpoint_reconcile_terminal_authority(
    snapshot: dict[str, Any] | None,
    *,
    turn_id: str | None,
) -> TurnTerminalAuthority:
    return resolve_active_turn_terminal_authority(
        snapshot,
        turn_id=turn_id,
        actor=TURN_TERMINAL_ACTOR_CHECKPOINT_RECONCILE,
    )


def recoverable_transcript_turn_id(snapshot: dict[str, Any] | None) -> str | None:
    authority = resolve_transcript_recovery_terminal_authority(snapshot)
    return authority.turn_id if authority.allowed else None


def needs_turn_recovery(snapshot: dict[str, Any] | None) -> bool:
    if not isinstance(snapshot, dict):
        return False
    return resolve_transcript_recovery_terminal_authority(snapshot).allowed


def build_turn_active_snapshot_updates(
    *,
    conversation_state: str,
    turn_id: str | None,
    active_interaction_id: str | None = None,
    worker_command_id: str | None | object = _UNSET,
    current_turn_remote_anchor: dict[str, Any] | None | object = _UNSET,
    current_turn_engine_anchor: dict[str, Any] | None | object = _UNSET,
    delivery_state: str | None | object = _UNSET,
) -> dict[str, Any]:
    updates: dict[str, Any] = {
        "conversation_state": conversation_state,
        "current_turn_id": str(turn_id or "").strip() or None,
        "turn_recovery_phase": None,
        "active_interaction_id": str(active_interaction_id or "").strip() or None,
        "last_turn_failure_phase": None,
        "last_turn_terminal_reason": None,
        "last_turn_terminal_frame": None,
    }
    if worker_command_id is not _UNSET:
        updates["current_turn_worker_command_id"] = (
            str(worker_command_id or "").strip() or None
        )
    if current_turn_remote_anchor is not _UNSET:
        anchor = normalize_current_turn_remote_anchor(current_turn_remote_anchor)
        updates["current_turn_remote_anchor"] = (
            dict(anchor) if isinstance(anchor, dict) else None
        )
    if current_turn_engine_anchor is not _UNSET:
        engine_anchor = normalize_current_turn_engine_anchor(current_turn_engine_anchor)
        updates["current_turn_engine_anchor"] = (
            dict(engine_anchor) if isinstance(engine_anchor, dict) else None
        )
    if delivery_state is not _UNSET:
        updates["delivery_state"] = str(delivery_state).strip() if delivery_state else None
    return updates


def build_turn_waiting_snapshot_updates(
    *,
    turn_id: str | None,
    interaction_id: str | None,
    current_turn_remote_anchor: dict[str, Any] | None = None,
    current_turn_engine_anchor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    updates = build_turn_active_snapshot_updates(
        conversation_state="WAITING_FOR_INTERACTION",
        turn_id=turn_id,
        active_interaction_id=interaction_id,
        worker_command_id=None,
        current_turn_remote_anchor=current_turn_remote_anchor,
        current_turn_engine_anchor=current_turn_engine_anchor,
        delivery_state="RECEIVED",
    )
    updates.update(
        {
            "last_turn_id": None,
            "last_turn_status": None,
            "last_turn_error": None,
            "last_turn_command_id": None,
        }
    )
    return updates


async def append_settle_terminal_frame(
    *,
    session_events_repo: Any,
    session_id: str,
    turn_id: str,
    command_id: str | None,
) -> dict[str, Any] | None:
    """Write the terminal frame that tells attached clients the turn is over.

    A platform-side settle has two halves and they are not interchangeable.
    The snapshot is what a reload reads; this frame is what every client
    already streaming reads, and the start-turn gate reads it as proof the
    turn ended. Updating only the snapshot leaves an open page running
    forever against a session the database calls idle — a header that stays
    PROCESSING with nothing behind it.

    It needs a command id to be addressable, so a settle without one (a
    reclaim, which has no command) leaves the proof to the recovery lane
    rather than inventing an unaddressable frame. A failure here is logged
    and returns ``None``: the snapshot half must still land.
    """

    normalized_command_id = str(command_id or "").strip()
    if not normalized_command_id:
        return None
    try:
        terminal_frame_seq = int(
            await session_events_repo.get_next_session_frame_seq(session_id)
        )
        await session_events_repo.append_frame(
            {
                "session_id": session_id,
                "turn_id": turn_id,
                "command_id": normalized_command_id,
                "source_kind": "turn_recovery",
                "frame_seq": terminal_frame_seq,
                "payload": {
                    "type": "finish",
                    "finishReason": AI_SDK_FINISH_REASON_STOP,
                },
                "created_at": _utcnow_iso(),
            }
        )
    except Exception:
        logger.warning(
            "settle: could not persist the terminal frame session=%s turn=%s",
            session_id, turn_id, exc_info=True,
        )
        return None
    return {
        "turn_id": turn_id,
        "command_id": normalized_command_id,
        "frame_seq": terminal_frame_seq,
        "type": "finish",
        "finish_reason": AI_SDK_FINISH_REASON_STOP,
    }


async def settle_parked_turn(
    *,
    session_events_repo: Any,
    session_snapshots_repo: Any,
    interaction_snapshots_repo: Any,
    session_id: str,
    turn_id: str,
    command_id: str | None,
    status: str,
    failure_phase: str | None,
    error_text: str | None,
    causation: str,
) -> int | None:
    """Settle a turn parked at an interaction that cannot be answered.

    A parked turn's worker exited at the interaction boundary, so nothing is
    left to consume a terminal for it — whoever takes the interaction's
    runtime away settles the turn here: close the turn and deactivate the
    interaction. Monotonic-CAS'd via ``expected_conversation_state`` so a
    concurrent settle converges — every outcome is "turn stopped".

    This is a platform-side closure: no engine terminal was observed, so it
    cannot carry an engine terminal reason. Platform lifecycle failures use
    ``failure_phase``; a user interrupt is already recorded by the preceding
    ``turn.interrupt_requested`` event. A user stop still completes rather
    than fails the turn.

    Returns the settle event's seq (``None``-safe int).
    """
    settle_event = await session_events_repo.append_event(
        {
            "session_id": session_id,
            "channel": "conversation",
            "turn_id": turn_id,
            # The event follows the status, or the journal and the snapshot
            # would disagree about the same turn. Two terminals are the whole
            # vocabulary — it ended well or it did not — and the reason rides
            # in the payload, mirroring the vendor's success/error result plus
            # its separate terminal_reason.
            "event_type": (
                "turn.completed"
                if str(status or "").strip() == "COMPLETED"
                else "turn.failed"
            ),
            "causation_id": causation,
            "correlation_id": causation,
            "payload": {
                "command_id": command_id,
                "error_text": error_text,
                "terminal_reason": None,
                "failure_phase": failure_phase,
            },
        }
    )
    settle_seq = int(settle_event.get("event_seq") or 0)
    # A settled turn ends on a terminal frame, cancellation included: the
    # vendor still emits a result for an interrupted run, and the start-turn
    # gate reads this frame as the proof the turn is over. Written before the
    # snapshot that points at it. It needs a command id to be addressable, so
    # a settle without one (a reclaim, which has no command) leaves the proof
    # to the recovery lane rather than inventing an unaddressable frame.
    terminal_frame = await append_settle_terminal_frame(
        session_events_repo=session_events_repo,
        session_id=session_id,
        turn_id=turn_id,
        command_id=command_id,
    )
    result = await session_snapshots_repo.apply_channel_update(
        session_id,
        channel="conversation",
        event_seq=settle_seq,
        updates=build_turn_terminal_snapshot_updates(
            turn_id=turn_id,
            status=status,
            error_text=error_text,
            command_id=command_id,
            failure_phase=failure_phase,
            terminal_reason=None,
            terminal_frame=terminal_frame,
        ),
        expected_conversation_state="WAITING_FOR_INTERACTION",
    )
    deactivated = 0
    try:
        deactivated = await interaction_snapshots_repo.deactivate_active_for_turn(
            session_id, turn_id
        )
    except Exception:
        pass
    # Close the turn's open tool cards on the frame stream. The parked
    # segment ended with the approval outstanding, so nothing ever wrote a
    # tool-output for the held call — and a settled turn whose tool card
    # spins forever reads as live work (for example, an interrupt-on-pending
    # left a Write "working" on a READY session).
    # Closing the cards is part of settling; a failure here degrades the
    # rendering only, so it is logged loudly and never fails the settle.
    try:
        # Page through the frames rather than taking the first read:
        # list_frames is bounded (500) and sorted ascending, so one call on a
        # long turn returns its head — and the held tool call is always at the
        # tail. A single read leaves the card open on exactly the turns that
        # ran long enough to need closing.
        open_calls: dict[str, None] = {}
        cursor_seq = -1
        while True:
            page = await session_events_repo.list_frames(
                session_id, turn_id=turn_id, after_seq=cursor_seq
            )
            if not page:
                break
            for row in page:
                payload = row.get("payload") or {}
                frame_type = str(payload.get("type") or "")
                tool_call_id = str(payload.get("toolCallId") or "").strip()
                if not tool_call_id:
                    continue
                if frame_type in ("tool-input-start", "tool-input-available"):
                    open_calls.setdefault(tool_call_id, None)
                elif frame_type in (
                    "tool-output-available",
                    "tool-output-error",
                    "tool-output-denied",
                ):
                    open_calls.pop(tool_call_id, None)
            page_end = max(int(row.get("frame_seq") or 0) for row in page)
            if page_end <= cursor_seq:
                break
            cursor_seq = page_end
        for tool_call_id in open_calls:
            frame_seq = await session_events_repo.get_next_session_frame_seq(session_id)
            await session_events_repo.append_frame(
                {
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "command_id": command_id or causation,
                    "source_kind": "turn_recovery",
                    "frame_seq": int(frame_seq),
                    "payload": (
                        {
                            "type": "tool-output-denied",
                            "toolCallId": tool_call_id,
                        }
                        if error_text is None
                        else {
                            "type": "tool-output-error",
                            "toolCallId": tool_call_id,
                            "errorText": error_text,
                        }
                    ),
                    "created_at": _utcnow_iso(),
                }
            )
    except Exception:
        logger.warning(
            "parked-turn settle: could not close open tool frames "
            "session=%s turn=%s",
            session_id, turn_id, exc_info=True,
        )
    if isinstance(result, dict):
        logger.info(
            "parked-turn settle: waiting-interaction turn settled "
            "session=%s turn=%s causation=%s interactions_deactivated=%s",
            session_id, turn_id, causation, deactivated,
        )
    return settle_seq


def build_turn_terminal_snapshot_updates(
    *,
    turn_id: str | None,
    status: str | None,
    error_text: str | None,
    command_id: str | None = None,
    recovery_anchor: dict[str, Any] | None = None,
    recovery_engine_anchor: dict[str, Any] | None = None,
    active_interaction_id: str | None = None,
    last_turn_id: str | None = None,
    delivery_state: str | None = None,
    failure_phase: str | None = None,
    terminal_reason: str | None = None,
    terminal_frame: dict[str, Any] | None = None,
    force_transcript_pending: bool = False,
) -> dict[str, Any]:
    anchor = normalize_current_turn_remote_anchor(recovery_anchor)
    engine_anchor = normalize_current_turn_engine_anchor(recovery_engine_anchor)
    keep_recovery_anchor = (
        str(status or "").strip() == "FAILED"
        and isinstance(anchor, dict)
    )
    keep_engine_recovery_anchor = (
        str(status or "").strip() == "FAILED"
        and isinstance(engine_anchor, dict)
    )
    resolved_last_turn_id = (
        str(last_turn_id).strip()
        if last_turn_id is not None
        else str(turn_id or "").strip()
    ) or None
    return {
        "conversation_state": "IDLE",
        "current_turn_id": None,
        "current_turn_worker_command_id": None,
        # The interrupt mark is turn-scoped state on this snapshot; clearing
        # it here — atomically with the slot — is what keeps a stop aimed at
        # this turn from settling the next turn's park.
        "interrupt_requested": False,
        "current_turn_remote_anchor": dict(anchor) if keep_recovery_anchor else None,
        "current_turn_engine_anchor": (
            dict(engine_anchor) if keep_engine_recovery_anchor else None
        ),
        # An anchor derives the pending phase implicitly; force_transcript_pending
        # sets it for an anchor-less handoff — the turn coordinator can still
        # derive its recovery handle from the journal (dispatch.confirmed), and
        # a bridge that died before the first mirror row was observed has no
        # anchor to preserve. Only meaningful with status=FAILED.
        "turn_recovery_phase": (
            TURN_RECOVERY_PHASE_TRANSCRIPT_PENDING
            if (
                keep_recovery_anchor
                or keep_engine_recovery_anchor
                or (force_transcript_pending and str(status or "").strip() == "FAILED")
            )
            else None
        ),
        "active_interaction_id": str(active_interaction_id or "").strip() or None,
        "last_turn_id": resolved_last_turn_id,
        "last_turn_status": str(status or "").strip() or None,
        "last_turn_error": str(error_text or "").strip() or None,
        "last_turn_command_id": str(command_id or "").strip() or None,
        "last_turn_terminal_frame": normalize_turn_terminal_frame(terminal_frame),
        "delivery_state": str(delivery_state).strip() if delivery_state else None,
        "last_turn_failure_phase": str(failure_phase).strip() if failure_phase else None,
        # The engine's account of why its loop ended, in its own vocabulary
        # (the agent SDK's `terminal_reason`: completed / max_turns /
        # aborted_streaming / aborted_tools). Distinct from `failure_phase`,
        # which belongs to the session lifecycle — a turn can complete with a
        # cancellation reason and no failure phase at all.
        "last_turn_terminal_reason": str(terminal_reason).strip() if terminal_reason else None,
    }
