"""Display names of users, recorded when a verified identity is first admitted."""

from __future__ import annotations

from sqlalchemy import Text, any_, literal, select
from sqlalchemy.dialects.postgresql import ARRAY, insert

from astrabox.persistence.sql import execute, fetch_all
from astrabox.persistence.sql.models.platform import UserProfile

_PROFILES = UserProfile.__table__


class UserProfileRepository:
    """User id to display name; a stored name is never overwritten."""

    async def upsert_if_absent(self, user_id: str, display_name: str) -> None:
        """Record a user's display name once; a stored profile is never overwritten.

        Runs on every conversation create. It is one statement that does
        nothing when the profile exists, so a concurrent first create neither
        fails nor waits on a row lock.
        """
        if not user_id or not display_name:
            return
        await execute(
            insert(_PROFILES)
            .values(user_id=user_id, display_name=display_name)
            .on_conflict_do_nothing(index_elements=[_PROFILES.c.user_id])
        )

    async def batch_get_display_names(self, user_ids: list[str]) -> dict[str, str]:
        """``{user_id: display_name}`` for the ids that have a profile, in one query."""
        ids = sorted({user_id for user_id in user_ids if user_id})
        if not ids:
            return {}
        rows = await fetch_all(
            select(_PROFILES.c.user_id, _PROFILES.c.display_name).where(
                _PROFILES.c.user_id == any_(literal(ids, ARRAY(Text)))
            )
        )
        return {row["user_id"]: row["display_name"] for row in rows}
