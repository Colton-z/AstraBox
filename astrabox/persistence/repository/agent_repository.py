"""Repository for the Agent collection."""

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


class AgentRepository:
    def __init__(self) -> None:
        settings = load_astrabox_settings()
        self._collection_name = settings.agents_collection
        self._index_ready = False

    async def _ensure_indexes(self) -> None:
        if self._index_ready:
            return
        collection = await get_async_collection(self._collection_name)
        if collection is not None:
            await ensure_unique_index(
                collection,
                "agent_id",
                collection_name=self._collection_name,
            )
            try:
                await _safe_create_index(
                    collection,
                    [("user_id", 1), ("deleted", 1), ("updated_at", -1)],
                )
                await _safe_create_index(
                    collection,
                    [("state", 1), ("updated_at", 1)],
                )
                await _safe_create_index(collection, "sandbox_id")
                await _safe_create_index(
                    collection,
                    [("sandbox_id", 1), ("expires_at", 1)],
                )
                await _safe_create_index(collection, "credential_vault_ids")
            except Exception as exc:
                logger.warning("ensure agent indexes failed: %s", exc)
        self._index_ready = True

    async def create_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        await self._ensure_indexes()
        now = utcnow_iso()
        doc = {"deleted": False, "created_at": now, "updated_at": now, **payload}
        collection = await get_async_collection(self._collection_name)
        await run_mongo_with_retry(
            "agents.create", lambda: collection.insert_one(doc)
        )
        stored = await run_mongo_with_retry(
            "agents.read_after_create",
            lambda: collection.find_one({"agent_id": doc["agent_id"]}),
        )
        return stored or doc

    async def get_agent(
        self, agent_id: str, *, include_deleted: bool = False
    ) -> dict[str, Any] | None:
        """Read one agent row, or ``None`` when no visible row matches.

        Deleting an agent only flips ``deleted``; the sessions and transcripts it
        owns keep naming it afterwards. ``include_deleted`` is for the read paths
        that must still resolve such an owner's durable metadata — every other
        caller takes the default, which hides deleted rows.
        """
        collection = await get_async_collection(self._collection_name)
        query: dict[str, Any] = {"agent_id": agent_id}
        if not include_deleted:
            query["deleted"] = {"$ne": True}
        return await run_mongo_with_retry(
            "agents.get",
            lambda: collection.find_one(query),
        )

    async def find_agent_by_sandbox_id(self, sandbox_id: str) -> dict[str, Any] | None:
        target = str(sandbox_id or "").strip()
        if not target:
            return None
        collection = await get_async_collection(self._collection_name)
        return await run_mongo_with_retry(
            "agents.find_by_sandbox_id",
            lambda: collection.find_one({"sandbox_id": target, "deleted": {"$ne": True}}),
            fault_context={"sandbox_id": target},
        )

    async def list_agents_with_resident_boxes(
        self, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Every Agent row currently naming a resident shared box."""

        page_limit = max(1, min(int(limit or 200), 10_000))
        collection = await get_async_collection(self._collection_name)

        async def _list() -> list[dict[str, Any]]:
            cursor = (
                collection.find(
                    {"sandbox_id": {"$type": "string", "$ne": ""}}
                )
                .sort("updated_at", 1)
                .limit(page_limit)
            )
            return [doc async for doc in cursor]

        return await run_mongo_with_retry(
            "agents.list_agents_with_resident_boxes",
            _list,
        )

    async def list_prewarm_enabled_agents(
        self, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Every live Agent that asked for a ready sandbox, with or without one.

        The warm-capacity sweep reads this rather than the resident-box list:
        an Agent whose box expired or whose slot build failed has no box to be
        listed by, and it is exactly the Agent the sweep exists to rebuild.
        """

        page_limit = max(1, min(int(limit or 200), 10_000))
        collection = await get_async_collection(self._collection_name)

        async def _list() -> list[dict[str, Any]]:
            cursor = (
                collection.find(
                    {
                        "prewarm_enabled": True,
                        "state": {"$nin": ["DELETING", "DELETED"]},
                    }
                )
                .sort("updated_at", 1)
                .limit(page_limit)
            )
            return [doc async for doc in cursor]

        return await run_mongo_with_retry(
            "agents.list_prewarm_enabled_agents",
            _list,
        )

    async def list_agents_by_sandbox_id(
        self, sandbox_id: str
    ) -> list[dict[str, Any]]:
        """Return every active Agent that still names ``sandbox_id``.

        One healthy shared sandbox has one Agent owner. Returning the complete
        set keeps lifecycle convergence correct if a damaged database contains
        duplicate owners: terminal resource evidence must not leave one stale
        pointer behind merely because ``find_one`` happened to choose another.
        """
        target = str(sandbox_id or "").strip()
        if not target:
            return []
        collection = await get_async_collection(self._collection_name)

        async def _list() -> list[dict[str, Any]]:
            cursor = collection.find(
                {"sandbox_id": target, "deleted": {"$ne": True}}
            )
            return [doc async for doc in cursor]

        return await run_mongo_with_retry(
            "agents.list_by_sandbox_id",
            _list,
            fault_context={"sandbox_id": target},
        )

    async def list_dead_binding_probe_candidates(
        self,
        *,
        now_iso: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return Agent-owned sandboxes whose durable lease is absent or stale."""
        page_limit = max(1, min(int(limit or 50), 500))
        query = {
            "deleted": {"$ne": True},
            "sandbox_id": {"$gt": ""},
            "$or": [
                {"expires_at": {"$exists": False}},
                {"expires_at": None},
                {"expires_at": {"$lte": now_iso}},
            ],
        }
        collection = await get_async_collection(self._collection_name)

        async def _list() -> list[dict[str, Any]]:
            cursor = (
                collection.find(query)
                .sort("expires_at", 1)
                .limit(page_limit)
            )
            return [doc async for doc in cursor]

        return await run_mongo_with_retry(
            "agents.list_dead_binding_probe_candidates",
            _list,
        )

    async def list_agents_by_credential_vault_id(
        self, vault_id: str
    ) -> list[dict[str, Any]]:
        """Return every active Agent that still references the managed Vault.

        Every one, not the first: an operator unbinding a Vault has to be told
        the whole set, or the refusal turns into one round trip per holder.
        """
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
            "agents.list_by_credential_vault_id",
            _list,
            fault_context={"vault_id": target},
        )

    async def list_agents_by_ids(
        self,
        agent_ids: list[str],
        *,
        include_deleted: bool = False,
        projection: dict[str, int] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Map each resolved ``agent_id`` to its row; unresolved ids are absent.

        ``include_deleted`` widens the read to deleted rows exactly as it does in
        :meth:`get_agent`.
        """
        ids = sorted({str(agent_id or "").strip() for agent_id in agent_ids if str(agent_id or "").strip()})
        if not ids:
            return {}
        collection = await get_async_collection(self._collection_name)

        async def _list() -> list[dict[str, Any]]:
            query: dict[str, Any] = {"agent_id": {"$in": ids}}
            if not include_deleted:
                query["deleted"] = {"$ne": True}
            cursor = collection.find(
                query,
                projection=dict(projection) if projection is not None else None,
            )
            return [doc async for doc in cursor]

        rows = await run_mongo_with_retry("agents.list_by_ids", _list)
        return {
            str(row.get("agent_id") or ""): row
            for row in rows
            if str(row.get("agent_id") or "").strip()
        }

    async def list_all_agents(self, limit: int = 200) -> list[dict[str, Any]]:
        await self._ensure_indexes()
        collection = await get_async_collection(self._collection_name)
        async def _list() -> list[dict[str, Any]]:
            cursor = (
                collection.find({"deleted": {"$ne": True}})
                .sort("updated_at", -1)
                .limit(limit)
            )
            return [doc async for doc in cursor]

        return await run_mongo_with_retry("agents.list_all", _list)

    async def list_agent_access_docs(self, limit: int = 200) -> list[dict[str, Any]]:
        await self._ensure_indexes()
        collection = await get_async_collection(self._collection_name)

        async def _list() -> list[dict[str, Any]]:
            cursor = (
                collection.find(
                    {"deleted": {"$ne": True}},
                    projection={
                        "name": 1,
                        "created_by": 1,
                        "admins": 1,
                        "visibility": 1,
                        "allowed_user_ids": 1,
                    },
                )
                .sort("updated_at", -1)
                .limit(limit)
            )
            return [doc async for doc in cursor]

        return await run_mongo_with_retry("agents.list_access_docs", _list)

    async def update_agent(self, agent_id: str, updates: dict[str, Any]) -> bool:
        updates = {**updates, "updated_at": utcnow_iso()}
        collection = await get_async_collection(self._collection_name)
        result = await run_mongo_with_retry(
            "agents.update",
            lambda: collection.update_one({"agent_id": agent_id}, {"$set": updates}),
        )
        return result.modified_count > 0

    async def compare_and_update_agent(
        self,
        agent_id: str,
        *,
        expected: dict[str, Any],
        updates: dict[str, Any],
    ) -> bool:
        updates = {**updates, "updated_at": utcnow_iso()}
        collection = await get_async_collection(self._collection_name)
        result = await run_mongo_with_retry(
            "agents.compare_and_update",
            lambda: collection.update_one(
                {"agent_id": agent_id, "deleted": {"$ne": True}, **dict(expected)},
                {"$set": updates},
            ),
        )
        return bool(getattr(result, "modified_count", 0) or getattr(result, "matched_count", 0))

    async def soft_delete(self, agent_id: str, user_id: str) -> bool:
        return await self.update_agent(
            agent_id,
            {"deleted": True, "state": "DELETED"},
        )
