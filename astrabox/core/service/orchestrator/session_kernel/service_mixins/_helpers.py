from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from astrabox.persistence.repository._compat import DuplicateKeyError, WriteError
from astrabox.common.utils.time_utils import parse_iso
from astrabox.core.service.orchestrator.engine.frame_scope import (
    EngineFrameScope,
    public_engine_frame_payload,
)
from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    AI_SDK_FINISH_REASON_STOP,
    AI_SDK_TURN_TERMINAL_FINISH_REASONS,
    SDK_RESPONSE_RESULT_BOUNDARY,
    coerce_int as _coerce_int,
    normalize_live_source_cursor,
    normalize_turn_terminal_frame,
    turn_terminal_frame_matches,
)
from astrabox.core.service.orchestrator.session_kernel.workers.reconcile_worker import (
    SCAN_INTERVAL_S as _RECONCILE_SCAN_INTERVAL_S,
)

_ACTIVE_SESSION_STATES = {"BUSY", "INTERRUPTING", "PROCESSING"}
_ACTIVE_CONVERSATION_SNAPSHOT_STATES = {
    "PROCESSING",
    "STREAMING",
    "INTERRUPTING",
    "WAITING_FOR_INTERACTION",
}
_ACTIVE_TERMINAL_SNAPSHOT_STATES = {"RUNNING", "INTERRUPTING"}
_MIRROR_SEQ_ENTRY_FIELD = "__astrabox_mirror_seq"


def _is_duplicate_frame_error(exc: Exception) -> bool:
    if isinstance(exc, DuplicateKeyError):
        return True
    if isinstance(exc, WriteError):
        return "Duplicate entry" in str(exc) or exc.code in (1, 11000)
    return False


