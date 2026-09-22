from __future__ import annotations

from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

from astrabox.core.service.orchestrator.assistant_text import coalesce_assistant_text
from astrabox.core.service.orchestrator.engine.base import ENGINE_MESSAGE_EVENT_TYPE
from astrabox.core.service.orchestrator.engine.registry import get_engine_adapter
from astrabox.core.service.orchestrator.engine.input_delivery import (
    command_input_id,
)
from astrabox.core.service.orchestrator.message_blocks import (
    canonicalize_terminal_message_blocks,
    merge_settled_message_blocks,
    normalize_message_blocks,
)
from astrabox.core.service.orchestrator.session_kernel.active_turn_projection import (
    build_active_engine_fifo_messages,
    build_active_turn_message,
)


_TERMINAL_EVENT_TYPES = frozenset(
    {
        "turn.completed",
        "turn.failed",
        "turn.recovered",
    }
)
_MESSAGE_EVENT_TYPES = frozenset(
    {
        "command.accepted",
        "input.consumed",
        ENGINE_MESSAGE_EVENT_TYPE,
        *_TERMINAL_EVENT_TYPES,
    }
)
_USER_MESSAGE_EVENT_TYPES = frozenset({"command.accepted", "input.consumed"})
_MESSAGE_SCAN_BATCH_SIZE = 100


def _event_seq(row: dict[str, Any]) -> int:
    value = row.get("event_seq")
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


def _frame_seq(row: dict[str, Any]) -> int:
    value = row.get("frame_seq")
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


def _message_seq(record: dict[str, Any]) -> int:
    value = record.get("message_seq")
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


def _index_of_message(
    records: list[dict[str, Any]],
    message_id: str | None,
) -> int | None:
    """Position of ``message_id`` in ``records``; the end of the list for ``None``."""

    if message_id is None:
        return len(records)
    for index, record in enumerate(records):
        if str(record.get("message_id") or "") == message_id:
            return index
    return None


def _text_from_blocks(blocks: list[dict[str, Any]]) -> str:
    text_parts: list[str] = []
    result_text: str | None = None
    for block in blocks:
        block_type = str(block.get("type") or "").strip()
        if block_type == "text":
            text = str(block.get("text") or "")
            if text:
                text_parts.append(text)
        elif block_type == "result":
            candidate = str(block.get("result") or "").strip()
            if candidate:
                result_text = candidate
    return coalesce_assistant_text("".join(text_parts), result_text)


def _root_message_blocks(value: Any) -> list[dict[str, Any]]:
    """Keep only blocks owned by a root turn's message projection."""

    return [
        block
        for block in normalize_message_blocks(value)
        if str(block.get("type") or "").strip() != "subagent"
    ]


def _user_message_from_command(event: dict[str, Any]) -> dict[str, Any] | None:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    if str(payload.get("command_type") or "").strip() != "StartTurn":
        return None
    if command_input_id(payload) is not None:
        # The active engine queue owns when this input becomes visible. Its corresponding
        # input.consumed event is the durable boundary used below.
        return None
    turn_id = str(event.get("turn_id") or "").strip()
    content = payload.get("content")
    if not turn_id or not isinstance(content, str) or not content.strip():
        return None
    seq = _event_seq(event)
    return {
        "session_id": str(event.get("session_id") or ""),
        "message_id": f"{turn_id}:user",
        "message_seq": seq,
        "turn_id": turn_id,
        "role": "user",
        "user_id": str(payload.get("author_user_id") or ""),
        "client_message_id": (
            str(payload.get("client_message_id") or "").strip() or None
        ),
        "content": content,
        "blocks": _user_message_blocks(payload),
        "source_event_seq_applied": seq,
        "created_at": str(event.get("occurred_at") or ""),
    }


