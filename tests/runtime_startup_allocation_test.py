from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator import runtime_manager as runtime_manager_module
from astrabox.core.service.orchestrator.runtime_manager import (
    RemoteAgentRuntimeManager,
    SessionRuntime,
)
from astrabox.core.service.orchestrator.session_workspace_plan import (
    RuntimeWorkspacePlan,
)
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.seams.sandbox import (
    SANDBOX_LIFECYCLE_PROBE_OK,
    SandboxAllocation,
)
from astrabox.seams.sandbox_disposal import SandboxClaim, SandboxDestruction


@pytest.fixture(autouse=True)
def _unbound_non_session_owners(monkeypatch: pytest.MonkeyPatch) -> None:
    """These allocation tests own only the explicit in-memory Session rows."""
    monkeypatch.setattr(
        runtime_manager_module.AssistantWorkspaceRepository,
        "list_workspaces_by_sandbox_id", AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        runtime_manager_module.AgentRepository,
        "find_agent_by_sandbox_id", AsyncMock(return_value=None),
    )


class _SessionRows:
    def __init__(
        self,
        rows: list[dict[str, Any]],
        *,
        record_failure: str | None = None,
    ) -> None:
        self.rows = {str(row["session_id"]): dict(row) for row in rows}
        self.record_failure = record_failure
        self.clear_calls: list[tuple[str, str]] = []
        self.after_list: Any = None

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = self.rows.get(session_id)
        if row is None or bool(row.get("deleted")):
            return None
        return dict(row)

    async def get_session_including_deleted(
        self, session_id: str
    ) -> dict[str, Any] | None:
        row = self.rows.get(session_id)
        return dict(row) if row is not None else None

    async def list_sessions_by_sandbox_id(self, sandbox_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.rows.values()
                if row.get("sandbox_id") == sandbox_id and not row.get("deleted")]

    async def record_startup_allocation(
        self,
        session_id: str,
        allocation: dict[str, Any],
    ) -> bool:
        if self.record_failure == "before_write":
            raise RuntimeError("database unavailable before write")
        existing = self.rows[session_id].get("startup_allocation")
        if isinstance(existing, dict):
            if existing == allocation:
                return True
            raise RuntimeError("another startup allocation already owns the row")
        self.rows[session_id]["startup_allocation"] = dict(allocation)
        if self.record_failure == "after_write":
            raise RuntimeError("database reply lost after write")
        return True

    async def clear_startup_allocation(
        self,
        session_id: str,
        *,
        allocation: dict[str, Any],
    ) -> bool:
        row = self.rows.get(session_id)
        if row is None:
            return True
        current = row.get("startup_allocation")
        if not isinstance(current, dict):
            return current is None
        if current != allocation:
            return True
        self.clear_calls.append((session_id, str(allocation["sandbox_id"])))
        row["startup_allocation"] = None
        return True

    async def list_startup_allocation_candidates(
        self,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        candidates = [
            dict(row)
            for row in self.rows.values()
            if isinstance(row.get("startup_allocation"), dict)
        ][:limit]
        if self.after_list is not None:
            self.after_list()
        return candidates


class _SandboxProvider:
    name = "test-backend"

    def __init__(self, *, close_error: BaseException | None = None) -> None:
        self.close_error = close_error
        self.closed_isolated_sessions: list[tuple[str, str]] = []
        self.claimed: list[tuple[str, str | None]] = []

    async def probe(self, sandbox_id: str) -> Any:
        _ = sandbox_id
        return SimpleNamespace(probe_status=SANDBOX_LIFECYCLE_PROBE_OK)

    async def close_isolated_session(
        self,
        sandbox_id: str,
        isolated_session_id: str,
    ) -> None:
        if self.close_error is not None:
            raise self.close_error
        self.closed_isolated_sessions.append((sandbox_id, isolated_session_id))

    async def claim_of(
        self,
        sandbox_id: str,
        *,
        expected_session_id: str | None = None,
    ) -> SandboxClaim:
        self.claimed.append((sandbox_id, expected_session_id))
        return SandboxClaim.mine(
            sandbox_id,
            detail="the Session owns this startup sandbox",
            session_id=expected_session_id,
        )


class _Client:
    def __init__(self) -> None:
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


class _AllocationAdapter:
    def __init__(
        self,
        allocation: SandboxAllocation,
        *,
        return_invalid_runtime: bool = False,
    ) -> None:
        self.allocation = allocation
        self.return_invalid_runtime = return_invalid_runtime
        self.client = _Client()


async def _start_with_recorded_allocation(
    platform: Any,
    adapter: _AllocationAdapter,
    *,
    session_id: str,
    **_kwargs: Any,
) -> SessionRuntime:
    """Stand in at the platform startup seam after a resource was allocated."""

    await platform.record_startup_allocation(session_id, adapter.allocation)
    if not adapter.return_invalid_runtime:
        raise AssertionError("a configured repository failure should stop here")
    return SessionRuntime(
        session_id=session_id,
        agent=None,
        engine_kind="claude_code",
        sandbox_id=adapter.allocation.sandbox_id,
        engine_client=adapter.client,
        engine_manifest=None,
    )


def _row(session_id: str = "session-1") -> dict[str, Any]:
    return {
        "session_id": session_id,
        "state": "CREATING",
        "updated_at": "2026-08-17T11:00:00+00:00",
        "sandbox_backend": "test-backend",
    }


def _plan(session_id: str = "session-1") -> RuntimeWorkspacePlan:
    return RuntimeWorkspacePlan(
        subject_kind="deployment_conversation",
        session_kind="agent_chat",
        operation="runtime_start",
        runtime_key=session_id,
        conversation_session_id=session_id,
        cwd="/workspace",
        resume_engine_session_key=None,
        sandbox_id=None,
        materialize_default_repo=False,
        default_repo_target_cwd=None,
        engine_kind="claude_code",
        agent_id="agent-1",
    )


def _isolated_allocation(
    sandbox_id: str = "shared-box",
    isolated_session_id: str = "isolated-session-1",
) -> SandboxAllocation:
    return SandboxAllocation(
        sandbox_id=sandbox_id,
        sandbox_backend="test-backend",
        scope="isolated_sessions",
        isolated_session_ids=(isolated_session_id,),
    )


@pytest.mark.parametrize("record_failure", ["before_write", "after_write"])
async def test_record_failure_rolls_back_exact_shared_placement(
    monkeypatch: pytest.MonkeyPatch,
    record_failure: str,
) -> None:
    repo = _SessionRows([_row()], record_failure=record_failure)
    provider = _SandboxProvider()
    manager = RemoteAgentRuntimeManager(sessions_repo=repo)
    allocation = _isolated_allocation()
    adapter = _AllocationAdapter(allocation)
    destroy = AsyncMock(
        side_effect=AssertionError("an isolated allocation must not destroy its box")
    )
    monkeypatch.setattr(manager, "destroy_sandbox_by_id", destroy)
    monkeypatch.setattr(
        runtime_manager_module,
        "sandbox_for_name",
        lambda _name: provider,
    )

    with (
        patch.object(runtime_manager_module, "get_engine_adapter", return_value=adapter),
        patch(
            "astrabox.core.service.orchestrator.engine.startup.start_platform_runtime",
            new=_start_with_recorded_allocation,
        ),
    ):
        with pytest.raises(APIError) as raised:
            await manager.create_runtime(
                "session-1",
                SimpleNamespace(),
                assignment_id="assignment-1",
                workspace_plan=_plan(),
            )

    assert raised.value.data is None
    assert provider.closed_isolated_sessions == [
        (allocation.sandbox_id, allocation.isolated_session_ids[0])
    ]
    destroy.assert_not_awaited()
    assert manager._pending_startup_sandbox_ids("session-1") == []
    assert repo.rows["session-1"].get("startup_allocation") is None


async def test_post_start_contract_failure_rolls_back_before_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _SessionRows([_row()])
    provider = _SandboxProvider()
    manager = RemoteAgentRuntimeManager(sessions_repo=repo)
    allocation = _isolated_allocation()
    adapter = _AllocationAdapter(allocation, return_invalid_runtime=True)
    monkeypatch.setattr(
        runtime_manager_module,
        "sandbox_for_name",
        lambda _name: provider,
    )

    with (
        patch.object(runtime_manager_module, "get_engine_adapter", return_value=adapter),
        patch(
            "astrabox.core.service.orchestrator.engine.startup.start_platform_runtime",
            new=_start_with_recorded_allocation,
        ),
    ):
        with pytest.raises(APIError) as raised:
            await manager.create_runtime(
                "session-1",
                SimpleNamespace(),
                assignment_id="assignment-1",
                workspace_plan=_plan(),
            )

    assert raised.value.code == "ENGINE_CAPABILITY_CONTRACT_VIOLATION"
    assert adapter.client.closed == 1
    assert provider.closed_isolated_sessions == [("shared-box", "isolated-session-1")]
    assert repo.rows["session-1"]["startup_allocation"] is None
    assert "session-1" not in manager._runtimes


async def test_conflicting_durable_allocation_is_preserved_while_new_one_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_allocation = SandboxAllocation(
        sandbox_id="old-box",
        sandbox_backend="test-backend",
        scope="sandbox",
    ).as_record()
    repo = _SessionRows([{**_row(), "startup_allocation": old_allocation}])
    provider = _SandboxProvider()
    manager = RemoteAgentRuntimeManager(sessions_repo=repo)
    new_allocation = _isolated_allocation(
        sandbox_id="shared-new",
        isolated_session_id="isolated-new",
    )
    adapter = _AllocationAdapter(new_allocation)
    destroy = AsyncMock(
        side_effect=AssertionError("neither the old box nor shared box may be destroyed")
    )
    monkeypatch.setattr(manager, "destroy_sandbox_by_id", destroy)
    monkeypatch.setattr(
        runtime_manager_module,
        "sandbox_for_name",
        lambda _name: provider,
    )

    with (
        patch.object(runtime_manager_module, "get_engine_adapter", return_value=adapter),
        patch(
            "astrabox.core.service.orchestrator.engine.startup.start_platform_runtime",
            new=_start_with_recorded_allocation,
        ),
    ):
        with pytest.raises(APIError) as raised:
            await manager.create_runtime(
                "session-1",
                SimpleNamespace(),
                assignment_id="assignment-1",
                workspace_plan=_plan(),
            )

    assert raised.value.data is None
    assert provider.closed_isolated_sessions == [("shared-new", "isolated-new")]
    assert repo.rows["session-1"]["startup_allocation"] == old_allocation
    destroy.assert_not_awaited()


async def test_reconcile_distinguishes_active_adopted_and_abandoned_allocations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    whole = lambda sandbox_id: SandboxAllocation(  # noqa: E731
        sandbox_id=sandbox_id,
        sandbox_backend="test-backend",
        scope="sandbox",
    ).as_record()
    rows = [
        {
            **_row("stale"),
            "startup_allocation": whole("box-stale"),
        },
        {
            **_row("fresh"),
            "updated_at": "2026-08-17T13:00:00+00:00",
            "startup_allocation": whole("box-fresh"),
        },
        {
            **_row("ready"),
            "state": "READY",
            "sandbox_id": "box-ready",
            "startup_allocation": whole("box-ready"),
        },
        {
            **_row("deleted"),
            "state": "READY",
            "sandbox_id": "box-deleted",
            "deleted": True,
            "updated_at": "2026-08-17T13:00:00+00:00",
            "startup_allocation": whole("box-deleted"),
        },
    ]
    repo = _SessionRows(rows)
    provider = _SandboxProvider()
    manager = RemoteAgentRuntimeManager(sessions_repo=repo)
    destroyed: list[str] = []

    async def destroy(sandbox_id: str) -> SandboxDestruction:
        destroyed.append(sandbox_id)
        return SandboxDestruction.confirmed_gone(
            sandbox_id,
            detail="provider confirmed deletion",
        )

    monkeypatch.setattr(manager, "destroy_sandbox_by_id", destroy)
    monkeypatch.setattr(
        runtime_manager_module,
        "sandbox_for_name",
        lambda _name: provider,
    )

    summary = await manager.reconcile_startup_allocations(
        stale_before=datetime(2026, 8, 17, 12, tzinfo=timezone.utc),
    )

    assert summary == {
        "startup_allocation_candidates": 4,
        "startup_allocations_adopted": 1,
        "startup_allocations_released": 2,
        "startup_allocations_deferred": 1,
        "startup_allocation_failures": 0,
    }
    assert destroyed == ["box-stale", "box-deleted"]
    assert repo.rows["fresh"]["startup_allocation"] == whole("box-fresh")
    assert repo.rows["ready"]["startup_allocation"] is None
    assert repo.rows["deleted"]["startup_allocation"] is None


async def test_reconcile_rereads_candidate_before_destroying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allocation = SandboxAllocation(
        sandbox_id="box-race",
        sandbox_backend="test-backend",
        scope="sandbox",
    ).as_record()
    repo = _SessionRows([{**_row("race"), "startup_allocation": allocation}])

    def publish_ready() -> None:
        repo.rows["race"].update({"state": "READY", "sandbox_id": "box-race"})

    repo.after_list = publish_ready
    manager = RemoteAgentRuntimeManager(sessions_repo=repo)
    destroy = AsyncMock(
        side_effect=AssertionError("a newly READY allocation must be adopted")
    )
    monkeypatch.setattr(manager, "destroy_sandbox_by_id", destroy)

    summary = await manager.reconcile_startup_allocations(
        stale_before=datetime(2026, 8, 17, 12, tzinfo=timezone.utc),
    )

    assert summary["startup_allocations_adopted"] == 1
    assert repo.rows["race"]["startup_allocation"] is None
    destroy.assert_not_awaited()


async def test_periodic_reconcile_does_not_reap_this_process_active_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _SessionRows([_row("active")])
    manager = RemoteAgentRuntimeManager(sessions_repo=repo)
    allocation = SandboxAllocation(
        sandbox_id="box-active",
        sandbox_backend="test-backend",
        scope="sandbox",
    )
    await manager.record_startup_allocation("active", allocation)
    destroy = AsyncMock(
        side_effect=AssertionError("the local startup still owns this allocation")
    )
    monkeypatch.setattr(manager, "destroy_sandbox_by_id", destroy)

    summary = await manager.reconcile_startup_allocations(
        stale_before=datetime(2026, 8, 17, 12, tzinfo=timezone.utc),
    )

    assert summary["startup_allocations_deferred"] == 1
    assert repo.rows["active"]["startup_allocation"] == allocation.as_record()
    destroy.assert_not_awaited()


async def test_failed_isolated_release_keeps_the_durable_name_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allocation = _isolated_allocation().as_record()
    repo = _SessionRows([{**_row("failed"), "startup_allocation": allocation}])
    provider = _SandboxProvider(close_error=RuntimeError("control plane unavailable"))
    manager = RemoteAgentRuntimeManager(sessions_repo=repo)
    monkeypatch.setattr(
        runtime_manager_module,
        "sandbox_for_name",
        lambda _name: provider,
    )

    summary = await manager.reconcile_startup_allocations(
        stale_before=datetime(2026, 8, 17, 12, tzinfo=timezone.utc),
    )

    assert summary["startup_allocation_failures"] == 1
    assert summary["startup_allocations_released"] == 0
    assert repo.rows["failed"]["startup_allocation"] == allocation


async def test_terminated_startup_retries_failed_isolated_cleanup_without_losing_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allocation = SandboxAllocation(
        sandbox_id="shared-box",
        sandbox_backend="test-backend",
        scope="isolated_sessions",
        isolated_session_ids=("failed-engine", "failed-terminal"),
    )
    successor = {**_row("successor"), "state": "READY", "sandbox_id": "shared-box"}
    repo = _SessionRows([_row("failed"), successor])
    provider = _SandboxProvider(close_error=RuntimeError("control plane unavailable"))
    manager = RemoteAgentRuntimeManager(sessions_repo=repo)
    destroy = AsyncMock(side_effect=AssertionError("the successor still owns this shared box"))
    monkeypatch.setattr(manager, "destroy_sandbox_by_id", destroy)
    monkeypatch.setattr(runtime_manager_module, "sandbox_for_name", lambda _name: provider)
    await manager.record_startup_allocation("failed", allocation)

    cleanup = await manager.cleanup_startup_allocation("failed")
    assert cleanup.released is False
    assert cleanup.leaked_sandbox_id is None
    assert repo.rows["failed"]["startup_allocation"] == allocation.as_record()

    # Startup failure terminates the Session even when closing its isolated
    # resources failed. Its whole-box leak field is empty for this scope.
    repo.rows["failed"]["state"] = "TERMINATED"
    cutoff = datetime(2026, 8, 17, 12, tzinfo=timezone.utc)
    failed = await manager.reconcile_startup_allocations(stale_before=cutoff)
    assert failed["startup_allocation_failures"] == 1
    assert failed["startup_allocations_adopted"] == 0
    assert failed["startup_allocations_deferred"] == 0
    assert repo.rows["failed"]["startup_allocation"] == allocation.as_record()
    assert repo.clear_calls == []
    assert provider.closed_isolated_sessions == []

    provider.close_error = None
    retried = await manager.reconcile_startup_allocations(stale_before=cutoff)
    assert retried["startup_allocations_released"] == 1
    assert retried["startup_allocation_failures"] == 0
    assert provider.closed_isolated_sessions == [
        ("shared-box", "failed-engine"), ("shared-box", "failed-terminal"),
    ]
    assert repo.rows["failed"]["startup_allocation"] is None
    assert repo.rows["successor"] == successor
    assert manager._pending_startup_allocations.get("failed", []) == []
    assert repo.clear_calls == [("failed", "shared-box")]
    destroy.assert_not_awaited()


async def test_deleted_startup_keeps_its_scope_until_whole_box_release_is_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allocation = SandboxAllocation(
        sandbox_id="deleted-box", sandbox_backend="test-backend", scope="sandbox",
    ).as_record()
    repo = _SessionRows([{
        **_row("deleted"), "state": "READY", "deleted": True,
        "sandbox_id": "deleted-box", "startup_allocation": allocation,
    }])
    provider = _SandboxProvider()
    manager = RemoteAgentRuntimeManager(sessions_repo=repo)
    destroy = AsyncMock(return_value=SandboxDestruction.unconfirmed(
        "deleted-box", detail="the supplier did not confirm release",
    ))
    monkeypatch.setattr(manager, "destroy_sandbox_by_id", destroy)
    monkeypatch.setattr(runtime_manager_module, "sandbox_for_name", lambda _name: provider)
    cutoff = datetime(2026, 8, 17, 12, tzinfo=timezone.utc)

    failed = await manager.reconcile_startup_allocations(stale_before=cutoff)
    assert failed["startup_allocation_failures"] == 1
    assert failed["startup_allocations_adopted"] == 0
    assert failed["startup_allocations_released"] == 0
    assert repo.rows["deleted"]["startup_allocation"] == allocation
    assert repo.clear_calls == []
    destroy.assert_awaited_once_with("deleted-box")

    destroy.return_value = SandboxDestruction.confirmed_gone(
        "deleted-box", detail="the supplier confirmed release",
    )
    retried = await manager.reconcile_startup_allocations(stale_before=cutoff)
    assert retried["startup_allocations_released"] == 1
    assert retried["startup_allocation_failures"] == 0
    assert destroy.await_count == 2
    assert repo.rows["deleted"]["startup_allocation"] is None
    assert repo.clear_calls == [("deleted", "deleted-box")]


async def test_deleted_session_allocation_is_cleared_with_an_exact_resource_fence() -> None:
    allocation = SandboxAllocation(
        sandbox_id="box-deleted",
        sandbox_backend="test-backend",
        scope="sandbox",
    ).as_record()
    captured: dict[str, Any] = {}

    class _Collection:
        async def update_one(
            self,
            query: dict[str, Any],
            update: dict[str, Any],
        ) -> Any:
            captured["query"] = dict(query)
            captured["update"] = dict(update)
            return SimpleNamespace(modified_count=1, matched_count=1)

    async def get_collection(_name: str) -> _Collection:
        return _Collection()

    async def run(_label: str, operation: Any, **_kwargs: Any) -> Any:
        return await operation()

    repository = SessionRepository()
    repository.get_session_including_deleted = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "session_id": "deleted",
            "deleted": True,
            "startup_allocation": allocation,
        }
    )
    with (
        patch(
            "astrabox.persistence.repository.session_repository.get_async_collection",
            new=get_collection,
        ),
        patch(
            "astrabox.persistence.repository.session_repository.run_mongo_with_retry",
            new=run,
        ),
    ):
        cleared = await repository.clear_startup_allocation(
            "deleted",
            allocation=allocation,
        )

    assert cleared is True
    assert captured["query"] == {
        "session_id": "deleted",
        "startup_allocation": allocation,
    }
    assert captured["update"] == {"$set": {"startup_allocation": None}}
