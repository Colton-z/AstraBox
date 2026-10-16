"""Read-only share links for sessions, one per session.

The token is the whole capability a link recipient presents. Sharing a session
again keeps its token, so links already handed out keep working; revoking
disables the link without discarding the token.

Rows are returned as dicts keyed by column name with times as ISO strings.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from astrabox.persistence.sql import execute, fetch_one, from_iso, to_iso
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
        statement = (
            pg_insert(_SHARES)
            .values(
                token=token,
                session_id=session_id,
                enabled=True,
                allow_download=bool(allow_download),
                expires_at=from_iso(expires_at),
                created_by=created_by,
            )
            .on_conflict_do_update(
                index_elements=["session_id"],
                set_={
                    "enabled": True,
                    "allow_download": bool(allow_download),
                    "expires_at": from_iso(expires_at),
                    "updated_at": func.now(),
                },
            )
            .returning(*_SHARES.c)
        )
        row = _view(await fetch_one(statement))
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
