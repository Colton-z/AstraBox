from __future__ import annotations

import hashlib
from collections.abc import Collection
from typing import Any

from astrabox.persistence.repository._compat import (
    BulkWriteError,
    DuplicateKeyError,
    OperationFailure,
    ReturnDocument,
    WriteError,
)

from astrabox.persistence.repository.backend import (
    get_async_collection,
    run_mongo_with_retry,
)
from astrabox.persistence.repository.index_verification import ensure_unique_index
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.time_utils import utcnow_iso

logger = get_logger(__name__)

COLLECTION_NAME = "session_events"
COUNTER_COLLECTION_NAME = "session_event_counters"
EVENT_KIND_FIELD = "event_kind"
EVENT_KIND_COMMAND = "command"
EVENT_KIND_STREAM = "stream"
EVENT_KIND_ENGINE_FRAME = "engine_frame"
IDEMPOTENCY_KEY_FIELD = "idempotency_key"
_IDEMPOTENCY_INDEX_NAME = "ux_session_events_idempotency_key"
_IDEMPOTENCY_INDEX_KEYS = [(IDEMPOTENCY_KEY_FIELD, 1)]
_MAX_INSERT_MANY_BATCH_SIZE = 500
_FRAME_SEMANTIC_KEYS = (
    "session_id",
    "command_id",
    "turn_id",
    "scope",
    "payload",
    "live_seq",
    "source_kind",
    "source_sandbox_turn_id",
    "source_sandbox_seq",
    "source_frame_index",
)


def _is_duplicate_error(exc: Exception) -> bool:
    if isinstance(exc, DuplicateKeyError):
        return True
    if isinstance(exc, WriteError):
        return "Duplicate entry" in str(exc) or exc.code in (1, 11000)
    text = str(exc).lower()
    if "duplicate" in text or "e11000" in text:
        return True
    if isinstance(exc, BulkWriteError):
        details = exc.details if isinstance(exc.details, dict) else {}
        return any(
            int(item.get("code") or 0) in (11000, 11001, 12582)
            or "duplicate" in str(item.get("errmsg") or "").lower()
            for item in details.get("writeErrors") or []
            if isinstance(item, dict)
        )
    if isinstance(exc, OperationFailure):
        return int(getattr(exc, "code", 0) or 0) in (11000, 11001, 12582)
    return False


def _frame_identity_filter(frame: dict[str, Any]) -> dict[str, Any]:
    session_id = str(frame.get("session_id") or "").strip()
    frame_seq = frame.get("frame_seq")
    if not session_id or isinstance(frame_seq, bool) or not isinstance(frame_seq, int):
        raise ValueError("engine frame requires session_id and integer frame_seq")
    return {
        "session_id": session_id,
        "event_seq": int(frame_seq),
        EVENT_KIND_FIELD: EVENT_KIND_ENGINE_FRAME,
    }


#: The part-vocabulary contract stamped on every segment-opening ``start``
#: row (docs/maintainers/seam-freeze-decisions-2026-08.md §1). Durable frames
#: outlive the deployment that wrote them, so a future reader must be able to
#: select the right renderer from the row itself instead of sniffing shapes.
#: One stamp per segment: the rows that follow a ``start`` belong to its
#: contract. Bump the suffix when the stored part vocabulary changes
#: incompatibly; rows written before the stamp landed carry no marker, and
#: the golden suite owns rendering them.
SEGMENT_PART_CONTRACT = "astrabox.parts/1"


def _stamped_part(part: Any) -> Any:
    if not isinstance(part, dict) or part.get("type") != "start":
        return part
    metadata = part.get("messageMetadata")
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    # A replayed historical row keeps its original stamp: the stamp records
    # what the segment was written under, not what the writer runs now.
    if "part_contract" not in metadata:
        metadata = {**metadata, "part_contract": SEGMENT_PART_CONTRACT}
    return {**part, "messageMetadata": metadata}


def _stored_frame(frame: dict[str, Any]) -> dict[str, Any]:
    identity = _frame_identity_filter(frame)
    doc = dict(frame)
    doc.pop("frame_seq", None)
    derived_scope = "turn" if str(doc.get("turn_id") or "").strip() else "session"
    declared_scope = str(doc.get("scope") or "").strip()
    if declared_scope and declared_scope != derived_scope:
        raise ValueError(
            "engine frame scope disagrees with turn ownership "
            f"scope={declared_scope!r} turn_id={doc.get('turn_id')!r}"
        )
    doc["scope"] = derived_scope
    if "payload" in doc:
        doc["payload"] = _stamped_part(doc["payload"])
    return {
        **doc,
        **identity,
        "event_type": "engine.frame",
    }


