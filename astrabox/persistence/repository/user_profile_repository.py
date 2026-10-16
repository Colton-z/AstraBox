"""Display names of users, recorded when a verified identity is first admitted."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from astrabox.persistence.sql import fetch_all, transaction
from astrabox.persistence.sql.models.platform import UserProfile
from astrabox.persistence.sql.statements import insert_if_absent

_PROFILES = UserProfile.__table__


class UserProfileRepository:
    """User id to display name; a stored name is never overwritten."""

    async def upsert_if_absent(self, user_id: str, display_name: str) -> None:
        """Record a user's display name once; a stored profile is never overwritten.

        Runs on every conversation create. Concurrent first creates keep the
        winning profile; an existing display name is never updated.
        """
        if not user_id or not display_name:
            return

        async def add(conn: AsyncConnection) -> None:
            await insert_if_absent(
                conn,
                _PROFILES,
                {"user_id": user_id, "display_name": display_name},
                index_columns=["user_id"],
            )

        await transaction(add)

    async def batch_get_display_names(self, user_ids: list[str]) -> dict[str, str]:
        """``{user_id: display_name}`` for the ids that have a profile, in one query."""
        ids = sorted({user_id for user_id in user_ids if user_id})
        if not ids:
            return {}
        rows = await fetch_all(
            select(_PROFILES.c.user_id, _PROFILES.c.display_name).where(
                _PROFILES.c.user_id.in_(ids)
            )
        )
        return {row["user_id"]: row["display_name"] for row in rows}
