"""The built-in scheduler provider: DBOS Transact, embedded in the server process.

DBOS owns cron dispatch, invocation identity, queueing, step retries, recovery
and the workflow ledger that is the Run ledger. This module adapts its public
API to :class:`astrabox.seams.scheduler.SchedulerProvider`; nothing outside it
sees DBOS's workflow and schedule vocabulary.

DBOS keeps its state in the ``dbos`` schema of the platform's PostgreSQL
database. Configuration refuses another database dialect. Native applications
and queues identify execution targets. A process without a worker uses
``DBOSClient`` for control operations and launches no DBOS runtime.

Properties a deployment has to know about (see ``docs/schedules.md``):

* One daemon thread per enabled schedule in every worker process of its target,
  listing that target's schedules once per polling interval; enabled schedules are capped at
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
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Literal, cast

from dbos import (
    DBOS,
    DBOSClient,
    ClientScheduleInput,
    EnqueueOptions,
    Queue,
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

_WORKER_EXECUTION_TARGET: str | None = None


def _queue_name(execution_target: str) -> str:
    return f"{execution_target}-deployment-runs"


@lru_cache(maxsize=None)
def _declare_run_queue(execution_target: str) -> Queue:
    # DBOS keeps declarations through destroy(); each name can be declared once.
    return Queue(_queue_name(execution_target), application_name=execution_target)


def _worker_execution_target() -> str:
    if _WORKER_EXECUTION_TARGET is None:
        raise RuntimeError("DBOS has no executing application target")
    return _WORKER_EXECUTION_TARGET


def _record_execution_target(status: Mapping[str, Any]) -> str:
    owner = status.get("application_name")
    if not isinstance(owner, str) or not owner:
        raise SchedulerUnavailable("workflow store omitted its execution owner")
    return owner

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
        execution_target=_worker_execution_target(),
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
async def _admit_deployment_input(
    context: dict[str, Any], workflow_id: str, session_id: str, scheduled_at: datetime
) -> dict[str, str]:
    binding = await _handler().admit_input(
        _invocation(context, workflow_id, scheduled_at), session_id=session_id
    )
    return {"input_id": binding.input_id}


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
    result = await _admit_deployment_input(context, workflow_id, session_id, scheduled_at)
    input_id = str(result.get("input_id") or "").strip()
    if input_id:
        await DBOS.update_workflow_attributes_async(
            workflow_id,
            {**attributes, "session_id": session_id, "input_id": input_id},
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
            execution_target=_record_execution_target(status),
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
        input_id=str(attributes.get("input_id") or "") or None,
        failed=state == "FAILED",
    )


def _dbos_url(raw_url: str) -> str | None:
    url = make_url(raw_url)
    driver = url.drivername.lower()
    if driver.startswith("postgresql") or driver.startswith("postgres"):
        return url.set(drivername="postgresql+psycopg").render_as_string(hide_password=False)
    return None


class DbosSchedulerProvider(SchedulerProvider):
    """DBOS-owned dispatch with native clients for control operations across targets."""

    name = "dbos"
    properties = SchedulerProperties(
        recovery_scope="executor",
        max_active_schedules=MAX_ACTIVE_SCHEDULES,
        pause_visibility_seconds=_POLLING_INTERVAL_SECONDS + _MAX_FIRE_JITTER_SECONDS,
    )

    def __init__(self) -> None:
        self._system_database_url: str | None = None
        self._execution_target: str | None = None
        self._clients: dict[str, DBOSClient] = {}
        self._client_lock = asyncio.Lock()
        self._started = False
        self._worker_started = False

    @staticmethod
    def _validate_target(execution_target: str) -> None:
        # DBOS2.31 application's native name contract (dbos/_dbos_config.py).
        if re.fullmatch(r"[a-z0-9_-]{3,30}", execution_target) is None:
            raise SchedulerConfigurationError(
                "DBOS execution targets require 3–30 lowercase letters, digits, underscores or hyphens"
            )

    def configure(self, *, database_url: str, execution_target: str) -> None:
        self._validate_target(execution_target)
        if self._execution_target is not None and self._execution_target != execution_target:
            raise SchedulerConfigurationError("a process cannot switch DBOS execution targets")
        dbos_url = _dbos_url(database_url)
        if dbos_url is None:
            raise SchedulerConfigurationError(
                "the dbos scheduler keeps its schedules and Run ledger in PostgreSQL; "
                f"the database URL uses {make_url(database_url).drivername!r}"
            )
        self._system_database_url = dbos_url
        self._execution_target = execution_target

    async def _client(self, execution_target: str) -> DBOSClient:
        self._validate_target(execution_target)
        if self._system_database_url is None:
            raise SchedulerNotStarted("the dbos scheduler has no configured system database")
        async with self._client_lock:
            client = self._clients.get(execution_target)
            if client is None:
                client = await asyncio.to_thread(
                    DBOSClient,
                    system_database_url=self._system_database_url,
                    dbos_system_schema="dbos",
                    application_name=execution_target,
                    retry_connection_errors=False,
                )
                self._clients[execution_target] = client
            return client

    async def start(self, handler: RunHandler | None) -> None:
        global _HANDLER, _WORKER_EXECUTION_TARGET
        if self._started:
            return
        if self._system_database_url is None or self._execution_target is None:
            raise SchedulerConfigurationError("the dbos scheduler was started before configure()")
        try:
            if handler is not None:
                _declare_run_queue(self._execution_target)
                DBOS(config={
                    "name": self._execution_target,
                    "system_database_url": self._system_database_url,
                    "dbos_system_schema": "dbos",
                    "executor_id": f"{self._execution_target}-{socket.gethostname()}",
                    "run_admin_server": False,
                    "scheduler_polling_interval_sec": _POLLING_INTERVAL_SECONDS,
                    "log_level": "WARNING",
                    "console_log_level": "WARNING",
                })
                _HANDLER = handler
                _WORKER_EXECUTION_TARGET = self._execution_target
                # Launch on the server loop; DBOS owns execution and native schema migrations.
                DBOS.launch()
                self._worker_started = True
            # A control-only client never launches workflows or migrates the native store.
            await self._client(self._execution_target)
        except Exception:
            if handler is not None:
                _HANDLER = None
                _WORKER_EXECUTION_TARGET = None
                DBOS.destroy()
                self._worker_started = False
            raise
        self._started = True

    async def stop(self) -> None:
        global _HANDLER, _WORKER_EXECUTION_TARGET
        if not self._started:
            return
        try:
            if self._worker_started:
                DBOS.destroy(workflow_completion_timeout_sec=5)
        finally:
            if self._worker_started:
                _HANDLER = None
                _WORKER_EXECUTION_TARGET = None
            self._started = False
            self._worker_started = False
            clients, self._clients = self._clients, {}
            for client in clients.values():
                await asyncio.to_thread(client.destroy)

    def _require_started(self) -> None:
        if not self._started:
            raise SchedulerNotStarted("the dbos scheduler control client is not started")

    @staticmethod
    def _schedule_input(definition: ScheduleDefinition) -> ClientScheduleInput:
        return {
            "schedule_name": _schedule_name(definition.deployment_id),
            "workflow_name": WORKFLOW_NAME,
            "schedule": definition.cron,
            "context": {**dict(definition.payload), "deployment_id": definition.deployment_id,
                        "trigger": "schedule"},
            "automatic_backfill": False,
            "cron_timezone": definition.timezone,
            "queue_name": _queue_name(definition.execution_target),
            "application_name": definition.execution_target,
        }

    @staticmethod
    def _held_input(schedule: dict[str, Any]) -> ClientScheduleInput:
        """Rebuild the native schedule including its recorded application owner."""
        return {
            "schedule_name": str(schedule["schedule_name"]),
            "workflow_name": str(schedule["workflow_name"]),
            "schedule": str(schedule["schedule"]),
            "context": schedule.get("context"),
            "automatic_backfill": bool(schedule.get("automatic_backfill", False)),
            "cron_timezone": str(schedule.get("cron_timezone") or "") or None,
            "queue_name": str(schedule.get("queue_name") or "") or None,
            "application_name": _record_execution_target(schedule),
        }

    async def _restore(
        self, name: str, previous: dict[str, Any] | None, *, execution_target: str,
    ) -> None:
        try:
            client = await self._client(execution_target)
            if previous is None:
                await client.delete_schedule_async(name)
                return
            await client.apply_schedules_async([self._held_input(previous)])
            change = client.pause_schedule if previous.get("status") == "PAUSED" else client.resume_schedule
            await asyncio.to_thread(change, name)
        except Exception as exc:
            raise SchedulerUnavailable("could not restore the stored schedule") from exc

    async def _active_schedule_names(self, execution_target: str) -> list[str]:
        client = await self._client(execution_target)
        rows = await client.list_schedules_async(
            status="ACTIVE", schedule_name_prefix=SCHEDULE_NAME_PREFIX,
            application_name=execution_target,
        )
        return [str(row["schedule_name"]) for row in rows]

    async def _ensure_capacity(self, name: str, execution_target: str) -> None:
        active = await self._active_schedule_names(execution_target)
        if name not in active and len(active) >= MAX_ACTIVE_SCHEDULES:
            raise ScheduleCapacityExceeded(MAX_ACTIVE_SCHEDULES)

    async def apply(self, definition: ScheduleDefinition) -> None:
        self._require_started()
        name = _schedule_name(definition.deployment_id)
        target = definition.execution_target
        previous: dict[str, Any] | None = None
        touched = False
        try:
            client = await self._client(target)
            existing = await client.get_schedule_async(name)
            previous = dict(existing) if existing is not None else None
            if previous is not None:
                if _record_execution_target(previous) != target:
                    raise SchedulerUnavailable("stored schedule belongs to another execution target")
                touched = True
                await asyncio.to_thread(client.pause_schedule, name)
            if definition.enabled:
                await self._ensure_capacity(name, target)
            touched = True
            await client.apply_schedules_async([self._schedule_input(definition)])
            change = client.resume_schedule if definition.enabled else client.pause_schedule
            await asyncio.to_thread(change, name)
            if definition.enabled and len(await self._active_schedule_names(target)) > MAX_ACTIVE_SCHEDULES:
                touched = False
                await self._restore(name, previous, execution_target=target)
                raise ScheduleCapacityExceeded(MAX_ACTIVE_SCHEDULES)
        except (ScheduleCapacityExceeded, SchedulerUnavailable):
            if touched:
                await self._restore(name, previous, execution_target=target)
            raise
        except dbos_error.DBOSException as exc:
            if touched:
                await self._restore(name, previous, execution_target=target)
            if exc.message.startswith(("Invalid cron schedule:", "Invalid timezone:")):
                raise InvalidSchedule("schedule cron or timezone is invalid") from exc
            raise SchedulerUnavailable("the schedule store is unavailable") from exc
        except Exception as exc:
            if touched:
                await self._restore(name, previous, execution_target=target)
            raise SchedulerUnavailable("the schedule store is unavailable") from exc

    async def delete(self, deployment_id: str, *, execution_target: str) -> None:
        self._require_started()
        try:
            client = await self._client(execution_target)
            name = _schedule_name(deployment_id)
            held = await client.get_schedule_async(name)
            if held is not None and _record_execution_target(held) != execution_target:
                raise SchedulerUnavailable("stored schedule belongs to another execution target")
            await client.delete_schedule_async(name)
        except SchedulerUnavailable:
            raise
        except Exception as exc:
            raise SchedulerUnavailable("the schedule store is unavailable") from exc

    async def reconcile(self, definitions: Sequence[ScheduleDefinition]) -> None:
        self._require_started()
        target = self._execution_target
        if not self._worker_started or target is None:
            raise SchedulerNotStarted("a control-only client cannot reconcile worker schedules")
        if any(d.execution_target != target for d in definitions):
            raise SchedulerUnavailable("worker reconciliation contains another execution target")
        enabled = [d for d in definitions if d.enabled]
        if len(enabled) > MAX_ACTIVE_SCHEDULES:
            raise ScheduleCapacityExceeded(MAX_ACTIVE_SCHEDULES)
        desired = {_schedule_name(d.deployment_id): d for d in definitions}
        try:
            client = await self._client(target)
            if desired:
                await client.apply_schedules_async([self._schedule_input(d) for d in desired.values()])
            held = await client.list_schedules_async(
                schedule_name_prefix=SCHEDULE_NAME_PREFIX, application_name=target,
            )
            for schedule in held:
                name = str(schedule["schedule_name"])
                if name not in desired:
                    await client.delete_schedule_async(name)
                    continue
                change = client.resume_schedule if desired[name].enabled else client.pause_schedule
                await asyncio.to_thread(change, name)
        except dbos_error.DBOSException as exc:
            if exc.message.startswith(("Invalid cron schedule:", "Invalid timezone:")):
                raise InvalidSchedule("a stored schedule's cron or timezone is invalid") from exc
            raise SchedulerUnavailable("could not reconcile stored schedules") from exc
        except Exception as exc:
            raise SchedulerUnavailable("could not reconcile stored schedules") from exc

    async def enqueue(
        self, *, deployment_id: str, execution_target: str,
        trigger: Literal["manual", "replay"], payload: Mapping[str, Any],
        replayed_from_run_id: str | None = None,
    ) -> RunRecord:
        self._require_started()
        run_id = f"{_run_prefix(deployment_id)}{trigger}-{uuid.uuid4().hex}"
        context = {**dict(payload), "deployment_id": deployment_id, "trigger": trigger}
        if replayed_from_run_id:
            context["replayed_from_run_id"] = replayed_from_run_id
        scheduled_at = datetime.now(timezone.utc)
        options: EnqueueOptions = {
            "workflow_id": run_id, "workflow_name": WORKFLOW_NAME,
            "queue_name": _queue_name(execution_target), "application_name": execution_target,
            "attributes": _workflow_attributes(context, scheduled_at),
        }
        try:
            client = await self._client(execution_target)
            handle: Any = await client.enqueue_async(options, scheduled_at, context)
            status = await handle.get_status()
        except Exception as exc:
            raise SchedulerUnavailable("the schedule store is unavailable") from exc
        return _record(_status_dict(status))

    async def get_run(self, run_id: str, *, execution_target: str) -> RunRecord | None:
        self._require_started()
        if not str(run_id).startswith(f"{RUN_ID_PREFIX}{SCHEDULE_NAME_PREFIX}"):
            return None
        try:
            client = await self._client(execution_target)
            handle: Any = await client.retrieve_workflow_async(run_id)
            status = await handle.get_status()
        except dbos_error.DBOSNonExistentWorkflowError:
            return None
        except Exception as exc:
            raise SchedulerUnavailable("the schedule store is unavailable") from exc
        if str(status.name or "") != WORKFLOW_NAME:
            return None
        record = _record(_status_dict(status))
        return record if record.invocation.execution_target == execution_target else None

    async def list_runs(
        self, deployment_id: str, *, execution_target: str, limit: int,
    ) -> Sequence[RunRecord]:
        self._require_started()
        try:
            client = await self._client(execution_target)
            rows = await client.list_workflows_async(
                name=WORKFLOW_NAME, workflow_id_prefix=_run_prefix(deployment_id),
                application_name=execution_target, limit=max(1, min(int(limit), 200)),
                sort_desc=True, load_input=True,
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