def _public_frame(event: dict[str, Any]) -> dict[str, Any]:
    frame = dict(event)
    frame["frame_seq"] = int(frame.pop("event_seq"))
    frame.pop("_id", None)
    frame.pop(EVENT_KIND_FIELD, None)
    frame.pop("event_type", None)
    return frame


def _frame_semantics_match(
    existing: dict[str, Any],
    expected: dict[str, Any],
) -> bool:
    return all(existing.get(key) == expected.get(key) for key in _FRAME_SEMANTIC_KEYS)


def _command_idempotency_key(session_id: str, causation_id: str, event_type: str) -> str:
    return f"command:{session_id}:{causation_id}:{event_type}"


def _command_claim_doc_id(idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    return f"session_event_claim:{digest}"


async def _ensure_indexes_once(collection) -> None:
    # The session-id+seq ordering key is the one safety-critical uniqueness
    # guarantee here: append_event's collision-retry allocation depends on the
    # index being real, not just attempted — verified via the shared helper,
    # which raises loud if it is missing.
    await ensure_unique_index(
        collection,
        [("session_id", 1), ("event_seq", 1)],
        name="ux_session_events_seq",
        collection_name=COLLECTION_NAME,
    )
    try:
        await collection.create_index(
            [("session_id", 1), ("channel", 1), ("turn_id", 1), ("event_type", 1)],
            name="ix_session_events_channel_turn_type",
        )
        await collection.create_index(
            [("session_id", 1), ("correlation_id", 1), ("event_seq", 1)],
            name="ix_session_events_correlation_seq",
        )
        await collection.create_index(
            [
                ("session_id", 1),
                (EVENT_KIND_FIELD, 1),
                ("turn_id", 1),
                ("event_seq", 1),
            ],
            name="ix_session_events_engine_turn_seq",
        )
    except Exception as exc:
        logger.warning("session event secondary index creation failed (non-blocking): %s", exc)

    # The sparse idempotency-key index is defense-in-depth metadata:
    # try_claim_event's exactly-once semantics are fenced by the deterministic
    # ``_id`` (see its docstring), not by this index. Losing it must not take
    # down every journal read/append — e.g. a managed-Mongo deployment
    # (runtime index creation opted out) whose DBAs pre-created the
    # load-bearing seq index but missed this secondary one. Warn loud, proceed.
    try:
        await ensure_unique_index(
            collection,
            _IDEMPOTENCY_INDEX_KEYS,
            name=_IDEMPOTENCY_INDEX_NAME,
            sparse=True,
            collection_name=COLLECTION_NAME,
        )
    except RuntimeError as exc:
        logger.warning(
            "session event idempotency index unavailable (non-blocking; "
            "exactly-once stays fenced by the claim _id): %s",
            exc,
        )


_index_ready = False
_counter_index_ready = False


async def _ensure_counter_index_once(collection: Any) -> None:
    await ensure_unique_index(
        collection,
        [("session_id", 1)],
        name="ux_session_event_counters_session",
        collection_name=COUNTER_COLLECTION_NAME,
    )


class SessionEventRepository:
    async def ensure_indexes(self) -> None:
        global _index_ready
        if _index_ready:
            return
        collection = await get_async_collection(COLLECTION_NAME)
        if collection is not None:
            await _ensure_indexes_once(collection)
        _index_ready = True

    async def ensure_counter_indexes(self) -> None:
        global _counter_index_ready
        if _counter_index_ready:
            return
        collection = await get_async_collection(COUNTER_COLLECTION_NAME)
        if collection is not None:
            await _ensure_counter_index_once(collection)
        _counter_index_ready = True

    async def allocate_event_sequence(
        self,
        session_id: str,
        *,
        count: int = 1,
    ) -> int:
        """Reserve one ordered range shared by commands and engine output."""

        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            raise ValueError("session_id is required")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("count must be a positive integer")
        await self.ensure_indexes()
        await self.ensure_counter_indexes()
        events = await get_async_collection(COLLECTION_NAME)
        counters = await get_async_collection(COUNTER_COLLECTION_NAME)

        async def _allocate() -> int:
            existing = await counters.find_one({"session_id": normalized_session_id})
            if not isinstance(existing, dict):
                latest = await events.find_one(
                    {"session_id": normalized_session_id},
                    projection={"event_seq": 1},
                    sort=[("event_seq", -1)],
                )
                next_event_seq = (
                    int(latest.get("event_seq") or 0) + 1
                    if isinstance(latest, dict)
                    else 1
                )
                try:
                    await counters.insert_one(
                        {
                            "session_id": normalized_session_id,
                            "next_event_seq": next_event_seq,
                        }
                    )
                except Exception as exc:
                    if not _is_duplicate_error(exc):
                        raise
            updated = await counters.find_one_and_update(
                {"session_id": normalized_session_id},
                {"$inc": {"next_event_seq": count}},
                return_document=ReturnDocument.AFTER,
            )
            if not isinstance(updated, dict):
                raise RuntimeError(
                    "failed to allocate session event sequence "
                    f"session_id={normalized_session_id}"
                )
            return int(updated.get("next_event_seq") or 0) - count

        return await run_mongo_with_retry("session_events.allocate_sequence", _allocate)

    async def append_event(self, event: dict[str, Any]) -> dict[str, Any]:
        await self.ensure_indexes()
        collection = await get_async_collection(COLLECTION_NAME)
        session_id = str(event.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("session_id is required")

        for attempt in range(1, 6):
            event_seq = await self.allocate_event_sequence(session_id)
            event_type = str(event.get("event_type") or "").strip()
            event_kind = (
                EVENT_KIND_COMMAND
                if event_type == "command.accepted"
                else EVENT_KIND_STREAM
            )
            doc = {
                "event_seq": event_seq,
                "occurred_at": utcnow_iso(),
                **event,
                EVENT_KIND_FIELD: event_kind,
            }

            async def _insert() -> None:
                await collection.insert_one(doc)

            try:
                await run_mongo_with_retry("session_events.append_event", _insert)
                return doc
            except (DuplicateKeyError, WriteError) as exc:
                if not _is_duplicate_error(exc):
                    raise
                logger.warning(
                    "session event seq collision session_id=%s attempt=%s err=%s",
                    session_id,
                    attempt,
                    exc,
                )
                continue

        raise RuntimeError(
            f"failed to allocate session event_seq after retries session_id={session_id}"
        )

    async def try_claim_event(self, event: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Atomically claim a command event using a deterministic primary key.

        Returns (event_doc, created):
          - (new_doc, True)      if the event was newly inserted
          - (existing_doc, False) if an event with the same key already exists

        ``idempotency_key`` remains query/debug metadata. Correctness is fenced
        by ``_id`` because the production PStore layer only reliably enforces
        primary-key uniqueness.
        """
        await self.ensure_indexes()
        collection = await get_async_collection(COLLECTION_NAME)
        session_id = str(event.get("session_id") or "").strip()
        causation_id = str(event.get("causation_id") or "").strip()
        event_type = str(event.get("event_type") or "").strip()
        if not session_id or not causation_id or not event_type:
            raise ValueError("session_id, causation_id, and event_type are required")
        idempotency_key = _command_idempotency_key(session_id, causation_id, event_type)
        claim_doc_id = _command_claim_doc_id(idempotency_key)

        idem_filter = {"_id": claim_doc_id}

        async def _find_existing() -> dict[str, Any] | None:
            return await collection.find_one(idem_filter)

        existing = await run_mongo_with_retry(
            "session_events.claim.find", _find_existing,
        )
        if isinstance(existing, dict):
            return dict(existing), False

        for attempt in range(1, 6):
            event_seq = await self.allocate_event_sequence(session_id)
            doc = {
                "_id": claim_doc_id,
                "event_seq": event_seq,
                "occurred_at": utcnow_iso(),
                **event,
                EVENT_KIND_FIELD: EVENT_KIND_COMMAND,
                IDEMPOTENCY_KEY_FIELD: idempotency_key,
            }

            async def _insert() -> None:
                await collection.insert_one(doc)

            try:
                await run_mongo_with_retry(
                    "session_events.claim.insert", _insert,
                )
                return doc, True
            except (DuplicateKeyError, WriteError) as exc:
                if not _is_duplicate_error(exc):
                    raise
                existing = await run_mongo_with_retry(
                    "session_events.claim.recheck", _find_existing,
                )
                if isinstance(existing, dict):
                    return dict(existing), False
                logger.warning(
                    "session event claim seq collision session_id=%s attempt=%s",
                    session_id,
                    attempt,
                )
                continue

        raise RuntimeError(
            f"failed to claim session event after retries session_id={session_id}"
        )

    async def append_frame(self, frame: dict[str, Any]) -> None:
        await self.append_frames([frame])

    async def append_frames(self, frames: list[dict[str, Any]]) -> None:
        if not frames:
            return
        await self.ensure_indexes()
        collection = await get_async_collection(COLLECTION_NAME)
        stored = [_stored_frame(frame) for frame in frames]
        for start in range(0, len(stored), _MAX_INSERT_MANY_BATCH_SIZE):
            await self._append_frame_chunk(
                collection,
                stored[start : start + _MAX_INSERT_MANY_BATCH_SIZE],
            )

    async def _append_frame_chunk(
        self,
        collection: Any,
        frames: list[dict[str, Any]],
    ) -> None:
        pending = list(frames)
        for _attempt in range(len(frames) + 1):
            try:
                if len(pending) == 1:
                    await run_mongo_with_retry(
                        "session_events.append_frame",
                        lambda: collection.insert_one(dict(pending[0])),
                    )
                else:
                    await run_mongo_with_retry(
                        "session_events.append_frames",
                        lambda: collection.insert_many(
                            [dict(frame) for frame in pending],
                            ordered=True,
                        ),
                    )
                return
            except Exception as exc:
                if not _is_duplicate_error(exc):
                    raise
                pending = await self._missing_frame_events(collection, frames)
                if not pending:
                    return
        raise RuntimeError("session event frame append did not converge")

    async def _missing_frame_events(
        self,
        collection: Any,
        expected: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        missing: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        for frame in expected:
            identity = {
                "session_id": frame["session_id"],
                "event_seq": frame["event_seq"],
                EVENT_KIND_FIELD: EVENT_KIND_ENGINE_FRAME,
            }
            existing = await collection.find_one(identity)
            if not isinstance(existing, dict):
                missing.append(frame)
            elif not _frame_semantics_match(existing, frame):
                conflicts.append(identity)
        if conflicts:
            raise RuntimeError(
                "session event idempotent frame append conflicts "
                f"identities={conflicts[:3]}"
            )
        return missing

    async def allocate_session_frame_seq(
        self,
        session_id: str,
        *,
        count: int = 1,
    ) -> int:
        return await self.allocate_event_sequence(session_id, count=count)

    async def get_next_session_frame_seq(self, session_id: str) -> int:
        return await self.allocate_event_sequence(session_id)

    async def get_max_session_frame_seq(self, session_id: str) -> int | None:
        return await self._get_max_frame_seq(session_id)

    async def get_max_turn_frame_seq(
        self,
        session_id: str,
        *,
        turn_id: str,
    ) -> int | None:
        return await self._get_max_frame_seq(session_id, turn_id=turn_id)

    async def _get_max_frame_seq(
        self,
        session_id: str,
        *,
        turn_id: str | None = None,
    ) -> int | None:
        await self.ensure_indexes()
        collection = await get_async_collection(COLLECTION_NAME)
        query: dict[str, Any] = {
            "session_id": session_id,
            EVENT_KIND_FIELD: EVENT_KIND_ENGINE_FRAME,
        }
        if turn_id is not None:
            query["turn_id"] = turn_id

        async def _query() -> int | None:
            row = await collection.find_one(
                query,
                projection={"event_seq": 1},
                sort=[("event_seq", -1)],
            )
            if not isinstance(row, dict):
                return None
            return int(row["event_seq"])

        return await run_mongo_with_retry("session_events.max_frame_seq", _query)

    async def list_frames(
        self,
        session_id: str,
        *,
        command_id: str | None = None,
        turn_id: str | None = None,
        turn_ids: Collection[str] | None = None,
        scope: str | None = None,
        after_seq: int = -1,
        before_seq: int | None = None,
        limit: int = 500,
        newest_first: bool = False,
    ) -> list[dict[str, Any]]:
        await self.ensure_indexes()
        collection = await get_async_collection(COLLECTION_NAME)
        if scope is not None and scope not in {"turn", "session"}:
            raise ValueError(f"unsupported engine frame scope={scope!r}")
        if turn_id is not None and turn_ids is not None:
            raise ValueError("turn_id and turn_ids are mutually exclusive")
        sequence_range: dict[str, int] = {"$gt": int(after_seq)}
        if before_seq is not None:
            sequence_range["$lt"] = int(before_seq)
        query: dict[str, Any] = {
            "session_id": session_id,
            EVENT_KIND_FIELD: EVENT_KIND_ENGINE_FRAME,
            "event_seq": sequence_range,
        }
        if command_id is not None:
            query["command_id"] = command_id
        if scope is not None:
            query["scope"] = scope
        if turn_id is not None:
            query["turn_id"] = turn_id
        elif turn_ids is not None:
            normalized_turn_ids = sorted(
                {
                    str(candidate or "").strip()
                    for candidate in turn_ids
                    if str(candidate or "").strip()
                }
            )
            if not normalized_turn_ids:
                return []
            query["turn_id"] = {"$in": normalized_turn_ids}

        async def _query() -> list[dict[str, Any]]:
            direction = -1 if newest_first else 1
            cursor = collection.find(query).sort("event_seq", direction).limit(limit)
            return [_public_frame(doc) async for doc in cursor]

        return await run_mongo_with_retry("session_events.list_frames", _query)

    async def get_command_event(
        self,
        session_id: str,
        *,
        command_id: str,
    ) -> dict[str, Any] | None:
        await self.ensure_indexes()
        collection = await get_async_collection(COLLECTION_NAME)
        return await run_mongo_with_retry(
            "session_events.get_command_event",
            lambda: collection.find_one(
                {
                    "session_id": session_id,
                    EVENT_KIND_FIELD: EVENT_KIND_COMMAND,
                    "event_type": "command.accepted",
                    "causation_id": command_id,
                },
            ),
        )

    async def find_command_by_client_message_id(
        self,
        session_id: str,
        *,
        client_message_id: str,
    ) -> dict[str, Any] | None:
        """The accepted command carrying this caller-supplied message id.

        The channel spine's attach-not-append idempotency read
        (docs/channel-spine.md): a re-driving worker asks whether its dispatch
        token already produced a command before appending a second one. The
        token is unique per drive attempt, so at most one command matches.
        """
        await self.ensure_indexes()
        collection = await get_async_collection(COLLECTION_NAME)
        return await run_mongo_with_retry(
            "session_events.find_command_by_client_message_id",
            lambda: collection.find_one(
                {
                    "session_id": session_id,
                    EVENT_KIND_FIELD: EVENT_KIND_COMMAND,
                    "event_type": "command.accepted",
                    "payload.client_message_id": client_message_id,
                },
            ),
        )

    async def find_input_command_by_input_id(
        self,
        session_id: str,
        *,
        input_id: str,
    ) -> dict[str, Any] | None:
        await self.ensure_indexes()
        collection = await get_async_collection(COLLECTION_NAME)
        return await run_mongo_with_retry(
            "session_events.find_input_command_by_input_id",
            lambda: collection.find_one(
                {
                    "session_id": session_id,
                    EVENT_KIND_FIELD: EVENT_KIND_COMMAND,
                    "event_type": "command.accepted",
                    "payload.input_id": input_id,
                },
            ),
        )

    async def list_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        before_seq: int | None = None,
        channel: str | None = None,
        turn_id: str | None = None,
        event_type: str | None = None,
        event_types: Collection[str] | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        limit: int = 500,
        newest_first: bool = False,
    ) -> list[dict[str, Any]]:
        await self.ensure_indexes()
        collection = await get_async_collection(COLLECTION_NAME)
        if event_type is not None and event_types is not None:
            raise ValueError("event_type and event_types are mutually exclusive")
        sequence_range: dict[str, int] = {"$gt": int(after_seq)}
        if before_seq is not None:
            sequence_range["$lt"] = int(before_seq)
        query: dict[str, Any] = {
            "session_id": session_id,
            EVENT_KIND_FIELD: {"$in": [EVENT_KIND_COMMAND, EVENT_KIND_STREAM]},
            "event_seq": sequence_range,
        }
        if channel is not None:
            query["channel"] = channel
        if turn_id is not None:
            query["turn_id"] = turn_id
        if event_type is not None:
            query["event_type"] = event_type
        elif event_types is not None:
            normalized_event_types = sorted(
                {
                    str(candidate or "").strip()
                    for candidate in event_types
                    if str(candidate or "").strip()
                }
            )
            if not normalized_event_types:
                return []
            query["event_type"] = {"$in": normalized_event_types}
        if correlation_id is not None:
            query["correlation_id"] = correlation_id
        if causation_id is not None:
            query["causation_id"] = causation_id

        async def _list() -> list[dict[str, Any]]:
            direction = -1 if newest_first else 1
            cursor = collection.find(query).sort("event_seq", direction).limit(limit)
            return [doc async for doc in cursor]

        return await run_mongo_with_retry(
            "session_events.list_events",
            _list,
        )
