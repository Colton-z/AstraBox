"""DBOS-backed execution projection for scheduled Deployment Runs.

This module is deliberately an adapter, not another scheduler. DBOS owns cron
dispatch, distributed invocation identity, queueing, retries, recovery, and the
workflow ledger. AstraBox owns the public Deployment configuration and maps the
ledger back to its Run resource.
"""

from __future__ import annotations

import asyncio
import socket
import uuid
from datetime import datetime, timezone
from typing import Any

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
from astrabox.common.utils.errors import APIError
from astrabox.config.settings import get_settings


logger = get_logger(__name__)

SCHEDULE_NAME_PREFIX = "astrabox-deployment-"
RUN_ID_PREFIX = "sched-"
DEPLOYMENT_RUN_QUEUE_NAME = "astrabox-deployment-runs"

# DBOS 2.28.0 creates one thread per active schedule in every executor. This cap
# keeps that per-executor cost bounded and explicit.
MAX_ACTIVE_SCHEDULES = 64

_RUN_QUEUE = Queue(DEPLOYMENT_RUN_QUEUE_NAME)


class PermanentDeploymentRunError(RuntimeError):
    """A Run cannot become valid by retrying the same captured invocation."""


def _should_retry_run_step(exc: BaseException) -> bool:
    return not isinstance(exc, PermanentDeploymentRunError)


def _schedule_name(deployment_id: str) -> str:
    return f"{SCHEDULE_NAME_PREFIX}{str(deployment_id).strip()}"


def _run_prefix(deployment_id: str) -> str:
    return f"{RUN_ID_PREFIX}{_schedule_name(deployment_id)}-"


def _iso(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).isoformat()


def _workflow_context(
    deployment: dict[str, Any],
    *,
    trigger: str = "schedule",
    replayed_from_run_id: str | None = None,
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "deployment_id": str(deployment.get("deployment_id") or "").strip(),
        "agent_id": str(deployment.get("agent_id") or "").strip(),
        "input_text": str(deployment.get("prompt_prefix") or ""),
        "trigger": trigger,
    }
    if replayed_from_run_id:
        context["replayed_from_run_id"] = replayed_from_run_id
    return context


def _workflow_attributes(
    context: dict[str, Any], scheduled_at: datetime
) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "deployment_id": str(context.get("deployment_id") or ""),
        "agent_id": str(context.get("agent_id") or ""),
        "trigger": str(context.get("trigger") or "schedule"),
    }
    if attributes["trigger"] == "schedule":
        attributes["scheduled_for"] = _iso(scheduled_at)
    replayed_from = str(context.get("replayed_from_run_id") or "").strip()
    if replayed_from:
        attributes["replayed_from_run_id"] = replayed_from
    return attributes


def _status_dict(status: Any) -> dict[str, Any]:
    return dict(vars(status))


@DBOS.step(
    retries_allowed=True,
    max_attempts=5,
    interval_seconds=1,
    backoff_rate=2,
    preemptible=True,
    should_retry=_should_retry_run_step,
)
async def _start_deployment_session(
    context: dict[str, Any], workflow_id: str
) -> str:
    from astrabox.core.service.orchestrator.service_registry import (
        get_platform_service,
    )

    return await get_platform_service().deployment_service.start_run_session(
        context, run_id=workflow_id
    )


@DBOS.step(
    retries_allowed=True,
    max_attempts=5,
    interval_seconds=1,
    backoff_rate=2,
    preemptible=True,
    should_retry=_should_retry_run_step,
)
async def _drive_deployment_turn(
    context: dict[str, Any], workflow_id: str, session_id: str
) -> dict[str, Any]:
    from astrabox.core.service.orchestrator.service_registry import (
        get_platform_service,
    )

    return await get_platform_service().deployment_service.drive_run_turn(
        context,
        run_id=workflow_id,
        session_id=session_id,
    )


