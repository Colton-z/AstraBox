"""A process summary's claim token fences a late finish from an expired claim."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import update

from astrabox.persistence.repository.process_summary_repository import ProcessSummaryRepository
from astrabox.persistence.sql import execute
from astrabox.persistence.sql.models.sessions import ProcessSummary
from tests._claude_session import create_session

pytestmark = pytest.mark.usefixtures("database")


async def test_a_late_finish_from_an_expired_claim_never_overwrites_the_retry() -> None:
    # The first writer's lease lapses while its model call is still out; a
    # page retries. The first writer's outcome then lands: without the claim
    # token in the finish condition it would replace the retry's label.
    session_id = await create_session()
    repo = ProcessSummaryRepository()
    first = await repo.claim(session_id, "r1", through_seq=5, response_completed=True)
    assert first is not None
    assert await repo.claim(session_id, "r1", through_seq=5, response_completed=True) is None
    await execute(update(ProcessSummary.__table__).values(expires_at=datetime(2000, 1, 1, tzinfo=timezone.utc)))
    assert (await repo.read(session_id, ["r1"]))["r1"]["status"] == "failed"

    retry = await repo.claim(session_id, "r1", through_seq=6, response_completed=True, retry_failed=True)
    assert retry is not None and retry != first
    assert not await repo.finish(session_id, "r1", claim_token=first, status="completed", summary="OLD", error=None)
    assert await repo.finish(session_id, "r1", claim_token=retry, status="completed", summary="NEW", error=None)

    assert (await repo.read(session_id, ["r1"]))["r1"]["summary"] == "NEW"
    assert await repo.claim(session_id, "r1", through_seq=7, response_completed=True, retry_failed=True) is None
