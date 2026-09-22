from __future__ import annotations

from typing import Any

from astrabox.persistence.repository._compat import DuplicateKeyError

from astrabox.persistence.repository.backend import (
    get_async_collection,
    run_mongo_with_retry,
)
from astrabox.persistence.repository.index_verification import ensure_unique_index
from astrabox.persistence.models.session_snapshot import (
    validate_channel_ownership,
    watermark_field_for_channel,
)
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.time_utils import utcnow_iso

logger = get_logger(__name__)

COLLECTION_NAME = "session_snapshots"


def _flatten_plain_filter_value(
    prefix: str,
    value: Any,
    output: dict[str, Any],
) -> None:
    if not isinstance(value, dict):
        output[prefix] = value
        return
    if any(str(key).startswith("$") for key in value):
        output[prefix] = value
        return
    if not value:
        raise ValueError(f"unsupported empty object equality filter: {prefix}")
    for key, nested in value.items():
        field = f"{prefix}.{key}"
        _flatten_plain_filter_value(field, nested, output)


def _normalize_extra_filter(extra_filter: dict[str, Any] | None) -> dict[str, Any]:
    """Convert embedded object equality to dotted-field equality.

    The production Mongo-compatible layer rejects plain JSON objects in a
    findAndModify filter unless they are comparison operator payloads.  Snapshot
    CAS filters should compare each embedded field explicitly instead.
    """

    if not isinstance(extra_filter, dict):
        return {}
    normalized: dict[str, Any] = {}
    for key, value in extra_filter.items():
        key_text = str(key)
        if key_text.startswith("$"):
            normalized[key_text] = value
            continue
        _flatten_plain_filter_value(key_text, value, normalized)
    return normalized


async def _ensure_indexes_once(collection) -> None:
    await ensure_unique_index(
        collection,
        [("session_id", 1)],
        name="ux_session_snapshots_session",
        collection_name=COLLECTION_NAME,
    )
    try:
        await collection.create_index(
            [("conversation_state", 1), ("updated_at", -1)],
            name="ix_session_snapshots_conversation_state",
        )
    except Exception as exc:
        logger.warning("session snapshot index creation failed (non-blocking): %s", exc)


_index_ready = False


