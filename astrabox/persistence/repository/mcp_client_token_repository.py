"""Persistence for the API keys an MCP client authenticates with.

The row holds ``secret_digest`` and never the secret. A credential store that
can hand back live credentials turns a leaked backup into a leaked deployment,
and nothing here needs the original: verification hashes what the caller sent
and compares digests.

Design: `docs/maintainers/mcp-client-tokens.md`.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, func, insert, select, update

from astrabox.common.logger.logger_factory import get_logger
from astrabox.persistence.sql import execute, fetch_all, fetch_one, from_iso, to_iso
from astrabox.persistence.sql.models.mcp import MCPClientToken

logger = get_logger(__name__)

_TOKENS = MCPClientToken.__table__


def _view(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        **row,
        "expires_at": to_iso(row["expires_at"]),
        "created_at": to_iso(row["created_at"]),
        "last_used_at": to_iso(row["last_used_at"]),
    }


class MCPClientTokenRepository:
    """Rows for issued MCP client keys, looked up by digest on every call."""

    async def create(
        self,
        *,
        user_id: str,
        org_id: str | None,
        name: str,
        scope: str,
        secret_digest: str,
        expires_at: str | None,
    ) -> dict[str, Any]:
        row = await fetch_one(
            insert(_TOKENS)
            .values(
                token_id=f"mtk_{uuid.uuid4().hex}",
                user_id=user_id,
                org_id=org_id,
                name=name,
                scope=scope,
                secret_digest=secret_digest,
                expires_at=from_iso(expires_at),
            )
            .returning(_TOKENS)
        )
        stored = _view(row)
        assert stored is not None
        return stored

    async def list_for_user(self, user_id: str) -> list[dict[str, Any]]:
        rows = await fetch_all(
            select(_TOKENS)
            .where(_TOKENS.c.user_id == user_id)
            .order_by(_TOKENS.c.created_at.desc(), _TOKENS.c.token_id)
        )
        return [row for row in map(_view, rows) if row is not None]

    async def find_by_digest(self, secret_digest: str) -> dict[str, Any] | None:
        return _view(
            await fetch_one(select(_TOKENS).where(_TOKENS.c.secret_digest == secret_digest))
        )

    async def delete(self, *, user_id: str, token_id: str) -> bool:
        """Delete this user's token. False when the id is not theirs.

        The owner is part of the statement's condition rather than checked after
        a read, so the delete cannot be raced into removing somebody else's row.
        """
        removed = await execute(
            delete(_TOKENS).where(_TOKENS.c.user_id == user_id, _TOKENS.c.token_id == token_id)
        )
        return removed == 1

    async def touch(self, token_id: str) -> None:
        """Record use. Best-effort: a failure here must not refuse a good call."""
        try:
            await execute(
                update(_TOKENS).where(_TOKENS.c.token_id == token_id).values(last_used_at=func.now())
            )
        except Exception:  # noqa: BLE001 - liveness reporting, never authorization
            logger.warning("MCP client token use was not recorded token=%s", token_id, exc_info=True)
