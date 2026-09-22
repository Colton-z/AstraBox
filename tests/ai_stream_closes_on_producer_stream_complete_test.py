"""The body ends when the PRODUCER says the stream is over, not when its task dies.

The bridge announces `ai_sdk_stream_complete` the moment it stops producing.
At an interaction boundary that moment is the whole point — the segment must
close while the worker itself stays alive waiting on a human — so keying the
body's end on the worker task conflates two different events, and at a park the
task never completes at all.
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from astrabox.core.service.orchestrator.session_kernel.service_mixins.turn_dispatch import (
    TurnDispatchStreamingMixin,
    _TailFrameState,
)

_SID = "session-1"
_CMD = "command-1"
_TURN = "turn-1"


class _FakeBroker:
    def __init__(self, queue: asyncio.Queue[Any]) -> None:
        self._queue = queue

    async def subscribe(self, session_id: str) -> asyncio.Queue[Any]:
        _ = session_id
        return self._queue


class _Harness(TurnDispatchStreamingMixin):
    def __init__(self, queue: asyncio.Queue[Any]) -> None:
        self._broker = _FakeBroker(queue)
        self._poll_interval_s = 0.05
        self.storage_reads = 0

    async def _tail_read_available_frames(self, state: _TailFrameState):
        _ = state
        self.storage_reads += 1
        return [], False


def _complete_event(**overrides: Any) -> dict[str, Any]:
    return {
        "type": "ai_sdk_stream_complete",
        "command_id": _CMD,
        "turn_id": _TURN,
        **overrides,
    }


async def _never_ends() -> None:
    await asyncio.Event().wait()


class StreamCompleteClosesBodyTests(unittest.IsolatedAsyncioTestCase):
    async def _drain(self, event: dict[str, Any] | None) -> None:
        queue: asyncio.Queue[Any] = asyncio.Queue()
        if event is not None:
            queue.put_nowait(event)
        harness = _Harness(queue)
        producer = asyncio.create_task(_never_ends())
        self.addCleanup(producer.cancel)
        frames = [
            frame
            async for frame in harness._tail_frames(
                _SID,
                command_id=_CMD,
                turn_id=_TURN,
                producer_task=producer,
            )
        ]
        self.assertEqual(frames, [])
        self.assertFalse(producer.done(), "the worker outlives the segment by design")

    async def test_the_body_ends_while_the_parked_worker_is_still_alive(self) -> None:
        await asyncio.wait_for(self._drain(_complete_event()), timeout=5.0)

    async def test_another_commands_completion_does_not_end_this_body(self) -> None:
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(
                self._drain(_complete_event(command_id="command-OTHER")), timeout=1.0
            )

    async def test_another_turns_completion_does_not_end_this_body(self) -> None:
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(
                self._drain(_complete_event(turn_id="turn-OTHER")), timeout=1.0
            )

    async def test_a_live_producer_with_no_announcement_keeps_the_body_open(self) -> None:
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(self._drain(None), timeout=1.0)


class StreamCompletePredicateTests(unittest.TestCase):
    def _state(self, *, command_id: str | None = _CMD, turn_id: str | None = _TURN):
        return _TailFrameState(
            session_id=_SID,
            command_id=command_id,
            turn_id=turn_id,
            after_seq=-1,
            include_terminal_resume_cursor=False,
            stop_on_segment_finish=True,
        )

    def test_only_the_completion_event_type_counts(self) -> None:
        state = self._state()
        self.assertTrue(
            TurnDispatchStreamingMixin._tail_is_stream_complete(state, _complete_event())
        )
        self.assertFalse(
            TurnDispatchStreamingMixin._tail_is_stream_complete(
                state, {**_complete_event(), "type": "ai_sdk_live_frame"}
            )
        )
        self.assertFalse(TurnDispatchStreamingMixin._tail_is_stream_complete(state, None))

    def test_an_unfiltered_tail_accepts_any_command(self) -> None:
        # A resume tail without a command/turn filter still ends on the producer's
        # announcement rather than hanging on a task it never spawned.
        state = self._state(command_id=None, turn_id=None)
        self.assertTrue(
            TurnDispatchStreamingMixin._tail_is_stream_complete(
                state, _complete_event(command_id="command-OTHER", turn_id="turn-OTHER")
            )
        )


if __name__ == "__main__":
    unittest.main()
