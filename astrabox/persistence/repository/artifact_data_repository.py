"""The documents of an artifact's ``db``."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncConnection

from astrabox.persistence.repository._artifact_repository import every, one, run
from astrabox.persistence.sql import to_iso
from astrabox.persistence.sql.models.artifacts import ArtifactDocument
from astrabox.persistence.sql.statements import upsert_primary_key

_DOCUMENTS = ArtifactDocument.__table__


class ArtifactDataRepository:
    """Documents keyed by artifact and path; times are ISO-8601 strings."""

    async def get(
        self, artifact_id: str, path: str, *, conn: AsyncConnection | None = None
    ) -> dict[str, Any] | None:
        row = await one(
            select(_DOCUMENTS).where(
                _DOCUMENTS.c.artifact_id == artifact_id, _DOCUMENTS.c.path == path
            ),
            conn,
        )
        if row is None:
            return None
        return {**row, "created_at": to_iso(row["created_at"]), "updated_at": to_iso(row["updated_at"])}

    async def put(
        self,
        artifact_id: str,
        path: str,
        *,
        collection: str,
        data: dict[str, Any],
        updated_by: str,
        now: datetime,
        conn: AsyncConnection,
    ) -> None:
        """Create or replace the document at ``path``; a replaced document keeps its ``created_at``."""
        await run(
            upsert_primary_key(
                conn,
                _DOCUMENTS,
                {
                    "artifact_id": artifact_id,
                    "path": path,
                    "collection": collection,
                    "data": data,
                    "updated_by": updated_by,
                    "created_at": now,
                    "updated_at": now,
                },
                update_columns=["data", "updated_by", "updated_at"],
            ),
            conn,
        )

    async def delete(self, artifact_id: str, path: str, *, conn: AsyncConnection) -> bool:
        """Delete the document at ``path``; whether there was one."""
        removed = await run(
            delete(_DOCUMENTS).where(
                _DOCUMENTS.c.artifact_id == artifact_id, _DOCUMENTS.c.path == path
            ),
            conn,
        )
        return removed == 1

    async def in_collection(self, artifact_id: str, collection: str) -> list[dict[str, Any]]:
        """``{path, data}`` of every document directly in ``collection``, by path."""
        return await every(
            select(_DOCUMENTS.c.path, _DOCUMENTS.c.data)
            .where(
                _DOCUMENTS.c.artifact_id == artifact_id, _DOCUMENTS.c.collection == collection
            )
            .order_by(_DOCUMENTS.c.path),
            None,
        )
