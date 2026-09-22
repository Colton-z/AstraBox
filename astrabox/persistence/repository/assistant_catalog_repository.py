"""Repository for the assistant_catalog collection.

Assistant identity + capability definition; cross-session immutable except for
``display_name`` / ``icon`` / ``description`` / overrides. ``engine_kind`` is
immutable once the corresponding ``assistant_workspace`` row materializes
(enforced in ``AssistantService.update_assistant``).
"""

from __future__ import annotations

from typing import Any

from astrabox.persistence.repository.backend import (
    get_async_collection,
    run_mongo_with_retry,
)
from astrabox.persistence.repository.index_verification import ensure_unique_index
from astrabox.persistence.repository.session_repository import _safe_create_index
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.common.utils.time_utils import utcnow_iso

logger = get_logger(__name__)


class AssistantCatalogRepository:
    def __init__(self) -> None:
        settings = load_astrabox_settings()
        self._collection_name = settings.assistant_catalog_collection
        self._index_ready = False

    async def _ensure_indexes(self) -> None:
        if self._index_ready:
            return
        collection = await get_async_collection(self._collection_name)
        if collection is not None:
            await ensure_unique_index(
                collection,
                "assistant_id",
                collection_name=self._collection_name,
            )
            try:
                await _safe_create_index(
                    collection,
                    [("owner_id", 1), ("deleted", 1), ("updated_at", -1)],
                )
                await _safe_create_index(collection, "credential_vault_ids")
            except Exception as exc:
                logger.warning("ensure assistant_catalog indexes failed: %s", exc)
        self._index_ready = True

    async def create_assistant(self, payload: dict[str, Any]) -> dict[str, Any]:
        await self._ensure_indexes()
        now = utcnow_iso()
        doc = {"deleted": False, "created_at": now, "updated_at": now, **payload}
        collection = await get_async_collection(self._collection_name)
        await run_mongo_with_retry(
            "assistant_catalog.create",
            lambda: collection.insert_one(doc),
        )
        stored = await run_mongo_with_retry(
            "assistant_catalog.read_after_create",
            lambda: collection.find_one({"assistant_id": doc["assistant_id"]}),
        )
        return stored or doc

    async def get_assistant(self, assistant_id: str) -> dict[str, Any] | None:
        collection = await get_async_collection(self._collection_name)
        return await run_mongo_with_retry(
            "assistant_catalog.get",
            lambda: collection.find_one(
                {"assistant_id": assistant_id, "deleted": {"$ne": True}}
            ),
        )

    async def list_assistants_by_credential_vault_id(
        self, vault_id: str
    ) -> list[dict[str, Any]]:
        """Return every active Assistant that still references the managed Vault."""
        target = str(vault_id or "").strip()
        if not target:
            return []
        collection = await get_async_collection(self._collection_name)

        async def _list() -> list[dict[str, Any]]:
            cursor = collection.find(
                {
                    "credential_vault_ids": target,
                    "deleted": {"$ne": True},
                }
            )
            return [doc async for doc in cursor]

        return await run_mongo_with_retry(
            "assistant_catalog.list_by_credential_vault_id",
            _list,
            fault_context={"vault_id": target},
        )

    async def list_assistants(self, limit: int = 50) -> list[dict[str, Any]]:
        collection = await get_async_collection(self._collection_name)

        async def _list() -> list[dict[str, Any]]:
            cursor = (
                collection.find({"deleted": {"$ne": True}})
                .sort("updated_at", -1)
                .limit(limit)
            )
            return [doc async for doc in cursor]

        return await run_mongo_with_retry("assistant_catalog.list", _list)

    async def update_assistant(
        self, assistant_id: str, updates: dict[str, Any]
    ) -> bool:
        updates = {**updates, "updated_at": utcnow_iso()}
        collection = await get_async_collection(self._collection_name)
        result = await run_mongo_with_retry(
            "assistant_catalog.update",
            lambda: collection.update_one(
                {"assistant_id": assistant_id, "deleted": {"$ne": True}},
                {"$set": updates},
            ),
        )
        return result.modified_count > 0

    async def soft_delete(self, assistant_id: str) -> bool:
        return await self.update_assistant(assistant_id, {"deleted": True})
