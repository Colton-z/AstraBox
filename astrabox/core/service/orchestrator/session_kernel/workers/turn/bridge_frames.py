"""Frame translation/accumulation + dispatch-confirm wiring for the bridge.

Module functions take ``(worker, state, ctx)`` plus their original parameters
(same convention as :mod:`bridge_journal`); each rebinds the ``ctx.*`` fields
it needs to local names at the top of the function body.

``_record_translated_frame`` accumulates browser frames only. Turn outcome is
carried by the typed engine terminal and written as the bridge's canonical
finish/error signal; a public result card is never an outcome authority.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from astrabox.persistence.repository.backend import is_mongo_transient_error
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.retry_utils import (
    build_retry_warning_before_sleep,
    retry_async_call,
)
from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    coerce_int as _coerce_int,
    is_terminal_data_result_frame,
    normalize_current_turn_remote_anchor as _normalize_current_turn_remote_anchor,
)
from astrabox.core.service.orchestrator.engine.frame_scope import (
    engine_frame_scope,
    engine_frame_turn_id,
)
from astrabox.core.service.orchestrator.tool_result_semantics import (
    AI_SDK_TOOL_OUTPUT_FRAME_TYPES,
    tool_result_block_from_ai_sdk_frame,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn import (
    bridge_anchors,
    bridge_journal,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn._helpers import (
    _build_current_turn_engine_anchor,
    _wm_max,
)

if TYPE_CHECKING:
    from astrabox.core.service.orchestrator.session_kernel.workers.turn.state import (
        _BridgeRunState,
    )

logger = get_logger(__name__)

# Reload closes the browser response at the following cursor, so its durable
# copy must deliver that cursor; live delivery can precede an older cursor.
_LIVE_DIRECT_SUPPRESSED_FRAME_TYPES = frozenset(
    {"finish", "error", "data-result", "data-session-store-reload"}
)


def _record_translated_frame(worker: Any, state: _BridgeRunState, ctx: Any, frame: dict[str, Any]) -> bool:
    ordered_assistant_segments = ctx.ordered_assistant_segments
    frame_type = str(frame.get("type") or "")
    if engine_frame_scope(frame) == "turn" and is_terminal_data_result_frame(frame):
        # UI presence is not terminal authority. It only prevents a later
        # typed terminal from replacing the supplier's complete result card.
        state.saw_public_result_frame = True
    if frame_type == "reasoning-delta":
        state.accumulated_thinking_parts.append(str(frame.get("delta") or ""))
    elif frame_type == "text-delta":
        delta = str(frame.get("delta") or "")
        state.last_assistant_text = f"{state.last_assistant_text}{delta}"
        if delta:
            text_id = str(frame.get("id") or "")
            last_segment = (
                ordered_assistant_segments[-1]
                if ordered_assistant_segments
                else None
            )
            if (
                last_segment is not None
                and last_segment.get("type") == "text"
                and last_segment.get("id") == text_id
            ):
                last_segment["text"] = (
                    str(last_segment.get("text") or "") + delta
                )
            else:
                ordered_assistant_segments.append(
                    {"type": "text", "id": text_id, "text": delta}
                )
    elif frame_type == "tool-input-available":
        tc_id = str(frame.get("toolCallId") or "")
        if tc_id:
            name = str(frame.get("toolName") or "")
            raw_input = frame.get("input")
            input_payload = (
                dict(raw_input) if isinstance(raw_input, dict) else {}
            )
            state.accumulated_tool_uses[tc_id] = {
                "name": name,
                "input": input_payload,
            }
            existing_seg = next(
                (
                    seg
                    for seg in ordered_assistant_segments
                    if seg.get("type") == "tool"
                    and seg.get("tc_id") == tc_id
                ),
                None,
            )
            if existing_seg is not None:
                if name and not existing_seg.get("name"):
                    existing_seg["name"] = name
                if input_payload and not existing_seg.get("input"):
                    existing_seg["input"] = input_payload
            else:
                ordered_assistant_segments.append(
                    {
                        "type": "tool",
                        "tc_id": tc_id,
                        "name": name,
                        "input": input_payload,
                        "result": None,
                    }
                )
    elif frame_type in AI_SDK_TOOL_OUTPUT_FRAME_TYPES:
        tool_result = tool_result_block_from_ai_sdk_frame(frame)
        if tool_result is not None:
            tc_id = str(tool_result.get("tool_use_id") or "")
            state.accumulated_tool_results[tc_id] = tool_result
            if tc_id:
                existing_seg = next(
                    (
                        seg
                        for seg in ordered_assistant_segments
                        if seg.get("type") == "tool"
                        and seg.get("tc_id") == tc_id
                    ),
                    None,
                )
                if existing_seg is None:
                    # tool-output arrived without a matching tool-input
                    # (e.g. recovery starting after the input frame);
                    # append a placeholder tool segment to keep the
                    # result in time order.
                    existing_seg = {
                        "type": "tool",
                        "tc_id": tc_id,
                        "name": "",
                        "input": {},
                        "result": None,
                    }
                    ordered_assistant_segments.append(existing_seg)
                existing_seg["result"] = dict(tool_result)
    return True


def _max_source_cursor_seq(worker: Any, state: _BridgeRunState, ctx: Any, frames: list[dict[str, Any]]) -> int | None:
    max_cursor_seq: int | None = None
    for frame in frames:
        cursor_seq = _coerce_int(frame.get("__source_sandbox_seq"))
        max_cursor_seq = _wm_max(max_cursor_seq, cursor_seq)
    return max_cursor_seq


async def _process_translated_frames(worker: Any, state: _BridgeRunState, ctx: Any, frames: list[dict[str, Any]]) -> None:
    durable_semantic_coalescer = ctx.durable_semantic_coalescer
    if not frames:
        return

    durable_ready_frames: list[dict[str, Any]] = []
    for raw_frame in frames:
        frame_doc = dict(raw_frame)
        frame_cursor_seq = _coerce_int(frame_doc.pop("__remote_cursor_seq", None))
        frame_mirror_seq = _coerce_int(frame_doc.pop("__remote_mirror_seq", None))
        if frame_cursor_seq is not None:
            source_anchor = _normalize_current_turn_remote_anchor(
                state.current_turn_remote_anchor
            )
            if isinstance(source_anchor, dict):
                frame_doc["__source_kind"] = "sandbox_transcript"
                frame_doc["__source_sandbox_turn_id"] = int(
                    source_anchor["sandbox_turn_id"]
                )
                frame_doc["__source_sandbox_seq"] = int(frame_cursor_seq)
        if frame_mirror_seq is not None:
            frame_doc["__source_kind"] = "transcript_mirror"
            frame_doc["__source_mirror_seq"] = int(frame_mirror_seq)
        if not _record_translated_frame(worker, state, ctx, frame_doc):
            continue
        frame_type = str(frame_doc.get("type") or "").strip()
        frame_scope = engine_frame_scope(frame_doc)
        publish_live = (
            frame_scope == "turn"
            and frame_type not in _LIVE_DIRECT_SUPPRESSED_FRAME_TYPES
        )
        if (
            frame_scope == "turn"
            and frame_type == "data-result"
            and frame_cursor_seq is not None
        ):
            publish_live = True
        if publish_live:
            live_seq = state.live_frame_seq
            state.live_frame_seq += 1
            frame_doc["__live_seq"] = live_seq
            await _publish_live_frame(worker, state, ctx,
                frame_doc,
                live_seq=live_seq,
                turn_id=engine_frame_turn_id(
                    frame_doc,
                    state.effective_turn_id or None,
                ),
            )
        durable_ready_frames.extend(durable_semantic_coalescer.ingest(frame_doc))

    if durable_ready_frames:
        await bridge_journal._enqueue_durable_frames(worker, state, ctx,
            [
                (
                    dict(frame_doc),
                    engine_frame_turn_id(
                        frame_doc,
                        state.effective_turn_id or None,
                    ),
                )
                for frame_doc in durable_ready_frames
            ],
            _max_source_cursor_seq(worker, state, ctx, durable_ready_frames),
        )


async def _run_live_frame_mongo_op(worker: Any, state: _BridgeRunState, ctx: Any,
    operation: str,
    op: Any,
    *,
    turn_id: str | None = None,
) -> Any:
    session_id = ctx.session_id
    return await retry_async_call(
        op,
        should_retry_exception=is_mongo_transient_error,
        max_delay_seconds=worker._live_frame_retry_window_s,
        wait_seconds=worker._live_frame_retry_delay_s,
        wait_exponential_max=2.0,
        before_sleep=build_retry_warning_before_sleep(
            logger,
            lambda retry_state, exc: (
                "session kernel live frame mongo retry session=%s turn_id=%s "
                "op=%s attempt=%s err=%s"
                % (
                    session_id,
                    str(turn_id or state.effective_turn_id or "").strip() or None,
                    operation,
                    retry_state.attempt_number,
                    exc,
                )
            ),
        ),
    )


async def _publish_broker_event(worker: Any, state: _BridgeRunState, ctx: Any, event: dict[str, Any]) -> None:
    session_id = ctx.session_id
    try:
        await worker._broker.publish(session_id, event)
    except Exception as exc:
        logger.warning(
            "session kernel broker publish failed session=%s event_type=%s err=%s",
            session_id,
            str(event.get("type") or "<unknown>"),
            exc,
        )


def _source_cursor_from_frame(worker: Any, state: _BridgeRunState, ctx: Any,
    frame: dict[str, Any],
    *,
    live_seq: int | None = None,
) -> dict[str, int] | None:
    cursor: dict[str, int] = {}
    normalized_live_seq = _coerce_int(live_seq)
    if normalized_live_seq is not None:
        cursor["live_seq"] = int(normalized_live_seq)
    source_sandbox_turn_id = _coerce_int(frame.get("__source_sandbox_turn_id"))
    source_sandbox_seq = _coerce_int(frame.get("__source_sandbox_seq"))
    source_mirror_seq = _coerce_int(frame.get("__source_mirror_seq"))
    if source_sandbox_turn_id is not None:
        cursor["sandbox_turn_id"] = int(source_sandbox_turn_id)
    if source_sandbox_seq is not None:
        cursor["sandbox_seq"] = int(source_sandbox_seq)
    if source_mirror_seq is not None:
        cursor["mirror_seq"] = int(source_mirror_seq)
    return cursor or None


def _current_live_source_cursor(worker: Any, state: _BridgeRunState, ctx: Any, *, live_seq: int | None = None) -> dict[str, int] | None:
    cursor: dict[str, int] = {}
    normalized_live_seq = _coerce_int(live_seq)
    if normalized_live_seq is not None:
        cursor["live_seq"] = int(normalized_live_seq)
    anchor = _normalize_current_turn_remote_anchor(state.current_turn_remote_anchor)
    if isinstance(anchor, dict):
        cursor["sandbox_turn_id"] = int(anchor["sandbox_turn_id"])
    if state.max_observed_sandbox_seq is not None:
        cursor["sandbox_seq"] = int(state.max_observed_sandbox_seq)
    return cursor or None


async def _publish_live_frame(worker: Any, state: _BridgeRunState, ctx: Any,
    frame: dict[str, Any],
    *,
    live_seq: int,
    turn_id: str | None,
) -> None:
    session_id = ctx.session_id
    command_id = ctx.command_id
    source_cursor = _source_cursor_from_frame(worker, state, ctx, frame, live_seq=live_seq)
    payload_doc = {
        key: value
        for key, value in frame.items()
        if not str(key).startswith("__")
    }
    event = {
        "type": "ai_sdk_live_frame",
        "command_id": command_id,
        "turn_id": turn_id,
        "live_seq": int(live_seq),
        "payload": payload_doc,
    }
    if isinstance(source_cursor, dict):
        event["source_cursor"] = source_cursor
    try:
        await worker._broker.publish(session_id, event)
    except Exception:
        logger.exception(
            "session kernel live broker publish failed session=%s turn=%s live_seq=%s",
            session_id,
            turn_id,
            live_seq,
        )
        raise


async def _ensure_frame_seq_initialized(worker: Any, state: _BridgeRunState, ctx: Any, turn_id: str | None) -> None:
    if state.frame_seq is None:
        raise RuntimeError("frame sequence range must be reserved before persistence")


async def _on_query_committed(worker: Any, state: _BridgeRunState, ctx: Any, dispatch_anchor: dict[str, Any]) -> None:
    """Write dispatch.confirmed after turn_service observes sandbox dispatch receipt."""
    session_id = ctx.session_id
    command_id = ctx.command_id
    correlation_id = ctx.correlation_id
    session = ctx.session
    latency_trace = ctx.latency_trace
    state.dispatch_confirmed = True
    if state.dispatch_event_written:
        return
    normalized_dispatch_anchor = _normalize_current_turn_remote_anchor(dispatch_anchor)
    engine_dispatch_anchor = _build_current_turn_engine_anchor(dispatch_anchor)
    if isinstance(engine_dispatch_anchor, dict):
        await bridge_anchors._observe_engine_anchor(worker, state, ctx, engine_dispatch_anchor)
    dispatch_event = await worker._session_events_repo.append_event(
        {
            "session_id": session_id,
            "channel": "conversation",
            "turn_id": state.effective_turn_id or None,
            "event_type": "dispatch.confirmed",
            "causation_id": command_id,
            "correlation_id": correlation_id,
            "payload": {
                "command_id": command_id,
                "recovery_context": {
                    "sandbox_id": str(session.get("sandbox_id") or "").strip() or None,
                    "remote_anchor": (
                        dict(normalized_dispatch_anchor)
                        if normalized_dispatch_anchor
                        else None
                    ),
                    "engine_anchor": (
                        dict(engine_dispatch_anchor)
                        if isinstance(engine_dispatch_anchor, dict)
                        else None
                    ),
                },
            },
        }
    )
    state.dispatch_event_written = True
    latency_trace.mark("turn_worker.dispatch_journal_written")
    latency_trace.log("dispatch_confirmed")
    if ctx.interaction_response:
        # The answer continuation (an AnswerInteraction command re-entering
        # the parked engine stream) confirms its dispatch here, so the answer
        # projection must run here too: it settles the interaction snapshot
        # to ANSWERED and appends the tool-approval-response frame. Without
        # it the interaction row stays OPEN/active forever and the session
        # projection keeps synthesizing WAITING_INPUT after the turn
        # completed.
        logger.info(
            "answer projection: running at dispatch confirmation session=%s",
            ctx.session_id,
        )
        await ctx.project_answer_after_dispatch_confirmed()


def _on_turn_service_timing(worker: Any, state: _BridgeRunState, ctx: Any, stage: str, payload: dict[str, Any]) -> None:
    latency_trace = ctx.latency_trace
    fields: dict[str, Any] = {}
    for key, value in dict(payload).items():
        if key == "stage":
            continue
        if key == "elapsed_ms":
            fields["turn_service_elapsed_ms"] = value
        elif key == "delta_ms":
            fields["turn_service_delta_ms"] = value
        else:
            fields[key] = value
    latency_trace.mark(stage, **fields)
