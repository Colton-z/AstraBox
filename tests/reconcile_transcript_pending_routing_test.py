"""A TRANSCRIPT_PENDING row is the turn coordinator's, not anchor recovery's.

The mirror-handoff settles a detached claude turn into FAILED +
turn_recovery_phase=TRANSCRIPT_PENDING; the coordinator (mirror-fed, per-turn
lease) recovers the content or settles the turn unrecoverable when the box is
gone — a TERMINAL outcome either way. Routing such rows back into anchor
recovery would repeatedly probe the provider for rows whose terminal owner is
the coordinator.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from astrabox.core.service.orchestrator.session_kernel.workers.reconcile_worker import (
    ReconcileWorker,
    _stuck_sessions_query,
)

_SID = "session-1"
_TURN = "turn-1"


def _build_worker(*, recover_fn: Any, wakeup_fn: Any) -> ReconcileWorker:
    interaction_repo = AsyncMock()
    interaction_repo.get_active_interaction.return_value = None
    sessions_repo = AsyncMock()
    sessions_repo.get_session.return_value = {"session_id": _SID}
    return ReconcileWorker(
        session_snapshots_repo=AsyncMock(),
        sessions_repo=sessions_repo,
        session_events_repo=AsyncMock(),
        interaction_snapshots_repo=interaction_repo,
        resolve_sandbox_endpoint_fn=AsyncMock(),
        recover_engine_session_fn=recover_fn,
        wakeup_turn_coordinator_fn=wakeup_fn,
    )


def _transcript_pending_snapshot() -> dict[str, Any]:
    return {
        "session_id": _SID,
        "conversation_state": "IDLE",
        "last_turn_id": _TURN,
        "last_turn_status": "FAILED",
        "turn_recovery_phase": "TRANSCRIPT_PENDING",
        "worker_heartbeat_at": "2020-01-01T00:00:00+00:00",
    }


class TranscriptPendingRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def _scan(
        self, worker: ReconcileWorker, snapshot: dict[str, Any]
    ) -> dict[str, int]:
        worker._session_snapshots_repo.get_snapshot = AsyncMock(return_value=snapshot)
        with patch.object(
            ReconcileWorker,
            "_find_stuck_sessions",
            AsyncMock(return_value=[snapshot]),
        ):
            return await worker.scan_once()

    async def test_pending_row_goes_to_the_coordinator_not_anchor_recovery(self) -> None:
        recover = AsyncMock()
        wakeup = Mock()
        worker = _build_worker(recover_fn=recover, wakeup_fn=wakeup)
        summary = await self._scan(worker, _transcript_pending_snapshot())
        self.assertEqual(summary["transcript_pending_routed"], 1)
        wakeup.assert_called_once_with(_SID)
        recover.assert_not_awaited()

    async def test_a_deleted_sessions_orphan_leaves_the_candidate_set_failed(self) -> None:
        # The session row is invisible while its snapshot remains DELETED +
        # IDLE + FAILED + TRANSCRIPT_PENDING. Merely scheduling the coordinator
        # is not progress; one scan must leave the durable row outside the
        # pending arm's match.
        recover = AsyncMock()
        wakeup = Mock()
        worker = _build_worker(recover_fn=recover, wakeup_fn=wakeup)
        worker._sessions_repo.get_session = AsyncMock(return_value=None)
        worker._session_snapshots_repo.force_update_fields = AsyncMock(return_value=True)
        snapshot = {
            **_transcript_pending_snapshot(),
            "session_lifecycle_state": "DELETED",
            "last_turn_terminal_frame": None,
        }

        summary = await self._scan(worker, snapshot)

        self.assertEqual(summary["settled_deleted_session_orphan"], 1)
        self.assertEqual(summary["transcript_pending_routed"], 0)
        wakeup.assert_not_called()
        recover.assert_not_awaited()
        worker._sessions_repo.get_session.assert_awaited_once_with(_SID)

        settle = worker._session_snapshots_repo.force_update_fields.await_args
        updates = settle.args[1]
        post_scan = {**snapshot, **updates}
        self.assertEqual(post_scan["conversation_state"], "IDLE")
        self.assertEqual(post_scan["last_turn_status"], "FAILED")
        self.assertIsNone(post_scan["turn_recovery_phase"])
        self.assertEqual(post_scan["session_lifecycle_state"], "DELETED")
        self.assertFalse(
            worker._should_reconcile_snapshot(
                post_scan,
                stale_threshold=datetime.now(timezone.utc),
            )
        )
        # Fence against an already-running coordinator resolving the same row
        # between the read and this terminal verdict.
        self.assertEqual(
            settle.kwargs["extra_filter"]["turn_recovery_phase"],
            "TRANSCRIPT_PENDING",
        )

    async def test_mid_turn_row_still_goes_to_anchor_recovery(self) -> None:
        recover = AsyncMock()
        wakeup = Mock()
        worker = _build_worker(recover_fn=recover, wakeup_fn=wakeup)
        summary = await self._scan(
            worker,
            {
                "session_id": _SID,
                "current_turn_id": _TURN,
                "conversation_state": "PROCESSING",
                "worker_heartbeat_at": "2020-01-01T00:00:00+00:00",
            },
        )
        self.assertEqual(summary["recovery_settled"], 1)
        recover.assert_awaited_once()
        wakeup.assert_not_called()


class StuckSessionsQueryShapeTests(unittest.TestCase):
    def test_the_pending_arm_carries_no_lifecycle_exclusion(self) -> None:
        # The rule existed twice — a top-level $nin in this query AND the
        # Python gate — and fixing the gate alone changed nothing: the DELETED
        # orphan never left the database. One copy, scoped to the arm it
        # belongs to, pinned here so the second copy cannot quietly return.
        query = _stuck_sessions_query("2026-01-01T00:00:00+00:00")
        self.assertNotIn("session_lifecycle_state", query)
        arms = query["$or"]
        # Found by what each arm IS, not by where it sits: the scan grew a
        # third (presence-only, parked rows) and a positional unpack made that
        # look like a regression in a rule it never touched.
        pending_arm = next(a for a in arms if "turn_recovery_phase" in a)
        stale_arm = next(a for a in arms if "worker_heartbeat_at" in str(a))
        self.assertIn("session_lifecycle_state", stale_arm)
        self.assertEqual(
            pending_arm, {"turn_recovery_phase": "TRANSCRIPT_PENDING"}
        )
        # Presence must not smuggle the exclusion back to the top level, and it
        # carries its own — a terminated session's box is not waiting to be
        # reconciled.
        parked_arm = next(
            a for a in arms
            if "conversation_state" in a and "worker_heartbeat_at" not in str(a)
        )
        self.assertIn("session_lifecycle_state", parked_arm)


if __name__ == "__main__":
    unittest.main()
