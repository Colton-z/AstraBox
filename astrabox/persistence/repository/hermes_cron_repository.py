"""Which AstraBox conversation each Hermes cron job reports to.

Hermes stores no creator on a job a TUI session schedules: the job's
``origin`` is filled only from a messaging platform's chat
(``tools/cronjob_job_args.py`` at the pinned commit), and an Assistant
conversation is not one. One document per (Hermes profile, job id) records
the conversation that created the job and the last run already delivered to
it, so a run is reported once even when several readers see it.
"""

from __future__ import annotations

from typing import Any

from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.persistence.repository.backend import (
    get_async_collection,
    run_mongo_with_retry,
)

HERMES_CRON_JOBS_COLLECTION = "hermes_cron_jobs"


def _job_doc_id(profile_key: str, job_id: str) -> str:
    return f"{profile_key}|{job_id}"


class HermesCronJobRepository:
    """The conversation each cron job belongs to, and its delivery watermark."""

    async def claim(self, *, profile_key: str, job_id: str, session_id: str) -> None:
        """Record the conversation that created ``job_id``; the first claim wins."""

        collection = await get_async_collection(HERMES_CRON_JOBS_COLLECTION)
        await run_mongo_with_retry(
            "hermes_cron_jobs.claim",
            lambda: collection.update_one(
                {"_id": _job_doc_id(profile_key, job_id)},
                {
                    "$setOnInsert": {
                        "profile_key": profile_key,
                        "job_id": job_id,
                        "session_id": session_id,
                        "delivered_run_at": None,
                        "created_at": utcnow_iso(),
                    }
                },
                upsert=True,
            ),
        )

    async def list_for_session(
        self, *, profile_key: str, session_id: str
    ) -> list[dict[str, Any]]:
        collection = await get_async_collection(HERMES_CRON_JOBS_COLLECTION)

        async def _list() -> list[dict[str, Any]]:
            cursor = collection.find({"profile_key": profile_key, "session_id": session_id})
            return [row async for row in cursor]

        return await run_mongo_with_retry("hermes_cron_jobs.list_for_session", _list)

    async def mark_delivered(
        self, *, profile_key: str, job_id: str, run_at: str
    ) -> None:
        collection = await get_async_collection(HERMES_CRON_JOBS_COLLECTION)
        await run_mongo_with_retry(
            "hermes_cron_jobs.mark_delivered",
            lambda: collection.update_one(
                {"_id": _job_doc_id(profile_key, job_id)},
                {"$set": {"delivered_run_at": run_at}},
            ),
        )

    async def forget(self, *, profile_key: str, job_id: str) -> None:
        collection = await get_async_collection(HERMES_CRON_JOBS_COLLECTION)
        await run_mongo_with_retry(
            "hermes_cron_jobs.forget",
            lambda: collection.delete_one({"_id": _job_doc_id(profile_key, job_id)}),
        )
