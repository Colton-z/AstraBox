"""Artifact records and their share links."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Select, delete, func, insert, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from astrabox.persistence.repository._artifact_repository import one, every, run
from astrabox.persistence.sql import from_iso, to_iso
from astrabox.persistence.sql.models.artifacts import Artifact, ArtifactShare

_ARTIFACTS = Artifact.__table__
_SHARES = ArtifactShare.__table__

#: ``origin`` keys and the columns that hold them.
_ORIGIN = {
    "session_kind": _ARTIFACTS.c.origin_session_kind,
    "session_id": _ARTIFACTS.c.origin_session_id,
    "agent_id": _ARTIFACTS.c.origin_agent_id,
    "assistant_id": _ARTIFACTS.c.origin_assistant_id,
}

_SHARE_COLUMNS = ("enabled", "token", "access", "audience", "version", "created_at", "expires_at")


def empty_share() -> dict[str, Any]:
    """The share state of an artifact whose owner has never shared it."""
    return {
        "enabled": False,
        "token": "",
        "access": "view",
        "audience": "anyone",
        "version": None,
        "created_at": None,
        "expires_at": None,
    }


def _records() -> Select[Any]:
    shared = [_SHARES.c[name].label(f"share_{name}") for name in _SHARE_COLUMNS]
    return select(_ARTIFACTS, *shared).select_from(
        _ARTIFACTS.outerjoin(_SHARES, _SHARES.c.artifact_id == _ARTIFACTS.c.artifact_id)
    )


def _record(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    share = empty_share()
    if row["share_token"] is not None:
        share = {name: row[f"share_{name}"] for name in _SHARE_COLUMNS}
        share["created_at"] = to_iso(share["created_at"])
        share["expires_at"] = to_iso(share["expires_at"])
    return {
        "artifact_id": row["artifact_id"],
        "owner_id": row["owner_id"],
        "title": row["title"],
        "description": row["description"],
        "kind": row["kind"],
        "language": row["language"],
        "current_version": row["current_version"],
        "manifest": row["manifest"],
        "origin": {
            key: row[column.name] for key, column in _ORIGIN.items() if row[column.name] is not None
        },
        "share": share,
        "revision": row["revision"],
        "event_seq": row["event_seq"],
        "document_count": row["document_count"],
        "created_at": to_iso(row["created_at"]),
        "updated_at": to_iso(row["updated_at"]),
    }


class ArtifactRecordRepository:
    """Artifact records, each with its share state, as the service's ``record`` dicts.

    A record carries ``origin`` (the keys of ``session_kind``, ``session_id``,
    ``agent_id`` and ``assistant_id`` that are set) and ``share`` (the
    :func:`empty_share` shape when the artifact was never shared); times are
    ISO-8601 strings.
    """

    async def insert(self, record: dict[str, Any], *, conn: AsyncConnection) -> None:
        origin = record["origin"]
        await run(
            insert(_ARTIFACTS).values(
                artifact_id=record["artifact_id"],
                owner_id=record["owner_id"],
                title=record["title"],
                description=record["description"],
                kind=record["kind"],
                language=record["language"],
                current_version=record["current_version"],
                manifest=record["manifest"],
                revision=record["revision"],
                event_seq=record["event_seq"],
                document_count=record["document_count"],
                created_at=from_iso(record["created_at"]),
                updated_at=from_iso(record["updated_at"]),
                **{column.name: origin.get(key) or None for key, column in _ORIGIN.items()},
            ),
            conn,
        )

    async def get(
        self, artifact_id: str, *, conn: AsyncConnection | None = None
    ) -> dict[str, Any] | None:
        return _record(await one(_records().where(_ARTIFACTS.c.artifact_id == artifact_id), conn))

    async def lock(self, artifact_id: str, *, conn: AsyncConnection) -> dict[str, Any] | None:
        """Lock the artifact against every other mutation until ``conn`` commits; its record.

        The lock is ``FOR NO KEY UPDATE``: it excludes other writers of the
        artifact but not the foreign-key checks of rows that reference it, so
        registering a viewer never waits behind a write. The record is read
        after the lock is held, so it includes everything the previous holder
        committed, its share included.
        """
        locked = await one(
            select(_ARTIFACTS.c.artifact_id)
            .where(_ARTIFACTS.c.artifact_id == artifact_id)
            .with_for_update(key_share=True),
            conn,
        )
        return None if locked is None else await self.get(artifact_id, conn=conn)

    async def get_by_share_token(self, token: str) -> dict[str, Any] | None:
        """The record whose share link has ``token``, enabled or not."""
        return _record(await one(_records().where(_SHARES.c.token == token), None))

    async def save(
        self, record: dict[str, Any], *, expected_revision: int, conn: AsyncConnection
    ) -> bool:
        """Write the record's mutable fields if its stored revision is ``expected_revision``."""
        written = await run(
            update(_ARTIFACTS)
            .where(
                _ARTIFACTS.c.artifact_id == record["artifact_id"],
                _ARTIFACTS.c.revision == expected_revision,
            )
            .values(
                title=record["title"],
                description=record["description"],
                current_version=record["current_version"],
                manifest=record["manifest"],
                revision=record["revision"],
                event_seq=record["event_seq"],
                document_count=record["document_count"],
                updated_at=from_iso(record["updated_at"]),
            ),
            conn,
        )
        return written == 1

    async def put_share(
        self, artifact_id: str, share: dict[str, Any], *, conn: AsyncConnection
    ) -> None:
        values = {
            "enabled": share["enabled"],
            "token": share["token"],
            "access": share["access"],
            "audience": share["audience"],
            "version": share["version"],
            "created_at": from_iso(share["created_at"]),
            "expires_at": from_iso(share["expires_at"]),
        }
        await run(
            pg_insert(_SHARES)
            .values(artifact_id=artifact_id, **values)
            .on_conflict_do_update(index_elements=[_SHARES.c.artifact_id], set_=values),
            conn,
        )

    async def delete_owned(self, artifact_id: str, owner_id: str) -> bool:
        """Delete the artifact and everything stored for it; False unless ``owner_id`` owns it."""
        removed = await run(
            delete(_ARTIFACTS).where(
                _ARTIFACTS.c.artifact_id == artifact_id, _ARTIFACTS.c.owner_id == owner_id
            ),
            None,
        )
        return removed == 1

    async def list_owned(
        self,
        owner_id: str,
        *,
        limit: int,
        session_id: str | None = None,
        assistant_id: str | None = None,
        agent_id: str | None = None,
        search: str | None = None,
        after: tuple[datetime, str] | None = None,
    ) -> list[dict[str, Any]]:
        """The owner's records, newest first by ``(updated_at, artifact_id)``.

        ``search`` matches a case-insensitive literal substring of the title
        or description. ``after`` is the position of the previous page's last
        record.
        """
        conditions = [_ARTIFACTS.c.owner_id == owner_id]
        for column, value in (
            (_ARTIFACTS.c.origin_session_id, session_id),
            (_ARTIFACTS.c.origin_assistant_id, assistant_id),
            (_ARTIFACTS.c.origin_agent_id, agent_id),
        ):
            if value:
                conditions.append(column == value)
        if search:
            needle = func.lower(search)
            conditions.append(
                or_(
                    func.strpos(func.lower(_ARTIFACTS.c.title), needle) > 0,
                    func.strpos(func.lower(_ARTIFACTS.c.description), needle) > 0,
                )
            )
        if after is not None:
            conditions.append(
                tuple_(_ARTIFACTS.c.updated_at, _ARTIFACTS.c.artifact_id) < tuple_(*after)
            )
        rows = await every(
            _records()
            .where(*conditions)
            .order_by(_ARTIFACTS.c.updated_at.desc(), _ARTIFACTS.c.artifact_id.desc())
            .limit(limit),
            None,
        )
        return [record for record in map(_record, rows) if record is not None]
