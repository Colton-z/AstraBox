"""Immutable artifact versions; content is read only where it is served."""

from __future__ import annotations

from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from astrabox.persistence.repository._artifact_repository import every, one, run
from astrabox.persistence.sql import from_iso, to_iso
from astrabox.persistence.sql.models.artifacts import ArtifactVersion, ArtifactVersionContent

_VERSIONS = ArtifactVersion.__table__
_CONTENTS = ArtifactVersionContent.__table__


def _version(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    version = {
        "artifact_id": row["artifact_id"],
        "version": row["version"],
        "sha256": row["sha256"],
        "byte_size": row["byte_size"],
        "kind": row["kind"],
        "title": row["title"],
        "language": row["language"],
        "manifest": row["manifest"],
        "author": {"user_id": row["author_user_id"], "kind": row["author_kind"]},
        "created_at": to_iso(row["created_at"]),
    }
    if "content" in row:
        version["content"] = row["content"]
    return version


class ArtifactVersionRepository:
    """Version metadata as dicts with ``author`` ``{user_id, kind}``; content only on request."""

    async def insert(self, row: dict[str, Any], content: str, *, conn: AsyncConnection) -> None:
        await run(
            insert(_VERSIONS).values(
                artifact_id=row["artifact_id"],
                version=row["version"],
                sha256=row["sha256"],
                byte_size=row["byte_size"],
                kind=row["kind"],
                title=row["title"],
                language=row["language"],
                manifest=row["manifest"],
                author_user_id=row["author"]["user_id"],
                author_kind=row["author"]["kind"],
                created_at=from_iso(row["created_at"]),
            ),
            conn,
        )
        await run(
            insert(_CONTENTS).values(
                artifact_id=row["artifact_id"], version=row["version"], content=content
            ),
            conn,
        )

    async def get(
        self,
        artifact_id: str,
        version: int,
        *,
        content: bool = False,
        conn: AsyncConnection | None = None,
    ) -> dict[str, Any] | None:
        """One version's metadata, with its ``content`` when ``content`` is true."""
        statement = select(_VERSIONS)
        if content:
            statement = select(_VERSIONS, _CONTENTS.c.content).join(
                _CONTENTS,
                (_CONTENTS.c.artifact_id == _VERSIONS.c.artifact_id)
                & (_CONTENTS.c.version == _VERSIONS.c.version),
            )
        return _version(
            await one(
                statement.where(
                    _VERSIONS.c.artifact_id == artifact_id, _VERSIONS.c.version == version
                ),
                conn,
            )
        )

    async def list_for_artifact(self, artifact_id: str, *, limit: int) -> list[dict[str, Any]]:
        """The artifact's versions without content, newest first."""
        rows = await every(
            select(_VERSIONS)
            .where(_VERSIONS.c.artifact_id == artifact_id)
            .order_by(_VERSIONS.c.version.desc())
            .limit(limit),
            None,
        )
        return [version for version in map(_version, rows) if version is not None]
