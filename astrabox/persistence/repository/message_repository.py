from __future__ import annotations

from typing import Any

from astrabox.persistence.repository._compat import (
    AutoReconnect,
    ConnectionFailure,
    NetworkTimeout,
    OperationFailure,
    ServerSelectionTimeoutError,
)

from astrabox.persistence.repository.backend import (
    get_async_collection,
    run_mongo_with_retry,
)
from astrabox.persistence.repository.index_verification import ensure_unique_index
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.common.utils.time_utils import utcnow_iso

logger = get_logger(__name__)


def _is_existing_index_error(exc: Exception) -> bool:
    if not isinstance(exc, OperationFailure):
        return False
    message = str(exc)
    return (
        "Duplicate entry" in message
        or "idx_indexname" in message
        or "already exists" in message
        or "IndexOptionsConflict" in message
    )


async def _safe_create_index(collection, keys, **kwargs) -> None:
    try:
        await collection.create_index(keys, **kwargs)
    except Exception as exc:
        if _is_existing_index_error(exc):
            return
        if isinstance(
            exc,
            (
                AutoReconnect,
                NetworkTimeout,
                ServerSelectionTimeoutError,
                ConnectionFailure,
            ),
        ):
            logger.warning("create index skipped due transient mongodb error: %s", exc)
            return
        raise


def _fix_message_order(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure user message comes before assistant message within the same turn.

    MongoDB sorts null created_at before all values, which can put
    assistant messages (with missing created_at) before their user message.
    """
    result: list[dict[str, Any]] = []
    by_turn: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for msg in messages:
        tid = str(msg.get("turn_id") or "")
        if tid not in by_turn:
            by_turn[tid] = []
            order.append(tid)
        by_turn[tid].append(msg)
    for tid in order:
        group = by_turn[tid]
        group.sort(key=lambda m: (0 if m.get("role") == "user" else 1))
        result.extend(group)
    return result


class MessageRepository:
    def __init__(self) -> None:
        settings = load_astrabox_settings()
        self._collection_name = settings.messages_collection
        self._index_ready = False

    async def ensure_indexes(self) -> None:
        if self._index_ready:
            return
        collection = await get_async_collection(self._collection_name)
        if collection is not None:
            try:
                await _safe_create_index(collection, [("session_id", 1), ("turn_id", 1)])
            except Exception as exc:
                logger.warning("ensure message indexes failed, continue without blocking: %s", exc)
            await ensure_unique_index(
                collection,
                [("session_id", 1), ("turn_id", 1), ("role", 1)],
                name="ux_session_turn_role",
                collection_name=self._collection_name,
            )
        self._index_ready = True

    async def append_many(self, messages: list[dict[str, Any]]) -> None:
        if not messages:
            return
        await self.ensure_indexes()

        collection = await get_async_collection(self._collection_name)
        await collection.insert_many(messages)

    async def list_recent(self, session_id: str, limit: int = 100) -> list[dict[str, Any]]:
        collection = await get_async_collection(self._collection_name)
        async def _list() -> list[dict[str, Any]]:
            cursor = (
                collection.find({"session_id": session_id})
                .sort("created_at", 1)
                .limit(limit)
            )
            rows = [doc async for doc in cursor]
            return _fix_message_order(rows)

        return await run_mongo_with_retry("messages.list_recent", _list)

    async def list_page(
        self,
        session_id: str,
        *,
        limit: int = 20,
        before: str | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Cursor-based pagination returning ``(messages, has_more)``.

        Messages are returned in chronological order (oldest first).
        *before* is an ISO timestamp cursor — only messages with
        ``created_at < before`` are returned.
        """
        collection = await get_async_collection(self._collection_name)

        async def _page() -> tuple[list[dict[str, Any]], bool]:
            query: dict[str, Any] = {"session_id": session_id}
            if before:
                query["created_at"] = {"$lt": before}
            cursor = (
                collection.find(query)
                .sort("created_at", -1)
                .limit(limit + 1)
            )
            rows = [doc async for doc in cursor]
            has_more = len(rows) > limit
            rows = rows[:limit]
            rows.reverse()
            return _fix_message_order(rows), has_more

        return await run_mongo_with_retry("messages.list_page", _page)

    async def list_all(self, session_id: str) -> list[dict[str, Any]]:
        collection = await get_async_collection(self._collection_name)
        async def _list() -> list[dict[str, Any]]:
            cursor = collection.find({"session_id": session_id}).sort("created_at", 1)
            return [doc async for doc in cursor]

        return await run_mongo_with_retry("messages.list_all", _list)

    async def commit_assistant(
        self,
        session_id: str,
        turn_id: str,
        user_id: str,
        content: str,
        blocks: list[dict[str, Any]] | None = None,
        created_at: str | None = None,
    ) -> None:
        """Upsert the authoritative assistant message for a turn.

        Uses ``$set`` so that a later commit with more complete blocks
        naturally supersedes an earlier one.  Idempotent.
        """
        await self.ensure_indexes()

        doc: dict[str, Any] = {
            "session_id": session_id,
            "turn_id": turn_id,
            "user_id": user_id,
            "role": "assistant",
            "content": content,
        }
        if blocks:
            doc["blocks"] = blocks
        doc["created_at"] = created_at or utcnow_iso()

        collection = await get_async_collection(self._collection_name)
        async def _upsert() -> None:
            await collection.update_one(
                {"session_id": session_id, "turn_id": turn_id, "role": "assistant"},
                {"$set": doc},
                upsert=True,
            )

        await run_mongo_with_retry("messages.commit_assistant", _upsert)

    async def update_message_fields(
        self,
        session_id: str,
        turn_id: str,
        role: str,
        updates: dict[str, Any],
    ) -> bool:
        if not updates:
            return False

        collection = await get_async_collection(self._collection_name)
        result = await collection.update_one(
            {
                "session_id": session_id,
                "turn_id": turn_id,
                "role": role,
            },
            {"$set": updates},
        )
        return bool(result.matched_count or result.modified_count)
