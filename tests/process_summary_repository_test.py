from __future__ import annotations

from typing import Any

import pytest

from astrabox.config.settings import get_settings
from astrabox.persistence.repository.backend import get_async_collection
from astrabox.persistence.repository.process_summary_repository import (
    _COLLECTION_NAME,
    ProcessSummaryRepository,
)


@pytest.fixture(autouse=True)
def _isolated_sqlite_state(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ASTRABOX_DB_BACKEND", "sqlite")
    monkeypatch.delenv("ASTRABOX_DB_URL", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_late_finish_from_an_expired_claim_never_overwrites_the_retry() -> None:
    # The first holder claims, its lease lapses while its model call is still
    # out, a reader expires it and retries. The first holder's completion then
    # lands: without the claim token in the finish predicate it would replace
    # the retry's row, and the label readers see would come from the run that
    # already lost its lease.
    repo = ProcessSummaryRepository()
    first = await repo.claim("session", "message", through_seq=5, turn_completed=True)
    assert first is not None
    assert await repo.claim("session", "message", through_seq=5, turn_completed=True) is None

    collection = await get_async_collection(_COLLECTION_NAME)
    await collection.update_one(
        {"_id": "session:message"},
        {"$set": {"expires_at": "2000-01-01T00:00:00+00:00"}},
    )
    assert (await repo.read("session", ["message"]))["message"]["status"] == "failed"

    retry = await repo.claim(
        "session", "message", through_seq=6, turn_completed=True, retry_failed=True
    )
    assert retry is not None and retry != first

    assert await repo.finish(
        "session", "message", {"status": "completed", "summary": "OLD"}, claim_token=first
    ) is False
    assert (await repo.read("session", ["message"]))["message"]["status"] == "generating"

    assert await repo.finish(
        "session", "message", {"status": "completed", "summary": "NEW"}, claim_token=retry
    ) is True
    row = (await repo.read("session", ["message"]))["message"]
    assert (row["status"], row["summary"]) == ("completed", "NEW")

    # A completed row is final: no retry reopens it, and the stale holder's
    # second attempt is refused the same way.
    assert await repo.claim(
        "session", "message", through_seq=7, turn_completed=True, retry_failed=True
    ) is None
    assert await repo.finish(
        "session", "message", {"status": "failed", "error": "late"}, claim_token=first
    ) is False
    assert (await repo.read("session", ["message"]))["message"]["summary"] == "NEW"
