"""Scheduled admission preserves the scheduler's retry classification."""

from unittest.mock import AsyncMock

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.deployment_service import DeploymentService
from astrabox.providers.scheduler_dbos import _should_retry_run_step
from astrabox.seams.scheduler import RunPermanentError


async def test_run_admission_preserves_retryable_errors_and_terminal_refusals() -> None:
    agent_repo = AsyncMock()
    agent_repo.get_agent.return_value = {"agent_id": "agent", "created_by": "owner"}
    runtime = AsyncMock()
    service = DeploymentService(
        deployment_repo=AsyncMock(),
        agent_repo=agent_repo,
        agent_service_getter=lambda: AsyncMock(),
        sessions_repo=AsyncMock(),
        session_runtime=runtime,
        session_facts=AsyncMock(),
        agent_config=AsyncMock(),
        channel_ingress=AsyncMock(),
    )
    for code, status, retryable in (
        ("ADMISSION_DENIED", 429, True),
        ("SERVICE_UNAVAILABLE", 503, True),
        ("SESSION_NOT_FOUND", 404, False),
        ("AGENT_DELETED", 409, False),
        ("AGENT_RUNTIME_ERROR", 409, False),
    ):
        refusal = APIError(code=code, message=code, status_code=status)
        runtime.send_as.reset_mock()
        runtime.send_as.side_effect = refusal
        with pytest.raises((APIError, RunPermanentError)) as raised:
            await service.admit_run_input(
                {"agent_id": "agent", "input_text": "the scheduled task"},
                run_id="run",
                session_id="session",
            )
        assert _should_retry_run_step(raised.value) is retryable
        if retryable:
            assert raised.value is refusal
        else:
            assert isinstance(raised.value, RunPermanentError)
            assert raised.value.__cause__ is refusal
        runtime.send_as.assert_awaited_once_with(
            "session",
            "the scheduled task",
            author_user_id="owner",
            source="schedule",
            client_message_id="deployment-run:run",
        )
