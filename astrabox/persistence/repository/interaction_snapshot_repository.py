from __future__ import annotations

from typing import Any

from astrabox.persistence.repository._compat import DuplicateKeyError

from astrabox.persistence.repository.backend import (
    get_async_collection,
    run_mongo_with_retry,
)
from astrabox.persistence.repository.index_verification import ensure_unique_index
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.time_utils import utcnow_iso

logger = get_logger(__name__)

COLLECTION_NAME = "interaction_snapshots"


async def _ensure_indexes_once(collection) -> None:
    await ensure_unique_index(
        collection,
        [("session_id", 1), ("interaction_id", 1)],
        name="ux_interaction_snapshots_session_interaction",
        collection_name=COLLECTION_NAME,
    )
    try:
        await collection.create_index(
            [("session_id", 1), ("active", 1), ("updated_at", -1)],
            name="ix_interaction_snapshots_active",
        )
        await collection.create_index(
            [("session_id", 1), ("updated_at", -1)],
            name="ix_interaction_snapshots_session_updated",
        )
        await collection.create_index(
            [("session_id", 1), ("sandbox_turn_id", 1), ("updated_at", -1)],
            name="ix_interaction_snapshots_session_sandbox_turn",
        )
    except Exception as exc:
        logger.warning("interaction snapshot index creation failed (non-blocking): %s", exc)


_index_ready = False


