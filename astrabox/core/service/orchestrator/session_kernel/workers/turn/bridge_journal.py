"""Durable frame-journal writer for the turn worker's bridge command.

Module functions take ``(worker, state, ctx)`` plus their explicit
parameters, where

* ``worker`` is the :class:`TurnWorker`,
* ``state`` is the per-turn :class:`_BridgeRunState`, and
* ``ctx`` bundles the ``_run_bridge_command`` locals and sibling closures the
  bodies use (``session_id``, ``command_id``, the durable queue / coalescer,
  and the frame-translation / anchor helpers).

Each function rebinds the ``ctx.*`` members it needs to local names at the
top of the body.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    coerce_int as _coerce_int,
    normalize_turn_terminal_frame,
)
from astrabox.core.service.orchestrator.engine.frame_scope import (
    engine_frame_turn_id,
    pop_engine_frame_scope,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn._fencing import (
    _TurnFencedOut,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn._helpers import (
    _wm_max,
)

if TYPE_CHECKING:
    from astrabox.core.service.orchestrator.session_kernel.workers.turn.state import (
        _BridgeRunState,
    )

logger = get_logger(__name__)


async def _persist_frames(
    worker: Any,
    state: _BridgeRunState,
    ctx: Any,
    frames: list[tuple[dict[str, Any], str | None]],
    *,
    publish: bool = True,
) -> list[dict[str, Any]]:
    session_id = ctx.session_id
    command_id = ctx.command_id
    _ensure_frame_seq_initialized = ctx.ensure_frame_seq_initialized
    _run_live_frame_mongo_op = ctx.run_live_frame_mongo_op
    _publish_broker_event = ctx.publish_broker_event
    if not frames:
        return []
    initialize_translator_seq = state.frame_seq is None
    starting_seq = await _run_live_frame_mongo_op(
        "session_event_counters.allocate_session_frame_seq",
        lambda: worker._session_events_repo.allocate_session_frame_seq(
            session_id,
            count=len(frames),
        ),
        turn_id=frames[0][1],
    )
    state.frame_seq = int(starting_seq)
    if initialize_translator_seq:
        await _ensure_frame_seq_initialized(frames[0][1])
    docs: list[dict[str, Any]] = []
    broker_events: list[dict[str, Any]] = []
    for payload_doc, payload_turn_id in frames:
        frame_payload = dict(payload_doc)
        frame_scope = pop_engine_frame_scope(frame_payload)
        if frame_scope == "session" and payload_turn_id is not None:
            raise RuntimeError("session-scoped engine frame cannot carry turn_id")
        live_seq = _coerce_int(frame_payload.pop("__live_seq", None))
        source_kind = str(frame_payload.pop("__source_kind", "") or "").strip()
        source_sandbox_turn_id = _coerce_int(
            frame_payload.pop("__source_sandbox_turn_id", None)
        )
        source_sandbox_seq = _coerce_int(
            frame_payload.pop("__source_sandbox_seq", None)
        )
        source_mirror_seq = _coerce_int(
            frame_payload.pop("__source_mirror_seq", None)
        )
        source_frame_index = _coerce_int(
            frame_payload.pop("__source_frame_index", None)
        )
        engine_kind_meta = str(
            frame_payload.pop("__engine_kind", "") or ""
        ).strip()
        engine_turn_id_meta = str(
            frame_payload.pop("__engine_turn_id", "") or ""
        ).strip()
        engine_sequence_number = _coerce_int(
            frame_payload.pop("__engine_sequence_number", None)
        )
        current_seq = int(state.frame_seq or 0)
        doc = {
            "session_id": session_id,
            "turn_id": payload_turn_id,
            "scope": frame_scope,
            "command_id": command_id,
            "frame_seq": current_seq,
            "payload": frame_payload,
            "created_at": utcnow_iso(),
        }
        broker_event = {
            "type": "ai_sdk_frame",
            "command_id": command_id,
            "turn_id": payload_turn_id,
            "scope": frame_scope,
            "frame_seq": current_seq,
            "payload": dict(frame_payload),
        }
        if live_seq is not None:
            doc["live_seq"] = live_seq
            broker_event["live_seq"] = live_seq
        source_cursor: dict[str, int] = {}
        if source_kind:
            doc["source_kind"] = source_kind
        if source_sandbox_turn_id is not None:
            doc["source_sandbox_turn_id"] = source_sandbox_turn_id
            source_cursor["sandbox_turn_id"] = source_sandbox_turn_id
        if source_sandbox_seq is not None:
            doc["source_sandbox_seq"] = source_sandbox_seq
            source_cursor["sandbox_seq"] = source_sandbox_seq
        if source_mirror_seq is not None:
            doc["source_mirror_seq"] = source_mirror_seq
            source_cursor["mirror_seq"] = source_mirror_seq
        if source_frame_index is not None:
            doc["source_frame_index"] = source_frame_index
        if engine_kind_meta:
            doc["engine_kind"] = engine_kind_meta
        if engine_turn_id_meta:
            doc["engine_turn_id"] = engine_turn_id_meta
        if engine_sequence_number is not None:
            doc["engine_sequence_number"] = int(engine_sequence_number)
        if live_seq is not None:
            source_cursor["live_seq"] = live_seq
        if source_cursor:
            broker_event["source_cursor"] = source_cursor
        docs.append(doc)
        broker_events.append(broker_event)
        state.frame_seq = current_seq + 1

    if len(docs) == 1:
        await _run_live_frame_mongo_op(
            "session_events.append_frame",
            lambda: worker._session_events_repo.append_frame(docs[0]),
            turn_id=frames[0][1],
        )
    else:
        append_frames = getattr(worker._session_events_repo, "append_frames", None)
        if callable(append_frames):
            await _run_live_frame_mongo_op(
                "session_events.append_frames",
                lambda: append_frames(docs),
                turn_id=frames[0][1],
            )
        else:
            for doc, payload_turn_id in zip(docs, frames, strict=False):
                await _run_live_frame_mongo_op(
                    "session_events.append_frame",
                    lambda doc=doc: worker._session_events_repo.append_frame(doc),
                    turn_id=payload_turn_id[1],
                )

    if publish:
        for broker_event in broker_events:
            await _publish_broker_event(broker_event)
    return docs


def _raise_durable_frame_writer_error(state: _BridgeRunState) -> None:
    if state.durable_frame_writer_error is not None:
        detail = str(state.durable_frame_writer_error).strip()
        cause_type = type(state.durable_frame_writer_error).__name__
        suffix = f": {cause_type}{f': {detail}' if detail else ''}"
        raise RuntimeError(f"live frame durable writer failed{suffix}") from state.durable_frame_writer_error


async def _enqueue_durable_frames(
    worker: Any,
    state: _BridgeRunState,
    ctx: Any,
    frames: list[tuple[dict[str, Any], str | None]],
    max_cursor_seq: int | None,
) -> None:
    durable_frame_queue = ctx.durable_frame_queue
    _raise_durable_frame_writer_error(state)
    await durable_frame_queue.put((frames, max_cursor_seq))
    _raise_durable_frame_writer_error(state)


async def _flush_durable_semantic_frames(
    worker: Any,
    state: _BridgeRunState,
    ctx: Any,
) -> None:
    durable_semantic_coalescer = ctx.durable_semantic_coalescer
    _max_source_cursor_seq = ctx.max_source_cursor_seq
    frames = durable_semantic_coalescer.flush()
    if not frames:
        return
    await _enqueue_durable_frames(
        worker,
        state,
        ctx,
        [
            (
                dict(frame_doc),
                engine_frame_turn_id(
                    frame_doc,
                    state.effective_turn_id or None,
                ),
            )
            for frame_doc in frames
        ],
        _max_source_cursor_seq(frames),
    )


async def _durable_frame_writer_loop(
    worker: Any,
    state: _BridgeRunState,
    ctx: Any,
) -> None:
    session_id = ctx.session_id
    durable_frame_queue = ctx.durable_frame_queue
    while True:
        item = await durable_frame_queue.get()
        items = [item]
        try:
            if item is None:
                return

            stop_after_flush = False
            while True:
                try:
                    next_item = durable_frame_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                items.append(next_item)
                if next_item is None:
                    stop_after_flush = True
                    break

            frames: list[tuple[dict[str, Any], str | None]] = []
            max_cursor_seq: int | None = None
            for queued_item in items:
                if queued_item is None:
                    continue
                item_frames, item_max_cursor_seq = queued_item
                frames.extend(item_frames)
                max_cursor_seq = _wm_max(max_cursor_seq, item_max_cursor_seq)

            if frames:
                await _persist_frames(worker, state, ctx, frames)
            if stop_after_flush:
                return
        except Exception as exc:
            state.durable_frame_writer_error = exc
            logger.exception(
                "session kernel live frame durable writer failed session=%s turn=%s frame_count=%s max_cursor_seq=%s",
                session_id,
                state.effective_turn_id or None,
                len(frames) if "frames" in locals() else None,
                max_cursor_seq if "max_cursor_seq" in locals() else None,
            )
            while True:
                try:
                    durable_frame_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                else:
                    durable_frame_queue.task_done()
            return
        finally:
            for _ in items:
                durable_frame_queue.task_done()


async def _drain_durable_frame_queue(
    worker: Any,
    state: _BridgeRunState,
    ctx: Any,
) -> None:
    durable_frame_queue = ctx.durable_frame_queue
    _raise_durable_frame_writer_error(state)
    await _flush_durable_semantic_frames(worker, state, ctx)
    await durable_frame_queue.join()
    _raise_durable_frame_writer_error(state)


async def _stop_durable_frame_writer(
    worker: Any,
    state: _BridgeRunState,
    ctx: Any,
    *,
    raise_on_error: bool = True,
) -> Exception | None:
    durable_frame_queue = ctx.durable_frame_queue
    if state.durable_frame_writer_task is None:
        return state.durable_frame_writer_error
    if not state.durable_frame_writer_task.done():
        await _flush_durable_semantic_frames(worker, state, ctx)
        await durable_frame_queue.put(None)
        join_task = asyncio.create_task(durable_frame_queue.join())
        try:
            done, pending = await asyncio.wait(
                {join_task, state.durable_frame_writer_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if state.durable_frame_writer_task in done and not join_task.done():
                join_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await join_task
            else:
                with contextlib.suppress(asyncio.CancelledError):
                    await state.durable_frame_writer_task
        finally:
            if not join_task.done():
                join_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await join_task
        with contextlib.suppress(asyncio.CancelledError):
            await state.durable_frame_writer_task
    if raise_on_error:
        _raise_durable_frame_writer_error(state)
    return state.durable_frame_writer_error


async def _publish_persisted_frame(
    worker: Any,
    state: _BridgeRunState,
    ctx: Any,
    frame_doc: dict[str, Any] | None,
) -> None:
    _publish_broker_event = ctx.publish_broker_event
    if not isinstance(frame_doc, dict):
        return
    payload = frame_doc.get("payload")
    if not isinstance(payload, dict):
        return
    await _publish_broker_event(
        _event_with_optional_live_seq(
            {
                "type": "ai_sdk_frame",
                "command_id": str(frame_doc.get("command_id") or "").strip() or None,
                "turn_id": str(frame_doc.get("turn_id") or "").strip() or None,
                "scope": frame_doc.get("scope"),
                "frame_seq": int(frame_doc.get("frame_seq") or 0),
                "payload": dict(payload),
                "live_seq": _coerce_int(frame_doc.get("live_seq")),
            }
        )
    )


def _event_with_optional_live_seq(fields: dict[str, Any]) -> dict[str, Any]:
    event = {
        key: value
        for key, value in fields.items()
        if key != "live_seq"
    }
    live_seq = _coerce_int(fields.get("live_seq"))
    if live_seq is not None:
        event["live_seq"] = live_seq
    return event


def _terminal_frame_proof_from_doc(frame_doc: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(frame_doc, dict):
        return None
    payload = frame_doc.get("payload")
    if not isinstance(payload, dict):
        return None
    proof: dict[str, Any] = {
        "turn_id": str(frame_doc.get("turn_id") or "").strip(),
        "command_id": str(frame_doc.get("command_id") or "").strip(),
        "frame_seq": int(frame_doc.get("frame_seq") or 0),
        "type": str(payload.get("type") or "").strip(),
    }
    finish_reason = str(payload.get("finishReason") or payload.get("finish_reason") or "").strip()
    if finish_reason:
        proof["finish_reason"] = finish_reason
    return normalize_turn_terminal_frame(proof)


async def _append_frame(
    worker: Any,
    state: _BridgeRunState,
    ctx: Any,
    frame: dict[str, Any],
    *,
    turn_id: str | None = None,
    publish: bool = True,
    require_durable_queue_clean: bool = True,
) -> dict[str, Any] | None:
    payload_doc = dict(frame)
    if require_durable_queue_clean:
        await _drain_durable_frame_queue(worker, state, ctx)
    docs = await _persist_frames(worker, state, ctx, [(payload_doc, turn_id)], publish=publish)
    return docs[0] if docs else None


async def _append_engine_diagnostic(
    worker: Any,
    state: _BridgeRunState,
    ctx: Any,
    event: dict[str, Any],
) -> dict[str, Any]:
    """Persist one private adapter diagnostic outside the browser frame log."""

    diagnostic_event_type = str(event.get("event_type") or "").strip()
    diagnostic_subtype = str(event.get("subtype") or "").strip()
    diagnostic_engine_kind = str(event.get("engine_kind") or "").strip()
    if not diagnostic_event_type or not diagnostic_subtype or not diagnostic_engine_kind:
        raise RuntimeError("engine diagnostic envelope is malformed")
    return await worker._session_events_repo.append_event(
        {
            "session_id": ctx.session_id,
            "channel": "engine",
            "turn_id": state.effective_turn_id or None,
            "event_type": "engine.diagnostic",
            "causation_id": ctx.command_id,
            "correlation_id": ctx.correlation_id,
            "payload": {
                "engine_kind": diagnostic_engine_kind,
                "engine_turn_id": str(event.get("engine_turn_id") or "").strip()
                or None,
                "event_type": diagnostic_event_type,
                "subtype": diagnostic_subtype,
                "raw": event.get("raw"),
            },
        }
    )


def _normalize_frame(
    worker: Any,
    state: _BridgeRunState,
    ctx: Any,
    frame: dict[str, Any],
) -> dict[str, Any]:
    command_id = ctx.command_id
    normalized = dict(frame)
    frame_type = str(normalized.get("type") or "").strip()
    if frame_type == "data-result" and "id" not in normalized:
        result_id = str(state.effective_turn_id or command_id or "").strip()
        if result_id:
            normalized["id"] = f"result:{result_id}"
    if frame_type == "data-turn-failure" and "id" not in normalized:
        failure_id = str(state.effective_turn_id or command_id or "").strip()
        if failure_id:
            normalized["id"] = f"turn-failure:{failure_id}"
    if frame_type == "data-raw-event" and "id" not in normalized:
        raw_id = str(state.effective_turn_id or command_id or "").strip()
        frame_data = normalized.get("data")
        subtype = ""
        if isinstance(frame_data, dict):
            subtype = str(frame_data.get("subtype") or "").strip()
        if raw_id:
            normalized["id"] = f"raw-event:{raw_id}:{subtype or 'system'}"
    if frame_type == "data-api-retry" and "id" not in normalized:
        normalized["id"] = "api-retry"
    return normalized
