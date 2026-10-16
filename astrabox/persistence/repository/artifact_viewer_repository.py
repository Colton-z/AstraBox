"""Signed-in viewers' opaque per-artifact ids and display names."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from astrabox.persistence.repository._artifact_repository import every, run
from astrabox.persistence.sql import ForeignKeyViolation
from astrabox.persistence.sql.models.artifacts import ArtifactViewer

_VIEWERS = ArtifactViewer.__table__


class ArtifactViewerRepository:
    """The viewer profiles ``user.profiles`` reads."""

    async def register(
        self, artifact_id: str, viewer_id: str, *, user_id: str, display_name: str
    ) -> bool:
        """Record the viewer; False when the artifact has been deleted.

        One statement, outside any lock on the artifact. A row is rewritten
        only when the display name changed, so a returning viewer's request
        writes nothing.
        """
        try:
            await run(
                pg_insert(_VIEWERS)
                .values(
                    artifact_id=artifact_id,
                    viewer_id=viewer_id,
                    user_id=user_id,
                    display_name=display_name,
                )
                .on_conflict_do_update(
                    index_elements=[_VIEWERS.c.artifact_id, _VIEWERS.c.viewer_id],
                    set_={"display_name": display_name, "updated_at": func.now()},
                    where=_VIEWERS.c.display_name != display_name,
                ),
                None,
            )
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
