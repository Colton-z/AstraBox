"""Which AstraBox conversation each Hermes cron job reports to.

Hermes stores no creator on a job a TUI session schedules: the job's
``origin`` is filled only from a messaging platform's chat
(``tools/cronjob_job_args.py`` at the pinned commit), and an Assistant
conversation is not one. One row per (Hermes profile, job id) records the
conversation that created the job and the newest run already delivered to it,
so a run is reported once even when several readers see it. The row is
deleted with its conversation.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from astrabox.persistence.sql import execute, fetch_all, from_iso, to_iso
from astrabox.persistence.sql.models.platform import HermesCronJob

_JOBS = HermesCronJob.__table__


def _view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "delivered_run_at": to_iso(row["delivered_run_at"]),
        "created_at": to_iso(row["created_at"]),
    }


class HermesCronJobRepository:
    """The conversation each cron job belongs to, and its delivery watermark."""

    async def claim(self, *, profile_key: str, job_id: str, session_id: str) -> None:
        """Record the conversation that created ``job_id``; the first claim wins."""
        await execute(
            insert(_JOBS)
            .values(profile_key=profile_key, job_id=job_id, session_id=session_id)
            .on_conflict_do_nothing(index_elements=[_JOBS.c.profile_key, _JOBS.c.job_id])
        )

    async def list_for_session(
        self, *, profile_key: str, session_id: str
    ) -> list[dict[str, Any]]:
        rows = await fetch_all(
            select(_JOBS)
            .where(_JOBS.c.session_id == session_id, _JOBS.c.profile_key == profile_key)
            .order_by(_JOBS.c.job_id)
        )
        return [_view(row) for row in rows]

    async def mark_delivered(self, *, profile_key: str, job_id: str, run_at: str) -> bool:
        """Move the job's watermark forward to ``run_at``.

        False when the watermark is already at or past ``run_at``: a reader
        that saw an older run cannot move it back and have a newer run
        delivered again.
        """
        delivered = from_iso(run_at)
        moved = await execute(
            update(_JOBS)
            .where(
                _JOBS.c.profile_key == profile_key,
                _JOBS.c.job_id == job_id,
                or_(_JOBS.c.delivered_run_at.is_(None), _JOBS.c.delivered_run_at < delivered),
            )
            .values(delivered_run_at=delivered)
        )
        return moved == 1

    async def forget(self, *, profile_key: str, job_id: str) -> None:
        await execute(
            delete(_JOBS).where(_JOBS.c.profile_key == profile_key, _JOBS.c.job_id == job_id)
        )
