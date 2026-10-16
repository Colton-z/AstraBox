"""Report a conversation's own Hermes cron runs back into that conversation.

Hermes runs a scheduled job in its gateway process, in a session of its own,
and delivers the result only to a messaging platform the gateway connects to
(``cron/scheduler_delivery.py`` at the pinned commit). An Assistant
conversation reaches Hermes through ``hermes serve`` instead, so Hermes keeps
the result in ``$HERMES_HOME/cron/output/<job_id>/`` and nothing reaches the
conversation that asked for the job. This module closes that gap without
platform credentials in the box:

1. When the conversation's own stream shows ``cronjob_manage`` creating a job,
   the job is recorded as that conversation's
   (:class:`~astrabox.persistence.repository.hermes_cron_repository.HermesCronJobRepository`).
2. Hermes broadcasts ``cron.changed`` whenever its job store changes, which
   every run does; a periodic check covers a missed broadcast and runs that
   finished while no reader was attached.
3. Each run of an owned job newer than the delivered watermark is reported
   to the conversation's runtime client, which carries it as one
   engine-originated response (``engine/hermes_runtime_client.py``), so the
   web view and every attached channel receive it like any other reply. The
   watermark moves only once the conversation has recorded that response. A
   successful run whose response is Hermes' quiet marker (``[SILENT]``),
   which a job answers when it has nothing worth reporting, is not reported,
   as Hermes' scheduler would not deliver it; the run still moves the
   watermark.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.time_utils import parse_iso
from astrabox.persistence.repository.hermes_cron_repository import (
    HermesCronJobRepository,
)

logger = get_logger(__name__)

HERMES_CRON_TOOL = "cronjob_manage"
#: How often a reader checks its jobs without a ``cron.changed`` broadcast.
CRON_CHECK_INTERVAL_SECONDS = 60.0
#: The longest result text published for one run.
CRON_RESULT_MAX_CHARS = 20_000


def created_job_id(event: dict[str, Any]) -> str | None:
    """The job id a ``tool.complete`` reports for a successful job creation."""

    if str(event.get("type") or "") != "tool.complete":
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict) or payload.get("name") != HERMES_CRON_TOOL:
        return None
    args = payload.get("args")
    action = str((args or {}).get("action") or "").strip().lower() if isinstance(args, dict) else ""
    if action not in {"create", "add"}:
        return None
    result = payload.get("result")
    if not isinstance(result, dict) or result.get("success") is False:
        return None
    job_id = str(result.get("job_id") or "").strip()
    return job_id or None


@dataclass(frozen=True)
class CronRunOutput:
    """The newest run Hermes saved for a job, read in the box."""

    #: The saved run file.
    output: str
    #: Whether its response is Hermes' quiet marker, by Hermes' own rule.
    silent: bool


def run_report_text(output: str) -> str:
    """The part of a Hermes run output file a reader should see.

    The file is a Markdown header (job, run time, schedule, prompt) followed
    by ``## Response`` (``cron/scheduler.py`` at the pinned commit); a failed
    run carries its error in place of a response.
    """

    text = output.strip()
    marker = "## Response"
    if marker in text:
        text = text.split(marker, 1)[1].strip()
    if len(text) > CRON_RESULT_MAX_CHARS:
        text = text[:CRON_RESULT_MAX_CHARS].rstrip() + "\n\n…"
    return text


@dataclass(frozen=True)
class CronRunReport:
    """One run of a conversation's cron job, due to be reported into it."""

    job_id: str
    run_at: str
    name: str | None
    #: Hermes' own word for how the run went (``last_status`` of the job).
    status: str | None
    text: str
    failed: bool

    @property
    def response_id(self) -> str:
        """The conversation response that reports this run; one per run."""

        return f"hermes-cron:{self.job_id}:{self.run_at}"


