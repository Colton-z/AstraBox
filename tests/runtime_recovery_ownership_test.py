"""Recovery CAS, stale completion, and optional-volume pool admission contracts."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.engine.provisioning import _claim_conversation_pool_box
from astrabox.core.service.orchestrator.runtime_manager import RemoteAgentRuntimeManager
from astrabox.core.service.orchestrator.session_public_projection import project_owner_session
from astrabox.core.service.orchestrator.session_kernel.workers.lifecycle.recover import (
    _RecoverSessionMixin,
)
from astrabox.core.service.orchestrator.session_kernel.workers.lifecycle.recovery_ownership import (
    RecoveryOwnership,
)


class Sessions:
    def __init__(self):
        self.row = {"session_id": "s", "state": "READY", "sandbox_generation": "old"}

    async def get_session(self, _session_id):
        return dict(self.row)

    async def compare_and_update_session(self, _session_id, *, expected, updates):
        if any(self.row.get(key) != value for key, value in expected.items()):
            return False
        self.row.update(updates)
        return True


class Worker(_RecoverSessionMixin):
    def __init__(self, sessions, started, release):
        self._sessions_repo = sessions
        self._session_service = SimpleNamespace(_sanitize_session=dict)
        self._runtime_manager = SimpleNamespace(get_runtime=Mock(return_value=None))
        self._session_snapshots_repo = SimpleNamespace(get_snapshot=AsyncMock(return_value=None))
        self.started = started
        self.release = release
        self.creates = 0

    async def _derive_effective_session_state_for_recover(self, *, session_id, session):
        return session["state"]

    async def _recover_owned_session(self, *, ownership, **_kwargs):
        self.creates += 1
        self.started.set()
        await self.release.wait()
        await ownership.update({"state": "READY", "sandbox_id": "winner"})
        return {"status": "reattached", "sandbox_id": "winner"}


async def recover(worker, session, command):
    return await worker._recover_session_direct(
        user=UserContext(user_id="u"),
        session=session,
        session_id="s",
        command_event={"causation_id": command},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("follower_projection", ["READY", "CREATING"])
async def test_two_workers_admitted_from_the_same_row_enter_only_one_recovery(follower_projection):
    sessions = Sessions()
    initial = dict(sessions.row)
    started, release = asyncio.Event(), asyncio.Event()
    winner = Worker(sessions, started, release)
    follower = Worker(sessions, asyncio.Event(), asyncio.Event())
    follower._derive_effective_session_state_for_recover = AsyncMock(
        return_value=follower_projection
    )
    pending = asyncio.create_task(recover(winner, dict(initial), "winner-command"))
    await started.wait()
    result = await recover(follower, dict(initial), "follower-command")
    assert result["status"] == "recovery-in-progress"
    assert follower.creates == 0
    assert sessions.row["_runtime_recovery_owner"] == "winner-command"
    assert sessions.row["state"] == "CREATING"
    release.set()
    await pending
    assert winner.creates == 1
    assert sessions.row["sandbox_id"] == "winner"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["READY", "TERMINATED"])
async def test_stale_request_does_not_describe_a_settled_successor_as_running(state):
    sessions = Sessions()
    initial = dict(sessions.row)
    sessions.row.update({"state": state, "_runtime_recovery_owner": "new"})
    worker = Worker(sessions, asyncio.Event(), asyncio.Event())
    with pytest.raises(APIError) as error:
        await recover(worker, initial, "old")
    assert error.value.code == "RUNTIME_RECOVERY_SUPERSEDED"
    assert worker.creates == 0
    assert sessions.row["state"] == state


@pytest.mark.asyncio
async def test_replayed_settle_accepts_only_the_same_owner_and_complete_write():
    sessions = Sessions()
    sessions.row.update({"state": "CREATING", "_runtime_recovery_owner": "old"})
    ownership = RecoveryOwnership(sessions, "s", "old")
    updates = {"state": "READY", "sandbox_id": "box", "startup_progress": None}
    assert await ownership.update(updates)
    assert await ownership.update(updates)  # committed response/readback was lost
    sessions.row.update({"state": "CREATING", "_runtime_recovery_owner": "new"})
    with pytest.raises(APIError) as error:
        await ownership.update(updates)
    assert error.value.code == "RUNTIME_RECOVERY_SUPERSEDED"
    assert sessions.row["_runtime_recovery_owner"] == "new"


@pytest.mark.asyncio
async def test_bootstrap_termination_fences_old_progress_and_allows_a_new_recovery():
    sessions = Sessions()
    sessions.row.update(
        {"state": "TERMINATED", "_runtime_recovery_owner": "old", "startup_progress": None}
    )
    ownership = RecoveryOwnership(sessions, "s", "old")
    with pytest.raises(APIError):
        await ownership.require_current()
    with pytest.raises(APIError):
        await ownership.update({"startup_progress": None})
    released = asyncio.Event()
    released.set()
    worker = Worker(sessions, asyncio.Event(), released)
    await recover(worker, dict(sessions.row), "new")
    assert worker.creates == 1
    assert sessions.row["state"] == "READY"
    assert sessions.row["_runtime_recovery_owner"] == "new"


@pytest.mark.asyncio
async def test_lost_recovery_cannot_publish_a_local_runtime_or_destroy_the_successor():
    sessions = Sessions()
    sessions.row.update({"state": "CREATING", "_runtime_recovery_owner": "old"})
    ownership = RecoveryOwnership(sessions, "s", "old")
    runtime = SimpleNamespace(sandbox_id="old-provisional-box")
    manager = RemoteAgentRuntimeManager.__new__(RemoteAgentRuntimeManager)
    manager._raise_if_quiesced = Mock()
    manager._validate_runtime_start_plan = Mock()
    manager._require_runtime_engine_manifest = Mock()
    manager._drop_runtime_for_unusable_client = AsyncMock(return_value=None)
    manager._disconnect_runtime_client = AsyncMock()
    manager._rollback_runtime_start = AsyncMock()
    manager._runtimes = {}
    manager._session_locks = {}

    async def start(**_kwargs):
        sessions.row.update({"_runtime_recovery_owner": "new", "sandbox_id": "successor"})
        return runtime

    manager._start_runtime = start
    with pytest.raises(APIError) as error:
        await manager.create_runtime(
            "s",
            SimpleNamespace(),
            assignment_id="old-command",
            workspace_plan=SimpleNamespace(),
            startup_guard=ownership.require_current,
        )
    assert error.value.code == "RUNTIME_RECOVERY_SUPERSEDED"
    assert manager._runtimes == {}
    assert sessions.row["sandbox_id"] == "successor"
    manager._disconnect_runtime_client.assert_awaited_once_with(
        runtime,
        session_id="s",
        reason="startup owner changed",
    )
    manager._rollback_runtime_start.assert_not_awaited()


def test_recovery_owner_is_not_a_public_session_field():
    assert project_owner_session(
        {
            "session_id": "s",
            "_runtime_recovery_owner": "private-command",
            "_retained_startup_allocations": [{"allocation": {"sandbox_id": "private-box"}}],
        }
    ) == {
        "session_id": "s",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("volume", ["", "durable-pvc"])
async def test_recovery_borrows_replacement_compute_without_replacing_workspace_identity(volume):
    acquired = SimpleNamespace(
        sandbox=object(),
        sandbox_id="warm",
        workload_id="slot",
        workspace_id="unused-pool-workspace",
    )
    repo = SimpleNamespace(
        get_session=AsyncMock(return_value={"workspace_id": "original-workspace"})
    )
    borrow = AsyncMock(return_value=acquired)
    with (
        patch(
            "astrabox.core.service.orchestrator.engine.provisioning.SessionRepository",
            return_value=repo,
        ),
        patch(
            "astrabox.core.service.orchestrator.engine.provisioning.load_astrabox_settings",
            return_value=SimpleNamespace(sandbox_workspace_volume=volume),
        ),
        patch(
            "astrabox.core.service.orchestrator.agent.client_pool.acquire_agent_client_pool", borrow
        ),
    ):
        result = await _claim_conversation_pool_box(
            object(),
            session_id="s",
            assignment_id="command",
            template=object(),
            backend_adapter=object(),
            workspace_plan=SimpleNamespace(resume_engine_session_key="native-session"),
            recovered_assignment=None,
        )
    assert result is not None
    assert result.sandbox is acquired.sandbox
    assert result.workspace_id == "original-workspace"
    borrow.assert_awaited_once()


@pytest.mark.asyncio
async def test_late_recovery_reset_cannot_clear_a_successors_turn_or_interaction():
    observed = {
        "updated_at": "old",
        "current_turn_id": "old-turn",
        "active_interaction_id": "old-interaction",
        "conversation_state": "FAILED",
    }
    current = {
        **observed,
        "updated_at": "new",
        "current_turn_id": "new-turn",
        "active_interaction_id": "new-interaction",
        "conversation_state": "RUNNING",
    }

    async def update(_session_id, updates, *, extra_filter):
        if any(current.get(key) != value for key, value in extra_filter.items()):
            return False
        current.update(updates)
        return True

    worker = _RecoverSessionMixin()
    worker._session_snapshots_repo = SimpleNamespace(
        force_update_fields=AsyncMock(side_effect=update)
    )
    worker._interaction_snapshots_repo = SimpleNamespace(deactivate_active_for_turn=AsyncMock())
    with pytest.raises(APIError) as error:
        await worker._reset_conversation_projection_for_recreate_recovery("s", observed)
    assert error.value.code == "RUNTIME_RECOVERY_SUPERSEDED"
    assert current["current_turn_id"] == "new-turn"
    assert current["active_interaction_id"] == "new-interaction"
    assert current["conversation_state"] == "RUNNING"
    worker._interaction_snapshots_repo.deactivate_active_for_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_reset_closes_only_the_observed_turn_after_snapshot_commit():
    worker = _RecoverSessionMixin()
    worker._session_snapshots_repo = SimpleNamespace(
        force_update_fields=AsyncMock(return_value=True)
    )
    worker._interaction_snapshots_repo = SimpleNamespace(deactivate_active_for_turn=AsyncMock())
    observed = {"updated_at": "old", "current_turn_id": "old-turn", "conversation_state": "FAILED"}
    await worker._reset_conversation_projection_for_recreate_recovery("s", observed)
    worker._interaction_snapshots_repo.deactivate_active_for_turn.assert_awaited_once_with(
        "s", "old-turn"
    )
    assert (
        worker._session_snapshots_repo.force_update_fields.await_args.kwargs["extra_filter"][
            "updated_at"
        ]
        == "old"
    )


@pytest.mark.asyncio
async def test_recovery_rollback_closes_only_its_exact_isolated_sessions():
    from astrabox.seams.sandbox import SandboxAllocation

    sessions = Sessions()
    sessions.row.update({"state": "CREATING", "_runtime_recovery_owner": "old"})
    ownership = RecoveryOwnership(sessions, "s", "old")
    allocation = SandboxAllocation(
        sandbox_id="shared",
        sandbox_backend="test",
        scope="isolated_sessions",
        isolated_session_ids=("old-isolated",),
    )
    successor = SandboxAllocation(
        sandbox_id="shared",
        sandbox_backend="test",
        scope="isolated_sessions",
        isolated_session_ids=("new-isolated",),
    )
    ownership.allocations.append(allocation)
    manager = RemoteAgentRuntimeManager.__new__(RemoteAgentRuntimeManager)
    manager._runtimes = {"s": SimpleNamespace(sandbox_id="shared")}
    manager._pending_startup_allocations = {"s": [allocation, successor]}
    manager._allocation_box_is_gone = AsyncMock(return_value=False)
    manager._finish_released_startup_allocation = AsyncMock(return_value=(True, ""))
    manager._load_startup_allocation = AsyncMock(return_value=successor)
    manager.terminate_runtime = AsyncMock()
    provider = SimpleNamespace(close_isolated_session=AsyncMock())
    with (
        ownership.bind(),
        patch(
            "astrabox.core.service.orchestrator.runtime_manager.sandbox_for_name",
            return_value=provider,
        ),
    ):
        # Simulate a new owner after rollback was entered. Exact allocation
        # identity, not an earlier owner read, must protect the shared box.
        sessions.row["_runtime_recovery_owner"] = "new"
        result = await manager.cleanup_startup_allocation("s")
    assert result.released
    assert manager._runtimes["s"].sandbox_id == "shared"
    provider.close_isolated_session.assert_awaited_once_with("shared", "old-isolated")
    manager.terminate_runtime.assert_not_awaited()
    manager._load_startup_allocation.assert_not_awaited()
    manager._finish_released_startup_allocation.assert_awaited_once_with(
        "s", allocation, clear_durable_record=True
    )


@pytest.mark.asyncio
async def test_startup_allocation_cas_rejects_owner_change_during_database_write():
    from astrabox.persistence.repository.session_repository import SessionRepository

    sessions = Sessions()
    sessions.row.update(
        {"state": "CREATING", "_runtime_recovery_owner": "old", "startup_allocation": None}
    )
    repo = SessionRepository()
    repo.get_session = sessions.get_session
    original_cas = sessions.compare_and_update_session

    async def replace_owner_then_write(session_id, *, expected, updates):
        sessions.row.update(
            {"_runtime_recovery_owner": "new", "startup_allocation": {"sandbox_id": "new-box"}}
        )
        return await original_cas(session_id, expected=expected, updates=updates)

    repo.compare_and_update_session = replace_owner_then_write
    with pytest.raises(RuntimeError, match="lost its Session fence"):
        await repo.record_startup_allocation(
            "s",
            {"sandbox_id": "old-box"},
            owner_expected={"state": "CREATING", "_runtime_recovery_owner": "old"},
        )
    assert sessions.row["startup_allocation"] == {"sandbox_id": "new-box"}


@pytest.mark.asyncio
async def test_late_attachment_disconnects_only_its_provisional_client():
    sessions = Sessions()
    sessions.row.update({"state": "CREATING", "_runtime_recovery_owner": "old"})
    ownership = RecoveryOwnership(sessions, "s", "old")
    manager = RemoteAgentRuntimeManager.__new__(RemoteAgentRuntimeManager)
    manager._raise_if_quiesced = Mock()
    manager._runtimes = {}
    manager._session_locks = {}
    manager._drop_runtime_for_sandbox_mismatch = AsyncMock(return_value=None)
    manager._drop_runtime_for_unusable_client = AsyncMock(return_value=None)
    manager._validate_runtime_attach_plan = Mock()
    manager._require_runtime_engine_manifest = Mock()
    manager._disconnect_runtime_client = AsyncMock()
    old = SimpleNamespace(sandbox_id="old-box")
    new = SimpleNamespace(sandbox_id="new-box")

    async def attached(*_args, **_kwargs):
        sessions.row.update({"_runtime_recovery_owner": "new", "sandbox_id": "new-box"})
        manager._runtimes["s"] = new
        return old

    with (
        ownership.bind(),
        patch(
            "astrabox.core.service.orchestrator.engine.startup.attach_platform_runtime",
            side_effect=attached,
        ),
        patch(
            "astrabox.core.service.orchestrator.runtime_manager.get_engine_adapter",
            return_value=object(),
        ),
    ):
        with pytest.raises(APIError) as error:
            await manager.ensure_runtime(
                "s",
                SimpleNamespace(),
                sandbox_id="old-box",
                session_kind="agent_chat",
                workspace_plan=SimpleNamespace(engine_kind="test"),
                startup_guard=ownership.require_current,
            )
    assert error.value.code == "RUNTIME_RECOVERY_SUPERSEDED"
    assert manager._runtimes["s"] is new
    manager._disconnect_runtime_client.assert_awaited_once_with(
        old, session_id="s", reason="attachment owner changed"
    )


@pytest.mark.asyncio
async def test_recovery_evict_cannot_remove_a_runtime_that_replaced_its_observation():
    manager = RemoteAgentRuntimeManager.__new__(RemoteAgentRuntimeManager)
    old, new = SimpleNamespace(), SimpleNamespace()
    manager._runtimes = {"s": new}
    manager._session_locks = {}
    manager._disconnect_evicted_runtime = AsyncMock()
    await manager.evict_runtime_if_current("s", old)
    assert manager._runtimes["s"] is new
    manager._disconnect_evicted_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_created_allocation_rejected_by_new_owner_keeps_its_durable_exact_scope():
    from astrabox.seams.sandbox import SandboxAllocation

    sessions = Sessions()
    sessions.row.update(
        {
            "state": "CREATING",
            "_runtime_recovery_owner": "new",
            "sandbox_generation": "new-generation",
            "startup_allocation": {"sandbox_id": "successor-box"},
        }
    )
    retained = []

    async def retain(_session_id, record):
        if record not in retained:
            retained.append(record)

    sessions.retain_startup_allocation = retain
    ownership = RecoveryOwnership(
        sessions,
        "s",
        "old",
        sandbox_generation="old-generation",
        assignment_id="old-startup-command",
    )
    manager = RemoteAgentRuntimeManager.__new__(RemoteAgentRuntimeManager)
    manager._sessions_repo = sessions
    manager._pending_startup_allocations = {}
    manager._sandbox_backend_cache = {}
    allocation = SandboxAllocation(
        sandbox_id="shared",
        sandbox_backend="test",
        scope="isolated_sessions",
        isolated_session_ids=("old-isolated", "old-terminal"),
    )
    with ownership.bind(), pytest.raises(APIError) as error:
        await manager.record_startup_allocation("s", allocation)
    assert error.value.code == "RUNTIME_RECOVERY_SUPERSEDED"
    assert sessions.row["startup_allocation"] == {"sandbox_id": "successor-box"}
    assert error.value.data["startup_allocations"] == [allocation.as_record()]
    assert retained == [
        {
            "allocation": allocation.as_record(),
            "sandbox_generation": "old-generation",
            "assignment_id": "old-startup-command",
        }
    ]
    assert manager._pending_startup_allocations["s"] == [allocation]


@pytest.mark.asyncio
async def test_retained_shared_receipt_never_authorizes_destroying_healthy_successor_compute():
    from datetime import datetime, timezone
    from astrabox.seams.sandbox import SandboxAllocation

    allocation = SandboxAllocation(
        sandbox_id="shared",
        sandbox_backend="test",
        scope="isolated_sessions",
        isolated_session_ids=("old-isolated",),
    )
    receipt = {
        "allocation": allocation.as_record(),
        "sandbox_generation": "old",
        "assignment_id": "old-command",
    }
    current = {
        "session_id": "s",
        "state": "READY",
        "sandbox_id": "shared",
        "_retained_startup_allocations": [receipt],
    }
    manager = RemoteAgentRuntimeManager.__new__(RemoteAgentRuntimeManager)
    manager._sessions_repo = SimpleNamespace(
        list_startup_allocation_candidates=AsyncMock(return_value=[current]),
        get_session_including_deleted=AsyncMock(return_value=current),
        clear_retained_startup_allocation=AsyncMock(),
    )
    manager._allocation_box_is_gone = AsyncMock(return_value=False)
    manager._release_startup_allocation = AsyncMock()
    result = await manager.reconcile_startup_allocations(stale_before=datetime.now(timezone.utc))
    assert result["startup_allocations_deferred"] == 1
    manager._release_startup_allocation.assert_not_awaited()
    manager._sessions_repo.clear_retained_startup_allocation.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_command_cannot_adopt_the_successors_session_generation():
    from astrabox.core.service.orchestrator.session_kernel.workers.lifecycle.startup import (
        _StartupOrchestrationMixin,
    )

    worker = _StartupOrchestrationMixin()
    worker._sessions_repo = SimpleNamespace(
        get_session=AsyncMock(
            return_value={
                "session_id": "s",
                "state": "CREATING",
                "sandbox_generation": "new-generation",
                "_runtime_recovery_owner": None,
            }
        )
    )

    async def repository_op(op, **_kwargs):
        return await op()

    worker._run_session_repo_op_with_retry = repository_op
    worker._runtime_subjects = SimpleNamespace(acquire_startup=AsyncMock())
    with pytest.raises(APIError) as error:
        await worker._run_startup_command(
            wakeup=SimpleNamespace(session_id="s"),
            command_event={"causation_id": "old-command"},
            payload={"sandbox_generation": "old-generation"},
        )
    assert error.value.code == "RUNTIME_RECOVERY_SUPERSEDED"
    worker._runtime_subjects.acquire_startup.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_session_workspace_generation_fences_old_ready_and_failure():
    from astrabox.core.service.orchestrator.assistant.assistant_service import AssistantService
    from astrabox.core.service.orchestrator.assistant.assistant_workspace_service import (
        AssistantWorkspaceService,
    )

    row = {
        "state": "MATERIALIZING",
        "provisioning_session_id": "s",
        "provisioning_sandbox_generation": "old",
        "current_sandbox_id": None,
    }

    async def get(*_args, **_kwargs):
        return dict(row)

    async def compare(*_args, expected, updates):
        if any(row.get(key) != value for key, value in expected.items()):
            return False
        row.update(updates)
        return True

    repo = SimpleNamespace(
        get_workspace=get, compare_and_update_workspace=compare, mark_ready=AsyncMock()
    )
    workspace = AssistantWorkspaceService(workspace_repo=repo)
    assert await workspace.claim_materialization_generation(
        user_id="u",
        assistant_id="a",
        provisioning_session_id="s",
        observed_generation="old",
        sandbox_generation="new",
    )
    row["updated_at"] = "ordinary gateway update"
    service = AssistantService.__new__(AssistantService)
    service._workspace_service = workspace
    with pytest.raises(APIError) as error:
        await service.publish_workspace_runtime_ready(
            UserContext(user_id="u"),
            "a",
            provisioning_session_id="s",
            provisioning_sandbox_generation="old",
            sandbox_id="old-box",
            expires_at=None,
            runtime_identity=None,
        )
    assert error.value.code == "ASSISTANT_WORKSPACE_READY_CONFLICT"
    assert not await service.converge_workspace_startup_failure(
        UserContext(user_id="u"),
        "a",
        provisioning_session_id="s",
        provisioning_sandbox_generation="old",
        failure_phase="ready_publish_failed",
        created_sandbox_id="old-box",
        cleanup=None,
    )
    assert row["state"] == "MATERIALIZING"
    assert row["provisioning_sandbox_generation"] == "new"
    repo.mark_ready.assert_not_awaited()
    await service.publish_workspace_runtime_ready(
        UserContext(user_id="u"),
        "a",
        provisioning_session_id="s",
        provisioning_sandbox_generation="new",
        sandbox_id="new-box",
        expires_at=None,
        runtime_identity=None,
    )
    assert repo.mark_ready.await_args.kwargs["provisioning_sandbox_generation"] == "new"


@pytest.mark.asyncio
async def test_workspace_ready_cas_contains_the_exact_generation_at_database_boundary():
    from astrabox.persistence.repository.assistant_workspace_repository import (
        AssistantWorkspaceRepository,
    )

    queries = []

    async def update(query, _updates):
        queries.append(query)
        # A competing command of the same Session changed only the generation.
        assert query["provisioning_sandbox_generation"] == "old"
        return SimpleNamespace(modified_count=0)

    async def retry(_operation, op, **_kwargs):
        return await op()

    collection = SimpleNamespace(update_one=update, find_one=AsyncMock(return_value=None))
    with (
        patch(
            "astrabox.persistence.repository.assistant_workspace_repository.get_async_collection",
            AsyncMock(return_value=collection),
        ),
        patch(
            "astrabox.persistence.repository.assistant_workspace_repository.run_mongo_with_retry",
            side_effect=retry,
        ),
    ):
        result = await AssistantWorkspaceRepository().mark_ready(
            "u",
            "a",
            provisioning_session_id="s",
            provisioning_sandbox_generation="old",
            sandbox_id="old-box",
            expires_at=None,
            runtime_identity=None,
            profile_marker_key="profile",
            profile_marker={},
        )
    assert result is False
    assert len(queries) == 1
    assert queries[0]["provisioning_session_id"] == "s"


@pytest.mark.asyncio
async def test_startup_rollback_retains_a_whole_box_adopted_by_the_workspace():
    from astrabox.seams.sandbox import SandboxAllocation

    sessions = Sessions()
    sessions.retain_startup_allocation = AsyncMock()
    sessions.list_sessions_by_sandbox_id = AsyncMock(return_value=[])
    ownership = RecoveryOwnership(
        sessions, "s", "old", sandbox_generation="old-generation", assignment_id="old-command"
    )
    allocation = SandboxAllocation(
        sandbox_id="adopted-box", sandbox_backend="test", scope="sandbox"
    )
    manager = RemoteAgentRuntimeManager.__new__(RemoteAgentRuntimeManager)
    manager._sessions_repo = sessions
    manager._allocation_box_is_gone = AsyncMock()
    manager.destroy_sandbox_by_id = AsyncMock()
    workspace_repo = SimpleNamespace(
        list_workspaces_by_sandbox_id=AsyncMock(
            return_value=[{"current_sandbox_id": "adopted-box"}]
        )
    )
    agent_repo = SimpleNamespace(find_agent_by_sandbox_id=AsyncMock(return_value=None))
    with (
        ownership.bind(),
        patch(
            "astrabox.core.service.orchestrator.runtime_manager.AssistantWorkspaceRepository",
            return_value=workspace_repo,
        ),
        patch(
            "astrabox.core.service.orchestrator.runtime_manager.AgentRepository",
            return_value=agent_repo,
        ),
    ):
        result = await manager._release_startup_allocation(
            "s", allocation, clear_durable_record=True
        )
    assert result.released is False
    assert result.destruction.outcome == "REFUSED"
    manager.destroy_sandbox_by_id.assert_not_awaited()
    manager._allocation_box_is_gone.assert_not_awaited()
    assert (
        sessions.retain_startup_allocation.await_args.args[1]["allocation"]
        == allocation.as_record()
    )


@pytest.mark.asyncio
async def test_retained_receipt_is_removed_only_after_supplier_confirms_its_box_absent():
    from datetime import datetime, timezone
    from astrabox.seams.sandbox import SandboxAllocation

    allocation = SandboxAllocation(
        sandbox_id="gone-box",
        sandbox_backend="test",
        scope="isolated_sessions",
        isolated_session_ids=("old-isolated",),
    )
    receipt = {
        "allocation": allocation.as_record(),
        "sandbox_generation": "old",
        "assignment_id": "old-command",
    }
    row = {"session_id": "s", "_retained_startup_allocations": [receipt]}
    manager = RemoteAgentRuntimeManager.__new__(RemoteAgentRuntimeManager)
    manager._sessions_repo = SimpleNamespace(
        list_startup_allocation_candidates=AsyncMock(return_value=[row]),
        get_session_including_deleted=AsyncMock(return_value=row),
        clear_retained_startup_allocation=AsyncMock(),
    )
    manager._allocation_box_is_gone = AsyncMock(return_value=True)
    manager._forget_pending_allocation = Mock()
    result = await manager.reconcile_startup_allocations(stale_before=datetime.now(timezone.utc))
    assert result["startup_allocations_released"] == 1
    manager._sessions_repo.clear_retained_startup_allocation.assert_awaited_once_with("s", receipt)
    manager._forget_pending_allocation.assert_called_once_with("s", allocation)


@pytest.mark.asyncio
async def test_superseded_final_binding_retains_an_already_recorded_allocation():
    from astrabox.seams.sandbox import SandboxAllocation

    sessions = Sessions()
    sessions.row.update(
        {"state": "CREATING", "_runtime_recovery_owner": "new", "sandbox_generation": "new"}
    )
    sessions.retain_startup_allocation = AsyncMock()
    allocation = SandboxAllocation(sandbox_id="old-box", sandbox_backend="test", scope="sandbox")
    ownership = RecoveryOwnership(
        sessions, "s", "old", sandbox_generation="old", assignment_id="old-command"
    )
    ownership.allocations.append(allocation)
    with pytest.raises(APIError) as error:
        await ownership.update(
            {"state": "READY", "sandbox_id": "old-box", "startup_allocation": None}
        )
    assert error.value.code == "RUNTIME_RECOVERY_SUPERSEDED"
    assert sessions.row["sandbox_generation"] == "new"
    sessions.retain_startup_allocation.assert_awaited_once_with(
        "s",
        {
            "allocation": allocation.as_record(),
            "sandbox_generation": "old",
            "assignment_id": "old-command",
        },
    )


@pytest.mark.asyncio
async def test_startup_attach_branch_cannot_publish_a_client_after_generation_changes():
    from astrabox.core.service.orchestrator.runtime_subject import RuntimeStartupTarget
    from astrabox.core.service.orchestrator.session_kernel.workers.lifecycle.startup import (
        _StartupOrchestrationMixin,
    )

    sessions = Sessions()
    sessions.row.update(
        {"state": "CREATING", "_runtime_recovery_owner": "old", "sandbox_generation": "old"}
    )
    ownership = RecoveryOwnership(
        sessions, "s", "old", sandbox_generation="old", assignment_id="old-command"
    )
    manager = RemoteAgentRuntimeManager.__new__(RemoteAgentRuntimeManager)
    manager._raise_if_quiesced = Mock()
    manager._runtimes = {}
    manager._session_locks = {}
    manager._drop_runtime_for_sandbox_mismatch = AsyncMock(return_value=None)
    manager._drop_runtime_for_unusable_client = AsyncMock(return_value=None)
    manager._validate_runtime_attach_plan = Mock()
    manager._require_runtime_engine_manifest = Mock()
    manager._disconnect_runtime_client = AsyncMock()
    entered, release = asyncio.Event(), asyncio.Event()
    old_client = SimpleNamespace(sandbox_id="old-box")
    new_client = SimpleNamespace(sandbox_id="new-box")

    async def attach(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return old_client

    worker = _StartupOrchestrationMixin()
    worker._sessions_repo = sessions
    worker._runtime_manager = manager
    worker._runtime_subjects = SimpleNamespace(
        acquire_startup=AsyncMock(
            return_value=RuntimeStartupTarget(
                action="attach_runtime",
                session=dict(sessions.row),
                workspace_plan=SimpleNamespace(
                    sandbox_id="old-box", engine_kind="test", session_kind="assistant_chat"
                ),
            )
        ),
        cleanup_failed_startup_runtime=AsyncMock(),
    )
    with (
        ownership.bind(),
        patch(
            "astrabox.core.service.orchestrator.engine.startup.attach_platform_runtime",
            side_effect=attach,
        ),
        patch(
            "astrabox.core.service.orchestrator.runtime_manager.get_engine_adapter",
            return_value=object(),
        ),
    ):
        pending = asyncio.create_task(
            worker._run_session_startup_direct(
                session_id="s",
                assignment_id="old-command",
                template=SimpleNamespace(),
                user_id="u",
                permission_mode=None,
                resume_session_id=None,
                on_progress=None,
                on_ready=None,
                on_failed=None,
                ownership=ownership,
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            sessions.row.update(
                {
                    "_runtime_recovery_owner": "new",
                    "sandbox_generation": "new",
                    "sandbox_id": "new-box",
                }
            )
            manager._runtimes["s"] = new_client
            release.set()
            with pytest.raises(APIError) as error:
                await pending
            assert error.value.code == "RUNTIME_RECOVERY_SUPERSEDED"
        finally:
            release.set()
            if not pending.done():
                pending.cancel("explicit")
                await asyncio.gather(pending, return_exceptions=True)
    assert manager._runtimes["s"] is new_client
    manager._disconnect_runtime_client.assert_awaited_once_with(
        old_client, session_id="s", reason="attachment owner changed"
    )
    worker._runtime_subjects.cleanup_failed_startup_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_background_reconcile_retains_a_box_published_to_the_workspace_before_session_ready():
    from datetime import datetime, timezone
    from astrabox.seams.sandbox import SandboxAllocation
    from astrabox.core.service.orchestrator.session_kernel.workers.lifecycle.recovery_ownership import (
        current_recovery,
    )

    allocation = SandboxAllocation(
        sandbox_id="published-box", sandbox_backend="test", scope="sandbox"
    )
    current = {
        "session_id": "s",
        "state": "CREATING",
        "sandbox_id": None,
        "updated_at": "2026-01-01T00:00:00+00:00",
        "startup_allocation": allocation.as_record(),
    }
    sessions = SimpleNamespace(
        list_startup_allocation_candidates=AsyncMock(return_value=[current]),
        get_session_including_deleted=AsyncMock(return_value=current),
        list_sessions_by_sandbox_id=AsyncMock(return_value=[]),
        clear_startup_allocation=AsyncMock(),
    )
    manager = RemoteAgentRuntimeManager.__new__(RemoteAgentRuntimeManager)
    manager._sessions_repo = sessions
    manager._pending_startup_allocations = {}
    manager._sandbox_backend_cache = {}
    manager._runtimes = {}
    manager.destroy_sandbox_by_id = AsyncMock()
    provider = SimpleNamespace(
        probe=AsyncMock(return_value=SimpleNamespace(probe_status="OK")),
        claim_of=AsyncMock(),
        confirm_destroyed=AsyncMock(),
    )
    workspace = {"state": "READY", "current_sandbox_id": "published-box"}
    workspace_repo = SimpleNamespace(
        list_workspaces_by_sandbox_id=AsyncMock(return_value=[workspace])
    )
    agent_repo = SimpleNamespace(find_agent_by_sandbox_id=AsyncMock(return_value=None))
    assert current_recovery("s") is None
    with (
        patch(
            "astrabox.core.service.orchestrator.runtime_manager.AssistantWorkspaceRepository",
            return_value=workspace_repo,
        ),
        patch(
            "astrabox.core.service.orchestrator.runtime_manager.AgentRepository",
            return_value=agent_repo,
        ),
        patch(
            "astrabox.core.service.orchestrator.runtime_manager.sandbox_for_name",
            return_value=provider,
        ),
    ):
        result = await manager.reconcile_startup_allocations(
            stale_before=datetime(2026, 9, 16, tzinfo=timezone.utc)
        )
    assert result["startup_allocations_released"] == 0
    assert result["startup_allocation_failures"] == 1
    assert current["startup_allocation"] == allocation.as_record()
    assert workspace == {"state": "READY", "current_sandbox_id": "published-box"}
    sessions.clear_startup_allocation.assert_not_awaited()
    manager.destroy_sandbox_by_id.assert_not_awaited()
    provider.confirm_destroyed.assert_not_awaited()
    provider.claim_of.assert_not_awaited()
