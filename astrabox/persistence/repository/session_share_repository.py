"""Read-only share links for sessions, one per session.

The token is the whole capability a link recipient presents. Sharing a session
again keeps its token, so links already handed out keep working; revoking
disables the link without discarding the token.

Rows are returned as dicts keyed by column name with times as ISO strings.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from astrabox.persistence.sql import execute, fetch_one, from_iso, to_iso, transaction
from astrabox.persistence.sql.models.sessions import Session, SessionShare

_SHARES = SessionShare.__table__
_SESSIONS = Session.__table__


def _view(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: (to_iso(value) if isinstance(value, datetime) else value) for key, value in row.items()}


class SessionShareRepository:
    async def get(self, session_id: str) -> dict[str, Any] | None:
        return _view(await fetch_one(select(_SHARES).where(_SHARES.c.session_id == session_id)))

    async def share(
        self,
        session_id: str,
        *,
        token: str,
        created_by: str,
        allow_download: bool,
        expires_at: str | None,
    ) -> dict[str, Any]:
        """Enable the session's link, keeping the token it already has; ``token`` names a new link."""
        values = {
            "enabled": True,
            "allow_download": bool(allow_download),
            "expires_at": from_iso(expires_at),
        }

        async def enable(conn: AsyncConnection) -> dict[str, Any]:
            # The parent exists before the first share, so it serializes creates
            # without treating a collision on another session's token as an upsert.
            await conn.execute(
                select(_SESSIONS.c.session_id)
                .where(_SESSIONS.c.session_id == session_id)
                .with_for_update()
            )
            result = await conn.execute(
                select(_SHARES.c.token)
                .where(_SHARES.c.session_id == session_id)
                .with_for_update()
            )
            existing = result.first()
            if existing is None:
                await conn.execute(
                    insert(_SHARES).values(
                        **values, token=token, session_id=session_id, created_by=created_by
                    )
                )
            else:
                await conn.execute(
                    update(_SHARES)
                    .where(_SHARES.c.session_id == session_id)
                    .values(**values, updated_at=func.now())
                )
            result = await conn.execute(
                select(_SHARES).where(_SHARES.c.session_id == session_id)
            )
            return dict(result.mappings().one())

        row = _view(await transaction(enable))
        if row is None:
            raise RuntimeError(f"share of session {session_id!r} was not stored")
        return row

    async def revoke(self, session_id: str) -> None:
        await execute(
            update(_SHARES)
            .where(_SHARES.c.session_id == session_id)
            .values(enabled=False, updated_at=func.now())
        )

    async def resolve(self, token: str) -> dict[str, Any] | None:
        """The share a token names, while its session is not deleted; enablement and expiry are the caller's to judge."""
        return _view(
            await fetch_one(
                select(_SHARES)
                .join(_SESSIONS, _SESSIONS.c.session_id == _SHARES.c.session_id)
                .where(_SHARES.c.token == token, _SESSIONS.c.deleted.is_(False))
            )
        )
