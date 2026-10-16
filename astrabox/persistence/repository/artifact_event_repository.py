"""The per-artifact live journal viewers tail by sequence."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import and_, delete, or_, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from astrabox.persistence.repository._artifact_repository import every, run
from astrabox.persistence.sql import to_iso
from astrabox.persistence.sql.models.artifacts import ArtifactEvent

_EVENTS = ArtifactEvent.__table__

#: The kinds whose newest event replaces the earlier ones of the same key, and
#: that key: the columns of the kind's partial unique index.
_REPLACED_BY_KEY = {
    "version": [_EVENTS.c.artifact_id],
    "data": [_EVENTS.c.artifact_id, _EVENTS.c.path],
}


def _event(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "seq": row["seq"],
        "kind": row["kind"],
        "payload": row["payload"],
        "created_at": to_iso(row["created_at"]),
    }


class ArtifactEventRepository:
    """Journal events as ``{seq, kind, payload, created_at}``, ``created_at`` an ISO string."""

    async def append(
        self,
        artifact_id: str,
        seq: int,
        kind: str,
        payload: dict[str, Any],
        *,
        created_at: datetime,
        conn: AsyncConnection,
    ) -> None:
        """Record an event at ``seq``, which the caller allocated under the artifact's lock.

        The journal holds one ``version`` event per artifact and one ``data``
        event per document path; a new one replaces the stored one and moves
        it to the new sequence, so a reader resuming from any position before
        it still receives it. Room and presence events are appended.
        """
        values = {"seq": seq, "kind": kind, "payload": payload, "created_at": created_at}
        path = payload["path"] if kind == "data" else None
        statement = pg_insert(_EVENTS).values(artifact_id=artifact_id, path=path, **values)
        if kind in _REPLACED_BY_KEY:
            statement = statement.on_conflict_do_update(
                index_elements=_REPLACED_BY_KEY[kind],
                index_where=text(f"kind = '{kind}'"),
                set_=values,
            )
        await run(statement, conn)

    async def after(self, artifact_id: str, seq: int, *, limit: int) -> list[dict[str, Any]]:
        """Up to ``limit`` events after ``seq``, in sequence order."""
        rows = await every(
            select(_EVENTS)
            .where(_EVENTS.c.artifact_id == artifact_id, _EVENTS.c.seq > seq)
            .order_by(_EVENTS.c.seq)
            .limit(limit),
            None,
        )
        return [_event(row) for row in rows]

    async def presence(
        self, artifact_id: str, *, through_seq: int, since: datetime
    ) -> list[dict[str, Any]]:
        """Presence events at or before ``through_seq`` written since ``since``, in order."""
        rows = await every(
            select(_EVENTS)
            .where(
                _EVENTS.c.artifact_id == artifact_id,
                _EVENTS.c.kind == "presence",
                _EVENTS.c.seq <= through_seq,
                _EVENTS.c.created_at >= since,
            )
            .order_by(_EVENTS.c.seq),
            None,
        )
        return [_event(row) for row in rows]

    async def prune(self, *, rooms_before: datetime, presence_before: datetime, limit: int) -> int:
        """Delete up to ``limit`` expired room and presence events, of any artifact.

        Rows another transaction holds are skipped, so concurrent pruners never
        wait on each other.
        """
        expired = (
            select(_EVENTS.c.artifact_id, _EVENTS.c.seq)
            .where(
                or_(
                    and_(_EVENTS.c.kind == "room", _EVENTS.c.created_at < rooms_before),
                    and_(_EVENTS.c.kind == "presence", _EVENTS.c.created_at < presence_before),
                )
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return await run(
            delete(_EVENTS).where(tuple_(_EVENTS.c.artifact_id, _EVENTS.c.seq).in_(expired)),
            None,
        )
