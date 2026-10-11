"""The built-in scheduler provider: DBOS Transact, embedded in the server process.

DBOS owns cron dispatch, invocation identity, queueing, step retries, recovery
and the workflow ledger that is the Run ledger. This module adapts its public
API to :class:`astrabox.seams.scheduler.SchedulerProvider`; nothing outside it
sees DBOS's workflow and schedule vocabulary.

DBOS keeps its state in a SQL system database, and this provider uses the
repository's: the ``dbos`` schema of the PostgreSQL database, or the SQLite
file. :meth:`DbosSchedulerProvider.configure` therefore refuses a repository
backend that does not declare ``sql_database_url()``, and the process does not
start.

Properties a deployment has to know about (see ``docs/schedules.md``):

* One daemon thread per enabled schedule in every process, each listing every
  schedule once per polling interval; enabled schedules are capped at
  :data:`MAX_ACTIVE_SCHEDULES`.
* Recovery is scoped to an executor: a restarted process resumes the
  invocations it was running only when it comes back with the same executor id
  (derived from the host name) and the same application version, which DBOS
  derives from the workflow functions' source and its own version. An upgrade
  that changes either leaves the invocations that were in flight at that moment
  unrecovered.
* A due time's id is rendered in the schedule's own timezone.
* A due time is skipped when the database cannot be reached at that moment.
"""

from __future__ import annotations

import asyncio
import socket
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Literal, cast

from dbos import (
    DBOS,
    Queue,
    ScheduleInput,
    SetWorkflowAttributes,
    SetWorkflowID,
    error as dbos_error,
)
from sqlalchemy import make_url

from astrabox.common.logger.logger_factory import get_logger
from astrabox.seams.scheduler import (
    InvalidSchedule,
    RunHandler,
    RunInvocation,
    RunPermanentError,
    RunRecord,
    RunTrigger,
    ScheduleCapacityExceeded,
    ScheduleDefinition,
    SchedulerConfigurationError,
    SchedulerNotStarted,
    SchedulerProperties,
    SchedulerProvider,
    SchedulerUnavailable,
    register_scheduler,
)

logger = get_logger(__name__)

SCHEDULE_NAME_PREFIX = "astrabox-deployment-"
RUN_ID_PREFIX = "sched-"
DEPLOYMENT_RUN_QUEUE_NAME = "astrabox-deployment-runs"
WORKFLOW_NAME = "astrabox.deployment.run"

#: DBOS 2.31 starts one thread per enabled schedule in every process. This cap
#: keeps that per-process cost bounded and explicit.
MAX_ACTIVE_SCHEDULES = 64

#: How often each process re-reads the schedule table: the delay before a new,
#: changed or disabled schedule takes effect.
_POLLING_INTERVAL_SECONDS = 1.0
#: DBOS delays each fire by up to a tenth of its wait, at most ten seconds
#: (``dbos/_scheduler.py``), so a disabled schedule may still fire this late.
_MAX_FIRE_JITTER_SECONDS = 10.0

_DBOS_STATES: dict[str, str] = {
    "ENQUEUED": "QUEUED",
    "DELAYED": "QUEUED",
    "PENDING": "DISPATCHING",
    "SUCCESS": "DISPATCHED",
    "ERROR": "FAILED",
    "MAX_RECOVERY_ATTEMPTS_EXCEEDED": "FAILED",
    "CANCELLED": "CANCELLED",
}

_RUN_QUEUE = Queue(DEPLOYMENT_RUN_QUEUE_NAME)

#: The platform's half of every invocation, installed by ``start``. DBOS is a
#: process-wide runtime, so its workflow functions reach the handler here.
_HANDLER: RunHandler | None = None


def _handler() -> RunHandler:
    if _HANDLER is None:
        raise RuntimeError("the DBOS scheduler provider was not started with a run handler")
    return _HANDLER


def _should_retry_run_step(exc: BaseException) -> bool:
    return not isinstance(exc, RunPermanentError)


def _schedule_name(deployment_id: str) -> str:
    return f"{SCHEDULE_NAME_PREFIX}{str(deployment_id).strip()}"


def _run_prefix(deployment_id: str) -> str:
    return f"{RUN_ID_PREFIX}{_schedule_name(deployment_id)}-"


def _aware_utc(value: datetime) -> datetime:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc)


