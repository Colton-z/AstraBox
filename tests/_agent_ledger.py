"""Agents with runtime state written straight to the store, for tests of the shared-box ledger.

A test seeds what a runtime would have recorded for an Agent (a resident box,
prepared slots, admissions) and reads back what the code under test left.
Slots are stored the way the repository stores them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import insert, select, update

from astrabox.persistence.repository.agent_repository import AgentRepository, _slot_values
from astrabox.persistence.repository.runtime_profile_repository import RuntimeProfileRepository
from astrabox.persistence.sql import execute, fetch_all, fetch_one
from astrabox.persistence.sql.models.agents import (
    AgentBoxAdmission,
    AgentPreparedSlot,
    AgentRuntime,
)

_RUNTIMES = AgentRuntime.__table__
_SLOTS = AgentPreparedSlot.__table__
_ADMISSIONS = AgentBoxAdmission.__table__


async def create_agent(
    agent_id: str = "agent-1", *, deleted: bool = False, **definition: Any
) -> AgentRepository:
    """An Agent of the ``env`` RuntimeProfile, deleted or not."""
    await RuntimeProfileRepository().put(
        "env",
        {
            "engine_kind": "claude_code",
            "idle_action": "terminate",
            "networking": {"type": "unrestricted"},
        },
        updated_by="test",
    )
    repo = AgentRepository()
    await repo.create_agent(
        {
            "agent_id": agent_id,
            "name": agent_id,
            "model": "model",
            "runtime_profile_name": "env",
            "created_by": "owner",
            "state": "DELETED" if deleted else "ACTIVE",
            **definition,
        }
    )
    return repo


async def set_runtime(agent_id: str, **columns: Any) -> None:
    """Write ``agent_runtimes`` columns as a runtime would have left them."""
    await execute(update(_RUNTIMES).where(_RUNTIMES.c.agent_id == agent_id).values(**columns))


async def put_slots(agent_id: str, *manifests: dict[str, Any]) -> None:
    """Record slot manifests for the Agent."""
    target = AgentRepository().environment_name
    await execute(insert(_SLOTS).values([
        _slot_values(agent_id, manifest, environment_name=target) for manifest in manifests
    ]))


async def put_admissions(agent_id: str, *admissions: dict[str, Any]) -> None:
    """Record admissions given as ``sandbox_id``, ``session_id`` and ``at`` in epoch seconds."""
    await execute(
        insert(_ADMISSIONS).values(
            [
                {
                    "agent_id": agent_id,
                    "environment_name": AgentRepository().environment_name,
                    "sandbox_id": admission["sandbox_id"],
                    "session_id": admission["session_id"],
                    "admitted_at": datetime.fromtimestamp(float(admission["at"]), timezone.utc),
                }
                for admission in admissions
            ]
        )
    )


async def runtime(agent_id: str) -> dict[str, Any]:
    """The Agent's ``agent_runtimes`` row, deleted Agent or not."""
    row = await fetch_one(select(_RUNTIMES).where(_RUNTIMES.c.agent_id == agent_id))
    assert row is not None
    return row


async def slots(agent_id: str) -> list[dict[str, Any]]:
    """The Agent's slot manifests, oldest first."""
    return await AgentRepository().list_slots(agent_id)


async def admissions(agent_id: str) -> list[dict[str, Any]]:
    """The Agent's admissions, oldest first, with ``at`` in epoch seconds."""
    rows = await fetch_all(
        select(_ADMISSIONS)
        .where(_ADMISSIONS.c.agent_id == agent_id)
        .order_by(_ADMISSIONS.c.admitted_at)
    )
    return [
        {
            "sandbox_id": row["sandbox_id"],
            "session_id": row["session_id"],
            "at": row["admitted_at"].timestamp(),
        }
        for row in rows
    ]
