"""A deployment's scheduler provider is selected and checked when it starts.

The built-in DBOS provider keeps its schedules and Run ledger in the
repository's SQL database, so a repository without one (MongoDB) cannot run it.
That must refuse the start with the way out named, instead of starting and
failing the first schedule a user creates. ``none`` is the explicit way to run
without scheduled Deployments: it refuses new schedules before anything is
written, and refuses to start while an enabled schedule is stored, because
that schedule would silently never fire.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import astrabox.providers.scheduler_dbos  # noqa: F401 - registers the dbos provider
from astrabox import bootstrap
from astrabox.common.utils.errors import APIError
from astrabox.config.settings import get_settings
from astrabox.core.service.orchestrator import deployment_scheduling
from astrabox.core.service.orchestrator.deployment_service import DeploymentService
from astrabox.persistence.repository import backend as repository_backend
from astrabox.seams.scheduler import configured_scheduler


@pytest.fixture(autouse=True)
def _fresh_settings() -> Any:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _select(monkeypatch: pytest.MonkeyPatch, provider: str) -> None:
    monkeypatch.setenv("ASTRABOX_SCHEDULER_PROVIDER", provider)
    get_settings.cache_clear()
    bootstrap._select_scheduler_provider()


def test_dbos_on_a_repository_without_sql_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repository_backend, "active_backend_name", lambda: "mongo")
    monkeypatch.setattr(repository_backend, "active_backend", lambda: SimpleNamespace())

    with pytest.raises(bootstrap.BootstrapConfigError) as refused:
        _select(monkeypatch, "dbos")

    message = str(refused.value)
    assert "ASTRABOX_SCHEDULER_PROVIDER=dbos" in message
    assert "'mongo'" in message
    assert "ASTRABOX_SCHEDULER_PROVIDER=none" in message


def test_dbos_on_the_sql_repository_is_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    _select(monkeypatch, "dbos")

    selected = configured_scheduler()
    assert selected is not None and selected.name == "dbos"


def test_an_uninstalled_provider_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(bootstrap.BootstrapConfigError, match="no scheduler provider named"):
        _select(monkeypatch, "temporal")


def test_none_disables_scheduling(monkeypatch: pytest.MonkeyPatch) -> None:
    _select(monkeypatch, "none")

    assert configured_scheduler() is None


async def test_none_refuses_to_start_while_an_enabled_schedule_is_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _select(monkeypatch, "none")
    stored = AsyncMock()
    stored.list_scheduled.return_value = [
        {"deployment_id": "d-1", "scene": "schedule", "enabled": True},
    ]
    monkeypatch.setattr(
        "astrabox.persistence.repository.DeploymentRepository", lambda: stored
    )

    with pytest.raises(RuntimeError, match="ASTRABOX_SCHEDULER_PROVIDER=none"):
        await deployment_scheduling.start_deployment_scheduler()

    stored.list_scheduled.return_value = [
        {"deployment_id": "d-1", "scene": "schedule", "enabled": False},
    ]
    await deployment_scheduling.start_deployment_scheduler()


def _service(repository: AsyncMock) -> DeploymentService:
    return DeploymentService(
        deployment_repo=repository,
        scheduler=None,
        agent_repo=AsyncMock(),
        agent_service_getter=lambda: AsyncMock(),
        stream_message_events_ds=AsyncMock(),
        dispatch_turn_input=AsyncMock(),
        sessions_repo=AsyncMock(),
        session_snapshots_repo=AsyncMock(),
        session_events_repo=AsyncMock(),
        spawn_background_task=lambda *args, **kwargs: None,
        agent_config=AsyncMock(),
        channel_ingress=AsyncMock(),
    )


async def test_without_a_scheduler_a_new_schedule_is_refused_before_any_write() -> None:
    repository = AsyncMock()
    service = _service(repository)

    with pytest.raises(APIError) as refused:
        await service.create(
            agent_id="agent-1",
            creator_user_id="owner-1",
            scene="schedule",
            name="Daily report",
            prompt_prefix="Prepare the report",
            schedule={"cron": "0 9 * * *", "timezone": "UTC"},
        )

    assert (refused.value.code, refused.value.status_code) == ("SCHEDULE_UNAVAILABLE", 409)
    assert "ASTRABOX_SCHEDULER_PROVIDER=none" in refused.value.message
    repository.upsert.assert_not_awaited()


async def test_without_a_scheduler_a_disabled_schedule_can_be_edited_and_deleted() -> None:
    existing = {
        "deployment_id": "deployment-1",
        "agent_id": "agent-1",
        "scene": "schedule",
        "name": "Daily report",
        "prompt_prefix": "Old prompt",
        "enabled": False,
        "schedule": {"cron": "0 9 * * *", "timezone": "UTC"},
    }
    repository = AsyncMock()
    repository.get_for_deployment.return_value = existing
    repository.update_for_deployment.side_effect = (
        lambda _deployment_id, _agent_id, updates, *, assistant_id="": {**existing, **updates}
    )
    service = _service(repository)

    updated = await service.update(
        "deployment-1", agent_id="agent-1", patch={"prompt_prefix": "New prompt"}
    )
    assert updated["prompt_prefix"] == "New prompt"

    with pytest.raises(APIError) as refused:
        await service.update("deployment-1", agent_id="agent-1", patch={"enabled": True})
    assert refused.value.code == "SCHEDULE_UNAVAILABLE"

    await service.delete("deployment-1", agent_id="agent-1")
    repository.soft_delete_for_deployment.assert_awaited_once()