def _invocation(
    context: Mapping[str, Any], workflow_id: str, scheduled_at: datetime
) -> RunInvocation:
    """Rebuild the seam's invocation from a workflow's recorded inputs.

    The context is the platform's payload with the three keys this provider
    writes into it: ``deployment_id``, ``trigger`` and, for a replay,
    ``replayed_from_run_id``.
    """
    trigger = cast(RunTrigger, str(context.get("trigger") or "schedule"))
    return RunInvocation(
        run_id=workflow_id,
        deployment_id=str(context.get("deployment_id") or ""),
        trigger=trigger,
        payload=dict(context),
        scheduled_for=_aware_utc(scheduled_at) if trigger == "schedule" else None,
        replayed_from_run_id=str(context.get("replayed_from_run_id") or "") or None,
    )


def _workflow_attributes(context: Mapping[str, Any], scheduled_at: datetime) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "deployment_id": str(context.get("deployment_id") or ""),
        "trigger": str(context.get("trigger") or "schedule"),
    }
    if attributes["trigger"] == "schedule":
        attributes["scheduled_for"] = _aware_utc(scheduled_at).isoformat()
    replayed_from = str(context.get("replayed_from_run_id") or "").strip()
    if replayed_from:
        attributes["replayed_from_run_id"] = replayed_from
    return attributes


@DBOS.step(
    retries_allowed=True,
    max_attempts=5,
    interval_seconds=1,
    backoff_rate=2,
    preemptible=True,
    should_retry=_should_retry_run_step,
)
async def _start_deployment_session(
    context: dict[str, Any], workflow_id: str, scheduled_at: datetime
) -> str:
    return await _handler().start_session(_invocation(context, workflow_id, scheduled_at))


@DBOS.step(
    retries_allowed=True,
    max_attempts=5,
    interval_seconds=1,
    backoff_rate=2,
    preemptible=True,
    should_retry=_should_retry_run_step,
)
async def _drive_deployment_turn(
    context: dict[str, Any], workflow_id: str, session_id: str, scheduled_at: datetime
) -> dict[str, str]:
    binding = await _handler().admit_turn(
        _invocation(context, workflow_id, scheduled_at), session_id=session_id
    )
    return {"command_id": binding.command_id, "turn_id": binding.turn_id}


@DBOS.workflow(name=WORKFLOW_NAME)
async def run_deployment_workflow(scheduled_at: datetime, context: dict[str, Any]) -> None:
    """One scheduled, manual or replayed invocation, driven through the handler."""

    workflow_id = str(DBOS.workflow_id or "").strip()
    if not workflow_id:
        raise RuntimeError("DBOS started a Deployment Run without a workflow ID")
    attributes = _workflow_attributes(context, scheduled_at)
    await DBOS.update_workflow_attributes_async(workflow_id, attributes)
    session_id = await _start_deployment_session(context, workflow_id, scheduled_at)
    await DBOS.update_workflow_attributes_async(
        workflow_id, {**attributes, "session_id": session_id}
    )
    result = await _drive_deployment_turn(context, workflow_id, session_id, scheduled_at)
    turn_id = str(result.get("turn_id") or "").strip()
    if turn_id:
        await DBOS.update_workflow_attributes_async(
            workflow_id,
            {**attributes, "session_id": session_id, "turn_id": turn_id},
        )


def _status_dict(status: Any) -> dict[str, Any]:
    return dict(vars(status))


