"""The platform MCP server's routes from a box's address to the subject it serves.

A row exists for two kinds of address (see
:class:`~astrabox.persistence.sql.models.sessions.PlatformMCPBinding`): an
Assistant workspace's, and a prepared unit's, which a session claims. A
session started without a prepared unit is addressed by its own id and has no
row; the platform MCP service reads such a session directly.

Rows end with what they route to. A deleted Assistant takes its workspace's
row through the workspace's foreign key; a deleted session takes the rows it
claimed (:meth:`~astrabox.persistence.repository.session_repository.SessionRepository.soft_delete`);
a discarded unit's row is deleted by the code that discards it, and an
abandoned unit's by :meth:`PlatformMCPBindingRepository.delete_unclaimed_prepared_bindings`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from astrabox.persistence.sql import execute, fetch_one, from_iso, to_iso
from astrabox.persistence.sql.models.sessions import PlatformMCPBinding

_BINDINGS = PlatformMCPBinding.__table__

#: What a publish may set; the times are the table's own.
_WRITABLE = frozenset(_BINDINGS.columns.keys()) - {"created_at", "updated_at"}


def _view(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        key: (to_iso(value) if isinstance(value, datetime) else value) for key, value in row.items()
    }


class PlatformMCPBindingRepository:
    """Platform MCP routes, one row per address."""

    async def upsert_binding(self, binding: dict[str, Any]) -> dict[str, Any]:
        """Publish an address's route; the stored row.

        Keys name columns, and a key that is not a writable column raises
        ``KeyError``. Publishing an address again replaces the columns it
        names and keeps the others, including when the row was created.
        """
        unknown = set(binding) - _WRITABLE
        if unknown:
            raise KeyError(f"platform_mcp_bindings has no writable column {sorted(unknown)!r}")
        deployment_id = str(binding.get("deployment_id") or "").strip()
        if not deployment_id:
            raise ValueError("deployment_id is required")
        values = {
            key: (str(value).strip() or None if isinstance(value, str) else value)
            for key, value in binding.items()
        }
        values["deployment_id"] = deployment_id
        statement = pg_insert(_BINDINGS).values(**values)
        row = await fetch_one(
            statement.on_conflict_do_update(
                index_elements=[_BINDINGS.c.deployment_id],
                set_={
                    **{key: statement.excluded[key] for key in values if key != "deployment_id"},
                    "updated_at": func.now(),
                },
            ).returning(_BINDINGS)
        )
        stored = _view(row)
        assert stored is not None
        return stored

    async def claim_prepared_binding(
        self, deployment_id: str, *, session_id: str, user_id: str | None
    ) -> bool:
        """Route a prepared unit's address to the session that claimed the unit.

        A session runs one runtime, so the addresses its earlier runtimes
        claimed are deleted in the same statement. ``False`` when the address
        has no route left to claim: the unit's row was deleted, or another
        session holds it.
        """
        target = str(deployment_id or "").strip()
        session = str(session_id or "").strip()
        if not target or not session:
            raise ValueError("claiming a prepared binding requires its address and a session")
        superseded = (
            delete(_BINDINGS)
            .where(_BINDINGS.c.session_id == session, _BINDINGS.c.deployment_id != target)
            .cte("superseded")
        )
        claimed = (
            update(_BINDINGS)
            .where(
                _BINDINGS.c.deployment_id == target,
                _BINDINGS.c.source == "prepared_slot",
                or_(_BINDINGS.c.session_id.is_(None), _BINDINGS.c.session_id == session),
            )
            .values(
                session_id=session,
                user_id=str(user_id or "").strip() or None,
                updated_at=func.now(),
            )
            .returning(_BINDINGS.c.deployment_id)
            .add_cte(superseded)
        )
        return await fetch_one(claimed) is not None

    async def get_binding(self, deployment_id: str) -> dict[str, Any] | None:
        normalized = str(deployment_id or "").strip()
        if not normalized:
            return None
        return _view(
            await fetch_one(select(_BINDINGS).where(_BINDINGS.c.deployment_id == normalized))
        )

    async def delete_binding(self, deployment_id: str) -> bool:
        """Remove one address's route; whether a row existed.

        Discarding a prepared unit deletes its route here, claimed or not:
        the unit's box is gone with it.
        """
        normalized = str(deployment_id or "").strip()
        if not normalized:
            return False
        return await execute(delete(_BINDINGS).where(_BINDINGS.c.deployment_id == normalized)) > 0

    async def delete_unclaimed_prepared_bindings(self, *, created_before: str) -> int:
        """Remove the routes of prepared units no session claimed, published before a time."""
        return await execute(
            delete(_BINDINGS).where(
                _BINDINGS.c.source == "prepared_slot",
                _BINDINGS.c.session_id.is_(None),
                _BINDINGS.c.created_at < from_iso(created_before),
            )
        )
