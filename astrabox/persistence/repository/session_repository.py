from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from typing import Any

from astrabox.persistence.repository._compat import (
    AutoReconnect,
    ConnectionFailure,
    DuplicateKeyError,
    NetworkTimeout,
    OperationFailure,
    ServerSelectionTimeoutError,
)

from astrabox.persistence.repository.backend import (
    collect_async_cursor,
    get_async_collection,
    run_mongo_with_retry,
)
from astrabox.persistence.repository.index_verification import ensure_unique_index
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.core.model import SessionState

logger = get_logger(__name__)


def _encode_session_list_cursor(*, updated_at: str, session_id: str) -> str:
    payload = {
        "updated_at": str(updated_at or "").strip(),
        "session_id": str(session_id or "").strip(),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_session_list_cursor(cursor: str | None) -> dict[str, str] | None:
    value = str(cursor or "").strip()
    if not value:
        return None
    try:
        raw = base64.urlsafe_b64decode((value + "=" * (-len(value) % 4)).encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError("invalid session list cursor") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid session list cursor")
    updated_at = str(payload.get("updated_at") or "").strip()
    session_id = str(payload.get("session_id") or "").strip()
    if not updated_at or not session_id:
        raise ValueError("invalid session list cursor")
    return {
        "updated_at": updated_at,
        "session_id": session_id,
    }


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


def _turn_preparation_active_turn_id(session: dict[str, Any]) -> str:
    """Canonical session-row turn owner."""
    return str(session.get("current_turn_id") or "").strip()


_TURN_PREPARATION_QUARANTINE_REASONS = frozenset(
    {"caller_cancelled", "deadline_expired"}
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


class SessionRepository:
    def __init__(self) -> None:
        settings = load_astrabox_settings()
        self._collection_name = settings.sessions_collection
        self._index_ready = False

    async def ensure_indexes(self) -> None:
        if self._index_ready:
            return
        collection = await get_async_collection(self._collection_name)
        if collection is not None:
            await ensure_unique_index(
                collection,
                "session_id",
                collection_name=self._collection_name,
            )
            try:
                await _safe_create_index(
                    collection,
                    [("user_id", 1), ("deleted", 1), ("updated_at", -1)],
                )
                await _safe_create_index(
                    collection,
                    [("owner_type", 1), ("owner_id", 1), ("deleted", 1)],
                )
                await _safe_create_index(
                    collection,
                    [("user_id", 1), ("hidden", 1), ("deleted", 1), ("updated_at", -1), ("session_id", -1)],
                )
                await _safe_create_index(
                    collection,
                    [("agent_id", 1), ("session_kind", 1), ("deleted", 1), ("updated_at", -1)],
                )
                await _safe_create_index(
                    collection,
                    [
                        ("agent_id", 1),
                        ("session_kind", 1),
                        ("state", 1),
                        ("deleted", 1),
                        ("updated_at", -1),
                        ("session_id", -1),
                    ],
                )
                await _safe_create_index(
                    collection,
                    [("state", 1), ("updated_at", 1)],
                )
                # Resolve a sandbox's persisted backend by its id (control-plane dispatch).
                await _safe_create_index(collection, "sandbox_id")
            except Exception as exc:
                logger.warning("ensure session indexes failed, continue without blocking: %s", exc)
        self._index_ready = True

    async def create_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        await self.ensure_indexes()
        now = utcnow_iso()
        doc = {
            "deleted": False,
            "created_at": now,
            "updated_at": now,
            **payload,
        }
        collection = await get_async_collection(self._collection_name)
        async def _insert() -> Any:
            return await collection.insert_one(doc)

        try:
            await run_mongo_with_retry("sessions.create_session.insert", _insert)
        except DuplicateKeyError:
            # Create uses UUID session_id; duplicate means an uncertain retry write
            # or an extremely unlikely collision. Read-after-write to converge.
            logger.warning("duplicate session_id on create, read existing doc: %s", doc["session_id"])
        except Exception:
            # On uncertain write failures (e.g. network timeout after server applied
            # write), try one read-before-fail to reduce false negatives.
            existing = await run_mongo_with_retry(
                "sessions.read_after_create_error",
                lambda: collection.find_one({"session_id": doc["session_id"]}),
                attempts=1,
            )
            if existing is not None:
                return existing
            raise

        return doc

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        collection = await get_async_collection(self._collection_name)
        return await run_mongo_with_retry(
            "sessions.get_session",
            lambda: collection.find_one({"session_id": session_id, "deleted": {"$ne": True}}),
            fault_context={"session_id": session_id},
        )

    async def get_session_including_deleted(self, session_id: str) -> dict[str, Any] | None:
        """Read one exact id for create-idempotency conflict checks.

        Normal product reads deliberately hide soft-deleted rows.  A caller
        retrying the same create operation must not reuse that identity for a
        different subject after deletion, however, so this narrow repository
        read includes tombstones and stays out of all user-facing read paths.
        """

        collection = await get_async_collection(self._collection_name)
        return await run_mongo_with_retry(
            "sessions.get_session_including_deleted",
            lambda: collection.find_one({"session_id": session_id}),
            fault_context={"session_id": session_id},
        )

    async def get_owned_session(
        self, session_id: str, user_id: str
    ) -> dict[str, Any] | None:
        """Ownership-scoped read — THE user-facing session accessor.

        The owner is part of the query filter, so a wrong-owner lookup returns
        ``None`` atomically (no read-then-compare in the caller, nothing to
        forget). User-facing paths go through this (via
        ``SessionService.must_get_owned_session``); the unscoped
        :meth:`get_session` is for control-plane/worker paths that legitimately
        operate across owners.
        """
        owner = str(user_id or "").strip()
        if not owner:
            return None
        collection = await get_async_collection(self._collection_name)
        return await run_mongo_with_retry(
            "sessions.get_owned_session",
            lambda: collection.find_one(
                {
                    "session_id": session_id,
                    "user_id": owner,
                    "deleted": {"$ne": True},
                }
            ),
            fault_context={"session_id": session_id},
        )

    async def find_session_by_sandbox_id(self, sandbox_id: str) -> dict[str, Any] | None:
        """Find the session row owning a sandbox_id (for control-plane backend lookup).

        A startup allocation is a durable address before the Session publishes
        READY, so it participates in backend resolution too. Deleted or rows
        without backend truth are not valid owners for backend dispatch. If
        multiple historical rows exist, prefer the newest.
        """
        target = str(sandbox_id or "").strip()
        if not target:
            return None
        collection = await get_async_collection(self._collection_name)
        async def _find() -> dict[str, Any] | None:
            cursor = (
                collection.find(
                    {
                        "deleted": {"$ne": True},
                        "$or": [
                            {
                                "sandbox_id": target,
                                "sandbox_backend": {
                                    "$exists": True,
                                    "$ne": "",
                                },
                            },
                            {
                                "startup_allocation.sandbox_id": target,
                                "startup_allocation.sandbox_backend": {
                                    "$exists": True,
                                    "$ne": "",
                                },
                            },
                        ],
                    }
                )
                .sort("updated_at", -1)
                .limit(1)
            )
            async for doc in cursor:
                return doc
            return None

        return await run_mongo_with_retry(
            "sessions.find_by_sandbox_id",
            _find,
            fault_context={"sandbox_id": target},
        )

    async def record_startup_allocation(
        self,
        session_id: str,
        allocation: dict[str, Any],
        *,
        replaces: dict[str, Any] | None = None,
        owner_expected: dict[str, Any] | None = None,
    ) -> bool:
        """Persist the one unadopted sandbox allocation for a Session startup.

        Lifecycle workers serialize startup commands, so a different existing
        allocation is a conflict unless the caller supplies its exact record
        as ``replaces`` after handing off that resource. The compare-and-update
        preserves the last durable address through the ownership transition.
        """

        normalized = dict(allocation)
        target = str(normalized.get("sandbox_id") or "").strip()
        if not target:
            raise ValueError("startup allocation requires sandbox_id")
        current = await self.get_session(session_id)
        if not isinstance(current, dict):
            raise RuntimeError(
                f"cannot record startup allocation: session {session_id!r} is missing"
            )
        if owner_expected and any(current.get(key) != value for key, value in owner_expected.items()):
            raise RuntimeError(f"startup allocation owner changed: {session_id!r}")
        existing = current.get("startup_allocation")
        if isinstance(existing, dict):
            if existing == normalized:
                return True
            if existing != replaces:
                raise RuntimeError(
                    "session already names a different startup allocation "
                    f"session={session_id!r} existing={existing.get('sandbox_id')!r} "
                    f"new={target!r}"
                )
        elif replaces is not None:
            raise RuntimeError(
                f"startup allocation handoff lost its previous record: {session_id!r}"
            )
        expected = {
            **(owner_expected or {}),
            "startup_allocation": (
                current.get("startup_allocation")
                if "startup_allocation" in current
                else {"$exists": False}
            )
        }
        recorded = await self.compare_and_update_session(
            session_id,
            expected=expected,
            updates={"startup_allocation": normalized},
        )
        if recorded:
            return True
        latest = await self.get_session(session_id)
        latest_allocation = (latest or {}).get("startup_allocation")
        if latest_allocation == normalized and all(
            (latest or {}).get(key) == value for key, value in (owner_expected or {}).items()
        ):
            return True
        raise RuntimeError(
            "startup allocation write lost its Session fence "
            f"session={session_id!r} sandbox={target!r}"
        )

    async def retain_startup_allocation(self, session_id: str, record: dict[str, Any]) -> None:
        """Keep exact orphan scope without overwriting the current startup owner."""
        await self._update_retained_startup_allocation(session_id, record, retain=True)

    async def clear_retained_startup_allocation(
        self, session_id: str, record: dict[str, Any]
    ) -> None:
        """Remove one exact orphan receipt after its supplier confirms absence."""
        await self._update_retained_startup_allocation(session_id, record, retain=False)

    async def _update_retained_startup_allocation(
        self, session_id: str, record: dict[str, Any], *, retain: bool,
    ) -> None:
        """CAS the receipt list using the collection's supported $set operation."""
        collection = await get_async_collection(self._collection_name)
        field = "_retained_startup_allocations"
        for _ in range(5):
            current = await self.get_session_including_deleted(session_id)
            if current is None:
                if not retain:
                    return
                raise RuntimeError(f"cannot retain allocation for missing Session {session_id!r}")
            observed = current.get(field)
            if observed is not None and not isinstance(observed, list):
                raise RuntimeError(f"invalid retained allocation list for Session {session_id!r}")
            receipts = list(observed or [])
            if (record in receipts) == retain:
                return
            updated = [*receipts, dict(record)] if retain else [r for r in receipts if r != record]
            result = await run_mongo_with_retry(
                "sessions.update_retained_startup_allocation",
                lambda: collection.update_one(
                    {
                        "session_id": session_id,
                        field: observed if field in current else {"$exists": False},
                    },
                    {"$set": {field: updated}},
                ),
            )
            if bool(getattr(result, "matched_count", 0)):
                return
        raise RuntimeError(
            f"retained allocation list kept changing for Session {session_id!r}"
        )

    async def clear_startup_allocation(
        self,
        session_id: str,
        *,
        allocation: dict[str, Any],
    ) -> bool:
        """Drop the session's reference to one exact released/adopted allocation.

        A different current allocation already satisfies that postcondition and
        must be left untouched. The update itself compares the full record, not
        only the sandbox id, so two isolated allocations in one shared box can
        never clear one another.
        """

        normalized = dict(allocation)
        target = str(normalized.get("sandbox_id") or "").strip()
        if not target:
            raise ValueError("clearing a startup allocation requires sandbox_id")
        current = await self.get_session_including_deleted(session_id)
        if not isinstance(current, dict):
            # The resource is already released when this method is called. A
            # missing Session therefore has no durable allocation left to
            # sever; treating it as a mismatch would turn confirmed cleanup
            # into a false leak and keep only a process-local name alive.
            return True
        current_allocation = current.get("startup_allocation")
        if current_allocation is None:
            return True
        if not isinstance(current_allocation, dict):
            raise RuntimeError(
                f"session {session_id!r} has an invalid startup allocation"
            )
        if current_allocation != normalized:
            return True
        collection = await get_async_collection(self._collection_name)
        result = await run_mongo_with_retry(
            "sessions.clear_startup_allocation",
            lambda: collection.update_one(
                {
                    "session_id": session_id,
                    "startup_allocation": normalized,
                },
                {"$set": {"startup_allocation": None}},
            ),
        )
        if bool(
            getattr(result, "modified_count", 0)
            or getattr(result, "matched_count", 0)
        ):
            return True
        latest = await self.get_session_including_deleted(session_id)
        return (latest or {}).get("startup_allocation") != normalized

    async def list_startup_allocation_candidates(
        self,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return rows with active or retained startup allocations, newest update first.

        The box-occupancy check uses this bounded page to find recent joiners.
        Sorting by descending ``updated_at`` prioritizes them over older
        allocation records awaiting cleanup.
        """

        page_limit = max(1, min(int(limit or 50), 10_000))

        async def _list() -> list[dict[str, Any]]:
            collection = await get_async_collection(self._collection_name)
            cursor = (
                collection.find(
                    {
                        "$or": [
                            {"startup_allocation": {"$type": "object"}},
                            {"_retained_startup_allocations": {"$type": "array", "$ne": []}},
                        ],
                    }
                )
                .sort("updated_at", -1)
                .limit(page_limit)
            )
            return [doc async for doc in cursor]

        return await run_mongo_with_retry(
            "sessions.list_startup_allocation_candidates",
            _list,
        )

    async def list_sessions_by_sandbox_id(self, sandbox_id: str) -> list[dict[str, Any]]:
        """EVERY live session bound to one sandbox, not just the newest.

        A box can serve many conversations — under the shared-sandbox mode by
        design, and incidentally whenever rows outlive their box. When the box
        itself dies, all of them lose their binding at once, so the caller that
        converges on that news needs the whole set: converging only the newest
        would leave the rest pointing at a box that is gone.
        """
        target = str(sandbox_id or "").strip()
        if not target:
            return []
        collection = await get_async_collection(self._collection_name)

        async def _find() -> list[dict[str, Any]]:
            cursor = collection.find(
                {"sandbox_id": target, "deleted": {"$ne": True}}
            ).sort("updated_at", -1)
            return [doc async for doc in cursor]

        return await run_mongo_with_retry(
            "sessions.list_by_sandbox_id",
            _find,
            fault_context={"sandbox_id": target},
        )

    async def list_dead_binding_probe_candidates(
        self,
        *,
        now_iso: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Sessions that may be holding a dead sandbox binding.

        Feeds the expiration watcher's pull backstop of the sandbox-death
        convergence capability: a session still bound to a sandbox past its
        lease with no runtime_unavailable mark is the one whose box may have
        died without a status callback. Lease-active sessions are excluded so
        the probe volume stays bounded to the suspicious set; sessions already
        converged (runtime_unavailable) or terminal never re-enter.
        """
        page_limit = max(1, min(int(limit or 50), 500))
        query = {
            "deleted": {"$ne": True},
            "runtime_unavailable": {"$ne": True},
            "sandbox_id": {"$gt": ""},
            "state": {
                "$nin": [SessionState.TERMINATED.value, SessionState.DELETED.value],
            },
            "$or": [
                {"expires_at": {"$exists": False}},
                {"expires_at": None},
                {"expires_at": {"$lte": now_iso}},
                # An engine transport that detached mid-turn. NOT a conclusion
                # — the box may be alive behind a network blip — only a reason
                # to ask the control plane before its lease lapses.
                {"sandbox_liveness_suspect_at": {"$gt": ""}},
            ],
        }
        collection = await get_async_collection(self._collection_name)

        async def _list() -> list[dict[str, Any]]:
            cursor = collection.find(query).limit(page_limit)
            return [doc async for doc in cursor]

        return await run_mongo_with_retry(
            "sessions.list_dead_binding_probe_candidates",
            _list,
        )

    async def list_idle_reclaim_candidates(
        self,
        *,
        now_iso: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Sessions holding a LIVE sandbox that may have gone quiet.

        The complement of :meth:`list_dead_binding_probe_candidates`: that one
        takes the bindings whose lease has LAPSED, because the box may be dead;
        this one takes the bindings whose lease is still good, because the box is
        up and may be up for nothing. Splitting them on the lease keeps each sweep
        off the other's rows — a box cannot be both suspected dead and reclaimed
        for idleness in the same tick.

        Already-parked sessions are excluded: a parked box is this sweep's
        OUTCOME, not its candidate, and re-parking one would renew a paused
        sandbox, which the control plane answers by failing it.

        Idleness itself is not decided here. How long an agent may sit idle is
        per-agent, and whether the conversation is between turns is a snapshot
        read, so the query only narrows to the rows that could possibly qualify.
        """
        page_limit = max(1, min(int(limit or 50), 500))
        query = {
            "deleted": {"$ne": True},
            "runtime_unavailable": {"$ne": True},
            "sandbox_id": {"$gt": ""},
            "state": {
                "$nin": [SessionState.TERMINATED.value, SessionState.DELETED.value],
            },
            "expires_at": {"$gt": now_iso},
            "$or": [
                {"sandbox_parked_at": {"$exists": False}},
                {"sandbox_parked_at": None},
                {"sandbox_parked_at": ""},
            ],
        }
        collection = await get_async_collection(self._collection_name)

        async def _list() -> list[dict[str, Any]]:
            cursor = collection.find(query).limit(page_limit)
            return [doc async for doc in cursor]

        return await run_mongo_with_retry(
            "sessions.list_idle_reclaim_candidates",
            _list,
        )

    async def find_session_by_share_token(self, token: str) -> dict[str, Any] | None:
        """Find the (non-deleted) session whose share.token matches (capability lookup)."""
        tok = str(token or "").strip()
        if not tok:
            return None
        collection = await get_async_collection(self._collection_name)
        return await run_mongo_with_retry(
            "sessions.find_by_share_token",
            lambda: collection.find_one({"share.token": tok, "deleted": {"$ne": True}}),
        )

    async def list_user_sessions(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        async def _list() -> list[dict[str, Any]]:
            collection = await get_async_collection(self._collection_name)
            cursor = (
                collection.find({
                    "user_id": user_id,
                    "deleted": {"$ne": True},
                    "$or": [{"hidden": {"$exists": False}}, {"hidden": False}],
                })
                .sort("updated_at", -1)
                .limit(limit)
            )
            return [doc async for doc in cursor]

        return await run_mongo_with_retry("sessions.list_user_sessions", _list)

    async def list_user_sessions_page(
        self,
        user_id: str,
        *,
        limit: int = 50,
        cursor: str | None = None,
        projection: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        page_limit = max(1, min(int(limit or 50), 100))
        cursor_data = _decode_session_list_cursor(cursor)

        match_filter: dict[str, Any] = {
            "user_id": user_id,
            "deleted": {"$ne": True},
            "$or": [{"hidden": {"$exists": False}}, {"hidden": False}],
        }
        query: dict[str, Any] = match_filter
        if cursor_data is not None:
            cursor_filter = {
                "$or": [
                    {"updated_at": {"$lt": cursor_data["updated_at"]}},
                    {
                        "updated_at": cursor_data["updated_at"],
                        "session_id": {"$lt": cursor_data["session_id"]},
                    },
                ],
            }
            query = {"$and": [match_filter, cursor_filter]}

        async def _list_page() -> list[dict[str, Any]]:
            collection = await get_async_collection(self._collection_name)
            cursor_obj = (
                collection.find(
                    query,
                    projection=dict(projection) if projection is not None else None,
                )
                # Both keys are ISO-8601 timestamps and UUID strings on every
                # session document, which is what lets the order be evaluated in
                # SQL — and that is what keeps a page of twenty from parsing
                # every session its user owns.
                .sort([("updated_at", -1), ("session_id", -1)], string_keyed=True)
                .limit(page_limit + 1)
            )
            return await collect_async_cursor(cursor_obj)

        rows = await run_mongo_with_retry("sessions.list_user_sessions_page", _list_page)
        has_more = len(rows) > page_limit
        visible_rows = rows[:page_limit]
        next_cursor = None
        if has_more and visible_rows:
            last = visible_rows[-1]
            next_cursor = _encode_session_list_cursor(
                updated_at=str(last.get("updated_at") or ""),
                session_id=str(last.get("session_id") or ""),
            )
        return {
            "sessions": visible_rows,
            "has_more": has_more,
            "next_cursor": next_cursor,
        }

    @staticmethod
    def _admin_session_filter(
        *,
        template_names: list[str] | None,
        agent_id: str | None,
        since: str | None,
        until: str | None,
    ) -> dict[str, Any]:
        """The admin listing's query, built once so list and count cannot diverge.

        Every narrowing belongs HERE rather than in a comprehension over the
        result. A deployment with a hundred agents and a thousand conversations a
        day each has six figures of rows: fetching a page and then filtering it
        returns an arbitrary sliver of one page, with no way to tell how much was
        left behind — and a count taken from that sliver is a number about the
        window, printed where a number about the collection is expected.
        """
        query: dict[str, Any] = {"deleted": {"$ne": True}}
        if template_names is not None:
            query["template_name"] = {"$in": list(template_names)}
        if agent_id:
            query["agent_id"] = agent_id
        # The window is over when a conversation STARTED. `updated_at` moves
        # whenever anything touches the row, so a window over it would pull a
        # months-old session into "yesterday" the moment a sweep looked at it.
        window: dict[str, str] = {}
        if since:
            window["$gte"] = since
        if until:
            window["$lte"] = until
        if window:
            query["created_at"] = window
        return query

    async def list_all_sessions(
        self,
        limit: int = 200,
        *,
        skip: int = 0,
        template_names: list[str] | None = None,
        agent_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[dict[str, Any]]:
        """One page of sessions across all users (admin use), newest first.

        ``template_names`` stores the Agent-name owner scope and is a real query term: passing
        ``[]`` matches nothing, which is the correct answer for a caller who
        administers no agents. ``None`` means unscoped and is only for the
        deliberately global admin-api surface.
        """
        query = self._admin_session_filter(
            template_names=template_names, agent_id=agent_id, since=since, until=until
        )

        async def _list() -> list[dict[str, Any]]:
            collection = await get_async_collection(self._collection_name)
            cursor = collection.find(query).sort("updated_at", -1).skip(skip).limit(limit)
            return [doc async for doc in cursor]

        return await run_mongo_with_retry("sessions.list_all_sessions", _list)

    async def list_bootstrap_reconcile_candidates(
        self,
        *,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        """Return Session rows whose interrupted startup needs reconciliation."""
        page_limit = max(1, min(int(limit or 10_000), 10_000))

        async def _list() -> list[dict[str, Any]]:
            collection = await get_async_collection(self._collection_name)
            cursor = (
                collection.find(
                    {
                        "deleted": {"$ne": True},
                        "state": SessionState.CREATING.value,
                    }
                )
                .sort("updated_at", 1)
                .limit(page_limit)
            )
            return [doc async for doc in cursor]

        return await run_mongo_with_retry(
            "sessions.list_bootstrap_reconcile_candidates",
            _list,
        )

    async def count_session_totals(
        self, *, template_names: list[str] | None = None
    ) -> dict[str, Any]:
        """Count all scoped sessions and their modelled states in one query.

        The aggregation retains unmodelled state buckets long enough to include
        them in ``total`` and then omits them from ``by_state``. This preserves
        the overview contract without nine serial collection scans per poll.
        """
        query = self._admin_session_filter(
            template_names=template_names,
            agent_id=None,
            since=None,
            until=None,
        )
        modelled = {state.value for state in SessionState}

        async def _count() -> dict[str, Any]:
            collection = await get_async_collection(self._collection_name)
            rows = await collect_async_cursor(
                collection.aggregate(
                    [
                        {"$match": query},
                        {"$group": {"_id": "$state", "count": {"$sum": 1}}},
                    ]
                )
            )
            total = 0
            by_state: dict[str, int] = {}
            for row in rows:
                count = int(row.get("count") or 0)
                total += count
                state = row.get("_id")
                if state in modelled and count:
                    by_state[str(state)] = count
            return {"total": total, "by_state": by_state}

        return await run_mongo_with_retry("sessions.count_session_totals", _count)

    async def count_sessions_by_state(
        self, *, template_names: list[str] | None = None
    ) -> dict[str, int]:
        """How many scoped sessions sit in each modelled state."""
        totals = await self.count_session_totals(template_names=template_names)
        return dict(totals["by_state"])

    async def count_all_sessions(
        self,
        *,
        template_names: list[str] | None = None,
        agent_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        state: str | None = None,
    ) -> int:
        """How many sessions match — the whole collection, not the page.

        Counted at the source. The console shows this beside a page of fifty, and
        a total derived from the page would say fifty no matter how many there
        are.
        """
        query = self._admin_session_filter(
            template_names=template_names, agent_id=agent_id, since=since, until=until
        )
        if state:
            query["state"] = state

        async def _count() -> int:
            collection = await get_async_collection(self._collection_name)
            return int(await collection.count_documents(query))

        return await run_mongo_with_retry("sessions.count_all_sessions", _count)

    async def find_sessions_by_template(
        self,
        template_name: str,
        *,
        since: str | None = None,
        until: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Find sessions matching the stored ``template_name`` Agent field and time range."""
        query: dict[str, Any] = {
            "deleted": {"$ne": True},
            "template_name": template_name,
        }
        if since or until:
            created_filter: dict[str, str] = {}
            if since:
                created_filter["$gte"] = since
            if until:
                created_filter["$lte"] = until
            query["created_at"] = created_filter

        async def _find() -> list[dict[str, Any]]:
            collection = await get_async_collection(self._collection_name)
            cursor = (
                collection.find(query)
                .sort("created_at", -1)
                .limit(limit)
            )
            return [doc async for doc in cursor]

        return await run_mongo_with_retry("sessions.find_sessions_by_template", _find)

    async def find_sessions_by_agent_id(
        self,
        agent_id: str,
        *,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Find non-deleted Sessions belonging to one ``agent_id``."""
        query: dict[str, Any] = {
            "deleted": {"$ne": True},
            "agent_id": agent_id,
        }

        async def _find() -> list[dict[str, Any]]:
            collection = await get_async_collection(self._collection_name)
            cursor = (
                collection.find(query)
                .sort("created_at", -1)
                .limit(limit)
            )
            return [doc async for doc in cursor]

        return await run_mongo_with_retry("sessions.find_sessions_by_agent_id", _find)

    async def update_session(
        self,
        session_id: str,
        updates: dict[str, Any],
        *,
        touch_updated_at: bool = True,
    ) -> bool:
        updates = dict(updates)
        if touch_updated_at:
            updates["updated_at"] = utcnow_iso()
        collection = await get_async_collection(self._collection_name)
        result = await run_mongo_with_retry(
            "sessions.update_session",
            lambda: collection.update_one({"session_id": session_id}, {"$set": updates}),
        )
        return bool(getattr(result, "modified_count", 0) or getattr(result, "matched_count", 0))

    async def compare_and_update_session(
        self,
        session_id: str,
        *,
        expected: dict[str, Any],
        updates: dict[str, Any],
        touch_updated_at: bool = True,
    ) -> bool:
        updates = dict(updates)
        if touch_updated_at:
            updates["updated_at"] = utcnow_iso()
        query = {
            "session_id": session_id,
            "deleted": {"$ne": True},
            **dict(expected),
        }
        # Initial startup has no recovery command owner. Both absent and null
        # mean unclaimed here; the collection's equality-to-null is narrower.
        if "_runtime_recovery_owner" in expected and expected["_runtime_recovery_owner"] is None:
            query.pop("_runtime_recovery_owner")
            query["$and"] = [
                *query.get("$and", []),
                {"$or": [
                    {"_runtime_recovery_owner": None},
                    {"_runtime_recovery_owner": {"$exists": False}},
                ]},
            ]
        collection = await get_async_collection(self._collection_name)
        result = await run_mongo_with_retry(
            "sessions.compare_and_update_session",
            lambda: collection.update_one(query, {"$set": updates}),
        )
        return bool(getattr(result, "modified_count", 0) or getattr(result, "matched_count", 0))

    async def mark_interaction(self, session_id: str, interacted_at: str) -> bool:
        timestamp = str(interacted_at or "").strip() or utcnow_iso()
        collection = await get_async_collection(self._collection_name)
        result = await run_mongo_with_retry(
            "sessions.mark_interaction",
            lambda: collection.update_one(
                {"session_id": session_id, "deleted": {"$ne": True}},
                {"$set": {"updated_at": timestamp}},
            ),
        )
        return bool(getattr(result, "modified_count", 0) or getattr(result, "matched_count", 0))

    async def archive_session(self, session_id: str, user_id: str) -> bool:
        now = utcnow_iso()
        return await self.update_session(
            session_id,
            {
                "hidden": True,
                "archived_at": now,
                "archived_by": user_id,
            },
        )

    async def soft_delete(self, session_id: str, user_id: str) -> bool:
        return await self.update_session(
            session_id,
            {
                "deleted": True,
                "state": SessionState.DELETED.value,
                "deleted_at": utcnow_iso(),
                "deleted_by": user_id,
            },
        )

    # There is deliberately NO session-row turn lock here. Per-session turn
    # mutual exclusion across instances is owned by the snapshot layer: a
    # StartTurn is only admitted into the EMPTY slot
    # (apply_channel_update extra_filter={"current_turn_id": None} in
    # _project_command_accepted), and every terminal transition clears the
    # slot. A second session-row lease would create another authority for
    # whether a turn is running and allow the two records to diverge.

    # ── Turn-preparation fencing (no TTL takeover) ──────────────────────

    async def claim_turn_preparation_guard(
        self,
        session_id: str,
        *,
        principal_id: str,
        sandbox_backend: str,
        sandbox_id: str,
        turn_id: str,
        attempt_id: str,
        owner_token: str,
    ) -> dict[str, Any] | None:
        """Claim the durable hook→engine-write guard for one sandbox attempt.

        Unlike an operational lease, this identity-isolation guard has no
        time-based takeover: once claimed, a second coordinator cannot prepare
        or write until the exact private owner releases it.  It is not a
        general turn-execution/checkpoint fence; same-logical-turn recovery is
        owned by the turn checkpoint layer.  A guard belonging to a different
        sandbox id is stale after a durable rebuild and may be replaced
        atomically.
        """
        guard = {
            "state": "ACTIVE",
            "sandbox_backend": sandbox_backend,
            "sandbox_id": sandbox_id,
            "turn_id": turn_id,
            "attempt_id": attempt_id,
            "owner_token": owner_token,
            "acquired_at": utcnow_iso(),
        }
        collection = await get_async_collection(self._collection_name)

        async def _claim() -> dict[str, Any] | None:
            from astrabox.persistence.repository._compat import ReturnDocument

            return await collection.find_one_and_update(
                {
                    "session_id": session_id,
                    "deleted": {"$ne": True},
                    "user_id": principal_id,
                    "sandbox_backend": sandbox_backend,
                    "sandbox_id": sandbox_id,
                    "turn_preparation_guard.sandbox_id": {"$ne": sandbox_id},
                    "current_turn_id": turn_id,
                },
                {
                    "$set": {
                        "turn_preparation_guard": guard,
                        "updated_at": utcnow_iso(),
                    },
                },
                return_document=ReturnDocument.AFTER,
            )

        result = await run_mongo_with_retry(
            "sessions.claim_turn_preparation_guard", _claim
        )
        if not isinstance(result, dict):
            # A transient database error can be raised after the server applied
            # find_one_and_update.  The retry then observes this call's own guard
            # as a conflict and returns None.  Converge only on this coordinator's
            # fresh private owner token before reporting a competing owner; the
            # provider-facing dispatch attempt id may legitimately be replayed.
            result = await run_mongo_with_retry(
                "sessions.claim_turn_preparation_guard.read_after_retry",
                lambda: collection.find_one(
                    {
                        "session_id": session_id,
                        "turn_preparation_guard.sandbox_id": sandbox_id,
                        "turn_preparation_guard.attempt_id": attempt_id,
                        "turn_preparation_guard.owner_token": owner_token,
                        "turn_preparation_guard.state": "ACTIVE",
                    }
                ),
            )
        if not isinstance(result, dict):
            return None
        stored_guard = result.get("turn_preparation_guard")
        if not isinstance(stored_guard, dict):
            return None
        binding_is_current = (
            not bool(result.get("deleted"))
            and result.get("user_id") == principal_id
            and str(result.get("sandbox_backend") or "").strip().lower()
            == sandbox_backend
            and str(result.get("sandbox_id") or "").strip() == sandbox_id
            and _turn_preparation_active_turn_id(result) == turn_id
        )
        if not binding_is_current:
            # An ambiguous first write may have installed this call's guard
            # immediately before the session moved to another turn/binding.  Discover it by
            # the private owner token, then remove only that exact stale guard.
            await self.release_turn_preparation_guard(
                session_id,
                sandbox_id=sandbox_id,
                attempt_id=attempt_id,
                owner_token=owner_token,
                expected_state="ACTIVE",
            )
            return None
        return dict(stored_guard)

    async def quarantine_turn_preparation_guard(
        self,
        session_id: str,
        *,
        sandbox_id: str,
        attempt_id: str,
        owner_token: str,
        reason: str,
    ) -> bool:
        """Mark exactly one ACTIVE attempt as QUARANTINED."""
        normalized_reason = str(reason or "").strip().lower()
        if normalized_reason not in _TURN_PREPARATION_QUARANTINE_REASONS:
            normalized_reason = "abandoned"
        collection = await get_async_collection(self._collection_name)
        result = await run_mongo_with_retry(
            "sessions.quarantine_turn_preparation_guard",
            lambda: collection.update_one(
                {
                    "session_id": session_id,
                    "turn_preparation_guard.sandbox_id": sandbox_id,
                    "turn_preparation_guard.attempt_id": attempt_id,
                    "turn_preparation_guard.owner_token": owner_token,
                    "turn_preparation_guard.state": "ACTIVE",
                },
                {
                    "$set": {
                        "turn_preparation_guard.state": "QUARANTINED",
                        "turn_preparation_guard.reason": normalized_reason,
                        "turn_preparation_guard.quarantined_at": utcnow_iso(),
                        "updated_at": utcnow_iso(),
                    }
                },
            ),
        )
        if bool(result.modified_count):
            return True
        # As above, a retry can see zero modifications after the first write
        # succeeded.  Only exact-attempt durable evidence counts as success.
        current = await run_mongo_with_retry(
            "sessions.quarantine_turn_preparation_guard.read_after_retry",
            lambda: collection.find_one(
                {
                    "session_id": session_id,
                    "turn_preparation_guard.sandbox_id": sandbox_id,
                    "turn_preparation_guard.attempt_id": attempt_id,
                    "turn_preparation_guard.owner_token": owner_token,
                    "turn_preparation_guard.state": "QUARANTINED",
                }
            ),
        )
        return isinstance(current, dict)

    async def release_turn_preparation_guard(
        self,
        session_id: str,
        *,
        sandbox_id: str,
        attempt_id: str,
        owner_token: str,
        expected_state: str,
    ) -> bool:
        """Clear only the exact attempt in the expected state."""
        collection = await get_async_collection(self._collection_name)
        result = await run_mongo_with_retry(
            "sessions.release_turn_preparation_guard",
            lambda: collection.update_one(
                {
                    "session_id": session_id,
                    "turn_preparation_guard.sandbox_id": sandbox_id,
                    "turn_preparation_guard.attempt_id": attempt_id,
                    "turn_preparation_guard.owner_token": owner_token,
                    "turn_preparation_guard.state": expected_state,
                },
                {
                    "$set": {
                        "turn_preparation_guard": None,
                        "updated_at": utcnow_iso(),
                    }
                },
            ),
        )
        if bool(result.modified_count):
            return True
        # Release is also an uncertain-write boundary.  Treat the operation as
        # converged only when the session still exists and has no guard; a newer
        # attempt is never cleared or mistaken for this call's successful release.
        current = await run_mongo_with_retry(
            "sessions.release_turn_preparation_guard.read_after_retry",
            lambda: collection.find_one({"session_id": session_id}),
        )
        if not isinstance(current, dict):
            return False
        current_guard = current.get("turn_preparation_guard")
        if current_guard is None:
            return True
        # A newer or rebuilt guard is safe from this exact-attempt CAS, but it
        # is not evidence that this call released the requested guard.
        return False

    async def clear_pending_interaction(
        self,
        session_id: str,
        *,
        interaction_id: str,
    ) -> bool:
        """Clear pending interaction only when the interaction_id still matches."""
        collection = await get_async_collection(self._collection_name)
        result = await run_mongo_with_retry(
            "sessions.clear_pending_interaction",
            lambda: collection.update_one(
                {
                    "session_id": session_id,
                    "pending_interaction.interaction_id": interaction_id,
                },
                {
                    "$set": {
                        "pending_interaction": None,
                        "updated_at": utcnow_iso(),
                    }
                },
            ),
        )
        return bool(result.modified_count)

    async def set_interaction_answer(
        self,
        session_id: str,
        *,
        interaction_id: str,
        answer: dict[str, Any],
    ) -> bool:
        """Atomically set pending_interaction.answer only if interaction_id matches."""
        collection = await get_async_collection(self._collection_name)
        result = await run_mongo_with_retry(
            "sessions.set_interaction_answer",
            lambda: collection.update_one(
                {
                    "session_id": session_id,
                    "pending_interaction.interaction_id": interaction_id,
                    "pending_interaction.answer": {"$exists": False},
                },
                {
                    "$set": {
                        "pending_interaction.answer": answer,
                        "updated_at": utcnow_iso(),
                    }
                },
            ),
        )
        return bool(getattr(result, "modified_count", 0))

    # ── Epoch fencing (attach-level cross-instance protection) ───────────

    async def try_acquire_attach_lease(
        self,
        session_id: str,
        *,
        machine_id: str,
    ) -> int | None:
        """Atomically increment ``runtime_epoch`` and claim attach ownership.

        Returns the new epoch on success, ``None`` if the session does not exist
        or MongoDB is unavailable.  Uses ``findOneAndUpdate`` to guarantee
        atomicity across machines.
        """
        collection = await get_async_collection(self._collection_name)
        async def _acquire() -> dict | None:
            from astrabox.persistence.repository._compat import ReturnDocument

            return await collection.find_one_and_update(
                {
                    "session_id": session_id,
                    "deleted": {"$ne": True},
                },
                {
                    "$inc": {"runtime_epoch": 1},
                    "$set": {
                        "attach_machine_id": machine_id,
                        "attach_started_at": utcnow_iso(),
                        "updated_at": utcnow_iso(),
                    },
                },
                return_document=ReturnDocument.AFTER,
            )

        result = await run_mongo_with_retry(
            "sessions.try_acquire_attach_lease", _acquire
        )
        if result is None:
            return None
        return int(result.get("runtime_epoch", 0))

    async def update_session_if_epoch(
        self,
        session_id: str,
        epoch: int,
        updates: dict[str, Any],
    ) -> bool:
        """Apply *updates* only if ``runtime_epoch`` still matches *epoch*.

        Returns ``True`` if the update was applied, ``False`` if the epoch was
        stale (another machine has since acquired a newer lease) or the session
        does not exist.
        """
        updates = {**updates, "updated_at": utcnow_iso()}

        collection = await get_async_collection(self._collection_name)
        async def _update() -> Any:
            return await collection.update_one(
                {
                    "session_id": session_id,
                    "runtime_epoch": epoch,
                },
                {"$set": updates},
            )

        result = await run_mongo_with_retry(
            "sessions.update_session_if_epoch", _update
        )
        return bool(getattr(result, "modified_count", 0))

    async def list_sessions_by_owner(
        self, owner_type: str, owner_id: str, *, limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List sessions owned by an agent."""
        collection = await get_async_collection(self._collection_name)
        async def _list() -> list[dict[str, Any]]:
            cursor = (
                collection.find({
                    "owner_type": owner_type,
                    "owner_id": owner_id,
                    "deleted": {"$ne": True},
                })
                .sort("updated_at", -1)
                .limit(limit)
            )
            return [doc async for doc in cursor]

        return await run_mongo_with_retry("sessions.list_sessions_by_owner", _list)

    async def list_sessions_by_agent(
        self,
        agent_id: str,
        *,
        session_kind: str | None = None,
        limit: int = 200,
        skip: int = 0,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {
            "agent_id": agent_id,
            "deleted": {"$ne": True},
        }
        if session_kind is not None:
            query["session_kind"] = session_kind

        collection = await get_async_collection(self._collection_name)

        async def _list() -> list[dict[str, Any]]:
            cursor = (
                collection.find(query)
                .sort([("updated_at", -1), ("session_id", -1)])
                .skip(max(0, int(skip or 0)))
                .limit(limit)
            )
            return [doc async for doc in cursor]

        return await run_mongo_with_retry("sessions.list_sessions_by_agent", _list)

    async def list_agent_sessions_by_state(
        self,
        agent_id: str,
        *,
        session_kind: str,
        state: str,
        limit: int = 200,
        skip: int = 0,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {
            "agent_id": agent_id,
            "session_kind": session_kind,
            "state": state,
            "deleted": {"$ne": True},
        }
        collection = await get_async_collection(self._collection_name)

        async def _list() -> list[dict[str, Any]]:
            cursor = (
                collection.find(query)
                .sort("updated_at", -1)
                .skip(max(0, int(skip or 0)))
                .limit(limit)
            )
            return [doc async for doc in cursor]

        return await run_mongo_with_retry("sessions.list_agent_sessions_by_state", _list)
