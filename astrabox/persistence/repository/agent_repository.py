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

#: Where each Agent row keeps the key the Agent list is ordered by. Every
#: write through this repository sets it with the name; rows stored before the
#: key existed are given theirs by schema migration 5, before the API serves.
NAME_SORT_KEY_FIELD = "_name_key"


def agent_name_sort_key(name: Any) -> str:
    """The Agent list's order for ``name``: the name without its case.

    Stored on the row rather than computed by each backend, because the SQL
    store's matcher, SQL itself and MongoDB each fold case differently, and a
    page that resumes after a key has to compare it the way the order was
    built.
    """

    return str(name or "").casefold()


def _with_name_sort_key(fields: dict[str, Any]) -> dict[str, Any]:
    """``fields``, plus the sort key when they set the name."""

    if "name" not in fields:
        return fields
    return {**fields, NAME_SORT_KEY_FIELD: agent_name_sort_key(fields["name"])}


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
        doc = _with_name_sort_key({"deleted": False, "created_at": now, "updated_at": now, **payload})
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

    async def _agent_id_page(
        self,
        query: dict[str, Any],
        *,
        after_agent_id: str | None,
        limit: int,
        operation: str,
    ) -> list[dict[str, Any]]:
        """One page of ``query``'s rows in ``agent_id`` order, after ``after_agent_id``."""

        page_limit = max(1, min(int(limit or 200), 10_000))
        after = str(after_agent_id or "").strip()
        if after:
            query = {**query, "agent_id": {"$gt": after}}
        collection = await get_async_collection(self._collection_name)

        async def _list() -> list[dict[str, Any]]:
            cursor = collection.find(query).sort("agent_id", 1).limit(page_limit)
            return [doc async for doc in cursor]

        return await run_mongo_with_retry(operation, _list)

    async def list_agents_with_resident_boxes(
        self, *, after_agent_id: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """One page of the Agent rows naming a resident shared box.

        Rows come in ``agent_id`` order after ``after_agent_id``, so a sweep
        that carries the last id forward reaches every row; a page shorter
        than ``limit`` is the last.
        """

        return await self._agent_id_page(
            {"sandbox_id": {"$type": "string", "$ne": ""}},
            after_agent_id=after_agent_id,
            limit=limit,
            operation="agents.list_agents_with_resident_boxes",
        )

    async def list_prewarm_enabled_agents(
        self, *, after_agent_id: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """One page of the live Agents that asked for a ready sandbox.

        The warm-capacity sweep reads this rather than the resident-box list:
        an Agent whose box expired or whose slot build failed has no box to be
        listed by, and it is exactly the Agent the sweep exists to rebuild.
        Rows come in ``agent_id`` order after ``after_agent_id``; a page
        shorter than ``limit`` is the last.
        """

        return await self._agent_id_page(
            {
                "prewarm_enabled": True,
                "state": {"$nin": ["DELETING", "DELETED"]},
            },
            after_agent_id=after_agent_id,
            limit=limit,
            operation="agents.list_prewarm_enabled_agents",
        )

    async def list_live_agents_page(
        self, *, after_agent_id: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """One page of the Agents that are not deleted, in ``agent_id`` order.

        For work that must reach every Agent, one page at a time.
        """

        return await self._agent_id_page(
            {"deleted": {"$ne": True}},
            after_agent_id=after_agent_id,
            limit=limit,
            operation="agents.list_live_agents_page",
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
        after_agent_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """One page of the Agent-owned sandboxes whose durable lease is absent or stale.

        Rows come in ``agent_id`` order after ``after_agent_id``: a probe that
        cannot settle a binding leaves its row in the set.
        """
        return await self._agent_id_page(
            {
                "deleted": {"$ne": True},
                "sandbox_id": {"$gt": ""},
                "$or": [
                    {"expires_at": {"$exists": False}},
                    {"expires_at": None},
                    {"expires_at": {"$lte": now_iso}},
                ],
            },
            after_agent_id=after_agent_id,
            limit=max(1, min(int(limit or 50), 500)),
            operation="agents.list_dead_binding_probe_candidates",
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

    async def list_all_agents(self) -> list[dict[str, Any]]:
        """Every live Agent, the most recently edited first.

        Uncapped: the answer holds every Agent however many there are. A
        caller that shows a page at a time reads
        :meth:`list_agents_by_name_page` instead.
        """
        await self._ensure_indexes()
        collection = await get_async_collection(self._collection_name)

        async def _list() -> list[dict[str, Any]]:
            cursor = collection.find({"deleted": {"$ne": True}}).sort(
                [("updated_at", -1), ("agent_id", -1)]
            )
            return [doc async for doc in cursor]

        return await run_mongo_with_retry("agents.list_all", _list)

    async def list_agent_access_docs(self) -> list[dict[str, Any]]:
        """Every live Agent's name, owners, visibility and enabled flag.

        The fields that decide who sees and manages an Agent, for counting and
        scoping over the whole collection without reading whole Agents.
        """
        await self._ensure_indexes()
        collection = await get_async_collection(self._collection_name)

        async def _list() -> list[dict[str, Any]]:
            cursor = collection.find(
                {"deleted": {"$ne": True}},
                projection={
                    "agent_id": 1,
                    "name": 1,
                    "created_by": 1,
                    "admins": 1,
                    "visibility": 1,
                    "allowed_user_ids": 1,
                    "enabled": 1,
                },
            )
            return [doc async for doc in cursor]

        return await run_mongo_with_retry("agents.list_access_docs", _list)

    async def list_agents_by_name_page(
        self, *, after: tuple[str, str] | None, limit: int
    ) -> list[dict[str, Any]]:
        """One page of the live Agents by name, case-insensitively, after ``after``.

        Rows come in ``(_name_key, agent_id)`` order: the name without its
        case (:func:`agent_name_sort_key`), then the id for names that fold to
        the same key. ``after`` is that pair for the last row already read; a
        page shorter than ``limit`` is the last. Both keys are strings, so the
        store orders the page itself and reads only the page.
        """
        await self._ensure_indexes()
        page_limit = max(1, min(int(limit or 200), 10_000))
        query: dict[str, Any] = {"deleted": {"$ne": True}}
        if after is not None:
            name_key, agent_id = after
            query["$or"] = [
                {NAME_SORT_KEY_FIELD: {"$gt": name_key}},
                {NAME_SORT_KEY_FIELD: name_key, "agent_id": {"$gt": agent_id}},
            ]
        collection = await get_async_collection(self._collection_name)

        async def _list() -> list[dict[str, Any]]:
            cursor = (
                collection.find(query)
                .sort([(NAME_SORT_KEY_FIELD, 1), ("agent_id", 1)], string_keyed=True)
                .limit(page_limit)
            )
            return [doc async for doc in cursor]

        return await run_mongo_with_retry("agents.list_by_name_page", _list)

    async def update_agent(self, agent_id: str, updates: dict[str, Any]) -> bool:
        """Write exactly ``updates``.

        ``updated_at`` means the Agent's definition changed, which is what the
        console shows and sorts by. The runtime keeps its bookkeeping on this
        row too, so the write does not stamp it: the authoring paths put
        ``updated_at`` in ``updates`` when an authored field changed.
        """
        collection = await get_async_collection(self._collection_name)
        fields = _with_name_sort_key(updates)
        result = await run_mongo_with_retry(
            "agents.update",
            lambda: collection.update_one({"agent_id": agent_id}, {"$set": fields}),
        )
        return result.modified_count > 0

    async def compare_and_update_agent(
        self,
        agent_id: str,
        *,
        expected: dict[str, Any],
        updates: dict[str, Any],
    ) -> bool:
        """Write exactly ``updates`` where ``expected`` still holds.

        Like :meth:`update_agent`, it leaves ``updated_at`` to the caller.
        """
        collection = await get_async_collection(self._collection_name)
        fields = _with_name_sort_key(updates)
        result = await run_mongo_with_retry(
            "agents.compare_and_update",
            lambda: collection.update_one(
                {"agent_id": agent_id, "deleted": {"$ne": True}, **dict(expected)},
                {"$set": fields},
            ),
        )
        return bool(getattr(result, "modified_count", 0) or getattr(result, "matched_count", 0))

    async def soft_delete(self, agent_id: str, user_id: str) -> bool:
        return await self.update_agent(
            agent_id,
            {"deleted": True, "state": "DELETED"},
        )
