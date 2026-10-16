"""Persistence for administrator-registered remote MCP servers."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, func, insert, select, update

from astrabox.persistence.sql import UniqueViolation, execute, fetch_all, fetch_one, to_iso
from astrabox.persistence.sql.models.mcp import MCPServer

_SERVERS = MCPServer.__table__


class MCPServerNameTaken(Exception):
    """The organization already has an MCP server with this name."""


def _view(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {**row, "created_at": to_iso(row["created_at"]), "updated_at": to_iso(row["updated_at"])}


class MCPServerRepository:
    """Registered MCP servers, one organization's names unique."""

    async def create(self, doc: dict[str, Any]) -> dict[str, Any]:
        """Register a server; raises :class:`MCPServerNameTaken` for a duplicate name."""
        values = {
            "mcp_server_id": str(doc.get("mcp_server_id") or f"mcp_{uuid.uuid4().hex}"),
            "org_id": doc["org_id"],
            "name": doc["name"],
            "description": doc.get("description"),
            "url": doc["url"],
            "transport": doc["transport"],
            "enabled": bool(doc.get("enabled", True)),
            "created_by": doc.get("created_by"),
            "updated_by": doc.get("updated_by"),
        }
        try:
            row = await fetch_one(insert(_SERVERS).values(**values).returning(_SERVERS))
        except UniqueViolation as exc:
            raise MCPServerNameTaken(values["name"]) from exc
        stored = _view(row)
        assert stored is not None
        return stored

    async def get(self, mcp_server_id: str) -> dict[str, Any] | None:
        return _view(
            await fetch_one(select(_SERVERS).where(_SERVERS.c.mcp_server_id == mcp_server_id))
        )

    async def list_for_org(self, *, org_id: str, limit: int = 500) -> list[dict[str, Any]]:
        rows = await fetch_all(
            select(_SERVERS)
            .where(_SERVERS.c.org_id == org_id)
            .order_by(_SERVERS.c.updated_at.desc(), _SERVERS.c.mcp_server_id)
            .limit(limit)
        )
        return [row for row in map(_view, rows) if row is not None]

    async def list_by_ids(self, mcp_server_ids: list[str], *, org_id: str) -> list[dict[str, Any]]:
        ids = sorted({str(value or "").strip() for value in mcp_server_ids} - {""})
        if not ids:
            return []
        rows = await fetch_all(
            select(_SERVERS).where(
                _SERVERS.c.org_id == org_id, _SERVERS.c.mcp_server_id.in_(ids)
            )
        )
        return [row for row in map(_view, rows) if row is not None]

    async def update(self, mcp_server_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        """Apply ``updates``; ``None`` when no such server exists.

        Raises :class:`MCPServerNameTaken` when a rename collides.
        """
        allowed = {"name", "description", "url", "transport", "enabled", "updated_by"}
        values = {key: value for key, value in updates.items() if key in allowed}
        try:
            row = await fetch_one(
                update(_SERVERS)
                .where(_SERVERS.c.mcp_server_id == mcp_server_id)
                .values(**values, updated_at=func.now())
                .returning(_SERVERS)
            )
        except UniqueViolation as exc:
            raise MCPServerNameTaken(str(values.get("name") or "")) from exc
        return _view(row)

    async def delete(self, mcp_server_id: str) -> None:
        await execute(delete(_SERVERS).where(_SERVERS.c.mcp_server_id == mcp_server_id))
