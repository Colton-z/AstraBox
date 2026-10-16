"""The runtime's own transcript entries, held so a replacement runtime can resume.

Each scope is one transcript file a session's runtime writes: the Claude
SDK's ``SessionStore`` keys (its main transcript and each sub-agent's), or a
log an image-resident mirror relays for Codex and the DeepSeek Harness. An
entry is one line, stored as the JSON text the sender serialized and handed
back deep-equal; the store never interprets it.

Every operation names the platform session the request was authorized for.
The rest of a scope's key comes from the box, which any caller can echo, so
the platform session is what keeps one tenant out of another's transcripts.

Appends are batches named by the sender's ``append_id``. One statement creates
the scope if needed, numbers the batch after the scope's last entry and stores
it, so a scope's entries are always ``1..N`` without gaps, and a batch is
either wholly stored or not at all. Re-sending a stored batch returns the
position it got the first time and stores nothing; reusing an ``append_id``
for different entries is refused. A batch's identity is its ``append_id``,
never its content: the same entries under a new ``append_id`` are a second
append.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, cast

from sqlalchemy import (
    ARRAY,
    ColumnElement,
    Table,
    Text,
    UniqueConstraint,
    and_,
    func,
    literal,
    select,
    true,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.sql import Select

from astrabox.persistence.sql import ForeignKeyViolation, UniqueViolation, fetch_all, fetch_value
from astrabox.persistence.sql.models.sessions import TranscriptEntry, TranscriptScope
from astrabox.persistence.sql.notify import notice

_SCOPES = cast(Table, TranscriptScope.__table__)
_ENTRIES = cast(Table, TranscriptEntry.__table__)

#: The constraint a second copy of a batch violates when two deliveries of it
#: race: each statement checks for the batch against the snapshot it started
#: with, so the delivery that commits second is stopped here instead.
_BATCH_KEY = next(
    str(constraint.name)
    for constraint in _ENTRIES.constraints
    if isinstance(constraint, UniqueConstraint)
)


class TranscriptBatchConflict(Exception):
    """An ``append_id`` the scope already stored names different entries."""


class TranscriptSessionMissing(LookupError):
    """The platform session a transcript append names does not exist."""


def _scope_key(
    platform_session_id: str, project_key: str, native_session_id: str, subpath: str | None
) -> ColumnElement[bool]:
    return and_(
        _SCOPES.c.platform_session_id == platform_session_id,
        _SCOPES.c.project_key == project_key,
        _SCOPES.c.native_session_id == native_session_id,
        _SCOPES.c.subpath == (subpath or ""),
    )


def _entries_of(where: ColumnElement[bool]) -> Select[Any]:
    """Entry text of the scopes ``where`` selects, scope by scope in order."""
    return (
        select(_ENTRIES.c.entry_json)
        .select_from(_SCOPES.join(_ENTRIES, _ENTRIES.c.scope_id == _SCOPES.c.scope_id))
        .where(where)
        .order_by(_SCOPES.c.scope_id, _ENTRIES.c.seq)
    )


class TranscriptEntryRepository:
    """Transcript scopes and entries, fenced by the platform session."""

    async def append_entries(
        self,
        project_key: str,
        native_session_id: str,
        subpath: str | None,
        entries: list[dict[str, Any]],
        *,
        append_id: str,
        platform_session_id: str,
    ) -> int:
        """Store one batch; the scope's entry count after it.

        The first append of k entries returns k. An empty batch returns the
        current count without storing anything. Re-sending a batch under an
        ``append_id`` already stored returns the count that batch reached and
        stores nothing.

        Raises:
            TranscriptBatchConflict: ``append_id`` names a batch already
                stored with different entries.
            TranscriptSessionMissing: ``platform_session_id`` names no session.
        """
        batch = str(append_id or "").strip()
        if not batch:
            raise ValueError("append_id must be a non-empty string")
        if not entries:
            return await self.current_sequence(
                project_key, native_session_id, subpath, platform_session_id=platform_session_id
            )
        texts = [json.dumps(entry, ensure_ascii=False) for entry in entries]
        try:
            stored = await self._store(
                platform_session_id, project_key, native_session_id, subpath, batch, texts
            )
        except UniqueViolation as exc:
            if exc.constraint != _BATCH_KEY:
                raise
            # Another delivery of this append_id committed while this one ran;
            # the key check waited for it, so its rows are there to compare.
            stored = None
        except ForeignKeyViolation as exc:
            raise TranscriptSessionMissing(platform_session_id) from exc
        if stored is not None:
            return stored
        return await self._stored_batch(
            platform_session_id, project_key, native_session_id, subpath, batch, entries
        )

    @staticmethod
    async def _store(
        platform_session_id: str,
        project_key: str,
        native_session_id: str,
        subpath: str | None,
        append_id: str,
        texts: list[str],
    ) -> int | None:
        """Store a batch the scope does not hold yet; ``None`` when it holds it.

        Inserting the scope, or updating its existing row, takes the row's
        lock, so concurrent appends to one scope number their batches one
        after another from the count the previous one committed.
        """
        count = len(texts)
        # The batch is looked up through the scope's key, so the subquery
        # stands alone instead of referring to the upsert's conflicting row.
        existing = _SCOPES.alias("existing")
        held = (
            select(true())
            .select_from(existing.join(_ENTRIES, _ENTRIES.c.scope_id == existing.c.scope_id))
            .where(
                existing.c.platform_session_id == platform_session_id,
                existing.c.project_key == project_key,
                existing.c.native_session_id == native_session_id,
                existing.c.subpath == (subpath or ""),
                _ENTRIES.c.append_id == append_id,
            )
            .correlate(None)
            .exists()
        )
        proposed = pg_insert(_SCOPES).values(
            platform_session_id=platform_session_id,
            project_key=project_key,
            native_session_id=native_session_id,
            subpath=subpath or "",
            last_sequence=count,
        )
        scope = (
            proposed.on_conflict_do_update(
                index_elements=[
                    _SCOPES.c.platform_session_id,
                    _SCOPES.c.project_key,
                    _SCOPES.c.native_session_id,
                    _SCOPES.c.subpath,
                ],
                set_={"last_sequence": _SCOPES.c.last_sequence + count, "updated_at": func.now()},
                where=~held,
            )
            .returning(_SCOPES.c.scope_id, _SCOPES.c.last_sequence)
            .cte("scope")
        )
        lines = (
            func.unnest(literal(texts, ARRAY(Text)))
            .table_valued("entry_json", with_ordinality="ordinal")
            .render_derived()
        )
        rows = (
            _ENTRIES.insert()
            .from_select(
                ["scope_id", "seq", "append_id", "batch_index", "entry_json"],
                select(
                    scope.c.scope_id,
                    scope.c.last_sequence - count + lines.c.ordinal,
                    literal(append_id),
                    lines.c.ordinal - 1,
                    lines.c.entry_json,
                ).select_from(scope.join(lines, true())),
            )
            .cte("stored")
        )
        last = await fetch_value(
            select(scope.c.last_sequence, notice(platform_session_id, "transcript")).add_cte(rows)
        )
        return None if last is None else int(last)

    @staticmethod
    async def _stored_batch(
        platform_session_id: str,
        project_key: str,
        native_session_id: str,
        subpath: str | None,
        append_id: str,
        entries: list[dict[str, Any]],
    ) -> int:
        """The count a stored batch reached, once it is shown to be this batch."""
        rows = await fetch_all(
            select(_ENTRIES.c.seq, _ENTRIES.c.entry_json)
            .select_from(_SCOPES.join(_ENTRIES, _ENTRIES.c.scope_id == _SCOPES.c.scope_id))
            .where(
                _scope_key(platform_session_id, project_key, native_session_id, subpath),
                _ENTRIES.c.append_id == append_id,
            )
            .order_by(_ENTRIES.c.seq)
        )
        if len(rows) != len(entries) or any(
            json.loads(row["entry_json"]) != entry for row, entry in zip(rows, entries)
        ):
            raise TranscriptBatchConflict(
                f"append_id {append_id!r} already names a different batch of "
                f"{len(rows)} entries in this transcript; an append_id identifies "
                "one batch and cannot be reused for another"
            )
        return int(rows[-1]["seq"])

    async def current_sequence(
        self,
        project_key: str,
        native_session_id: str,
        subpath: str | None,
        *,
        platform_session_id: str,
    ) -> int:
        """The scope's entry count; 0 for a scope nothing has written."""
        value = await fetch_value(
            select(_SCOPES.c.last_sequence).where(
                _scope_key(platform_session_id, project_key, native_session_id, subpath)
            )
        )
        return int(value or 0)

    async def load_entries(
        self,
        project_key: str,
        native_session_id: str,
        subpath: str | None,
        *,
        platform_session_id: str,
    ) -> list[dict[str, Any]] | None:
        """The scope's entries in order; ``None`` for a scope nothing has written.

        ``None`` and an empty list are different answers in the SessionStore
        contract: a scope exists only once a batch was stored in it.
        """
        rows = await fetch_all(
            _entries_of(_scope_key(platform_session_id, project_key, native_session_id, subpath))
        )
        if not rows:
            return None
        return [json.loads(row["entry_json"]) for row in rows]

    async def load_subpath_entries_by_platform_session(
        self, platform_session_id: str, *, subpath: str | None
    ) -> list[dict[str, Any]]:
        """The entries of the session's scopes at ``subpath`` (``None``: main), in order."""
        if not str(platform_session_id or "").strip() or (
            subpath is not None and not subpath.strip()
        ):
            return []
        rows = await fetch_all(
            _entries_of(
                and_(
                    _SCOPES.c.platform_session_id == platform_session_id,
                    _SCOPES.c.subpath == (subpath or ""),
                )
            )
        )
        return [json.loads(row["entry_json"]) for row in rows]

    async def list_scopes_by_platform_session(
        self, platform_session_id: str
    ) -> list[dict[str, Any]]:
        """Every scope of one platform session, in the order they were created.

        A scope is ``{"project_key", "session_id", "subpath"}`` in the
        SessionStore key's own terms: ``session_id`` is the runtime's
        conversation id, and ``subpath`` is ``None`` for a main transcript. The
        main transcript and each sub-agent are separate files, which an export
        or a restore writes one by one.
        """
        if not str(platform_session_id or "").strip():
            return []
        rows = await fetch_all(
            select(_SCOPES.c.project_key, _SCOPES.c.native_session_id, _SCOPES.c.subpath)
            .where(_SCOPES.c.platform_session_id == platform_session_id)
            .order_by(_SCOPES.c.scope_id)
        )
        return [
            {
                "project_key": row["project_key"],
                "session_id": row["native_session_id"],
                "subpath": row["subpath"] or None,
            }
            for row in rows
        ]

    async def list_sessions(
        self, project_key: str, *, platform_session_id: str
    ) -> list[dict[str, Any]]:
        """The runtime conversations with a main transcript under ``project_key``.

        Each is ``{"session_id", "mtime"}``, ``mtime`` being when its last
        batch was stored in epoch milliseconds, the unit the SessionStore
        contract lists sessions in.
        """
        rows = await fetch_all(
            select(_SCOPES.c.native_session_id, _SCOPES.c.updated_at)
            .where(
                _SCOPES.c.platform_session_id == platform_session_id,
                _SCOPES.c.project_key == project_key,
                _SCOPES.c.subpath == "",
            )
            .order_by(_SCOPES.c.scope_id)
        )
        return [
            {"session_id": row["native_session_id"], "mtime": _epoch_ms(row["updated_at"])}
            for row in rows
        ]

    async def list_subkeys(
        self, project_key: str, native_session_id: str, *, platform_session_id: str
    ) -> list[str]:
        """The sub-file paths under one runtime conversation, in the order they were created."""
        rows = await fetch_all(
            select(_SCOPES.c.subpath)
            .where(
                _SCOPES.c.platform_session_id == platform_session_id,
                _SCOPES.c.project_key == project_key,
                _SCOPES.c.native_session_id == native_session_id,
                _SCOPES.c.subpath != "",
            )
            .order_by(_SCOPES.c.scope_id)
        )
        return [str(row["subpath"]) for row in rows]


def _epoch_ms(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)
