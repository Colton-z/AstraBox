"""The E2E turn-frame barrier used by deterministic reconnect coverage."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Iterator

import pytest

from astrabox.common.fault_injection import clear_fault_hooks, pass_fault_barrier
from astrabox.testing import e2e_faults


@pytest.fixture(autouse=True)
def _isolated_fault_hooks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ASTRABOX_E2E_FAULTS", "1")
    monkeypatch.setenv(
        "ASTRABOX_E2E_TURN_TERMINAL_DROP_FAULT_FILE",
        str(tmp_path / "faults.json"),
    )
    clear_fault_hooks()
    yield
    clear_fault_hooks()


def _write(path: Path, payload: dict[str, object]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


async def _wait_until_consumed(path: Path) -> dict[str, object]:
    for _ in range(200):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("consumed"):
            return payload
        await asyncio.sleep(0.01)
    raise AssertionError("turn-frame hold was not consumed")


async def test_matching_frame_holds_until_declaration_releases(tmp_path: Path) -> None:
    declaration = tmp_path / "faults.json"
    _write(
        declaration,
        {
            "faults": {"hold_after_frame": 1},
            "match": {"session_id": "session-1", "frame_type": "text-delta"},
            "release": False,
            "consumed": [],
        },
    )
    e2e_faults.install_e2e_fault_hooks()
    prepared = asyncio.Event()

    async def prepare_hold() -> None:
        prepared.set()

    blocked = asyncio.create_task(
        pass_fault_barrier(
            "turn_frame_processed",
            session_id="session-1",
            frame_type="text-delta",
            prepare_hold=prepare_hold,
        )
    )
    payload = await _wait_until_consumed(declaration)
    await asyncio.wait_for(prepared.wait(), timeout=2)
    assert not blocked.done(), "the bridge must remain held before release"
    assert payload["consumed"] == [
        {
            "fault": "hold_after_frame",
            "session_id": "session-1",
            "frame_type": "text-delta",
        }
    ]

    payload["release"] = True
    _write(declaration, payload)
    await asyncio.wait_for(blocked, timeout=2)


async def test_nonmatching_frame_does_not_consume_or_hold(tmp_path: Path) -> None:
    declaration = tmp_path / "faults.json"
    initial = {
        "faults": {"hold_after_frame": 1},
        "match": {"session_id": "session-1", "frame_type": "text-delta"},
        "release": False,
        "consumed": [],
    }
    _write(declaration, initial)
    e2e_faults.install_e2e_fault_hooks()

    async def unexpected_prepare() -> None:
        raise AssertionError("a nonmatching frame must not prepare the hold")

    await pass_fault_barrier(
        "turn_frame_processed",
        session_id="session-1",
        frame_type="text-start",
        prepare_hold=unexpected_prepare,
    )

    assert json.loads(declaration.read_text(encoding="utf-8")) == initial
