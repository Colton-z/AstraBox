"""Repository for the assistant_catalog collection.

Assistant identity + capability definition; cross-session immutable except for
``display_name`` / ``icon`` / ``description`` / ``system`` / overrides.
``engine_kind`` is immutable once the corresponding ``assistant_workspace`` row
materializes (enforced in ``AssistantService.update_assistant``).
"""

from __future__ import annotations

from typing import Any

from astrabox.persistence.repository.backend import (
    get_async_collection,
    run_mongo_with_retry,
)
from astrabox.persistence.repository.environment_repository import write_environment_binding
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
        await write_environment_binding(
            self._collection_name, str(doc.get("environment_name") or "").strip(),
            "assistant_catalog.create", lambda bound: bound.insert_one(doc),
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

    async def list_assistants_by_environment(
        self, name: str, *, transaction: Any = None,
    ) -> list[dict[str, Any]]:
        """Every Assistant referencing a preset, including deleted cleanup owners."""
        collection = (
            transaction.collection(self._collection_name) if transaction is not None
            else await get_async_collection(self._collection_name)
        )

        async def _list() -> list[dict[str, Any]]:
            cursor = collection.find(
                {"environment_name": name},
                projection={"assistant_id": 1, "display_name": 1, "deleted": 1},
            )
            return [doc async for doc in cursor]

        if transaction is not None:
            return await _list()
        return await run_mongo_with_retry("assistant_catalog.list_by_environment", _list)

    async def list_owner_assistants(self, owner_id: str) -> list[dict[str, Any]]:
        """Every live Assistant ``owner_id`` owns, the most recently edited first."""

        collection = await get_async_collection(self._collection_name)

        async def _list() -> list[dict[str, Any]]:
            cursor = collection.find(
                {"owner_id": str(owner_id or ""), "deleted": {"$ne": True}}
            ).sort([("updated_at", -1), ("assistant_id", -1)])
            return [doc async for doc in cursor]

        return await run_mongo_with_retry("assistant_catalog.list_owner", _list)

    async def list_owner_assistants_page(
        self,
        owner_id: str,
        *,
        after: tuple[str, str] | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """One page of :meth:`list_owner_assistants`, after ``after``.

        ``after`` is the ``(updated_at, assistant_id)`` of the last row already
        read; a page shorter than ``limit`` is the last. Both keys are strings
        on every row (``create_assistant`` stamps ``updated_at``), so the store
        orders the page itself and reads only the page.
        """

        page_limit = max(1, min(int(limit or 50), 10_000))
        query: dict[str, Any] = {"owner_id": str(owner_id or ""), "deleted": {"$ne": True}}
        if after is not None:
            updated_at, assistant_id = after
            query["$or"] = [
                {"updated_at": {"$lt": updated_at}},
                {"updated_at": updated_at, "assistant_id": {"$lt": assistant_id}},
            ]
        collection = await get_async_collection(self._collection_name)

        async def _list() -> list[dict[str, Any]]:
            cursor = (
                collection.find(query)
                .sort([("updated_at", -1), ("assistant_id", -1)], string_keyed=True)
                .limit(page_limit)
            )
            return [doc async for doc in cursor]

        return await run_mongo_with_retry("assistant_catalog.list_owner_page", _list)

    async def update_assistant(
        self, assistant_id: str, updates: dict[str, Any]
    ) -> bool:
        """Write exactly ``updates``.

        ``updated_at`` means the Assistant's definition changed. The platform
        keeps its own state on this row too (the workspace id), so the write
        does not stamp it: the authoring paths put ``updated_at`` in
        ``updates`` when an authored field changed.
        """
        collection = await get_async_collection(self._collection_name)
        query = {"assistant_id": assistant_id, "deleted": {"$ne": True}}
        if "environment_name" in updates:
            result = await write_environment_binding(
                self._collection_name, str(updates["environment_name"] or "").strip(),
                "assistant_catalog.update",
                lambda bound: bound.update_one(query, {"$set": updates}),
            )
        else:
            result = await run_mongo_with_retry(
                "assistant_catalog.update",
                lambda: collection.update_one(query, {"$set": updates}),
            )
        return result.modified_count > 0

    async def soft_delete(self, assistant_id: str) -> bool:
        return await self.update_assistant(assistant_id, {"deleted": True})
