"""A backend's quota ledger admits exactly the room its ceiling has, under concurrent
creates.
"""

from __future__ import annotations

import asyncio

import pytest

from astrabox.core.service.orchestrator.runtime import sandbox_quota
from astrabox.seams.sandbox import SandboxDescriptor, SandboxPage

pytestmark = pytest.mark.usefixtures("database")

_GIB = 2**30


class _Docker:
    name = "docker-quota"
    supports_inventory_listing = True

    def __init__(self, states: dict[str, str]) -> None:
        self.states = states

    def per_box_overhead(self) -> tuple[float, int]:
        return 0.0, 0

    def paused_box_releases_resources(self) -> bool:
        return False

    async def list_sandboxes(
        self, *, page: int, page_size: int, metadata: dict[str, str]
    ) -> SandboxPage:
        charge = sandbox_quota.SandboxCharge(cpu=1.0, memory=_GIB).metadata()
        items = tuple(
            SandboxDescriptor(sandbox_id=sandbox_id, state=state, metadata=charge)
            for sandbox_id, state in self.states.items()
        )
        return SandboxPage(
            items=items, page=1, page_size=page_size, total_items=len(items),
            total_pages=1, has_next_page=False,
        )


async def _create(backend: _Docker, index: int) -> str | sandbox_quota.SandboxCapacityExhausted:
    try:
        return await sandbox_quota.reserve(
            backend,
            sandbox_quota.SandboxCharge(cpu=1.0, memory=_GIB),
            reason="conversation",
            session_id=f"s-{index}",
            assignment_id=f"a-{index}",
        )
    except sandbox_quota.SandboxCapacityExhausted as refused:
        return refused


async def test_concurrent_creates_take_exactly_the_room_the_quota_has(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Twelve creates ask a backend for room at once and three boxes fit: each
    # judges the usage it reads, so two that read it before either is stored
    # would both take the last box.
    backend = _Docker({})
    monkeypatch.setitem(sandbox_quota._QUOTAS, backend.name, sandbox_quota.SandboxQuota(boxes=3))
    await sandbox_quota.reconcile_sandbox_quota(backend)

    outcomes = await asyncio.gather(*(_create(backend, index) for index in range(12)))

    admitted = [outcome for outcome in outcomes if isinstance(outcome, str)]
    refused = [
        outcome for outcome in outcomes
        if isinstance(outcome, sandbox_quota.SandboxCapacityExhausted)
    ]
    assert len(admitted) == 3
    assert {error.ceiling for error in refused} == {"boxes"} and len(refused) == 9
    [entry] = await sandbox_quota.quota_report({backend.name: backend})
    assert (entry["usage"]["boxes"], entry["usage"]["creating"]) == (3, 3)


async def test_concurrent_first_creates_on_a_new_backend_all_find_its_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No replica has used this backend yet, so every create first folds in an
    # inventory, which creates the backend's ledger; none may fail on it.
    backend = _Docker({})
    monkeypatch.delitem(sandbox_quota._QUOTAS, backend.name, raising=False)

    outcomes = await asyncio.gather(*(_create(backend, index) for index in range(12)))

    assert len({outcome for outcome in outcomes if isinstance(outcome, str)}) == 12
    [entry] = await sandbox_quota.quota_report({backend.name: backend})
    assert entry["inventory_at"] is not None
    assert entry["usage"]["boxes"] == 12
