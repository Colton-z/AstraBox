"""Cards a platform MCP tool shows in a conversation, each for a Session or an Assistant."""

from __future__ import annotations

from typing import Any

from astrabox.persistence.repository.backend import get_async_collection, run_mongo_with_retry
from astrabox.persistence.repository.index_verification import ensure_unique_index

COLLECTION_NAME = "tool_cards"

_index_ready = False


class ToolCardRepository:
    async def ensure_indexes(self) -> None:
        global _index_ready
        if _index_ready:
            return
        collection = await get_async_collection(COLLECTION_NAME)
        if collection is not None:
            await ensure_unique_index(
                collection,
                [("card_id", 1)],
                name="ux_tool_cards_card_id",
                collection_name=COLLECTION_NAME,
            )
        _index_ready = True

    async def create(self, card: dict[str, Any]) -> None:
        await self.ensure_indexes()
        collection = await get_async_collection(COLLECTION_NAME)
        document = dict(card)
        await run_mongo_with_retry("tool_cards.create", lambda: collection.insert_one(document))

    async def named(self, card_ids: list[str]) -> list[dict[str, Any]]:
        """The recorded cards among ``card_ids``; an id with no record is absent."""

        await self.ensure_indexes()
        collection = await get_async_collection(COLLECTION_NAME)

        async def _find() -> list[dict[str, Any]]:
            cursor = collection.find({"card_id": {"$in": list(card_ids)}})
            return [doc async for doc in cursor]

        return await run_mongo_with_retry("tool_cards.named", _find)
