"""Background reconciliation pages a stable journal order across equal timestamps."""

from typing import Any

import pytest

from astrabox.config.settings import get_settings
from astrabox.core.service.orchestrator.session_kernel.service_mixins.background_continuation import (
    BackgroundContinuationMixin,
)
from astrabox.persistence.repository.backend import get_async_collection
from astrabox.persistence.repository.session_event_repository import COLLECTION_NAME


@pytest.fixture
def isolated_store(tmp_path: Any, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ASTRABOX_DB_BACKEND", "sqlite")
    monkeypatch.delenv("ASTRABOX_DB_URL", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_manifest_pages_use_numeric_sequence_and_session_tiebreakers(isolated_store) -> None:
    collection = await get_async_collection(COLLECTION_NAME)
    rows = [
        ("2026-09-30T00:00:02+00:00", 1, "new"),
        ("2026-09-30T00:00:01+00:00", 10, "z"),
        ("2026-09-30T00:00:01+00:00", 10, "a"),
        ("2026-09-30T00:00:01+00:00", 2, "y"),
        ("2026-09-30T00:00:00+00:00", 100, "old"),
    ]
    for occurred_at, event_seq, session_id in reversed(rows):
        await collection.insert_one({
            "_id": session_id, "session_id": session_id,
            "occurred_at": occurred_at, "event_seq": event_seq,
            "channel": "conversation", "event_type": "turn.background_tasks_opened",
        })
    for channel, event_type in [
        ("conversation", "turn.background_tasks_materialized"),
        ("other", "turn.background_tasks_opened"),
    ]:
        await collection.insert_one({
            "_id": channel + event_type, "session_id": "unrelated", "event_seq": 1000,
            "occurred_at": rows[0][0], "channel": channel, "event_type": event_type,
        })
    scanner = BackgroundContinuationMixin()
    first = await scanner._list_background_task_opened_events(limit=2)
    assert [row["session_id"] for row in first] == ["new", "z"]
    await collection.delete_one({"_id": "z"})
    await collection.insert_one({
        "_id": "inserted", "session_id": "inserted", "event_seq": 1,
        "occurred_at": "2026-09-30T00:00:03+00:00",
        "channel": "conversation", "event_type": "turn.background_tasks_opened",
    })
    second = await scanner._list_background_task_opened_events(limit=2, before=rows[1])
    assert [row["session_id"] for row in second] == ["a", "y"]
    last = await scanner._list_background_task_opened_events(limit=2, before=rows[3])
    assert [row["session_id"] for row in last] == ["old"]
    assert await scanner._list_background_task_opened_events(limit=2, before=rows[4]) == []
