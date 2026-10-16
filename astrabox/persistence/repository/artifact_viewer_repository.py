"""Signed-in viewers' opaque per-artifact ids and display names."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from astrabox.persistence.repository._artifact_repository import every, one, run
from astrabox.persistence.sql import ForeignKeyViolation, transaction
from astrabox.persistence.sql.models.artifacts import ArtifactViewer
from astrabox.persistence.sql.statements import insert_if_absent_primary_key

_VIEWERS = ArtifactViewer.__table__


class ArtifactViewerRepository:
    """The viewer profiles ``user.profiles`` reads."""

    async def register(
        self, artifact_id: str, viewer_id: str, *, user_id: str, display_name: str
    ) -> bool:
        """Record the viewer; False when the artifact has been deleted.

        Registration does not acquire the artifact's mutation lock. A profile
        is rewritten only when the display name changed, so returning viewers
        keep their stored timestamp and user identity.
        """
        async def register(conn: AsyncConnection) -> None:
            await run(
                insert_if_absent_primary_key(conn, _VIEWERS, {
                    "artifact_id": artifact_id,
                    "viewer_id": viewer_id,
                    "user_id": user_id,
                    "display_name": display_name,
                }),
                conn,
            )
            existing = await one(
                select(_VIEWERS.c.display_name)
                .where(_VIEWERS.c.artifact_id == artifact_id, _VIEWERS.c.viewer_id == viewer_id)
                .with_for_update(),
                conn,
            )
            assert existing is not None
            if existing["display_name"] == display_name:
                return
            await run(
                update(_VIEWERS)
                .where(_VIEWERS.c.artifact_id == artifact_id, _VIEWERS.c.viewer_id == viewer_id)
                .values(display_name=display_name, updated_at=func.now()),
                conn,
            )

        try:
            await transaction(register)
        except ForeignKeyViolation:
            return False
        return True

    async def profiles(self, artifact_id: str, viewer_ids: list[str]) -> list[dict[str, Any]]:
        """``{viewer_id, display_name}`` of the registered viewers among ``viewer_ids``."""
        if not viewer_ids:
            return []
        return await every(
            select(_VIEWERS.c.viewer_id, _VIEWERS.c.display_name).where(
                _VIEWERS.c.artifact_id == artifact_id, _VIEWERS.c.viewer_id.in_(viewer_ids)
            ),
            None,
        )