def _build_resume_cursor_payload(
    *,
    frame_seq: int,
    turn_id: str | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {"frameSeq": int(frame_seq)}
    if turn_id:
        data["turnId"] = turn_id
    return {
        "type": "data-resume-cursor",
        "transient": True,
        "data": data,
    }


_STALE_PROCESSING_THRESHOLD_SECONDS = 300
_STALE_NO_ANCHOR_THRESHOLD_SECONDS = 30
_ORPHANED_ANSWER_REPLAY_GRACE_SECONDS = max(5.0, float(_RECONCILE_SCAN_INTERVAL_S))
# A conversation whose runtime subject is unavailable re-enters startup on the
# next message. The message path waits up to this budget for the subject to
# publish a dispatchable binding before sending the turn.
_RUNTIME_SUBJECT_REBUILD_READY_BUDGET_SECONDS = 180.0
_RUNTIME_SUBJECT_REBUILD_READY_POLL_SECONDS = 2.0


def _is_snapshot_stale(snapshot: dict[str, Any], *, threshold_seconds: int = _STALE_PROCESSING_THRESHOLD_SECONDS) -> bool:
    updated_at = snapshot.get("updated_at")
    if not isinstance(updated_at, str) or not updated_at:
        return False
    try:
        ts = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return (now - ts).total_seconds() > threshold_seconds
    except (ValueError, TypeError):
        return False


def _journal_event_age_seconds(event: dict[str, Any]) -> float | None:
    occurred_at = str((event or {}).get("occurred_at") or "").strip()
    if not occurred_at:
        return None
    try:
        now = datetime.now(timezone.utc)
        return max(0.0, (now - parse_iso(occurred_at)).total_seconds())
    except (ValueError, TypeError):
        return None


class _ResumeCursorTracker:
    def __init__(self, *, response_messages: bool = False) -> None:
        self.response_messages = response_messages
        self._pending_message_start: dict[str, Any] | None = None
        self._message_started = False
        self._open_text_ids: set[tuple[str, str]] = set()
        self._open_reasoning_ids: set[tuple[str, str]] = set()
        self._open_tool_input_ids: set[tuple[str, str]] = set()
        # A complete tool input is not yet a safe replay boundary. Approval,
        # interaction and output frames refer back to that invocation. Keep
        # the cursor before tool-input-start until the invocation resolves so
        # reconnect always reconstructs the dependency first.
        self._unresolved_tool_ids: set[tuple[str, str]] = set()

    def message_payloads(
        self, payload: dict[str, Any], *, turn_id: str | None
    ) -> list[dict[str, Any]]:
        """Open the reader's message at consumption, not dispatch acknowledgement."""
        if not self.response_messages:
            return [payload]
        payload_type = payload.get("type")
        if payload_type == "start":
            self._pending_message_start = payload
            return []
        if payload_type == "data-turn-accepted":
            return [{**payload, "transient": True}]
        if payload_type == "data-input-consumed":
            data = payload.get("data")
            response_id = str(data.get("responseMessageId") or "").strip() if isinstance(data, dict) else ""
            if not response_id or not turn_id:
                raise ValueError("input consumption is missing response or turn identity")
            metadata = dict((self._pending_message_start or {}).get("messageMetadata") or {})
            metadata["turn_id"] = turn_id
            self._message_started = True
            self._pending_message_start = None
            return [
                {"type": "start", "messageId": response_id, "messageMetadata": metadata},
                payload,
            ]
        if (
            not self._message_started
            and self._pending_message_start is not None
            and payload.get("transient") is not True
        ):
            self._message_started = True
            return [self._pending_message_start, payload]
        return [payload]

    def observe(self, payload: dict[str, Any], *, scope: str | None = None) -> None:
        payload_type = str(payload.get("type") or "").strip()
        if payload_type == "text-start":
            self._remember(self._open_text_ids, payload.get("id"), scope)
            return
        if payload_type == "text-end":
            self._forget(self._open_text_ids, payload.get("id"), scope)
            return
        if payload_type == "reasoning-start":
            self._remember(self._open_reasoning_ids, payload.get("id"), scope)
            return
        if payload_type == "reasoning-end":
            self._forget(self._open_reasoning_ids, payload.get("id"), scope)
            return
        if payload_type == "tool-input-start":
            self._remember(self._open_tool_input_ids, payload.get("toolCallId"), scope)
            self._remember(self._unresolved_tool_ids, payload.get("toolCallId"), scope)
            return
        if payload_type in {"tool-input-delta", "tool-input-end"}:
            # Normal replay suppresses an orphan delta before observe().  The
            # durable-counterpart path deliberately calls observe() directly,
            # however, so a lost live start still has to make this invocation
            # an unsafe cursor boundary.
            self._remember(self._unresolved_tool_ids, payload.get("toolCallId"), scope)
            return
        if payload_type == "tool-input-available":
            self._forget(self._open_tool_input_ids, payload.get("toolCallId"), scope)
            self._remember(self._unresolved_tool_ids, payload.get("toolCallId"), scope)
            return
        if payload_type == "tool-approval-request":
            self._remember(self._unresolved_tool_ids, payload.get("toolCallId"), scope)
            return
        if payload_type == "tool-input-error":
            self._forget(self._open_tool_input_ids, payload.get("toolCallId"), scope)
            self._forget(self._unresolved_tool_ids, payload.get("toolCallId"), scope)
            return
        if payload_type in {
            "tool-output-available",
            "tool-output-error",
            "tool-output-denied",
        }:
            self._forget(self._unresolved_tool_ids, payload.get("toolCallId"), scope)
            return
        if payload_type == "finish-step":
            self._clear_scope(self._open_text_ids, scope)
            self._clear_scope(self._open_reasoning_ids, scope)
            return
        if payload_type == "finish":
            self._clear_scope(self._open_text_ids, scope)
            self._clear_scope(self._open_reasoning_ids, scope)
            self._clear_scope(self._open_tool_input_ids, scope)
            finish_reason = str(
                payload.get("finishReason") or payload.get("finish_reason") or ""
            ).strip()
            if finish_reason != "tool-calls":
                self._clear_scope(self._unresolved_tool_ids, scope)
            return
        if payload_type == "error":
            self._clear_scope(self._open_text_ids, scope)
            self._clear_scope(self._open_reasoning_ids, scope)
            self._clear_scope(self._open_tool_input_ids, scope)
            self._clear_scope(self._unresolved_tool_ids, scope)

    def can_resume_after_current_frame(self) -> bool:
        # Text / reasoning resumes are safe mid-block because resume playback
        # synthesizes the missing *-start frame before the next delta/end. Tool
        # input remains boundary-only: resuming in the middle of partial JSON
        # would lose already-emitted input fragments. A completed input also
        # remains unresolved until its output/denial because approval and
        # interaction frames depend on the invocation already being present.
        return not self._open_tool_input_ids and not self._unresolved_tool_ids

    def should_suppress(self, payload: dict[str, Any], *, scope: str | None = None) -> bool:
        """Return True if *payload* is an orphan delta that must not be forwarded."""
        payload_type = str(payload.get("type") or "").strip()
        if payload_type in ("tool-input-delta", "tool-input-end"):
            tcid = str(payload.get("toolCallId") or "").strip()
            if not tcid or self._key(tcid, scope) not in self._open_tool_input_ids:
                return True
        return False

    def synthesize_missing_starts(
        self,
        payload: dict[str, Any],
        *,
        scope: str | None = None,
    ) -> list[dict[str, Any]]:
        """If *payload* is a delta/end for a block whose start was never seen,
        return synthetic start frames that must be emitted before it."""
        payload_type = str(payload.get("type") or "").strip()
        synthesized: list[dict[str, Any]] = []

        if payload_type in ("reasoning-delta", "reasoning-end"):
            bid = str(payload.get("id") or "").strip()
            if bid and self._key(bid, scope) not in self._open_reasoning_ids:
                synthesized.append({"type": "reasoning-start", "id": bid})
                self._remember(self._open_reasoning_ids, bid, scope)

        elif payload_type in ("text-delta", "text-end"):
            bid = str(payload.get("id") or "").strip()
            if bid and self._key(bid, scope) not in self._open_text_ids:
                synthesized.append({"type": "text-start", "id": bid})
                self._remember(self._open_text_ids, bid, scope)

        return synthesized

    @staticmethod
    def _key(raw_value: Any, scope: str | None) -> tuple[str, str]:
        return (str(scope or "").strip(), str(raw_value or "").strip())

    @classmethod
    def _remember(
        cls,
        target: set[tuple[str, str]],
        raw_value: Any,
        scope: str | None,
    ) -> None:
        value = str(raw_value or "").strip()
        if value:
            target.add(cls._key(value, scope))

    @classmethod
    def _forget(
        cls,
        target: set[tuple[str, str]],
        raw_value: Any,
        scope: str | None,
    ) -> None:
        value = str(raw_value or "").strip()
        if value:
            target.discard(cls._key(value, scope))

    @staticmethod
    def _clear_scope(target: set[tuple[str, str]], scope: str | None) -> None:
        normalized_scope = str(scope or "").strip()
        # ``difference_update`` mutates ``target`` as it consumes its input.
        # Feeding it a generator over that same set raises ``RuntimeError: Set
        # changed size during iteration`` as soon as a finish/error closes a
        # non-empty block.  Session followers then disconnect before emitting
        # the durable cursor and reconnect from the same old interaction
        # frame forever.  Materialize the removal set first so iteration and
        # mutation are separate operations.
        target.difference_update({
            item for item in target if item[0] == normalized_scope
        })


def _emit_resumable_payloads(
    *,
    cursor_tracker: _ResumeCursorTracker,
    payload: dict[str, Any],
    frame_seq: int | None = None,
    payload_scope: EngineFrameScope,
    payload_turn_id: str | None = None,
    include_resume_cursor: bool = False,
    include_terminal_resume_cursor: bool = True,
) -> tuple[list[dict[str, Any]], bool]:
    if cursor_tracker.should_suppress(payload, scope=payload_turn_id):
        return [], False
    payload_type = str(payload.get("type") or "")
    response_boundary = (
        cursor_tracker.response_messages
        and payload.get(SDK_RESPONSE_RESULT_BOUNDARY) is True
    )
    payload_is_terminal = payload_type in {"finish", "error"} or response_boundary
    synthetic_starts = cursor_tracker.synthesize_missing_starts(
        payload,
        scope=payload_turn_id,
    )
    cursor_tracker.observe(payload, scope=payload_turn_id)
    public_payload = public_engine_frame_payload(
        payload,
        frame_seq=frame_seq,
        scope=payload_scope,
    )
    emitted: list[dict[str, Any]] = list(synthetic_starts)
    if public_payload is not None:
        emitted.extend(cursor_tracker.message_payloads(public_payload, turn_id=payload_turn_id))
    if response_boundary:
        emitted.append({
            "type": "finish",
            "finishReason": AI_SDK_FINISH_REASON_STOP,
            "messageMetadata": {"turn_id": payload_turn_id, "response_boundary": True},
        })
    if (
        include_resume_cursor
        and frame_seq is not None
        and (include_terminal_resume_cursor or not payload_is_terminal)
        and cursor_tracker.can_resume_after_current_frame()
    ):
        cursor = _build_resume_cursor_payload(
            frame_seq=frame_seq,
            turn_id=payload_turn_id,
        )
        # The AI SDK raises as soon as it reads an error chunk, so data after
        # that chunk is not observable. Advance through the durable error first;
        # clean finish cursors remain after finish and are consumed normally.
        if payload_type == "error":
            emitted.insert(len(synthetic_starts), cursor)
        else:
            emitted.append(cursor)
    return emitted, payload_is_terminal


def _payload_is_turn_terminal(payload: dict[str, Any]) -> bool:
    payload_type = str(payload.get("type") or "").strip()
    if payload_type == "error":
        return True
    if payload_type != "finish":
        return False
    finish_reason = str(
        payload.get("finishReason") or payload.get("finish_reason") or ""
    ).strip()
    return finish_reason in AI_SDK_TURN_TERMINAL_FINISH_REASONS


def _completed_turn_has_terminal_frame_proof(snapshot: dict[str, Any] | None) -> bool:
    if not isinstance(snapshot, dict):
        return False
    if str(snapshot.get("last_turn_status") or "").strip() != "COMPLETED":
        return True
    last_turn_id = str(snapshot.get("last_turn_id") or "").strip() or None
    if not last_turn_id:
        return True
    last_turn_command_id = str(snapshot.get("last_turn_command_id") or "").strip() or None
    return turn_terminal_frame_matches(
        snapshot.get("last_turn_terminal_frame"),
        turn_id=last_turn_id,
        command_id=last_turn_command_id,
        frame_type="finish",
        finish_reason=AI_SDK_FINISH_REASON_STOP,
    )


def _live_terminal_proof_matches(
    raw: Any,
    *,
    turn_id: str | None,
    command_id: str | None,
    frame_type: str = "finish",
    finish_reason: str | None = "stop",
) -> bool:
    if turn_terminal_frame_matches(
        raw,
        turn_id=turn_id,
        command_id=command_id,
        frame_type=frame_type,
        finish_reason=finish_reason,
    ):
        return True
    if not isinstance(raw, dict):
        return False
    expected_turn_id = str(turn_id or "").strip()
    expected_command_id = str(command_id or "").strip()
    proof_turn_id = str(raw.get("turn_id") or raw.get("turnId") or "").strip()
    proof_command_id = str(raw.get("command_id") or raw.get("commandId") or "").strip()
    proof_type = str(raw.get("type") or "").strip()
    proof_finish_reason = str(raw.get("finish_reason") or raw.get("finishReason") or "").strip()
    if not expected_turn_id or proof_turn_id != expected_turn_id:
        return False
    if expected_command_id and proof_command_id != expected_command_id:
        return False
    if proof_type != frame_type:
        return False
    if finish_reason is not None and proof_finish_reason != finish_reason:
        return False
    live_seq = _coerce_int(raw.get("live_seq"))
    if live_seq is None:
        live_seq = _coerce_int(raw.get("liveSeq"))
    frame_seq = _coerce_int(raw.get("frame_seq"))
    if frame_seq is None:
        frame_seq = _coerce_int(raw.get("frameSeq"))
    return (live_seq is not None and live_seq >= 0) or (
        frame_seq is not None and frame_seq >= 0
    )


def _has_authoritative_terminal_conversation_state(snapshot: dict[str, Any] | None) -> bool:
    if not isinstance(snapshot, dict):
        return False
    if str(snapshot.get("conversation_state") or "").strip() != "IDLE":
        return False
    if str(snapshot.get("current_turn_id") or "").strip():
        return False
    if str(snapshot.get("active_interaction_id") or "").strip():
        return False
    if str(snapshot.get("last_turn_status") or "").strip() not in {"COMPLETED", "FAILED"}:
        return False
    return _completed_turn_has_terminal_frame_proof(snapshot)
