"""A Hermes cron job's delivery watermark only advances."""

from __future__ import annotations

import pytest

from astrabox.persistence.repository.hermes_cron_repository import HermesCronJobRepository
from astrabox.persistence.repository.session_repository import SessionRepository

pytestmark = pytest.mark.usefixtures("database")


async def _conversation(session_id: str) -> None:
    await SessionRepository().create_session(
        {"session_id": session_id, "user_id": "u1", "session_kind": "agent_chat", "state": "READY"}
    )


async def test_a_reader_that_saw_an_older_run_cannot_move_the_watermark_back() -> None:
    repo = HermesCronJobRepository()
    await _conversation("s-1")
    await repo.claim(profile_key="profile", job_id="job-1", session_id="s-1")

    # Hermes writes run times with its configured zone's offset: 09:00-07:00
    # is 16:00 UTC, so the 15:30 UTC run another reader saw is the older one.
    assert await repo.mark_delivered(
        profile_key="profile", job_id="job-1", run_at="2026-10-14T09:00:00-07:00"
    )
    assert not await repo.mark_delivered(
        profile_key="profile", job_id="job-1", run_at="2026-10-14T15:30:00+00:00"
    )

    [row] = await repo.list_for_session(profile_key="profile", session_id="s-1")
    assert row["delivered_run_at"] == "2026-10-14T16:00:00+00:00"