class HermesCronWatch:
    """Find the due runs of one conversation's own cron jobs while its reader is attached.

    ``report`` receives each due run once per watch. :meth:`delivered` moves
    the watermark after the conversation recorded the run, so a reader that
    stops before recording it leaves the run to the next one.
    """

    def __init__(
        self,
        *,
        profile_key: str,
        session_id: str,
        store: HermesCronJobRepository,
        list_jobs: Callable[[], Awaitable[list[dict[str, Any]]]],
        read_run_output: Callable[[str], Awaitable[CronRunOutput | None]],
        report: Callable[[CronRunReport], None],
    ) -> None:
        self._profile_key = profile_key
        self._session_id = session_id
        self._store = store
        self._list_jobs = list_jobs
        self._read_run_output = read_run_output
        self._report = report
        self._reported: set[tuple[str, str]] = set()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._wake.set()
            self._task = asyncio.create_task(
                self._run(), name=f"hermes-cron-watch-{self._session_id}"
            )

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task})

    async def observe(self, event: dict[str, Any]) -> None:
        """Take what one event of the conversation's stream says about cron."""

        try:
            job_id = created_job_id(event)
            if job_id is not None:
                await self._store.claim(
                    profile_key=self._profile_key, job_id=job_id, session_id=self._session_id
                )
                self._wake.set()
            elif str(event.get("type") or "") == "cron.changed":
                self._wake.set()
        except Exception as exc:  # noqa: BLE001 — the stream must keep flowing
            logger.warning(
                "Hermes cron ownership record failed: session=%s error=%s",
                self._session_id,
                exc,
            )

    async def delivered(self, report: CronRunReport) -> None:
        """The conversation recorded ``report``: move the job's watermark past it."""

        await self._store.mark_delivered(
            profile_key=self._profile_key, job_id=report.job_id, run_at=report.run_at
        )
        logger.info(
            "Hermes cron run delivered: session=%s job=%s run_at=%s failed=%s",
            self._session_id,
            report.job_id,
            report.run_at,
            report.failed,
        )

    async def _run(self) -> None:
        while True:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=CRON_CHECK_INTERVAL_SECONDS)
            self._wake.clear()
            try:
                await self.report_due()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — retried on the next wake
                logger.warning(
                    "Hermes cron delivery check failed: session=%s error=%s",
                    self._session_id,
                    exc,
                )

    async def report_due(self) -> None:
        """Report every owned job's run newer than its watermark."""

        owned = await self._store.list_for_session(
            profile_key=self._profile_key, session_id=self._session_id
        )
        if not owned:
            return
        jobs = {str(job.get("job_id") or ""): job for job in await self._list_jobs()}
        for row in owned:
            job_id = str(row.get("job_id") or "")
            job = jobs.get(job_id)
            if job is None:
                # Removed from Hermes: nothing will run under this id again.
                await self._store.forget(profile_key=self._profile_key, job_id=job_id)
                continue
            run_at = str(job.get("last_run_at") or "").strip()
            if not run_at or (job_id, run_at) in self._reported:
                continue
            # Hermes writes ``last_run_at`` with its configured zone's offset
            # and the watermark is stored in UTC, so they compare as times.
            delivered = row.get("delivered_run_at")
            if delivered and parse_iso(run_at) <= parse_iso(delivered):
                continue
            report = await self._read(job, run_at)
            if report is None:
                await self._store.mark_delivered(
                    profile_key=self._profile_key, job_id=job_id, run_at=run_at
                )
                continue
            self._reported.add((job_id, run_at))
            self._report(report)

    async def _read(self, job: dict[str, Any], run_at: str) -> CronRunReport | None:
        """The report of one run, or ``None`` when there is nothing to report."""

        job_id = str(job.get("job_id") or "")
        run = await self._read_run_output(job_id)
        status = str(job.get("last_status") or "").strip() or None
        failed = (status or "").lower() == "error"
        if run is not None and run.silent and not failed:
            return None
        text = run_report_text(run.output if run else "") or str(job.get("last_error") or "").strip()
        if not text:
            return None
        name = job.get("name")
        return CronRunReport(
            job_id=job_id,
            run_at=run_at,
            name=str(name) if name else None,
            status=status,
            text=text,
            failed=failed,
        )