def _user_message_blocks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Carry an input's non-text content into the message the browser reads.

    ``content`` is the input's text, which is all a plain turn ever has. When
    the turn was sent images too, they are the engine's own content blocks and
    travel to the reader unchanged.
    """

    blocks = payload.get("content_blocks")
    if not isinstance(blocks, list):
        return []
    return [
        dict(block)
        for block in blocks
        if isinstance(block, dict) and str(block.get("type") or "") != "text"
    ]


def _user_message_from_consumed(event: dict[str, Any]) -> dict[str, Any] | None:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    input_id = str(payload.get("input_id") or "").strip()
    turn_id = str(event.get("turn_id") or "").strip()
    content = payload.get("content")
    if not input_id or not turn_id or not isinstance(content, str):
        return None
    seq = _event_seq(event)
    return {
        "session_id": str(event.get("session_id") or ""),
        "message_id": f"{input_id}:user",
        "message_seq": seq,
        "turn_id": turn_id,
        "role": "user",
        "user_id": "",
        "client_message_id": (
            str(payload.get("client_message_id") or "").strip() or None
        ),
        "content": content,
        "blocks": _user_message_blocks(payload),
        "source_event_seq_applied": seq,
        "created_at": str(event.get("occurred_at") or ""),
    }


def _terminal_message(
    event: dict[str, Any],
    *,
    frames: list[dict[str, Any]],
    user_id: str,
    fifo_message: dict[str, Any] | None = None,
    include_terminal_payload: bool = True,
) -> dict[str, Any] | None:
    turn_id = str(event.get("turn_id") or "").strip()
    if not turn_id:
        return None
    payload = event.get("payload")
    payload = dict(payload) if isinstance(payload, dict) else {}
    event_seq = _event_seq(event)
    if fifo_message is not None:
        projected = fifo_message
        message_id = str(projected["message_id"])
        message_seq = int(projected["message_seq"])
    else:
        projected = build_active_turn_message(
            session_id=str(event.get("session_id") or ""),
            turn_id=turn_id,
            message_id=turn_id,
            frames=frames,
            existing_message=None,
            default_message_seq=event_seq,
            user_id=user_id,
            incremental=False,
        )
        message_id = turn_id
        message_seq = event_seq
    blocks = _root_message_blocks((projected or {}).get("blocks"))
    assistant_text = str((projected or {}).get("content") or "")
    if include_terminal_payload:
        blocks = merge_settled_message_blocks(
            blocks,
            _root_message_blocks(payload.get("blocks")),
        )
        assistant_text = str(payload.get("assistant_text") or "")
    content = coalesce_assistant_text(
        _text_from_blocks(blocks) or str((projected or {}).get("content") or ""),
        assistant_text,
    )
    event_type = str(event.get("event_type") or "").strip()
    if event_type == "turn.failed":
        error_text = str(
            payload.get("error_text")
            or payload.get("reason")
            or "turn failed"
        ).strip()
        failure_phase = str(payload.get("failure_phase") or "").strip() or None
        if not any(str(block.get("type") or "") == "turn_failure" for block in blocks):
            failure_block: dict[str, Any] = {
                "type": "turn_failure",
                "error": error_text,
            }
            if failure_phase:
                failure_block["failure_phase"] = failure_phase
            blocks.append(failure_block)

    blocks = canonicalize_terminal_message_blocks(blocks)
    if not blocks and not content.strip():
        return None
    max_frame_seq = max((_frame_seq(frame) for frame in frames), default=-1)
    return {
        "session_id": str(event.get("session_id") or ""),
        "message_id": message_id,
        "message_seq": message_seq,
        "turn_id": turn_id,
        "role": "assistant",
        "user_id": user_id,
        "content": content,
        "blocks": blocks,
        "source_event_seq_applied": event_seq,
        "source_frame_seq_applied": max_frame_seq if max_frame_seq >= 0 else None,
        "created_at": str(
            (projected or {}).get("created_at")
            or event.get("occurred_at")
            or ""
        ),
        **(
            {"synthetic": True}
            if event_type == "turn.failed" and not frames and not assistant_text
            else {}
        ),
    }


def _resident_session_message(event: dict[str, Any]) -> dict[str, Any] | None:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("durable engine event requires a payload")
    engine_kind = str(payload.get("engine_kind") or "").strip()
    message = payload.get("message")
    if not engine_kind or not isinstance(message, dict):
        raise ValueError("durable engine event requires engine_kind and native message")
    fact = get_engine_adapter(engine_kind).durable_session_message(message)
    if fact is None:
        return None
    seq = _event_seq(event)
    return {
        "session_id": str(event["session_id"]),
        "message_id": fact.message_id,
        "message_seq": seq,
        "turn_id": "",
        "role": "assistant",
        "user_id": "",
        "content": fact.content,
        "blocks": [{"type": "text", "text": fact.content}],
        "source_event_seq_applied": seq,
        "created_at": str(event.get("occurred_at") or ""),
    }


def project_session_messages(
    *,
    events: list[dict[str, Any]],
    frames: list[dict[str, Any]],
    user_id: str = "",
) -> list[dict[str, Any]]:
    """Build the message read model from the one canonical session event log."""

    ordered_events = sorted(events, key=_event_seq)
    ordered_frames = sorted(frames, key=_frame_seq)
    frames_by_turn: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for frame in ordered_frames:
        turn_id = str(frame.get("turn_id") or "").strip()
        if turn_id:
            frames_by_turn[turn_id].append(frame)

    messages: dict[str, dict[str, Any]] = {}
    terminal_by_turn: dict[str, dict[str, Any]] = {}
    for event in ordered_events:
        event_type = str(event.get("event_type") or "").strip()
        if event_type == ENGINE_MESSAGE_EVENT_TYPE:
            resident = _resident_session_message(event)
            if resident is not None:
                messages[str(resident["message_id"])] = resident
        user_message = None
        if event_type == "command.accepted":
            user_message = _user_message_from_command(event)
        elif event_type == "input.consumed":
            user_message = _user_message_from_consumed(event)
        if isinstance(user_message, dict):
            if not user_message.get("user_id"):
                user_message["user_id"] = user_id
            messages[str(user_message["message_id"])] = user_message
        if event_type in _TERMINAL_EVENT_TYPES:
            turn_id = str(event.get("turn_id") or "").strip()
            if turn_id:
                terminal_by_turn[turn_id] = event

    for turn_id, terminal_event in terminal_by_turn.items():
        turn_frames = frames_by_turn.get(turn_id, [])
        fifo_messages = build_active_engine_fifo_messages(
            session_id=str(terminal_event.get("session_id") or ""),
            turn_id=turn_id,
            frames=turn_frames,
            default_message_seq=_event_seq(terminal_event),
            user_id=user_id,
        )
        fifo_assistants = [
            dict(message)
            for message in fifo_messages
            if str(message.get("role") or "") == "assistant"
        ]
        for fifo_message in fifo_messages:
            message_id = str(fifo_message.get("message_id") or "").strip()
            if not message_id:
                continue
            existing = messages.get(message_id)
            if existing and str(existing.get("role") or "") == "user":
                continue
            messages[message_id] = dict(fifo_message)

        terminal_message = _terminal_message(
            terminal_event,
            frames=turn_frames,
            user_id=user_id,
            fifo_message=fifo_assistants[-1] if fifo_assistants else None,
            # A single response owns the whole settled payload, including
            # transcript recovery after a partial stream. Multiple responses
            # retain their own segments; a whole-turn payload cannot be
            # assigned to the last response.
            include_terminal_payload=len(fifo_assistants) <= 1,
        )
        if terminal_message is None:
            continue
        if fifo_assistants:
            messages.pop(turn_id, None)
        terminal_message_id = str(terminal_message["message_id"])
        messages[terminal_message_id] = terminal_message

    return sorted(
        messages.values(),
        key=lambda message: (
            int(message.get("message_seq") or 0),
            str(message.get("created_at") or ""),
            str(message.get("message_id") or ""),
        ),
    )


class SessionMessageView:
    """Read-only messages derived from ``session_events``.

    This object deliberately has no write API. Commands, engine frames and
    terminal events are the durable facts; a second mutable transcript would
    make recovery depend on two stores agreeing.
    """

    def __init__(self, session_events_repo: Any) -> None:
        self._session_events_repo = session_events_repo

    async def _events(
        self,
        session_id: str,
        *,
        turn_id: str | None = None,
        event_types: frozenset[str] | None = None,
        after_seq: int = 0,
        before_seq: int | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        while True:
            batch = await self._session_events_repo.list_events(
                session_id,
                after_seq=after_seq,
                before_seq=before_seq,
                turn_id=turn_id,
                event_types=event_types,
                limit=500,
            )
            if not batch:
                return rows
            rows.extend(dict(row) for row in batch)
            next_seq = max(_event_seq(row) for row in batch)
            if next_seq <= after_seq:
                raise RuntimeError("session event message scan did not advance")
            after_seq = next_seq
            if len(batch) < 500:
                return rows

    async def resident_message_frames(
        self,
        session_id: str,
        *,
        after_seq: int,
        before_seq: int | None = None,
    ) -> list[dict[str, Any]]:
        """Expose the same durable messages to the existing Session follower."""
        events = await self._events(
            session_id,
            event_types=frozenset({ENGINE_MESSAGE_EVENT_TYPE}),
            after_seq=after_seq,
            before_seq=before_seq,
        )
        return [
            {
                "frame_seq": message["source_event_seq_applied"],
                "scope": "session",
                "payload": {
                    "type": "data-session-message",
                    "id": message["message_id"],
                    "transient": True,
                    "data": message,
                },
            }
            for message in project_session_messages(events=events, frames=[])
        ]

    async def _frames(
        self,
        session_id: str,
        *,
        turn_id: str | None = None,
        turn_ids: frozenset[str] | None = None,
        before_seq: int | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        after_seq = -1
        while True:
            batch = await self._session_events_repo.list_frames(
                session_id,
                turn_id=turn_id,
                turn_ids=turn_ids,
                scope="turn",
                after_seq=after_seq,
                before_seq=before_seq,
                limit=500,
            )
            if not batch:
                return rows
            rows.extend(dict(row) for row in batch)
            next_seq = max(_frame_seq(row) for row in batch)
            if next_seq <= after_seq:
                raise RuntimeError("session frame message scan did not advance")
            after_seq = next_seq
            if len(batch) < 500:
                return rows

    async def _messages(
        self,
        session_id: str,
        *,
        user_id: str = "",
    ) -> list[dict[str, Any]]:
        events = await self._events(session_id)
        frames = await self._frames(session_id)
        return project_session_messages(events=events, frames=frames, user_id=user_id)

    async def _messages_for_turn(
        self,
        session_id: str,
        *,
        turn_id: str,
        user_id: str = "",
    ) -> list[dict[str, Any]]:
        events = await self._events(
            session_id,
            turn_id=turn_id,
            event_types=_MESSAGE_EVENT_TYPES,
        )
        frames = await self._frames(session_id, turn_id=turn_id)
        return project_session_messages(events=events, frames=frames, user_id=user_id)

    async def _messages_from_tail(
        self,
        session_id: str,
        *,
        required_count: int,
        before: str | None = None,
        role: str | None = None,
        user_id: str = "",
    ) -> list[dict[str, Any]]:
        """Read only the newest event suffix needed for one message page."""

        events: list[dict[str, Any]] = []
        frames: list[dict[str, Any]] = []
        loaded_turn_ids: set[str] = set()
        before_seq: int | None = None
        batch_size = max(20, min(_MESSAGE_SCAN_BATCH_SIZE, required_count * 4))
        while True:
            batch = await self._session_events_repo.list_events(
                session_id,
                after_seq=0,
                before_seq=before_seq,
                event_types=_MESSAGE_EVENT_TYPES,
                limit=batch_size,
                newest_first=True,
            )
            if batch:
                events.extend(dict(row) for row in batch)
                terminal_turn_ids = {
                    str(row.get("turn_id") or "").strip()
                    for row in batch
                    if str(row.get("event_type") or "").strip()
                    in _TERMINAL_EVENT_TYPES
                    and str(row.get("turn_id") or "").strip()
                }
                missing_turn_ids = terminal_turn_ids - loaded_turn_ids
                if missing_turn_ids:
                    frames.extend(
                        await self._frames(
                            session_id,
                            turn_ids=frozenset(missing_turn_ids),
                        )
                    )
                    loaded_turn_ids.update(missing_turn_ids)

            messages = project_session_messages(
                events=events,
                frames=frames,
                user_id=user_id,
            )
            eligible = [
                message
                for message in messages
                if (not before or str(message.get("created_at") or "") < before)
                and (role is None or str(message.get("role") or "") == role)
            ]
            if len(eligible) >= required_count:
                return eligible
            if not batch or len(batch) < batch_size:
                return eligible

            next_before_seq = min(_event_seq(row) for row in batch)
            if before_seq is not None and next_before_seq >= before_seq:
                raise RuntimeError("session event reverse message scan did not advance")
            before_seq = next_before_seq

    async def get_message(
        self,
        session_id: str,
        message_id: str,
        *,
        user_id: str = "",
    ) -> dict[str, Any] | None:
        return next(
            (
                message
                for message in await self._messages(session_id, user_id=user_id)
                if str(message.get("message_id") or "") == message_id
            ),
            None,
        )

    async def get_first_user_message(
        self,
        session_id: str,
        *,
        user_id: str = "",
    ) -> dict[str, Any] | None:
        messages = await self.list_first_user_messages(
            session_id,
            limit=1,
            user_id=user_id,
        )
        return messages[0] if messages else None

    async def get_last_assistant_message(
        self,
        session_id: str,
        *,
        user_id: str = "",
    ) -> dict[str, Any] | None:
        messages = await self._messages_from_tail(
            session_id,
            required_count=1,
            role="assistant",
            user_id=user_id,
        )
        return messages[-1] if messages else None

    async def get_assistant_message_for_turn(
        self,
        session_id: str,
        *,
        turn_id: str,
        user_id: str = "",
    ) -> dict[str, Any] | None:
        # The event query already scopes every projected message to this
        # platform turn. An engine with a native input FIFO may give the final
        # assistant response its own opaque message id; keep that engine identity
        # verbatim and select the last assistant inside the scoped projection.
        return next(
            (
                message
                for message in reversed(
                    await self._messages_for_turn(
                        session_id,
                        turn_id=turn_id,
                        user_id=user_id,
                    )
                )
                if str(message.get("role") or "") == "assistant"
            ),
            None,
        )

    async def list_first_user_messages(
        self,
        session_id: str,
        *,
        limit: int = 3,
        user_id: str = "",
    ) -> list[dict[str, Any]]:
        capped_limit = max(1, min(10, int(limit or 3)))
        events: list[dict[str, Any]] = []
        after_seq = 0
        while True:
            batch = await self._session_events_repo.list_events(
                session_id,
                after_seq=after_seq,
                event_types=_USER_MESSAGE_EVENT_TYPES,
                limit=_MESSAGE_SCAN_BATCH_SIZE,
            )
            if batch:
                events.extend(dict(row) for row in batch)
            messages = [
                message
                for message in project_session_messages(events=events, frames=[])
                if str(message.get("role") or "") == "user"
            ]
            if len(messages) >= capped_limit:
                return messages[:capped_limit]
            if not batch or len(batch) < _MESSAGE_SCAN_BATCH_SIZE:
                return messages
            next_after_seq = max(_event_seq(row) for row in batch)
            if next_after_seq <= after_seq:
                raise RuntimeError("session event user message scan did not advance")
            after_seq = next_after_seq

    async def history_checkpoint_seq(self, session_id: str) -> int:
        """Return the event_seq a history read should be pinned to.

        Every page of one history read is projected from the same suffix of the
        event log. Without that pin a turn settling between two pages shifts
        every record the reader has not seen yet, and the page after the shift
        either repeats a record or skips one. ``0`` means the session has no
        message events at all.
        """

        rows = await self._session_events_repo.list_events(
            session_id,
            after_seq=0,
            event_types=_MESSAGE_EVENT_TYPES,
            limit=1,
            newest_first=True,
        )
        return _event_seq(rows[0]) if rows else 0

    async def _history_scan(
        self,
        session_id: str,
        *,
        through_seq: int,
        user_id: str,
        batch_size: int,
    ) -> AsyncIterator[tuple[list[dict[str, Any]], bool, int]]:
        """Project the newest records first, one event batch at a time.

        Yields ``(records, exhausted, scanned_floor)`` after each batch, so a
        caller stops as soon as it has the records it needs instead of reading
        a whole session. ``scanned_floor`` is the oldest event sequence the scan
        has read: every event at or above it is loaded, so the projection is
        complete from there upwards, while a record whose own sequence lies
        below it — a turn's frames are loaded whole the moment its terminal is
        seen, and their sequences precede that terminal — can still have
        unscanned events between it and the floor. Events and frames are both
        bounded by ``through_seq``, which is what keeps every page of one read
        on the same pinned history.
        """

        events: list[dict[str, Any]] = []
        frames: list[dict[str, Any]] = []
        loaded_turn_ids: set[str] = set()
        frame_before_seq = through_seq + 1
        before_seq: int | None = frame_before_seq
        scanned_floor = frame_before_seq
        while True:
            batch = await self._session_events_repo.list_events(
                session_id,
                after_seq=0,
                before_seq=before_seq,
                event_types=_MESSAGE_EVENT_TYPES,
                limit=batch_size,
                newest_first=True,
            )
            if batch:
                events.extend(dict(row) for row in batch)
                terminal_turn_ids = {
                    str(row.get("turn_id") or "").strip()
                    for row in batch
                    if str(row.get("event_type") or "").strip() in _TERMINAL_EVENT_TYPES
                    and str(row.get("turn_id") or "").strip()
                }
                missing_turn_ids = terminal_turn_ids - loaded_turn_ids
                if missing_turn_ids:
                    frames.extend(
                        await self._frames(
                            session_id,
                            turn_ids=frozenset(missing_turn_ids),
                            before_seq=frame_before_seq,
                        )
                    )
                    loaded_turn_ids.update(missing_turn_ids)

            exhausted = not batch or len(batch) < batch_size
            if batch:
                scanned_floor = min(scanned_floor, min(_event_seq(row) for row in batch))
            if exhausted:
                scanned_floor = 0
            yield (
                project_session_messages(
                    events=events,
                    frames=frames,
                    user_id=user_id,
                ),
                exhausted,
                scanned_floor,
            )
            if exhausted:
                return
            next_before_seq = min(_event_seq(row) for row in batch)
            if before_seq is not None and next_before_seq >= before_seq:
                raise RuntimeError("session event history block scan did not advance")
            before_seq = next_before_seq

    async def list_history_blocks_page(
        self,
        session_id: str,
        *,
        limit: int,
        through_seq: int | None,
        before_block_id: str | None,
        user_id: str = "",
    ) -> dict[str, Any]:
        """Return one page of records, newest last, pinned to ``through_seq``.

        Paging is by record rather than by block, so a tool call and its result
        are never split across a page boundary — they belong to the same
        record, and a record is either wholly on this page or on the next one.

        ``before_block_id`` is the ``message_id`` the previous page started
        with. It must still exist at this checkpoint; a cursor naming a record
        outside the pinned history raises :class:`ValueError` rather than
        silently answering from the tail.
        """

        capped_limit = max(1, int(limit))
        checkpoint = (
            await self.history_checkpoint_seq(session_id)
            if through_seq is None
            else max(0, int(through_seq))
        )
        if checkpoint <= 0:
            return {
                "records": [],
                "has_more": False,
                "through_seq": checkpoint,
                "next_before": None,
            }

        batch_size = max(20, min(_MESSAGE_SCAN_BATCH_SIZE, capped_limit * 4))
        records: list[dict[str, Any]] = []
        boundary: int | None = None
        exhausted = False
        async with aclosing(
            self._history_scan(
                session_id,
                through_seq=checkpoint,
                user_id=user_id,
                batch_size=batch_size,
            )
        ) as scan:
            async for scanned, scan_exhausted, scanned_floor in scan:
                records = scanned
                exhausted = scan_exhausted
                boundary = _index_of_message(records, before_block_id)
                if scan_exhausted:
                    break
                if boundary is None or boundary < capped_limit + 1:
                    continue
                # The page's records exist, but the page is only complete once
                # the scan has read every event at or above its oldest record:
                # a turn's frames project responses whose sequences precede its
                # terminal, and an event that sits between those responses and
                # the terminal — a resident message, another turn's terminal —
                # is still unread. Serving the page now would settle its
                # cursor past that event and never show it.
                page_start = max(0, boundary - capped_limit)
                if _message_seq(records[page_start]) >= scanned_floor:
                    break
        if boundary is None:
            raise ValueError("block cursor is outside history")

        start = max(0, boundary - capped_limit)
        page = records[start:boundary]
        has_more = start > 0 or not exhausted
        return {
            "records": [dict(record) for record in page],
            "has_more": has_more,
            "through_seq": checkpoint,
            "next_before": str(page[0]["message_id"]) if has_more and page else None,
        }

    async def get_history_block_record(
        self,
        session_id: str,
        *,
        message_id: str,
        through_seq: int,
        user_id: str = "",
    ) -> dict[str, Any] | None:
        """Return one record as it stood at ``through_seq``, or ``None``.

        A detail read reopens blocks a page folded away, so it has to project
        the same record that page projected — not the record as later events
        left it.
        """

        checkpoint = max(0, int(through_seq))
        if checkpoint <= 0 or not message_id:
            return None
        async with aclosing(
            self._history_scan(
                session_id,
                through_seq=checkpoint,
                user_id=user_id,
                batch_size=_MESSAGE_SCAN_BATCH_SIZE,
            )
        ) as scan:
            async for records, exhausted, _scanned_floor in scan:
                index = _index_of_message(records, message_id)
                if index is not None:
                    return dict(records[index])
                if exhausted:
                    return None
        return None

    async def list_page(
        self,
        session_id: str,
        *,
        limit: int = 20,
        before: str | None = None,
        user_id: str = "",
    ) -> tuple[list[dict[str, Any]], bool]:
        capped_limit = max(1, int(limit))
        messages = await self._messages_from_tail(
            session_id,
            required_count=capped_limit + 1,
            before=before,
            user_id=user_id,
        )
        rows = messages[-(capped_limit + 1) :]
        has_more = len(rows) > capped_limit
        return rows[-capped_limit:], has_more
