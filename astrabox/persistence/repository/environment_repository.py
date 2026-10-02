from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from astrabox.persistence.repository.backend import (
    document_transaction_runner,
    get_async_collection,
    run_mongo_with_retry,
)
from astrabox.persistence.repository.index_verification import ensure_unique_index
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.common.utils.errors import APIError

T = TypeVar("T")


async def write_environment_binding(
    collection_name: str, environment_name: str, operation_name: str,
    operation: Callable[[Any], Awaitable[T]],
) -> T:
    """Serialize a new reference with deletion, before writing its owner row."""
    runner = await document_transaction_runner() if environment_name else None
    if runner is None:
        # Without this capability deletion is refused, so existing backends
        # can continue normal authoring with their existing collection API.
        collection = await get_async_collection(collection_name)
        return await run_mongo_with_retry(operation_name, lambda: operation(collection))

    environment_collection = load_astrabox_settings().environment_collection

    async def bind(transaction: Any) -> T:
        environment = await transaction.collection(environment_collection).lock_one(
            {"name": environment_name},
        )
        if environment is None:
            raise APIError(
                code="ENVIRONMENT_NOT_FOUND",
                message="environment no longer exists; reload its configuration",
                status_code=404,
            )
        return await operation(transaction.collection(collection_name))

    # Do not retry individual commands inside the transaction or replay a
    # transaction whose commit outcome is unknown after a connection failure.
    return await runner(bind)


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
        self._index_ready = False

    async def _ensure_indexes(self) -> None:
        if self._index_ready:
            return
        collection = await get_async_collection(self._collection_name)
        await ensure_unique_index(collection, "name", collection_name=self._collection_name)
        self._index_ready = True

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
        await self._ensure_indexes()
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

    async def delete_after_reference_check(
        self, name: str, check_references: Callable[[Any], Awaitable[None]],
    ) -> None:
        """Hold the parent guard through reference checks and the final delete."""
        runner = await document_transaction_runner()
        if runner is None:
            raise APIError(
                code="ENVIRONMENT_DELETE_UNSUPPORTED",
                message="the configured database backend does not support atomic Environment deletion",
                status_code=503,
            )

        await self._ensure_indexes()

        async def remove(transaction: Any) -> None:
            collection = transaction.collection(self._collection_name)
            if await collection.lock_one({"name": name}) is None:
                raise APIError(
                    code="ENVIRONMENT_NOT_FOUND", message="environment not found", status_code=404,
                )
            await check_references(transaction)
            await collection.delete_one({"name": name})

        await runner(remove)
