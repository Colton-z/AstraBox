"""Persistence for administrator-registered remote MCP servers."""

from __future__ import annotations

import uuid
from typing import Any

from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.persistence.repository.backend import (
    get_async_collection,
    run_mongo_with_retry,
)
from astrabox.persistence.repository.index_verification import ensure_unique_index
from astrabox.persistence.repository.session_repository import _safe_create_index

MCP_SERVERS_COLLECTION = "mcp_servers"


class MCPServerRepository:
    """Small metadata store used by the first-release MCP registry."""

    def __init__(self) -> None:
        self._index_ready = False

    async def _ensure_indexes(self) -> None:
        if self._index_ready:
            return
        collection = await get_async_collection(MCP_SERVERS_COLLECTION)
        await ensure_unique_index(
            collection,
            "mcp_server_id",
            collection_name=MCP_SERVERS_COLLECTION,
        )
        await _safe_create_index(collection, [("org_id", 1), ("name", 1)])
        await _safe_create_index(collection, [("org_id", 1), ("updated_at", -1)])
        self._index_ready = True

    async def create(self, doc: dict[str, Any]) -> dict[str, Any]:
        await self._ensure_indexes()
        now = utcnow_iso()
        stored = {
            "mcp_server_id": str(
                doc.get("mcp_server_id") or f"mcp_{uuid.uuid4().hex}"
            ),
            "created_at": now,
            "updated_at": now,
            **doc,
        }
        collection = await get_async_collection(MCP_SERVERS_COLLECTION)
        await run_mongo_with_retry(
            "mcp_servers.create", lambda: collection.insert_one(dict(stored))
        )
        return stored

    async def get(self, mcp_server_id: str) -> dict[str, Any] | None:
        await self._ensure_indexes()
        collection = await get_async_collection(MCP_SERVERS_COLLECTION)
        doc = await run_mongo_with_retry(
            "mcp_servers.get",
            lambda: collection.find_one({"mcp_server_id": mcp_server_id}),
        )
        return doc if isinstance(doc, dict) else None

    async def get_by_name(self, *, org_id: str, name: str) -> dict[str, Any] | None:
        await self._ensure_indexes()
        collection = await get_async_collection(MCP_SERVERS_COLLECTION)
        doc = await run_mongo_with_retry(
            "mcp_servers.get_by_name",
            lambda: collection.find_one({"org_id": org_id, "name": name}),
        )
        return doc if isinstance(doc, dict) else None

    async def list_for_org(
        self, *, org_id: str, limit: int = 500
    ) -> list[dict[str, Any]]:
        await self._ensure_indexes()
        collection = await get_async_collection(MCP_SERVERS_COLLECTION)

        async def _list() -> list[dict[str, Any]]:
            cursor = collection.find({"org_id": org_id}).sort("updated_at", -1).limit(limit)
            return [doc async for doc in cursor]

        return await run_mongo_with_retry("mcp_servers.list", _list)

    async def list_by_ids(
        self, mcp_server_ids: list[str], *, org_id: str
    ) -> list[dict[str, Any]]:
        ids = sorted(
            {
                str(value or "").strip()
                for value in mcp_server_ids
                if str(value or "").strip()
            }
        )
        if not ids:
            return []
        await self._ensure_indexes()
        collection = await get_async_collection(MCP_SERVERS_COLLECTION)

        async def _list() -> list[dict[str, Any]]:
            cursor = collection.find(
                {"org_id": org_id, "mcp_server_id": {"$in": ids}}
            )
            return [doc async for doc in cursor]

        return await run_mongo_with_retry("mcp_servers.list_by_ids", _list)

    async def update(
        self, mcp_server_id: str, updates: dict[str, Any]
    ) -> dict[str, Any] | None:
        await self._ensure_indexes()
        collection = await get_async_collection(MCP_SERVERS_COLLECTION)
        await run_mongo_with_retry(
            "mcp_servers.update",
            lambda: collection.update_one(
                {"mcp_server_id": mcp_server_id},
                {"$set": {**updates, "updated_at": utcnow_iso()}},
            ),
        )
        return await self.get(mcp_server_id)

    async def delete(self, mcp_server_id: str) -> None:
        await self._ensure_indexes()
        collection = await get_async_collection(MCP_SERVERS_COLLECTION)
        await run_mongo_with_retry(
            "mcp_servers.delete",
            lambda: collection.delete_many({"mcp_server_id": mcp_server_id}),
        )
