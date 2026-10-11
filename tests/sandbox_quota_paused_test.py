"""A paused box its backend released holds no quota until its resume takes it back."""

from __future__ import annotations

from typing import Any

import pytest

from astrabox.core.service.orchestrator.runtime import sandbox_quota
from astrabox.seams.sandbox import SandboxDescriptor, SandboxPage

_GIB = 2**30


class _Ledgers:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def read(self, backend: str) -> dict[str, Any]:
        row = self.rows.setdefault(
            backend,
            {"backend": backend, "version": 0, "reservations": [], "inventory": {}, "inventory_at": None},
        )
        return {**row, "reservations": list(row["reservations"]), "inventory": dict(row["inventory"])}

    async def compare_and_set(self, backend: str, *, version: int, reservations, inventory=None, inventory_at=None) -> bool:
        row = self.rows[backend]
        if row["version"] != version:
            return False
        row.update(version=version + 1, reservations=reservations)
        if inventory is not None:
            row.update(inventory=inventory, inventory_at=inventory_at)
        return True


class _Kubernetes:
    name = "opensandbox"
    supports_inventory_listing = True

    def __init__(self, states: dict[str, str]) -> None:
        self.states = states

    def per_box_overhead(self) -> tuple[float, int]:
        return 0.0, 0

    def paused_box_releases_resources(self) -> bool:
        return True

    async def list_sandboxes(self, *, page: int, page_size: int, metadata: dict[str, str]) -> SandboxPage:
        charge = sandbox_quota.SandboxCharge(cpu=1.0, memory=_GIB).metadata()
        items = tuple(
            SandboxDescriptor(sandbox_id=sandbox_id, state=state, metadata=charge)
            for sandbox_id, state in self.states.items()
        )
        return SandboxPage(items=items, page=1, page_size=page_size, total_items=len(items), total_pages=1, has_next_page=False)


@pytest.fixture
def ledgers(monkeypatch: pytest.MonkeyPatch) -> _Ledgers:
    ledgers = _Ledgers()
    monkeypatch.setattr(sandbox_quota, "_repo", lambda: ledgers)

    async def installation() -> str:
        return "installation-1"

    monkeypatch.setattr("astrabox.seams.sandbox.sandbox_installation_id", installation)
    monkeypatch.setitem(sandbox_quota._QUOTAS, "opensandbox", sandbox_quota.SandboxQuota(boxes=2))
    return ledgers


async def test_a_released_paused_box_frees_its_room_until_it_resumes(ledgers: _Ledgers) -> None:
    backend = _Kubernetes({"box-running": "Running", "box-paused": "Paused"})
    await sandbox_quota.reconcile_sandbox_quota(backend)
    usage = sandbox_quota._usage(await ledgers.read("opensandbox"))
    assert (usage.boxes, usage.memory) == (1, _GIB), "the paused box holds nothing"

    created = await sandbox_quota.reserve(
        backend, sandbox_quota.SandboxCharge(cpu=1.0, memory=_GIB),
        reason="conversation", session_id="s-new", assignment_id="a-new",
    )
    await sandbox_quota.mark_created("opensandbox", created, "box-new")
    backend.states["box-new"] = "Running"
    with pytest.raises(sandbox_quota.SandboxCapacityExhausted) as refused:
        await sandbox_quota.reserve_resume(backend, "box-paused")
    assert "boxes 2 of 2 in use" in refused.value.message, "a resume past the quota is refused"

    backend.states.pop("box-running")
    key = await sandbox_quota.reserve_resume(backend, "box-paused")
    assert key is not None
    await sandbox_quota.mark_created("opensandbox", key, "box-paused")
    usage = sandbox_quota._usage(await ledgers.read("opensandbox"))
    assert (usage.boxes, usage.memory) == (2, 2 * _GIB), "the resumed box takes back what it was created with"

    backend.states["box-paused"] = "Running"
    await sandbox_quota.reconcile_sandbox_quota(backend)
    ledger = await ledgers.read("opensandbox")
    assert ledger["reservations"] == [], "the inventory charges the resumed box from then on"
    assert sandbox_quota._usage(ledger).boxes == 2
