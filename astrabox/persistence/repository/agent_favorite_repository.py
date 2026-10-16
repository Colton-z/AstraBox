"""The Agents each user has favorited.

One row per user and Agent, keyed by both, so favoriting and unfavoriting are
idempotent and two toggles from different tabs cannot overwrite each other's
other favorites the way a single per-user list would.
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncConnection

from astrabox.persistence.sql import execute, fetch_all, transaction
from astrabox.persistence.sql.models.agents import AgentFavorite
from astrabox.persistence.sql.statements import insert_if_absent

_FAVORITES = AgentFavorite.__table__


class AgentFavoriteRepository:
    async def add(self, user_id: str, agent_id: str) -> None:
        async def add(conn: AsyncConnection) -> None:
            await insert_if_absent(
                conn,
                _FAVORITES,
                {"user_id": user_id, "agent_id": agent_id},
                index_columns=["user_id", "agent_id"],
            )

        await transaction(add)

    async def remove(self, user_id: str, agent_id: str) -> None:
        await execute(
            delete(_FAVORITES).where(
                _FAVORITES.c.user_id == user_id, _FAVORITES.c.agent_id == agent_id
            )
        )

    async def agent_ids_for(self, user_id: str) -> set[str]:
        rows = await fetch_all(
            select(_FAVORITES.c.agent_id).where(_FAVORITES.c.user_id == user_id)
        )
        return {str(row["agent_id"]) for row in rows}

    async def remove_agent(self, agent_id: str) -> None:
        """Every user's favorite of one Agent, when the Agent is deleted."""
        await execute(delete(_FAVORITES).where(_FAVORITES.c.agent_id == agent_id))
