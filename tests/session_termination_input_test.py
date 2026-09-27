"""Explicit termination must win over missing compute without disabling recovery."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.admin_service import AdminService
from astrabox.core.service.orchestrator.session_kernel.service import SessionKernelService
from astrabox.seams.sandbox_disposal import SandboxDestruction


_USER = UserContext(user_id="owner")


def _dispatch_service(state, trigger):
    service = SessionKernelService.__new__(SessionKernelService)
    session = {"session_id": "s1", "state": state, "runtime_unavailable": trigger == "unavailable"}
    service._must_get_projection_backed_session = AsyncMock(return_value=session)
    service._get_kernel_session_snapshot = AsyncMock(return_value=None)
    service._bound_runtime_lease_expired = lambda _session: trigger == "lease"
    service._bound_subject_sandbox_gone = AsyncMock(return_value=trigger == "subject")
    service.recover_session = AsyncMock()
    service._await_runtime_subject_rebuild_ready = AsyncMock(return_value={**session, "state": "READY", "runtime_unavailable": False})
    service._dispatch_active_input_queue = AsyncMock(return_value={"accepted": True})
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["TERMINATED", "DELETED"])
@pytest.mark.parametrize("trigger", ["unavailable", "lease", "subject"])
async def test_a_terminal_session_rejects_input_without_rebuilding_compute(state, trigger):
    service = _dispatch_service(state, trigger)
    with pytest.raises(APIError) as error:
        await service.dispatch_turn_input(_USER, "s1", "do not resurrect this conversation")
    assert error.value.status_code == 409
    service.recover_session.assert_not_awaited()
    service._await_runtime_subject_rebuild_ready.assert_not_awaited()
    service._dispatch_active_input_queue.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["unavailable", "lease", "subject"])
async def test_missing_compute_on_an_active_session_still_recovers_and_accepts_input(trigger):
    service = _dispatch_service("READY", trigger)
    assert await service.dispatch_turn_input(_USER, "s1", "continue normally") == {"accepted": True}
    service.recover_session.assert_awaited_once_with(_USER, "s1")
    service._dispatch_active_input_queue.assert_awaited_once()


@pytest.mark.asyncio
async def test_admin_termination_publishes_the_lifecycle_without_erasing_history():
    row = {"session_id": "s1", "state": "READY", "session_kind": "agent_chat", "sandbox_id": "box1"}
    snapshots = SimpleNamespace(apply_channel_update=AsyncMock())
    events = SimpleNamespace(append_event=AsyncMock(return_value={"event_seq": 42}))
    async def update(_session_id, fields):
        row.update(fields)
    sessions = SimpleNamespace(get_session=AsyncMock(side_effect=lambda _id: dict(row)), update_session=AsyncMock(side_effect=update))
    kernel = SessionKernelService.__new__(SessionKernelService)
    kernel._session_snapshots_repo = snapshots
    service = AdminService.__new__(AdminService)
    service._sessions_repo = sessions
    service._session_events_repo = events
    service._session_snapshots_repo = snapshots
    service._session_kernel = kernel
    service._assert_can_manage_session = AsyncMock()
    service._runtime_manager = SimpleNamespace(terminate_runtime=AsyncMock(return_value=SandboxDestruction.confirmed_gone("box1", detail="test destruction")))

    await service.admin_kill_session(_USER, "s1")

    assert row["state"] == "TERMINATED"
    event = events.append_event.await_args.args[0]
    assert event["channel"] == "lifecycle"
    assert event["event_type"] == "session.terminated"
    update = snapshots.apply_channel_update.await_args.kwargs
    assert update["event_seq"] == 42
    assert update["channel"] == "lifecycle"
    assert update["updates"]["session_lifecycle_state"] == "TERMINATED"
    assert update["updates"]["runtime_connectivity_state"] == "LOST"
    assert "conversation_state" not in update["updates"]
    assert "deleted" not in row
