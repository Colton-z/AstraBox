"""Incremental engine-owned relationship projections over immutable native entries."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from astrabox.persistence.sql import fetch_all, transaction
from astrabox.persistence.sql.models.sessions import TranscriptChildRunIndex, TranscriptEntry, TranscriptScope
from astrabox.persistence.sql.statements import insert_if_absent

_SCOPES = TranscriptScope.__table__
_ENTRIES = TranscriptEntry.__table__
_INDEX = TranscriptChildRunIndex.__table__
_PAGE_SIZE = 64
ChildRunProjector = Callable[[list[dict[str, Any]], list[dict[str, Any]]], list[dict[str, Any]]]


def _project_page(
    projector: ChildRunProjector, projected: list[dict[str, Any]], raw: list[str],
) -> list[dict[str, Any]]:
    return projector(projected, [json.loads(entry) for entry in raw])


class TranscriptChildRunRepository:
    """Read compact native metadata, scoped to the authorized platform session."""

    async def scopes(
        self, session_id: str, *, engine_kind: str, projector: ChildRunProjector,
    ) -> list[dict[str, Any]]:
        """Catch up each scope once; warm reads never load native message bodies.

        A scope's immutable sequence permits existing history to be indexed
        lazily and new appends to advance only the unprocessed suffix. Each page
        commits independently; decoding and vendor projection run off the event
        loop. Concurrent readers share the committed scope cursor.
        """
        rows = await fetch_all(
            select(
                _SCOPES.c.scope_id, _SCOPES.c.project_key, _SCOPES.c.native_session_id,
                _SCOPES.c.subpath, _SCOPES.c.last_sequence, _INDEX.c.through_seq, _INDEX.c.entries,
            ).select_from(_SCOPES.outerjoin(_INDEX, and_(
                _INDEX.c.scope_id == _SCOPES.c.scope_id, _INDEX.c.engine_kind == engine_kind,
            )))
            .where(_SCOPES.c.platform_session_id == session_id)
            .order_by(_SCOPES.c.subpath, _SCOPES.c.scope_id)
        )
        scopes: list[dict[str, Any]] = []
        for row in rows:
            target = int(row["last_sequence"])
            cursor = row["through_seq"]
            projected = row["entries"] or []
            while cursor is None or cursor < target:
                cursor, projected = await self._advance(
                    int(row["scope_id"]), engine_kind, target, projector, create=cursor is None,
                )
            scopes.append({
                "project_key": row["project_key"], "session_id": row["native_session_id"],
                "subpath": row["subpath"], "entries": projected,
            })
        return scopes

    async def _advance(
        self, scope_id: int, engine_kind: str, target: int, projector: ChildRunProjector, *, create: bool,
    ) -> tuple[int, list[dict[str, Any]]]:
        where = and_(_INDEX.c.scope_id == scope_id, _INDEX.c.engine_kind == engine_kind)

        async def work(conn: AsyncConnection) -> tuple[int, list[dict[str, Any]]]:
            if create:
                await insert_if_absent(
                    conn, _INDEX,
                    {"scope_id": scope_id, "engine_kind": engine_kind, "through_seq": 0, "entries": []},
                    index_columns=["scope_id", "engine_kind"],
                )
            index = (await conn.execute(select(_INDEX).where(where).with_for_update())).mappings().one()
            cursor = int(index["through_seq"])
            projected = index["entries"]
            if cursor >= target:
                return cursor, projected
            page = (await conn.execute(
                select(_ENTRIES.c.seq, _ENTRIES.c.entry_json)
                .where(_ENTRIES.c.scope_id == scope_id, _ENTRIES.c.seq > cursor, _ENTRIES.c.seq <= target)
                .order_by(_ENTRIES.c.seq).limit(_PAGE_SIZE)
            )).all()
            if not page:
                raise RuntimeError(f"native child index lacks entries scope_id={scope_id} after={cursor} target={target}")
            projected = await asyncio.to_thread(_project_page, projector, projected, [row.entry_json for row in page])
            cursor = int(page[-1].seq)
            await conn.execute(update(_INDEX).where(where).values(through_seq=cursor, entries=projected))
            return cursor, projected

        return await transaction(work)