def _record(status: Mapping[str, Any]) -> RunRecord:
    """Translate one DBOS workflow status into the seam's Run record."""

    workflow_input = status.get("input")
    args = workflow_input.get("args") if isinstance(workflow_input, dict) else None
    scheduled_at: datetime | None = None
    context: dict[str, Any] = {}
    if isinstance(args, (list, tuple)):
        if args and isinstance(args[0], datetime):
            scheduled_at = args[0]
        if len(args) > 1 and isinstance(args[1], dict):
            context = dict(args[1])
    attributes = status.get("attributes")
    if not isinstance(attributes, dict):
        attributes = {}
    trigger = cast(
        RunTrigger, str(attributes.get("trigger") or context.get("trigger") or "schedule")
    )
    scheduled_for: datetime | None = None
    recorded = str(attributes.get("scheduled_for") or "").strip()
    if recorded:
        scheduled_for = _aware_utc(datetime.fromisoformat(recorded))
    elif trigger == "schedule" and scheduled_at is not None:
        scheduled_for = _aware_utc(scheduled_at)
    created_at: datetime | None = None
    raw_created = status.get("created_at")
    if raw_created is not None:
        created_at = datetime.fromtimestamp(int(raw_created) / 1000, tz=timezone.utc)
    state = _DBOS_STATES.get(str(status.get("status") or ""), "UNKNOWN")
    return RunRecord(
        invocation=RunInvocation(
            run_id=str(status.get("workflow_id") or ""),
            deployment_id=str(
                attributes.get("deployment_id") or context.get("deployment_id") or ""
            ),
            trigger=trigger,
            payload=context,
            scheduled_for=scheduled_for,
            replayed_from_run_id=str(
                attributes.get("replayed_from_run_id")
                or context.get("replayed_from_run_id")
                or ""
            )
            or None,
        ),
        state=cast(Any, state),
        created_at=created_at,
        session_id=str(attributes.get("session_id") or "") or None,
        turn_id=str(attributes.get("turn_id") or "") or None,
        failed=state == "FAILED",
    )


def _dbos_url(raw_url: str) -> str | None:
    url = make_url(raw_url)
    driver = url.drivername.lower()
    if driver.startswith("postgresql") or driver.startswith("postgres"):
        return url.set(drivername="postgresql+psycopg").render_as_string(hide_password=False)
    if driver.startswith("sqlite"):
        return url.set(drivername="sqlite").render_as_string(hide_password=False)
    return None


