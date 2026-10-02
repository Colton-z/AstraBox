"""A renewal hold proves the exact overlap without racing the watcher timer."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Iterator

import pytest

from astrabox.common.fault_injection import clear_fault_hooks, pass_fault_barrier
from astrabox.testing import e2e_faults


@pytest.fixture(autouse=True)
def _hooks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ASTRABOX_E2E_FAULTS", "1")
    monkeypatch.setenv("ASTRABOX_E2E_TURN_TERMINAL_DROP_FAULT_FILE", str(tmp_path / "fault.json"))
    clear_fault_hooks()
    yield
    clear_fault_hooks()


def _declare(tmp_path: Path) -> Path:
    path = tmp_path / "fault.json"
    e2e_faults._atomic_write(str(path), {
        "faults": {"hold_prepared_slot_renewal": 1},
        "match": {"agent_id": "agent-1", "slot_id": "slot-1"},
        "release": False,
        "consumed": [],
    })
    return path


@pytest.mark.parametrize("release_by_removal", [False, True])
async def test_exact_renewal_stays_held_until_release(
    tmp_path: Path, release_by_removal: bool,
) -> None:
    path = _declare(tmp_path)
    e2e_faults.install_e2e_fault_hooks()
    blocked = asyncio.create_task(pass_fault_barrier(
        "prepared_slot_renewal", agent_id="agent-1", slot_id="slot-1",
    ))
    try:
        async with asyncio.timeout(2):
            while not (payload := json.loads(path.read_text()))["consumed"]:
                await asyncio.sleep(0.01)
        assert not blocked.done()
        assert payload["consumed"] == [{
            "fault": "hold_prepared_slot_renewal", "agent_id": "agent-1", "slot_id": "slot-1",
        }]
        assert payload["faults"]["hold_prepared_slot_renewal"] == 0
        if release_by_removal:
            path.unlink()
        else:
            e2e_faults._atomic_write(str(path), {**payload, "release": True})
        await asyncio.wait_for(blocked, timeout=2)
        # A later reconciliation cannot inherit this one-use hold.
        await asyncio.wait_for(pass_fault_barrier(
            "prepared_slot_renewal", agent_id="agent-1", slot_id="slot-1",
        ), timeout=2)
    finally:
        path.unlink(missing_ok=True)
        await asyncio.wait_for(blocked, timeout=2)


@pytest.mark.parametrize("agent_id,slot_id", [("other", "slot-1"), ("agent-1", "other")])
async def test_another_agent_or_slot_cannot_consume_the_hold(
    tmp_path: Path, agent_id: str, slot_id: str,
) -> None:
    path = _declare(tmp_path)
    before = path.read_text()
    e2e_faults.install_e2e_fault_hooks()
    await pass_fault_barrier("prepared_slot_renewal", agent_id=agent_id, slot_id=slot_id)
    assert path.read_text() == before


async def test_unarmed_deployment_never_reads_the_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _declare(tmp_path)
    before = path.read_text()
    monkeypatch.delenv("ASTRABOX_E2E_FAULTS")
    e2e_faults.install_e2e_fault_hooks()
    await pass_fault_barrier("prepared_slot_renewal", agent_id="agent-1", slot_id="slot-1")
    assert path.read_text() == before


async def test_unreleased_renewal_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _declare(tmp_path)
    monkeypatch.setattr(e2e_faults, "_HOLD_RELEASE_TIMEOUT_SECONDS", 0.01)
    e2e_faults.install_e2e_fault_hooks()
    with pytest.raises(TimeoutError, match="Agent 'agent-1' slot 'slot-1'"):
        await pass_fault_barrier("prepared_slot_renewal", agent_id="agent-1", slot_id="slot-1")
