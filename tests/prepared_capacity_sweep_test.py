"""The watcher keeps an Agent's prepared capacity reconciled between Sessions.

The client pool replenishes and expires its own boxes; the sweep renews the
resident shared box's lease, schedules the reaping of seats that went stale in
boxes the Agent already took, and reconciles each Agent on a cadence so a
restarted replica runs its pool schedulers again without waiting for a
conversation.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from astrabox.core.service.orchestrator.agent import prepared_slots as ps
from astrabox.core.service.orchestrator.runtime_manager import (
    RemoteAgentRuntimeManager,
)
from astrabox.persistence.repository import agent_repository


def _manifest(age_seconds: float, **overrides: Any) -> dict[str, Any]:
    prepared_at = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return {
        "state": "prepared",
        "slot_id": f"slot-{int(age_seconds)}",
        "runtime_generation": "generation-1",
        "prepared_at": prepared_at.isoformat(),
        **overrides,
    }


class _Repo:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def list_agents_with_prepared_capacity(
        self, *, after_agent_id: str | None = None, limit: int = 200
    ):
        ordered = sorted(self.rows, key=lambda row: row["agent_id"])
        return [row for row in ordered if after_agent_id is None or row["agent_id"] > after_agent_id][:limit]


class _AgentService:
    def __init__(self) -> None:
        self.scheduled: list[str] = []

    def schedule_runtime_reconciliation(self, agent_id: str) -> bool:
        self.scheduled.append(agent_id)
        return True


def _manager(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]) -> tuple[RemoteAgentRuntimeManager, _AgentService]:
    monkeypatch.setattr(agent_repository, "AgentRepository", lambda: _Repo(rows))
    service = _AgentService()
    manager = RemoteAgentRuntimeManager(agent_service_getter=lambda: service)
    manager.renewed: list[tuple[str, int]] = []  # type: ignore[attr-defined]

    async def _renew(sandbox_id: str, ttl_seconds: int) -> None:
        manager.renewed.append((sandbox_id, ttl_seconds))  # type: ignore[attr-defined]

    monkeypatch.setattr(manager, "renew_sandbox_by_id", _renew)
    return manager, service


def test_repeat_schedules_do_not_overlap_and_coalesce_into_one_followup() -> None:
    """Repeated requests cannot overlap one Agent's active reconciliation."""

    from astrabox.core.service.orchestrator.agent.agent_service import AgentService

    service = AgentService.__new__(AgentService)
    service._quiesced_reason = ""
    service._platform = None
    service._preparation_tasks = set()
    service._reconciliations_in_flight = {}
    service._reconciliation_requested = set()
    started: list[str] = []
    release = asyncio.Event()

    async def reconcile(agent_id: str) -> None:
        started.append(agent_id)
        await release.wait()

    service._reconcile_agent_runtime = reconcile  # type: ignore[method-assign]

    async def run() -> list[str]:
        assert service._schedule_runtime_reconciliation("a") is True
        assert service._schedule_runtime_reconciliation("a") is False
        assert service._schedule_runtime_reconciliation("a") is False
        assert service._schedule_runtime_reconciliation("b") is True
        await asyncio.sleep(0)
        first = list(started)
        release.set()
        while service._preparation_tasks:
            await asyncio.gather(*service._preparation_tasks)
            # Gathering already-complete tasks need not yield to their cleanup callbacks.
            await asyncio.sleep(0)
        assert started == ["a", "b", "a"]
        assert service._schedule_runtime_reconciliation("a") is True
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(*service._preparation_tasks)
        return first

    assert asyncio.run(run()) == ["a", "b"]
    assert started == ["a", "b", "a", "a"]
    assert service._reconciliations_in_flight == {}
    assert service._reconciliation_requested == set()


def test_a_seat_past_its_idle_lease_schedules_a_reconciliation_every_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = _manifest(ps.prepared_seat_ttl_seconds() + 60)
    rows = [{"agent_id": "stale", "sandbox_id": "box-1", ps.PREPARED_SLOTS_FIELD: [stale]}]
    manager, service = _manager(monkeypatch, rows)

    first = asyncio.run(manager.keep_prewarmed_agents_ready())
    second = asyncio.run(manager.keep_prewarmed_agents_ready())

    assert service.scheduled == ["stale", "stale"]
    assert first["prepared_seats_reaped"] == 1
    assert second["prepared_seats_reaped"] == 1


def test_a_claimed_seat_its_session_never_cleared_schedules_a_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orphan = _manifest(60, state="claimed", claimed_session_id="s-dead",
                       claimed_at=(datetime.now(timezone.utc) - timedelta(seconds=ps.CLAIMED_SLOT_ORPHAN_SECONDS + 60)).isoformat())
    rows = [{"agent_id": "orphaned", "sandbox_id": "box-1", ps.PREPARED_SLOTS_FIELD: [orphan]}]
    manager, service = _manager(monkeypatch, rows)

    summary = asyncio.run(manager.keep_prewarmed_agents_ready())

    assert service.scheduled == ["orphaned"]
    assert summary["prepared_seats_reaped"] == 1


def test_an_agent_with_nothing_stale_is_reconciled_once_per_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [{"agent_id": "warm", "sandbox_id": "box-1", ps.PREPARED_SLOTS_FIELD: [_manifest(60)]}]
    manager, service = _manager(monkeypatch, rows)

    first = asyncio.run(manager.keep_prewarmed_agents_ready())
    second = asyncio.run(manager.keep_prewarmed_agents_ready())

    assert service.scheduled == ["warm"]
    assert first["prepared_capacity_reconciled"] == 1
    assert second["prepared_capacity_reconciled"] == 0


def test_a_prewarmed_agents_box_lease_is_renewed_on_the_sweeps_own_cadence(monkeypatch: pytest.MonkeyPatch) -> None:
    """The only renew path was a turn's activity; a box holding nothing but
    prepared seats expired one lease after the last conversation."""

    rows = [{"agent_id": "warm", "sandbox_id": "box-1", ps.PREPARED_SLOTS_FIELD: [_manifest(60)]}]
    manager, _service = _manager(monkeypatch, rows)

    first = asyncio.run(manager.keep_prewarmed_agents_ready())
    second = asyncio.run(manager.keep_prewarmed_agents_ready())

    assert manager.renewed == [("box-1", 14400)]  # type: ignore[attr-defined]
    assert first["agent_box_leases_renewed"] == 1
    assert second["agent_box_leases_renewed"] == 0
