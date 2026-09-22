"""Vault + vault-credential metadata persistence.

Two collections: ``vaults`` (one document per vault) and ``vault_credentials``
(one document per credential, referencing its vault). ONLY metadata lives here —
display names, key fields (``mcp_server_url`` / ``secret_name``), scoping and
injection rules, refresh configuration, timestamps. Sensitive values (tokens,
secret values, refresh material) live in the :class:`astrabox.seams.secrets.SecretStore`
keyed by ``(vault/<vault_id>, <credential_id>/<field>)`` and are purged there on
archive/delete; this layer never sees them.

Business rules (uniqueness, the per-vault credential cap, immutable key fields,
archive semantics) are enforced by ``vault_service`` — this layer is plain
storage in the house repository idiom.
"""

from __future__ import annotations

import uuid
from typing import Any

from astrabox.persistence.repository.backend import (
    get_async_collection,
    run_mongo_with_retry,
)
from astrabox.common.utils.time_utils import utcnow_iso

VAULTS_COLLECTION = "vaults"
VAULT_CREDENTIALS_COLLECTION = "vault_credentials"


class VaultRepository:
    """Storage for vault + credential metadata documents."""

    # ── vaults ──────────────────────────────────────────────────────────────

    async def create_vault(self, doc: dict[str, Any]) -> dict[str, Any]:
        stamped = {
            "vault_id": str(doc.get("vault_id") or f"vlt_{uuid.uuid4().hex}"),
            **doc,
            "archived_at": None,
            "created_at": utcnow_iso(),
            "updated_at": utcnow_iso(),
        }

        async def _create() -> dict[str, Any]:
            collection = await get_async_collection(VAULTS_COLLECTION)
            await collection.insert_one(dict(stamped))
            return stamped

        return await run_mongo_with_retry("vaults.create", _create)

    async def get_vault(self, vault_id: str) -> dict[str, Any] | None:
        async def _get() -> Any:
            collection = await get_async_collection(VAULTS_COLLECTION)
            return await collection.find_one({"vault_id": vault_id})

        doc = await run_mongo_with_retry("vaults.get", _get)
        return doc if isinstance(doc, dict) else None

    async def list_vaults(self, *, org_id: str, limit: int = 200) -> list[dict[str, Any]]:
        async def _list() -> list[dict[str, Any]]:
            collection = await get_async_collection(VAULTS_COLLECTION)
            cursor = collection.find({"org_id": org_id})
            docs = [doc async for doc in cursor]
            docs.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
            return docs[: int(limit)]

        return await run_mongo_with_retry("vaults.list", _list)

    async def update_vault(self, vault_id: str, updates: dict[str, Any]) -> bool:
        async def _update() -> bool:
            collection = await get_async_collection(VAULTS_COLLECTION)
            result = await collection.update_one(
                {"vault_id": vault_id},
                {"$set": {**updates, "updated_at": utcnow_iso()}},
            )
            return bool(getattr(result, "matched_count", 0))

        return await run_mongo_with_retry("vaults.update", _update)

    async def delete_vault(self, vault_id: str) -> None:
        async def _delete() -> None:
            collection = await get_async_collection(VAULTS_COLLECTION)
            await collection.delete_many({"vault_id": vault_id})

        await run_mongo_with_retry("vaults.delete", _delete)

    # ── credentials ─────────────────────────────────────────────────────────

    async def create_credential(self, doc: dict[str, Any]) -> dict[str, Any]:
        stamped = {
            "credential_id": str(doc.get("credential_id") or f"vcr_{uuid.uuid4().hex}"),
            **doc,
            "archived_at": None,
            "created_at": utcnow_iso(),
            "updated_at": utcnow_iso(),
        }

        async def _create() -> dict[str, Any]:
            collection = await get_async_collection(VAULT_CREDENTIALS_COLLECTION)
            await collection.insert_one(dict(stamped))
            return stamped

        return await run_mongo_with_retry("vault_credentials.create", _create)

    async def get_credential(self, credential_id: str) -> dict[str, Any] | None:
        async def _get() -> Any:
            collection = await get_async_collection(VAULT_CREDENTIALS_COLLECTION)
            return await collection.find_one({"credential_id": credential_id})

        doc = await run_mongo_with_retry("vault_credentials.get", _get)
        return doc if isinstance(doc, dict) else None

    async def list_credentials(self, vault_id: str) -> list[dict[str, Any]]:
        async def _list() -> list[dict[str, Any]]:
            collection = await get_async_collection(VAULT_CREDENTIALS_COLLECTION)
            cursor = collection.find({"vault_id": vault_id})
            docs = [doc async for doc in cursor]
            docs.sort(key=lambda item: str(item.get("created_at") or ""))
            return docs

        return await run_mongo_with_retry("vault_credentials.list", _list)

    async def update_credential(self, credential_id: str, updates: dict[str, Any]) -> bool:
        async def _update() -> bool:
            collection = await get_async_collection(VAULT_CREDENTIALS_COLLECTION)
            result = await collection.update_one(
                {"credential_id": credential_id},
                {"$set": {**updates, "updated_at": utcnow_iso()}},
            )
            return bool(getattr(result, "matched_count", 0))

        return await run_mongo_with_retry("vault_credentials.update", _update)

    async def delete_credential(self, credential_id: str) -> None:
        async def _delete() -> None:
            collection = await get_async_collection(VAULT_CREDENTIALS_COLLECTION)
            await collection.delete_many({"credential_id": credential_id})

        await run_mongo_with_retry("vault_credentials.delete", _delete)

    async def delete_vault_credentials(self, vault_id: str) -> None:
        async def _delete() -> None:
            collection = await get_async_collection(VAULT_CREDENTIALS_COLLECTION)
            await collection.delete_many({"vault_id": vault_id})

        await run_mongo_with_retry("vault_credentials.delete_by_vault", _delete)
