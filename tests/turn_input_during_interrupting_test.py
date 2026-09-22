"""An input that arrives while a turn is stopping waits for the settle.

The dying turn's FIFO verdict is taken when the interrupt lands, so a
submit classified into its stream strands the reply — the cancel-handoff
e2e's ledger showed SubmitInput accepted after InterruptTurn and consumed
after turn.completed, answered by nothing. Dispatch therefore parks the
input until the conversation leaves INTERRUPTING, and refuses loudly (as
retryable) when the stop itself is stuck rather than queueing work behind
a hang.
"""

from __future__ import annotations

from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.session_kernel.service_mixins.turn_dispatch import (
    TurnDispatchStreamingMixin,
)


class _Snapshots:
    def __init__(self, states: list[str]) -> None:
        self._states = states
        self.reads = 0

    async def get_snapshot(self, session_id: str) -> dict[str, Any]:
        state = self._states[min(self.reads, len(self._states) - 1)]
        self.reads += 1
        return {"conversation_state": state}


class _Harness(TurnDispatchStreamingMixin):
    def __init__(self, snapshots: _Snapshots) -> None:
        self._session_snapshots_repo = snapshots


@pytest.mark.asyncio
async def test_the_wait_returns_the_settled_snapshot() -> None:
    snapshots = _Snapshots(["INTERRUPTING", "INTERRUPTING", "IDLE"])
    harness = _Harness(snapshots)

    snapshot = await harness._await_interrupt_settled("session-1")

    assert snapshot == {"conversation_state": "IDLE"}
    assert snapshots.reads == 3


@pytest.mark.asyncio
async def test_a_stuck_stop_is_refused_rather_than_queued_behind() -> None:
    harness = _Harness(_Snapshots(["INTERRUPTING"]))

    with pytest.raises(APIError) as caught:
        await harness._await_interrupt_settled("session-1", budget_s=0.3)

    assert caught.value.code == "SESSION_BUSY"