class SessionSnapshotRepository:
    async def ensure_indexes(self) -> None:
        global _index_ready
        if _index_ready:
            return
        collection = await get_async_collection(COLLECTION_NAME)
        if collection is not None:
            await _ensure_indexes_once(collection)
        _index_ready = True

    async def get_snapshot(self, session_id: str) -> dict[str, Any] | None:
        await self.ensure_indexes()
        collection = await get_async_collection(COLLECTION_NAME)
        return await run_mongo_with_retry(
            "session_snapshots.get_snapshot",
            lambda: collection.find_one({"session_id": session_id}),
        )

    async def get_snapshots_batch(
        self,
        session_ids: list[str],
        *,
        projection: dict[str, int] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Fetch snapshots for multiple sessions in one round-trip."""
        if not session_ids:
            return {}
        await self.ensure_indexes()
        collection = await get_async_collection(COLLECTION_NAME)
        result: dict[str, dict[str, Any]] = {}

        async def _batch_find() -> list[dict[str, Any]]:
            cursor = collection.find(
                {"session_id": {"$in": list(session_ids)}},
                projection=dict(projection) if projection is not None else None,
            )
            return [doc async for doc in cursor]

        docs = await run_mongo_with_retry(
            "session_snapshots.get_snapshots_batch",
            _batch_find,
        )
        for doc in docs:
            sid = str(doc.get("session_id") or "")
            if sid:
                result[sid] = doc
        return result

    async def apply_channel_update(
        self,
        session_id: str,
        *,
        channel: str,
        event_seq: int,
        updates: dict[str, Any],
        expected_conversation_state: str | None = None,
        extra_filter: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        validate_channel_ownership(channel, set(updates.keys()))
        await self.ensure_indexes()
        collection = await get_async_collection(COLLECTION_NAME)
        watermark_field = watermark_field_for_channel(channel)
        now_iso = utcnow_iso()
        payload = {
            **dict(updates),
            watermark_field: int(event_seq),
            "updated_at": now_iso,
        }
        # Strip identity/index fields from $set to avoid
        # "_id not allow update" on MongoDB-compatible servers.
        update_payload = {
            k: v for k, v in payload.items() if k not in ("_id", "session_id")
        }
        normalized_extra_filter = _normalize_extra_filter(extra_filter)
        has_conditional_guard = (
            expected_conversation_state is not None or bool(normalized_extra_filter)
        )

        async def _update_existing() -> dict[str, Any] | None:
            from astrabox.persistence.repository._compat import ReturnDocument

            filter_doc = {
                "session_id": session_id,
                "$or": [
                    {watermark_field: {"$lt": int(event_seq)}},
                    {watermark_field: {"$exists": False}},
                ],
            }
            if expected_conversation_state is not None:
                filter_doc["conversation_state"] = expected_conversation_state
            filter_doc.update(normalized_extra_filter)
            return await collection.find_one_and_update(
                filter_doc,
                {"$set": update_payload},
                return_document=ReturnDocument.AFTER,
            )

        updated = await run_mongo_with_retry(
            "session_snapshots.apply_channel_update.update_existing",
            _update_existing,
        )
        if isinstance(updated, dict):
            return updated

        async def _find_existing() -> dict[str, Any] | None:
            return await collection.find_one({"session_id": session_id})

        existing = await run_mongo_with_retry(
            "session_snapshots.apply_channel_update.find_existing",
            _find_existing,
        )
        if isinstance(existing, dict):
            return None
        if has_conditional_guard:
            return None

        # Document might not exist yet (first write for this session).
        # Try insert; if it races with another writer, catch the duplicate
        # and retry the conditional update.
        # Note: this MongoDB-compatible layer returns WriteError (not
        # DuplicateKeyError) for duplicate key violations.
        from astrabox.persistence.repository._compat import WriteError

        created = {
            "session_id": session_id,
            "created_at": now_iso,
            **payload,
        }

        async def _insert_new() -> None:
            await collection.insert_one(created)

        try:
            await run_mongo_with_retry(
                "session_snapshots.apply_channel_update.insert_new",
                _insert_new,
            )
            return created
        except (DuplicateKeyError, WriteError):
            return await run_mongo_with_retry(
                "session_snapshots.apply_channel_update.retry_existing",
                _update_existing,
            )

    async def mark_channel_degraded(
        self,
        session_id: str,
        *,
        channel: str,
        event_seq: int,
        error_code: str,
        error_message: str,
    ) -> dict[str, Any] | None:
        degraded_field = f"degraded_channels.{channel}"
        return await self.apply_channel_update(
            session_id,
            channel=channel,
            event_seq=event_seq,
            updates={
                degraded_field: {
                    "event_seq": int(event_seq),
                    "error_code": error_code,
                    "error_message": error_message,
                    "marked_at": utcnow_iso(),
                }
            },
        )

    async def force_update_fields(
        self,
        session_id: str,
        updates: dict[str, Any],
        *,
        extra_filter: dict[str, Any] | None = None,
    ) -> bool:
        """Update snapshot fields bypassing watermark checks.

        When *extra_filter* is supplied the update is conditional (CAS):
        the write only lands if the snapshot also matches those fields.
        Returns True if the document was matched (write landed).
        """
        await self.ensure_indexes()
        collection = await get_async_collection(COLLECTION_NAME)
        now_iso = utcnow_iso()
        set_payload = {
            k: v for k, v in updates.items() if k not in ("_id", "session_id")
        }
        set_payload["updated_at"] = now_iso
        query: dict[str, Any] = {"session_id": session_id}
        if extra_filter:
            query.update(_normalize_extra_filter(extra_filter))
        result = await run_mongo_with_retry(
            "session_snapshots.force_update_fields",
            lambda: collection.update_one(
                query,
                {"$set": set_payload},
            ),
            fault_context={"session_id": session_id},
        )
        return bool(getattr(result, "matched_count", 0))
