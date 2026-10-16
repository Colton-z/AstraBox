"""A2A protocol identities and provenance; execution remains in the Session journal."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, literal, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import ColumnElement

from astrabox.persistence.sql import execute, fetch_all, fetch_one, fetch_value, to_iso, transaction
from astrabox.persistence.sql.models.platform import AgentTaskReference
from astrabox.persistence.sql.statements import insert_if_absent

_REFERENCES = AgentTaskReference.__table__

#: The columns a reservation may set; the rest belong to the store.
_RESERVED_FIELDS = (
    "reference_id",
    "kind",
    "agent_id",
    "user_id",
    "org_id",
    "context_id",
    "message_id",
    "task_id",
    "fingerprint",
)


def _view(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {**row, "created_at": to_iso(row["created_at"])}


def _tasks_of(
    *, user_id: str, org_id: str, agent_id: str, context_id: str
) -> list[ColumnElement[bool]]:
    conditions = [
        _REFERENCES.c.agent_id == agent_id,
        _REFERENCES.c.user_id == user_id,
        _REFERENCES.c.org_id == org_id,
        _REFERENCES.c.kind == "task",
        _REFERENCES.c.session_id.is_not(None),
    ]
    if context_id:
        conditions.append(_REFERENCES.c.context_id == context_id)
    return conditions


class AgentTaskReferenceRepository:
    async def reserve(self, reference: dict[str, Any]) -> dict[str, Any]:
        """Store ``reference`` unless its id is taken; the stored row either way.

        The id is deterministic, so the caller compares the returned row with
        its request to tell a retry from a different request reusing the id.
        """
        values = {key: reference[key] for key in _RESERVED_FIELDS if key in reference}

        async def reserve(conn: AsyncConnection) -> dict[str, Any] | None:
            await insert_if_absent(conn, _REFERENCES, values, index_columns=["reference_id"])
            # Read-only confirmation must not upgrade a duplicate's shared lock.
            result = await conn.execute(
                select(_REFERENCES)
                .where(_REFERENCES.c.reference_id == values["reference_id"])
                .with_for_update(read=True)
            )
            row = result.mappings().first()
            return dict(row) if row is not None else None

        stored = _view(await transaction(reserve))
        if stored is None:
            raise RuntimeError("reserved Agent task reference is missing")
        return stored

    async def get(self, reference_id: str) -> dict[str, Any] | None:
        return _view(
            await fetch_one(
                select(_REFERENCES).where(_REFERENCES.c.reference_id == reference_id)
            )
        )

    async def bind_session(self, reference_id: str, session_id: str) -> None:
        """Bind the reference to ``session_id``; the first binding wins.

        Raises ``RuntimeError`` when the reference is bound to another
        conversation, or does not exist.
        """
        bound = await execute(
            update(_REFERENCES)
            .where(
                _REFERENCES.c.reference_id == reference_id,
                or_(
                    _REFERENCES.c.session_id.is_(None),
                    _REFERENCES.c.session_id == session_id,
                ),
            )
            .values(session_id=session_id)
        )
        if bound != 1:
            raise RuntimeError("Agent task reference has conflicting Session bindings")

    async def list_tasks(
        self,
        *,
        user_id: str,
        org_id: str,
        agent_id: str,
        context_id: str = "",
        after: tuple[datetime, str] | None = None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """One page of the caller's bound tasks, newest first.

        ``after`` is the ``(created_at, reference_id)`` of the previous page's
        last task; the page holds the tasks strictly after it.
        """
        statement = select(_REFERENCES).where(
            *_tasks_of(user_id=user_id, org_id=org_id, agent_id=agent_id, context_id=context_id)
        )
        if after is not None:
            statement = statement.where(
                tuple_(_REFERENCES.c.created_at, _REFERENCES.c.reference_id)
                < tuple_(
                    literal(after[0], type_=_REFERENCES.c.created_at.type),
                    literal(after[1], type_=_REFERENCES.c.reference_id.type),
                )
            )
        rows = await fetch_all(
            statement.order_by(
                _REFERENCES.c.created_at.desc(), _REFERENCES.c.reference_id.desc()
            ).limit(limit)
        )
        return [row for row in map(_view, rows) if row is not None]

    async def count_tasks(
        self, *, user_id: str, org_id: str, agent_id: str, context_id: str = ""
    ) -> int:
        """How many bound tasks :meth:`list_tasks` pages through."""
        return int(
            await fetch_value(
                select(func.count()).where(
                    *_tasks_of(
                        user_id=user_id, org_id=org_id, agent_id=agent_id, context_id=context_id
                    )
                )
            )
            or 0
        )
