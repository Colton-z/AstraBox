"""The runtime sweeps reach every row of the sets they sweep.

Prewarm-enabled Agents, Agents holding a resident box, deferred startup
allocations, bindings a probe could not settle and conversations that are not
idle yet all stay in the sets the watcher sweeps after a visit. A sweep that
reads one capped page of such a set in a fixed order reads the same page on
every tick, and the rows behind it are never renewed, rebuilt, reaped,
released, probed or parked. These tests put more rows in the real repositories
than one page holds and record which rows the sweeps reach.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.config.settings import get_settings
from astrabox.core.service.orchestrator.agent.agent_service import AgentService
from astrabox.core.service.orchestrator.assistant.assistant_workspace_service import (
    AssistantWorkspaceService,
)
from astrabox.core.service.orchestrator.expiration_watcher import ExpirationWatcher
from astrabox.core.service.orchestrator.runtime_manager import (
    RemoteAgentRuntimeManager,
    StartupAllocationCleanup,
)
from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.persistence.repository.assistant_workspace_repository import (
    AssistantWorkspaceRepository,
)
from astrabox.persistence.repository.backend import get_async_collection
from astrabox.persistence.repository.keyset import iter_keyset_pages
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.core.service.orchestrator.sandbox_lifecycle import SandboxLifecycleService
from astrabox.seams.sandbox import SandboxAllocation
from astrabox.seams.sandbox_disposal import SandboxDestruction
from astrabox.common.utils.time_utils import parse_iso

#: More Agents than the watcher's former page of 100.
_AGENTS = 150
#: More Agents than the display listing's page of 200.
_MORE_THAN_THE_LISTING = 250


@pytest.fixture(autouse=True)
def _isolated_sqlite_state(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ASTRABOX_DB_BACKEND", "sqlite")
    monkeypatch.delenv("ASTRABOX_DB_URL", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _stamp(index: int) -> str:
    """An ``updated_at`` that increases with ``index``."""

    return f"2026-01-01T{index // 3600:02d}:{index // 60 % 60:02d}:{index % 60:02d}+00:00"


async def _agents(count: int, **fields: Any) -> list[str]:
    repo = AgentRepository()
    ids = [f"agent-{index:03d}" for index in range(count)]
    for index, agent_id in enumerate(ids):
        await repo.create_agent(
            {
                "agent_id": agent_id,
                "name": agent_id,
                "state": "ACTIVE",
                "updated_at": _stamp(index),
                **{key: value(index) if callable(value) else value for key, value in fields.items()},
            }
        )
    return ids


class _AgentService:
    def __init__(self) -> None:
        self.scheduled: list[str] = []

    def schedule_runtime_reconciliation(self, agent_id: str) -> bool:
        self.scheduled.append(agent_id)
        return True


async def test_the_prewarm_sweep_reaches_every_prewarm_enabled_agent_on_every_tick() -> None:
    ids = await _agents(_AGENTS, prewarm_enabled=True)
    # Rows the sweep must not pick up, between and after the ones it must.
    repo = AgentRepository()
    await repo.create_agent({"agent_id": "agent-050x", "name": "off", "state": "ACTIVE",
                             "prewarm_enabled": False, "updated_at": _stamp(0)})
    await repo.create_agent({"agent_id": "agent-999", "name": "going", "state": "DELETING",
                             "prewarm_enabled": True, "updated_at": _stamp(0)})
    service = _AgentService()
    manager = RemoteAgentRuntimeManager(agent_service_getter=lambda: service)

    ticks = [await manager.keep_prewarmed_agents_ready() for _ in range(3)]

    # None of these Agents has a prepared slot, so reaching one schedules its
    # build; later ticks fall inside the rebuild retry window.
    missed = sorted(set(ids) - set(service.scheduled))
    assert missed == [], f"{len(missed)} prewarm-enabled Agents never reached, first {missed[:3]}"
    assert sorted(service.scheduled) == ids
    # Slot and lease renewal are due at a time, so every tick visits them all.
    assert [tick["prewarm_agents_scanned"] for tick in ticks] == [_AGENTS] * 3
    assert all(tick["prewarm_sweep_failures"] == 0 for tick in ticks)


async def test_the_reap_sweep_reaches_empty_boxes_behind_a_page_of_occupied_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _agents(_AGENTS, sandbox_id=lambda index: f"box-{index:03d}")
    # The Agents updated first hold occupied boxes, a full tick's worth.
    occupied = {f"box-{index:03d}" for index in range(100)}
    empty = [f"box-{index:03d}" for index in range(100, _AGENTS)]
    manager = RemoteAgentRuntimeManager()

    async def _occupants(sandbox_id: str, *, excluding: str, provider: Any = None) -> bool:
        return sandbox_id in occupied

    destroyed: list[str] = []

    async def _destroy(sandbox_id: str) -> SandboxDestruction:
        destroyed.append(sandbox_id)
        return SandboxDestruction.confirmed_gone(sandbox_id, detail="gone")

    monkeypatch.setattr(manager, "agent_box_has_other_occupants", _occupants)
    monkeypatch.setattr(manager, "destroy_sandbox_by_id", _destroy)

    ticks = [await manager.reap_abandoned_agent_boxes() for _ in range(3)]

    assert sorted(destroyed) == empty, f"reached {len(destroyed)} of {len(empty)} empty boxes"
    # Each visit probes the control plane, so a tick keeps its budget.
    assert all(tick["agent_boxes_scanned"] <= 100 for tick in ticks)
    assert sum(tick["agent_boxes_kept"] for tick in ticks[:2]) == len(occupied)
    remaining = await AgentRepository().list_agents_with_resident_boxes(limit=1_000)
    assert sorted(row["sandbox_id"] for row in remaining) == sorted(occupied)


def _allocation(sandbox_id: str) -> dict[str, Any]:
    return SandboxAllocation(
        sandbox_id=sandbox_id, sandbox_backend="open_sandbox", scope="sandbox"
    ).as_record()


async def _sessions(rows: list[dict[str, Any]]) -> None:
    collection = await get_async_collection(SessionRepository()._collection_name)
    await collection.insert_many(rows)


async def test_startup_reconcile_reaches_stale_allocations_behind_a_page_of_deferred_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Fifty startups still inside their grace window, updated last, and ten
    # stale ones behind them.
    deferred = [f"s-{index:03d}" for index in range(50)]
    stale = [f"s-{index:03d}" for index in range(50, 60)]
    await _sessions(
        [
            {
                "session_id": session_id,
                "state": "CREATING",
                "updated_at": _stamp(1_000 + index) if session_id in deferred else _stamp(index),
                "startup_allocation": _allocation(f"box-{session_id}"),
            }
            for index, session_id in enumerate(deferred + stale)
        ]
    )
    manager = RemoteAgentRuntimeManager()
    released: list[str] = []

    async def _cleanup(session_id: str, **_kwargs: Any) -> StartupAllocationCleanup:
        released.append(session_id)
        return StartupAllocationCleanup(allocation=None, released=True)

    monkeypatch.setattr(manager, "cleanup_startup_allocation", _cleanup)
    stale_before = parse_iso(_stamp(500))

    ticks = [
        await manager.reconcile_startup_allocations(stale_before=stale_before, limit=50)
        for _ in range(3)
    ]

    assert sorted(set(released)) == stale, f"released {sorted(set(released))}"
    assert all(tick["startup_allocation_candidates"] <= 50 for tick in ticks)


async def test_the_occupancy_check_sees_a_joiner_behind_five_hundred_newer_allocations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    box = "shared-box"
    # The joiner's allocation is the oldest; five hundred startups on other
    # boxes were updated after it.
    await _sessions(
        [{"session_id": "joiner", "state": "CREATING", "updated_at": _stamp(0),
          "startup_allocation": _allocation(box)}]
        + [
            {"session_id": f"other-{index:03d}", "state": "CREATING",
             "updated_at": _stamp(1 + index),
             "startup_allocation": _allocation(f"box-{index:03d}")}
            for index in range(500)
        ]
    )
    manager = RemoteAgentRuntimeManager()

    class _EmptyBox:
        async def count_live_isolated_sessions(self, sandbox_id: str) -> int:
            return 0

    async def _provider(sandbox_id: str) -> Any:
        return _EmptyBox()

    monkeypatch.setattr(manager, "resolve_sandbox_provider", _provider)

    assert await manager.agent_box_has_other_occupants(box, excluding="") is True
    assert await manager.agent_box_has_other_occupants(box, excluding="joiner") is False


async def test_environment_and_startup_preparation_reach_every_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = await _agents(_MORE_THAN_THE_LISTING, environment_name="research")
    service = AgentService(
        platform_service=None,
        sessions_repo=None,
        runtime_manager=None,
        agent_config=None,
        turn_service=None,
        broker=None,
    )
    scheduled: list[str] = []

    def _schedule(agent_id: str) -> bool:
        scheduled.append(agent_id)
        return True

    monkeypatch.setattr(service, "_schedule_runtime_reconciliation", _schedule)

    # An Environment change must refresh every Agent bound to it.
    assert await service.reconcile_environment_runtimes("research") == len(ids)
    assert sorted(scheduled) == ids
    # Process start prepares every persisted Agent.
    scheduled.clear()
    await service.ensure_bootstrap()
    assert sorted(scheduled) == ids


async def test_a_page_that_does_not_advance_fails_instead_of_looping() -> None:
    page = [{"agent_id": f"agent-{index}"} for index in range(3)]

    async def _ignores_after(after: str | None) -> list[dict[str, Any]]:
        return list(page)

    with pytest.raises(RuntimeError, match="did not advance"):
        async for _row in iter_keyset_pages(_ignores_after, key="agent_id", page_size=3):
            pass


#: Lapsed or suspect bindings per owner collection: more than the watcher's
#: probe budget of 50 in each.
_BINDINGS = 60
_LAPSED = "2026-01-01T00:00:00+00:00"


class _ProbesAnswerNothing:
    """A control plane whose probe settles nothing, so every binding stays."""

    def __init__(self) -> None:
        self.probed: list[str] = []

    async def get_sandbox_lifecycle_probe(self, sandbox_id: str) -> Any:
        self.probed.append(sandbox_id)
        return SimpleNamespace(probe_status="UNAVAILABLE", sandbox_state="", error_text=None)

    def _is_terminal_sandbox_lifecycle_probe(self, probe: Any) -> bool:
        return False


async def test_the_dead_binding_sweep_probes_every_owner_row_behind_unsettled_ones() -> None:
    sessions = [f"session-box-{index:03d}" for index in range(_BINDINGS)]
    agents = [f"agent-box-{index:03d}" for index in range(_BINDINGS)]
    workspaces = [f"workspace-box-{index:03d}" for index in range(_BINDINGS)]
    await _sessions(
        [
            {"session_id": f"s-{index:03d}", "state": "READY", "sandbox_id": box,
             "expires_at": _stamp(index)}
            for index, box in enumerate(sessions)
        ]
    )
    await _agents(_BINDINGS, sandbox_id=lambda index: agents[index], expires_at=_stamp)
    workspace_collection = await get_async_collection(
        AssistantWorkspaceRepository()._collection_name
    )
    await workspace_collection.insert_many(
        [
            {"_id": f"workspace-{index:03d}", "assistant_id": f"assistant-{index:03d}",
             "state": "READY", "current_sandbox_id": box,
             "current_sandbox_expires_at": _stamp(index)}
            for index, box in enumerate(workspaces)
        ]
    )
    control_plane = _ProbesAnswerNothing()
    platform = SimpleNamespace(
        _sessions_repo=SessionRepository(),
        _agent_repo=AgentRepository(),
        _assistant_workspace_service=AssistantWorkspaceService(),
        _runtime_manager=control_plane,
    )
    platform._sandbox_lifecycle_service = SandboxLifecycleService(platform_service=platform)
    watcher = ExpirationWatcher(platform_service=platform)

    ticks = []
    for _tick in range(5):
        before = len(control_plane.probed)
        await watcher._sweep_dead_bindings()
        ticks.append(len(control_plane.probed) - before)

    missed = sorted(set(sessions + agents + workspaces) - set(control_plane.probed))
    assert missed == [], f"{len(missed)} bindings never probed, first {missed[:3]}"
    # The probe budget per tick still holds.
    assert all(0 < probes <= 50 for probes in ticks), ticks


async def test_the_idle_sweep_reaches_a_parkable_conversation_behind_ones_that_stay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Twenty live conversations whose environment does not park, and five
    # behind them whose environment does. The sweep examines ten per tick.
    stays = [f"a-{index:03d}" for index in range(20)]
    parks = [f"b-{index:03d}" for index in range(5)]
    await _sessions(
        [
            {"session_id": session_id, "state": "READY", "sandbox_id": f"box-{session_id}",
             "expires_at": "2999-01-01T00:00:00+00:00"}
            for session_id in stays + parks
        ]
    )
    repo = SessionRepository()
    platform = SimpleNamespace(_sessions_repo=repo)

    async def _no_background(session_id: str) -> None:
        return None

    platform._get_background_task_state = _no_background
    watcher = ExpirationWatcher(platform_service=platform)

    async def _window(session: dict[str, Any], *, default_idle_seconds: int) -> int | None:
        return 60 if session["session_id"] in parks else None

    async def _idle(session: dict[str, Any], *, idle_after_seconds: int) -> bool:
        return True

    parked: list[str] = []

    async def _park(*, session_id: str, sandbox_id: str, retention_seconds: int) -> bool:
        parked.append(session_id)
        await repo.update_session(session_id, {"sandbox_parked_at": _LAPSED})
        return True

    monkeypatch.setattr(watcher, "_idle_window_if_parking", _window)
    monkeypatch.setattr(watcher, "_is_idle_past", _idle)
    monkeypatch.setattr(watcher, "_park_sandbox", _park)

    ticks = [await watcher._sweep_idle_bindings() for _ in range(4)]

    assert sorted(parked) == parks, f"parked {parked}"
    assert all(tick.get("idle_candidates", 0) <= 10 for tick in ticks)
