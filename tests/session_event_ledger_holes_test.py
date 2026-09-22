"""Sequence holes in the durable ledger are permanent, and readers scan past.

``allocate_session_frame_seq`` reserves a number before any row is inserted;
a crash between the two leaves a hole no writer will ever fill — the counter
has already moved on. Every reader is therefore an after-scan and must treat
a hole as dead air, never as an in-flight row worth waiting for: a reader
that waits on a hole reintroduces the reconcile-livelock class. The existing
readers behave that way only incidentally; this file turns the coincidence
into a stated contract.
"""

from __future__ import annotations

from typing import Any

import pytest

from astrabox.config.settings import get_settings
from astrabox.persistence.repository import session_event_repository as repository_module
from astrabox.persistence.repository.session_event_repository import (
    SessionEventRepository,
)


@pytest.fixture(autouse=True)
def _isolated_sqlite_state(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ASTRABOX_DB_BACKEND", "sqlite")
    monkeypatch.delenv("ASTRABOX_DB_URL", raising=False)
    monkeypatch.setattr(repository_module, "_index_ready", False)
    monkeypatch.setattr(repository_module, "_counter_index_ready", False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _frame(seq: int) -> dict[str, Any]:
    return {
        "session_id": "session-1",
        "turn_id": "turn-1",
        "command_id": "command-1",
        "frame_seq": seq,
        "payload": {"type": "text-delta", "delta": f"frame-{seq}"},
    }


@pytest.mark.asyncio
async def test_readers_scan_past_a_permanent_allocation_hole() -> None:
    repository = SessionEventRepository()

    first = await repository.allocate_session_frame_seq("session-1")
    await repository.append_frame(_frame(first))
    second = await repository.allocate_session_frame_seq("session-1")
    await repository.append_frame(_frame(second))
    # The crash window: two sequences reserved, never inserted. Nothing will
    # ever fill them — the counter only moves forward.
    burned = await repository.allocate_session_frame_seq("session-1", count=2)
    survivor = await repository.allocate_session_frame_seq("session-1")
    await repository.append_frame(_frame(survivor))

    assert [first, second] == [1, 2]
    assert burned == 3
    assert survivor == 5

    # A full scan crosses the hole: it neither truncates the list at the gap
    # nor blocks on it.
    frames = await repository.list_frames("session-1")
    assert [row["frame_seq"] for row in frames] == [1, 2, 5]

    # A resuming reader positioned just before the hole lands on the first
    # row past it — the hole reads as dead air, not as pending work.
    resumed = await repository.list_frames("session-1", after_seq=second)
    assert [row["frame_seq"] for row in resumed] == [5]

    # The counter never re-offers the burned numbers, so the hole is
    # permanent by construction, not by luck.
    assert await repository.get_next_session_frame_seq("session-1") == 6
