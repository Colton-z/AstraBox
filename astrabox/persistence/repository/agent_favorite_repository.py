"""Persistence for the Agents each user has favorited.

One record per user and Agent, keyed by both, so favoriting and unfavoriting
are idempotent and two toggles from different tabs cannot overwrite each
other's other favorites the way a single per-user list would.
"""

from __future__ import annotations

from typing import Any

from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.persistence.repository.backend import (
    get_async_collection,
    run_mongo_with_retry,
)
from astrabox.persistence.repository.index_verification import ensure_unique_index
from astrabox.persistence.repository.session_repository import _safe_create_index

AGENT_FAVORITES_COLLECTION = "agent_favorites"


def _favorite_id(user_id: str, agent_id: str) -> str:
    return f"{user_id}:{agent_id}"


class AgentFavoriteRepository:
    def __init__(self) -> None:
        self._index_ready = False

    async def _ensure_indexes(self) -> None:
        if self._index_ready:
            return
        collection = await get_async_collection(AGENT_FAVORITES_COLLECTION)
        await ensure_unique_index(
            collection, "favorite_id", collection_name=AGENT_FAVORITES_COLLECTION
        )
        await _safe_create_index(collection, [("user_id", 1)])
        await _safe_create_index(collection, [("agent_id", 1)])
        self._index_ready = True

    async def add(self, user_id: str, agent_id: str) -> None:
        await self._ensure_indexes()
        collection = await get_async_collection(AGENT_FAVORITES_COLLECTION)
        favorite_id = _favorite_id(user_id, agent_id)
        await run_mongo_with_retry(
            "agent_favorites.add",
            lambda: collection.update_one(
                {"favorite_id": favorite_id},
                {
                    "$setOnInsert": {
                        "favorite_id": favorite_id,
                        "user_id": user_id,
                        "agent_id": agent_id,
                        "created_at": utcnow_iso(),
                    }
                },
                upsert=True,
            ),
        )

    async def remove(self, user_id: str, agent_id: str) -> None:
        await self._ensure_indexes()
        collection = await get_async_collection(AGENT_FAVORITES_COLLECTION)
        favorite_id = _favorite_id(user_id, agent_id)
        await run_mongo_with_retry(
            "agent_favorites.remove",
            lambda: collection.delete_one({"favorite_id": favorite_id}),
        )

    async def agent_ids_for(self, user_id: str) -> set[str]:
        await self._ensure_indexes()
        collection = await get_async_collection(AGENT_FAVORITES_COLLECTION)

        async def _list() -> list[dict[str, Any]]:
            return [doc async for doc in collection.find({"user_id": user_id})]

        rows = await run_mongo_with_retry("agent_favorites.list", _list)
        return {str(row.get("agent_id") or "") for row in rows if row.get("agent_id")}

    async def remove_agent(self, agent_id: str) -> None:
        """Every user's favorite of one Agent, when the Agent is deleted."""
        await self._ensure_indexes()
        collection = await get_async_collection(AGENT_FAVORITES_COLLECTION)
        await run_mongo_with_retry(
            "agent_favorites.remove_agent",
            lambda: collection.delete_many({"agent_id": agent_id}),
        )
