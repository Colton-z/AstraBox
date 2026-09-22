"""Stopping a parked turn is an outcome; losing its sandbox is a failure.

Both go through the same settle — a parked turn's worker has exited, so
whoever takes the interaction's runtime away has to close the turn — so the
settle must keep them apart: recording both as FAILED with an error text
would tell a user who pressed stop, in the transcript and on the session,
that something had gone wrong.

The vendor SDK keeps the two apart and does it without a third status:
`SDKResultMessage` has exactly `success` and `error`, an interrupted run
returns SUCCESS, but this platform-side closure did not observe that result.
It therefore settles COMPLETED with no error and no engine terminal reason;
the preceding interrupt event records why the platform closed it. The held
tool call closes the way a denial closes rather than the way a crash does.
"""

from __future__ import annotations

import unittest
from typing import Any

from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    settle_parked_turn,
)

_SID = "session-1"
_TURN = "turn-1"


class _FakeJournal:
    def __init__(self) -> None:
        self.appended: list[dict[str, Any]] = []

    async def append_event(self, event: dict[str, Any]) -> dict[str, Any]:
        stored = {**event, "event_seq": len(self.appended) + 1}
        self.appended.append(stored)
        return stored


class _FakeSnapshots:
    def __init__(self) -> None:
        self.updates: dict[str, Any] = {}

    async def apply_channel_update(self, session_id: str, **kwargs: Any) -> dict[str, Any]:
        self.updates = dict(kwargs["updates"])
        return {"session_id": session_id}


class _FakeInteractions:
    async def deactivate_active_for_turn(self, session_id: str, turn_id: str) -> int:
        _ = (session_id, turn_id)
        return 1


class _FakeFrames:
    def __init__(self, existing: list[dict[str, Any]] | None = None) -> None:
        self.frames = list(existing or [])
        self.appended: list[dict[str, Any]] = []

    async def list_frames(self, session_id: str, *, turn_id: str, after_seq: int = -1):
        _ = (session_id, turn_id, after_seq)
        return [dict(item) for item in self.frames]

    async def get_next_session_frame_seq(self, session_id: str) -> int:
        _ = session_id
        return len(self.frames) + len(self.appended)

    async def append_frame(self, frame: dict[str, Any]) -> None:
        self.appended.append(dict(frame))


class _FakeSessionEvents:
    def __init__(self, journal: _FakeJournal, frames: _FakeFrames) -> None:
        self._journal = journal
        self._frames = frames

    async def append_event(self, event: dict[str, Any]) -> dict[str, Any]:
        return await self._journal.append_event(event)

    async def list_frames(self, session_id: str, *, turn_id: str, after_seq: int = -1):
        return await self._frames.list_frames(
            session_id,
            turn_id=turn_id,
            after_seq=after_seq,
        )

    async def get_next_session_frame_seq(self, session_id: str) -> int:
        return await self._frames.get_next_session_frame_seq(session_id)

    async def append_frame(self, frame: dict[str, Any]) -> None:
        await self._frames.append_frame(frame)


def _held_tool_call() -> list[dict[str, Any]]:
    return [
        {"payload": {"type": "tool-input-start", "toolCallId": "toolu_1"}},
        {"payload": {"type": "tool-input-available", "toolCallId": "toolu_1"}},
    ]


async def _settle(**overrides: Any) -> tuple[_FakeJournal, _FakeSnapshots, _FakeFrames]:
    journal, snapshots = _FakeJournal(), _FakeSnapshots()
    frames = _FakeFrames(_held_tool_call())
    events = _FakeSessionEvents(journal, frames)
    kwargs: dict[str, Any] = {
        "session_id": _SID,
        "turn_id": _TURN,
        "command_id": "cmd-1",
        "status": "COMPLETED",
        "failure_phase": None,
        "error_text": None,
        "causation": f"interrupt-settle:{_SID}:{_TURN}",
    }
    kwargs.update(overrides)
    await settle_parked_turn(
        session_events_repo=events,
        session_snapshots_repo=snapshots,
        interaction_snapshots_repo=_FakeInteractions(),
        **kwargs,
    )
    return journal, snapshots, frames


def _payload_types(frames: _FakeFrames) -> list[str]:
    return [str((f.get("payload") or {}).get("type") or "") for f in frames.appended]


class ParkedTurnSettleOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_user_stop_is_a_completed_turn_with_no_error(self) -> None:
        journal, snapshots, _ = await _settle()
        self.assertEqual(snapshots.updates["last_turn_status"], "COMPLETED")
        self.assertIsNone(snapshots.updates["last_turn_error"])
        # No engine terminal was observed on this platform-side closure.
        self.assertIsNone(snapshots.updates["last_turn_terminal_reason"])
        self.assertIsNone(snapshots.updates["last_turn_failure_phase"])
        # The journal must not call it a failure while the snapshot calls it done.
        self.assertEqual([e["event_type"] for e in journal.appended], ["turn.completed"])

    async def test_a_stopped_turn_still_ends_on_a_terminal_frame(self) -> None:
        # The gate reads this frame as the proof the turn is over, and the
        # vendor emits a result for an interrupted run too.
        _, snapshots, frames = await _settle()
        self.assertIn("finish", _payload_types(frames))
        proof = snapshots.updates["last_turn_terminal_frame"]
        self.assertEqual(proof["type"], "finish")
        self.assertEqual(proof["turn_id"], _TURN)
        self.assertEqual(proof["command_id"], "cmd-1")

    async def test_the_held_tool_call_closes_as_denied_not_as_a_crash(self) -> None:
        _, _, frames = await _settle()
        self.assertIn("tool-output-denied", _payload_types(frames))
        self.assertNotIn("tool-output-error", _payload_types(frames))

    async def test_a_lost_sandbox_is_still_a_failure(self) -> None:
        journal, snapshots, frames = await _settle(
            command_id=None,
            status="FAILED",
            failure_phase="sandbox_reclaimed",
            error_text="sandbox reclaimed while awaiting interaction",
            causation=f"reclaim-settle:{_SID}:{_TURN}",
        )
        self.assertEqual(snapshots.updates["last_turn_status"], "FAILED")
        self.assertIn("reclaimed", snapshots.updates["last_turn_error"])
        # Sandbox reclamation is a platform lifecycle reason; the engine reports
        # no terminal reason for this path.
        self.assertEqual(snapshots.updates["last_turn_failure_phase"], "sandbox_reclaimed")
        self.assertIsNone(snapshots.updates["last_turn_terminal_reason"])
        self.assertEqual([e["event_type"] for e in journal.appended], ["turn.failed"])
        self.assertIn("tool-output-error", _payload_types(frames))

    async def test_a_settle_without_a_command_invents_no_terminal_frame(self) -> None:
        # A terminal frame is addressed by (turn, command). With no command
        # there is nothing to address, and a proof nobody can match is worse
        # than none — recovery owns the turn from here.
        _, snapshots, frames = await _settle(
            command_id=None,
            status="FAILED",
            failure_phase="sandbox_reclaimed",
            error_text="gone",
        )
        self.assertNotIn("finish", _payload_types(frames))
        self.assertIsNone(snapshots.updates["last_turn_terminal_frame"])


if __name__ == "__main__":
    unittest.main()
