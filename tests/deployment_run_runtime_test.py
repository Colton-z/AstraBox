"""The scheduled-Deployment adapter delegates execution state to DBOS."""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from dbos import DBOS

from astrabox.common.utils.errors import APIError
from astrabox.config.settings import get_settings
from astrabox.core.service.orchestrator.deployment_run_runtime import (
    DeploymentRunRuntime,
)


async def test_embedded_runtime_launches_before_repository_reads(monkeypatch) -> None:
    run_module = importlib.import_module(
        "astrabox.core.service.orchestrator.deployment_run_runtime"
    )
    repository_module = importlib.import_module("astrabox.persistence.repository")
    runtime = run_module.deployment_run_runtime
    events: list[str] = []

    class _Repository:
        async def list_scheduled(self) -> list[dict[str, object]]:
            events.append("repository-read")
            return []

    async def _start() -> None:
        events.append("runtime-launch")
        runtime._started = True
        runtime._unsupported_reason = None

    async def _reconcile(_deployments: list[dict[str, object]]) -> None:
        events.append("schedule-reconcile")

    monkeypatch.setattr(repository_module, "DeploymentRepository", _Repository)
    monkeypatch.setattr(runtime, "start", _start)
    monkeypatch.setattr(runtime, "reconcile", _reconcile)
    monkeypatch.setattr(runtime, "_started", False)
    monkeypatch.setattr(runtime, "_unsupported_reason", None)

    await run_module.start_deployment_run_runtime()

    assert events == ["runtime-launch", "repository-read", "schedule-reconcile"]


