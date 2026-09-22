"""Recovery ownership and orphan receipts through real PostgreSQL repositories."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.assistant.assistant_workspace_service import (
    AssistantWorkspaceService,
)
from astrabox.core.service.orchestrator.session_kernel.workers.lifecycle.recovery_ownership import (
    RecoveryOwnership,
)
from astrabox.persistence.repository import assistant_workspace_repository, session_repository
from astrabox.persistence.repository.assistant_workspace_repository import (
    AssistantWorkspaceRepository,
    assistant_workspace_fence_id,
)
from astrabox.persistence.repository.postgresql import AsyncCollection, create_all, dispose_engines
from astrabox.persistence.repository.session_repository import SessionRepository
from tests._postgresql_support import resolve_postgresql_test_url

pytestmark = pytest.mark.postgresql


@pytest.fixture
async def collections(monkeypatch: pytest.MonkeyPatch):
    url = resolve_postgresql_test_url()
    await create_all(url)
    prefix = f"runtime_recovery_{uuid.uuid4().hex}"
    opened: dict[str, AsyncCollection] = {}

    async def get_collection(name: str) -> AsyncCollection:
        if name not in opened:
            opened[name] = AsyncCollection(f"{prefix}_{name}", url)
        return opened[name]

    monkeypatch.setattr(session_repository, "get_async_collection", get_collection)
    monkeypatch.setattr(assistant_workspace_repository, "get_async_collection", get_collection)
    try:
        yield get_collection
    finally:
        for collection in opened.values():
            await collection.delete_many({})
        await dispose_engines(url)


@pytest.mark.parametrize("explicit_null", [False, True], ids=["missing-owner", "null-owner"])
async def test_initial_startup_publishes_then_recovery_fences_the_old_owner(
    collections, explicit_null: bool,
) -> None:
    collection = await collections("sessions")
    await collection.insert_one({
        "session_id": "session", "state": "CREATING", "sandbox_generation": "generation-1",
        **({"_runtime_recovery_owner": None} if explicit_null else {}),
    })
    repo = SessionRepository()
    initial = RecoveryOwnership(repo, "session", None, sandbox_generation="generation-1")
    await initial.require_current()
    await initial.update({"startup_progress": "provisioning_container"})
    allocation = {"sandbox_id": "box-1", "sandbox_backend": "open_sandbox", "scope": "sandbox"}
    assert await repo.record_startup_allocation("session", allocation, owner_expected=initial.expected)
    await initial.update({"state": "READY", "sandbox_id": "box-1", "startup_allocation": None})

    assert await repo.compare_and_update_session(
        "session",
        expected={"state": "READY", "_runtime_recovery_owner": None, "sandbox_generation": "generation-1"},
        updates={"state": "CREATING", "_runtime_recovery_owner": "recovery-2", "sandbox_generation": "generation-2"},
    )
    successor = RecoveryOwnership(repo, "session", "recovery-2", sandbox_generation="generation-2")
    await successor.update({"state": "READY", "sandbox_id": "box-2"})
    with pytest.raises(APIError) as lost:
        await initial.update({"state": "TERMINATED", "sandbox_id": None})
    assert lost.value.code == "RUNTIME_RECOVERY_SUPERSEDED"
    current = await repo.get_session("session")
    assert current is not None
    assert (current["state"], current["sandbox_id"], current["sandbox_generation"]) == (
        "READY", "box-2", "generation-2",
    )


@pytest.mark.parametrize("explicit_null", [False, True], ids=["missing-generation", "null-generation"])
async def test_workspace_reservation_upgrade_fences_publish_failure_and_stalled_cleanup(
    collections, explicit_null: bool,
) -> None:
    collection = await collections("assistant_workspace")
    await collection.insert_one({
        "_id": assistant_workspace_fence_id("user", "assistant"),
        "assistant_id": "assistant", "created_by_user_id": "user",
        "state": "MATERIALIZING", "engine_kind": "assistant",
        "provisioning_session_id": "session", "current_sandbox_id": None,
        **({"provisioning_sandbox_generation": None} if explicit_null else {}),
    })
    repo = AssistantWorkspaceRepository()
    service = AssistantWorkspaceService(workspace_repo=repo)
    owner = {"user_id": "user", "assistant_id": "assistant", "provisioning_session_id": "session"}
    assert await service.claim_materialization_generation(
        **owner, observed_generation=None, sandbox_generation="generation-1",
    )
    assert await service.claim_materialization_generation(
        **owner, observed_generation="generation-1", sandbox_generation="generation-2",
    )
    assert not await service.claim_materialization_generation(
        **owner, observed_generation=None, sandbox_generation="stale-generation",
    )
    assert not await service.mark_materialization_failed(
        **owner, provisioning_sandbox_generation="generation-1", failure_phase="late_failure",
    )
    assert not await service.claim_stalled_materialization(
        **owner, provisioning_sandbox_generation="generation-1", last_error="late_cleanup",
    )
    ready: dict[str, Any] = {
        **owner, "sandbox_id": "box-2", "expires_at": None, "runtime_identity": None,
        "profile_marker_key": "profile", "profile_marker": {"sandbox_id": "box-2"},
    }
    assert not await repo.mark_ready(**ready, provisioning_sandbox_generation="generation-1")
    assert await repo.mark_ready(**ready, provisioning_sandbox_generation="generation-2")
    current = await service.get_workspace(user_id="user", assistant_id="assistant")
    assert current is not None
    assert (current["state"], current["current_sandbox_id"]) == ("READY", "box-2")
    assert current["provisioning_sandbox_generation"] is None


def _receipt(assignment: str) -> dict[str, Any]:
    return {
        "allocation": {"sandbox_id": "shared-box", "sandbox_backend": "open_sandbox",
                       "scope": "isolated_session", "isolated_session_ids": [assignment]},
        "sandbox_generation": "old-generation", "assignment_id": assignment,
    }


async def test_retained_receipts_are_discoverable_and_removed_only_by_exact_scope(collections) -> None:
    collection = await collections("sessions")
    await collection.insert_one({"session_id": "deleted-session", "deleted": True, "state": "TERMINATED"})
    await collection.insert_one({"session_id": "empty-session", "_retained_startup_allocations": []})
    await collection.insert_one({"session_id": "absent-session"})
    repo = SessionRepository()
    receipt = _receipt("old-placement")
    await repo.retain_startup_allocation("deleted-session", receipt)
    await repo.retain_startup_allocation("deleted-session", receipt)
    candidates = await repo.list_startup_allocation_candidates()
    assert [row["session_id"] for row in candidates] == ["deleted-session"]
    assert candidates[0]["_retained_startup_allocations"] == [receipt]
    await repo.clear_retained_startup_allocation("deleted-session", _receipt("other-placement"))
    assert len(await repo.list_startup_allocation_candidates()) == 1
    await repo.clear_retained_startup_allocation("deleted-session", receipt)
    assert await repo.list_startup_allocation_candidates() == []
    with pytest.raises(RuntimeError, match="missing Session"):
        await repo.retain_startup_allocation("nonexistent", receipt)


async def test_concurrent_receipt_append_and_remove_preserve_successor_binding(
    collections, monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection = await collections("sessions")
    old, new = _receipt("old-placement"), _receipt("new-placement")
    successor = {"sandbox_id": "successor-box", "state": "READY", "sandbox_generation": "new-generation",
                 "_runtime_recovery_owner": "new-owner", "startup_allocation": {"sandbox_id": "active-box"}}
    await collection.insert_one({"session_id": "session", **successor, "_retained_startup_allocations": [old]})
    repo = SessionRepository()
    real_read = repo.get_session_including_deleted
    both_read = asyncio.Event()
    reads = 0

    async def hold_first_reads(session_id: str):
        nonlocal reads
        row = await real_read(session_id)
        reads += 1
        if reads == 2:
            both_read.set()
        if reads <= 2:
            await both_read.wait()
        return row

    monkeypatch.setattr(repo, "get_session_including_deleted", hold_first_reads)
    await asyncio.wait_for(asyncio.gather(
        repo.retain_startup_allocation("session", new),
        repo.clear_retained_startup_allocation("session", old),
    ), timeout=10)
    current = await real_read("session")
    assert current is not None
    assert current["_retained_startup_allocations"] == [new]
    assert {key: current[key] for key in successor} == successor
