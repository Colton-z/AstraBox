from __future__ import annotations

from typing import Any

from astrabox.persistence.repository.backend import (
    get_async_collection,
    run_mongo_with_retry,
)
from astrabox.persistence.repository.index_verification import ensure_unique_index
from astrabox.persistence.repository.session_repository import _safe_create_index
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.time_utils import utcnow_iso

logger = get_logger(__name__)

COLLECTION_NAME = "platform_mcp_bindings"


class PlatformMCPBindingRepository:
    def __init__(self) -> None:
        self._index_ready = False

    async def ensure_indexes(self) -> None:
        if self._index_ready:
            return
        collection = await get_async_collection(COLLECTION_NAME)
        if collection is not None:
            await ensure_unique_index(
                collection,
                "deployment_id",
                collection_name=COLLECTION_NAME,
            )
            try:
                await _safe_create_index(collection, [("scope_kind", 1), ("updated_at", -1)])
            except Exception as exc:
                logger.warning("platform MCP binding index creation failed: %s", exc)
        self._index_ready = True

    async def upsert_binding(self, binding: dict[str, Any]) -> dict[str, Any]:
        await self.ensure_indexes()
        deployment_id = str(binding.get("deployment_id") or "").strip()
        if not deployment_id:
            raise ValueError("deployment_id is required")
        payload = {
            **dict(binding),
            "deployment_id": deployment_id,
            "created_at": str(binding.get("created_at") or utcnow_iso()),
            "updated_at": utcnow_iso(),
        }
        collection = await get_async_collection(COLLECTION_NAME)
        await run_mongo_with_retry(
            "platform_mcp_bindings.upsert",
            lambda: collection.update_one(
                {"deployment_id": deployment_id},
                {"$set": payload},
                upsert=True,
            ),
        )
        stored = await run_mongo_with_retry(
            "platform_mcp_bindings.read_after_upsert",
            lambda: collection.find_one({"deployment_id": deployment_id}),
        )
        return stored or payload

    async def get_binding(self, deployment_id: str) -> dict[str, Any] | None:
        await self.ensure_indexes()
        normalized = str(deployment_id or "").strip()
        if not normalized:
            return None
        collection = await get_async_collection(COLLECTION_NAME)
        return await run_mongo_with_retry(
            "platform_mcp_bindings.get",
            lambda: collection.find_one({"deployment_id": normalized}),
        )

    async def delete_binding(self, deployment_id: str) -> bool:
        """Remove one binding row; True when a row existed.

        Rows are only ever fetched by exact deployment id, so a leaked row is
        inert — but a discarded prepared slot's row is still debris, and the
        discard path owns removing it.
        """
        await self.ensure_indexes()
        normalized = str(deployment_id or "").strip()
        if not normalized:
            return False
        collection = await get_async_collection(COLLECTION_NAME)
        result = await run_mongo_with_retry(
            "platform_mcp_bindings.delete",
            lambda: collection.delete_one({"deployment_id": normalized}),
        )
        return bool(getattr(result, "deleted_count", 0))
