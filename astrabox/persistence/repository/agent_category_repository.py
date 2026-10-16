"""The Agent categories a platform administrator defines.

Categories are deployment-wide, like the Agents they sort. An Agent is listed
under a category by a row of ``agent_category_links``, so renaming a category
rewrites one record and never the Agents, and deleting one takes it off every
Agent in the same transaction.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, func, insert, literal, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from astrabox.persistence.sql import UniqueViolation, fetch_all, fetch_one, to_iso, transaction
from astrabox.persistence.sql.models.agents import Agent, AgentCategory, AgentCategoryLink

_CATEGORIES = AgentCategory.__table__
_LINKS = AgentCategoryLink.__table__
_AGENTS = Agent.__table__


class CategoryNameTaken(Exception):
    """Another category already reads the same."""


def _view(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {**row, "created_at": to_iso(row["created_at"]), "updated_at": to_iso(row["updated_at"])}


class AgentCategoryRepository:
    async def list_all(self) -> list[dict[str, Any]]:
        """Every category, in the administrator's order."""
        rows = await fetch_all(
            select(_CATEGORIES).order_by(_CATEGORIES.c.position, _CATEGORIES.c.name_key)
        )
        return [row for row in map(_view, rows) if row is not None]

    async def create(self, *, name: str, name_key: str) -> dict[str, Any]:
        """Add a category after the last one; raises :class:`CategoryNameTaken`."""
        after_last = select(
            literal(f"cat_{uuid.uuid4().hex}"),
            literal(name),
            literal(name_key),
            func.coalesce(func.max(_CATEGORIES.c.position), -1) + 1,
        )
        try:
            row = await fetch_one(
                insert(_CATEGORIES)
                .from_select(["category_id", "name", "name_key", "position"], after_last)
                .returning(_CATEGORIES)
            )
        except UniqueViolation as exc:
            raise CategoryNameTaken(name) from exc
        stored = _view(row)
        assert stored is not None
        return stored

    async def rename(self, category_id: str, *, name: str, name_key: str) -> dict[str, Any] | None:
        """Rename a category; ``None`` when it does not exist. Raises :class:`CategoryNameTaken`."""
        try:
            row = await fetch_one(
                update(_CATEGORIES)
                .where(_CATEGORIES.c.category_id == category_id)
                .values(name=name, name_key=name_key, updated_at=func.now())
                .returning(_CATEGORIES)
            )
        except UniqueViolation as exc:
            raise CategoryNameTaken(name) from exc
        return _view(row)

    async def reorder(self, category_ids: list[str]) -> bool:
        """Put every category in the given order; ``False`` unless it names each once."""

        async def place(conn: AsyncConnection) -> bool:
            known = {
                row.category_id
                for row in await conn.execute(select(_CATEGORIES.c.category_id).with_for_update())
            }
            if sorted(known) != sorted(category_ids):
                return False
            for position, category_id in enumerate(category_ids):
                await conn.execute(
                    update(_CATEGORIES)
                    .where(_CATEGORIES.c.category_id == category_id)
                    .values(position=position, updated_at=func.now())
                )
            return True

        return await transaction(place)

    async def delete(self, category_id: str) -> int | None:
        """Delete a category and take it off every Agent; ``None`` when it does not exist.

        Returns how many Agents it was taken off. Each of them is an authored
        change, so their versions move in the same transaction: an edit
        prepared before the delete cannot write the category back.
        """

        async def remove(conn: AsyncConnection) -> int | None:
            linked = select(_LINKS.c.agent_id).where(_LINKS.c.category_id == category_id)
            unbound = await conn.execute(
                update(_AGENTS)
                .where(_AGENTS.c.agent_id.in_(linked), _AGENTS.c.state != "DELETED")
                .values(version=_AGENTS.c.version + 1)
            )
            removed = await conn.execute(
                delete(_CATEGORIES).where(_CATEGORIES.c.category_id == category_id)
            )
            if removed.rowcount != 1:
                return None
            return int(unbound.rowcount)

        return await transaction(remove)

    async def agent_counts(self) -> dict[str, int]:
        """How many live Agents each category holds."""
        rows = await fetch_all(
            select(_LINKS.c.category_id, func.count().label("agents"))
            .join(_AGENTS, _AGENTS.c.agent_id == _LINKS.c.agent_id)
            .where(_AGENTS.c.state != "DELETED")
            .group_by(_LINKS.c.category_id)
        )
        return {str(row["category_id"]): int(row["agents"]) for row in rows}
