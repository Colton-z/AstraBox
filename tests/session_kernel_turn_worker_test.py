from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from astrabox.core.service.orchestrator.session_kernel.workers.turn.worker import (
    TurnWorker,
)
class _FakeSessionsRepo:
    def __init__(self, session: dict[str, Any]) -> None:
        self.session = dict(session)
        self.update_calls: list[dict[str, Any]] = []

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        if self.session.get("session_id") != session_id:
            return None
        return dict(self.session)

    async def update_session(self, session_id: str, updates: dict[str, Any]) -> None:
        _ = session_id
        self.update_calls.append(dict(updates))
        self.session.update(updates)


class _FakeSessionEventsRepo:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def append_event(self, event: dict[str, Any]) -> dict[str, Any]:
        stored = {**dict(event), "event_seq": len(self.events) + 1}
        self.events.append(stored)
        return stored


class _FakeSessionSnapshotsRepo:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []
        self.force_updates: list[dict[str, Any]] = []

    async def get_snapshot(self, session_id: str) -> dict[str, Any]:
        _ = session_id
        return {
            "current_turn_remote_anchor": {
                "sandbox_turn_id": 7,
                "last_sandbox_seq": 11,
            }
        }

    async def apply_channel_update(
        self,
        session_id: str,
        *,
        channel: str,
        event_seq: int,
        updates: dict[str, Any],
        extra_filter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        stored = {
            "session_id": session_id,
            "channel": channel,
            "event_seq": event_seq,
            "updates": dict(updates),
            "extra_filter": dict(extra_filter) if isinstance(extra_filter, dict) else None,
        }
        self.updates.append(stored)
        return stored

    async def force_update_fields(
        self,
        session_id: str,
        updates: dict[str, Any],
        *,
        extra_filter: dict[str, Any] | None = None,
    ) -> bool:
        stored = {
            "session_id": session_id,
            "updates": dict(updates),
            "extra_filter": dict(extra_filter) if isinstance(extra_filter, dict) else None,
        }
        self.force_updates.append(stored)
        return True


class _PreprojectedSessionSnapshotsRepo(_FakeSessionSnapshotsRepo):
    async def apply_channel_update(
        self,
        session_id: str,
        *,
        channel: str,
        event_seq: int,
        updates: dict[str, Any],
        extra_filter: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        await super().apply_channel_update(
            session_id,
            channel=channel,
            event_seq=event_seq,
            updates=updates,
            extra_filter=extra_filter,
        )
        return None


class _FakeTurnService:
    def __init__(self, order: list[str]) -> None:
        self._order = order

    async def _load_turn_recovery_checkpoint(self, **kwargs: Any) -> tuple[Any, ...]:
        _ = kwargs
        return (None, None, None, [], [])

    async def set_engine_permission_mode(
        self, _session: dict[str, Any], _mode: str
    ) -> bool:
        self._order.append("set")
        return True


class TurnWorkerPermissionModeTests(unittest.IsolatedAsyncioTestCase):
    def _build_worker(
        self,
        *,
        session: dict[str, Any],
        order: list[str],
    ) -> tuple[TurnWorker, Any, _FakeSessionsRepo, _FakeSessionSnapshotsRepo]:
        sessions_repo = _FakeSessionsRepo(session)
        runtime_manager = SimpleNamespace(
            set_permission_mode=AsyncMock(side_effect=lambda *args, **kwargs: order.append("set") or True),
            respond_to_interaction=AsyncMock(side_effect=lambda *args, **kwargs: order.append("respond")),
            get_runtime=lambda *args, **kwargs: None,
            _resolve_validated_sandbox_endpoint=AsyncMock(return_value="http://sidecar.local"),
            get_pending_interaction_view=AsyncMock(return_value=None),
        )
        snapshots_repo = _FakeSessionSnapshotsRepo()
        worker = TurnWorker(
            worker_id="test-worker",
            sessions_repo=sessions_repo,
            turn_service=_FakeTurnService(order),
            runtime_manager=runtime_manager,
            broker=SimpleNamespace(),
            session_events_repo=_FakeSessionEventsRepo(),
            session_snapshots_repo=snapshots_repo,
            interaction_snapshots_repo=SimpleNamespace(),
            transcript_entries_repo=SimpleNamespace(),
            bridge_event_stall_timeout_s=0.01,
            worker_heartbeat_interval_s=3600.0,
            live_frame_retry_window_s=0.0,
            live_frame_retry_delay_s=0.01,
            requested_projection_retry_window_s=0.0,
            requested_projection_retry_delay_s=0.01,
            terminal_settle_retry_window_s=0.0,
            terminal_settle_retry_delay_s=0.01,
        )
        return worker, runtime_manager, sessions_repo, snapshots_repo

    async def test_answer_interaction_command_accepted_sets_worker_owner_and_heartbeat(self) -> None:
        session_id = "s-answer-command-owner"
        worker, _, _, snapshots_repo = self._build_worker(
            session={
                "session_id": session_id,
                "sandbox_id": "sandbox-1",
                "sandbox_endpoint": "http://sidecar.local",
                "permission_mode": "default",
            },
            order=[],
        )
        worker._interaction_snapshots_repo = SimpleNamespace(
            get_active_interaction=AsyncMock(
                return_value={"interaction_id": "pi-1", "turn_id": "turn-1"}
            )
        )

        await worker._project_command_accepted(
            session_id=session_id,
            turn_id="turn-1",
            command_event={
                "causation_id": "cmd-answer",
                "event_seq": 42,
                "payload": {"command_type": "AnswerInteraction"},
            },
            command_type="AnswerInteraction",
        )

        update = snapshots_repo.updates[-1]["updates"]
        self.assertEqual(update["conversation_state"], "PROCESSING")
        self.assertEqual(update["current_turn_id"], "turn-1")
        self.assertEqual(update["active_interaction_id"], "pi-1")
        self.assertEqual(update["current_turn_worker_command_id"], "cmd-answer")
        self.assertTrue(str(update.get("worker_heartbeat_at") or "").startswith("20"))

    async def test_start_turn_uses_the_accepted_event_as_its_user_message(self) -> None:
        session_id = "s-start-preprojected"
        worker, _, _, snapshots_repo = self._build_worker(
            session={
                "session_id": session_id,
                "sandbox_id": "sandbox-1",
                "sandbox_endpoint": "http://sidecar.local",
                "permission_mode": "default",
            },
            order=[],
        )
        await worker._project_command_accepted(
            session_id=session_id,
            turn_id="turn-1",
            command_event={
                "causation_id": "cmd-start",
                "event_seq": 43,
                "payload": {
                    "command_type": "StartTurn",
                    "content": "hello",
                },
            },
            command_type="StartTurn",
        )

        self.assertEqual(worker._session_events_repo.events, [])
        update = snapshots_repo.updates[-1]["updates"]
        self.assertEqual(update["conversation_state"], "PROCESSING")
        self.assertEqual(update["current_turn_id"], "turn-1")
        self.assertEqual(update["current_turn_worker_command_id"], "cmd-start")

    async def test_native_fifo_start_waits_for_sdk_consumption_before_user_projection(self) -> None:
        session_id = "s-start-native-fifo"
        worker, _, _, snapshots_repo = self._build_worker(
            session={
                "session_id": session_id,
                "sandbox_id": "sandbox-1",
                "sandbox_endpoint": "http://sidecar.local",
                "permission_mode": "default",
            },
            order=[],
        )
        await worker._project_command_accepted(
            session_id=session_id,
            turn_id="turn-1",
            command_event={
                "causation_id": "cmd-start",
                "event_seq": 43,
                "payload": {
                    "command_type": "StartTurn",
                    "content": "queued root",
                    "input_id": "00000000-0000-0000-0000-000000000001",
                },
            },
            command_type="StartTurn",
        )

        self.assertEqual(worker._session_events_repo.events, [])
        update = snapshots_repo.updates[-1]["updates"]
        self.assertEqual(update["conversation_state"], "PROCESSING")

    async def test_readmit_after_settle_admits_via_empty_slot_not_watermark(self) -> None:
        # The handoff continuation's command event predates the settle events,
        # so a watermark-guarded apply can never land; admission must be the
        # empty-slot CAS instead.
        session_id = "s-handoff-readmit"
        worker, _, _, snapshots_repo = self._build_worker(
            session={
                "session_id": session_id,
                "sandbox_id": "sandbox-1",
                "sandbox_endpoint": "http://sidecar.local",
                "permission_mode": "default",
            },
            order=[],
        )

        await worker._project_command_accepted(
            session_id=session_id,
            turn_id="handoff-turn-1",
            command_event={
                "causation_id": "cmd-stranded",
                "event_seq": 40,
                "payload": {"command_type": "SubmitInput", "content": "queued"},
            },
            command_type="SubmitInput",
            readmit_after_settle=True,
        )

        self.assertEqual(snapshots_repo.updates, [])
        admission = snapshots_repo.force_updates[-1]
        self.assertEqual(admission["extra_filter"], {"current_turn_id": None})
        update = admission["updates"]
        self.assertEqual(update["conversation_state"], "PROCESSING")
        self.assertEqual(update["current_turn_id"], "handoff-turn-1")
        self.assertEqual(update["current_turn_worker_command_id"], "cmd-stranded")
        self.assertTrue(str(update.get("worker_heartbeat_at") or "").startswith("20"))

    async def test_readmit_that_loses_the_slot_does_not_raise(self) -> None:
        session_id = "s-handoff-lost-slot"
        worker, _, _, snapshots_repo = self._build_worker(
            session={
                "session_id": session_id,
                "sandbox_id": "sandbox-1",
                "sandbox_endpoint": "http://sidecar.local",
                "permission_mode": "default",
            },
            order=[],
        )

        async def deny_admission(*args: Any, **kwargs: Any) -> bool:
            _ = args, kwargs
            return False

        snapshots_repo.force_update_fields = deny_admission  # type: ignore[method-assign]

        await worker._project_command_accepted(
            session_id=session_id,
            turn_id="handoff-turn-2",
            command_event={
                "causation_id": "cmd-stranded-2",
                "event_seq": 40,
                "payload": {"command_type": "SubmitInput", "content": "queued"},
            },
            command_type="SubmitInput",
            readmit_after_settle=True,
        )

        self.assertEqual(snapshots_repo.updates, [])

    async def test_preprojected_start_turn_refreshes_same_command_heartbeat(self) -> None:
        session_id = "s-start-preprojected-heartbeat"
        worker, _, _, snapshots_repo = self._build_worker(
            session={
                "session_id": session_id,
                "sandbox_id": "sandbox-1",
                "sandbox_endpoint": "http://sidecar.local",
                "permission_mode": "default",
            },
            order=[],
        )
        preprojected_snapshots = _PreprojectedSessionSnapshotsRepo()
        worker._session_snapshots_repo = preprojected_snapshots
        await worker._project_command_accepted(
            session_id=session_id,
            turn_id="turn-1",
            command_event={
                "causation_id": "cmd-start",
                "event_seq": 43,
                "payload": {
                    "command_type": "StartTurn",
                    "content": "hello",
                },
            },
            command_type="StartTurn",
        )

        self.assertEqual(worker._session_events_repo.events, [])
        self.assertEqual(len(preprojected_snapshots.force_updates), 1)
        force = preprojected_snapshots.force_updates[0]
        self.assertEqual(
            force["extra_filter"],
            {
                "current_turn_id": "turn-1",
                "current_turn_worker_command_id": "cmd-start",
            },
        )
        self.assertEqual(force["updates"]["current_turn_worker_command_id"], "cmd-start")
        self.assertTrue(str(force["updates"].get("worker_heartbeat_at") or "").startswith("20"))


if __name__ == "__main__":
    unittest.main()
