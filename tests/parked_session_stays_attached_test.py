"""A session parked on a question needs the host present, not recovered.

The measured failure: the box asks for tool approval, blocks its PreToolUse
hook holding the answer slot, and gives up once the host has been absent longer
than its budget. Restart the platform and the approval dies — the user clicks
Allow and gets INTERACTION_EXPIRED — while the session sits perfectly healthy.

Nothing reconnected because the one piece of code that looks at parked sessions
is the reconciler's parked fence, whose entire job is to leave them alone. It is
right about recovery: a parked turn's heartbeat is stale BY DESIGN and
recovering it would fail a healthy turn. It was wrong that leaving a turn alone
also means staying away from its box.
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

from astrabox.core.service.orchestrator.session_kernel.workers.reconcile_worker import (
    ReconcileWorker,
)

_SID = "sess-parked"


def _worker(attach: Any) -> ReconcileWorker:
    worker = ReconcileWorker.__new__(ReconcileWorker)
    worker._attach_parked_runtime = attach
    worker._recover_engine_session = AsyncMock()
    worker._resume_orphaned_answer_command = None
    worker._wakeup_turn_coordinator = None
    snapshot = {"session_id": _SID, "conversation_state": "WAITING_FOR_INTERACTION"}
    worker._session_snapshots_repo = AsyncMock()
    worker._session_snapshots_repo.get_snapshot.return_value = snapshot
    worker._sessions_repo = AsyncMock()
    return worker


class ParkedSessionsStayAttachedTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_parked_session_is_attached_and_never_recovered(self) -> None:
        attach = AsyncMock()
        worker = _worker(attach)
        snapshot = {"session_id": _SID, "conversation_state": "WAITING_FOR_INTERACTION"}

        with patch.object(
            ReconcileWorker, "_is_parked_awaiting_interaction", AsyncMock(return_value=True)
        ), patch.object(
            ReconcileWorker, "_find_stuck_sessions", AsyncMock(return_value=[snapshot])
        ), patch.object(
            # The staleness gate is upstream of the branch under test and needs
            # heartbeat fields this harness does not fabricate.
            ReconcileWorker, "_should_reconcile_snapshot", lambda *a, **k: True
        ):
            await worker.scan_once()

        attach.assert_awaited_once_with(_SID)
        worker._recover_engine_session.assert_not_awaited()

    async def test_an_attach_failure_never_stops_the_scan(self) -> None:
        # Presence is best-effort: an unreachable session must not stall the
        # reconciler for every other session behind it.
        attach = AsyncMock(side_effect=RuntimeError("box unreachable"))
        worker = _worker(attach)
        snapshot = {"session_id": _SID, "conversation_state": "WAITING_FOR_INTERACTION"}

        with patch.object(
            ReconcileWorker, "_is_parked_awaiting_interaction", AsyncMock(return_value=True)
        ), patch.object(
            ReconcileWorker, "_find_stuck_sessions", AsyncMock(return_value=[snapshot])
        ), patch.object(
            # The staleness gate is upstream of the branch under test and needs
            # heartbeat fields this harness does not fabricate.
            ReconcileWorker, "_should_reconcile_snapshot", lambda *a, **k: True
        ):
            try:
                summary = await worker.scan_once()
            except RuntimeError:
                self.fail("an unreachable parked box must not propagate out of the scan")
        self.assertEqual(
            summary["recovery_settled"], 0, "a parked session is never counted as reconciled"
        )
        # And a failed attach is still presence: the row was handled by its own
        # arm, which is what keeps it out of the recovery path next tick.
        self.assertEqual(summary["parked_present"], 1)


if __name__ == "__main__":
    unittest.main()
