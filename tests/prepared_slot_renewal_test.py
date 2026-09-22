"""The watcher renews prepared slots before they expire.

A prepared slot is rebuilt only at a refill, and a refill only follows
Session activity or an Agent config write; the sweep here is what turns the
30-minute TTL into a rebuild on a timer instead of a cold start for the
next Session. The manifests are shaped like the ones ``prepare_slot_for_agent``
publishes; the ages are what decide.
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

    async def list_prewarm_enabled_agents(self, *, limit: int = 200):
        return list(self.rows)


class _AgentService:
    def __init__(self) -> None:
        self.scheduled: list[str] = []

    def schedule_runtime_reconciliation(self, agent_id: str) -> bool:
        self.scheduled.append(agent_id)
        return True


def _manager(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]) -> tuple[RemoteAgentRuntimeManager, _AgentService]:
    monkeypatch.setattr(agent_repository, "AgentRepository", lambda: _Repo(rows))
    monkeypatch.setattr(ps, "prepared_slot_renewal_lead_seconds", lambda: 300)
    service = _AgentService()
    manager = RemoteAgentRuntimeManager(agent_service_getter=lambda: service)
    manager.renewed: list[tuple[str, int]] = []  # type: ignore[attr-defined]

    async def _renew(sandbox_id: str, ttl_seconds: int) -> None:
        manager.renewed.append((sandbox_id, ttl_seconds))  # type: ignore[attr-defined]

    monkeypatch.setattr(manager, "renew_sandbox_by_id", _renew)
    return manager, service


def test_a_slot_that_would_expire_before_the_next_sweep_is_renewed(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {"agent_id": "due", "sandbox_id": "box-1", ps.PREPARED_SLOT_FIELD: _manifest(ps.PREPARED_SLOT_TTL_SECONDS - 100)},
        {"agent_id": "fresh", "sandbox_id": "box-2", ps.PREPARED_SLOT_FIELD: _manifest(60)},
        {"agent_id": "claimed", "sandbox_id": "box-3", ps.PREPARED_SLOT_FIELD: _manifest(60, state="claimed", claimed_at=(datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat())},
    ]
    manager, service = _manager(monkeypatch, rows)

    summary = asyncio.run(manager.keep_prewarmed_agents_ready())

    assert service.scheduled == ["due"]
    assert summary["prewarm_agents_scanned"] == 3
    assert summary["prepared_slots_renewed"] == 1
    assert summary["prepared_slots_rebuild_scheduled"] == 0
    assert summary["prewarm_sweep_failures"] == 0


def test_an_already_expired_slot_is_renewed_too(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"agent_id": "late", "sandbox_id": "box-1", ps.PREPARED_SLOT_FIELD: _manifest(ps.PREPARED_SLOT_TTL_SECONDS + 600)}]
    manager, service = _manager(monkeypatch, rows)

    asyncio.run(manager.keep_prewarmed_agents_ready())

    assert service.scheduled == ["late"]


def test_the_claim_keeps_the_strict_ttl_while_the_reaper_renews_early() -> None:
    """A Session may still adopt a slot inside the lead window: the lead is
    when the reaper starts rebuilding, not when the slot stops being good."""

    inside_lead = _manifest(ps.PREPARED_SLOT_TTL_SECONDS - 100)
    assert (
        ps._manifest_is_reapable(inside_lead, current_runtime_generation="generation-1")
        is None
    )
    assert ps._manifest_is_reapable(
        inside_lead, current_runtime_generation="generation-1", renewal_lead_seconds=300
    )


class _CasRepo:
    """The field's exact current value decides the CAS, as the store does."""

    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row

    async def compare_and_update_agent(self, agent_id: str, *, expected: dict[str, Any], updates: dict[str, Any]) -> bool:
        # An absent key matches nothing, not even None — the store's own
        # behaviour, and why the first publish writes an explicit None.
        for key, value in expected.items():
            if key not in self.row or self.row[key] != value:
                return False
        self.row.update(updates)
        return True


def test_a_renewal_swaps_the_manifest_against_the_slot_it_replaces() -> None:
    old = _manifest(ps.PREPARED_SLOT_TTL_SECONDS - 100)
    new = _manifest(0)
    repo = _CasRepo({"agent_id": "a", ps.PREPARED_SLOT_FIELD: old})

    asyncio.run(ps._publish_prepared_manifest(repo, "a", new, replacing=old))

    assert repo.row[ps.PREPARED_SLOT_FIELD] is new


