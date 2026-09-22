"""Persistence for the API keys an MCP client authenticates with.

The row holds ``secret_digest`` and never the secret. A credential store that
can hand back live credentials turns a leaked backup into a leaked deployment,
and nothing here needs the original: verification hashes what the caller sent
and compares digests.

Design: `docs/maintainers/mcp-client-tokens.md`.
"""

from __future__ import annotations

import uuid
from typing import Any

from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.persistence.repository.backend import (
    get_async_collection,
    run_mongo_with_retry,
)
from astrabox.persistence.repository.index_verification import ensure_unique_index
from astrabox.persistence.repository.session_repository import _safe_create_index

MCP_CLIENT_TOKENS_COLLECTION = "mcp_client_tokens"


class MCPClientTokenRepository:
    """Rows for issued MCP client keys, looked up by digest on every call."""

    def __init__(self) -> None:
        self._index_ready = False

    async def _ensure_indexes(self) -> None:
        if self._index_ready:
            return
        collection = await get_async_collection(MCP_CLIENT_TOKENS_COLLECTION)
        await ensure_unique_index(
            collection,
            "token_id",
            collection_name=MCP_CLIENT_TOKENS_COLLECTION,
        )
        # The verification path's only query. Unique because a digest collision
        # would be two identities behind one secret.
        await ensure_unique_index(
            collection,
            "secret_digest",
            collection_name=MCP_CLIENT_TOKENS_COLLECTION,
        )
        await _safe_create_index(collection, [("user_id", 1), ("created_at", -1)])
        self._index_ready = True

    async def create(
        self,
        *,
        user_id: str,
        org_id: str | None,
        name: str,
        scope: str,
        secret_digest: str,
        expires_at: str | None,
    ) -> dict[str, Any]:
        await self._ensure_indexes()
        doc = {
            "token_id": f"mtk_{uuid.uuid4().hex}",
            "user_id": user_id,
            "org_id": org_id,
            "name": name,
            "scope": scope,
            "secret_digest": secret_digest,
            "expires_at": expires_at,
            "created_at": utcnow_iso(),
            "last_used_at": None,
        }
        collection = await get_async_collection(MCP_CLIENT_TOKENS_COLLECTION)

        async def _op() -> None:
            await collection.insert_one(dict(doc))

        await run_mongo_with_retry(f"{MCP_CLIENT_TOKENS_COLLECTION}.create", _op)
        return doc

    async def list_for_user(self, user_id: str) -> list[dict[str, Any]]:
        await self._ensure_indexes()
        collection = await get_async_collection(MCP_CLIENT_TOKENS_COLLECTION)

        async def _op() -> list[dict[str, Any]]:
            cursor = collection.find({"user_id": user_id})
            return [row async for row in cursor]

        rows = await run_mongo_with_retry(f"{MCP_CLIENT_TOKENS_COLLECTION}.list", _op)
        rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        return rows

    async def find_by_digest(self, secret_digest: str) -> dict[str, Any] | None:
        await self._ensure_indexes()
        collection = await get_async_collection(MCP_CLIENT_TOKENS_COLLECTION)

        async def _op() -> dict[str, Any] | None:
            return await collection.find_one({"secret_digest": secret_digest})

        row = await run_mongo_with_retry(f"{MCP_CLIENT_TOKENS_COLLECTION}.find", _op)
        return dict(row) if isinstance(row, dict) else None

    async def delete(self, *, user_id: str, token_id: str) -> bool:
        """Delete this user's token. False when the id is not theirs.

        Scoped by ``user_id`` in the query rather than checked after the read:
        one query cannot be raced into deleting somebody else's row, and the
        caller learns only whether it had one.
        """
        await self._ensure_indexes()
        collection = await get_async_collection(MCP_CLIENT_TOKENS_COLLECTION)

        async def _op() -> Any:
            return await collection.delete_one({"user_id": user_id, "token_id": token_id})

        result = await run_mongo_with_retry(f"{MCP_CLIENT_TOKENS_COLLECTION}.delete", _op)
        return int(getattr(result, "deleted_count", 0) or 0) == 1

    async def touch(self, token_id: str) -> None:
        """Record use. Best-effort: a failure here must not refuse a good call."""
        collection = await get_async_collection(MCP_CLIENT_TOKENS_COLLECTION)

        async def _op() -> None:
            await collection.update_one(
                {"token_id": token_id}, {"$set": {"last_used_at": utcnow_iso()}}
            )

        try:
            await run_mongo_with_retry(f"{MCP_CLIENT_TOKENS_COLLECTION}.touch", _op)
        except Exception:  # noqa: BLE001 - liveness reporting, never authorization
            return