async def test_non_sql_backend_only_disables_scheduled_deployments(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_DB_BACKEND", "mongo")
    monkeypatch.delenv("ASTRABOX_DB_URL", raising=False)
    monkeypatch.setenv("ASTRABOX_MONGODB_URI", "mongodb://example.invalid/astrabox")
    get_settings.cache_clear()
    runtime = DeploymentRunRuntime()

    try:
        await runtime.start()

        assert runtime.available is False
        with pytest.raises(APIError) as unavailable:
            await runtime.run_now({"deployment_id": "deployment-1"})
        assert unavailable.value.code == "SCHEDULE_UNAVAILABLE"
        assert "DBOS" not in unavailable.value.message
    finally:
        await runtime.stop()
        get_settings.cache_clear()


async def test_dbos_owns_schedule_queue_replay_and_run_ledger(
    monkeypatch, tmp_path
) -> None:
    database = tmp_path / "runtime.sqlite"
    monkeypatch.setenv("ASTRABOX_DB_BACKEND", "sqlite")
    monkeypatch.setenv("ASTRABOX_DB_URL", f"sqlite+aiosqlite:///{database}")
    get_settings.cache_clear()

    deployment_service = SimpleNamespace(
        start_run_session=AsyncMock(return_value="session-1"),
        drive_run_turn=AsyncMock(
            return_value={"command_id": "command-1", "turn_id": "turn-1"}
        ),
    )
    platform = SimpleNamespace(deployment_service=deployment_service)
    import astrabox.core.service.orchestrator.service_registry as registry

    monkeypatch.setattr(registry, "get_platform_service", lambda: platform)
    runtime = DeploymentRunRuntime()
    deployment = {
        "deployment_id": "deployment-1",
        "agent_id": "agent-1",
        "enabled": True,
        "scene": "schedule",
        "prompt_prefix": "prepare the report",
        "schedule": {"cron": "0 0 1 1 *", "timezone": "UTC"},
    }

    try:
        await runtime.start()

        invalid = {
            **deployment,
            "schedule": {"cron": "0 9 * * *", "timezone": "Mars/Olympus"},
        }
        with pytest.raises(APIError) as invalid_error:
            await runtime.apply(invalid)
        assert invalid_error.value.code == "INVALID_SCHEDULE"
        assert invalid_error.value.status_code == 400
        assert invalid_error.value.message == "schedule cron or timezone is invalid"
        assert "DBOS" not in invalid_error.value.message
        assert await runtime.get_schedule("deployment-1") is None

        await runtime.apply(deployment)
        schedule = await runtime.get_schedule("deployment-1")
        assert schedule is not None
        assert schedule["schedule"] == "0 0 1 1 *"
        assert schedule["cron_timezone"] == "UTC"
        assert schedule["automatic_backfill"] is False
        listed = await DBOS.list_schedules_async(
            schedule_name_prefix="astrabox-deployment-"
        )
        assert [row["schedule_name"] for row in listed] == [
            "astrabox-deployment-deployment-1"
        ]

        invalid_disable = {
            **deployment,
            "enabled": False,
            "schedule": {"cron": "0 9 * * *", "timezone": "Mars/Olympus"},
        }
        with pytest.raises(APIError) as invalid_disable_error:
            await runtime.apply(invalid_disable)
        assert invalid_disable_error.value.code == "INVALID_SCHEDULE"
        unchanged = await runtime.get_schedule("deployment-1")
        assert unchanged is not None
        assert unchanged["status"] == "ACTIVE"
        assert unchanged["schedule"] == "0 0 1 1 *"

        disabled = {**deployment, "enabled": False}
        await runtime.apply(disabled)
        paused = await runtime.get_schedule("deployment-1")
        assert paused is not None and paused["status"] == "PAUSED"

        await runtime.reconcile([disabled])
        reconciled = await runtime.get_schedule("deployment-1")
        assert reconciled is not None and reconciled["status"] == "PAUSED"

        await runtime.apply(deployment)
        resumed = await runtime.get_schedule("deployment-1")
        assert resumed is not None and resumed["status"] == "ACTIVE"

        fresh_disabled = {
            **deployment,
            "deployment_id": "deployment-2",
            "enabled": False,
        }
        orphan = {**deployment, "deployment_id": "deployment-orphan"}
        await runtime.apply(orphan)
        await runtime.reconcile([deployment, fresh_disabled])
        reconciled_fresh = await runtime.get_schedule("deployment-2")
        assert reconciled_fresh is not None
        assert reconciled_fresh["status"] == "PAUSED"
        assert await runtime.get_schedule("deployment-orphan") is None

        scheduled_handle = await asyncio.to_thread(
            DBOS.trigger_schedule, "astrabox-deployment-deployment-1"
        )
        assert await asyncio.to_thread(scheduled_handle.get_result) is None

        manual = await runtime.run_now(deployment)
        manual_id = manual["workflow_id"]
        assert "scheduled_for" not in manual["attributes"]
        manual_handle = await DBOS.retrieve_workflow_async(manual_id)
        assert await manual_handle.get_result() is None

        replay = await runtime.replay(
            {**deployment, "prompt_prefix": "a later edited prompt"}, manual_id
        )
        replay_id = replay["workflow_id"]
        replay_handle = await DBOS.retrieve_workflow_async(replay_id)
        assert await replay_handle.get_result() is None

        runs = await runtime.list_runs("deployment-1")
        assert [row["workflow_id"] for row in runs[:2]] == [replay_id, manual_id]
        assert len(runs) == 3
        assert runs[2]["workflow_id"] == scheduled_handle.workflow_id
        assert runs[2]["attributes"]["scheduled_for"]
        assert runtime.context_from_status(runs[0])["replayed_from_run_id"] == manual_id
        assert runtime.context_from_status(runs[0])["input_text"] == "prepare the report"
        assert deployment_service.start_run_session.await_count == 3
        assert deployment_service.drive_run_turn.await_count == 3

        monkeypatch.setattr(
            DBOS,
            "list_workflows_async",
            AsyncMock(side_effect=RuntimeError("DBOS implementation detail")),
        )
        with pytest.raises(APIError) as unavailable:
            await runtime.list_runs("deployment-1")
        assert unavailable.value.code == "SCHEDULE_EXECUTION_UNAVAILABLE"
        assert "DBOS" not in unavailable.value.message

        await runtime.delete("deployment-1")
        assert await runtime.get_schedule("deployment-1") is None
    finally:
        await runtime.stop()
        get_settings.cache_clear()
