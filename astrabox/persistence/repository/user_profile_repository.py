"""Repository for the user_profiles collection (user_id -> display_name mapping)."""

from __future__ import annotations

from astrabox.persistence.repository.backend import (
    get_async_collection,
    run_mongo_with_retry,
)
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.time_utils import utcnow_iso

logger = get_logger(__name__)

_COLLECTION_NAME = "user_profiles"


class UserProfileRepository:

    async def upsert_if_absent(self, user_id: str, display_name: str) -> None:
        if not user_id or not display_name:
            return

        async def _upsert() -> None:
            collection = await get_async_collection(_COLLECTION_NAME)
            existing = await collection.find_one({"_id": user_id})
            if existing is not None:
                return
            await collection.insert_one({
                "_id": user_id,
                "display_name": display_name,
                "created_at": utcnow_iso(),
            })

        try:
            await run_mongo_with_retry("user_profiles.upsert_if_absent", _upsert)
        except Exception:
            logger.debug("user_profiles upsert skipped (duplicate or error) user_id=%s", user_id)

    async def batch_get_display_names(self, user_ids: list[str]) -> dict[str, str]:
        """Return {user_id: display_name} for all matching user_ids."""
        if not user_ids:
            return {}

        async def _get() -> dict[str, str]:
            collection = await get_async_collection(_COLLECTION_NAME)
            result: dict[str, str] = {}
            for uid in user_ids:
                doc = await collection.find_one({"_id": uid})
                if doc and doc.get("display_name"):
                    result[uid] = doc["display_name"]
            return result

        return await run_mongo_with_retry("user_profiles.batch_get_display_names", _get)
