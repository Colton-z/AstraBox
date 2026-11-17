"""The versions of an Agent's authored definition, newest first."""

from __future__ import annotations

from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from astrabox.persistence.sql import fetch_all, fetch_one, to_iso
from astrabox.persistence.sql.models.agents import AgentVersion

_VERSIONS = AgentVersion.__table__

#: Everything about a version except the definition it stored.
_METADATA = tuple(column for column in _VERSIONS.c if column.key != "definition")


def _version(row: dict[str, Any]) -> dict[str, Any]:
    version = {
        "agent_id": row["agent_id"],
        "version": row["version"],
        "operation": row["operation"],
        "restored_from_version": row["restored_from_version"],
        "author_user_id": row["author_user_id"],
        "created_at": to_iso(row["created_at"]),
    }
    if "definition" in row:
        version["definition"] = row["definition"]
    return version


async def record_version(
    conn: AsyncConnection,
    *,
    agent_id: str,
    version: int,
    definition: dict[str, Any],
    operation: str,
    author_user_id: str | None,
    restored_from_version: int | None = None,
) -> None:
    """Store the definition ``version`` of the Agent has, in the transaction that wrote it."""
    await conn.execute(
        insert(_VERSIONS).values(
            agent_id=agent_id,
            version=version,
            definition=definition,
            operation=operation,
            restored_from_version=restored_from_version,
            author_user_id=author_user_id,
        )
    )


class AgentVersionRepository:
    """Version records as dicts; ``definition`` only where a caller asks for it."""

    async def list_for_agent(
        self, agent_id: str, *, before: int | None, limit: int, definitions: bool = False
    ) -> list[dict[str, Any]]:
        """The Agent's versions below ``before`` (all when ``None``), newest first."""
        statement = select(_VERSIONS) if definitions else select(*_METADATA)
        statement = statement.where(_VERSIONS.c.agent_id == agent_id)
        if before is not None:
            statement = statement.where(_VERSIONS.c.version < before)
        rows = await fetch_all(statement.order_by(_VERSIONS.c.version.desc()).limit(limit))
        return [_version(row) for row in rows]

    async def get(self, agent_id: str, version: int) -> dict[str, Any] | None:
        """One version with its definition, or ``None`` when the Agent kept none by that number."""
        row = await fetch_one(
            select(_VERSIONS).where(
                _VERSIONS.c.agent_id == agent_id, _VERSIONS.c.version == version
            )
        )
        return _version(row) if row is not None else None

    async def get_preceding(self, agent_id: str, version: int) -> dict[str, Any] | None:
        """The newest version below ``version`` with its definition, or ``None``."""
        row = await fetch_one(
            select(_VERSIONS)
            .where(_VERSIONS.c.agent_id == agent_id, _VERSIONS.c.version < version)
            .order_by(_VERSIONS.c.version.desc())
            .limit(1)
        )
        return _version(row) if row is not None else None
