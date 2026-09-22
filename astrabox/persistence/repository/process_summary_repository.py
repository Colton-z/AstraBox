"""Repository for the session_process_summaries collection.

A process summary is a label the platform writes over a folded block of work.
It is never part of the conversation the engine sees, so it lives beside the
transcript rather than in it, keyed by the response it describes.

One row per ``(session_id, message_id)`` doubles as the generation lease:
:meth:`ProcessSummaryRepository.claim` inserts it, and a duplicate key means
another reader or the turn worker is already writing that label. A holder that
dies leaves the row ``generating`` forever, so the row carries ``expires_at``
and :meth:`ProcessSummaryRepository.read` marks an expired lease failed before
answering — the browser then sees a failure it can retry instead of a spinner
with nothing behind it.

Times are ISO strings throughout: the SQLite and PostgreSQL collection shims
compare stored JSON values, where a ``datetime`` has no ordering against the
strings every other field uses.
"""

from __future__ import annotations

import uuid
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.time_utils import plus_seconds_iso, utcnow_iso
from astrabox.persistence.repository._compat import DuplicateKeyError
from astrabox.persistence.repository.backend import (
    collect_async_cursor,
    get_async_collection,
    run_mongo_with_retry,
)

logger = get_logger(__name__)

_COLLECTION_NAME = "session_process_summaries"

#: How long a generation claim stays valid. A summary is one short completion;
#: a claim still open after this lost its holder.
_CLAIM_LEASE_SECONDS = 60

_EXPIRED_CLAIM_ERROR = "process summary generation expired"

_READ_FIELDS = ("status", "summary", "error", "through_seq", "turn_completed")


def _row_id(session_id: str, message_id: str) -> str:
    return f"{session_id}:{message_id}"


class ProcessSummaryRepository:
    """Claim, finish and read the generated label for one response."""

    async def claim(
        self,
        session_id: str,
        message_id: str,
        *,
        through_seq: int,
        turn_completed: bool,
        retry_failed: bool = False,
    ) -> str | None:
        """Take the generation lease, or report that someone else holds it.

        Returns the claim token the row now carries, or ``None`` when another
        holder has the row. The token identifies this one generation: it is
        what :meth:`finish` has to present, so a completion that comes back
        after its lease expired and the row was reopened cannot overwrite the
        newer holder's outcome.

        ``retry_failed`` first tries to reopen a row that failed, so a reader
        can ask again for a label whose model call did not come back. A row
        that is ``completed`` is never reopened.
        """

        row_id = _row_id(session_id, message_id)
        now = utcnow_iso()
        claim_token = uuid.uuid4().hex
        generating = {
            "status": "generating",
            "claim_token": claim_token,
            "through_seq": int(through_seq),
            "turn_completed": bool(turn_completed),
            "error": None,
            "summary": None,
            "expires_at": plus_seconds_iso(_CLAIM_LEASE_SECONDS),
            "updated_at": now,
        }

        if retry_failed:

            async def _retry() -> int:
                collection = await get_async_collection(_COLLECTION_NAME)
                result = await collection.update_one(
                    {"_id": row_id, "status": "failed"},
                    {"$set": generating},
                )
                return int(getattr(result, "modified_count", 0) or 0)

            if await run_mongo_with_retry("process_summaries.retry_claim", _retry):
                return claim_token

        async def _insert() -> bool:
            collection = await get_async_collection(_COLLECTION_NAME)
            try:
                await collection.insert_one(
                    {
                        "_id": row_id,
                        "session_id": session_id,
                        "message_id": message_id,
                        "created_at": now,
                        **generating,
                    }
                )
            except DuplicateKeyError:
                return False
            return True

        if await run_mongo_with_retry("process_summaries.claim", _insert):
            return claim_token
        return None

    async def finish(
        self,
        session_id: str,
        message_id: str,
        data: dict[str, Any],
        *,
        claim_token: str,
    ) -> bool:
        """Write the outcome of the claim ``claim_token`` names.

        The write is conditional on the row still being that claim, open.
        A completion that returns after :meth:`read` expired its lease and a
        retry reopened the row finds another token there and writes nothing;
        the newer generation's outcome stands. Returns whether the outcome
        was recorded.
        """

        row_id = _row_id(session_id, message_id)
        updates = {**data, "updated_at": utcnow_iso()}

        async def _finish() -> int:
            collection = await get_async_collection(_COLLECTION_NAME)
            result = await collection.update_one(
                {"_id": row_id, "status": "generating", "claim_token": claim_token},
                {"$set": updates},
            )
            return int(getattr(result, "matched_count", 0) or 0)

        recorded = bool(await run_mongo_with_retry("process_summaries.finish", _finish))
        if not recorded:
            logger.warning(
                "process summary outcome discarded: claim no longer current "
                "session=%s message=%s",
                session_id,
                message_id,
            )
        return recorded

    async def read(
        self,
        session_id: str,
        message_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        """Return the stored label for each requested response, if it has one.

        Expired claims are failed first, so a page never shows work in progress
        that nothing is working on. The caller compares each row's
        ``through_seq`` with its own checkpoint before showing the label.
        """

        if not message_ids:
            return {}
        ids = sorted({_row_id(session_id, message_id) for message_id in message_ids})
        now = utcnow_iso()

        async def _read() -> dict[str, dict[str, Any]]:
            collection = await get_async_collection(_COLLECTION_NAME)
            expired = await collection.update_many(
                {
                    "_id": {"$in": ids},
                    "status": "generating",
                    "expires_at": {"$lt": now},
                },
                {
                    "$set": {
                        "status": "failed",
                        "error": _EXPIRED_CLAIM_ERROR,
                        "updated_at": now,
                    }
                },
            )
            expired_count = int(getattr(expired, "modified_count", 0) or 0)
            if expired_count:
                logger.warning(
                    "process summary claims expired session=%s count=%s",
                    session_id,
                    expired_count,
                )
            rows = await collect_async_cursor(collection.find({"_id": {"$in": ids}}))
            return {
                str(row["message_id"]): {
                    key: row[key] for key in _READ_FIELDS if key in row
                }
                for row in rows
                if row.get("message_id")
            }

        return await run_mongo_with_retry("process_summaries.read", _read)
