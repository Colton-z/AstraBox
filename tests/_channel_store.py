"""Rows that channel records reference, for tests whose Agents and Sessions are fakes.

A channel record references its binding (a ``deployments`` row) and, once it
has one, its Session (a ``sessions`` row); a binding references its Agent.
Tests that fake those services write the referenced rows here, so the store's
foreign keys hold.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.dialects.postgresql import insert

from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.persistence.sql import execute
from astrabox.persistence.sql.models.deployments import Deployment
from astrabox.persistence.sql.models.sessions import Session
from tests._agent_ledger import create_agent


async def seed_binding(
    deployment_id: str,
    *,
    agent_id: str = "agent-1",
    scene: str = "channel:recorder",
    **columns: Any,
) -> None:
    """A binding row on an Agent the repository wrote, with any other columns given."""
    if await AgentRepository().get_agent(agent_id) is None:
        await create_agent(agent_id)
    await execute(
        insert(Deployment.__table__)
        .values(
            deployment_id=deployment_id,
            created_by="creator",
            scene=scene,
            agent_id=agent_id,
            **columns,
        )
        .on_conflict_do_nothing()
    )


async def seed_sessions(*session_ids: str, user_id: str = "creator", state: str = "READY") -> None:
    """Agent conversation Session rows, by id, owned by ``user_id`` in lifecycle ``state``.

    Seeding an existing id gives it that owner and state.
    """
    for session_id in session_ids:
        await execute(
            insert(Session.__table__)
            .values(
                session_id=session_id,
                user_id=user_id,
                session_kind="agent_chat",
                workspace_kind="agent",
                state=state,
            )
            .on_conflict_do_update(
                index_elements=["session_id"], set_={"user_id": user_id, "state": state}
            )
        )
