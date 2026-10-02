"""Confirmed sandbox loss clears only its exact binding, including deleted owners."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from astrabox.config.settings import get_settings
from astrabox.core.service.orchestrator import runtime_manager as runtime_module
from astrabox.core.service.orchestrator.runtime_manager import RemoteAgentRuntimeManager
from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.seams.sandbox import SandboxLifecycleProbeResult, SANDBOX_LIFECYCLE_PROBE_NOT_FOUND


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ASTRABOX_DB_BACKEND", "sqlite")
    monkeypatch.delenv("ASTRABOX_DB_URL", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("deleted", [True, False])
async def test_confirmed_missing_binding_is_cleared_once_without_restoring_owner(
    monkeypatch: pytest.MonkeyPatch, deleted: bool,
) -> None:
    repo = AgentRepository()
    original = {
        "agent_id": "owner", "name": "Owner", "sandbox_id": "gone-box",
        "sandbox_backend": "open_sandbox", "_resident_sandbox_generation": "old-generation",
        "deleted": deleted, "state": "DELETED" if deleted else "ACTIVE",
        "updated_at": "2026-09-01T00:00:00+00:00", "version": 7,
    }
    await repo.create_agent(original)
    provider = AsyncMock()
    provider.probe.return_value = SandboxLifecycleProbeResult(
        probe_status=SANDBOX_LIFECYCLE_PROBE_NOT_FOUND,
    )
    monkeypatch.setattr(runtime_module, "sandbox_for_name", lambda name: provider)
    manager = RemoteAgentRuntimeManager()
    destroy = AsyncMock(side_effect=AssertionError("missing compute must not be destroyed again"))
    monkeypatch.setattr(manager, "destroy_sandbox_by_id", destroy)
    first = await manager.reap_abandoned_agent_boxes()
    row = await repo.get_agent("owner", include_deleted=True)
    assert row is not None and row["sandbox_id"] is None
    assert row["sandbox_backend"] is None
    assert row["_resident_sandbox_generation"] is None
    for field in ("deleted", "state", "version", "updated_at"):
        assert row[field] == original[field]
    assert first == {
        "agent_boxes_scanned": 1, "agent_boxes_reaped": 1,
        "agent_boxes_kept": 0, "agent_box_reap_failures": 0,
    }
    second = await manager.reap_abandoned_agent_boxes()
    assert second["agent_boxes_scanned"] == 0
    provider.probe.assert_awaited_once_with("gone-box")
    destroy.assert_not_awaited()
    if deleted:
        assert await repo.get_agent("owner") is None
        assert not await repo.compare_and_update_agent(
            "owner", expected={"version": 7}, updates={"version": 8},
        )


@pytest.mark.parametrize("deleted", [True, False])
@pytest.mark.parametrize("replacement", [
    {"sandbox_id": "new-box", "sandbox_backend": "open_sandbox"},
    {"sandbox_id": "old-box", "sandbox_backend": "another_backend"},
])
async def test_a_binding_moved_during_probe_is_kept_and_not_reported_reaped(
    monkeypatch: pytest.MonkeyPatch, deleted: bool, replacement: dict[str, str],
) -> None:
    repo = AgentRepository()
    await repo.create_agent({
        "agent_id": "owner", "name": "Owner", "sandbox_id": "old-box",
        "sandbox_backend": "open_sandbox", "deleted": deleted,
    })

    async def probe(sandbox_id: str) -> SandboxLifecycleProbeResult:
        assert sandbox_id == "old-box"
        await repo.update_agent("owner", {
            **replacement,
            "_resident_sandbox_generation": "new-generation",
        })
        return SandboxLifecycleProbeResult(probe_status=SANDBOX_LIFECYCLE_PROBE_NOT_FOUND)

    provider = AsyncMock()
    provider.probe.side_effect = probe
    monkeypatch.setattr(runtime_module, "sandbox_for_name", lambda name: provider)
    manager = RemoteAgentRuntimeManager()
    summary = await manager.reap_abandoned_agent_boxes()
    row = await repo.get_agent("owner", include_deleted=True)
    assert row is not None
    assert all(row[key] == value for key, value in replacement.items())
    assert row["_resident_sandbox_generation"] == "new-generation"
    assert summary["agent_boxes_reaped"] == 0
    assert summary["agent_boxes_kept"] == 1


@pytest.mark.parametrize("deleted", [False, True])
async def test_missing_resident_withdraws_fresh_capacity_atomically(
    monkeypatch: pytest.MonkeyPatch, deleted: bool,
) -> None:
    repo = AgentRepository()
    manifest = {
        "slot_id": "fresh-slot", "sandbox_id": "gone-box",
        "sandbox_backend": "open_sandbox", "placement": "shared_slot",
        "state": "prepared", "prepared_at": "2099-01-01T00:00:00+00:00",
        "isolated_session_id": "child", "runtime_generation": "generation",
    }
    await repo.create_agent({
        "agent_id": "owner", "sandbox_id": "gone-box",
        "sandbox_backend": "open_sandbox", "deleted": deleted,
        "_prepared_slot": manifest,
    })
    provider = AsyncMock()
    provider.probe.return_value = SandboxLifecycleProbeResult(
        probe_status=SANDBOX_LIFECYCLE_PROBE_NOT_FOUND,
    )
    monkeypatch.setattr(runtime_module, "sandbox_for_name", lambda name: provider)
    summary = await RemoteAgentRuntimeManager().reap_abandoned_agent_boxes()
    row = await repo.get_agent("owner", include_deleted=True)
    assert row is not None and row["sandbox_id"] is None
    retired = row["_prepared_slot"]
    assert retired["state"] == "retiring"
    assert all(retired[key] == value for key, value in manifest.items() if key != "state")
    assert retired["retire_reason"] == "resident sandbox is confirmed gone"
    assert summary["agent_boxes_reaped"] == 1


@pytest.mark.parametrize("change", ["claim", "publish", "replacement"])
async def test_binding_cleanup_rechecks_a_concurrent_manifest_write(
    monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    from astrabox.persistence.repository import agent_repository as repository_module

    repo = AgentRepository()
    prepared = {
        "slot_id": "slot", "sandbox_id": "old-box",
        "sandbox_backend": "open_sandbox", "placement": "shared_slot",
        "state": "prepared",
    }
    await repo.create_agent({
        "agent_id": "owner", "sandbox_id": "old-box",
        "sandbox_backend": "open_sandbox", "_prepared_slot": None,
    })
    original_retry = repository_module.run_mongo_with_retry
    raced = False
    changed = dict(prepared)
    if change == "claim":
        changed.update(state="claimed", claimed_session_id="session")
    if change == "replacement":
        changed.update(sandbox_id="new-box", slot_id="new-slot")

    async def race(name: str, operation: Any) -> Any:
        nonlocal raced
        if name == "agents.clear_resident_sandbox_binding" and not raced:
            raced = True
            updates: dict[str, Any] = {"_prepared_slot": changed}
            if change == "replacement":
                updates["sandbox_id"] = "new-box"
            await repo.update_agent("owner", updates)
        return await original_retry(name, operation)

    monkeypatch.setattr(repository_module, "run_mongo_with_retry", race)
    cleared = await repo.clear_resident_sandbox_binding(
        "owner", sandbox_id="old-box", sandbox_backend="open_sandbox",
    )
    row = await repo.get_agent("owner")
    assert row is not None and raced
    if change == "replacement":
        assert not cleared and row["sandbox_id"] == "new-box"
        assert row["_prepared_slot"] == changed
    elif change == "claim":
        assert cleared and row["sandbox_id"] is None
        assert row["_prepared_slot"] == changed
    else:
        assert cleared and row["sandbox_id"] is None
        assert row["_prepared_slot"]["state"] == "retiring"
        assert row["_prepared_slot"]["slot_id"] == "slot"


@pytest.mark.parametrize("slot_patch", [
    {"sandbox_id": "other-box"},
    {"sandbox_backend": "other-backend"},
    {"placement": "conversation_box"},
    {"state": "claimed", "claimed_session_id": "session"},
])
async def test_binding_cleanup_preserves_capacity_it_does_not_own(
    slot_patch: dict[str, str],
) -> None:
    repo = AgentRepository()
    manifest = {
        "sandbox_id": "box", "sandbox_backend": "open_sandbox",
        "placement": "shared_slot", "state": "prepared", **slot_patch,
    }
    await repo.create_agent({
        "agent_id": "owner", "sandbox_id": "box",
        "sandbox_backend": "open_sandbox", "_prepared_slot": manifest,
    })
    assert await repo.clear_resident_sandbox_binding(
        "owner", sandbox_id="box", sandbox_backend="open_sandbox",
    )
    row = await repo.get_agent("owner")
    assert row is not None and row["_prepared_slot"] == manifest
