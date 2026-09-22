"""Reconcile ticks account for every candidate by outcome.

The scan selects active conversations with stale heartbeats, while recovery has
additional turn and runtime preconditions. Each selected row must land in one
outcome bucket so ``recovery_settled`` cannot include attempts that made no
progress. Repeated ``recovery_incomplete`` outcomes expose candidates that stay
eligible across ticks, and unrecoverable rows must leave the candidate set.
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import AsyncMock, Mock

from astrabox.core.service.orchestrator.session_kernel.workers.reconcile_worker import (
    _ACTIVE_CONVERSATION_STATES,
    ReconcileWorker,
)

_SID = "session-1"
_TURN = "turn-1"


def _worker(*, recover_returns: Any) -> ReconcileWorker:
    sessions_repo = AsyncMock()
    sessions_repo.get_session = AsyncMock(return_value={"session_id": _SID})
    interaction_repo = AsyncMock()
    interaction_repo.get_active_interaction = AsyncMock(return_value=None)
    return ReconcileWorker(
        session_snapshots_repo=AsyncMock(),
        sessions_repo=sessions_repo,
        session_events_repo=AsyncMock(),
        interaction_snapshots_repo=interaction_repo,
        resolve_sandbox_endpoint_fn=AsyncMock(),
        recover_engine_session_fn=AsyncMock(return_value=recover_returns),
        wakeup_turn_coordinator_fn=Mock(),
    )


_STUCK = {
    "session_id": _SID,
    "current_turn_id": _TURN,
    "conversation_state": "PROCESSING",
    "worker_heartbeat_at": "2020-01-01T00:00:00+00:00",
}


class OutcomeAccountingTests(unittest.IsolatedAsyncioTestCase):
    async def _scan(self, worker: ReconcileWorker) -> dict[str, int]:
        worker._find_stuck_sessions = AsyncMock(return_value=[dict(_STUCK)])  # type: ignore[method-assign]
        worker._session_snapshots_repo.get_snapshot = AsyncMock(return_value=dict(_STUCK))
        return await worker.scan_once()

    async def test_a_recovery_that_moved_nothing_is_not_counted_as_progress(self) -> None:
        """``None`` means the row remains eligible, so it counts as incomplete."""
        summary = await self._scan(_worker(recover_returns=None))

        self.assertEqual(summary["recovery_incomplete"], 1)
        self.assertEqual(summary["recovery_settled"], 0)

    async def test_a_recovery_that_committed_is_counted_as_progress(self) -> None:
        """A committed recovery counts as settled rather than incomplete."""
        summary = await self._scan(_worker(recover_returns={"session_id": _SID}))

        self.assertEqual(summary["recovery_settled"], 1)
        self.assertEqual(summary["recovery_incomplete"], 0)

    async def test_the_tick_reports_how_many_rows_it_looked_at(self) -> None:
        """`candidates` against the outcome keys is what makes a livelock
        legible: the same candidates every tick, none of them settled."""
        summary = await self._scan(_worker(recover_returns=None))

        self.assertEqual(summary["candidates"], 1)
        self.assertEqual(
            sum(
                summary[key]
                for key in ("recovery_settled", "recovery_incomplete", "skipped", "failed")
            ),
            1,
            "every candidate must land in exactly one outcome, or the breakdown "
            "hides rows instead of accounting for them",
        )

    async def test_an_empty_scan_reports_nothing_rather_than_a_zeroed_tick(self) -> None:
        """`any(summary.values())` is what the loop logs on, so an empty scan
        must be empty — a dict of zeros would log every idle tick."""
        worker = _worker(recover_returns=None)
        worker._find_stuck_sessions = AsyncMock(return_value=[])  # type: ignore[method-assign]

        self.assertEqual(await worker.scan_once(), {})


class LeavingTheCandidateSetTests(unittest.IsolatedAsyncioTestCase):
    """A row the scan can select but recovery can never run on.

    The scan asks "is this conversation active with a stale heartbeat"; recovery
    asks "is there a turn to recover". Those are different questions, and a row
    in the gap between them needs an explicit exit — without one, the cheapest
    exit is "try again next tick", which no branch has to justify.
    """

    async def _scan(self, worker: ReconcileWorker, snapshot: dict[str, Any]) -> dict[str, int]:
        worker._find_stuck_sessions = AsyncMock(return_value=[dict(snapshot)])  # type: ignore[method-assign]
        worker._session_snapshots_repo.get_snapshot = AsyncMock(return_value=dict(snapshot))
        worker._session_snapshots_repo.force_update_fields = AsyncMock(return_value=True)
        return await worker.scan_once()

    async def test_a_row_with_no_current_turn_is_settled_not_retried(self) -> None:
        worker = _worker(recover_returns=None)
        summary = await self._scan(
            worker,
            {
                "session_id": _SID,
                "conversation_state": "PROCESSING",
                "worker_heartbeat_at": "2020-01-01T00:00:00+00:00",
            },
        )

        self.assertEqual(summary["settled_unrecoverable"], 1)
        self.assertEqual(summary["recovery_incomplete"], 0)
        worker._recover_engine_session.assert_not_awaited()

    async def test_settling_actually_removes_it_from_the_scans_reach(self) -> None:
        """Not "stops being counted" — the row must stop matching the query, or
        the next tick selects it again and the count was the only thing fixed."""
        worker = _worker(recover_returns=None)
        await self._scan(
            worker,
            {
                "session_id": _SID,
                "conversation_state": "PROCESSING",
                "turn_recovery_phase": "AWAITING_RUNTIME",
                "worker_heartbeat_at": "2020-01-01T00:00:00+00:00",
            },
        )

        writes = worker._session_snapshots_repo.force_update_fields.await_args
        updates = writes.args[1] if len(writes.args) > 1 else writes.kwargs["updates"]
        self.assertNotIn(updates["conversation_state"], _ACTIVE_CONVERSATION_STATES)
        # The transcript-pending arm selects on this field, so a row that kept it
        # would leave one arm and stay selected by another.
        self.assertIsNone(updates["turn_recovery_phase"])

    async def test_a_row_that_changed_while_being_judged_is_left_alone(self) -> None:
        """The CAS half. A worker that came back to life between the scan and
        the verdict still owns its turn, and settling it would end a live one."""
        worker = _worker(recover_returns=None)
        worker._find_stuck_sessions = AsyncMock(  # type: ignore[method-assign]
            return_value=[{"session_id": _SID, "conversation_state": "PROCESSING",
                           "worker_heartbeat_at": "2020-01-01T00:00:00+00:00"}]
        )
        worker._session_snapshots_repo.get_snapshot = AsyncMock(
            return_value={"session_id": _SID, "conversation_state": "PROCESSING",
                          "worker_heartbeat_at": "2020-01-01T00:00:00+00:00"}
        )
        worker._session_snapshots_repo.force_update_fields = AsyncMock(return_value=False)

        summary = await worker.scan_once()

        self.assertEqual(summary["settled_unrecoverable"], 0)
        self.assertEqual(summary["skipped"], 1)

    async def test_a_recoverable_row_still_reaches_recovery(self) -> None:
        """The half that keeps the predicate from swallowing real work."""
        worker = _worker(recover_returns={"session_id": _SID})
        summary = await self._scan(worker, dict(_STUCK))

        self.assertEqual(summary["recovery_settled"], 1)
        self.assertEqual(summary["settled_unrecoverable"], 0)
        worker._recover_engine_session.assert_awaited_once()
