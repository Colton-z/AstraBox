"""Persistence for the Agent categories a platform administrator defines.

Categories are deployment-wide, like the Agents they sort. An Agent names its
categories by id in its own ``categories`` field, so renaming a category
rewrites one record and never the Agents.
"""

from __future__ import annotations

import uuid
from typing import Any

from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.persistence.repository.backend import (
    get_async_collection,
    run_mongo_with_retry,
)
from astrabox.persistence.repository.index_verification import ensure_unique_index

AGENT_CATEGORIES_COLLECTION = "agent_categories"


class AgentCategoryRepository:
    def __init__(self) -> None:
        self._index_ready = False

    async def _ensure_indexes(self) -> None:
        if self._index_ready:
            return
        collection = await get_async_collection(AGENT_CATEGORIES_COLLECTION)
        await ensure_unique_index(
            collection, "category_id", collection_name=AGENT_CATEGORIES_COLLECTION
        )
        await ensure_unique_index(
            collection, "name_key", collection_name=AGENT_CATEGORIES_COLLECTION
        )
        self._index_ready = True

    async def list_all(self) -> list[dict[str, Any]]:
        """Every category, in the administrator's order."""
        await self._ensure_indexes()
        collection = await get_async_collection(AGENT_CATEGORIES_COLLECTION)

        async def _list() -> list[dict[str, Any]]:
            cursor = collection.find({}).sort([("position", 1), ("name_key", 1)])
            return [dict(doc) async for doc in cursor]

        rows = await run_mongo_with_retry("agent_categories.list", _list)
        for row in rows:
            row.pop("_id", None)
        return rows

    async def get(self, category_id: str) -> dict[str, Any] | None:
        await self._ensure_indexes()
        collection = await get_async_collection(AGENT_CATEGORIES_COLLECTION)
        doc = await run_mongo_with_retry(
            "agent_categories.get",
            lambda: collection.find_one({"category_id": category_id}),
        )
        if not isinstance(doc, dict):
            return None
        doc.pop("_id", None)
        return doc

    async def get_by_name_key(self, name_key: str) -> dict[str, Any] | None:
        await self._ensure_indexes()
        collection = await get_async_collection(AGENT_CATEGORIES_COLLECTION)
        doc = await run_mongo_with_retry(
            "agent_categories.get_by_name",
            lambda: collection.find_one({"name_key": name_key}),
        )
        if not isinstance(doc, dict):
            return None
        doc.pop("_id", None)
        return doc

    async def create(self, *, name: str, name_key: str, position: int) -> dict[str, Any]:
        await self._ensure_indexes()
        now = utcnow_iso()
        stored = {
            "category_id": f"cat_{uuid.uuid4().hex}",
            "name": name,
            "name_key": name_key,
            "position": position,
            "created_at": now,
            "updated_at": now,
        }
        collection = await get_async_collection(AGENT_CATEGORIES_COLLECTION)
        await run_mongo_with_retry(
            "agent_categories.create", lambda: collection.insert_one(dict(stored))
        )
        return stored

    async def update(self, category_id: str, updates: dict[str, Any]) -> bool:
        await self._ensure_indexes()
        collection = await get_async_collection(AGENT_CATEGORIES_COLLECTION)
        result = await run_mongo_with_retry(
            "agent_categories.update",
            lambda: collection.update_one(
                {"category_id": category_id},
                {"$set": {**updates, "updated_at": utcnow_iso()}},
            ),
        )
        return bool(getattr(result, "matched_count", 0))

    async def delete(self, category_id: str) -> bool:
        await self._ensure_indexes()
        collection = await get_async_collection(AGENT_CATEGORIES_COLLECTION)
        result = await run_mongo_with_retry(
            "agent_categories.delete",
            lambda: collection.delete_one({"category_id": category_id}),
        )
        return bool(getattr(result, "deleted_count", 0))
