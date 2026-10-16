"""Durable workspace identity is one-winner state, not a last-write hint."""

from __future__ import annotations

import asyncio

import pytest

from astrabox.core.service.orchestrator.runtime.storage._identity import (
    _ensure_agent_workspace_id,
)
from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.persistence.repository.environment_repository import EnvironmentRepository

pytestmark = pytest.mark.usefixtures("database")


async def _agent(*, deleted: bool = False) -> None:
    await EnvironmentRepository().put(
        "env",
        {"engine_kind": "claude_code", "idle_action": "terminate", "networking": {"type": "unrestricted"}},
        updated_by="test",
    )
    await AgentRepository().create_agent(
        {
            "agent_id": "agent-1", "name": "agent-1", "model": "model",
            "environment_name": "env", "created_by": "owner",
            "state": "DELETED" if deleted else "ACTIVE",
        }
    )


async def test_concurrent_agent_box_creators_mount_the_same_workspace() -> None:
    """Pool refill and cold start cannot publish different Agent roots."""
    await _agent()

    minted = await asyncio.gather(*(_ensure_agent_workspace_id("agent-1") for _ in range(6)))

    assert len(set(minted)) == 1
    stored = await AgentRepository().get_agent("agent-1")
    assert stored is not None and stored["workspace_id"] == minted[0]
    # Later boxes mount the same root.
    assert await _ensure_agent_workspace_id("agent-1") == minted[0]
