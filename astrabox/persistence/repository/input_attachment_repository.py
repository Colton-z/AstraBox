"""Files a message carries: staged by the endpoint that took them, then owned by an input.

See :class:`~astrabox.persistence.sql.models.sessions.InputAttachment`. Binding
happens inside :meth:`SessionRuntimeRepository.accept_input`, so a staged file
becomes an input's in the same transaction that accepts the input.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from astrabox.persistence.sql import fetch_all, fetch_one, to_iso, transaction
from astrabox.persistence.sql.models.sessions import InputAttachment

_ATTACHMENTS = InputAttachment.__table__
_DESCRIPTION = [column for column in _ATTACHMENTS.c if column.name != "data"]

#: Expired staged files removed by one staging call; the rest wait for the next.
_PURGE_BATCH = 100


def _view(row: Any) -> dict[str, Any]:
    return {key: (to_iso(value) if isinstance(value, datetime) else value) for key, value in dict(row).items()}


class InputAttachmentRepository:
    async def stage(
        self, *, owner_user_id: str, scope: str, name: str, media_type: str, data: bytes, ttl: timedelta,
    ) -> dict[str, Any]:
        """Keep one file for ``owner_user_id`` until ``ttl`` passes or an input takes it.

        Staged files nothing took are removed here, a batch at a time, so no
        process has to sweep for them.
        """
        now = datetime.now(timezone.utc)
        row = {
            "attachment_id": uuid.uuid4().hex, "owner_user_id": owner_user_id, "scope": scope,
            "name": name, "media_type": media_type, "size": len(data), "data": data,
            "expires_at": now + ttl,
        }

        async def write(conn: AsyncConnection) -> None:
            expired = select(_ATTACHMENTS.c.attachment_id).where(
                _ATTACHMENTS.c.session_id.is_(None), _ATTACHMENTS.c.expires_at < now,
            ).limit(_PURGE_BATCH)
            ids = [value for (value,) in (await conn.execute(expired)).all()]
            if ids:
                await conn.execute(delete(_ATTACHMENTS).where(
                    _ATTACHMENTS.c.attachment_id.in_(ids), _ATTACHMENTS.c.session_id.is_(None),
                ))
            await conn.execute(insert(_ATTACHMENTS).values(**row))

        await transaction(write)
        return {key: value for key, value in row.items() if key != "data"} | {"expires_at": to_iso(row["expires_at"])}

    async def staged(
        self, attachment_ids: Sequence[str], *, owner_user_id: str, scope: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        """The caller's own unexpired staged files among ``attachment_ids``, with their bytes.

        ``scope``, when given, narrows them to the files one endpoint staged.
        """
        if not attachment_ids:
            return {}
        conditions = [
            _ATTACHMENTS.c.attachment_id.in_(list(attachment_ids)), _ATTACHMENTS.c.owner_user_id == owner_user_id,
            _ATTACHMENTS.c.session_id.is_(None), _ATTACHMENTS.c.expires_at > datetime.now(timezone.utc),
        ]
        if scope is not None:
            conditions.append(_ATTACHMENTS.c.scope == scope)
        rows = await fetch_all(select(_ATTACHMENTS).where(*conditions))
        return {str(row["attachment_id"]): _view(row) for row in rows}

    async def staged_file(self, attachment_id: str, *, owner_user_id: str, scope: str) -> dict[str, Any] | None:
        return (await self.staged([attachment_id], owner_user_id=owner_user_id, scope=scope)).get(attachment_id)

    async def for_input(self, session_id: str, input_id: str) -> list[dict[str, Any]]:
        """An input's files, with their bytes, in the order it sent them."""
        rows = await fetch_all(select(_ATTACHMENTS).where(
            _ATTACHMENTS.c.session_id == session_id, _ATTACHMENTS.c.input_id == input_id,
        ).order_by(_ATTACHMENTS.c.ordinal))
        return [_view(row) for row in rows]

    async def described_for_inputs(self, session_id: str, input_ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
        """Each input's files without their bytes, as history names them."""
        if not input_ids:
            return {}
        rows = await fetch_all(select(*_DESCRIPTION).where(
            _ATTACHMENTS.c.session_id == session_id, _ATTACHMENTS.c.input_id.in_(list(input_ids)),
        ).order_by(_ATTACHMENTS.c.input_id, _ATTACHMENTS.c.ordinal))
        found: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            found.setdefault(str(row["input_id"]), []).append(_view(row))
        return found

    async def bound_file(self, session_id: str, attachment_id: str) -> dict[str, Any] | None:
        row = await fetch_one(select(_ATTACHMENTS).where(
            _ATTACHMENTS.c.session_id == session_id, _ATTACHMENTS.c.attachment_id == attachment_id,
        ))
        return None if row is None else _view(row)
