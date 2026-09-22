from __future__ import annotations

from typing import Any

from astrabox.persistence.repository.backend import (
    get_async_collection,
    run_mongo_with_retry,
)
from astrabox.common.utils.settings import load_astrabox_settings


class EnvironmentRepository:
    """Admin-managed runtime environment presets, keyed by name.

    One document per environment, identified by a unique ``name``. Used by the
    management console to CRUD coarse-grained runtime presets (Claude Code /
    Assistant). Besides the sandbox runtime fields it carries ``provider_access``
    (base_url / api_key), the model-provider credentials shared across the Agents
    that select this Environment (``docs/domain-model.md``).
    """

    def __init__(self) -> None:
        settings = load_astrabox_settings()
        self._collection_name = settings.environment_collection

    async def list_all(self) -> list[dict[str, Any]]:
        async def _list() -> list[dict[str, Any]]:
            collection = await get_async_collection(self._collection_name)
            cursor = collection.find({})
            docs = [doc async for doc in cursor]
            docs.sort(key=lambda item: str(item.get("name") or ""))
            return docs

        return await run_mongo_with_retry("environments.list_all", _list)

    async def count_all(self) -> int:
        async def _count() -> int:
            collection = await get_async_collection(self._collection_name)
            return int(await collection.count_documents({}))

        return await run_mongo_with_retry("environments.count_all", _count)

    async def get_any_by_name(self, name: str) -> dict[str, Any] | None:
        collection = await get_async_collection(self._collection_name)
        return await run_mongo_with_retry(
            "environments.get_any_by_name",
            lambda: collection.find_one({"name": name}),
        )

    async def upsert_by_name(self, name: str, doc: dict[str, Any]) -> None:
        """Insert or update an environment preset by name."""
        collection = await get_async_collection(self._collection_name)
        doc_with_name = {**doc, "name": name}
        await run_mongo_with_retry(
            "environments.upsert_by_name",
            lambda: collection.update_one(
                {"name": name},
                {"$set": doc_with_name},
                upsert=True,
            ),
        )

    async def upsert(self, name: str, doc: dict[str, Any]) -> dict[str, Any]:
        """Upsert and return the stored document."""
        await self.upsert_by_name(name, doc)
        stored = await self.get_any_by_name(name)
        return stored or {**doc, "name": name}
