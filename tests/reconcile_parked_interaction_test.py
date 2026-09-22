"""The reconciler must not "recover" a turn parked at an open interaction.

The park design (answer continuation, see turn_dispatch's
``_answer_via_engine_client``): the turn worker exits at the interaction
boundary so the SSE segment can close, which means a parked turn's heartbeat
goes stale BY DESIGN. Before the fence, ``ReconcileWorker.scan_once`` treated
that signature as a dead worker ~12 s after every park and invoked anchor
recovery on a healthy turn (the give-up path would have FAILED the turn had
it not crashed first).

The fence is scoped: only a WAITING_FOR_INTERACTION snapshot whose ACTIVE
interaction belongs to the current turn is parked. Once the interaction is
answered (its ACTIVE status cleared), a stale heartbeat is real evidence of a
dead continuation and recovery must proceed.

Also pins ``_append_recovery_error_frame`` — the failed-turn terminal used by
``_fail_engine_turn_unrecoverable`` — against referencing a method that
does not exist (an AttributeError there crashes the reconciler instead of
settling the turn).
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

from astrabox.core.service.orchestrator.session_kernel.workers.reconcile_worker import (
    ReconcileWorker,
)

_SID = "session-1"
_TURN = "turn-1"


def _build_worker(
    *,
    active_interaction: dict[str, Any] | None,
    recover_fn: Any,
    resume_fn: Any = None,
) -> ReconcileWorker:
    interaction_repo = AsyncMock()
    interaction_repo.get_active_interaction.return_value = active_interaction
    sessions_repo = AsyncMock()
    sessions_repo.get_session.return_value = {"session_id": _SID}
    return ReconcileWorker(
        session_snapshots_repo=AsyncMock(),
        sessions_repo=sessions_repo,
        session_events_repo=AsyncMock(),
        interaction_snapshots_repo=interaction_repo,
        resolve_sandbox_endpoint_fn=AsyncMock(),
        resume_orphaned_answer_command_fn=resume_fn,
        recover_engine_session_fn=recover_fn,
    )


def _stale_processing_snapshot(**overrides: Any) -> dict[str, Any]:
    # The snapshot still says PROCESSING after its watermark write loses the
    # race to the pending-interaction projection; the scan's active-state filter
    # therefore cannot exclude it on its own.
    return {
        "session_id": _SID,
        "current_turn_id": _TURN,
        "conversation_state": "PROCESSING",
        "worker_heartbeat_at": "2020-01-01T00:00:00+00:00",
        **overrides,
    }


class ParkedInteractionFenceTests(unittest.IsolatedAsyncioTestCase):
    async def _scan(self, worker: ReconcileWorker, snapshot: dict[str, Any]) -> int:
        worker._session_snapshots_repo.get_snapshot = AsyncMock(return_value=snapshot)
        with patch.object(
            ReconcileWorker,
            "_find_stuck_sessions",
            AsyncMock(return_value=[snapshot]),
        ):
            return await worker.scan_once()

    async def test_parked_turn_with_open_interaction_is_left_alone(self) -> None:
        recover = AsyncMock()
        worker = _build_worker(
            active_interaction={"interaction_id": "i-1", "turn_id": _TURN},
            recover_fn=recover,
        )
        summary = await self._scan(worker, _stale_processing_snapshot())
        self.assertEqual(summary["recovery_settled"], 0)
        self.assertEqual(summary["recovery_incomplete"], 0)
        recover.assert_not_awaited()

    async def test_answered_interaction_no_longer_fences_recovery(self) -> None:
        recover = AsyncMock()
        worker = _build_worker(active_interaction=None, recover_fn=recover)
        summary = await self._scan(worker, _stale_processing_snapshot())
        self.assertEqual(summary["recovery_settled"], 1)
        recover.assert_awaited_once()

    async def test_other_turns_interaction_does_not_fence(self) -> None:
        # An active interaction left over from a DIFFERENT turn is not this
        # turn's park — the stale heartbeat still means a dead worker.
        recover = AsyncMock()
        worker = _build_worker(
            active_interaction={"interaction_id": "i-0", "turn_id": "other-turn"},
            recover_fn=recover,
        )
        summary = await self._scan(worker, _stale_processing_snapshot())
        self.assertEqual(summary["recovery_settled"], 1)
        recover.assert_awaited_once()

    async def test_a_healthy_park_is_attended_to_and_never_recovered(self) -> None:
        # A converged park (WAITING_FOR_INTERACTION) still never reaches
        # recovery, and it stays visible to the scan: excluding it from the
        # scan's active-state filter would mean nothing goes back to its box
        # after a platform restart, so the runner's approval wait expires and
        # the user's decision is discarded. It is selected by its own arm and
        # handled by the fence: attached, and returned before any recovery
        # path.
        recover = AsyncMock()
        attach = AsyncMock()
        worker = _build_worker(active_interaction=None, recover_fn=recover)
        worker._attach_parked_runtime = attach
        summary = await self._scan(
            worker,
            _stale_processing_snapshot(conversation_state="WAITING_FOR_INTERACTION"),
        )
        # Two facts, not one zero: the row was attended to, AND nothing was
        # recovered. A single 0 cannot distinguish "attended, not recovered"
        # from "the arm never ran at all".
        self.assertEqual(summary["parked_present"], 1, "presence is not reconciliation")
        self.assertEqual(summary["recovery_settled"], 0)
        self.assertEqual(summary["recovery_incomplete"], 0)
        recover.assert_not_awaited()
        attach.assert_awaited_once()


class RecoveryErrorFrameTests(unittest.IsolatedAsyncioTestCase):
    """``_append_recovery_error_frame`` on the checkpoint mixin."""

    def _mixin_host(self) -> Any:
        from astrabox.core.service.orchestrator.session_kernel.service_mixins import (
            durable_recovery_checkpoint,
        )

        class _Host(durable_recovery_checkpoint.DurableRecoveryCheckpointMixin):  # type: ignore[name-defined]
            pass

        host = _Host.__new__(_Host)
        frames_repo = AsyncMock()
        frames_repo.get_next_session_frame_seq.return_value = 7
        frames_repo.append_frame.return_value = None
        frames_repo.list_frames.return_value = []
        host._session_events_repo = frames_repo
        return host

    async def test_appends_error_terminal_and_returns_proof(self) -> None:
        host = self._mixin_host()
        proof = await host._append_recovery_error_frame(
            session_id=_SID,
            turn_id=_TURN,
            command_id="cmd-1",
            error_text="engine recovery unrecoverable: no anchor",
        )
        self.assertEqual(proof["type"], "error")
        self.assertEqual(proof["turn_id"], _TURN)
        self.assertEqual(proof["frame_seq"], 7)
        appended = host._session_events_repo.append_frame.await_args.args[0]
        self.assertEqual(
            appended["payload"],
            {
                "type": "error",
                "errorText": "engine recovery unrecoverable: no anchor",
            },
        )

    async def test_existing_error_terminal_is_reused(self) -> None:
        host = self._mixin_host()
        existing = {
            "turn_id": _TURN,
            "command_id": "cmd-0",
            "frame_seq": 3,
            "type": "error",
        }
        with patch.object(
            type(host),
            "_find_existing_turn_terminal_frame",
            AsyncMock(return_value=existing),
        ):
            proof = await host._append_recovery_error_frame(
                session_id=_SID,
                turn_id=_TURN,
                command_id="cmd-1",
                error_text="boom",
            )
        self.assertEqual(proof, existing)
        host._session_events_repo.append_frame.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
