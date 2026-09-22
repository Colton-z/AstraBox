"""A confirmed-dead mid-turn sandbox settles in the selecting reconcile pass.

The runner link can die while the session still names its sandbox. Lightweight
attach then returns no runtime, but that alone is not a death verdict: a control
plane outage has the same local shape. Only a confirmed-terminal lifecycle
probe may turn it into FAILED. That terminal must carry a durable error-frame
proof and stop matching the stale-turn scan immediately.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from astrabox.core.service.orchestrator.session_kernel.service_mixins.durable_recovery_checkpoint import (
    DurableRecoveryMaterializationMixin,
)
from astrabox.core.service.orchestrator.session_kernel.service_mixins import (
    durable_recovery_assistant,
)
from astrabox.core.service.orchestrator.session_kernel.workers.reconcile_worker import (
    ReconcileWorker,
)

_SESSION_ID = "session-dead-sandbox"
_SANDBOX_ID = "sandbox-dead"
_TURN_ID = "turn-mid-stream"
_COMMAND_ID = "command-mid-stream"


class _Snapshots:
    def __init__(self, snapshot: dict[str, Any]) -> None:
        self.doc = dict(snapshot)
        self.writes: list[dict[str, Any]] = []

    async def get_snapshot(self, session_id: str) -> dict[str, Any]:
        assert session_id == _SESSION_ID
        return dict(self.doc)

    async def force_update_fields(
        self,
        session_id: str,
        updates: dict[str, Any],
        *,
        extra_filter: dict[str, Any] | None = None,
    ) -> bool:
        assert session_id == _SESSION_ID
        observed = dict(extra_filter or {})
        if any(self.doc.get(key) != value for key, value in observed.items()):
            return False
        self.writes.append(
            {"updates": dict(updates), "extra_filter": observed}
        )
        self.doc.update(updates)
        return True


class _Sessions:
    def __init__(self, session: dict[str, Any]) -> None:
        self.doc = dict(session)
        self.updates: list[dict[str, Any]] = []

    async def get_session(self, session_id: str) -> dict[str, Any]:
        assert session_id == _SESSION_ID
        return dict(self.doc)

    async def update_session(
        self,
        session_id: str,
        updates: dict[str, Any],
    ) -> bool:
        assert session_id == _SESSION_ID
        self.updates.append(dict(updates))
        self.doc.update(updates)
        return True


class _Journal:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def try_claim_event(
        self,
        event: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        # Deliberately behind the snapshot watermark. The adf0462 terminal path
        # preserves that newer same-turn watermark instead of treating it as
        # lost ownership.
        stored = {**event, "event_seq": 7}
        self.events.append(stored)
        return stored, True


class _Frames:
    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []

    async def list_frames(
        self,
        session_id: str,
        *,
        command_id: str | None = None,
        turn_id: str | None = None,
        after_seq: int = -1,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        assert session_id == _SESSION_ID
        rows = [
            dict(doc)
            for doc in self.docs
            if int(doc.get("frame_seq") or -1) > after_seq
            and (command_id is None or doc.get("command_id") == command_id)
            and (turn_id is None or doc.get("turn_id") == turn_id)
        ]
        return rows[:limit]

    async def get_next_session_frame_seq(self, session_id: str) -> int:
        assert session_id == _SESSION_ID
        return max((int(doc["frame_seq"]) for doc in self.docs), default=20) + 1

    async def append_frame(self, doc: dict[str, Any]) -> None:
        self.docs.append(dict(doc))


class _RuntimeManager:
    def __init__(self, probe_status: str) -> None:
        self.probe_status = probe_status
        self.probes: list[str] = []

    def get_runtime(
        self,
        session_id: str,
        *,
        sandbox_id: str | None = None,
    ) -> None:
        assert session_id == _SESSION_ID
        assert sandbox_id == _SANDBOX_ID
        return None

    async def get_sandbox_lifecycle_probe(self, sandbox_id: str) -> Any:
        self.probes.append(sandbox_id)
        return SimpleNamespace(
            probe_status=self.probe_status,
            sandbox_state=None,
            error_text=(
                "control plane no longer knows this sandbox"
                if self.probe_status == "NOT_FOUND"
                else "control plane unreachable"
            ),
        )

    @staticmethod
    def _is_terminal_sandbox_lifecycle_probe(probe: Any) -> bool:
        return str(getattr(probe, "probe_status", "") or "") == "NOT_FOUND"


class _RecoveryHost(DurableRecoveryMaterializationMixin):
    def __init__(
        self,
        *,
        snapshots: _Snapshots,
        sessions: _Sessions,
        journal: _Journal,
        frames: _Frames,
        runtime_manager: _RuntimeManager,
    ) -> None:
        self._session_snapshots_repo = snapshots
        self._sessions_repo = sessions
        journal.get_next_session_frame_seq = frames.get_next_session_frame_seq
        journal.append_frame = frames.append_frame
        journal.list_frames = frames.list_frames
        self._session_events_repo = journal
        self._runtime_manager = runtime_manager
        self._turn_service = SimpleNamespace(
            _ensure_runtime_lightweight_for_session=AsyncMock(return_value=None)
        )

    async def _recover_initiating_user_message_for_turn(self, **_: Any) -> None:
        return None


def _snapshot() -> dict[str, Any]:
    return {
        "session_id": _SESSION_ID,
        "session_lifecycle_state": "READY",
        "conversation_state": "PROCESSING",
        "conversation_event_seq_applied": 19,
        "current_turn_id": _TURN_ID,
        "current_turn_worker_command_id": _COMMAND_ID,
        "current_turn_remote_anchor": {
            "sandbox_turn_id": 4,
            "last_sandbox_seq": 12,
        },
        "current_turn_engine_anchor": {
            "engine_kind": "assistant",
            "engine_turn_id": "engine-turn-mid-stream",
            "engine_sequence_number": 5,
        },
        "last_turn_terminal_frame": None,
        "worker_heartbeat_at": "2020-01-01T00:00:00+00:00",
        "updated_at": "2020-01-01T00:00:00+00:00",
    }


def _scene(
    probe_status: str,
) -> tuple[ReconcileWorker, _RecoveryHost, _Snapshots, _Journal, _Frames, _RuntimeManager]:
    snapshot = _snapshot()
    snapshots = _Snapshots(snapshot)
    sessions = _Sessions(
        {
            "session_id": _SESSION_ID,
            "sandbox_id": _SANDBOX_ID,
            "session_kind": "assistant_chat",
            "engine_kind": "assistant",
            "runtime_unavailable": False,
            "user_id": "user-1",
        }
    )
    journal = _Journal()
    frames = _Frames()
    runtime_manager = _RuntimeManager(probe_status)
    host = _RecoveryHost(
        snapshots=snapshots,
        sessions=sessions,
        journal=journal,
        frames=frames,
        runtime_manager=runtime_manager,
    )
    interactions = AsyncMock()
    interactions.get_active_interaction = AsyncMock(return_value=None)
    worker = ReconcileWorker(
        session_snapshots_repo=snapshots,
        sessions_repo=sessions,
        session_events_repo=journal,
        interaction_snapshots_repo=interactions,
        resolve_sandbox_endpoint_fn=AsyncMock(),
        recover_engine_session_fn=host._recover_engine_via_anchor,
    )
    worker._find_stuck_sessions = AsyncMock(return_value=[snapshot])  # type: ignore[method-assign]
    return worker, host, snapshots, journal, frames, runtime_manager


class DeadSandboxAnchorRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmed_gone_settles_failed_with_proof_in_one_scan(self) -> None:
        worker, host, snapshots, journal, frames, runtime_manager = _scene("NOT_FOUND")

        with patch.object(
            durable_recovery_assistant,
            "get_engine_adapter",
            return_value=SimpleNamespace(),
        ):
            summary = await worker.scan_once()

        self.assertEqual(summary["settled_dead_sandbox"], 1)
        self.assertEqual(summary["recovery_incomplete"], 0)
        self.assertEqual(summary["recovery_settled"], 0)
        self.assertEqual(runtime_manager.probes, [_SANDBOX_ID])
        host._turn_service._ensure_runtime_lightweight_for_session.assert_awaited_once()

        self.assertEqual(len(journal.events), 1)
        self.assertEqual(journal.events[0]["event_type"], "turn.failed")
        self.assertEqual(len(frames.docs), 1)
        self.assertEqual(frames.docs[0]["payload"]["type"], "error")

        settled = snapshots.doc
        self.assertEqual(settled["conversation_state"], "IDLE")
        self.assertIsNone(settled["current_turn_id"])
        self.assertEqual(settled["last_turn_id"], _TURN_ID)
        self.assertEqual(settled["last_turn_status"], "FAILED")
        self.assertIn("control-plane-confirmed gone", settled["last_turn_error"])
        self.assertEqual(
            settled["last_turn_terminal_frame"],
            {
                "turn_id": _TURN_ID,
                "command_id": _COMMAND_ID,
                "frame_seq": frames.docs[0]["frame_seq"],
                "type": "error",
            },
        )
        self.assertIsNone(settled["turn_recovery_phase"])
        self.assertIsNone(settled["current_turn_remote_anchor"])
        self.assertIsNone(settled["current_turn_engine_anchor"])
        self.assertEqual(
            settled["conversation_event_seq_applied"],
            19,
            "a same-turn terminal must preserve the newer projection watermark",
        )
        self.assertEqual(
            snapshots.writes[0]["extra_filter"],
            {
                "current_turn_id": _TURN_ID,
                "conversation_event_seq_applied": 19,
            },
        )
        self.assertFalse(
            worker._should_reconcile_snapshot(
                settled,
                stale_threshold=datetime.now(timezone.utc),
            ),
            "the terminal row must leave the scan's candidate set",
        )

    async def test_unconfirmed_probe_keeps_the_turn_recoverable(self) -> None:
        worker, _host, snapshots, journal, frames, runtime_manager = _scene(
            "PROBE_FAILED"
        )

        with patch.object(
            durable_recovery_assistant,
            "get_engine_adapter",
            return_value=SimpleNamespace(),
        ):
            summary = await worker.scan_once()

        self.assertEqual(summary["settled_dead_sandbox"], 0)
        self.assertEqual(summary["recovery_incomplete"], 1)
        self.assertEqual(runtime_manager.probes, [_SANDBOX_ID])
        self.assertEqual(snapshots.doc["conversation_state"], "PROCESSING")
        self.assertEqual(snapshots.writes, [])
        self.assertEqual(journal.events, [])
        self.assertEqual(frames.docs, [])


if __name__ == "__main__":
    unittest.main()
