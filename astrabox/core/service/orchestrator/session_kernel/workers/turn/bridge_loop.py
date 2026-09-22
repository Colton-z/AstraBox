"""Bridge consumer loop + snapshot-CAS heartbeat fencing.

This module holds the ``_run_bridge_command`` orchestrator (the while-True
consumer dispatch table, terminal-settle ladder, and try/except/finally task
lifecycle) plus the heartbeat and main-consumer-loop helper functions.
Module-level functions take ``(worker, state, ctx)`` (same convention as
:mod:`bridge_frames`).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections import deque
from types import SimpleNamespace
from typing import Any

from astrabox.common.fault_injection import consume_fault, pass_fault_barrier
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.common.utils.user_context import UserContext
from astrabox.core.model import SessionState
from astrabox.core.service.orchestrator.engine.base import EngineStreamDetached
from astrabox.core.service.orchestrator.engine.input_delivery import (
    confirm_engine_input_consumed,
    consumption_carrier,
)
from astrabox.core.service.orchestrator.engine.capabilities import (
    resolve_session_capabilities,
)
from astrabox.core.service.orchestrator.engine.interaction_contract import (
    build_tool_approval_request_frame,
    build_tool_approval_response_frame,
)
from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    AI_SDK_FINISH_REASON_STOP,
    AI_SDK_FINISH_REASON_TOOL_CALLS,
    coerce_int as _coerce_int,
    normalize_current_turn_engine_anchor as _normalize_current_turn_engine_anchor,
    normalize_current_turn_remote_anchor as _normalize_current_turn_remote_anchor,
    turn_terminal_frame_matches,
)
from astrabox.core.service.orchestrator.stream_errors import (
    IncompleteStreamError,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn import (
    bridge_anchors,
    bridge_frames,
    bridge_journal,
    bridge_resume,
    bridge_terminal,
)
from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    SDK_RESPONSE_RESULT_BOUNDARY,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn._fencing import (
    _TurnFencedOut,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn._helpers import (
    _build_current_turn_engine_anchor,
    _callable_accepts_keyword,
    _extract_current_turn_remote_anchor,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn._latency import (
    _TurnLatencyTrace,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn._replay import (
    _DurableSemanticFrameCoalescer,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn.state import (
    _BridgeRunState,
)

logger = get_logger(__name__)

def _build_ack_control_frames(
    turn_id: str,
    client_message_id: str | None,
) -> list[dict[str, Any]]:
    platform_turn_id = str(turn_id or "").strip()
    if not platform_turn_id:
        raise RuntimeError("engine dispatch ack is missing turn_id")
    frames: list[dict[str, Any]] = [
        {
            "type": "start",
            "messageId": platform_turn_id,
            "messageMetadata": {"turn_id": platform_turn_id},
        }
    ]
    normalized_client_message_id = str(client_message_id or "").strip()
    if normalized_client_message_id:
        frames.append(
            {
                "type": "data-turn-accepted",
                "data": {
                    "clientMessageId": normalized_client_message_id,
                    "turnId": platform_turn_id,
                },
            }
        )
    return frames


async def _confirm_engine_input_consumed(
    worker: Any,
    state: _BridgeRunState,
    ctx: Any,
    frame: dict[str, Any],
    *,
    consumer_carrier: str | None = None,
) -> None:
    data = frame.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("SDK input consumption frame data is malformed")
    input_id = str(data.get("inputId") or "").strip()
    response_message_id = str(data.get("responseMessageId") or "").strip()
    content = data.get("content")
    if not input_id or not response_message_id or not isinstance(content, str):
        raise RuntimeError("SDK input consumption frame is incomplete")

    consumed = await confirm_engine_input_consumed(
        worker._session_events_repo,
        session_id=ctx.session_id,
        input_id=input_id,
        response_message_id=response_message_id,
        content=content,
        consumer_carrier=consumer_carrier,
    )
    consumed_payload = consumed.get("payload")
    accepted_content = (
        consumed_payload.get("content")
        if isinstance(consumed_payload, dict)
        else None
    )
    if not isinstance(accepted_content, str):
        raise RuntimeError("SDK input consumption has no accepted content")
    # Claude Code rewrites slash commands into its command envelope while
    # retaining the submitted UUID. The transcript keeps that SDK content;
    # the platform message projects the operator-authored accepted input.
    data["content"] = accepted_content
    accepted_blocks = (
        consumed_payload.get("content_blocks")
        if isinstance(consumed_payload, dict)
        else None
    )
    if accepted_blocks:
        # Same reason as the content above: the engine's prompt boundary
        # reports the text, and the journal is what knows an image came with
        # it. Attaching it here means the live message shows what a reload
        # would show, rather than the picture appearing only on the way back.
        data["contentBlocks"] = accepted_blocks
    client_message_id = str(
        (consumed_payload or {}).get("client_message_id")
        if isinstance(consumed_payload, dict)
        else ""
    ).strip()
    if client_message_id:
        # The browser id is platform delivery metadata, not SDK vocabulary.
        # Carry it only after the journal has proved the UUID correlation so a
        # hook frame that races (or outlives) its HTTP receipt can still clear
        # the exact optimistic outbox row.
        data["clientMessageId"] = client_message_id
    consumed_event_seq = int(consumed.get("event_seq") or 0)
    state.last_event_seq = max(
        state.last_event_seq,
        consumed_event_seq,
    )


async def _write_heartbeat(worker: Any, state: _BridgeRunState, ctx: Any, *, raise_on_fence: bool) -> bool:
    session_id = ctx.session_id
    command_id = ctx.command_id
    updated = await worker._session_snapshots_repo.force_update_fields(
        session_id,
        {"worker_heartbeat_at": utcnow_iso()},
        extra_filter=(
            {
                "current_turn_id": state.effective_turn_id,
                "current_turn_worker_command_id": command_id,
            }
            if state.effective_turn_id
            else None
        ),
    )
    if state.effective_turn_id and not updated:
        if raise_on_fence:
            raise _TurnFencedOut(state.effective_turn_id)
        return False
    state._last_heartbeat_mono = time.monotonic()
    return updated


async def _maybe_write_heartbeat(worker: Any, state: _BridgeRunState, ctx: Any) -> None:
    now = time.monotonic()
    if now - state._last_heartbeat_mono >= worker._worker_heartbeat_interval_s:
        await _write_heartbeat(worker, state, ctx, raise_on_fence=True)


def _bridge_done_before_terminal(worker: Any, state: _BridgeRunState, ctx: Any) -> bool:
    return (
        state.bridge_stream_task is not None
        and state.bridge_stream_task.done()
        and not state.turn_settled
        and not state.frame_hold_active
    )


async def _project_answer_after_dispatch_confirmed(worker: Any, state: _BridgeRunState, ctx: Any) -> None:
    session_id = ctx.session_id
    command_event = ctx.command_event
    payload = ctx.payload
    if state.answer_projection_written:
        return
    event_seq = await worker._project_answer_persisted(
        session_id=session_id,
        turn_id=state.effective_turn_id or None,
        command_event=command_event,
        payload=payload,
    )
    state.last_event_seq = max(state.last_event_seq, int(event_seq or 0))
    state.answer_projection_written = True
    await _append_answered_tool_approval_frame(worker, state, ctx)


async def _append_answered_tool_approval_frame(worker: Any, state: _BridgeRunState, ctx: Any) -> None:
    """Project the accepted answer onto the native approval stream.

    The answer projection is the durable authority; the
    ``tool-approval-response`` frame is its stream projection so
    replaying and concurrently attached clients resolve the pending
    approval before the tool output settles. Emitted once per
    approval id: a worker retry that re-runs the answer command finds
    the durable frame and skips the append.
    """
    interaction_response = ctx.interaction_response
    if not isinstance(state.pending_interaction, dict):
        return
    frame = build_tool_approval_response_frame(
        state.pending_interaction,
        dict(interaction_response or {}),
    )
    if frame is None:
        return
    if await bridge_terminal._turn_has_durable_tool_approval_response(worker, state, ctx,
        str(frame["approvalId"])
    ):
        return
    await bridge_journal._append_frame(worker, state, ctx, frame, turn_id=state.effective_turn_id or None)


async def _turn_heartbeat_loop(worker: Any, state: _BridgeRunState, ctx: Any) -> None:
    session_id = ctx.session_id
    while True:
        await asyncio.sleep(worker._worker_heartbeat_interval_s)
        if _bridge_done_before_terminal(worker, state, ctx):
            logger.warning(
                "session kernel bridge ended before terminal; stop renewing "
                "active turn heartbeat session=%s turn=%s",
                session_id,
                state.effective_turn_id or None,
            )
            return
        try:
            updated = await _write_heartbeat(worker, state, ctx, raise_on_fence=False)
            if not updated:
                return
            # Sandbox keepalive: the same heartbeat that keeps this turn
            # "processing" also renews the sandbox's backend lease, so a long-running
            # turn (hours) never has its sandbox reclaimed mid-work by the
            # runtime TTL. Throttled inside maybe_renew (only hits the backend
            # when the remaining lease drops below the renew threshold), so this is a
            # cheap no-op on most ticks. When the turn settles or is fenced the
            # loop returns above, renewal stops, and an abandoned sandbox
            # expires naturally after one lease — keepalive is strictly scoped
            # to active processing. Best-effort: a renew failure never breaks
            # the heartbeat.
            with contextlib.suppress(Exception):
                await worker._runtime_manager.maybe_renew_lease_on_activity(session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "session kernel heartbeat write failed session=%s turn=%s",
                session_id,
                state.effective_turn_id or None,
                exc_info=True,
            )


async def _terminal_already_durable(worker: Any, state: Any, ctx: Any) -> bool:
    """Whether this turn's terminal reached the durable frames.

    Asked while the stream is silent. A terminal that is durable and did not
    arrive here is a dropped frame, and no further waiting delivers something
    that has already happened — the reader's post-loop reconciliation settles it
    from exactly this evidence. Without the question, one lost frame costs a
    full stall interval before anyone looks, and the conversation reads as still
    generating long after its own answer finished rendering.
    """
    if not state.effective_turn_id:
        return False
    return await bridge_terminal._turn_has_durable_frame_type(
        worker, state, ctx, "data-result"
    )


async def _note_quiet_interval(
    worker: Any, state: _BridgeRunState, session_id: str
) -> None:
    """Record one silent interval, and on the first ask whether the box is alive.

    Silence is not a verdict. A stream that stays open and produces nothing
    says only that the engine has not spoken; whether the turn is finished is
    the agent runtime's answer to give, and a clock cannot stand in for it.
    Measured: a model spent five minutes on one step, then delivered a complete
    answer two minutes after a spent budget had already recorded the turn as
    failed, and that answer reached nobody.

    What the platform may ask is its own question — is the sandbox still
    there — so the first quiet interval marks the Session for the control-plane
    probe, the same route a detached transport takes. The probe answers from
    the control plane rather than from elapsed time, and clears the mark when
    the box is alive.
    """
    state.consecutive_quiet_intervals += 1
    logger.warning(
        "session kernel bridge quiet interval session=%s turn_id=%s "
        "quiet_s=%.1f consecutive=%d",
        session_id,
        state.effective_turn_id or None,
        worker._bridge_event_stall_timeout_s,
        state.consecutive_quiet_intervals,
    )
    if state.consecutive_quiet_intervals > 1:
        return
    try:
        await worker._sessions_repo.update_session(
            session_id,
            {"sandbox_liveness_suspect_at": utcnow_iso()},
            touch_updated_at=False,
        )
    except Exception:
        # Losing the prompt probe costs latency in noticing a dead box; it
        # must not become a reason to end a turn the engine still owns.
        logger.warning(
            "session kernel bridge went quiet; failed to mark its Session for "
            "a sandbox probe session=%s turn=%s",
            session_id,
            state.effective_turn_id or None,
            exc_info=True,
        )


async def _next_bridge_item(worker: Any, state: _BridgeRunState, ctx: Any, wait_timeout: float) -> tuple[str, Any]:
    deferred_bridge_items = ctx.deferred_bridge_items
    bridge_event_queue = ctx.bridge_event_queue
    if deferred_bridge_items:
        return deferred_bridge_items.popleft()
    return await asyncio.wait_for(
        bridge_event_queue.get(),
        timeout=wait_timeout,
    )


async def _handle_engine_stream_detached(
    worker: Any,
    state: _BridgeRunState,
    ctx: Any,
    exc: EngineStreamDetached,
) -> int:
    """Leave the turn unsettled and schedule a control-plane sandbox probe."""
    if state.turn_settled:
        return state.last_event_seq
    try:
        marked = await worker._sessions_repo.update_session(
            ctx.session_id,
            {"sandbox_liveness_suspect_at": utcnow_iso()},
            touch_updated_at=False,
        )
        if not marked:
            logger.warning(
                "engine stream detached but its Session could not be marked "
                "for a sandbox probe session=%s turn=%s",
                ctx.session_id,
                state.effective_turn_id or None,
            )
    except Exception:
        # A failed bookkeeping write must not turn a transport observation
        # into a turn verdict. Heartbeat recovery remains authoritative, but
        # losing the prompt control-plane probe is visible to operators.
        logger.warning(
            "engine stream detached; failed to mark its Session for a "
            "sandbox probe session=%s turn=%s",
            ctx.session_id,
            state.effective_turn_id or None,
            exc_info=True,
        )
    logger.warning(
        "session kernel engine stream detached; leaving turn to recovery "
        "session=%s turn=%s detail=%s",
        ctx.session_id,
        state.effective_turn_id or None,
        exc,
    )
    return state.last_event_seq


async def _run_bridge_command(
    worker: Any,
    *,
    user: UserContext,
    session_id: str,
    session: dict[str, Any],
    command_event: dict[str, Any],
    payload: dict[str, Any],
    latency_trace: _TurnLatencyTrace | None = None,
    turn_id_override: str | None = None,
) -> int:
    command_id = str(command_event.get("causation_id") or "").strip()
    correlation_id = str(command_event.get("correlation_id") or "").strip() or command_id
    content = str(payload.get("content") or "")
    interaction_response = payload.get("interaction_response")
    if interaction_response is not None and not isinstance(interaction_response, dict):
        interaction_response = None
    permission_mode = payload.get("permission_mode")
    if permission_mode is not None:
        permission_mode = str(permission_mode)
    client_message_id = str(payload.get("client_message_id") or "").strip() or None
    command_type = str(payload.get("command_type") or "").strip()
    if latency_trace is None:
        latency_trace = _TurnLatencyTrace(
            session_id=session_id,
            turn_id=str(command_event.get("turn_id") or "").strip() or None,
            command_id=command_id or None,
            command_type=command_type or None,
            accepted_at=command_event.get("occurred_at"),
        )
        latency_trace.set_context(user=user, session=session)
    latency_trace.mark("turn_worker.bridge_start")

    state = _BridgeRunState()
    state.effective_turn_id = (
        str(turn_id_override or "").strip()
        or str(command_event.get("turn_id") or "").strip()
    )
    state.last_event_seq = int(command_event.get("event_seq") or 0)
    remote_stream_unsettled = False
    remote_stream_unsettled_state = ""
    # Accumulators for rich message projection (result card, tool cards, thinking)
    # Ordered AI SDK segments captured in frame-arrival order. Used by
    # _build_projected_assistant_blocks to lay out derived message blocks
    # as "text → tool → text → tool → text" rather than collapsing all text
    # into one prefix block followed by every tool. Each entry is one of:
    #   {"type": "text", "id": <text-block-id>, "text": <accumulated text>}
    #   {"type": "tool", "tc_id": <tool_call_id>, "name": ..., "input": ...,
    #    "result": <tool_result block> | None}
    ordered_assistant_segments: list[dict[str, Any]] = []
    bridge_event_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
    durable_frame_queue: asyncio.Queue[
        tuple[list[tuple[dict[str, Any], str | None]], int | None] | None
    ] = asyncio.Queue()
    durable_semantic_coalescer = _DurableSemanticFrameCoalescer()
    deferred_bridge_items: deque[tuple[str, Any]] = deque()
    heartbeat_task: asyncio.Task | None = None
    state._last_heartbeat_mono = time.monotonic()
    snapshot = await worker._session_snapshots_repo.get_snapshot(session_id)
    latency_trace.mark("turn_worker.snapshot_loaded")
    state.persisted_current_turn_remote_anchor = _normalize_current_turn_remote_anchor(
        (snapshot or {}).get("current_turn_remote_anchor")
    )
    state.current_turn_remote_anchor = (
        dict(state.persisted_current_turn_remote_anchor)
        if isinstance(state.persisted_current_turn_remote_anchor, dict)
        else None
    )
    state.persisted_current_turn_engine_anchor = _normalize_current_turn_engine_anchor(
        (snapshot or {}).get("current_turn_engine_anchor")
    )
    state.current_turn_engine_anchor = (
        dict(state.persisted_current_turn_engine_anchor)
        if isinstance(state.persisted_current_turn_engine_anchor, dict)
        else None
    )
    _bridge_ctx = SimpleNamespace(
        session_id=session_id,
        command_id=command_id,
        correlation_id=correlation_id,
        command_type=command_type,
        client_message_id=client_message_id,
        command_event=command_event,
        user=user,
        session=session,
        latency_trace=latency_trace,
        ordered_assistant_segments=ordered_assistant_segments,
        durable_frame_queue=durable_frame_queue,
        bridge_event_queue=bridge_event_queue,
        deferred_bridge_items=deferred_bridge_items,
        payload=payload,
        interaction_response=interaction_response,
        durable_semantic_coalescer=durable_semantic_coalescer,
        project_answer_after_dispatch_confirmed=lambda: _project_answer_after_dispatch_confirmed(worker, state, _bridge_ctx),
        ensure_frame_seq_initialized=lambda turn_id: bridge_frames._ensure_frame_seq_initialized(
            worker, state, _bridge_ctx, turn_id,
        ),
        run_live_frame_mongo_op=lambda operation, op, *, turn_id=None: bridge_frames._run_live_frame_mongo_op(
            worker, state, _bridge_ctx, operation, op, turn_id=turn_id,
        ),
        publish_broker_event=lambda event: bridge_frames._publish_broker_event(
            worker, state, _bridge_ctx, event,
        ),
        max_source_cursor_seq=lambda frames: bridge_frames._max_source_cursor_seq(
            worker, state, _bridge_ctx, frames,
        ),
        publish_live_frame=lambda frame, *, live_seq, turn_id: bridge_frames._publish_live_frame(
            worker, state, _bridge_ctx, frame, live_seq=live_seq, turn_id=turn_id,
        ),
        build_terminal_assistant_blocks=lambda: worker._build_terminal_assistant_blocks(
            state, _bridge_ctx,
        ),
        record_background_task_manifest_if_needed=lambda: worker._record_background_task_manifest_if_needed(
            state, _bridge_ctx,
        ),
    )

    try:
        # Initial heartbeat at turn start.
        await _write_heartbeat(worker, state, _bridge_ctx, raise_on_fence=True)
        latency_trace.mark("turn_worker.initial_heartbeat_written")
    except _TurnFencedOut:
        logger.info(
            "session kernel turn worker fenced out session=%s turn=%s",
            session_id, state.effective_turn_id,
        )
        return state.last_event_seq

    heartbeat_task = asyncio.create_task(
        _turn_heartbeat_loop(worker, state, _bridge_ctx),
        name=f"session-kernel-turn-heartbeat-{session_id}",
    )

    state.durable_frame_writer_task = asyncio.create_task(
        bridge_journal._durable_frame_writer_loop(worker, state, _bridge_ctx),
        name=f"session-kernel-live-durable-writer-{session_id}",
    )

    try:
        state.pending_interaction = await worker._resolve_pending_interaction(
            session=session,
            session_id=session_id,
            interaction_response=interaction_response,
        )
        delivery_command = None
        consumer_carrier: str | None = None
        if str(payload.get("input_id") or "").strip():
            delivery_runtime = await worker._turn_service.deliver_pending_inputs(
                user=user,
                session=session,
                session_id=session_id,
                requested_command_id=command_id,
                permission_mode=permission_mode,
            )
            # The runtime that delivery just fed is the carrier of everything
            # this bridge streams: consumption receipts are signed with it,
            # and the pending projection judges older receipts against it —
            # one consumed by a dead generation and never answered stays an
            # open row, so consumption_confirmed below correctly reads False
            # and the turn waits for the redelivered input's own consumption.
            consumer_carrier = consumption_carrier(
                getattr(delivery_runtime, "sandbox_id", None),
                getattr(delivery_runtime, "isolated_session_id", None),
            )
            pending_input_rows = await worker._turn_service.pending_input_rows(
                session_id,
                current_carrier=consumer_carrier,
            )
            # This turn serves the conversation's oldest unconsumed input, not
            # necessarily the message that started it. An input whose delivery
            # never reached the engine — a sandbox lost between accept and
            # dispatch — is still owed, so the next delivery redelivers it and
            # it becomes the FIFO head. Dispatching this turn for a later
            # message would ask the engine to consume out of order, which the
            # resident adapters refuse by design and which the platform's own
            # consumption check calls "not the durable FIFO head exactly": the
            # conversation then cannot speak again, because every later turn
            # meets the same head. The rest of the queue is not stranded — the
            # adapter advances to the next command when this turn completes.
            head = pending_input_rows[0] if pending_input_rows else None
            if head is not None:
                delivery_command = {
                    "command_id": str(head.get("command_id") or ""),
                    "session_id": session_id,
                    "sequence": int(head.get("sequence") or 0),
                    "input_id": str(head.get("input_id") or "").strip(),
                    "content": str(head.get("content") or ""),
                    "content_blocks": head.get("content_blocks"),
                    "client_message_id": str(
                        head.get("client_message_id") or ""
                    ).strip()
                    or None,
                    "consumption_confirmed": False,
                }
            else:
                # Nothing is owed: this turn's own input was already consumed,
                # and the receipt it needs is the one that closed it.
                delivery_command = {
                    "command_id": command_id,
                    "session_id": session_id,
                    "sequence": int(command_event.get("event_seq") or 0),
                    "input_id": str(payload.get("input_id") or "").strip(),
                    "content": content,
                    "client_message_id": client_message_id,
                    "consumption_confirmed": True,
                    "content_blocks": payload.get("content_blocks"),
                }
        bridge_kwargs = {
            "user": user,
            "session": session,
            "session_id": session_id,
            "content": content,
            "turn_id": state.effective_turn_id or str(uuid.uuid4()),
            "interaction_response": interaction_response,
            "permission_mode": permission_mode,
            "client_message_id": client_message_id,
            "delivery_command": delivery_command,
            "on_query_committed": lambda dispatch_anchor: bridge_frames._on_query_committed(
                worker, state, _bridge_ctx, dispatch_anchor,
            ),
            "on_timing": lambda stage, payload: bridge_frames._on_turn_service_timing(
                worker, state, _bridge_ctx, stage, payload,
            ),
        }
        if _callable_accepts_keyword(
            worker._turn_service.iter_sandbox_events,
            "command_id",
        ):
            bridge_kwargs["command_id"] = command_id
        event_iter = worker._turn_service.iter_sandbox_events(**bridge_kwargs)

        latency_trace.mark("turn_worker.event_iter_created")

        state.bridge_stream_task = asyncio.create_task(
            bridge_resume._bridge_event_producer(worker, state, _bridge_ctx, event_iter),
            name=f"session-kernel-bridge-stream-{session_id}",
        )
        latency_trace.mark("turn_worker.bridge_stream_started")

        while True:
            # WAITING_FOR_INTERACTION clears the active worker owner; from
            # that point this worker may only drain segment-close events,
            # not renew the active-owner heartbeat.
            if not state.turn_settled and not _bridge_done_before_terminal(worker, state, _bridge_ctx):
                await _maybe_write_heartbeat(worker, state, _bridge_ctx)
            try:
                wait_timeout = worker._bridge_event_stall_timeout_s
                item_type, item_payload = await _next_bridge_item(worker, state, _bridge_ctx, wait_timeout)
            except asyncio.TimeoutError:
                if state.bridge_stream_task is not None and state.bridge_stream_task.done():
                    if state.turn_settled and state.waiting_for_interaction:
                        break
                    logger.warning(
                        "session kernel bridge task ended without terminal; "
                        "ending active owner session=%s turn_id=%s",
                        session_id,
                        state.effective_turn_id or None,
                    )
                    break
                if state.turn_settled and state.waiting_for_interaction:
                    raise RuntimeError(
                        "pending interaction bridge segment did not close after authoritative settle"
                    )
                # A silent stream raises exactly one answerable question: has
                # this turn ALREADY ended somewhere else? A terminal that
                # reached the durable frames and not this stream is a dropped
                # frame, and no further waiting delivers what has happened —
                # the reconciliation below settles it from that evidence. This
                # is the only thing silence licenses. It is not evidence that a
                # turn still running has stopped, so nothing past this point
                # ends the read: the engine owns that call and has not made it.
                if await _terminal_already_durable(worker, state, _bridge_ctx):
                    logger.info(
                        "session kernel bridge: turn already has a durable terminal "
                        "while the stream is silent; settling from it rather than "
                        "waiting on a segment nothing will close session=%s turn_id=%s",
                        session_id,
                        state.effective_turn_id,
                    )
                    break
                await _note_quiet_interval(worker, state, session_id)
                continue

            # Any item at all means the stream is alive and producing, so the
            # box is answering and the probe has nothing to chase.
            state.consecutive_quiet_intervals = 0

            if item_type == "done":
                if state.bridge_stream_task is not None and state.bridge_stream_task.done():
                    try:
                        await state.bridge_stream_task
                    except asyncio.CancelledError as exc:
                        cancel_reason = str(exc).strip()
                        if not state.started:
                            raise RuntimeError(
                                "bridge stream cancelled before dispatch ack"
                            ) from exc
                        raise RuntimeError(
                            f"bridge stream cancelled: {cancel_reason or 'unknown reason'}"
                        ) from exc
                break
            if item_type == "error":
                exc = item_payload
                raise exc
            if item_type != "event" or not isinstance(item_payload, dict):
                continue

            event = item_payload
            evt_type = str(event.get("type") or "")

            if evt_type == "ack":
                if state.started:
                    continue
                state.started = True
                state.dispatch_confirmed = True
                state.effective_turn_id = str(event.get("turn_id") or "").strip()
                ack_engine_anchor = _build_current_turn_engine_anchor(event)
                if isinstance(ack_engine_anchor, dict):
                    await bridge_anchors._observe_engine_anchor(worker, state, _bridge_ctx, ack_engine_anchor)
                elif command_type == "StartTurn":
                    state.current_turn_engine_anchor = None
                    state.persisted_current_turn_engine_anchor = None
                ack_control_frames = _build_ack_control_frames(
                    state.effective_turn_id,
                    client_message_id,
                )
                await bridge_frames._process_translated_frames(worker, state, _bridge_ctx, ack_control_frames)
                await worker._project_turn_requested_with_retry(state, _bridge_ctx, )
                continue

            if evt_type == "status":
                session_state = str(event.get("state") or "")
                runtime_warning = bool(event.get("runtime_warning"))
                # Bridge emits READY+runtime_warning for recoverable failures;
                # remap to internal RECOVERY_REQUIRED so the snapshot records
                # conversation_state=DEGRADED and preserves the remote anchor.
                if session_state == SessionState.READY.value and runtime_warning:
                    session_state = SessionState.RECOVERY_REQUIRED.value
                if session_state == "BUSY":
                    continue
                if session_state == SessionState.READY.value and state.waiting_for_interaction:
                    # A pending interaction closes the current SSE segment,
                    # but the turn is not terminal until the answer
                    # continuation settles.
                    continue
                if session_state in {SessionState.READY.value, SessionState.TERMINATED.value, SessionState.RECOVERY_REQUIRED.value}:
                    if session_state == SessionState.READY.value:
                        # Not over an error. The engine stream reports READY on
                        # its way out whether the turn succeeded or failed, and
                        # appending a finish on top of an error terminal
                        # would rewrite a FAILED turn as COMPLETED. Preserve an
                        # existing error terminal instead of appending a
                        # contradictory finish/stop proof.
                        if turn_terminal_frame_matches(
                            state.terminal_frame_proof,
                            turn_id=state.effective_turn_id,
                            command_id=command_id,
                            frame_type="error",
                        ):
                            terminal_doc = None
                        else:
                            terminal_doc = await bridge_terminal._append_terminal_signal(worker, state, _bridge_ctx,
                                "finish",
                                publish=False,
                                publish_live_first=True,
                            )
                    else:
                        # No local guard: `_append_terminal_signal` refuses a
                        # second terminal for a turn that already has one.
                        terminal_doc = await bridge_terminal._append_terminal_signal(worker, state, _bridge_ctx,
                            "error",
                            error_text=state.last_error_text or "turn ended before completion",
                            publish=False,
                            publish_live_first=True,
                        )
                    await bridge_terminal._project_turn_terminal_with_retry(worker, state, _bridge_ctx,
                        session_state,
                        terminal_frame=state.terminal_frame_proof,
                    )
                    await bridge_journal._publish_persisted_frame(worker, state, _bridge_ctx, terminal_doc)
                    # For non-interaction turns, READY/TERMINATED/RECOVERY_REQUIRED
                    # is authoritative terminal state. Do not keep consuming
                    # trailing bridge events after the durable terminal settle,
                    # otherwise a late error can pollute the finished turn.
                    return state.last_event_seq
                continue

            if evt_type == "engine_diagnostic":
                await bridge_journal._append_engine_diagnostic(
                    worker,
                    state,
                    _bridge_ctx,
                    event,
                )
                continue

            if evt_type == "background_tasks_opened":
                manifest = event.get("manifest")
                engine_kind = str(event.get("engine_kind") or "").strip()
                if not isinstance(manifest, dict) or not engine_kind:
                    raise RuntimeError("background-task manifest envelope is malformed")
                state.background_tasks_opened = {
                    "engine_kind": engine_kind,
                    **manifest,
                }
                continue

            if evt_type == "response_result":
                response_data = event.get("data")
                if not isinstance(response_data, dict):
                    raise RuntimeError("SDK response Result data is malformed")
                await bridge_journal._append_frame(
                    worker,
                    state,
                    _bridge_ctx,
                    {
                        "type": "data-result",
                        SDK_RESPONSE_RESULT_BOUNDARY: True,
                        "data": dict(response_data),
                    },
                    turn_id=state.effective_turn_id or None,
                )
                continue

            if evt_type == "ai_sdk_frame":
                # Engine adapters that emit AI SDK frames natively
                # (Assistant via translate_assistant_response_event) deliver
                # each frame through this envelope. The frame is
                # already in AI SDK wire shape — no canonical-projector
                # pass — so route it through the engine-agnostic
                # ``_process_translated_frames`` helper which the
                # Claude/sandbox path also uses:
                #   - ``_publish_live_frame`` fans the frame to the
                #     in-memory broker (asyncio.Queue.put_nowait), so
                #     SSE clients see the delta within microseconds;
                #   - ``_enqueue_durable_frames`` hands the doc to the
                #     background ``_durable_frame_writer_loop`` task,
                #     which appends an engine-frame event asynchronously
                #     and never blocks the main turn loop.
                # Terminal proof / snapshot projection arrive via the
                # engine-agnostic ``result`` and ``status`` envelopes;
                # they are not synthesized from the v5 ``finish`` frame here.
                frame_payload_raw = event.get("frame")
                if not isinstance(frame_payload_raw, dict):
                    continue
                frame_engine_kind = str(event.get("engine_kind") or "").strip()
                frame_engine_turn_id = str(
                    event.get("engine_turn_id") or ""
                ).strip()
                normalized_v5 = bridge_journal._normalize_frame(worker, state, _bridge_ctx, dict(frame_payload_raw))
                if frame_engine_kind:
                    normalized_v5.setdefault("__engine_kind", frame_engine_kind)
                if frame_engine_turn_id:
                    normalized_v5.setdefault(
                        "__engine_turn_id", frame_engine_turn_id
                    )
                frame_engine_sequence_number = _coerce_int(
                    event.get("engine_sequence_number")
                )
                if frame_engine_sequence_number is not None:
                    normalized_v5.setdefault(
                        "__engine_sequence_number",
                        int(frame_engine_sequence_number),
                    )
                frame_index = event.get("frame_index")
                if isinstance(frame_index, int):
                    normalized_v5.setdefault("__source_frame_index", frame_index)
                if normalized_v5.get("type") == "data-input-consumed":
                    # The runner's UserPromptSubmit hook is the consumption
                    # authority. Commit the matching journal dequeue before the
                    # UI sees the root message; input_ack only proved delivery.
                    await _confirm_engine_input_consumed(
                        worker,
                        state,
                        _bridge_ctx,
                        normalized_v5,
                        consumer_carrier=consumer_carrier,
                    )
                await bridge_frames._process_translated_frames(worker, state, _bridge_ctx, [normalized_v5])
                state.frame_hold_active = True
                try:
                    await pass_fault_barrier(
                        "turn_frame_processed",
                        session_id=session_id,
                        frame_type=str(normalized_v5.get("type") or "").strip(),
                        prepare_hold=lambda: bridge_journal._drain_durable_frame_queue(
                            worker,
                            state,
                            _bridge_ctx,
                        ),
                    )
                finally:
                    state.frame_hold_active = False
                continue

            if evt_type == "pending_interaction":
                pending = event.get("pending_interaction")
                tool_name = str(event.get("tool_name") or "")
                if isinstance(pending, dict):
                    event_interaction_id = str(  # noqa: F841
                        event.get("interaction_id")
                        or pending.get("interaction_id")
                        or ""
                    ).strip()
                    existing_interaction = None
                    interaction_id = str(pending.get("interaction_id") or "").strip()
                    if interaction_id:
                        existing_interaction = await worker._interaction_snapshots_repo.get_interaction(
                            session_id,
                            interaction_id,
                        )
                    if str((existing_interaction or {}).get("interaction_state") or "").strip() == "ANSWERED":
                        await worker._project_interaction_opened(state, _bridge_ctx, dict(pending), tool_name)
                        continue
                    # Commit-before-emit: durable authority first, then frames.
                    # _project_interaction_opened writes turn.awaiting_interaction
                    # and projects it onto the interaction and session snapshots.
                    # Control-plane frames are only emitted after commit succeeds.
                    await worker._project_interaction_opened(state, _bridge_ctx, dict(pending), tool_name)
                    # Interrupt-vs-park race, closed from the park side. The
                    # interrupt command marks the snapshot first (CAS-scoped
                    # to the active turn — session_snapshots owns
                    # current_turn_id) and settles a turn it finds already
                    # parked. A park that commits after that check would wait
                    # forever: the interrupt lands mid-stream, the SDK
                    # surfaces one more tool call before aborting, and the
                    # session stays in WAITING_INPUT. Both sides use the same
                    # store, so each writes its own signal and reads the
                    # other's — no window between them.
                    _snap = await worker._session_snapshots_repo.get_snapshot(session_id)
                    if bool((_snap or {}).get("interrupt_requested")):
                        await worker._settle_interrupted_waiting_interaction(
                            session_id=session_id,
                            turn_id=state.effective_turn_id or "",
                            command_id=_bridge_ctx.command_id,
                        )
                        continue

                    approval_request_frame = build_tool_approval_request_frame(pending)
                    if approval_request_frame is not None:
                        await bridge_journal._append_frame(worker, state, _bridge_ctx,
                            approval_request_frame,
                            turn_id=state.effective_turn_id or None,
                        )
                        await bridge_journal._append_frame(worker, state, _bridge_ctx,
                            {
                                "type": "data-interaction",
                                "data": dict(pending),
                            },
                            turn_id=state.effective_turn_id or None,
                        )
                    else:
                        # Standalone forms have their own durable identity;
                        # presenting one does not create a model tool call.
                        await bridge_journal._append_frame(worker, state, _bridge_ctx,
                            {
                                "type": "data-interaction",
                                "data": dict(pending),
                            },
                            turn_id=state.effective_turn_id or None,
                        )
                await bridge_journal._append_frame(worker, state, _bridge_ctx,
                    {
                        "type": "finish",
                        "finishReason": AI_SDK_FINISH_REASON_TOOL_CALLS,
                    },
                    turn_id=state.effective_turn_id or None,
                )
                if heartbeat_task is not None and not heartbeat_task.done():
                    heartbeat_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await heartbeat_task
                continue

            if evt_type == "result":
                data = event.get("data")
                if isinstance(data, dict):
                    # The adapter has declared that its terminal closed the
                    # native interaction. Core acts on that platform fact and
                    # never decodes a vendor deferred-tool object.
                    if data.get("interaction_closed") is True and state.effective_turn_id:
                        deferred_count = await worker._interaction_snapshots_repo.deactivate_active_for_turn(
                            session_id, state.effective_turn_id
                        )
                        logger.info(
                            "session kernel closed deferred interaction session=%s turn=%s "
                            "deactivated=%s deferred=%s",
                            session_id,
                            state.effective_turn_id,
                            deferred_count,
                            True,
                        )
                if isinstance(data, dict) and not state.saw_result_frame:
                    state.saw_result_frame = True
                    if state.last_result_data is None:
                        state.last_result_data = dict(data)
                    state.last_terminal_reason = (
                        str(data.get("terminal_reason") or "").strip() or None
                    )
                    if not state.saw_public_result_frame:
                        public_result: dict[str, Any] = {}
                        usage = data.get("usage")
                        if isinstance(usage, dict):
                            public_result["usage"] = dict(usage)
                        await bridge_journal._append_frame(worker, state, _bridge_ctx,
                            bridge_journal._normalize_frame(
                                worker,
                                state,
                                _bridge_ctx,
                                {"type": "data-result", "data": public_result},
                            ),
                            turn_id=state.effective_turn_id or None,
                        )
                # E2E-only: deterministically reproduce the "result received
                # but terminal signal lost" reconnect race against a real
                # sandbox. Structurally inert: no hook is installed unless
                # the app armed astrabox.testing at startup (ASTRABOX_E2E_FAULTS).
                if state.saw_result_frame and consume_fault(
                    "turn_terminal_drop", session_id=session_id
                ):
                    logger.warning(
                        "e2e: dropping bridge terminal after result session=%s turn=%s",
                        session_id,
                        state.effective_turn_id or None,
                    )
                    break
                continue

            if evt_type == "assistant_message":
                assistant_message_text = str(event.get("text") or "")
                if assistant_message_text.strip():
                    state.last_assistant_text = assistant_message_text
                continue

            if evt_type == "error":
                state.last_error_text = str(event.get("message") or "unknown error")
                state.last_terminal_reason = (
                    str(event.get("terminal_reason") or "").strip() or None
                )
                state.saw_error_event = True
                if bool(event.get("stream_unsettled")):
                    remote_stream_unsettled = True
                    remote_stream_unsettled_state = str(event.get("active_state") or "").strip()
                    continue
                await bridge_terminal._append_terminal_signal(worker, state, _bridge_ctx,
                    "error",
                    error_text=state.last_error_text,
                )
                continue
        if not state.turn_settled:
            terminal_signal_error: Exception | None = None
            terminal_doc: dict[str, Any] | None = None
            if remote_stream_unsettled:
                logger.warning(
                    "session kernel bridge stream unsettled; keeping active state "
                    "session=%s turn=%s state=%s error=%s",
                    session_id,
                    state.effective_turn_id or None,
                    remote_stream_unsettled_state or (
                        "STREAMING" if state.dispatch_confirmed else "PROCESSING"
                    ),
                    state.last_error_text,
                )
                return state.last_event_seq
            if not state.started:
                state.last_error_text = state.last_error_text or "bridge ended before dispatch ack"
                logger.error(
                    "session kernel bridge ended before ack session=%s turn_id=%s command_id=%s",
                    session_id,
                    state.effective_turn_id or None,
                    command_id,
                )
            if (
                state.started
                and (state.saw_result_frame or state.saw_mirror_terminal_evidence)
                and not state.saw_error_event
            ):
                # Terminal-without-status: the model's authoritative terminal
                # signal was observed either as live data-result or as the
                # durable mirror's own assistant end_turn, but the dispatch
                # "finish" status never reached this reader. The turn
                # completed, so settle it as a successful finish.
                await bridge_journal._stop_durable_frame_writer(worker, state, _bridge_ctx, raise_on_error=False)
                try:
                    terminal_doc = await bridge_terminal._append_terminal_signal(worker, state, _bridge_ctx,
                        "finish",
                        publish=False,
                        require_durable_queue_clean=False,
                    )
                except Exception as exc:
                    terminal_signal_error = exc
                    logger.warning(
                        "session kernel failed to persist finish terminal after "
                        "result-without-terminal session=%s",
                        session_id,
                        exc_info=True,
                    )
                logger.info(
                    "session kernel settled finish from buffered terminal "
                    "(status signal lost) session=%s turn=%s source=%s",
                    session_id,
                    state.effective_turn_id or None,
                    "result" if state.saw_result_frame else "mirror_end_turn",
                )
                await bridge_terminal._project_turn_terminal_with_retry(worker, state, _bridge_ctx,
                    SessionState.READY.value,
                    terminal_frame=state.terminal_frame_proof,
                )
                await bridge_journal._publish_persisted_frame(worker, state, _bridge_ctx, terminal_doc)
                if terminal_signal_error is not None:
                    raise terminal_signal_error
                return state.last_event_seq
            if not state.saw_error_event:
                state.last_error_text = state.last_error_text or "turn ended without terminal event"
                try:
                    terminal_doc = await bridge_terminal._append_terminal_error_after_stopping_durable_writer(worker, state, _bridge_ctx,
                        state.last_error_text,
                        publish=False,
                    )
                except Exception as exc:
                    terminal_signal_error = exc
                    logger.warning(
                        "session kernel failed to persist terminal error frame after unfinished bridge session=%s",
                        session_id,
                        exc_info=True,
                    )
            await bridge_terminal._project_turn_terminal_with_retry(worker, state, _bridge_ctx,
                SessionState.RECOVERY_REQUIRED.value,
                terminal_frame=state.terminal_frame_proof,
            )
            await bridge_journal._publish_persisted_frame(worker, state, _bridge_ctx, terminal_doc)
            if terminal_signal_error is not None:
                raise terminal_signal_error
    except _TurnFencedOut:
        logger.info(
            "session kernel turn worker fenced out session=%s turn=%s",
            session_id, state.effective_turn_id,
        )
        return state.last_event_seq
    except EngineStreamDetached as exc:
        # The transport died, not the turn: the box may still be running it.
        # Writing a terminal state here would let an interrupted worker turn
        # the loss of its own stream into the outcome of the turn. The typed
        # transport fact also schedules an early control-plane probe; recovery
        # still owns the eventual verdict from durable evidence.
        return await _handle_engine_stream_detached(
            worker,
            state,
            _bridge_ctx,
            exc,
        )
    except Exception as exc:
        state.last_error_text = str(exc)
        if state.turn_settled:
            logger.warning(
                "session kernel turn worker bridge errored after authoritative settle session=%s",
                session_id,
                exc_info=True,
            )
            raise
        if await bridge_terminal._has_durable_completed_terminal(worker, state, _bridge_ctx):
            logger.warning(
                "session kernel ignoring late bridge error after durable completed terminal session=%s turn=%s",
                session_id,
                state.effective_turn_id,
                exc_info=True,
            )
            return state.last_event_seq
        if turn_terminal_frame_matches(
            state.terminal_frame_proof,
            turn_id=state.effective_turn_id,
            command_id=command_id,
            frame_type="finish",
            finish_reason=AI_SDK_FINISH_REASON_STOP,
        ):
            logger.warning(
                "session kernel completed finish frame persisted but terminal projection failed session=%s turn=%s",
                session_id,
                state.effective_turn_id,
                exc_info=True,
            )
            raise
        logger.exception("session kernel turn worker bridge failed session=%s", session_id)
        signal_error: Exception | None = None
        terminal_doc: dict[str, Any] | None = None
        try:
            terminal_doc = await bridge_terminal._append_terminal_error_after_stopping_durable_writer(worker, state, _bridge_ctx,
                state.last_error_text,
                publish=False,
            )
        except Exception as signal_exc:
            signal_error = signal_exc
            logger.warning(
                "session kernel failed to persist terminal error frame session=%s err=%s signal_err=%s",
                session_id,
                exc,
                signal_exc,
            )
        await bridge_terminal._project_turn_terminal_with_retry(worker, state, _bridge_ctx,
            SessionState.RECOVERY_REQUIRED.value,
            terminal_frame=state.terminal_frame_proof,
        )
        await bridge_journal._publish_persisted_frame(worker, state, _bridge_ctx, terminal_doc)
        if signal_error is not None:
            raise signal_error from exc
        return state.last_event_seq
    finally:
        durable_stop_error = await bridge_journal._stop_durable_frame_writer(worker, state, _bridge_ctx, raise_on_error=False)
        if durable_stop_error is not None and not state.turn_settled:
            detail = str(durable_stop_error).strip()
            cause_type = type(durable_stop_error).__name__
            state.last_error_text = (
                "live frame durable writer failed"
                f": {cause_type}{f': {detail}' if detail else ''}"
            )
            logger.warning(
                "session kernel durable writer failed during turn stop; "
                "projecting terminal error session=%s turn=%s err=%s",
                session_id,
                state.effective_turn_id,
                durable_stop_error,
            )
            terminal_doc: dict[str, Any] | None = None
            try:
                terminal_doc = await bridge_terminal._append_terminal_signal(worker, state, _bridge_ctx,
                    "error",
                    error_text=state.last_error_text,
                    publish=False,
                    require_durable_queue_clean=False,
                )
                await bridge_terminal._project_turn_terminal_with_retry(worker, state, _bridge_ctx,
                    SessionState.RECOVERY_REQUIRED.value,
                    terminal_frame=state.terminal_frame_proof,
                )
                await bridge_journal._publish_persisted_frame(worker, state, _bridge_ctx, terminal_doc)
            except Exception as terminal_exc:
                raise terminal_exc from durable_stop_error
        if heartbeat_task is not None and not heartbeat_task.done():
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat_task
        if state.bridge_stream_task is not None and not state.bridge_stream_task.done():
            state.bridge_stream_task.cancel("explicit")
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await state.bridge_stream_task
        latency_trace.log("bridge_stop")
        await bridge_frames._publish_broker_event(worker, state, _bridge_ctx,
            {
                "type": "ai_sdk_stream_complete",
                "command_id": command_id,
                "turn_id": state.effective_turn_id or None,
            }
        )

    return state.last_event_seq