class InteractionSnapshotRepository:
    async def ensure_indexes(self) -> None:
        global _index_ready
        if _index_ready:
            return
        collection = await get_async_collection(COLLECTION_NAME)
        if collection is not None:
            await _ensure_indexes_once(collection)
        _index_ready = True

    async def get_active_interaction(self, session_id: str) -> dict[str, Any] | None:
        await self.ensure_indexes()
        collection = await get_async_collection(COLLECTION_NAME)
        return await run_mongo_with_retry(
            "interaction_snapshots.get_active_interaction",
            lambda: collection.find_one(
                {"session_id": session_id, "active": True},
                sort=[("updated_at", -1)],
            ),
        )

    async def get_latest_interaction(self, session_id: str) -> dict[str, Any] | None:
        await self.ensure_indexes()
        collection = await get_async_collection(COLLECTION_NAME)
        return await run_mongo_with_retry(
            "interaction_snapshots.get_latest_interaction",
            lambda: collection.find_one(
                {"session_id": session_id},
                sort=[("updated_at", -1)],
            ),
        )

    async def get_interaction(
        self,
        session_id: str,
        interaction_id: str,
    ) -> dict[str, Any] | None:
        await self.ensure_indexes()
        collection = await get_async_collection(COLLECTION_NAME)
        return await run_mongo_with_retry(
            "interaction_snapshots.get_interaction",
            lambda: collection.find_one(
                {
                    "session_id": session_id,
                    "interaction_id": interaction_id,
                }
            ),
        )

    async def find_latest_by_sandbox_turn_id(
        self,
        session_id: str,
        sandbox_turn_id: int,
    ) -> dict[str, Any] | None:
        await self.ensure_indexes()
        collection = await get_async_collection(COLLECTION_NAME)
        return await run_mongo_with_retry(
            "interaction_snapshots.find_latest_by_sandbox_turn_id",
            lambda: collection.find_one(
                {
                    "session_id": session_id,
                    "sandbox_turn_id": int(sandbox_turn_id),
                },
                sort=[("updated_at", -1)],
            ),
        )

    async def deactivate_all_active(self, session_id: str) -> int:
        """Deactivate all active interaction snapshots for a session.

        Called when a turn completes, fails, or is interrupted to prevent
        stale pending interactions from lingering after the turn ends.
        Returns the number of documents modified.
        """
        await self.ensure_indexes()
        collection = await get_async_collection(COLLECTION_NAME)
        now_iso = utcnow_iso()

        async def _do_update():
            result = await collection.update_many(
                {"session_id": session_id, "active": True},
                {"$set": {"active": False, "updated_at": now_iso}},
            )
            return result.modified_count if result else 0

        return await run_mongo_with_retry(
            "interaction_snapshots.deactivate_all_active",
            _do_update,
        )

    async def deactivate_active_for_turn(self, session_id: str, turn_id: str) -> int:
        """Deactivate active interaction snapshots that belong to a terminal turn."""
        await self.ensure_indexes()
        collection = await get_async_collection(COLLECTION_NAME)
        now_iso = utcnow_iso()

        async def _do_update():
            result = await collection.update_many(
                {
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "active": True,
                },
                {"$set": {"active": False, "updated_at": now_iso}},
            )
            return result.modified_count if result else 0

        return await run_mongo_with_retry(
            "interaction_snapshots.deactivate_active_for_turn",
            _do_update,
        )

    async def upsert_snapshot(
        self,
        *,
        session_id: str,
        interaction_id: str,
        source_event_seq_applied: int,
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        await self.ensure_indexes()
        collection = await get_async_collection(COLLECTION_NAME)
        now_iso = utcnow_iso()
        payload = {
            **dict(updates),
            "session_id": session_id,
            "interaction_id": interaction_id,
            "source_event_seq_applied": int(source_event_seq_applied),
            "updated_at": now_iso,
        }

        # Strip identity/index fields from $set to avoid
        # "_id not allow update" on MongoDB-compatible servers.
        update_payload = {
            k: v
            for k, v in payload.items()
            if k not in ("_id", "session_id", "interaction_id")
        }

        async def _update_existing() -> dict[str, Any] | None:
            from astrabox.persistence.repository._compat import ReturnDocument

            return await collection.find_one_and_update(
                {
                    "session_id": session_id,
                    "interaction_id": interaction_id,
                    "$or": [
                        {"source_event_seq_applied": {"$lt": int(source_event_seq_applied)}},
                        {"source_event_seq_applied": {"$exists": False}},
                    ],
                },
                {"$set": update_payload},
                return_document=ReturnDocument.AFTER,
            )

        updated = await run_mongo_with_retry(
            "interaction_snapshots.upsert_snapshot.update_existing",
            _update_existing,
        )
        if isinstance(updated, dict):
            return updated

        existing = await run_mongo_with_retry(
            "interaction_snapshots.upsert_snapshot.read_existing",
            lambda: collection.find_one(
                {"session_id": session_id, "interaction_id": interaction_id},
                projection={"source_event_seq_applied": 1},
            ),
        )
        if isinstance(existing, dict):
            existing_seq = int(existing.get("source_event_seq_applied") or 0)
            if existing_seq >= int(source_event_seq_applied):
                return None

        created = {
            "created_at": now_iso,
            **payload,
        }

        async def _insert_new() -> None:
            await collection.insert_one(created)

        try:
            await run_mongo_with_retry(
                "interaction_snapshots.upsert_snapshot.insert_new",
                _insert_new,
            )
            return created
        except DuplicateKeyError:
            return await run_mongo_with_retry(
                "interaction_snapshots.upsert_snapshot.retry_existing",
                _update_existing,
            )

    async def project_open_interaction(
        self,
        *,
        session_id: str,
        interaction_id: str,
        source_event_seq_applied: int,
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Project an interaction into OPEN without resurrecting resolved state.

        Once an interaction becomes ANSWERED/inactive, later OPEN projections
        for the same interaction_id must not overwrite it, even if they carry a
        larger event_seq.
        """
        await self.ensure_indexes()
        collection = await get_async_collection(COLLECTION_NAME)
        now_iso = utcnow_iso()
        payload = {
            **dict(updates),
            "session_id": session_id,
            "interaction_id": interaction_id,
            "source_event_seq_applied": int(source_event_seq_applied),
            "updated_at": now_iso,
        }
        update_payload = {
            k: v
            for k, v in payload.items()
            if k not in ("_id", "session_id", "interaction_id")
        }

        async def _update_existing() -> dict[str, Any] | None:
            from astrabox.persistence.repository._compat import ReturnDocument

            return await collection.find_one_and_update(
                {
                    "session_id": session_id,
                    "interaction_id": interaction_id,
                    "interaction_state": {"$ne": "ANSWERED"},
                    "active": {"$ne": False},
                    "$or": [
                        {"source_event_seq_applied": {"$lt": int(source_event_seq_applied)}},
                        {"source_event_seq_applied": {"$exists": False}},
                    ],
                },
                {"$set": update_payload},
                return_document=ReturnDocument.AFTER,
            )

        updated = await run_mongo_with_retry(
            "interaction_snapshots.project_open_interaction.update_existing",
            _update_existing,
        )
        if isinstance(updated, dict):
            return updated

        existing = await run_mongo_with_retry(
            "interaction_snapshots.project_open_interaction.read_existing",
            lambda: collection.find_one(
                {"session_id": session_id, "interaction_id": interaction_id},
            ),
        )
        if isinstance(existing, dict):
            return existing

        created = {
            "created_at": now_iso,
            **payload,
        }

        async def _insert_new() -> None:
            await collection.insert_one(created)

        try:
            await run_mongo_with_retry(
                "interaction_snapshots.project_open_interaction.insert_new",
                _insert_new,
            )
            return created
        except DuplicateKeyError:
            return await run_mongo_with_retry(
                "interaction_snapshots.project_open_interaction.retry_existing",
                lambda: collection.find_one(
                    {"session_id": session_id, "interaction_id": interaction_id},
                ),
            )

    async def project_answered_interaction(
        self,
        *,
        session_id: str,
        interaction_id: str,
        source_event_seq_applied: int,
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Project an interaction into ANSWERED as an absorbing state."""
        await self.ensure_indexes()
        collection = await get_async_collection(COLLECTION_NAME)
        now_iso = utcnow_iso()
        payload = {
            **dict(updates),
            "interaction_state": "ANSWERED",
            "active": False,
            "session_id": session_id,
            "interaction_id": interaction_id,
            "source_event_seq_applied": int(source_event_seq_applied),
            "updated_at": now_iso,
        }
        update_payload = {
            k: v
            for k, v in payload.items()
            if k not in ("_id", "session_id", "interaction_id")
        }

        async def _update_existing() -> dict[str, Any] | None:
            from astrabox.persistence.repository._compat import ReturnDocument

            return await collection.find_one_and_update(
                {"session_id": session_id, "interaction_id": interaction_id},
                {"$set": update_payload},
                return_document=ReturnDocument.AFTER,
            )

        updated = await run_mongo_with_retry(
            "interaction_snapshots.project_answered_interaction.update_existing",
            _update_existing,
        )
        if isinstance(updated, dict):
            return updated

        created = {
            "created_at": now_iso,
            **payload,
        }

        async def _insert_new() -> None:
            await collection.insert_one(created)

        try:
            await run_mongo_with_retry(
                "interaction_snapshots.project_answered_interaction.insert_new",
                _insert_new,
            )
            return created
        except DuplicateKeyError:
            return await run_mongo_with_retry(
                "interaction_snapshots.project_answered_interaction.retry_existing",
                _update_existing,
            )

    async def try_answer_interaction(
        self,
        *,
        session_id: str,
        interaction_id: str,
        source_event_seq_applied: int,
        answer: dict[str, Any],
        updates: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Atomic CAS: OPEN -> ANSWERED.  Returns the updated doc on success,
        None if the interaction is already ANSWERED or doesn't exist."""
        await self.ensure_indexes()
        collection = await get_async_collection(COLLECTION_NAME)
        now_iso = utcnow_iso()
        update_payload = {
            **dict(updates or {}),
            "interaction_state": "ANSWERED",
            "response": dict(answer),
            "active": False,
            "source_event_seq_applied": int(source_event_seq_applied),
            "updated_at": now_iso,
        }
        update_payload = {
            k: v
            for k, v in update_payload.items()
            if k not in ("_id", "session_id", "interaction_id")
        }

        async def _cas() -> dict[str, Any] | None:
            from astrabox.persistence.repository._compat import ReturnDocument

            return await collection.find_one_and_update(
                {
                    "session_id": session_id,
                    "interaction_id": interaction_id,
                    "interaction_state": "OPEN",
                },
                {"$set": update_payload},
                return_document=ReturnDocument.AFTER,
            )

        return await run_mongo_with_retry(
            "interaction_snapshots.try_answer_interaction",
            _cas,
        )