@DBOS.workflow(name="astrabox.deployment.run")
async def run_deployment_workflow(
    scheduled_at: datetime, context: dict[str, Any]
) -> None:
    """Execute one scheduled/manual/replayed invocation through Session."""

    workflow_id = str(DBOS.workflow_id or "").strip()
    if not workflow_id:
        raise RuntimeError("DBOS started a Deployment Run without a workflow ID")
    attributes = _workflow_attributes(context, scheduled_at)
    await DBOS.update_workflow_attributes_async(workflow_id, attributes)
    session_id = await _start_deployment_session(context, workflow_id)
    await DBOS.update_workflow_attributes_async(
        workflow_id, {**attributes, "session_id": session_id}
    )
    result = await _drive_deployment_turn(context, workflow_id, session_id)
    turn_id = str(result.get("turn_id") or "").strip()
    if turn_id:
        await DBOS.update_workflow_attributes_async(
            workflow_id,
            {**attributes, "session_id": session_id, "turn_id": turn_id},
        )


class DeploymentRunRuntime:
    """Thin public-API adapter around the process-global DBOS runtime."""

    def __init__(self) -> None:
        self._started = False
        self._unsupported_reason: str | None = None

    @property
    def available(self) -> bool:
        return self._started and self._unsupported_reason is None

    @staticmethod
    def _dbos_url(raw_url: str) -> str | None:
        url = make_url(raw_url)
        driver = url.drivername.lower()
        if driver.startswith("postgresql") or driver.startswith("postgres"):
            return url.set(drivername="postgresql+psycopg").render_as_string(
                hide_password=False
            )
        if driver.startswith("sqlite"):
            return url.set(drivername="sqlite").render_as_string(hide_password=False)
        return None

    async def start(self) -> None:
        if self._started:
            return
        settings = get_settings()
        configured_backend = str(settings.db_backend or "").strip().lower()
        if configured_backend not in {"postgres", "postgresql", "sqlite"}:
            self._unsupported_reason = (
                "scheduled Deployments require the PostgreSQL or SQLite "
                f"repository backend; configured backend is {configured_backend!r}"
            )
            logger.warning("Deployment schedules unavailable: %s", self._unsupported_reason)
            return
        raw_url = settings.resolved_db_url
        dbos_url = self._dbos_url(raw_url)
        if dbos_url is None:
            self._unsupported_reason = (
                "scheduled Deployments require the PostgreSQL or SQLite "
                f"repository backend; configured URL uses {make_url(raw_url).drivername!r}"
            )
            logger.warning("Deployment schedules unavailable: %s", self._unsupported_reason)
            return
        try:
            DBOS(
                config={
                    "name": "astrabox",
                    "system_database_url": dbos_url,
                    "dbos_system_schema": "dbos",
                    # The server image runs one application process per host or
                    # container. A stable host identity lets a restarted
                    # process recover only its own interrupted workflows and
                    # prevents a newly started replica from stealing another
                    # live replica's work.
                    "executor_id": f"astrabox-{socket.gethostname()}",
                    "run_admin_server": False,
                    # Bound pause/resume and newly-created schedule visibility.
                    # The Session step still checks desired state before work.
                    "scheduler_polling_interval_sec": 1.0,
                    "log_level": "WARNING",
                    "console_log_level": "WARNING",
                }
            )
            # Called on FastAPI's running loop. DBOS adopts it for async
            # workflows, so repositories never cross event-loop ownership.
            DBOS.launch()
        except Exception:
            DBOS.destroy()
            raise
        self._started = True
        self._unsupported_reason = None
        logger.info("Deployment Run runtime started")

    async def stop(self) -> None:
        if not self._started:
            return
        try:
            DBOS.destroy(workflow_completion_timeout_sec=5)
        finally:
            self._started = False
        logger.info("Deployment Run runtime stopped")

    def _require_available(self) -> None:
        if self.available:
            return
        reason = self._unsupported_reason or "Deployment Run runtime is not started"
        raise APIError(
            code="SCHEDULE_UNAVAILABLE",
            message=reason,
            status_code=409,
        )

    @staticmethod
    def _schedule_input(deployment: dict[str, Any]) -> ScheduleInput:
        spec = deployment.get("schedule")
        if not isinstance(spec, dict):
            raise APIError(
                code="INVALID_SCHEDULE",
                message="scheduled Deployment requires a schedule object",
                status_code=400,
            )
        cron = " ".join(str(spec.get("cron") or "").split())
        timezone_name = str(spec.get("timezone") or "").strip()
        if len(cron.split()) != 5:
            raise APIError(
                code="INVALID_SCHEDULE",
                message="schedule cron must contain exactly five fields",
                status_code=400,
            )
        if not timezone_name:
            raise APIError(
                code="INVALID_SCHEDULE",
                message="schedule timezone is required",
                status_code=400,
            )
        return {
            "schedule_name": _schedule_name(str(deployment.get("deployment_id") or "")),
            "workflow_fn": run_deployment_workflow,
            "schedule": cron,
            "context": _workflow_context(deployment),
            "automatic_backfill": False,
            "cron_timezone": timezone_name,
            "queue_name": DEPLOYMENT_RUN_QUEUE_NAME,
        }

    @staticmethod
    def _projection_input(schedule: dict[str, Any]) -> ScheduleInput:
        """Rebuild one AstraBox schedule definition returned by DBOS."""

        return {
            "schedule_name": str(schedule.get("schedule_name") or ""),
            "workflow_fn": run_deployment_workflow,
            "schedule": str(schedule.get("schedule") or ""),
            "context": schedule.get("context"),
            "automatic_backfill": bool(schedule.get("automatic_backfill", False)),
            "cron_timezone": str(schedule.get("cron_timezone") or "") or None,
            "queue_name": str(schedule.get("queue_name") or "") or None,
        }

    @staticmethod
    async def _restore_projection(
        name: str, previous: dict[str, Any] | None
    ) -> None:
        """Restore the exact pre-update definition after a partial DBOS write."""

        try:
            if previous is None:
                await DBOS.delete_schedule_async(name)
                return
            await DBOS.apply_schedules_async(
                [DeploymentRunRuntime._projection_input(previous)]
            )
            status_change = (
                DBOS.pause_schedule
                if str(previous.get("status") or "") == "PAUSED"
                else DBOS.resume_schedule
            )
            await asyncio.to_thread(status_change, name)
        except Exception as exc:
            logger.exception("could not restore Deployment schedule projection %s", name)
            raise APIError(
                code="SCHEDULE_EXECUTION_UNAVAILABLE",
                message="the schedule execution store could not restore the prior definition",
                status_code=503,
            ) from exc

    @staticmethod
    def _execution_unavailable() -> APIError:
        return APIError(
            code="SCHEDULE_EXECUTION_UNAVAILABLE",
            message="the schedule execution store is unavailable",
            status_code=503,
        )

    async def _active_schedules(self) -> list[dict[str, Any]]:
        self._require_available()
        rows = await DBOS.list_schedules_async(
            status="ACTIVE", schedule_name_prefix=SCHEDULE_NAME_PREFIX
        )
        return [dict(row) for row in rows]

    async def ensure_capacity(self, deployment_id: str) -> None:
        existing_name = _schedule_name(deployment_id)
        active = await self._active_schedules()
        if any(str(row.get("schedule_name") or "") == existing_name for row in active):
            return
        if len(active) >= MAX_ACTIVE_SCHEDULES:
            raise APIError(
                code="SCHEDULE_CAPACITY_EXCEEDED",
                message=(
                    f"this AstraBox release supports at most {MAX_ACTIVE_SCHEDULES} "
                    "active schedules per installation"
                ),
                status_code=409,
            )

    async def apply(self, deployment: dict[str, Any]) -> None:
        self._require_available()
        deployment_id = str(deployment.get("deployment_id") or "").strip()
        name = _schedule_name(deployment_id)
        if deployment.get("deleted") is True:
            await self.delete(deployment_id)
            return
        schedule_input = self._schedule_input(deployment)
        disabled = deployment.get("enabled") is False
        previous: dict[str, Any] | None = None
        projection_touched = False
        try:
            existing = await DBOS.get_schedule_async(name)
            previous = dict(existing) if existing is not None else None
            if previous is not None:
                # DBOS preserves status on upsert. Quiesce the old definition
                # while replacing it so an edit never temporarily reopens a
                # paused schedule; the final pause/resume below is explicit.
                projection_touched = True
                await asyncio.to_thread(DBOS.pause_schedule, name)
            if not disabled:
                await self.ensure_capacity(deployment_id)
            projection_touched = True
            await DBOS.apply_schedules_async([schedule_input])
            status_change = DBOS.pause_schedule if disabled else DBOS.resume_schedule
            await asyncio.to_thread(status_change, name)

            # The preflight count and upsert are separate public DBOS calls.
            # Re-check after insertion so concurrent creators cannot leave the
            # installation over its explicit thread budget.
            if not disabled and len(await self._active_schedules()) > MAX_ACTIVE_SCHEDULES:
                projection_touched = False
                await self._restore_projection(name, previous)
                raise APIError(
                    code="SCHEDULE_CAPACITY_EXCEEDED",
                    message=(
                        f"this AstraBox release supports at most {MAX_ACTIVE_SCHEDULES} "
                        "active schedules per installation"
                    ),
                    status_code=409,
                )
        except APIError:
            if projection_touched:
                await self._restore_projection(name, previous)
            raise
        except dbos_error.DBOSException as exc:
            if projection_touched:
                await self._restore_projection(name, previous)
            if exc.message.startswith(("Invalid cron schedule:", "Invalid timezone:")):
                raise APIError(
                    code="INVALID_SCHEDULE",
                    message="schedule cron or timezone is invalid",
                    status_code=400,
                ) from exc
            raise self._execution_unavailable() from exc
        except Exception as exc:
            if projection_touched:
                await self._restore_projection(name, previous)
            raise self._execution_unavailable() from exc

    async def delete(self, deployment_id: str) -> None:
        self._require_available()
        try:
            await DBOS.delete_schedule_async(_schedule_name(deployment_id))
        except Exception as exc:
            raise self._execution_unavailable() from exc

    async def reconcile(self, deployments: list[dict[str, Any]]) -> None:
        if not self.available:
            return
        live = [
            row
            for row in deployments
            if row.get("deleted") is not True
        ]
        enabled = [row for row in live if row.get("enabled") is not False]
        if len(enabled) > MAX_ACTIVE_SCHEDULES:
            raise RuntimeError(
                f"configured active schedules ({len(enabled)}) exceed the "
                f"AstraBox safety limit ({MAX_ACTIVE_SCHEDULES})"
            )
        desired = {
            _schedule_name(str(row.get("deployment_id") or "")): row for row in live
        }
        try:
            if desired:
                await DBOS.apply_schedules_async(
                    [self._schedule_input(row) for row in desired.values()]
                )
            existing = await DBOS.list_schedules_async(
                schedule_name_prefix=SCHEDULE_NAME_PREFIX
            )
            for schedule in existing:
                name = str(schedule.get("schedule_name") or "")
                if name not in desired:
                    await DBOS.delete_schedule_async(name)
                else:
                    status_change = (
                        DBOS.pause_schedule
                        if desired[name].get("enabled") is False
                        else DBOS.resume_schedule
                    )
                    await asyncio.to_thread(status_change, name)
        except Exception as exc:
            message = exc.message if isinstance(exc, APIError) else str(exc)
            raise RuntimeError(
                f"could not reconcile scheduled Deployments: {message}"
            ) from exc

    async def get_schedule(self, deployment_id: str) -> dict[str, Any] | None:
        self._require_available()
        try:
            row = await DBOS.get_schedule_async(_schedule_name(deployment_id))
        except Exception as exc:
            raise self._execution_unavailable() from exc
        return dict(row) if row is not None else None

    async def list_runs(
        self, deployment_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        self._require_available()
        try:
            rows = await DBOS.list_workflows_async(
                name="astrabox.deployment.run",
                workflow_id_prefix=_run_prefix(deployment_id),
                limit=max(1, min(int(limit), 200)),
                sort_desc=True,
                load_input=True,
            )
        except Exception as exc:
            raise self._execution_unavailable() from exc
        return [_status_dict(row) for row in rows]

    async def _enqueue(
        self,
        deployment: dict[str, Any],
        *,
        trigger: str,
        replayed_from_run_id: str | None = None,
        input_text: str | None = None,
    ) -> dict[str, Any]:
        self._require_available()
        deployment_id = str(deployment.get("deployment_id") or "").strip()
        run_id = f"{_run_prefix(deployment_id)}{trigger}-{uuid.uuid4().hex}"
        context = _workflow_context(
            deployment,
            trigger=trigger,
            replayed_from_run_id=replayed_from_run_id,
        )
        if input_text is not None:
            context["input_text"] = input_text
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
                raise RuntimeError(f"execution store did not persist Run {run_id}")
        except Exception as exc:
            raise self._execution_unavailable() from exc
        return _status_dict(status)

    async def run_now(self, deployment: dict[str, Any]) -> dict[str, Any]:
        return await self._enqueue(deployment, trigger="manual")

    async def replay(
        self, deployment: dict[str, Any], source_run_id: str
    ) -> dict[str, Any]:
        self._require_available()
        if not str(source_run_id).startswith(
            _run_prefix(str(deployment.get("deployment_id") or ""))
        ):
            raise APIError(
                code="NOT_FOUND", message="Deployment Run not found", status_code=404
            )
        try:
            source = await DBOS.get_workflow_status_async(source_run_id)
        except Exception as exc:
            raise self._execution_unavailable() from exc
        if source is None or str(source.name or "") != "astrabox.deployment.run":
            raise APIError(
                code="NOT_FOUND", message="Deployment Run not found", status_code=404
            )
        context = self.context_from_status(_status_dict(source))
        if str(context.get("deployment_id") or "") != str(
            deployment.get("deployment_id") or ""
        ):
            raise APIError(
                code="NOT_FOUND", message="Deployment Run not found", status_code=404
            )
        return await self._enqueue(
            deployment,
            trigger="replay",
            replayed_from_run_id=source_run_id,
            input_text=str(context.get("input_text") or ""),
        )

    @staticmethod
    def context_from_status(status: dict[str, Any]) -> dict[str, Any]:
        workflow_input = status.get("input")
        if not isinstance(workflow_input, dict):
            return {}
        args = workflow_input.get("args")
        if not isinstance(args, (list, tuple)) or len(args) < 2:
            return {}
        context = args[1]
        return dict(context) if isinstance(context, dict) else {}


deployment_run_runtime = DeploymentRunRuntime()


async def start_deployment_run_runtime() -> None:
    from astrabox.persistence.repository import DeploymentRepository

    # DBOS may apply PostgreSQL migrations containing CREATE INDEX
    # CONCURRENTLY. Launch before opening AstraBox repository reads so its
    # migration cannot wait on a snapshot owned by this same event loop.
    await deployment_run_runtime.start()
    deployments = await DeploymentRepository().list_scheduled()
    if not deployment_run_runtime.available:
        active = [row for row in deployments if row.get("enabled") is not False]
        if active:
            raise RuntimeError(
                "active scheduled Deployments require the PostgreSQL or SQLite "
                "repository backend"
            )
        return
    try:
        await deployment_run_runtime.reconcile(deployments)
    except Exception:
        await deployment_run_runtime.stop()
        raise


async def stop_deployment_run_runtime() -> None:
    await deployment_run_runtime.stop()


__all__ = [
    "DeploymentRunRuntime",
    "MAX_ACTIVE_SCHEDULES",
    "PermanentDeploymentRunError",
    "deployment_run_runtime",
    "run_deployment_workflow",
    "start_deployment_run_runtime",
    "stop_deployment_run_runtime",
]
