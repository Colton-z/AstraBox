"""Terminal-frame signalling and COMPLETED/FAILED classification for the bridge.

Module functions take ``(worker, state, ctx)`` plus their own explicit
parameters, the same convention as :mod:`bridge_journal`.

``_project_turn_terminal`` mints ``turn.completed`` only from a durable finish
proof plus ``READY``. A public result card never classifies the turn.

Throughout this module ``state`` is always the per-turn ``_BridgeRunState``
object; the *session* state string (``"READY"`` / ``"RECOVERY_REQUIRED"`` /
...) is always named ``session_state``, both as a parameter and at call
sites, so the two can never be confused.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING, Any

from astrabox.persistence.models.session_snapshot import (
    validate_channel_ownership,
    watermark_field_for_channel,
)
from astrabox.persistence.repository.backend import is_mongo_transient_error
from astrabox.common.logger.logger_factory import get_logger
from astrabox.core.model import SessionState
from astrabox.core.service.orchestrator.message_blocks import (
    canonicalize_terminal_message_blocks,
    drop_unresolved_tool_use_blocks,
)
from astrabox.core.service.orchestrator.session_mirror import (
    build_terminal_session_mirror_updates,
)
from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    AI_SDK_FINISH_REASON_STOP,
    USER_STOP_FAILURE_TEXT,
    build_turn_terminal_snapshot_updates,
    coerce_int as _coerce_int,
    is_terminal_data_result_frame,
    normalize_current_turn_engine_anchor as _normalize_current_turn_engine_anchor,
    normalize_current_turn_remote_anchor as _normalize_current_turn_remote_anchor,
    normalize_turn_terminal_frame,
    turn_terminal_frame_matches,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn import bridge_journal
from astrabox.core.service.orchestrator.session_kernel.workers.turn._fencing import (
    _TurnFencedOut,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn._helpers import (
    _synthesize_assistant_text_from_blocks,
)

if TYPE_CHECKING:
    from astrabox.core.service.orchestrator.session_kernel.workers.turn.state import (
        _BridgeRunState,
    )

logger = get_logger(__name__)


def _engine_terminal_reason(state: Any) -> str | None:
    """Return the adapter-carried native reason without interpreting it."""

    return str(getattr(state, "last_terminal_reason", None) or "").strip() or None


async def _turn_has_durable_frame_type(
    worker: Any,
    state: _BridgeRunState,
    ctx: Any,
    frame_type: str,
) -> bool:
    session_id = ctx.session_id
    _run_live_frame_mongo_op = ctx.run_live_frame_mongo_op
    normalized_type = str(frame_type or "").strip()
    if not normalized_type:
        raise RuntimeError("missing durable frame type")
    if not state.effective_turn_id:
        raise RuntimeError("missing turn id for durable frame scan")

    after_seq = -1
    while True:
        rows = await _run_live_frame_mongo_op(
            "session_events.list_frames",
            lambda after_seq=after_seq: worker._session_events_repo.list_frames(
                session_id,
                turn_id=state.effective_turn_id,
                after_seq=after_seq,
                limit=500,
            ),
            turn_id=state.effective_turn_id,
        )
        if not rows:
            return False

        next_after_seq = after_seq
        for row in rows:
            row_seq = _coerce_int(row.get("frame_seq"))
            if row_seq is not None:
                next_after_seq = max(next_after_seq, row_seq)
            payload_doc = row.get("payload")
            if (
                isinstance(payload_doc, dict)
                and str(payload_doc.get("type") or "").strip() == normalized_type
                and (
                    normalized_type != "data-result"
                    or is_terminal_data_result_frame(payload_doc)
                )
            ):
                return True

        if len(rows) < 500:
            return False
        if next_after_seq <= after_seq:
            raise RuntimeError("ai sdk frame scan did not advance")
        after_seq = next_after_seq


async def _turn_has_durable_tool_approval_response(
    worker: Any,
    state: _BridgeRunState,
    ctx: Any,
    approval_id: str,
) -> bool:
    session_id = ctx.session_id
    _run_live_frame_mongo_op = ctx.run_live_frame_mongo_op
    normalized_approval_id = str(approval_id or "").strip()
    if not normalized_approval_id:
        raise RuntimeError("missing approval id for durable frame scan")
    if not state.effective_turn_id:
        raise RuntimeError("missing turn id for durable frame scan")

    after_seq = -1
    while True:
        rows = await _run_live_frame_mongo_op(
            "session_events.list_frames",
            lambda after_seq=after_seq: worker._session_events_repo.list_frames(
                session_id,
                turn_id=state.effective_turn_id,
                after_seq=after_seq,
                limit=500,
            ),
            turn_id=state.effective_turn_id,
        )
        if not rows:
            return False

        next_after_seq = after_seq
        for row in rows:
            row_seq = _coerce_int(row.get("frame_seq"))
            if row_seq is not None:
                next_after_seq = max(next_after_seq, row_seq)
            payload_doc = row.get("payload")
            if (
                isinstance(payload_doc, dict)
                and str(payload_doc.get("type") or "").strip()
                == "tool-approval-response"
                and str(payload_doc.get("approvalId") or "").strip()
                == normalized_approval_id
            ):
                return True

        if len(rows) < 500:
            return False
        if next_after_seq <= after_seq:
            raise RuntimeError("ai sdk frame scan did not advance")
        after_seq = next_after_seq


async def _append_replay_terminal_result_if_missing(
    worker: Any,
    state: _BridgeRunState,
    ctx: Any,
    frame: dict[str, Any],
) -> None:
    if not state.effective_turn_id:
        raise RuntimeError("missing turn id for replay terminal result")
    if await _turn_has_durable_frame_type(worker, state, ctx, "data-result"):
        return
    await bridge_journal._append_frame(worker, state, ctx, frame, turn_id=state.effective_turn_id)


async def _append_terminal_signal(
    worker: Any,
    state: _BridgeRunState,
    ctx: Any,
    signal_type: str,
    *,
    error_text: str | None = None,
    publish: bool = True,
    require_durable_queue_clean: bool = True,
    publish_live_first: bool = False,
) -> dict[str, Any] | None:
    session_id = ctx.session_id
    command_id = ctx.command_id
    _publish_live_frame = ctx.publish_live_frame
    # A turn has one terminal outcome. Centralizing the guard here makes the
    # first published terminal authoritative for every caller and refuses all
    # later signals, including a finish after an error. Match any terminal type
    # explicitly because `turn_terminal_frame_matches` otherwise defaults to
    # the narrower finish-only question.
    settled = normalize_turn_terminal_frame(state.terminal_frame_proof)
    if (
        isinstance(settled, dict)
        and settled.get("turn_id") == (str(state.effective_turn_id or "").strip() or None)
        and settled.get("command_id") == (str(command_id or "").strip() or None)
    ):
        existing = str(settled.get("type") or "")
        if existing != signal_type:
            logger.info(
                "terminal already settled as %s; refusing %s session=%s turn=%s",
                existing,
                signal_type,
                session_id,
                state.effective_turn_id,
            )
        return None
    payload_doc: dict[str, Any]
    if signal_type == "finish":
        payload_doc = {
            "type": "finish",
            "finishReason": AI_SDK_FINISH_REASON_STOP,
        }
    else:
        payload_doc = {"type": "error", "errorText": error_text or "unknown error"}
        # The failure phase reaches the live stream, not just durable history:
        # pre_dispatch means the engine never received the write (a
        # retry is safe); post_dispatch means it may have. Clients drive
        # retry/settlement affordances off this — polling the session detail
        # races the stream close. Versioned data part, mirrored from the
        # data-result projection; the deterministic id + durable-type check
        # keep replays single-shot.
        failure_phase = (
            "post_dispatch" if state.dispatch_confirmed else "pre_dispatch"
        )
        if not await _turn_has_durable_frame_type(
            worker, state, ctx, "data-turn-failure"
        ):
            await bridge_journal._append_frame(
                worker, state, ctx,
                bridge_journal._normalize_frame(worker, state, ctx, {
                    "type": "data-turn-failure",
                    "data": {
                        "version": 1,
                        "error": error_text or "unknown error",
                        "failure_phase": failure_phase,
                    },
                }),
                turn_id=state.effective_turn_id or None,
                publish=publish,
                require_durable_queue_clean=require_durable_queue_clean,
            )
    if publish_live_first:
        bridge_journal._raise_durable_frame_writer_error(state)
        live_seq = state.live_frame_seq
        state.live_frame_seq += 1
        payload_doc["__live_seq"] = live_seq
        await _publish_live_frame(
            dict(payload_doc),
            live_seq=live_seq,
            turn_id=state.effective_turn_id or None,
        )
    frame_doc = await bridge_journal._append_frame(worker, state, ctx,
        payload_doc,
        turn_id=state.effective_turn_id or None,
        publish=publish,
        require_durable_queue_clean=require_durable_queue_clean,
    )
    state.terminal_frame_proof = bridge_journal._terminal_frame_proof_from_doc(frame_doc)
    return frame_doc


async def _append_terminal_error_after_stopping_durable_writer(
    worker: Any,
    state: _BridgeRunState,
    ctx: Any,
    error_text: str | None,
    *,
    publish: bool = False,
) -> dict[str, Any] | None:
    await bridge_journal._stop_durable_frame_writer(worker, state, ctx, raise_on_error=False)
    return await _append_terminal_signal(
        worker,
        state,
        ctx,
        "error",
        error_text=error_text,
        publish=publish,
        require_durable_queue_clean=False,
    )


async def _find_existing_conversation_event(
    worker: Any,
    state: _BridgeRunState,
    ctx: Any,
    event_type: str,
) -> dict[str, Any] | None:
    session_id = ctx.session_id
    command_id = ctx.command_id
    existing_events = await worker._session_events_repo.list_events(
        session_id,
        channel="conversation",
        causation_id=command_id,
        limit=50,
    )
    return next(
        (
            dict(item)
            for item in existing_events
            if str(item.get("event_type") or "") == event_type
            and str(item.get("turn_id") or "").strip() == str(state.effective_turn_id or "").strip()
        ),
        None,
    )


async def _commit_turn_terminal_snapshot(
    worker: Any,
    *,
    session_id: str,
    turn_id: str,
    event_seq: int,
    updates: dict[str, Any],
) -> dict[str, Any] | None:
    """Commit a terminal projection while this turn still owns the slot.

    A continuation worker can advance the conversation watermark for the same
    turn after an earlier worker has journaled its terminal. The watermark
    orders projections; it does not transfer turn ownership. Preserve its
    monotonic value with an optimistic compare-and-retry, but treat only a
    changed ``current_turn_id`` as a terminal fence.
    """
    normalized_turn_id = str(turn_id or "").strip()
    if not normalized_turn_id:
        raise RuntimeError("missing turn id for terminal snapshot commit")
    terminal_event_seq = int(event_seq)
    if terminal_event_seq <= 0:
        raise RuntimeError("missing terminal event sequence for snapshot commit")

    channel = "conversation"
    watermark_field = watermark_field_for_channel(channel)
    validate_channel_ownership(channel, set(updates) | {watermark_field})
    snapshot = await worker._session_snapshots_repo.get_snapshot(session_id)

    while isinstance(snapshot, dict):
        if str(snapshot.get("current_turn_id") or "").strip() != normalized_turn_id:
            return None
        raw_watermark = snapshot.get(watermark_field)
        current_watermark = _coerce_int(raw_watermark)
        if current_watermark is None or current_watermark < 0:
            raise RuntimeError(
                "active turn snapshot has no valid conversation event watermark"
            )

        committed_updates = {
            **updates,
            watermark_field: max(current_watermark, terminal_event_seq),
        }
        applied = await worker._session_snapshots_repo.force_update_fields(
            session_id,
            committed_updates,
            extra_filter={
                "current_turn_id": normalized_turn_id,
                watermark_field: raw_watermark,
            },
        )
        if applied:
            return {**snapshot, **committed_updates}

        latest = await worker._session_snapshots_repo.get_snapshot(session_id)
        if not isinstance(latest, dict):
            return None
        if str(latest.get("current_turn_id") or "").strip() != normalized_turn_id:
            return None
        if latest.get(watermark_field) == raw_watermark:
            raise RuntimeError(
                "terminal snapshot CAS missed without a turn or watermark change"
            )
        snapshot = latest
    return None


async def _project_turn_terminal(
    worker: Any,
    state: _BridgeRunState,
    ctx: Any,
    session_state: str,
    *,
    terminal_frame: dict[str, Any] | None = None,
    turn_failed: bool = False,
) -> None:
    session_id = ctx.session_id
    command_id = ctx.command_id
    correlation_id = ctx.correlation_id
    _build_terminal_assistant_blocks = ctx.build_terminal_assistant_blocks
    _record_background_task_manifest_if_needed = ctx.record_background_task_manifest_if_needed
    # turn_failed decouples the turn lifecycle (COMPLETED/FAILED) from the
    # session lifecycle (``session_state``: READY / RECOVERY_REQUIRED / ...).
    # A turn can fail while the conversation stays READY (e.g. the engine
    # process crashed mid-turn but the runtime self-healed / is re-borrowable).
    # When set, the turn is projected FAILED regardless of ``session_state``.
    # session_snapshots remains the truth owner; the sessions-row fields are
    # mirrored only after terminal projection succeeds.
    base_updates: dict[str, Any] = {
        "interrupt_requested": False,
    }

    # Terminalizer fence: if this turn has an unresolved
    # turn.awaiting_interaction, turn.failed is forbidden.
    # "Unresolved" = the interaction is still OPEN/active or has not
    # yet been projected (the worker died between the journal claim and
    # the interaction projection).
    # After the user answers, interaction_snapshot becomes
    # ANSWERED/active=false, and the resumed turn may legitimately
    # end in turn.failed — the fence must not block that.
    if (turn_failed or session_state != SessionState.READY.value) and state.effective_turn_id:
        # turn_failed covers a durable error terminal, so this branch owns
        # every failed turn regardless of which signal noticed first.
        awaiting_events = await worker._session_events_repo.list_events(
            session_id,
            channel="conversation",
            turn_id=state.effective_turn_id,
            event_type="turn.awaiting_interaction",
        )
        # A single turn can open multiple interactions across
        # resume cycles.  Scan from newest to oldest and fence
        # if any interaction is still unresolved.
        for _ae in reversed(awaiting_events):
            _fence_payload = _ae.get("payload") or {}
            _fence_iid = str(_fence_payload.get("interaction_id") or "").strip()
            if not _fence_iid:
                continue
            _fence_interaction = (
                await worker._interaction_snapshots_repo.get_interaction(
                    session_id, _fence_iid
                )
            )
            _interaction_resolved = (
                isinstance(_fence_interaction, dict)
                and (
                    str(_fence_interaction.get("interaction_state") or "") == "ANSWERED"
                    or not _fence_interaction.get("active")
                )
            )
            if not _interaction_resolved:
                logger.info(
                    "terminalizer fenced: turn.awaiting_interaction unresolved, "
                    "skipping turn.failed session=%s turn=%s interaction=%s",
                    session_id, state.effective_turn_id, _fence_iid,
                )
                await worker._sessions_repo.update_session(session_id, base_updates)
                state.turn_settled = True
                return

    # The outcome is derived in one place. Four signals can each answer "did
    # this turn fail" — the stream's trailing status, the live
    # `data-result.is_error` (`turn_failed`), an error event, and the durable
    # terminal — and deriving it from a different subset in each branch lets
    # two of them disagree: a failed turn recorded as COMPLETED, or "missing
    # durable finish frame proof" raised for a turn that ended in a valid
    # error.
    #
    # The terminal frame is the authority: it is durable, it is what a resume
    # reads, and it is written once. The other three decide what gets written;
    # none of them re-decides what it means.
    if turn_terminal_frame_matches(
        terminal_frame,
        turn_id=state.effective_turn_id,
        command_id=command_id,
        frame_type="error",
    ):
        turn_failed = True

    _failure_phase: str | None = None
    if not turn_failed and session_state == SessionState.READY.value:
        if not turn_terminal_frame_matches(
            terminal_frame,
            turn_id=state.effective_turn_id,
            command_id=command_id,
            frame_type="finish",
            finish_reason=AI_SDK_FINISH_REASON_STOP,
        ):
            raise RuntimeError("missing durable finish frame proof for completed turn")
        event_type = "turn.completed"
        updates = build_turn_terminal_snapshot_updates(
            turn_id=state.effective_turn_id,
            status="COMPLETED",
            error_text=None,
            command_id=command_id,
            terminal_reason=_engine_terminal_reason(state),
            terminal_frame=terminal_frame,
        )
    else:
        event_type = "turn.failed"
        _failure_phase = "post_dispatch" if state.dispatch_confirmed else "pre_dispatch"
        durable_recovery_anchor = _normalize_current_turn_remote_anchor(
            state.persisted_current_turn_remote_anchor
        )
        durable_engine_anchor = _normalize_current_turn_engine_anchor(
            state.persisted_current_turn_engine_anchor or state.current_turn_engine_anchor
        )
        has_recovery_anchor = (
            session_state == SessionState.RECOVERY_REQUIRED.value
            and isinstance(durable_recovery_anchor, dict)
        )
        has_engine_recovery_anchor = (
            session_state == SessionState.RECOVERY_REQUIRED.value
            and isinstance(durable_engine_anchor, dict)
        )
        _terminal_kwargs: dict[str, Any] = dict(
            turn_id=state.effective_turn_id,
            status="FAILED",
            error_text=state.last_error_text or None,
            command_id=command_id,
            terminal_reason=_engine_terminal_reason(state),
            recovery_anchor=durable_recovery_anchor if has_recovery_anchor else None,
            recovery_engine_anchor=(
                durable_engine_anchor if has_engine_recovery_anchor else None
            ),
            failure_phase=_failure_phase,
            terminal_frame=terminal_frame,
        )
        if _failure_phase == "pre_dispatch":
            _terminal_kwargs["delivery_state"] = "NOT_RECEIVED"
        updates = build_turn_terminal_snapshot_updates(**_terminal_kwargs)
    projected_blocks = _build_terminal_assistant_blocks()
    if session_state == SessionState.READY.value:
        projected_blocks = drop_unresolved_tool_use_blocks(projected_blocks)
    has_content = bool(
        (state.last_assistant_text or "").strip()
        or projected_blocks
        or state.accumulated_tool_results
        or state.accumulated_thinking_parts
        or state.last_result_data
    )
    if _failure_phase == "post_dispatch":
        projected_blocks = [
            *projected_blocks,
            {
                "type": "turn_failure",
                "error": state.last_error_text or "unknown error",
                "failure_phase": "post_dispatch",
            },
        ]
    projected_blocks = canonicalize_terminal_message_blocks(projected_blocks)

    event = await _find_existing_conversation_event(worker, state, ctx, event_type)
    if event is None:
        event = await worker._session_events_repo.append_event(
            {
                "session_id": session_id,
                "channel": "conversation",
                "turn_id": state.effective_turn_id or None,
                "event_type": event_type,
                "causation_id": command_id,
                "correlation_id": correlation_id,
                "payload": {
                    "command_id": command_id,
                    "final_state": session_state,
                    "assistant_text": state.last_assistant_text or None,
                    "blocks": projected_blocks,
                    "error_text": state.last_error_text or None,
                    "failure_phase": _failure_phase,
                },
            }
        )
    state.last_event_seq = int(event.get("event_seq") or state.last_event_seq)
    title_assistant_text: str | None = None
    if not turn_failed and session_state == SessionState.READY.value and has_content:
        title_assistant_text = str(
            _synthesize_assistant_text_from_blocks(projected_blocks)
            or state.last_assistant_text
        ).strip() or None
    result = await _commit_turn_terminal_snapshot(
        worker,
        session_id=session_id,
        turn_id=state.effective_turn_id,
        event_seq=state.last_event_seq,
        updates=updates,
    )
    if result is None and state.effective_turn_id:
        raise _TurnFencedOut(state.effective_turn_id)
    session_updates = build_terminal_session_mirror_updates(
        state=session_state,
        projection_result=result,
        base_updates=base_updates,
    )
    await worker._sessions_repo.update_session(session_id, session_updates)
    # On FAILED turns, deactivate any lingering active interaction
    # snapshots so they are not served as stale pending_interaction.
    # Do not deactivate on COMPLETED turns — the turn may have parked on
    # a pending interaction that must remain active until the user
    # answers.
    if turn_failed or session_state != SessionState.READY.value:
        try:
            await worker._interaction_snapshots_repo.deactivate_all_active(session_id)
        except Exception as exc:
            logger.warning(
                "deactivate stale interactions on turn terminal failed session=%s: %s",
                session_id,
                exc,
            )
    state.turn_settled = True
    if not turn_failed and session_state == SessionState.READY.value:
        await _record_background_task_manifest_if_needed()
    if (
        not turn_failed
        and session_state == SessionState.READY.value
        and state.effective_turn_id
        and title_assistant_text
        and worker._session_title_service is not None
    ):
        title_result = await worker._session_title_service.generate_for_first_completed_turn(
            session_id=session_id,
            turn_id=state.effective_turn_id,
            assistant_text=title_assistant_text,
        )
        if title_result.get("status") == "completed":
            await bridge_journal._append_frame(
                worker,
                state,
                ctx,
                {
                    "type": "data-session-changed",
                    "transient": True,
                    "data": {"sessionId": session_id},
                },
                turn_id=state.effective_turn_id,
            )
    if state.effective_turn_id and (
        (not turn_failed and session_state == SessionState.READY.value)
        or (
            turn_failed
            and str(state.last_error_text or "").strip() == USER_STOP_FAILURE_TEXT
        )
    ):
        # Settling the turn must not wait on a model call. The label is offered
        # here so it is already there when the page renders the folded header,
        # and a reader who arrives first asks for the same one over the API —
        # the generation claim lets both run without producing two labels.
        _schedule_process_summary(worker, session_id, state.effective_turn_id)


def _schedule_process_summary(worker: Any, session_id: str, turn_id: str) -> None:
    """Start the process-summary generation for a settled turn, without awaiting it.

    A worker built without a spawn function schedules nothing at all: the label
    is an offer, and a reader who opens the page asks for it over the API.
    Nothing here can fail the turn — it is already settled by this point.
    """

    spawn = getattr(worker, "_spawn_background_task", None)
    generate = getattr(
        worker._session_title_service,
        "generate_process_summary_for_turn",
        None,
    )
    if not callable(spawn) or not callable(generate):
        return

    async def _generate() -> None:
        try:
            await generate(session_id, turn_id)
        except Exception as exc:
            logger.warning(
                "process summary generation failed session=%s turn=%s err=%s",
                session_id,
                turn_id,
                exc,
            )

    coro = _generate()
    try:
        spawn(coro, name=f"process-summary:{session_id}:{turn_id}")
    except Exception as exc:
        with contextlib.suppress(Exception):
            coro.close()
        logger.warning(
            "process summary scheduling failed session=%s turn=%s err=%s",
            session_id,
            turn_id,
            exc,
        )


async def _has_durable_completed_terminal(
    worker: Any,
    state: _BridgeRunState,
    ctx: Any,
) -> bool:
    session_id = ctx.session_id
    command_id = ctx.command_id
    if not state.effective_turn_id:
        return False
    snapshot = await worker._session_snapshots_repo.get_snapshot(session_id)
    if str(snapshot.get("current_turn_id") or "").strip() == state.effective_turn_id:
        return False
    if str(snapshot.get("last_turn_id") or "").strip() != state.effective_turn_id:
        return False
    if str(snapshot.get("last_turn_status") or "").strip() != "COMPLETED":
        return False
    return turn_terminal_frame_matches(
        snapshot.get("last_turn_terminal_frame"),
        turn_id=state.effective_turn_id,
        command_id=command_id,
        frame_type="finish",
        finish_reason=AI_SDK_FINISH_REASON_STOP,
    )


async def _project_turn_terminal_with_retry(
    worker: Any,
    state: _BridgeRunState,
    ctx: Any,
    session_state: str,
    *,
    terminal_frame: dict[str, Any] | None = None,
    turn_failed: bool = False,
) -> None:
    session_id = ctx.session_id
    deadline = time.monotonic() + worker._terminal_settle_retry_window_s
    delay_s = worker._terminal_settle_retry_delay_s
    while True:
        try:
            await _project_turn_terminal(
                worker, state, ctx, session_state, terminal_frame=terminal_frame, turn_failed=turn_failed
            )
            return
        except Exception as exc:
            if not is_mongo_transient_error(exc) or time.monotonic() >= deadline:
                raise
            logger.warning(
                "session kernel terminal settle retry session=%s turn_id=%s state=%s err=%s",
                session_id,
                state.effective_turn_id or None,
                session_state,
                exc,
            )
            await asyncio.sleep(delay_s)
            delay_s = min(delay_s * 2, 2.0)