def test_a_renewal_loses_to_a_session_that_claimed_the_old_slot_meanwhile() -> None:
    """The Session keeps what it claimed; the fresh build is the one discarded."""

    from astrabox.common.utils.errors import APIError

    old = _manifest(ps.PREPARED_SLOT_TTL_SECONDS - 100)
    claimed = {**old, "state": "claimed", "claimed_session_id": "s1"}
    repo = _CasRepo({"agent_id": "a", ps.PREPARED_SLOT_FIELD: claimed})

    with pytest.raises(APIError) as refused:
        asyncio.run(ps._publish_prepared_manifest(repo, "a", _manifest(0), replacing=old))

    assert refused.value.code == "AGENT_PREWARM_SLOT_CONFLICT"
    assert repo.row[ps.PREPARED_SLOT_FIELD] == claimed


def test_a_first_publish_still_expects_the_explicit_none() -> None:
    from astrabox.common.utils.errors import APIError

    repo = _CasRepo({"agent_id": "a", ps.PREPARED_SLOT_FIELD: None})
    asyncio.run(ps._publish_prepared_manifest(repo, "a", _manifest(0), replacing=None))
    assert repo.row[ps.PREPARED_SLOT_FIELD]["state"] == "prepared"

    absent = _CasRepo({"agent_id": "a"})
    with pytest.raises(APIError):
        asyncio.run(ps._publish_prepared_manifest(absent, "a", _manifest(0), replacing=None))


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


def test_the_renewal_lead_covers_a_build(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Settings:
        expiration_watcher_interval_seconds = 5

    monkeypatch.setattr(ps, "load_astrabox_settings", lambda: _Settings())

    assert ps.prepared_slot_renewal_lead_seconds() == 5 + ps.PREPARED_SLOT_RENEWAL_BUILD_SECONDS


def test_a_build_already_in_flight_is_not_counted_as_a_renewal(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"agent_id": "due", "sandbox_id": "box-1", ps.PREPARED_SLOT_FIELD: _manifest(ps.PREPARED_SLOT_TTL_SECONDS - 100)}]
    monkeypatch.setattr(agent_repository, "AgentRepository", lambda: _Repo(rows))
    monkeypatch.setattr(ps, "prepared_slot_renewal_lead_seconds", lambda: 300)

    class _Busy:
        def schedule_runtime_reconciliation(self, agent_id: str) -> bool:
            return False

    manager = RemoteAgentRuntimeManager(agent_service_getter=lambda: _Busy())
    summary = asyncio.run(manager.keep_prewarmed_agents_ready())

    assert summary["prewarm_agents_scanned"] == 1
    assert summary["prepared_slots_renewed"] == 0


def test_an_agent_with_no_slot_is_rebuilt_once_per_retry_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed build or an expired box left the Agent cold until a Session
    arrived; the sweep now rebuilds it, and retries on a cadence, not every tick."""

    rows = [{"agent_id": "cold", "sandbox_id": "", ps.PREPARED_SLOT_FIELD: None}]
    manager, service = _manager(monkeypatch, rows)

    first = asyncio.run(manager.keep_prewarmed_agents_ready())
    second = asyncio.run(manager.keep_prewarmed_agents_ready())

    assert service.scheduled == ["cold"]
    assert first["prepared_slots_rebuild_scheduled"] == 1
    assert second["prepared_slots_rebuild_scheduled"] == 0
    assert manager.renewed == []  # type: ignore[attr-defined]


def test_a_claimed_manifest_its_session_never_cleared_is_rebuilt(monkeypatch: pytest.MonkeyPatch) -> None:
    orphan = _manifest(60, state="claimed", claimed_session_id="s-dead",
                       claimed_at=(datetime.now(timezone.utc) - timedelta(seconds=ps.CLAIMED_SLOT_ORPHAN_SECONDS + 60)).isoformat())
    rows = [{"agent_id": "orphaned", "sandbox_id": "box-1", ps.PREPARED_SLOT_FIELD: orphan}]
    manager, service = _manager(monkeypatch, rows)

    asyncio.run(manager.keep_prewarmed_agents_ready())

    assert service.scheduled == ["orphaned"]


def test_a_prewarmed_agents_box_lease_is_renewed_on_the_sweeps_own_cadence(monkeypatch: pytest.MonkeyPatch) -> None:
    """The only renew path was a turn's activity; a box holding nothing but a
    prepared slot expired one lease after the last conversation."""

    rows = [{"agent_id": "warm", "sandbox_id": "box-1", ps.PREPARED_SLOT_FIELD: _manifest(60)}]
    manager, service = _manager(monkeypatch, rows)

    first = asyncio.run(manager.keep_prewarmed_agents_ready())
    second = asyncio.run(manager.keep_prewarmed_agents_ready())

    assert manager.renewed == [("box-1", 14400)]  # type: ignore[attr-defined]
    assert first["agent_box_leases_renewed"] == 1
    assert second["agent_box_leases_renewed"] == 0
    assert service.scheduled == []
