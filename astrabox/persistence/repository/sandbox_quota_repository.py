"""Repository for the sandbox_quota collection: one ledger per sandbox backend.

A ledger holds what the backend's last inventory reported this installation
running and the reservations made for boxes not in that inventory yet. It is
written only by compare-and-set on its ``version``, so two replicas that each
see room for one more box cannot both take it.
"""

from __future__ import annotations

from typing import Any

from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.persistence.repository.backend import (
    get_async_collection,
    run_mongo_with_retry,
)
from astrabox.persistence.repository.index_verification import ensure_unique_index

COLLECTION_NAME = "sandbox_quota"


class SandboxQuotaRepository:
    def __init__(self) -> None:
        self._index_ready = False

    async def _ensure_indexes(self) -> None:
        if self._index_ready:
            return
        collection = await get_async_collection(COLLECTION_NAME)
        if collection is not None:
            await ensure_unique_index(collection, "backend", collection_name=COLLECTION_NAME)
        self._index_ready = True

    async def read(self, backend: str) -> dict[str, Any]:
        """The backend's ledger, created empty the first time it is read."""
        await self._ensure_indexes()
        collection = await get_async_collection(COLLECTION_NAME)
        await run_mongo_with_retry(
            "sandbox_quota.ensure",
            lambda: collection.update_one(
                {"backend": backend},
                {
                    "$setOnInsert": {
                        "backend": backend,
                        "version": 0,
                        "reservations": [],
                        "inventory": {},
                        "inventory_at": None,
                    }
                },
                upsert=True,
            ),
        )
        ledger = await run_mongo_with_retry(
            "sandbox_quota.read",
            lambda: collection.find_one({"backend": backend}),
        )
        if not isinstance(ledger, dict):
            raise RuntimeError(f"sandbox quota ledger for {backend!r} could not be read back")
        ledger.pop("_id", None)
        return ledger

    async def compare_and_set(
        self,
        backend: str,
        *,
        version: int,
        reservations: list[dict[str, Any]],
        inventory: dict[str, Any] | None = None,
        inventory_at: str | None = None,
    ) -> bool:
        """Write the ledger if nobody else wrote it since ``version`` was read."""
        updates: dict[str, Any] = {
            "reservations": reservations,
            "version": version + 1,
            "updated_at": utcnow_iso(),
        }
        if inventory is not None:
            updates["inventory"] = inventory
            updates["inventory_at"] = inventory_at
        collection = await get_async_collection(COLLECTION_NAME)
        result = await run_mongo_with_retry(
            "sandbox_quota.compare_and_set",
            lambda: collection.update_one(
                {"backend": backend, "version": version},
                {"$set": updates},
            ),
        )
        return bool(getattr(result, "modified_count", 0))
