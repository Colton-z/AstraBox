"""A stop the platform settles itself is in the next history read.

A history read is pinned to the newest message event, and it loads a turn's
frames up to that pin once it reaches the turn's terminal. Two settles end a
turn without an engine terminal: a stop while the turn waits on an
interaction, and a stop before the first token. Both write frames of their
own: the cancelled result, the terminal finish, and for a held tool call its
denial. A frame written after the terminal event sits outside the pin. So a
page that adopted history as the stop settled showed the held call still
running and no stop line, and a reload showed the same until some later event
moved the pin.

The chain here is the real one: the settle writes through the event
repository, and the history page reads back through the message view.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from astrabox.core.service.orchestrator.session_kernel.workers.turn.worker import (
    TurnWorker,
)
from astrabox.core.service.orchestrator.session_message_view import SessionMessageView
from astrabox.core.service.orchestrator.tool_result_semantics import TOOL_RESULT_STATE_DENIED
from astrabox.persistence.repository import SessionEventRepository, SessionSnapshotRepository
from astrabox.persistence.repository.interaction_snapshot_repository import (
    InteractionSnapshotRepository,
)


async def _append_frame(
    events: SessionEventRepository, session_id: str, turn_id: str, payload: dict[str, Any]
) -> None:
    await events.append_frame(
        {
            "session_id": session_id,
            "turn_id": turn_id,
            "command_id": f"{session_id}:input-1",
            "source_kind": "engine",
            "frame_seq": int(await events.get_next_session_frame_seq(session_id)),
            "payload": payload,
            "created_at": "2026-09-23T18:40:35+00:00",
        }
    )


async def _accepted_input(events: SessionEventRepository, session_id: str, turn_id: str) -> int:
    accepted = await events.append_event(
        {
            "session_id": session_id,
            "channel": "conversation",
            "turn_id": turn_id,
            "event_type": "command.accepted",
            "causation_id": f"{session_id}:input-1",
            "correlation_id": f"{session_id}:input-1",
            "payload": {
                "command_type": "StartTurn",
                "content": "write the file, then wait for me",
                "client_message_id": "client-1",
                "author_user_id": "user-1",
            },
        }
    )
    return int(accepted["event_seq"])


async def _stop(
    events: SessionEventRepository,
    snapshots: SessionSnapshotRepository,
    session_id: str,
    turn_id: str,
) -> int | None:
    """The worker's settle for a stop that meets no engine terminal."""
    worker = TurnWorker.__new__(TurnWorker)
    worker._session_events_repo = events
    worker._session_snapshots_repo = snapshots
    worker._interaction_snapshots_repo = InteractionSnapshotRepository()
    return await worker._settle_pre_first_token_interrupt(
        session_id=session_id,
        turn_id=turn_id,
        command_id=f"{session_id}:input-1",
        snapshot=await snapshots.get_snapshot(session_id),
    )


async def _assistant_record(events: SessionEventRepository, session_id: str) -> dict[str, Any]:
    page = await SessionMessageView(events).list_history_blocks_page(
        session_id, limit=10, through_seq=None, before_block_id=None
    )
    [assistant] = [record for record in page["records"] if record["role"] == "assistant"]
    return assistant


@pytest.mark.asyncio
async def test_a_stop_on_a_parked_turn_is_in_the_next_history_read() -> None:
    session_id, turn_id = str(uuid.uuid4()), str(uuid.uuid4())
    events = SessionEventRepository()
    snapshots = SessionSnapshotRepository()
    await _accepted_input(events, session_id, turn_id)
    # The parked segment as the bridge leaves it: the held call, and the
    # segment's own finish at the interaction.
    await _append_frame(events, session_id, turn_id, {"type": "start", "messageId": "response-1"})
    await _append_frame(events, session_id, turn_id, {
        "type": "tool-input-available", "toolCallId": "call-1", "toolName": "Write",
        "input": {"file_path": "/workspace/held.txt", "content": "held"}, "dynamic": True,
    })
    await _append_frame(events, session_id, turn_id, {"type": "finish", "finishReason": "tool-calls"})
    parked = await events.append_event(
        {
            "session_id": session_id,
            "channel": "conversation",
            "turn_id": turn_id,
            "event_type": "turn.awaiting_interaction",
            "causation_id": f"{session_id}:input-1",
            "correlation_id": f"{session_id}:input-1",
            "payload": {},
        }
    )
    await snapshots.apply_channel_update(
        session_id,
        channel="conversation",
        event_seq=int(parked["event_seq"]),
        updates={"conversation_state": "WAITING_FOR_INTERACTION", "current_turn_id": turn_id},
    )

    assert await _stop(events, snapshots, session_id, turn_id) is not None

    blocks = (await _assistant_record(events, session_id))["blocks"]
    assert [block.get("finish_reason") for block in blocks if block["type"] == "result"] == [
        "cancelled"
    ]
    [denial] = [block for block in blocks if block["type"] == "tool_result"]
    assert denial["tool_use_id"] == "call-1"
    assert denial["tool_result_state"] == TOOL_RESULT_STATE_DENIED


@pytest.mark.asyncio
async def test_a_stop_before_the_first_token_is_in_the_next_history_read() -> None:
    session_id, turn_id = str(uuid.uuid4()), str(uuid.uuid4())
    events = SessionEventRepository()
    snapshots = SessionSnapshotRepository()
    accepted_seq = await _accepted_input(events, session_id, turn_id)
    await snapshots.apply_channel_update(
        session_id,
        channel="conversation",
        event_seq=accepted_seq,
        updates={"conversation_state": "PROCESSING", "current_turn_id": turn_id},
    )

    assert await _stop(events, snapshots, session_id, turn_id) is not None
    blocks = (await _assistant_record(events, session_id))["blocks"]
    assert [block.get("finish_reason") for block in blocks if block["type"] == "result"] == [
        "cancelled"
    ]
