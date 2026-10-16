"""A2A task references: retries reach the first reservation and the first binding wins."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from astrabox.persistence.repository.agent_task_reference_repository import (
    AgentTaskReferenceRepository,
)
from astrabox.persistence.repository.session_repository import SessionRepository
from tests._agent_ledger import create_agent

pytestmark = pytest.mark.usefixtures("database")


async def _anchors(*, agents: list[str], sessions: list[str]) -> None:
    for agent_id in agents:
        await create_agent(agent_id)
    for session_id in sessions:
        await SessionRepository().create_session(
            {"session_id": session_id, "user_id": "user-1", "session_kind": "agent_chat", "state": "READY"}
        )


def _task(reference_id: str, **fields: Any) -> dict[str, Any]:
    return {
        "reference_id": reference_id,
        "kind": "task",
        "agent_id": "agent-1",
        "user_id": "user-1",
        "org_id": "org-1",
        "context_id": "context-1",
        "message_id": reference_id,
        "task_id": "",
        "fingerprint": "request-1",
        **fields,
    }


async def test_a_reference_keeps_its_first_reservation_and_its_first_conversation() -> None:
    await _anchors(agents=["agent-1"], sessions=["s-1", "s-2"])
    repo = AgentTaskReferenceRepository()

    # A client retries a message while the first attempt is still reserving;
    # a different request reusing the message id must see the first one.
    first, second = await asyncio.gather(
        repo.reserve(_task("a2a-1", fingerprint="request-1")),
        repo.reserve(_task("a2a-1", fingerprint="request-2")),
    )
    assert first["fingerprint"] == second["fingerprint"]

    # Two deliveries bind the task to different conversations at once.
    outcomes = await asyncio.gather(
        repo.bind_session("a2a-1", "s-1"),
        repo.bind_session("a2a-1", "s-2"),
        return_exceptions=True,
    )
    winners = [session for session, outcome in zip(("s-1", "s-2"), outcomes) if outcome is None]
    assert len(winners) == 1
    assert sum(isinstance(outcome, RuntimeError) for outcome in outcomes) == 1
    stored = await repo.get("a2a-1")
    assert stored is not None and stored["session_id"] == winners[0]
    # Binding the winner again is the same binding, not a conflict.
    await repo.bind_session("a2a-1", winners[0])
