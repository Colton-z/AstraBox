"""Stream close implies the turn slot is admissible — and not a second more.

Two measured failure modes bound this contract from both sides. Closing on the
durable finish frame alone is a few milliseconds EARLY: a no-gap client races
the terminal snapshot CAS and 409s SESSION_BUSY. Waiting for the worker task
is seconds LATE: run() keeps doing post-settle work (drains, manifests), and
the held-open stream shows the client a phantom "generating" tail past the
settle budget. The close therefore waits on the contract itself: the snapshot
leaving the mid-turn states, fast-polled and bounded.
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from astrabox.core.service.orchestrator.session_kernel.service_mixins.turn_dispatch import (
    TurnDispatchStreamingMixin,
)


class _FakeSnapshots:
    def __init__(self, states: list[dict[str, Any]]) -> None:
        self.states = list(states)
        self.reads = 0

    async def get_snapshot(self, session_id: str) -> dict[str, Any]:
        self.reads += 1
        if len(self.states) > 1:
            return self.states.pop(0)
        return self.states[0]


class _Harness(TurnDispatchStreamingMixin):
    def __init__(self, snapshots: _FakeSnapshots) -> None:
        self._session_snapshots_repo = snapshots
        self.coordinator_wakeups: list[str] = []

    def _wakeup_turn_coordinator(self, session_id: str) -> None:
        self.coordinator_wakeups.append(session_id)


class TailAwaitTurnSlotSettledTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_waits_until_the_slot_frees(self) -> None:
        snapshots = _FakeSnapshots(
            [
                {"conversation_state": "STREAMING", "current_turn_id": "t-1"},
                {"conversation_state": "STREAMING", "current_turn_id": "t-1"},
                {"conversation_state": "IDLE", "current_turn_id": None},
            ]
        )
        h = _Harness(snapshots)
        await h._tail_await_turn_slot_settled("s-1", turn_id="t-1")
        # It polled through the mid-turn states and returned on IDLE.
        self.assertEqual(snapshots.reads, 3)

    async def test_a_parked_turn_is_a_settled_boundary(self) -> None:
        snapshots = _FakeSnapshots(
            [{"conversation_state": "WAITING_FOR_INTERACTION", "current_turn_id": "t-1"}]
        )
        h = _Harness(snapshots)
        await h._tail_await_turn_slot_settled("s-1", turn_id="t-1")
        self.assertEqual(snapshots.reads, 1)

    async def test_a_superseding_turn_frees_the_close(self) -> None:
        # The slot changed hands: whatever owns it now, this stream is over.
        snapshots = _FakeSnapshots(
            [{"conversation_state": "PROCESSING", "current_turn_id": "t-NEXT"}]
        )
        h = _Harness(snapshots)
        await h._tail_await_turn_slot_settled("s-1", turn_id="t-1")
        self.assertEqual(snapshots.reads, 1)

    async def test_transcript_pending_waits_and_wakes_the_coordinator_once(self) -> None:
        # The bridge died pre-terminal and the worker settled THIS turn into
        # TRANSCRIPT_PENDING; the close must ride out the phase (its resolver
        # is kicked immediately, not left to the reconcile tick) so the next
        # send is admitted instead of 409ing on recovery-pending.
        snapshots = _FakeSnapshots(
            [
                {
                    "conversation_state": "IDLE",
                    "last_turn_id": "t-1",
                    "turn_recovery_phase": "TRANSCRIPT_PENDING",
                },
                {
                    "conversation_state": "IDLE",
                    "last_turn_id": "t-1",
                    "turn_recovery_phase": "TRANSCRIPT_PENDING",
                },
                {"conversation_state": "IDLE", "last_turn_id": "t-1"},
            ]
        )
        h = _Harness(snapshots)
        await h._tail_await_turn_slot_settled("s-1", turn_id="t-1")
        self.assertEqual(snapshots.reads, 3)
        self.assertEqual(h.coordinator_wakeups, ["s-1"])

    async def test_another_turns_pending_phase_does_not_hold_the_close(self) -> None:
        snapshots = _FakeSnapshots(
            [
                {
                    "conversation_state": "IDLE",
                    "last_turn_id": "t-OLD",
                    "turn_recovery_phase": "TRANSCRIPT_PENDING",
                }
            ]
        )
        h = _Harness(snapshots)
        await h._tail_await_turn_slot_settled("s-1", turn_id="t-1")
        self.assertEqual(snapshots.reads, 1)
        self.assertEqual(h.coordinator_wakeups, [])

    async def test_timeout_closes_anyway(self) -> None:
        snapshots = _FakeSnapshots(
            [{"conversation_state": "PROCESSING", "current_turn_id": "t-1"}]
        )
        h = _Harness(snapshots)
        started = asyncio.get_running_loop().time()
        await h._tail_await_turn_slot_settled("s-1", turn_id="t-1", timeout_s=0.1)
        elapsed = asyncio.get_running_loop().time() - started
        self.assertLess(elapsed, 2.0, "the wait must be bounded")


if __name__ == "__main__":
    unittest.main()
