"""Scheduled Deployments delegate durability to the scheduler provider while
the repository stays the desired state and the Session stays the authority on
whether a Run's turn is running."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import ANY, AsyncMock

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.deployment_scheduling import schedule_definition
from astrabox.core.service.orchestrator.deployment_service import DeploymentService
from astrabox.seams.scheduler import InvalidSchedule, RunInvocation, RunRecord


def _record(
    run_id: str,
    state: str,
    *,
    trigger: str = "schedule",
    session_id: str | None = None,
    turn_id: str | None = None,
) -> RunRecord:
    return RunRecord(
        invocation=RunInvocation(
            run_id=run_id,
            deployment_id="deployment-1",
            trigger=trigger,  # type: ignore[arg-type]
            payload={"agent_id": "agent-1", "input_text": "Prepare the report"},
        ),
        state=state,  # type: ignore[arg-type]
        created_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
        session_id=session_id,
        turn_id=turn_id,
        failed=state == "FAILED",
    )


def _service(
    *,
    deployment_repo: AsyncMock | None = None,
    scheduler: AsyncMock | None = None,
    agent_repo: AsyncMock | None = None,
    sessions_repo: AsyncMock | None = None,
    snapshots_repo: AsyncMock | None = None,
    journal_repo: AsyncMock | None = None,
    agent_service: AsyncMock | None = None,
    stream: Any = None,
    dispatch: AsyncMock | None = None,
) -> DeploymentService:
    selected_agent_service = agent_service or AsyncMock()
    return DeploymentService(
        deployment_repo=deployment_repo or AsyncMock(),
        scheduler=scheduler or AsyncMock(),
        agent_repo=agent_repo or AsyncMock(),
        agent_service_getter=lambda: selected_agent_service,
        stream_message_events_ds=stream or AsyncMock(),
        dispatch_turn_input=dispatch or AsyncMock(),
        sessions_repo=sessions_repo or AsyncMock(),
        session_snapshots_repo=snapshots_repo or AsyncMock(),
        session_events_repo=journal_repo or AsyncMock(),
        spawn_background_task=lambda *args, **kwargs: None,
        agent_config=AsyncMock(),
        channel_ingress=AsyncMock(),
    )


async def test_schedule_creation_persists_desired_state_then_projects_it() -> None:
    repository = AsyncMock()
    repository.upsert.side_effect = lambda _deployment_id, doc: dict(doc)
    runtime = AsyncMock()
    service = _service(deployment_repo=repository, scheduler=runtime)

    created = await service.create(
        agent_id="agent-1",
        creator_user_id="owner-1",
        scene="schedule",
        name="  Daily report  ",
        prompt_prefix="Prepare the daily report",
        schedule={
            "cron": "  0   9  *  *  * ",
            "timezone": " America/Los_Angeles ",
        },
    )

    assert created["name"] == "Daily report"
    assert created["schedule"] == {
        "cron": "0 9 * * *",
        "timezone": "America/Los_Angeles",
    }
    assert created["enabled"] is True
    assert "secret" not in created
    stored = repository.upsert.await_args.args[1]
    projected = runtime.apply.await_args.args[0]
    assert projected == schedule_definition(stored)
    assert projected.cron == "0 9 * * *"
    assert projected.timezone == "America/Los_Angeles"
    assert projected.enabled is True
    assert projected.payload == {
        "agent_id": "agent-1",
        "input_text": "Prepare the daily report",
    }


async def test_schedule_creation_removes_desired_state_when_projection_rejects() -> None:
    repository = AsyncMock()
    repository.upsert.side_effect = lambda _deployment_id, doc: dict(doc)
    runtime = AsyncMock()
    runtime.apply.side_effect = InvalidSchedule("the engine rejected the expression")
    service = _service(deployment_repo=repository, scheduler=runtime)

    with pytest.raises(APIError) as raised:
        await service.create(
            agent_id="agent-1",
            creator_user_id="owner-1",
            scene="schedule",
            name="Daily report",
            prompt_prefix="Prepare the report",
            schedule={"cron": "0 9 * * *", "timezone": "UTC"},
        )

    assert raised.value.code == "INVALID_SCHEDULE"
    deployment_id = repository.upsert.await_args.args[0]
    repository.soft_delete_for_deployment.assert_awaited_once_with(
        deployment_id, "agent-1", assistant_id=""
    )
    runtime.delete.assert_not_awaited()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("name", "", "name is required"),
        ("prompt_prefix", "  ", "prompt_prefix is required"),
        ("schedule", {"cron": "* * * *", "timezone": "UTC"}, "five fields"),
        ("schedule", {"cron": "* * * * *", "timezone": ""}, "timezone"),
        ("schedule", {"cron": "* * * * *", "timezone": "Mars/Olympus"}, "IANA"),
    ],
)
async def test_schedule_structure_fails_before_any_write(
    field: str, value: Any, message: str
) -> None:
    repository = AsyncMock()
    runtime = AsyncMock()
    service = _service(deployment_repo=repository, scheduler=runtime)
    kwargs: dict[str, Any] = {
        "agent_id": "agent-1",
        "creator_user_id": "owner-1",
        "scene": "schedule",
        "name": "Daily report",
        "prompt_prefix": "Prepare the report",
        "schedule": {"cron": "0 9 * * *", "timezone": "UTC"},
    }
    kwargs[field] = value

    with pytest.raises(APIError, match=message):
        await service.create(**kwargs)

    repository.upsert.assert_not_awaited()
    runtime.apply.assert_not_awaited()


async def test_schedule_update_is_validated_by_runtime_before_product_write() -> None:
    existing = {
        "deployment_id": "deployment-1",
        "agent_id": "agent-1",
        "scene": "schedule",
        "name": "Daily report",
        "prompt_prefix": "Old prompt",
        "enabled": True,
        "schedule": {"cron": "0 9 * * *", "timezone": "UTC"},
    }
    repository = AsyncMock()
    repository.get_for_deployment.return_value = existing
    repository.update_for_deployment.side_effect = (
        lambda _deployment_id, _agent_id, updates, *, assistant_id="": {**existing, **updates}
    )
    runtime = AsyncMock()
    service = _service(deployment_repo=repository, scheduler=runtime)

    updated = await service.update(
        "deployment-1",
        agent_id="agent-1",
        patch={
            "name": "Weekday report",
            "prompt_prefix": "New prompt",
            "schedule": {"cron": " 30  8 * * 1-5 ", "timezone": " UTC "},
        },
    )

    candidate = runtime.apply.await_args.args[0]
    assert (candidate.cron, candidate.timezone) == ("30 8 * * 1-5", "UTC")
    assert candidate.payload["input_text"] == "New prompt"
    assert updated["name"] == "Weekday report"
    repository.update_for_deployment.assert_awaited_once()


async def test_schedule_update_restores_projection_when_product_write_fails() -> None:
    existing = {
        "deployment_id": "deployment-1",
        "agent_id": "agent-1",
        "scene": "schedule",
        "name": "Daily report",
        "prompt_prefix": "Old prompt",
        "enabled": True,
        "schedule": {"cron": "0 9 * * *", "timezone": "UTC"},
    }
    repository = AsyncMock()
    repository.get_for_deployment.return_value = existing
    repository.update_for_deployment.side_effect = RuntimeError("write failed")
    runtime = AsyncMock()
    service = _service(deployment_repo=repository, scheduler=runtime)

    with pytest.raises(RuntimeError, match="write failed"):
        await service.update(
            "deployment-1",
            agent_id="agent-1",
            patch={"prompt_prefix": "New prompt"},
        )

    assert runtime.apply.await_count == 2
    candidate, restored = [call.args[0] for call in runtime.apply.await_args_list]
    assert candidate.payload["input_text"] == "New prompt"
    assert restored == schedule_definition(existing)


async def test_schedule_update_removes_projection_after_delete_race() -> None:
    existing = {
        "deployment_id": "deployment-1",
        "agent_id": "agent-1",
        "scene": "schedule",
        "name": "Daily report",
        "prompt_prefix": "Old prompt",
        "enabled": True,
        "schedule": {"cron": "0 9 * * *", "timezone": "UTC"},
    }
    repository = AsyncMock()
    repository.get_for_deployment.return_value = existing
    repository.update_for_deployment.return_value = None
    runtime = AsyncMock()
    service = _service(deployment_repo=repository, scheduler=runtime)

    with pytest.raises(APIError) as raised:
        await service.update(
            "deployment-1",
            agent_id="agent-1",
            patch={"prompt_prefix": "New prompt"},
        )

    assert raised.value.code == "NOT_FOUND"
    runtime.apply.assert_awaited_once()
    runtime.delete.assert_awaited_once_with("deployment-1")


async def test_run_state_is_projected_from_dispatch_state_and_the_bound_session_turn() -> None:
    deployment = {
        "deployment_id": "deployment-1",
        "agent_id": "agent-1",
        "scene": "schedule",
    }
    repository = AsyncMock()
    repository.get_for_deployment.return_value = deployment
    runtime = AsyncMock()
    runtime.list_runs.return_value = [
        _record("run-complete", "DISPATCHED", session_id="session-1", turn_id="turn-1"),
        # The provider settles its dispatch once the Session admits the turn;
        # the bound Session stays authoritative while that turn is active.
        _record("run-waiting", "DISPATCHED", trigger="manual", session_id="session-2"),
        _record("run-error", "FAILED", trigger="replay"),
    ]
    snapshots = AsyncMock()
    snapshots.get_snapshots_batch.return_value = {
        "session-1": {"last_turn_id": "turn-1", "last_turn_status": "COMPLETED"},
        "session-2": {
            "current_turn_id": "turn-2",
            "active_interaction_id": "interaction-1",
        },
    }
    service = _service(
        deployment_repo=repository,
        scheduler=runtime,
        snapshots_repo=snapshots,
    )

    runs = await service.list_runs_for_deployment(
        "deployment-1", agent_id="agent-1"
    )

    assert [run["status"] for run in runs] == [
        "COMPLETED",
        "WAITING_INPUT",
        "FAILED",
    ]
    assert runs[2]["error"] == "Deployment Run execution failed"
    assert runs[1]["turn_id"] == "turn-2"
    assert "settled_at" not in runs[1]
    assert [run["run_id"] for run in runs] == ["run-complete", "run-waiting", "run-error"]
    assert runs[0]["agent_id"] == "agent-1"


async def test_run_now_response_uses_session_state_after_admission() -> None:
    deployment = {
        "deployment_id": "deployment-1",
        "agent_id": "agent-1",
        "scene": "schedule",
    }
    repository = AsyncMock()
    repository.get_for_deployment.return_value = deployment
    runtime = AsyncMock()
    runtime.enqueue.return_value = _record(
        "run-1", "DISPATCHED", trigger="manual", session_id="session-1", turn_id="turn-1"
    )
    snapshots = AsyncMock()
    snapshots.get_snapshot.return_value = {
        "current_turn_id": "turn-1",
        "active_interaction_id": None,
    }
    service = _service(
        deployment_repo=repository,
        scheduler=runtime,
        snapshots_repo=snapshots,
    )

    run = await service.trigger_now("deployment-1", agent_id="agent-1")

    assert run["status"] == "RUNNING"
    snapshots.get_snapshot.assert_awaited_once_with("session-1")
    enqueued = runtime.enqueue.await_args.kwargs
    assert (enqueued["deployment_id"], enqueued["trigger"]) == ("deployment-1", "manual")


async def test_run_uses_one_identity_for_session_and_turn_idempotency() -> None:
    deployment = {
        "deployment_id": "deployment-1",
        "agent_id": "agent-1",
        "scene": "schedule",
        "enabled": True,
    }
    repository = AsyncMock()
    repository.get_by_id.return_value = deployment
    agent_repo = AsyncMock()
    agent_repo.get_agent.return_value = {
        "agent_id": "agent-1",
        "user_id": "owner-1",
    }
    agent_service = AsyncMock()
    agent_service.start_conversation.return_value = {"session_id": "session-1"}
    sessions = AsyncMock()
    sessions.get_session.return_value = {"state": "READY"}
    journal = AsyncMock()
    journal.find_command_by_client_message_id.side_effect = [
        None,
        {"causation_id": "command-1", "turn_id": "turn-1"},
    ]
    # Native SDK FIFO receipts identify the accepted command/input; the
    # journal is the engine-neutral source for its platform turn binding.
    journal.get_command_event.return_value = {"turn_id": "turn-1"}
    dispatch = AsyncMock(return_value={"command_id": "command-1", "input_id": "input-1"})

    service = _service(
        deployment_repo=repository,
        agent_repo=agent_repo,
        agent_service=agent_service,
        sessions_repo=sessions,
        journal_repo=journal,
        dispatch=dispatch,
    )
    context = {
        "deployment_id": "deployment-1",
        "agent_id": "agent-1",
        "input_text": "Prepare the report",
        "trigger": "schedule",
    }

    session_id = await service.start_run_session(context, run_id="run-1")
    turn = await service.drive_run_turn(
        context, run_id="run-1", session_id=session_id
    )
    redriven = await service.drive_run_turn(
        context, run_id="run-1", session_id=session_id
    )

    agent_service.start_conversation.assert_awaited_once_with(
        ANY,
        "agent-1",
        idempotency_key="deployment-run:run-1",
    )
    assert turn == {"command_id": "command-1", "turn_id": "turn-1"}
    assert redriven == turn
    dispatch.assert_awaited_once_with(
        ANY,
        "session-1",
        "Prepare the report",
        client_message_id="deployment-run:run-1",
    )
    journal.get_command_event.assert_awaited_once_with(
        "session-1", command_id="command-1"
    )
    journal.find_command_by_client_message_id.assert_awaited_with(
        "session-1", client_message_id="deployment-run:run-1"
    )