class DbosSchedulerProvider(SchedulerProvider):
    """Scheduled Deployments on DBOS, in the repository's SQL database."""

    name = "dbos"
    properties = SchedulerProperties(
        recovery_scope="executor",
        max_active_schedules=MAX_ACTIVE_SCHEDULES,
        pause_visibility_seconds=_POLLING_INTERVAL_SECONDS + _MAX_FIRE_JITTER_SECONDS,
    )

    def __init__(self) -> None:
        self._system_database_url: str | None = None
        self._started = False

    def configure(self, *, repository_backend: Any) -> None:
        sql_database_url = getattr(repository_backend, "sql_database_url", None)
        if not callable(sql_database_url):
            raise SchedulerConfigurationError(
                "the dbos scheduler keeps its schedules and Run ledger in the "
                "repository's SQL database, and this repository backend has none"
            )
        try:
            raw_url = str(sql_database_url())
        except ValueError as exc:
            raise SchedulerConfigurationError(
                f"the dbos scheduler could not resolve the repository's database URL: {exc}"
            ) from exc
        dbos_url = _dbos_url(raw_url)
        if dbos_url is None:
            raise SchedulerConfigurationError(
                "the dbos scheduler supports PostgreSQL and SQLite; the repository "
                f"database uses {make_url(raw_url).drivername!r}"
            )
        self._system_database_url = dbos_url

    async def start(self, handler: RunHandler) -> None:
        global _HANDLER
        if self._started:
            return
        if self._system_database_url is None:
            raise SchedulerConfigurationError(
                "the dbos scheduler was started before configure() bound its database"
            )
        try:
            DBOS(
                config={
                    "name": "astrabox",
                    "system_database_url": self._system_database_url,
                    "dbos_system_schema": "dbos",
                    # One application process per host or container. A stable
                    # host identity lets a restarted process recover only its
                    # own interrupted workflows and keeps a new replica from
                    # taking another live replica's work.
                    "executor_id": f"astrabox-{socket.gethostname()}",
                    "run_admin_server": False,
                    "scheduler_polling_interval_sec": _POLLING_INTERVAL_SECONDS,
                    "log_level": "WARNING",
                    "console_log_level": "WARNING",
                }
            )
            _HANDLER = handler
            # Called on the server's running loop. DBOS adopts it for async
            # workflows, so the handler's repositories never cross loops.
            DBOS.launch()
        except Exception:
            _HANDLER = None
            DBOS.destroy()
            raise
        self._started = True

    async def stop(self) -> None:
        global _HANDLER
        if not self._started:
            return
        try:
            DBOS.destroy(workflow_completion_timeout_sec=5)
        finally:
            self._started = False
            _HANDLER = None

    def _require_started(self) -> None:
        if not self._started:
            raise SchedulerNotStarted("the dbos scheduler is not started in this process")

    @staticmethod
    def _schedule_input(definition: ScheduleDefinition) -> ScheduleInput:
        context = {
            **dict(definition.payload),
            "deployment_id": definition.deployment_id,
            "trigger": "schedule",
        }
        return {
            "schedule_name": _schedule_name(definition.deployment_id),
            "workflow_fn": run_deployment_workflow,
            "schedule": definition.cron,
            "context": context,
            "automatic_backfill": False,
            "cron_timezone": definition.timezone,
            "queue_name": DEPLOYMENT_RUN_QUEUE_NAME,
        }

    @staticmethod
    def _held_input(schedule: dict[str, Any]) -> ScheduleInput:
        """Rebuild a schedule definition exactly as DBOS returned it."""

        return {
            "schedule_name": str(schedule.get("schedule_name") or ""),
            "workflow_fn": run_deployment_workflow,
            "schedule": str(schedule.get("schedule") or ""),
            "context": schedule.get("context"),
            "automatic_backfill": bool(schedule.get("automatic_backfill", False)),
            "cron_timezone": str(schedule.get("cron_timezone") or "") or None,
            "queue_name": str(schedule.get("queue_name") or "") or None,
        }

    async def _restore(self, name: str, previous: dict[str, Any] | None) -> None:
        """Put back the exact definition a failed write replaced."""

        try:
            if previous is None:
                await DBOS.delete_schedule_async(name)
                return
            await DBOS.apply_schedules_async([self._held_input(previous)])
            status_change = (
                DBOS.pause_schedule
                if str(previous.get("status") or "") == "PAUSED"
                else DBOS.resume_schedule
            )
            await asyncio.to_thread(status_change, name)
        except Exception as exc:
            logger.exception("could not restore schedule %s", name)
            raise SchedulerUnavailable(
                "the schedule store could not restore the prior definition"
            ) from exc

    async def _active_schedule_names(self) -> list[str]:
        rows = await DBOS.list_schedules_async(
            status="ACTIVE", schedule_name_prefix=SCHEDULE_NAME_PREFIX
        )
        return [str(dict(row).get("schedule_name") or "") for row in rows]

    async def _ensure_capacity(self, name: str) -> None:
        active = await self._active_schedule_names()
        if name not in active and len(active) >= MAX_ACTIVE_SCHEDULES:
            raise ScheduleCapacityExceeded(MAX_ACTIVE_SCHEDULES)

    async def apply(self, definition: ScheduleDefinition) -> None:
        self._require_started()
        name = _schedule_name(definition.deployment_id)
        previous: dict[str, Any] | None = None
        touched = False
        try:
            existing = await DBOS.get_schedule_async(name)
            previous = dict(existing) if existing is not None else None
            if previous is not None:
                # DBOS keeps a schedule's status across an upsert. Pausing the
                # old definition first means an edit never briefly reopens a
                # disabled schedule; the final pause or resume is explicit.
                touched = True
                await asyncio.to_thread(DBOS.pause_schedule, name)
            if definition.enabled:
                await self._ensure_capacity(name)
            touched = True
            await DBOS.apply_schedules_async([self._schedule_input(definition)])
            status_change = DBOS.resume_schedule if definition.enabled else DBOS.pause_schedule
            await asyncio.to_thread(status_change, name)
            # The capacity check and the upsert are separate calls, so two
            # creators can race past the check; re-count after inserting.
            if (
                definition.enabled
                and len(await self._active_schedule_names()) > MAX_ACTIVE_SCHEDULES
            ):
                touched = False
                await self._restore(name, previous)
                raise ScheduleCapacityExceeded(MAX_ACTIVE_SCHEDULES)
        except (ScheduleCapacityExceeded, SchedulerUnavailable):
            if touched:
                await self._restore(name, previous)
            raise
        except dbos_error.DBOSException as exc:
            if touched:
                await self._restore(name, previous)
            if exc.message.startswith(("Invalid cron schedule:", "Invalid timezone:")):
                raise InvalidSchedule("schedule cron or timezone is invalid") from exc
            raise SchedulerUnavailable("the schedule store is unavailable") from exc
        except Exception as exc:
            if touched:
                await self._restore(name, previous)
            raise SchedulerUnavailable("the schedule store is unavailable") from exc

    async def delete(self, deployment_id: str) -> None:
        self._require_started()
        try:
            await DBOS.delete_schedule_async(_schedule_name(deployment_id))
        except Exception as exc:
            raise SchedulerUnavailable("the schedule store is unavailable") from exc

    async def reconcile(self, definitions: Sequence[ScheduleDefinition]) -> None:
        self._require_started()
        enabled = [definition for definition in definitions if definition.enabled]
        if len(enabled) > MAX_ACTIVE_SCHEDULES:
            raise ScheduleCapacityExceeded(MAX_ACTIVE_SCHEDULES)
        desired = {
            _schedule_name(definition.deployment_id): definition for definition in definitions
        }
        try:
            if desired:
                await DBOS.apply_schedules_async(
                    [self._schedule_input(definition) for definition in desired.values()]
                )
            held = await DBOS.list_schedules_async(schedule_name_prefix=SCHEDULE_NAME_PREFIX)
            for schedule in held:
                name = str(schedule.get("schedule_name") or "")
                if name not in desired:
                    await DBOS.delete_schedule_async(name)
                    continue
                status_change = (
                    DBOS.resume_schedule if desired[name].enabled else DBOS.pause_schedule
                )
                await asyncio.to_thread(status_change, name)
        except dbos_error.DBOSException as exc:
            if exc.message.startswith(("Invalid cron schedule:", "Invalid timezone:")):
                raise InvalidSchedule(
                    f"a stored schedule's cron or timezone is invalid: {exc.message}"
                ) from exc
            raise SchedulerUnavailable(f"could not reconcile schedules: {exc.message}") from exc
        except Exception as exc:
            raise SchedulerUnavailable(f"could not reconcile schedules: {exc}") from exc

    async def enqueue(
        self,
        *,
        deployment_id: str,
        trigger: Literal["manual", "replay"],
        payload: Mapping[str, Any],
        replayed_from_run_id: str | None = None,
    ) -> RunRecord:
        self._require_started()
        run_id = f"{_run_prefix(deployment_id)}{trigger}-{uuid.uuid4().hex}"
        context: dict[str, Any] = {
            **dict(payload),
            "deployment_id": deployment_id,
            "trigger": trigger,
        }
        if replayed_from_run_id:
            context["replayed_from_run_id"] = replayed_from_run_id
        scheduled_at = datetime.now(timezone.utc)
        try:
            with SetWorkflowID(run_id), SetWorkflowAttributes(
                _workflow_attributes(context, scheduled_at)
            ):
                handle = await _RUN_QUEUE.enqueue_async(
                    run_deployment_workflow, scheduled_at, context
                )
            status = await DBOS.get_workflow_status_async(handle.workflow_id)
            if status is None:
                raise RuntimeError(f"the workflow store did not persist Run {run_id}")
        except Exception as exc:
            raise SchedulerUnavailable("the schedule store is unavailable") from exc
        return _record(_status_dict(status))

    async def get_run(self, run_id: str) -> RunRecord | None:
        self._require_started()
        if not str(run_id).startswith(f"{RUN_ID_PREFIX}{SCHEDULE_NAME_PREFIX}"):
            return None
        try:
            status = await DBOS.get_workflow_status_async(run_id)
        except Exception as exc:
            raise SchedulerUnavailable("the schedule store is unavailable") from exc
        if status is None or str(status.name or "") != WORKFLOW_NAME:
            return None
        return _record(_status_dict(status))

    async def list_runs(self, deployment_id: str, *, limit: int) -> Sequence[RunRecord]:
        self._require_started()
        try:
            rows = await DBOS.list_workflows_async(
                name=WORKFLOW_NAME,
                workflow_id_prefix=_run_prefix(deployment_id),
                limit=max(1, min(int(limit), 200)),
                sort_desc=True,
                load_input=True,
            )
        except Exception as exc:
            raise SchedulerUnavailable("the schedule store is unavailable") from exc
        return [_record(_status_dict(row)) for row in rows]


# Consumer-driven registration: importing the module installs the provider.
register_scheduler(DbosSchedulerProvider())


__all__ = [
    "DEPLOYMENT_RUN_QUEUE_NAME",
    "MAX_ACTIVE_SCHEDULES",
    "DbosSchedulerProvider",
    "run_deployment_workflow",
]
