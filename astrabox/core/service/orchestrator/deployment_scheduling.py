"""The platform's half of scheduled Deployments: definitions, Run admission, lifecycle.

The selected :class:`~astrabox.seams.scheduler.SchedulerProvider` fires due
times, keeps the Run ledger and recovers interrupted invocations. This module
owns what the provider must not: turning a stored Deployment into the
definition the provider projects, handing an invocation to the Deployment
service to admit its Session and input, and starting the provider with the
deployment's desired schedules once the process is up.
"""

from __future__ import annotations

from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.core.service.orchestrator.conversation_targets import (
    target_fields,
    target_ref,
)
from astrabox.seams.scheduler import (
    RunInputBinding,
    RunInvocation,
    ScheduleDefinition,
    configured_scheduler,
    configured_scheduler_name,
)

logger = get_logger(__name__)


def run_payload(deployment: dict[str, Any], *, input_text: str | None = None) -> dict[str, Any]:
    """The invocation input a Run carries: its target and its prompt.

    ``input_text`` overrides the Deployment's current prompt, which is how a
    replay runs the prompt its source Run captured.
    """

    target = target_ref(deployment)
    return {
        **(target_fields(*target) if target is not None else {}),
        **({"scope": "personal", "owner_user_id": deployment["owner_user_id"],
            "owner_org_id": deployment["owner_org_id"],
            "source_assistant_id": deployment.get("source_assistant_id", "")}
           if deployment.get("scope") == "personal" else {}),
        "input_text": (
            str(deployment.get("prompt_prefix") or "") if input_text is None else input_text
        ),
    }


def schedule_definition(deployment: dict[str, Any]) -> ScheduleDefinition:
    """The schedule a stored, validated scheduled Deployment asks the provider to keep."""

    schedule = deployment.get("schedule") if isinstance(deployment.get("schedule"), dict) else {}
    return ScheduleDefinition(
        deployment_id=str(deployment.get("deployment_id") or "").strip(),
        environment_name=str(deployment["environment_name"]),
        cron=str(schedule.get("cron") or ""),
        timezone=str(schedule.get("timezone") or ""),
        enabled=deployment.get("enabled") is not False,
        payload=run_payload(deployment),
    )


def invocation_context(invocation: RunInvocation) -> dict[str, Any]:
    """The invocation as the Deployment service's Run methods read it."""

    context: dict[str, Any] = {
        **dict(invocation.payload),
        "deployment_id": invocation.deployment_id,
        "environment_name": invocation.environment_name,
        "trigger": invocation.trigger,
    }
    if invocation.replayed_from_run_id:
        context["replayed_from_run_id"] = invocation.replayed_from_run_id
    return context


class DeploymentRunHandler:
    """Admits each invocation through the platform's Deployment service.

    The service is looked up per call: the provider can resume an interrupted
    invocation as soon as it starts, before every platform service exists.
    """

    async def start_session(self, invocation: RunInvocation) -> str:
        from astrabox.core.service.orchestrator.service_registry import (
            get_platform_service,
        )

        return await get_platform_service().deployment_service.start_run_session(
            invocation_context(invocation), run_id=invocation.run_id
        )

    async def admit_input(
        self, invocation: RunInvocation, *, session_id: str
    ) -> RunInputBinding:
        from astrabox.core.service.orchestrator.service_registry import (
            get_platform_service,
        )

        admitted = await get_platform_service().deployment_service.admit_run_input(
            invocation_context(invocation),
            run_id=invocation.run_id,
            session_id=session_id,
        )
        return RunInputBinding(input_id=str(admitted["input_id"]))


async def start_deployment_scheduler() -> None:
    """Start the selected scheduler provider and reconcile it to the stored schedules.

    The provider starts before any repository read: an embedded engine may
    migrate its own schema with statements (PostgreSQL's ``CREATE INDEX
    CONCURRENTLY``) that would wait on a snapshot a reader on this same event
    loop holds open. With scheduling disabled (``none``), a stored enabled
    schedule refuses the start, because it would silently never run.
    """

    from astrabox.persistence.repository.deployment_repository import DeploymentRepository
    from astrabox.config.settings import get_settings

    scheduler = configured_scheduler()
    if scheduler is None:
        if not get_settings().background_execution_enabled:
            logger.info("scheduler worker disabled for Environment %s", get_settings().environment_name)
            return
        deployments = await DeploymentRepository().list_scheduled()
        enabled = [row for row in deployments if row.get("enabled") is not False]
        if enabled:
            raise RuntimeError(
                f"{len(enabled)} enabled scheduled Deployments are stored, but "
                "ASTRABOX_SCHEDULER_PROVIDER=none disables scheduling; select a "
                "scheduler provider or disable those Deployments"
            )
        logger.info("scheduled Deployments are disabled (ASTRABOX_SCHEDULER_PROVIDER=none)")
        return
    await scheduler.start(
        DeploymentRunHandler() if get_settings().background_execution_enabled else None
    )
    if not get_settings().background_execution_enabled:
        logger.info("scheduler control client ready; worker disabled for Environment %s", get_settings().environment_name)
        return
    logger.info(
        "scheduler provider %s started: recovery_scope=%s max_active_schedules=%s "
        "pause_visibility_seconds=%s",
        scheduler.name,
        scheduler.properties.recovery_scope,
        scheduler.properties.max_active_schedules,
        scheduler.properties.pause_visibility_seconds,
    )
    try:
        deployments = await DeploymentRepository().list_scheduled()
        await scheduler.reconcile([schedule_definition(row) for row in deployments])
    except Exception as exc:
        await scheduler.stop()
        raise RuntimeError(
            f"could not reconcile scheduled Deployments with the "
            f"{configured_scheduler_name()!r} scheduler: {exc}"
        ) from exc


async def stop_deployment_scheduler() -> None:
    """Stop the selected scheduler provider, if one is selected."""

    if configured_scheduler_name() is None:
        return
    scheduler = configured_scheduler()
    if scheduler is not None:
        await scheduler.stop()


__all__ = [
    "DeploymentRunHandler",
    "invocation_context",
    "run_payload",
    "schedule_definition",
    "start_deployment_scheduler",
    "stop_deployment_scheduler",
]
