"""A settle has two halves, and only one of them reaches an open page.

Stopping a turn before its first token settles it on the platform's side. The
snapshot half is what a reload reads; the frame half is what every client
already streaming reads. With only the snapshot written, the database calls the
session idle while an attached page keeps its header running — measured at
ninety seconds of PROCESSING against a session whose record said IDLE, which is
the hang this force-settle exists to end, moved one layer out.

The sibling settle for a parked turn already writes both and says why. These
pin that the pre-first-token settle does too, and that the frame is addressable
— a terminal nothing can address is not a terminal.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrabox.core.service.orchestrator.session_kernel.workers.turn.worker import (
    TurnWorker,
)


def _worker(*, frames: list[dict] | None = None) -> tuple[TurnWorker, dict]:
    captured: dict = {}

    async def apply_channel_update(session_id, **kwargs):
        captured["session_id"] = session_id
        captured.update(kwargs)
        return {"conversation_state": "IDLE"}

    worker = TurnWorker.__new__(TurnWorker)
    worker._session_events_repo = SimpleNamespace(
        list_frames=AsyncMock(return_value=list(frames or [])),
        append_event=AsyncMock(return_value={"event_seq": 41}),
        get_next_session_frame_seq=AsyncMock(return_value=77),
        append_frame=AsyncMock(return_value=None),
    )
    worker._session_snapshots_repo = SimpleNamespace(
        apply_channel_update=AsyncMock(side_effect=apply_channel_update),
    )
    return worker, captured


@pytest.mark.asyncio
async def test_the_settle_publishes_a_terminal_frame_for_attached_clients() -> None:
    worker, captured = _worker()

    seq = await worker._settle_pre_first_token_interrupt(
        session_id="session-1",
        turn_id="turn-1",
        command_id="command-1",
        snapshot={"current_turn_id": "turn-1", "conversation_state": "PROCESSING"},
    )

    assert seq == 41
    appended = worker._session_events_repo.append_frame.await_args
    assert appended is not None, "an attached client learns from the frame, not the row"
    frame = appended.args[0]
    assert frame["payload"]["type"] == "finish"
    assert frame["turn_id"] == "turn-1"
    assert frame["command_id"] == "command-1", "a terminal must be addressable"
    assert frame["frame_seq"] == 77

    # And the snapshot points at it, so a reload and a live client agree.
    assert captured["updates"]["last_turn_terminal_frame"] == {
        "turn_id": "turn-1",
        "command_id": "command-1",
        "frame_seq": 77,
        "type": "finish",
        "finish_reason": "stop",
    }


@pytest.mark.asyncio
async def test_a_settle_without_a_command_writes_no_unaddressable_frame() -> None:
    worker, _ = _worker()

    await worker._settle_pre_first_token_interrupt(
        session_id="session-1",
        turn_id="turn-1",
        command_id=None,
        snapshot={"current_turn_id": "turn-1", "conversation_state": "PROCESSING"},
    )

    worker._session_events_repo.append_frame.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_turn_that_already_produced_frames_is_left_to_the_bridge() -> None:
    """The force-settle is scoped to the zero-frame case; a turn with content
    settles through the bridge, which carries what it produced."""

    worker, _ = _worker(frames=[{"frame_seq": 1}])

    seq = await worker._settle_pre_first_token_interrupt(
        session_id="session-1",
        turn_id="turn-1",
        command_id="command-1",
        snapshot={"current_turn_id": "turn-1", "conversation_state": "STREAMING"},
    )

    assert seq is None
    worker._session_events_repo.append_frame.assert_not_awaited()
    worker._session_snapshots_repo.apply_channel_update.assert_not_awaited()
