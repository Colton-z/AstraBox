"""Document operations shared by the five artifact collections."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from astrabox.persistence.repository.backend import get_async_collection, run_mongo_with_retry
from astrabox.persistence.repository.index_verification import ensure_unique_index

T = TypeVar("T")


class ArtifactDocumentRepository:
    COLLECTION_NAME: str

    def __init__(self) -> None:
        self._index_ready = False

    async def ensure_indexes(self) -> None:
        if not self._index_ready:
            collection = await get_async_collection(self.COLLECTION_NAME)
            await ensure_unique_index(collection, "doc_id", collection_name=self.COLLECTION_NAME)
            self._index_ready = True

    async def _run(
        self, operation: str, callback: Callable[[Any], Awaitable[T]], transaction: Any = None,
    ) -> T:
        if transaction is not None:
            return await callback(transaction.collection(self.COLLECTION_NAME))
        await self.ensure_indexes()
        collection = await get_async_collection(self.COLLECTION_NAME)
        return await run_mongo_with_retry(f"artifacts.{operation}", lambda: callback(collection))

    async def get(self, doc_id: str, *, transaction: Any = None) -> dict[str, Any] | None:
        return await self._run("get", lambda c: c.find_one({"doc_id": doc_id}), transaction)

    async def lock(self, doc_id: str, *, transaction: Any) -> dict[str, Any] | None:
        return await self._run("lock", lambda c: c.lock_one({"doc_id": doc_id}), transaction)

    async def find_one(
        self, query: dict[str, Any], *, transaction: Any = None,
    ) -> dict[str, Any] | None:
        return await self._run("find", lambda c: c.find_one(query), transaction)

    async def list(
        self, query: dict[str, Any], *, sort: list[tuple[str, int]] | None = None,
        limit: int = 10000, transaction: Any = None,
    ) -> list[dict[str, Any]]:
        async def read(collection: Any) -> list[dict[str, Any]]:
            cursor = collection.find(query)
            if sort:
                cursor = cursor.sort(sort)
            return [row async for row in cursor.limit(limit)]
        return await self._run("list", read, transaction)

    async def insert(self, row: dict[str, Any], *, transaction: Any = None) -> None:
        await self._run("insert", lambda c: c.insert_one(dict(row)), transaction)

    async def put(self, row: dict[str, Any], *, transaction: Any = None) -> None:
        payload = {key: value for key, value in row.items() if key != "_id"}
        await self._run(
            "put", lambda c: c.update_one(
                {"doc_id": row["doc_id"]}, {"$set": payload}, upsert=True,
            ), transaction,
        )

    async def cas(
        self, doc_id: str, field: str, expected: int, updates: dict[str, Any],
        *, transaction: Any = None,
    ) -> bool:
        payload = {key: value for key, value in updates.items() if key != "_id"}
        result = await self._run(
            "cas", lambda c: c.update_one(
                {"doc_id": doc_id, field: expected}, {"$set": payload},
            ), transaction,
        )
        return int(result.matched_count) == 1

    async def delete(self, query: dict[str, Any], *, transaction: Any = None) -> None:
        await self._run("delete", lambda c: c.delete_many(query), transaction)
