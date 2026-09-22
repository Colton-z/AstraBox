"""Scheduler deployments preserve operator-authored prompts and slash commands."""

from typing import Any
from unittest.mock import AsyncMock

from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.deployment_service import DeploymentService


def test_scheduler_preserves_a_standalone_slash_command() -> None:
    assert DeploymentService._build_content(
        {"scene": "scheduler", "prompt_prefix": ""},
        b"/compact",
    ) == "/compact"


async def test_scheduler_turn_carries_stable_native_fifo_input_identity() -> None:
    seen: list[dict[str, Any]] = []

    async def stream(
        user: UserContext,
        session_id: str,
        content: str,
        *,
        client_message_id: str | None = None,
    ):
        seen.append(
            {
                "user_id": user.user_id,
                "session_id": session_id,
                "content": content,
                "client_message_id": client_message_id,
            }
        )
        yield {"type": "finish"}

    sessions_repo = AsyncMock()
    sessions_repo.get_session.return_value = {"state": "READY"}
    service = DeploymentService(
        deployment_repo=AsyncMock(),
        agent_repo=AsyncMock(),
        agent_service_getter=lambda: AsyncMock(),
        stream_message_events_ds=stream,
        dispatch_turn_input=AsyncMock(),
        sessions_repo=sessions_repo,
        spawn_background_task=lambda *args, **kwargs: None,
        agent_config=AsyncMock(),
        channel_ingress=AsyncMock(),
    )

    await service._fire_when_ready(
        UserContext(user_id="owner"),
        "session-1",
        "/compact",
        "deployment-1",
    )

    assert seen == [
        {
            "user_id": "owner",
            "session_id": "session-1",
            "content": "/compact",
            "client_message_id": "deployment:deployment-1:session-1",
        }
    ]
