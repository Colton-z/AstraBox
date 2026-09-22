"""A detached claude turn hands off to mirror recovery instead of dying.

The claude engine client has no turn-event replay — the durable transcript
mirror is its recovery authority, and the in-box runner keeps executing the
turn while the platform is detached. Settling such a turn FAILED-unrecoverable
(empty reply, turn_failure block) while the mirror is still receiving the real
content would discard content that exists. The handoff instead settles FAILED
with the remote anchor preserved, which derives
turn_recovery_phase=TRANSCRIPT_PENDING — the turn coordinator's pickup signal
to replace the failure with the mirror-recovered content.
"""

from __future__ import annotations

import unittest
from typing import Any

from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    TURN_RECOVERY_PHASE_TRANSCRIPT_PENDING,
)
from astrabox.core.service.orchestrator.session_kernel.service_mixins.durable_recovery_assistant import (
    DurableEngineRecoveryMixin,
)


class _FakeJournal:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def try_claim_event(self, event: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        stored = {**event, "event_seq": len(self.events) + 1}
        self.events.append(stored)
        return stored, True


class _FakeSnapshots:
    def __init__(self) -> None:
        self.applied: list[dict[str, Any]] = []

    async def apply_channel_update(self, session_id: str, **kwargs: Any) -> dict[str, Any]:
        self.applied.append({"session_id": session_id, **kwargs})
        return {"session_id": session_id, **kwargs.get("updates", {})}

    async def get_snapshot(self, session_id: str) -> dict[str, Any]:
        return {"session_id": session_id}


class _FakeSessions:
    def __init__(self) -> None:
        self.updated: list[tuple[str, dict[str, Any]]] = []

    async def update_session(self, session_id: str, updates: dict[str, Any]) -> None:
        self.updated.append((session_id, dict(updates)))


class _Harness(DurableEngineRecoveryMixin):
    def __init__(self) -> None:
        self._session_events_repo = _FakeJournal()
        self._session_snapshots_repo = _FakeSnapshots()
        self._sessions_repo = _FakeSessions()
        self.error_frames: list[str] = []

    async def _append_recovery_error_frame(self, **kwargs: Any) -> dict[str, Any]:
        self.error_frames.append(str(kwargs.get("error_text") or ""))
        return {"type": "error"}


_ANCHOR = {"sandbox_turn_id": 7, "last_sandbox_seq": 41}


class TranscriptPendingHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_handoff_preserves_anchor_and_enters_transcript_pending(self) -> None:
        h = _Harness()
        result = await h._settle_engine_turn_transcript_pending(
            session_id="s-1",
            session={"session_id": "s-1"},
            snapshot={"conversation_state": "STREAMING"},
            turn_id="t-1",
            command_id="c-1",
            engine_kind="custom_mirror_engine",
            remote_anchor=dict(_ANCHOR),
        )
        # The journal carries the provisional terminal (idempotent by causation).
        self.assertEqual(len(h._session_events_repo.events), 1)
        event = h._session_events_repo.events[0]
        self.assertEqual(event["event_type"], "turn.failed")
        self.assertEqual(event["causation_id"], "recover:s-1:t-1")
        # The snapshot settle is the mirror-lane pickup signal: FAILED with the
        # anchor preserved derives TRANSCRIPT_PENDING — not a dead turn.
        applied = h._session_snapshots_repo.applied[0]["updates"]
        self.assertEqual(applied["last_turn_status"], "FAILED")
        self.assertEqual(
            applied["turn_recovery_phase"], TURN_RECOVERY_PHASE_TRANSCRIPT_PENDING
        )
        self.assertEqual(
            applied["current_turn_remote_anchor"], {"sandbox_turn_id": 7, "last_sandbox_seq": 41}
        )
        # No fabricated error terminal frame: the coordinator owns the terminal
        # frame it recovers from the mirror.
        self.assertEqual(h.error_frames, [])
        self.assertEqual(event["payload"]["assistant_text"], None)
        self.assertEqual(event["payload"]["blocks"], [])
        self.assertIsInstance(result, dict)

    async def test_anchorless_death_still_enters_transcript_pending(self) -> None:
        # A bridge that died before the first mirror row was observed has no
        # remote anchor — the coordinator derives its handle from the journal
        # (dispatch.confirmed), so the handoff must still enter the pending
        # phase instead of falling back to a dead FAILED.
        h = _Harness()
        await h._settle_engine_turn_transcript_pending(
            session_id="s-3",
            session={"session_id": "s-3"},
            snapshot={"conversation_state": "STREAMING"},
            turn_id="t-3",
            command_id="c-3",
            engine_kind="custom_mirror_engine",
            remote_anchor=None,
        )
        applied = h._session_snapshots_repo.applied[0]["updates"]
        self.assertEqual(
            applied["turn_recovery_phase"], TURN_RECOVERY_PHASE_TRANSCRIPT_PENDING
        )
        self.assertIsNone(applied["current_turn_remote_anchor"])

    async def test_settle_is_gated_on_the_observed_conversation_state(self) -> None:
        h = _Harness()
        await h._settle_engine_turn_transcript_pending(
            session_id="s-2",
            session={"session_id": "s-2"},
            snapshot={"conversation_state": "STREAMING"},
            turn_id="t-2",
            command_id="c-2",
            engine_kind="custom_mirror_engine",
            remote_anchor=dict(_ANCHOR),
        )
        # The CAS carries the observed state, so a turn that moved on (e.g. a
        # live bridge that resumed) is never clobbered by a stale settle.
        self.assertEqual(
            h._session_snapshots_repo.applied[0]["expected_conversation_state"],
            "STREAMING",
        )


if __name__ == "__main__":
    unittest.main()
