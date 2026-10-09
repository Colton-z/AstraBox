"""Repository for the assistant_defaults collection.

Each document is one layer of what every Assistant beneath it inherits: the
Environment and model new Assistants use, instructions, and the Skills, MCP
servers, packages, native options and Credential Vaults provided to them.
``scope`` names the layer; ``deployment`` covers the whole deployment.
Assistants read their layers when their runtime view is resolved.
"""

from __future__ import annotations

from typing import Any

from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.persistence.repository.backend import (
    get_async_collection,
    run_mongo_with_retry,
)
from astrabox.persistence.repository.environment_repository import write_environment_binding
from astrabox.persistence.repository.index_verification import ensure_unique_index

DEPLOYMENT_SCOPE = "deployment"


class AssistantDefaultsRepository:
    def __init__(self) -> None:
        self._collection_name = load_astrabox_settings().assistant_defaults_collection
        self._index_ready = False

    async def _ensure_indexes(self) -> None:
        if self._index_ready:
            return
        collection = await get_async_collection(self._collection_name)
        if collection is not None:
            await ensure_unique_index(collection, "scope", collection_name=self._collection_name)
        self._index_ready = True

    async def get_layer(self, scope: str) -> dict[str, Any] | None:
        collection = await get_async_collection(self._collection_name)
        return await run_mongo_with_retry(
            "assistant_defaults.get",
            lambda: collection.find_one({"scope": scope}),
        )

    async def put_layer(self, scope: str, fields: dict[str, Any], *, updated_by: str) -> dict[str, Any]:
        """Replace one layer with ``fields``."""

        await self._ensure_indexes()
        document = {**fields, "scope": scope, "updated_at": utcnow_iso(), "updated_by": updated_by}
        collection = await get_async_collection(self._collection_name)
        await write_environment_binding(
            self._collection_name,
            str(document.get("environment_name") or "").strip(),
            "assistant_defaults.put",
            lambda bound: bound.update_one({"scope": scope}, {"$set": document}, upsert=True),
        )
        stored = await run_mongo_with_retry(
            "assistant_defaults.read_after_put",
            lambda: collection.find_one({"scope": scope}),
        )
        return stored or document

    async def list_by_credential_vault_id(self, vault_id: str) -> list[dict[str, Any]]:
        """The layers that bind this Credential Vault."""

        collection = await get_async_collection(self._collection_name)

        async def _list() -> list[dict[str, Any]]:
            cursor = collection.find(
                {"credential_vault_ids": vault_id},
                projection={"scope": 1, "credential_vault_ids": 1},
            )
            return [doc async for doc in cursor]

        return await run_mongo_with_retry("assistant_defaults.list_by_credential_vault_id", _list)

    async def list_by_environment(self, name: str, *, transaction: Any = None) -> list[dict[str, Any]]:
        """The layers that name this Environment."""

        collection = (
            transaction.collection(self._collection_name) if transaction is not None
            else await get_async_collection(self._collection_name)
        )

        async def _list() -> list[dict[str, Any]]:
            cursor = collection.find({"environment_name": name}, projection={"scope": 1})
            return [doc async for doc in cursor]

        if transaction is not None:
            return await _list()
        return await run_mongo_with_retry("assistant_defaults.list_by_environment", _list)