class _Events:
    def __init__(self, commands: list[dict[str, Any]], frames: list[dict[str, Any]]) -> None:
        self._commands = commands
        self._frames = frames
        self.claimed: list[dict[str, Any]] = []

    async def list_events(self, session_id: str, **_kw: Any) -> list[dict[str, Any]]:
        return list(self._commands)

    async def list_frames(self, session_id: str) -> list[dict[str, Any]]:
        return list(self._frames)

    async def try_claim_event(self, doc: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        self.claimed.append(doc)
        return {**doc, "event_seq": 100 + len(self.claimed)}, True


class _Sessions:
    async def get_session(self, session_id: str) -> dict[str, Any]:
        return {"session_id": session_id, "user_id": "owner"}

    async def mark_interaction(self, session_id: str, at: str) -> None:
        return None


class _SweepHarness(TurnDispatchStreamingMixin):
    def __init__(self, snapshot: dict[str, Any], events: _Events) -> None:
        self._snapshot = snapshot
        self._session_events_repo = events
        self._sessions_repo = _Sessions()
        self._session_snapshots_repo = self
        self.spawned: list[tuple[str, str]] = []
        self.projected: list[str] = []

    async def get_snapshot(self, session_id: str) -> dict[str, Any]:
        return dict(self._snapshot)

    async def _project_command_to_snapshot(self, **kw: Any) -> None:
        self.projected.append(str(kw.get("command_id")))

    def _spawn_accepted_turn_producer(
        self, session_id: str, *, command_id: str, turn_id: str
    ) -> None:
        self.spawned.append((command_id, turn_id))


_SESSION = "3f1f2d51-9d2f-49a5-8a6e-2d55b1c0aa11"


def _command(seq: int, command_type: str, **payload: Any) -> dict[str, Any]:
    return {
        "event_seq": seq,
        "turn_id": "turn-1",
        "causation_id": payload.pop("causation_id", f"cmd-{seq}"),
        "payload": {"command_type": command_type, **payload},
    }


def _idle_snapshot() -> dict[str, Any]:
    return {"conversation_state": "IDLE", "current_turn_id": "", "last_turn_id": "turn-1"}


@pytest.mark.asyncio
async def test_a_submit_after_the_last_interrupt_is_redriven() -> None:
    events = _Events(
        [
            _command(11, "StartTurn"),
            _command(35, "InterruptTurn"),
            _command(
                39,
                "SubmitInput",
                input_id="9c1f2d51-9d2f-49a5-8a6e-2d55b1c0aa22",
                content="follow up",
                client_message_id="client-2",
                author_user_id="owner",
            ),
        ],
        frames=[],
    )
    harness = _SweepHarness(_idle_snapshot(), events)

    await harness._redrive_stranded_inputs_after_settle(_SESSION, turn_id="turn-1")

    # No second command: the original SubmitInput stays the one FIFO root
    # (a duplicate root can never be consumed and haunts the queue forever);
    # the producer carries it under a deterministic fresh turn.
    assert events.claimed == []
    assert len(harness.spawned) == 1
    spawned_command, spawned_turn = harness.spawned[0]
    assert spawned_command == "cmd-39"
    assert spawned_turn


@pytest.mark.asyncio
async def test_inputs_before_the_interrupt_or_already_answered_stay() -> None:
    answered_id = "cmd-51"
    events = _Events(
        [
            _command(11, "StartTurn"),
            _command(20, "SubmitInput", input_id="a" * 36, content="early"),
            _command(35, "InterruptTurn"),
            _command(
                51,
                "SubmitInput",
                causation_id=answered_id,
                input_id="9c1f2d51-9d2f-49a5-8a6e-2d55b1c0aa33",
                content="answered",
            ),
        ],
        frames=[
            {"command_id": answered_id, "payload": {"type": "finish"}},
        ],
    )
    harness = _SweepHarness(_idle_snapshot(), events)

    await harness._redrive_stranded_inputs_after_settle(_SESSION, turn_id="turn-1")

    assert events.claimed == []
    assert harness.spawned == []


@pytest.mark.asyncio
async def test_an_input_carried_to_a_terminal_by_the_fifo_stream_stays() -> None:
    # A carried input's frames never name its own command: the continues_fifo
    # stream runs it under the interrupted turn's identity. The evidence of
    # delivery is the engine's input-consumed marker followed by a terminal
    # frame on that same carrier turn; a handoff turn minted here would wait
    # for frames that already streamed and settle only by watchdog.
    input_id = "9c1f2d51-9d2f-49a5-8a6e-2d55b1c0aa44"
    events = _Events(
        [
            _command(11, "StartTurn"),
            _command(35, "InterruptTurn"),
            _command(40, "SubmitInput", input_id=input_id, content="queued"),
        ],
        frames=[
            {
                # SessionEventRepository.list_frames() exposes the durable
                # event sequence under the public frame_seq name.
                "frame_seq": 49,
                "turn_id": "turn-1",
                "payload": {"type": "data-input-consumed", "id": f"input-consumed:{input_id}"},
            },
            {
                "frame_seq": 73,
                "turn_id": "turn-1",
                "command_id": "cmd-11",
                "payload": {"type": "data-result"},
            },
            {
                "frame_seq": 74,
                "turn_id": "turn-1",
                "command_id": "cmd-11",
                "payload": {"type": "finish"},
            },
        ],
    )
    harness = _SweepHarness(_idle_snapshot(), events)

    await harness._redrive_stranded_inputs_after_settle(_SESSION, turn_id="turn-1")

    assert events.claimed == []
    assert harness.spawned == []


@pytest.mark.asyncio
async def test_an_input_consumed_without_a_carrier_terminal_is_redriven() -> None:
    # The marker alone proves the engine took the input, not that anything
    # delivered its reply — the shape that motivated the sweep was consumed
    # AFTER turn.completed, answered by nothing. A terminal that precedes the
    # consumption belongs to the response the interrupt ended, so ordering is
    # part of the criterion, not just presence.
    input_id = "9c1f2d51-9d2f-49a5-8a6e-2d55b1c0aa55"
    events = _Events(
        [
            _command(11, "StartTurn"),
            _command(35, "InterruptTurn"),
            _command(40, "SubmitInput", input_id=input_id, content="queued"),
        ],
        frames=[
            {
                "frame_seq": 70,
                "turn_id": "turn-1",
                "command_id": "cmd-11",
                "payload": {"type": "finish"},
            },
            {
                "frame_seq": 80,
                "turn_id": "turn-1",
                "payload": {"type": "data-input-consumed", "id": f"input-consumed:{input_id}"},
            },
        ],
    )
    harness = _SweepHarness(_idle_snapshot(), events)

    await harness._redrive_stranded_inputs_after_settle(_SESSION, turn_id="turn-1")

    assert len(harness.spawned) == 1
    assert harness.spawned[0][0] == "cmd-40"


@pytest.mark.asyncio
async def test_a_live_conversation_is_left_alone() -> None:
    events = _Events(
        [
            _command(35, "InterruptTurn"),
            _command(39, "SubmitInput", input_id="b" * 36, content="x"),
        ],
        frames=[],
    )
    snapshot = {
        "conversation_state": "PROCESSING",
        "current_turn_id": "turn-2",
        "last_turn_id": "turn-1",
    }
    harness = _SweepHarness(snapshot, events)

    await harness._redrive_stranded_inputs_after_settle(_SESSION, turn_id="turn-1")

    assert events.claimed == []
