from __future__ import annotations

from typing import Any

TURN_RECOVERY_PHASE_TRANSCRIPT_PENDING = "TRANSCRIPT_PENDING"
AI_SDK_FINISH_REASON_STOP = "stop"
_OFFLINE_BLOCKING_CONVERSATION_STATES = frozenset(
    {"PROCESSING", "STREAMING", "INTERRUPTING"}
)
_TERMINAL_TURN_STATUSES = frozenset({"COMPLETED", "FAILED"})


def _normalize_turn_recovery_phase(raw: Any) -> str | None:
    text = str(raw or "").strip().upper()
    if text == TURN_RECOVERY_PHASE_TRANSCRIPT_PENDING:
        return text
    return None


def _coerce_nonnegative_int(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _normalize_turn_terminal_frame(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    turn_id = str(raw.get("turn_id") or "").strip()
    command_id = str(raw.get("command_id") or "").strip()
    frame_type = str(raw.get("type") or "").strip()
    if not turn_id or not command_id or frame_type not in {"finish", "error"}:
        return None
    finish_reason = str(raw.get("finish_reason") or raw.get("finishReason") or "").strip()
    if frame_type == "finish" and finish_reason != AI_SDK_FINISH_REASON_STOP:
        return None
    frame_seq = _coerce_nonnegative_int(raw.get("frame_seq"))
    if frame_seq is None:
        return None
    return {
        "turn_id": turn_id,
        "command_id": command_id,
        "frame_seq": frame_seq,
        "type": frame_type,
    }


def _current_turn_has_terminal_proof(
    snapshot: dict[str, Any],
    *,
    turn_id: str,
) -> bool:
    terminal_frame = _normalize_turn_terminal_frame(
        snapshot.get("last_turn_terminal_frame")
    )
    if (
        isinstance(terminal_frame, dict)
        and str(terminal_frame.get("turn_id") or "").strip() == turn_id
    ):
        return True
    last_turn_id = str(snapshot.get("last_turn_id") or "").strip()
    if last_turn_id != turn_id:
        return False
    return str(snapshot.get("last_turn_status") or "").strip() in _TERMINAL_TURN_STATUSES


def describe_offline_blocking_turn(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return blocking evidence only for a live/durable turn still advancing."""
    if not isinstance(snapshot, dict):
        return None
    conversation_state = str(snapshot.get("conversation_state") or "").strip()
    if conversation_state in _OFFLINE_BLOCKING_CONVERSATION_STATES:
        turn_id = str(snapshot.get("current_turn_id") or "").strip()
        if not turn_id:
            return None
        if _current_turn_has_terminal_proof(snapshot, turn_id=turn_id):
            return None
        return {
            "reason": "active_conversation_turn",
            "conversation_state": conversation_state,
            "turn_id": turn_id,
            "recovery_phase": _normalize_turn_recovery_phase(
                snapshot.get("turn_recovery_phase")
            ),
        }
    return None
