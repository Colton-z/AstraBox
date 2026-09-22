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

COLLECTION_NAME = "artifacts"


async def _ensure_indexes_once(collection) -> None:
    await ensure_unique_index(
        collection,
        [("session_id", 1), ("artifact_id", 1)],
        name="ux_artifacts_session_artifact",
        collection_name=COLLECTION_NAME,
    )
    try:
        await collection.create_index(
            [("session_id", 1), ("turn_id", 1), ("artifact_type", 1), ("updated_at", -1)],
            name="ix_artifacts_session_turn_type",
        )
        await collection.create_index(
            [("session_id", 1), ("artifact_type", 1), ("command_id", 1), ("terminal_seq", 1)],
            name="ix_artifacts_terminal_event_seq",
        )
    except Exception as exc:
        logger.warning("artifact index creation failed (non-blocking): %s", exc)


_index_ready = False


class ArtifactRepository:
    async def ensure_indexes(self) -> None:
        global _index_ready
        if _index_ready:
            return
        collection = await get_async_collection(COLLECTION_NAME)
        if collection is not None:
            await _ensure_indexes_once(collection)
        _index_ready = True

    async def upsert_artifact(self, artifact: dict[str, Any]) -> dict[str, Any]:
        await self.ensure_indexes()
        collection = await get_async_collection(COLLECTION_NAME)
        session_id = str(artifact.get("session_id") or "").strip()
        artifact_id = str(artifact.get("artifact_id") or "").strip()
        if not session_id or not artifact_id:
            raise ValueError("session_id and artifact_id are required")

        payload = {
            **dict(artifact),
            "updated_at": utcnow_iso(),
        }

        async def _update_existing():
            return await collection.update_one(
                {"session_id": session_id, "artifact_id": artifact_id},
                {"$set": payload},
                upsert=False,
            )

        updated = await run_mongo_with_retry(
            "artifacts.upsert_artifact.update_existing",
            _update_existing,
        )
        if int(getattr(updated, "matched_count", 0) or 0) <= 0:
            created = {
                "created_at": utcnow_iso(),
                **payload,
            }

            async def _insert_new() -> None:
                await collection.insert_one(created)

            try:
                await run_mongo_with_retry(
                    "artifacts.upsert_artifact.insert_new",
                    _insert_new,
                )
            except DuplicateKeyError:
                logger.warning(
                    "artifact upsert collided session_id=%s artifact_id=%s; retrying update",
                    session_id,
                    artifact_id,
                )
                await run_mongo_with_retry(
                    "artifacts.upsert_artifact.retry_existing",
                    _update_existing,
                )

        stored = await run_mongo_with_retry(
            "artifacts.read_after_upsert",
            lambda: collection.find_one(
                {"session_id": session_id, "artifact_id": artifact_id},
            ),
        )
        return stored or payload

    async def list_artifacts(
        self,
        session_id: str,
        *,
        turn_id: str | None = None,
        artifact_type: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        await self.ensure_indexes()
        collection = await get_async_collection(COLLECTION_NAME)
        query: dict[str, Any] = {"session_id": session_id}
        if turn_id is not None:
            query["turn_id"] = turn_id
        if artifact_type is not None:
            query["artifact_type"] = artifact_type

        async def _list() -> list[dict[str, Any]]:
            cursor = collection.find(query).sort("updated_at", -1).limit(limit)
            return [doc async for doc in cursor]

        return await run_mongo_with_retry("artifacts.list_artifacts", _list)

    async def get_artifact(
        self,
        session_id: str,
        artifact_id: str,
    ) -> dict[str, Any] | None:
        await self.ensure_indexes()
        collection = await get_async_collection(COLLECTION_NAME)
        return await run_mongo_with_retry(
            "artifacts.get_artifact",
            lambda: collection.find_one(
                {
                    "session_id": str(session_id or "").strip(),
                    "artifact_id": str(artifact_id or "").strip(),
                }
            ),
        )

    async def append_terminal_event(self, event: dict[str, Any]) -> dict[str, Any]:
        await self.ensure_indexes()
        session_id = str(event.get("session_id") or "").strip()
        command_id = str(event.get("command_id") or "").strip()
        terminal_seq = int(event.get("terminal_seq") or 0)
        if not session_id or not command_id:
            raise ValueError("session_id and command_id are required")

        artifact_id = str(event.get("artifact_id") or "").strip() or f"terminal:{command_id}:{terminal_seq}"
        payload = {
            **dict(event),
            "artifact_id": artifact_id,
            "artifact_type": "terminal_event",
        }
        return await self.upsert_artifact(payload)

    async def list_terminal_events(
        self,
        session_id: str,
        *,
        command_id: str | None = None,
        after_seq: int = -1,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        await self.ensure_indexes()
        collection = await get_async_collection(COLLECTION_NAME)
        query: dict[str, Any] = {
            "session_id": session_id,
            "artifact_type": "terminal_event",
            "terminal_seq": {"$gt": int(after_seq)},
        }
        if command_id is not None:
            query["command_id"] = command_id

        async def _list() -> list[dict[str, Any]]:
            cursor = collection.find(query).sort("terminal_seq", 1).limit(limit)
            return [doc async for doc in cursor]

        return await run_mongo_with_retry("artifacts.list_terminal_events", _list)
