"""The platform Session output subscription shared by every channel.

HTTP streaming and durable channel delivery use the same journal reader, reply
boundaries and replay rules. Transport adapters only render its output.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.persistence.repository.backend import is_mongo_transient_error
from astrabox.core.service.orchestrator.engine.frame_scope import EngineFrameScope, stored_engine_frame_scope
from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    AI_SDK_FINISH_REASON_STOP, SDK_RESPONSE_RESULT_BOUNDARY,
    coerce_int as _coerce_int, derive_turn_recovery_phase,
)
from astrabox.core.service.orchestrator.session_kernel.service_mixins._helpers import (
    _ACTIVE_SESSION_STATES, _ACTIVE_CONVERSATION_SNAPSHOT_STATES,
    _build_resume_cursor_payload, _ResumeCursorTracker,
    _emit_resumable_payloads, _payload_is_turn_terminal,
)

logger = get_logger(__name__)


@dataclass
class SessionOutputResponse:
    """One mainline reply, reconstructed without storing another message copy."""

    response_id: str
    turn_id: str
    start_after_seq: int
    response_seq: int
    text: str = ""
    complete: bool = False
    error: str | None = None


@dataclass
class SessionOutputBatch:
    responses: list[SessionOutputResponse]
    after_seq: int


_TailDurablePayload = tuple[
    dict[str, Any],
    int,
    EngineFrameScope,
    str | None,
    str | None,
    int | None,
]


class _TailFrameState:
    """Mutable tail state for :meth:`SessionOutputSubscriptionMixin._tail_frames`,
    threaded through the sectioned non-yielding tail helpers so the yielding
    multiplexer core can stay a single generator."""

    def __init__(
        self,
        *,
        session_id: str,
        command_id: str | None,
        turn_id: str | None,
        after_seq: int,
        include_terminal_resume_cursor: bool,
        stop_on_segment_finish: bool,
        follow_session: bool = False,
        response_messages: bool = False,
        acknowledged_after_seq: int = -1,
    ) -> None:
        self.session_id = session_id
        self.command_id = command_id
        self.turn_id = turn_id
        self.include_terminal_resume_cursor = include_terminal_resume_cursor
        self.stop_on_segment_finish = stop_on_segment_finish
        #: Select frames session-wide and wait while the session is idle. The
        #: response still closes at a reply boundary so one AI SDK
        #: parser never receives two assistant messages.
        self.follow_session = follow_session
        self.response_messages = response_messages
        self.last_seq = int(after_seq)
        self.acknowledged_after_seq = int(acknowledged_after_seq)
        self.cursor_tracker = _ResumeCursorTracker(response_messages=response_messages)
        # Live counters restart for each command, including an interaction
        # continuation within the same turn. Only copies from that command
        # share an identity; the Session-wide durable cursor is separate.
        self.emitted_live_keys: set[tuple[str | None, int]] = set()
        self.durable_live_keys: set[tuple[str | None, int]] = set()
        # Bounded grace window for reconciling an ERROR terminal
        # against its durable counterpart / producer completion while the
        # producer is still alive (unset until the first such error).
        self.error_settle_deadline: float | None = None

    def note_error_terminal_seen(self, window_s: float) -> None:
        """Start the bounded grace window the first time a live or durable
        ERROR terminal is seen while the producer is still alive. A no-op on
        subsequent calls, so the window is measured from first sighting."""
        if self.error_settle_deadline is None:
            self.error_settle_deadline = time.monotonic() + max(0.0, float(window_s))

    @property
    def error_grace_expired(self) -> bool:
        """True once the bounded grace window has elapsed, so the tail
        settles instead of waiting forever on a producer that never
        completes and never emits a durable counterpart for the error it
        already surfaced live."""
        return (
            self.error_settle_deadline is not None
            and time.monotonic() >= self.error_settle_deadline
        )


class SessionOutputSubscriptionMixin:
    """One output consumer for HTTP and durable channel deliveries."""

    async def follow_session_output(
        self, session_id: str, *, after_seq: int = -1,
        available_only: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        """Follow one reply, waiting for output unless draining durable work.

        Access checks belong to the caller: HTTP authorizes the user and the
        channel spine authorizes its persisted destination binding. Durable
        consumers retain a reply cursor. Every consumer rebuilds parser state
        from the same safe boundary after a disconnect or a partial read.
        """
        await self._observe_session_output(session_id)
        safe_after_seq = await self._rewind_unsafe_resume_cursor(
            session_id, requested_after_seq=after_seq,
        )
        async for frame in self._tail_frames(
            session_id, after_seq=safe_after_seq, acknowledged_after_seq=after_seq,
            follow_session=True, response_messages=True,
            include_terminal_resume_cursor=True, stop_on_segment_finish=False,
            available_only=available_only,
        ):
            yield frame

    async def _observe_session_output(self, session_id: str) -> None:
        session = await self._sessions_repo.get_session(session_id)
        if session is not None:
            try:
                await self._turn_service.observe_existing_engine_output(session)
            except Exception:
                # Existing durable output is readable even when live attachment
                # fails. The next subscription attempt retries observation.
                logger.exception("Session output attachment failed session=%s", session_id)

    async def session_output_cursor(self, session_id: str) -> int:
        """Snapshot the shared journal position when attaching a destination."""
        events = await self._session_events_repo.list_events(
            session_id, newest_first=True, limit=1,
        )
        frames = await self._session_events_repo.list_frames(
            session_id, newest_first=True, limit=1,
        )
        return max(
            int(events[0]["event_seq"]) if events else 0,
            int(frames[0]["frame_seq"]) if frames else 0,
        )

    async def read_session_output(
        self, session_id: str, *, after_seq: int,
    ) -> SessionOutputBatch:
        """Drain up to fifty reply boundaries without waiting for another input."""
        responses: list[SessionOutputResponse] = []
        cursor = after_seq
        for _ in range(50):
            batch = await self._read_available_response(session_id, after_seq=cursor)
            responses.extend(batch.responses)
            if batch.after_seq <= cursor:
                break
            cursor = batch.after_seq
            if any(not response.complete for response in batch.responses):
                break
        return SessionOutputBatch(responses=responses, after_seq=cursor)

    async def _read_available_response(
        self, session_id: str, *, after_seq: int,
    ) -> SessionOutputBatch:
        """Drain available output through the same subscription used by Web.

        A partial reply retains its starting cursor so reconstruction includes
        earlier text and tool dependencies. Completed resident messages carry
        their own identities and never borrow an input command's identity.
        """
        responses: list[SessionOutputResponse] = []
        active: SessionOutputResponse | None = None
        separate_text_block = False
        cursor = after_seq
        committed = after_seq
        async for payload in self.follow_session_output(
            session_id, after_seq=after_seq, available_only=True,
        ):
            kind = payload.get("type")
            if kind == "data-resume-cursor":
                cursor = int(payload["data"]["frameSeq"])
                if active is None or active.complete:
                    committed = cursor
            elif kind == "data-session-message":
                message = payload["data"]
                if (
                    message.get("role") == "assistant" and message["is_response"]
                    and int(message["source_event_seq_applied"]) > after_seq
                ):
                    responses.append(SessionOutputResponse(
                        response_id=str(message["message_id"]),
                        turn_id=str(message.get("turn_id") or ""),
                        start_after_seq=after_seq,
                        response_seq=int(message["source_event_seq_applied"]),
                        text=str(message.get("content") or ""), complete=True,
                    ))
            elif kind == "start":
                if active is not None and not active.complete:
                    raise RuntimeError("Session output started a reply before completing its predecessor")
                active = SessionOutputResponse(
                    response_id=str(payload["messageId"]),
                    turn_id=str((payload.get("messageMetadata") or {}).get("turn_id") or ""),
                    start_after_seq=after_seq, response_seq=cursor + 1,
                )
                responses.append(active)
                separate_text_block = False
            elif kind == "text-start" and active is not None:
                # Web renders each text part independently. A plain-text
                # destination needs the same boundary between paragraphs.
                separate_text_block = bool(active.text)
            elif kind == "text-delta" and active is not None:
                delta = str(payload.get("delta") or "")
                if delta:
                    if separate_text_block:
                        active.text += "\n\n"
                        separate_text_block = False
                    active.text += delta
            elif active is not None and (
                _payload_is_turn_terminal(payload)
                or (payload.get("messageMetadata") or {}).get("response_boundary") is True
            ):
                active.complete = True
                if kind == "error":
                    active.error = str(payload.get("errorText") or "response failed")
                committed = cursor
        if active is not None and not active.complete:
            committed = active.start_after_seq
        return SessionOutputBatch(responses=responses, after_seq=committed)

    async def _rewind_unsafe_resume_cursor(
        self,
        session_id: str,
        *,
        requested_after_seq: int,
    ) -> int:
        """Rebuild the current reply for the SDK's fresh resume parser.

        A cursor names received frames, not parser state. Replay the current
        reply from its consumed-input boundary so its identity, earlier text,
        and completed tools survive replacement of the browser's last message.
        Completed FIFO replies stay outside the next response.
        """
        requested = int(requested_after_seq)
        if requested < 0:
            return -1

        tracker = _ResumeCursorTracker()
        safe_after_seq = -1
        scanned_after_seq = -1
        response_after_seq: int | None = None
        page_size = 500
        while scanned_after_seq < requested:
            frames = await self._session_events_repo.list_frames(
                session_id,
                after_seq=scanned_after_seq,
                limit=page_size,
            )
            if not frames:
                break
            advanced = False
            for frame in frames:
                frame_seq = int(frame.get("frame_seq") or 0)
                if frame_seq > requested:
                    break
                if frame_seq <= scanned_after_seq:
                    continue
                advanced = True
                scanned_after_seq = frame_seq
                payload = frame.get("payload")
                if not isinstance(payload, dict):
                    continue
                if payload.get("type") in {"start", "data-input-consumed"}:
                    response_after_seq = frame_seq - 1
                elif (
                    payload.get(SDK_RESPONSE_RESULT_BOUNDARY) is True
                    or _payload_is_turn_terminal(payload)
                ):
                    response_after_seq = None
                tracker.observe(
                    payload,
                    scope=str(frame.get("turn_id") or "").strip() or None,
                )
                if tracker.can_resume_after_current_frame():
                    safe_after_seq = frame_seq
            if scanned_after_seq >= requested or not advanced or len(frames) < page_size:
                break

        if response_after_seq is not None:
            safe_after_seq = min(safe_after_seq, response_after_seq)
        if safe_after_seq != requested:
            logger.info(
                "rewound unsafe ai-stream resume cursor session=%s requested=%s safe=%s",
                session_id,
                requested,
                safe_after_seq,
            )
        return safe_after_seq

    def _tail_payload_should_stop(
        self, state: _TailFrameState, payload: dict[str, Any]
    ) -> bool:
        if state.response_messages and payload.get(SDK_RESPONSE_RESULT_BOUNDARY) is True:
            return True
        if state.stop_on_segment_finish:
            return str(payload.get("type") or "").strip() in {"finish", "error"}
        return _payload_is_turn_terminal(payload)

    async def _tail_retry_transient_read(
        self,
        state: _TailFrameState,
        operation: str,
        read: Callable[[], Awaitable[Any]],
    ) -> Any:
        deadline = time.monotonic() + self._turn_terminal_settle_retry_window_s
        delay_s = self._turn_terminal_settle_retry_delay_s
        while True:
            try:
                return await read()
            except Exception as exc:
                if (
                    not is_mongo_transient_error(exc)
                    or time.monotonic() >= deadline
                ):
                    raise
                logger.warning(
                    "session kernel tail read retry session=%s op=%s err=%s",
                    state.session_id,
                    operation,
                    exc,
                )
                await asyncio.sleep(delay_s)
                delay_s = min(delay_s * 2, 2.0)

    async def _tail_ensure_finish_frame_visible(
        self,
        state: _TailFrameState,
        *,
        payload: dict[str, Any],
        frame_seq: int,
        payload_turn_id: str | None,
        payload_command_id: str | None,
    ) -> None:
        if str(payload.get("type") or "").strip() != "finish":
            return
        finish_reason = str(payload.get("finishReason") or payload.get("finish_reason") or "").strip()
        if finish_reason != AI_SDK_FINISH_REASON_STOP:
            return
        resolved_turn_id = str(payload_turn_id or state.turn_id or "").strip()
        resolved_command_id = str(payload_command_id or state.command_id or "").strip()
        if not resolved_turn_id or not resolved_command_id:
            raise APIError(
                code="STREAM_PROTOCOL_VIOLATION",
                message="finish frame is missing turn identity",
                status_code=500,
            )
        _ = frame_seq

    async def _tail_read_available_frames(
        self, state: _TailFrameState
    ) -> tuple[list[_TailDurablePayload], bool]:
        saw_finish = False
        payloads: list[_TailDurablePayload] = []
        frames = await self._tail_retry_transient_read(
            state,
            "list_frames",
            lambda: self._session_events_repo.list_frames(
                state.session_id,
                command_id=state.command_id,
                turn_id=state.turn_id,
                after_seq=state.last_seq,
            ),
        )
        if state.follow_session:
            resident_frames = await self._tail_retry_transient_read(
                state,
                "resident_message_frames",
                lambda: self._message_view.resident_message_frames(
                    state.session_id,
                    after_seq=state.last_seq,
                    before_seq=(
                        max(int(frame["frame_seq"]) for frame in frames) + 1
                        if frames else None
                    ),
                ),
            )
            frames = sorted(
                [*frames, *resident_frames], key=lambda frame: int(frame["frame_seq"])
            )
        for frame in frames:
            frame_seq = int(frame.get("frame_seq") or 0)
            state.last_seq = max(state.last_seq, frame_seq)
            payload = frame.get("payload")
            if not isinstance(payload, dict):
                continue
            if (
                payload.get("type") == "data-session-store-reload"
                and frame_seq <= state.acknowledged_after_seq
            ):
                # Reply content rebuilds the fresh SDK parser; an acknowledged
                # history-reload command must not restart that rebuild.
                live_seq = _coerce_int(frame.get("live_seq"))
                if live_seq is not None:
                    state.durable_live_keys.add((
                        str(frame.get("command_id") or "").strip() or None,
                        live_seq,
                    ))
                continue
            if self._tail_payload_should_stop(state, payload):
                saw_finish = True
            payloads.append(
                (
                    payload,
                    frame_seq,
                    stored_engine_frame_scope(frame.get("scope")),
                    str(frame.get("turn_id") or "").strip() or None,
                    str(frame.get("command_id") or "").strip() or None,
                    _coerce_int(frame.get("live_seq")),
                )
            )
        return payloads, saw_finish

    def _tail_extract_durable_broker_frame(
        self,
        state: _TailFrameState,
        event: Any,
    ) -> _TailDurablePayload | None:
        if not isinstance(event, dict):
            return None
        if str(event.get("type") or "").strip() != "ai_sdk_frame":
            return None
        event_command_id = str(event.get("command_id") or "").strip() or None
        if state.command_id is not None and event_command_id != state.command_id:
            return None
        event_turn_id = str(event.get("turn_id") or "").strip() or None
        if state.turn_id is not None and event_turn_id != state.turn_id:
            return None
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return None
        _fsv = event.get("frame_seq")
        frame_seq = int(_fsv) if _fsv is not None else -1
        if frame_seq <= state.last_seq:
            return None
        state.last_seq = frame_seq
        return (
            payload,
            frame_seq,
            stored_engine_frame_scope(event.get("scope")),
            event_turn_id,
            event_command_id,
            _coerce_int(event.get("live_seq")),
        )

    @staticmethod
    def _tail_is_stream_complete(state: _TailFrameState, event: Any) -> bool:
        """Whether this broker event is this command's end-of-stream announcement.

        The bridge publishes it when production stops, including an interaction
        boundary where the segment closes while the worker remains alive.
        Reading task completion would conflate stream completion with unrelated
        post-settle worker work and cannot represent a parked worker.
        """
        if not isinstance(event, dict):
            return False
        if str(event.get("type") or "").strip() != "ai_sdk_stream_complete":
            return False
        event_command_id = str(event.get("command_id") or "").strip() or None
        if state.command_id is not None and event_command_id != state.command_id:
            return False
        event_turn_id = str(event.get("turn_id") or "").strip() or None
        return not (state.turn_id is not None and event_turn_id != state.turn_id)

    def _tail_extract_live_broker_frame(
        self,
        state: _TailFrameState,
        event: Any,
    ) -> tuple[dict[str, Any], int, str | None, str | None] | None:
        if not isinstance(event, dict):
            return None
        if str(event.get("type") or "").strip() != "ai_sdk_live_frame":
            return None
        event_command_id = str(event.get("command_id") or "").strip() or None
        if state.command_id is not None and event_command_id != state.command_id:
            return None
        event_turn_id = str(event.get("turn_id") or "").strip() or None
        if state.turn_id is not None and event_turn_id != state.turn_id:
            return None
        live_seq = _coerce_int(event.get("live_seq"))
        if live_seq is None:
            return None
        live_key = (event_command_id, live_seq)
        if live_key in state.durable_live_keys or live_key in state.emitted_live_keys:
            return None
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return None
        state.emitted_live_keys.add(live_key)
        return payload, live_seq, event_turn_id, event_command_id

    def _tail_emit_durable_cursor_for_live_frame(
        self,
        state: _TailFrameState,
        *,
        payload: dict[str, Any],
        frame_seq: int,
        payload_turn_id: str | None,
    ) -> list[dict[str, Any]]:
        # The live broker and durable journal are independent deliveries.  A
        # durable frame can carry a live_seq already recorded by this tail even
        # when the corresponding live payload did not make it through the
        # parser/tracker path (for example, around reconnect queue draining).
        # Re-observing is intentionally idempotent: it repairs that gap and
        # prevents a cursor from landing after tool-input-start but before the
        # invocation's output/denial dependency has arrived.
        state.cursor_tracker.observe(payload, scope=payload_turn_id)
        if (
            not state.include_terminal_resume_cursor
            and str(payload.get("type") or "") in {"finish", "error"}
        ):
            return []
        if not state.cursor_tracker.can_resume_after_current_frame():
            return []
        return [
            _build_resume_cursor_payload(
                frame_seq=frame_seq,
                turn_id=payload_turn_id,
            )
        ]

    async def _tail_frames(
        self,
        session_id: str,
        *,
        command_id: str | None = None,
        turn_id: str | None = None,
        producer_task: asyncio.Task | None = None,
        after_seq: int = -1,
        include_terminal_resume_cursor: bool = True,
        stop_on_segment_finish: bool = True,
        follow_session: bool = False,
        response_messages: bool = False,
        acknowledged_after_seq: int = -1,
        available_only: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        tail_state = _TailFrameState(
            session_id=session_id,
            command_id=command_id,
            turn_id=turn_id,
            after_seq=after_seq,
            include_terminal_resume_cursor=include_terminal_resume_cursor,
            stop_on_segment_finish=stop_on_segment_finish,
            follow_session=follow_session,
            response_messages=response_messages,
            acknowledged_after_seq=acknowledged_after_seq,
        )
        queue = await self._broker.subscribe(session_id)
        pending_storage_sync = True

        try:
            seen_terminal_payload = False
            stream_complete_seen = False
            while True:
                if pending_storage_sync:
                    payloads, saw_finish = await self._tail_read_available_frames(tail_state)
                    pending_storage_sync = False
                    terminal_seen_in_batch = False
                    for (
                        payload,
                        frame_seq,
                        payload_scope,
                        payload_turn_id,
                        payload_command_id,
                        live_seq,
                    ) in payloads:
                        if live_seq is not None:
                            tail_state.durable_live_keys.add((payload_command_id, live_seq))
                        await self._tail_ensure_finish_frame_visible(
                            tail_state,
                            payload=payload,
                            frame_seq=frame_seq,
                            payload_turn_id=payload_turn_id,
                            payload_command_id=payload_command_id,
                        )
                        if live_seq is not None and (payload_command_id, live_seq) in tail_state.emitted_live_keys:
                            emitted = self._tail_emit_durable_cursor_for_live_frame(
                                tail_state,
                                payload=payload,
                                frame_seq=frame_seq,
                                payload_turn_id=payload_turn_id,
                            )
                            payload_is_finish = False
                        else:
                            emitted, payload_is_finish = _emit_resumable_payloads(
                                cursor_tracker=tail_state.cursor_tracker,
                                payload=payload,
                                frame_seq=frame_seq,
                                payload_scope=payload_scope,
                                payload_turn_id=payload_turn_id,
                                include_resume_cursor=True,
                                include_terminal_resume_cursor=include_terminal_resume_cursor,
                            )
                        for item in emitted:
                            yield item
                        if payload_is_finish and self._tail_payload_should_stop(tail_state, payload):
                            seen_terminal_payload = True
                            terminal_seen_in_batch = True
                            break
                    if terminal_seen_in_batch:
                        if producer_task is None:
                            return
                        continue
                    if saw_finish and not tail_state.follow_session:
                        # The stream-close settle contract is enforced at the
                        # single chokepoint in resume_command_stream, after this
                        # generator completes — not per-exit here.
                        return

                if available_only:
                    if not payloads:
                        return
                    pending_storage_sync = True
                    continue

                if producer_task is None and not tail_state.follow_session:
                    # An idle session ends a turn-scoped replay. A session
                    # follower instead waits here for the next accepted turn.
                    session = await self._tail_retry_transient_read(
                        tail_state,
                        "get_session_for_resume_idle_check",
                        lambda: self._sessions_repo.get_session(session_id),
                    )
                    if session is None:
                        return
                    state = str(session.get("state") or "")
                    if state not in _ACTIVE_SESSION_STATES:
                        snapshot = await self._tail_retry_transient_read(
                            tail_state,
                            "get_snapshot_for_resume_idle_check",
                            lambda: self._session_snapshots_repo.get_snapshot(session_id),
                        )
                        snapshot_state = str((snapshot or {}).get("conversation_state") or "")
                        if (
                            snapshot_state not in _ACTIVE_CONVERSATION_SNAPSHOT_STATES
                            and derive_turn_recovery_phase(snapshot) is None
                        ):
                            return

                try:
                    broker_event = await asyncio.wait_for(queue.get(), timeout=self._poll_interval_s)
                except asyncio.TimeoutError:
                    producer_done = producer_task is not None and producer_task.done()
                    # An error terminal keeps the tail open past producer
                    # death to reconcile the durable counterpart, but a
                    # perpetually-alive producer must not wait forever — once
                    # the grace window (started when the error was first
                    # seen) elapses, settle exactly like a dead producer would,
                    # minus awaiting a task that never finished.
                    if producer_done or stream_complete_seen or tail_state.error_grace_expired:
                        producer_error: Exception | None = None
                        if producer_done:
                            try:
                                await producer_task
                            except Exception as exc:
                                producer_error = exc
                        pending_storage_sync = True
                        payloads, _ = await self._tail_read_available_frames(tail_state)
                        has_terminal_payload = seen_terminal_payload
                        for (
                            payload,
                            frame_seq,
                            payload_scope,
                            payload_turn_id,
                            payload_command_id,
                            live_seq,
                        ) in payloads:
                            if live_seq is not None:
                                tail_state.durable_live_keys.add((payload_command_id, live_seq))
                            await self._tail_ensure_finish_frame_visible(
                                tail_state,
                                payload=payload,
                                frame_seq=frame_seq,
                                payload_turn_id=payload_turn_id,
                                payload_command_id=payload_command_id,
                            )
                            if live_seq is not None and (payload_command_id, live_seq) in tail_state.emitted_live_keys:
                                emitted = self._tail_emit_durable_cursor_for_live_frame(
                                    tail_state,
                                    payload=payload,
                                    frame_seq=frame_seq,
                                    payload_turn_id=payload_turn_id,
                                )
                                payload_is_finish = False
                            else:
                                emitted, payload_is_finish = _emit_resumable_payloads(
                                    cursor_tracker=tail_state.cursor_tracker,
                                    payload=payload,
                                    frame_seq=frame_seq,
                                    payload_scope=payload_scope,
                                    payload_turn_id=payload_turn_id,
                                    include_resume_cursor=True,
                                    include_terminal_resume_cursor=include_terminal_resume_cursor,
                                )
                            if payload_is_finish and self._tail_payload_should_stop(tail_state, payload):
                                has_terminal_payload = True
                                seen_terminal_payload = True
                            for item in emitted:
                                yield item
                        while True:
                            try:
                                queued_event = queue.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                            live_frame = self._tail_extract_live_broker_frame(tail_state, queued_event)
                            if live_frame is not None:
                                payload, _live_seq, payload_turn_id, payload_command_id = live_frame
                                emitted, payload_is_finish = _emit_resumable_payloads(
                                    cursor_tracker=tail_state.cursor_tracker,
                                    payload=payload,
                                    payload_scope="turn",
                                    payload_turn_id=payload_turn_id,
                                    include_resume_cursor=False,
                                )
                                if payload_is_finish and self._tail_payload_should_stop(tail_state, payload):
                                    has_terminal_payload = True
                                    seen_terminal_payload = True
                                for item in emitted:
                                    yield item
                                continue
                            durable_frame = self._tail_extract_durable_broker_frame(tail_state, queued_event)
                            if durable_frame is None:
                                continue
                            (
                                payload,
                                frame_seq,
                                payload_scope,
                                payload_turn_id,
                                payload_command_id,
                                live_seq,
                            ) = durable_frame
                            if live_seq is not None:
                                tail_state.durable_live_keys.add((payload_command_id, live_seq))
                            await self._tail_ensure_finish_frame_visible(
                                tail_state,
                                payload=payload,
                                frame_seq=frame_seq,
                                payload_turn_id=payload_turn_id,
                                payload_command_id=payload_command_id,
                            )
                            if live_seq is not None and (payload_command_id, live_seq) in tail_state.emitted_live_keys:
                                emitted = self._tail_emit_durable_cursor_for_live_frame(
                                    tail_state,
                                    payload=payload,
                                    frame_seq=frame_seq,
                                    payload_turn_id=payload_turn_id,
                                )
                                payload_is_finish = False
                            else:
                                emitted, payload_is_finish = _emit_resumable_payloads(
                                    cursor_tracker=tail_state.cursor_tracker,
                                    payload=payload,
                                    frame_seq=frame_seq,
                                    payload_scope=payload_scope,
                                    payload_turn_id=payload_turn_id,
                                    include_resume_cursor=True,
                                    include_terminal_resume_cursor=include_terminal_resume_cursor,
                                )
                            if payload_is_finish and self._tail_payload_should_stop(tail_state, payload):
                                has_terminal_payload = True
                                seen_terminal_payload = True
                            for item in emitted:
                                yield item
                        if producer_error is not None:
                            if not has_terminal_payload:
                                raise producer_error
                            snapshot = await self._tail_retry_transient_read(
                                tail_state,
                                "get_snapshot_after_producer_error",
                                lambda: self._session_snapshots_repo.get_snapshot(session_id),
                            )
                            snapshot_state = str((snapshot or {}).get("conversation_state") or "").strip()
                            if snapshot_state in _ACTIVE_CONVERSATION_SNAPSHOT_STATES:
                                raise producer_error
                        return
                    if producer_task is None:
                        pending_storage_sync = True
                    continue
                if self._tail_is_stream_complete(tail_state, broker_event):
                    # The producer says it is done streaming. Drain and end the
                    # body on the next pass rather than waiting for its task,
                    # which at an interaction boundary outlives the segment.
                    #
                    # A session follower does not end on this producer signal:
                    # an interaction park ends a segment but not its turn. The
                    # terminal payload is the response boundary.
                    if not tail_state.follow_session:
                        stream_complete_seen = True
                    continue
                if producer_task is not None or tail_state.follow_session:
                    live_frame = self._tail_extract_live_broker_frame(tail_state, broker_event)
                    if live_frame is not None:
                        payload, _live_seq, payload_turn_id, payload_command_id = live_frame
                        live_payload_type = str(payload.get("type") or "")
                        live_payload_is_terminal = self._tail_payload_should_stop(tail_state, payload)
                        if live_payload_is_terminal:
                            if tail_state.follow_session:
                                # Terminal signals arrive live before their
                                # durable frame. Hold the signal so the response
                                # can emit it with its exact resume coordinate.
                                # Extraction reserved the live sequence, but no
                                # payload was sent, so the durable copy owns it.
                                tail_state.emitted_live_keys.discard((payload_command_id, _live_seq))
                                continue
                            seen_terminal_payload = True
                            terminal_emitted_from_storage = False
                            while True:
                                try:
                                    queued_event = queue.get_nowait()
                                except asyncio.QueueEmpty:
                                    break
                                durable_frame = self._tail_extract_durable_broker_frame(tail_state, queued_event)
                                if durable_frame is None:
                                    continue
                                (
                                    durable_payload,
                                    frame_seq,
                                    durable_scope,
                                    durable_turn_id,
                                    durable_command_id,
                                    durable_live_seq,
                                ) = durable_frame
                                if durable_live_seq is not None:
                                    tail_state.durable_live_keys.add((durable_command_id, durable_live_seq))
                                if (
                                    durable_live_seq is not None
                                    and (durable_command_id, durable_live_seq) in tail_state.emitted_live_keys
                                ):
                                    durable_emitted = self._tail_emit_durable_cursor_for_live_frame(
                                        tail_state,
                                        payload=durable_payload,
                                        frame_seq=frame_seq,
                                        payload_turn_id=durable_turn_id,
                                    )
                                    durable_payload_is_finish = False
                                else:
                                    (
                                        durable_emitted,
                                        durable_payload_is_finish,
                                    ) = _emit_resumable_payloads(
                                        cursor_tracker=tail_state.cursor_tracker,
                                        payload=durable_payload,
                                        frame_seq=frame_seq,
                                        payload_scope=durable_scope,
                                        payload_turn_id=durable_turn_id,
                                        include_resume_cursor=True,
                                        include_terminal_resume_cursor=include_terminal_resume_cursor,
                                    )
                                for item in durable_emitted:
                                    yield item
                                if durable_payload_is_finish and self._tail_payload_should_stop(tail_state, durable_payload):
                                    terminal_emitted_from_storage = True
                                    break
                            if not terminal_emitted_from_storage:
                                emitted, _ = _emit_resumable_payloads(
                                    cursor_tracker=tail_state.cursor_tracker,
                                    payload=payload,
                                    payload_scope="turn",
                                    payload_turn_id=payload_turn_id,
                                    include_resume_cursor=False,
                                )
                                for item in emitted:
                                    yield item
                            if live_payload_type == "error" and producer_task is not None:
                                tail_state.note_error_terminal_seen(
                                    self._turn_terminal_settle_retry_window_s
                                )
                                continue
                            return
                        emitted, payload_is_finish = _emit_resumable_payloads(
                            cursor_tracker=tail_state.cursor_tracker,
                            payload=payload,
                            payload_scope="turn",
                            payload_turn_id=payload_turn_id,
                            include_resume_cursor=False,
                        )
                        for item in emitted:
                            yield item
                        continue
                    durable_frame = self._tail_extract_durable_broker_frame(tail_state, broker_event)
                    if durable_frame is not None:
                        (
                            payload,
                            frame_seq,
                            payload_scope,
                            payload_turn_id,
                            payload_command_id,
                            live_seq,
                        ) = durable_frame
                        if live_seq is not None:
                            tail_state.durable_live_keys.add((payload_command_id, live_seq))
                        await self._tail_ensure_finish_frame_visible(
                            tail_state,
                            payload=payload,
                            frame_seq=frame_seq,
                            payload_turn_id=payload_turn_id,
                            payload_command_id=payload_command_id,
                        )
                        if live_seq is not None and (payload_command_id, live_seq) in tail_state.emitted_live_keys:
                            emitted = self._tail_emit_durable_cursor_for_live_frame(
                                tail_state,
                                payload=payload,
                                frame_seq=frame_seq,
                                payload_turn_id=payload_turn_id,
                            )
                            payload_is_finish = False
                        else:
                            emitted, payload_is_finish = _emit_resumable_payloads(
                                cursor_tracker=tail_state.cursor_tracker,
                                payload=payload,
                                frame_seq=frame_seq,
                                payload_scope=payload_scope,
                                payload_turn_id=payload_turn_id,
                                include_resume_cursor=True,
                                include_terminal_resume_cursor=include_terminal_resume_cursor,
                            )
                        for item in emitted:
                            yield item
                        if payload_is_finish and self._tail_payload_should_stop(tail_state, payload):
                            seen_terminal_payload = True
                            if str(payload.get("type") or "") == "error" and producer_task is not None:
                                tail_state.note_error_terminal_seen(
                                    self._turn_terminal_settle_retry_window_s
                                )
                                continue
                            return
                        continue
                pending_storage_sync = True
        finally:
            with contextlib.suppress(Exception):
                await self._broker.unsubscribe(session_id, queue)
