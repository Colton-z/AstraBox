"""Cards a platform MCP tool shows in a conversation, each for a session or an Assistant."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import insert, or_, select

from astrabox.persistence.sql import execute, fetch_all, to_iso
from astrabox.persistence.sql.models.sessions import Session, ToolCard

_CARDS = ToolCard.__table__
_SESSIONS = Session.__table__

_COLUMNS = ("card_id", "session_id", "assistant_id", "server", "tool", "kind", "data")


class ToolCardRepository:
    async def create(self, card: dict[str, Any]) -> None:
        """Record one card; it belongs to exactly one session or one Assistant."""
        await execute(insert(_CARDS).values(**{key: card.get(key) for key in _COLUMNS}))

    async def named(self, session_id: str, card_ids: list[str]) -> list[dict[str, Any]]:
        """The cards among ``card_ids`` that ``session_id`` may show.

        Those are the cards recorded for the session and, in an Assistant's
        session, the cards recorded for that Assistant. An id with no such
        card is absent.
        """
        ids = sorted({str(card_id) for card_id in card_ids if card_id})
        if not ids or not session_id:
            return []
        assistant = (
            select(_SESSIONS.c.assistant_id)
            .where(_SESSIONS.c.session_id == session_id)
            .scalar_subquery()
        )
        rows = await fetch_all(
            select(_CARDS).where(
                _CARDS.c.card_id.in_(ids),
                or_(_CARDS.c.session_id == session_id, _CARDS.c.assistant_id == assistant),
            )
        )
        return [
            {key: (to_iso(value) if isinstance(value, datetime) else value) for key, value in row.items()}
            for row in rows
        ]
