from __future__ import annotations

import asyncio
from typing import Any

import pytest

from astrabox.config.settings import get_settings
from astrabox.persistence.repository import session_event_repository as repository_module
from astrabox.persistence.repository.session_event_repository import (
    COLLECTION_NAME,
    SessionEventRepository,
)
from astrabox.persistence.repository.backend import get_async_collection


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


@pytest.mark.asyncio
async def test_session_event_ranges_seed_from_all_history_and_never_overlap() -> None:
    events = await get_async_collection(COLLECTION_NAME)
    await events.insert_many(
        [
            {
                "session_id": "session-1",
                "event_seq": 1,
                "event_kind": "engine_frame",
                "event_type": "engine.frame",
            },
            {
                "session_id": "session-1",
                "event_seq": 3,
                "event_kind": "stream",
                "event_type": "turn.started",
            },
        ]
    )
    repository = SessionEventRepository()
    await repository.ensure_indexes()
    await repository.ensure_counter_indexes()

    starts = await asyncio.gather(
        repository.allocate_session_frame_seq("session-1", count=2),
        repository.allocate_session_frame_seq("session-1", count=1),
    )
    reserved = [
        set(range(starts[0], starts[0] + 2)),
        {starts[1]},
    ]

    assert reserved[0].isdisjoint(reserved[1])
    assert reserved[0] | reserved[1] == {4, 5, 6}
    assert await repository.get_next_session_frame_seq("session-1") == 7


@pytest.mark.asyncio
async def test_engine_frames_share_order_without_becoming_lifecycle_events() -> None:
    repository = SessionEventRepository()

    opened = await repository.append_event(
        {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "event_type": "turn.started",
        }
    )
    frame_seq = await repository.allocate_session_frame_seq("session-1")
    await repository.append_frame(
        {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "command_id": "command-1",
            "frame_seq": frame_seq,
            "payload": {"type": "text-delta", "delta": "hello"},
        }
    )
    settled = await repository.append_event(
        {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "event_type": "turn.settled",
        }
    )

    assert [opened["event_seq"], frame_seq, settled["event_seq"]] == [1, 2, 3]
    assert [row["event_type"] for row in await repository.list_events("session-1")] == [
        "turn.started",
        "turn.settled",
    ]
    assert await repository.list_frames("session-1", turn_id="turn-1") == [
        {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "command_id": "command-1",
            "scope": "turn",
            "frame_seq": 2,
            "payload": {"type": "text-delta", "delta": "hello"},
        }
    ]

    collection = await get_async_collection(COLLECTION_NAME)
    stored = await collection.find_one(
        {
            "session_id": "session-1",
            "event_kind": "engine_frame",
        }
    )
    assert stored is not None
    assert stored["event_seq"] == 2
    assert "frame_seq" not in stored


@pytest.mark.asyncio
async def test_appended_command_is_immediately_visible_to_worker_lookup() -> None:
    repository = SessionEventRepository()
    command_id = "command-create-1"

    appended = await repository.append_event(
        {
            "session_id": "session-command-1",
            "channel": "command",
            "event_type": "command.accepted",
            "causation_id": command_id,
            "correlation_id": command_id,
            "payload": {"command_type": "CreateSession"},
        }
    )

    stored = await repository.get_command_event(
        "session-command-1",
        command_id=command_id,
    )
    assert stored is not None
    assert stored["event_seq"] == appended["event_seq"]
    assert stored["event_kind"] == "command"


@pytest.mark.asyncio
async def test_engine_frame_replay_is_idempotent_but_conflicts_fail_loud() -> None:
    repository = SessionEventRepository()
    frame = {
        "session_id": "session-1",
        "turn_id": "turn-1",
        "command_id": "command-1",
        "frame_seq": await repository.allocate_session_frame_seq("session-1"),
        "payload": {"type": "finish"},
    }

    await repository.append_frame(frame)
    await repository.append_frame(dict(frame))

    conflicting = {**frame, "payload": {"type": "error"}}
    with pytest.raises(RuntimeError, match="idempotent frame append conflicts"):
        await repository.append_frame(conflicting)


@pytest.mark.asyncio
async def test_event_reads_support_bounded_reverse_message_queries() -> None:
    repository = SessionEventRepository()
    first = await repository.append_event(
        {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "event_type": "command.accepted",
        }
    )
    frame_seq = await repository.allocate_session_frame_seq("session-1")
    await repository.append_frame(
        {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "command_id": "command-1",
            "frame_seq": frame_seq,
            "payload": {"type": "text-delta", "delta": "one"},
        }
    )
    second = await repository.append_event(
        {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "event_type": "turn.completed",
        }
    )
    third = await repository.append_event(
        {
            "session_id": "session-1",
            "turn_id": "turn-2",
            "event_type": "turn.failed",
        }
    )

    rows = await repository.list_events(
        "session-1",
        before_seq=int(third["event_seq"]) + 1,
        event_types={"turn.completed", "turn.failed"},
        newest_first=True,
        limit=2,
    )

    assert [row["event_seq"] for row in rows] == [
        third["event_seq"],
        second["event_seq"],
    ]
    earlier_rows = await repository.list_events(
        "session-1",
        before_seq=int(second["event_seq"]),
        event_types={"command.accepted"},
        newest_first=True,
    )
    assert [row["event_seq"] for row in earlier_rows] == [first["event_seq"]]
    assert await repository.list_frames(
        "session-1",
        turn_ids={"turn-1", "missing"},
        before_seq=int(second["event_seq"]),
        newest_first=True,
    ) == [
        {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "command_id": "command-1",
            "scope": "turn",
            "frame_seq": frame_seq,
            "payload": {"type": "text-delta", "delta": "one"},
        }
    ]


@pytest.mark.asyncio
async def test_frame_scope_is_derived_once_and_queryable() -> None:
    repository = SessionEventRepository()
    await repository.append_frames(
        [
            {
                "session_id": "session-1",
                "turn_id": "turn-1",
                "command_id": "command-1",
                "frame_seq": 1,
                "payload": {"type": "text-delta", "delta": "root"},
            },
            {
                "session_id": "session-1",
                "turn_id": None,
                "scope": "session",
                "command_id": "command-1",
                "frame_seq": 2,
                "payload": {"type": "data-subagent", "transient": True},
            },
        ]
    )

    turn_frames = await repository.list_frames("session-1", scope="turn")
    session_frames = await repository.list_frames("session-1", scope="session")
    assert [row["frame_seq"] for row in turn_frames] == [1]
    assert [row["frame_seq"] for row in session_frames] == [2]

    with pytest.raises(ValueError, match="scope disagrees"):
        await repository.append_frame(
            {
                "session_id": "session-1",
                "turn_id": "turn-2",
                "scope": "session",
                "command_id": "command-2",
                "frame_seq": 3,
                "payload": {"type": "text-delta", "delta": "invalid"},
            }
        )
