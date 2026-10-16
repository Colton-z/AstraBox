"""Confirmed sandbox loss clears only its exact binding, including deleted owners."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from astrabox.core.service.orchestrator import runtime_manager as runtime_module
from astrabox.core.service.orchestrator.runtime_manager import RemoteAgentRuntimeManager
from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.persistence.sql import fetch_one
from astrabox.persistence.sql.models.agents import Agent
from astrabox.seams.sandbox import SandboxLifecycleProbeResult, SANDBOX_LIFECYCLE_PROBE_NOT_FOUND
from tests._agent_ledger import create_agent, put_slots, runtime, set_runtime, slots


pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("database")]


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path))


async def _owner(
    *, deleted: bool = False, manifests: tuple[dict[str, Any], ...] = (), **record: Any,
) -> AgentRepository:
    """The Agent ``owner``, deleted or not, with ``record`` on its runtime record."""
    repo = await create_agent("owner", deleted=deleted)
    if record:
        await set_runtime("owner", **record)
    if manifests:
        await put_slots("owner", *manifests)
    return repo


async def _stored(agent_id: str) -> dict[str, Any]:
    """The Agent row and its runtime record as stored, deleted or not."""
    row = await fetch_one(select(Agent.__table__).where(Agent.__table__.c.agent_id == agent_id))
    assert row is not None
    return {**row, **await runtime(agent_id)}


@pytest.mark.parametrize("deleted", [True, False])
@pytest.mark.parametrize("replacement", [
    {"sandbox_id": "new-box", "sandbox_backend": "open_sandbox"},
    {"sandbox_id": "old-box", "sandbox_backend": "another_backend"},
])
async def test_a_binding_moved_during_probe_is_kept_and_not_reported_reaped(
    monkeypatch: pytest.MonkeyPatch, deleted: bool, replacement: dict[str, str],
) -> None:
    await _owner(deleted=deleted, sandbox_id="old-box", sandbox_backend="open_sandbox")

    async def probe(sandbox_id: str) -> SandboxLifecycleProbeResult:
        assert sandbox_id == "old-box"
        await set_runtime("owner", **replacement, resident_generation="new-generation")
        return SandboxLifecycleProbeResult(probe_status=SANDBOX_LIFECYCLE_PROBE_NOT_FOUND)

    provider = AsyncMock()
    provider.probe.side_effect = probe
    monkeypatch.setattr(runtime_module, "sandbox_for_name", lambda name: provider)
    manager = RemoteAgentRuntimeManager()
    summary = await manager.reap_abandoned_agent_boxes()
    row = await _stored("owner")
    assert all(row[key] == value for key, value in replacement.items())
    assert row["resident_generation"] == "new-generation"
    assert summary["agent_boxes_reaped"] == 0
    assert summary["agent_boxes_kept"] == 1


@pytest.mark.parametrize("change", ["claim", "publish", "replacement"])
async def test_binding_cleanup_judges_the_manifests_it_finds(change: str) -> None:
    prepared = {
        "slot_id": "slot", "sandbox_id": "old-box",
        "sandbox_backend": "open_sandbox", "placement": "shared_slot",
        "state": "prepared",
    }
    changed = dict(prepared)
    if change == "claim":
        changed.update(state="claimed", claimed_session_id="session")
    if change == "replacement":
        changed.update(sandbox_id="new-box", slot_id="new-slot")
    # The ledger as another writer left it just before the cleanup locks it.
    repo = await _owner(
        sandbox_id="new-box" if change == "replacement" else "old-box",
        sandbox_backend="open_sandbox",
        manifests=(changed,),
    )
    cleared = await repo.clear_resident_sandbox_binding(
        "owner", sandbox_id="old-box", sandbox_backend="open_sandbox",
    )
    row = await repo.get_agent("owner")
    assert row is not None
    if change == "replacement":
        assert not cleared and row["sandbox_id"] == "new-box"
        assert await slots("owner") == [changed]
    elif change == "claim":
        assert cleared and "sandbox_id" not in row
        assert await slots("owner") == [changed]
    else:
        assert cleared and "sandbox_id" not in row
        [retired] = await slots("owner")
        assert (retired["state"], retired["slot_id"]) == ("retiring", "slot")
