"""Durable, sandbox-independent store for claude transcript entries.

Backs the claude_agent_sdk ``SessionStore`` mirror: each row is one JSONL
transcript line (``SessionStoreEntry``) scoped by ``(project_key, session_id,
subpath)`` and ordered by a per-scope monotonic ``seq``. This is the externalized
runtime state the backend reads for recovery without the sandbox, and the SDK
materializes it for ``--resume``. The runner's transport journal has a separate
lifetime and is not the durable transcript store.

Design keeps the Mongo surface minimal (find / insert_many / delete_many; sort
and dedup happen in Python) so the storage is portable and easy to reason about.
Entry payloads are persisted as a JSON string (``entry_json``) so arbitrary
transcript field names round-trip without Mongo field-name constraints — the
SessionStore contract only requires ``json.dumps``/``json.loads`` round-trip
equality, never byte-equality.

Idempotency is per BATCH, keyed by the caller's ``append_id``. A scope document
carries the scope's ``last_sequence`` plus a record of every batch whose sequence
range is allocated but whose rows are not yet all written; the range is claimed
by a compare-and-set on ``last_sequence``, so concurrent appends to one scope
produce a contiguous, gap-free ``1..N``. Re-sending a batch under the same
``append_id`` returns the same ``store_sequence`` and writes nothing new,
whatever the entries carry — a batch's identity is its ``append_id``, never its
content, so the same entries under a NEW ``append_id`` are a second append and
advance the sequence. Sending different entries under an ``append_id`` that is
already committed is a hard error, not a silent overwrite.

Entry-level identity comes from the row ``_id``, derived from
``(scope, append_id, index)``: re-writing a batch is a duplicate-key no-op per
row, which is also how a crash between claiming the range and writing the rows
repairs itself when the batch is re-sent.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid as uuid_mod
from typing import Any

from astrabox.persistence.repository._compat import DuplicateKeyError

from astrabox.persistence.repository.backend import (
    get_async_collection,
    run_mongo_with_retry,
)
from astrabox.common.logger.logger_factory import get_logger

logger = get_logger(__name__)

COLLECTION_NAME = "transcript_entries"

#: Suffix of the companion collection holding one document per SessionStore
#: scope. Separate from the entry rows so the recovery reads, which select on
#: ``platform_session_id`` alone, cannot pick a scope document up as an entry.
SCOPE_COLLECTION_SUFFIX = "_scopes"

# Business turn boundaries and ordering carried by mirrored entries. The
# lifecycle readers use these fields to select a turn's durable transcript.
SANDBOX_TURN_ID_ENTRY_FIELD = "__astrabox_sandbox_turn_id"
PLATFORM_TURN_ID_ENTRY_FIELD = "__astrabox_platform_turn_id"
COMMAND_ID_ENTRY_FIELD = "__astrabox_command_id"
DISPATCH_ID_ENTRY_FIELD = "__astrabox_dispatch_id"
MIRROR_SEQ_ENTRY_FIELD = "__astrabox_mirror_seq"


class TranscriptEntryRepository:
    """Transcript store over the configured database's document collections."""

    def __init__(self, *, collection_name: str = COLLECTION_NAME) -> None:
        self._collection_name = collection_name
        self._scope_collection_name = collection_name + SCOPE_COLLECTION_SUFFIX
        self._indexes_ready = False
        # Strictly-monotonic storage write time (epoch ms) for this instance, so
        # back-to-back appends always get distinct, increasing mtimes — the
        # SessionStore list_sessions/summary staleness contract relies on this.
        self._last_mtime = 0

    def _next_mtime(self) -> int:
        now_ms = int(time.time() * 1000)
        if now_ms <= self._last_mtime:
            now_ms = self._last_mtime + 1
        self._last_mtime = now_ms
        return now_ms

    async def _collection(self) -> Any:
        collection = await get_async_collection(self._collection_name)
        if not self._indexes_ready:
            await self._ensure_indexes(collection)
            self._indexes_ready = True
        return collection

    async def _ensure_indexes(self, collection: Any) -> None:
        try:
            # Resolving a re-sent batch reads the rows it already wrote, so
            # (scope_id, append_id) is on the append path, not just recovery.
            await collection.create_index(
                [("scope_id", 1), ("append_id", 1)],
                name="ix_transcript_scope_append",
            )
            await collection.create_index(
                [("project_key", 1), ("session_id", 1), ("subpath", 1), ("seq", 1)],
                name="ix_transcript_scope_seq",
            )
            # Recovery reads the durable mirror by platform session id (which the
            # platform always has) + the business turn stamp, so it never needs the
            # sandbox-derived project_key nor the (possibly-dead) sandbox WAL.
            await collection.create_index(
                [("platform_session_id", 1), ("seq", 1)],
                name="ix_transcript_platform_seq",
            )
            await collection.create_index(
                [
                    ("platform_session_id", 1),
                    ("platform_turn_id", 1),
                    ("subpath", 1),
                    ("seq", 1),
                ],
                name="ix_transcript_platform_turn_seq",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("transcript index creation failed (non-blocking): %s", exc)

    async def _scope_collection(self) -> Any:
        return await get_async_collection(self._scope_collection_name)

    @staticmethod
    def _scope(project_key: str, session_id: str, subpath: str | None) -> dict[str, Any]:
        return {"project_key": project_key, "session_id": session_id, "subpath": subpath}

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @classmethod
    def _scope_id(
        cls,
        platform_session_id: str | None,
        project_key: str,
        session_id: str,
        subpath: str | None,
    ) -> str:
        """Identity of one sequence space.

        ``platform_session_id`` leads because it is the tenant boundary: it is
        the capability-verified path parameter, whereas project_key / session_id
        / subpath are sandbox-derived values any caller can echo. Leaving it out
        would let one tenant claim sequence numbers in another tenant's scope by
        naming that scope in the request body.
        """
        return cls._digest(
            json.dumps(
                [platform_session_id, project_key, session_id, subpath],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

    @classmethod
    def _row_id(cls, scope_id: str, append_id: str, index: int) -> str:
        return cls._digest(f"{scope_id}:{append_id}:{index}")

    @staticmethod
    def _entries_digest(entries: list[dict[str, Any]]) -> str:
        """Content identity of a batch, order-sensitive but key-order-independent."""
        return hashlib.sha256(
            json.dumps(
                entries, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _normalized_append_id(append_id: str | None) -> str:
        """The batch's identity.

        ``None`` mints one: a caller with no batch identity of its own (a direct
        host-side ``SessionStore.append``, which the SDK contract gives no such
        parameter) is asking for exactly one append. A caller that CAN be
        re-delivered — the in-box spool — always supplies its own.
        """
        if append_id is None:
            return str(uuid_mod.uuid4())
        normalized = str(append_id).strip()
        if not normalized:
            raise ValueError("append_id must be a non-empty string")
        return normalized

    @staticmethod
    def _entry_str_field(entry: dict[str, Any], field: str) -> str | None:
        value = str(entry.get(field) or "").strip()
        return value or None

    @staticmethod
    def _entry_int_field(entry: dict[str, Any], field: str) -> int | None:
        value = entry.get(field)
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return int(value)
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return int(text)
        except (TypeError, ValueError):
            return None

    async def append_entries(
        self,
        project_key: str,
        session_id: str,
        subpath: str | None,
        entries: list[dict[str, Any]],
        *,
        append_id: str | None = None,
        platform_session_id: str | None = None,
    ) -> int:
        """Append one batch; return the scope's ``store_sequence`` after it.

        The sequence is 1-based and counts entries committed to this scope, so
        the first append of k entries returns k. An empty batch returns the
        current sequence without advancing it. Re-sending a batch under an
        ``append_id`` already seen returns the sequence that batch got the first
        time and stores nothing new.

        Raises:
            RuntimeError: ``append_id`` names a batch already stored with
                different entries.
        """
        platform_sid = str(platform_session_id or "").strip() or None
        scope_id = self._scope_id(platform_sid, project_key, session_id, subpath)
        resolved_append_id = self._normalized_append_id(append_id)
        if not entries:
            return await self.current_sequence(
                project_key, session_id, subpath, platform_session_id=platform_sid
            )

        async def _op() -> int:
            collection = await self._collection()
            scope_collection = await self._scope_collection()
            await self._ensure_scope(
                collection,
                scope_collection,
                scope_id=scope_id,
                platform_session_id=platform_sid,
                project_key=project_key,
                session_id=session_id,
                subpath=subpath,
            )
            commit = await self._resolve_or_claim_commit(
                collection,
                scope_collection,
                scope_id=scope_id,
                append_id=resolved_append_id,
                entries=entries,
            )
            # Writing the rows is what makes the claimed range real, so it runs
            # on the re-send path too: a crash between the claim and the rows
            # leaves a claim whose rows the next delivery of this append_id
            # completes, at the sequence the claim already fixed.
            await self._write_batch_rows(
                collection,
                scope_id=scope_id,
                append_id=resolved_append_id,
                first_sequence=int(commit["first_sequence"]),
                entries=entries,
                project_key=project_key,
                session_id=session_id,
                subpath=subpath,
                platform_session_id=platform_sid,
            )
            await self._release_claim(
                scope_collection, scope_id=scope_id, append_id=resolved_append_id
            )
            return int(commit["last_sequence"])

        return await run_mongo_with_retry(f"{self._collection_name}.append", _op)

    async def current_sequence(
        self,
        project_key: str,
        session_id: str,
        subpath: str | None,
        *,
        platform_session_id: str | None = None,
    ) -> int:
        """The scope's committed sequence; 0 for a scope nothing has written."""
        platform_sid = str(platform_session_id or "").strip() or None
        scope_id = self._scope_id(platform_sid, project_key, session_id, subpath)

        async def _op() -> int:
            scope_collection = await self._scope_collection()
            scope_doc = await scope_collection.find_one({"_id": f"scope:{scope_id}"})
            if isinstance(scope_doc, dict):
                return int(scope_doc.get("last_sequence") or 0)
            collection = await self._collection()
            return await self._highest_row_sequence(
                collection,
                project_key=project_key,
                session_id=session_id,
                subpath=subpath,
                platform_session_id=platform_sid,
            )

        return await run_mongo_with_retry(
            f"{self._collection_name}.current_sequence", _op
        )

    @staticmethod
    async def _highest_row_sequence(
        collection: Any,
        *,
        project_key: str,
        session_id: str,
        subpath: str | None,
        platform_session_id: str | None,
    ) -> int:
        """Highest ``seq`` already stored in a scope, or 0 when it holds nothing.

        Read once, when a scope document is first created, to start the counter
        above whatever the rows already hold — the counter describes the rows,
        so it cannot be assumed to start from nothing while rows exist.
        """
        query: dict[str, Any] = {
            "project_key": project_key,
            "session_id": session_id,
            "subpath": subpath,
        }
        if platform_session_id is not None:
            query["platform_session_id"] = platform_session_id
        highest = 0
        cursor = collection.find(query, {"seq": 1}).sort("seq", -1).limit(1)
        async for row in cursor:
            highest = max(highest, int(row.get("seq") or 0))
        return highest

    async def _ensure_scope(
        self,
        collection: Any,
        scope_collection: Any,
        *,
        scope_id: str,
        platform_session_id: str | None,
        project_key: str,
        session_id: str,
        subpath: str | None,
    ) -> None:
        doc_id = f"scope:{scope_id}"
        if isinstance(await scope_collection.find_one({"_id": doc_id}), dict):
            return
        last_sequence = await self._highest_row_sequence(
            collection,
            project_key=project_key,
            session_id=session_id,
            subpath=subpath,
            platform_session_id=platform_session_id,
        )
        try:
            await scope_collection.insert_one(
                {
                    "_id": doc_id,
                    "scope_id": scope_id,
                    "platform_session_id": platform_session_id,
                    "project_key": project_key,
                    "session_id": session_id,
                    "subpath": subpath,
                    "last_sequence": last_sequence,
                    "claimed_batches": {},
                }
            )
        except DuplicateKeyError:
            # Another writer created the scope first; its document is as good
            # as the one this call would have written.
            return

    async def _resolve_or_claim_commit(
        self,
        collection: Any,
        scope_collection: Any,
        *,
        scope_id: str,
        append_id: str,
        entries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Return this batch's sequence range, claiming one if it has none yet.

        Three answers, in the order they are asked. An outstanding claim names a
        batch whose range is fixed but whose rows may be incomplete. Rows already
        written are the durable record of a batch whose claim was released. Only
        a batch with neither takes a new range, under a compare-and-set on the
        scope's ``last_sequence`` so two writers cannot claim the same numbers.
        """
        doc_id = f"scope:{scope_id}"
        entries_sha256 = self._entries_digest(entries)
        claim_key = self._digest(append_id)
        while True:
            scope_doc = await scope_collection.find_one({"_id": doc_id})
            if not isinstance(scope_doc, dict):
                raise RuntimeError(
                    f"transcript scope {scope_id} vanished while appending batch "
                    f"{append_id}"
                )
            claimed = scope_doc.get("claimed_batches")
            if not isinstance(claimed, dict):
                raise RuntimeError(
                    f"transcript scope {scope_id} has a malformed claim ledger"
                )
            outstanding = claimed.get(claim_key)
            if isinstance(outstanding, dict):
                self._assert_same_batch(
                    outstanding,
                    scope_id=scope_id,
                    append_id=append_id,
                    entry_count=len(entries),
                    entries_sha256=entries_sha256,
                )
                return outstanding
            settled = await self._settled_commit(
                collection,
                scope_id=scope_id,
                append_id=append_id,
                entries=entries,
            )
            if settled is not None:
                return settled
            previous = int(scope_doc.get("last_sequence") or 0)
            commit = {
                "append_id": append_id,
                "entry_count": len(entries),
                "entries_sha256": entries_sha256,
                "first_sequence": previous + 1,
                "last_sequence": previous + len(entries),
            }
            result = await scope_collection.update_one(
                {"_id": doc_id, "last_sequence": previous},
                {
                    "$inc": {"last_sequence": len(entries)},
                    "$set": {f"claimed_batches.{claim_key}": commit},
                },
            )
            if int(getattr(result, "modified_count", 0) or 0) == 1:
                return commit
            # Another writer moved last_sequence between the read and the
            # compare-and-set. Re-read and claim above the range it took.

    @staticmethod
    def _assert_same_batch(
        commit: dict[str, Any],
        *,
        scope_id: str,
        append_id: str,
        entry_count: int,
        entries_sha256: str,
    ) -> None:
        if (
            commit.get("append_id") != append_id
            or int(commit.get("entry_count") or -1) != entry_count
            or commit.get("entries_sha256") != entries_sha256
        ):
            raise RuntimeError(
                f"transcript append_id {append_id} in scope {scope_id} already names "
                "a different batch; an append_id identifies one batch of entries "
                "and cannot be reused for another"
            )

    async def _settled_commit(
        self,
        collection: Any,
        *,
        scope_id: str,
        append_id: str,
        entries: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """The sequence range of a batch whose rows are all stored, else None."""
        cursor = collection.find(
            {"scope_id": scope_id, "append_id": append_id},
            {"seq": 1, "batch_index": 1, "entry_json": 1},
        )
        rows = [row async for row in cursor]
        if not rows:
            return None
        if len(rows) != len(entries):
            raise RuntimeError(
                f"transcript append_id {append_id} in scope {scope_id} already stored "
                f"{len(rows)} entries; this delivery carries {len(entries)}"
            )
        by_index = {int(row.get("batch_index") or 0): row for row in rows}
        for index, entry in enumerate(entries):
            row = by_index.get(index)
            if row is None or json.loads(row["entry_json"]) != entry:
                raise RuntimeError(
                    f"transcript append_id {append_id} in scope {scope_id} already "
                    f"names a different batch (entry {index} differs)"
                )
        sequences = [int(row.get("seq") or 0) for row in rows]
        first_sequence = min(sequences)
        last_sequence = max(sequences)
        if last_sequence - first_sequence + 1 != len(rows):
            raise RuntimeError(
                f"transcript batch {append_id} in scope {scope_id} holds a "
                "non-contiguous sequence range"
            )
        return {
            "append_id": append_id,
            "entry_count": len(rows),
            "entries_sha256": self._entries_digest(entries),
            "first_sequence": first_sequence,
            "last_sequence": last_sequence,
        }

    async def _write_batch_rows(
        self,
        collection: Any,
        *,
        scope_id: str,
        append_id: str,
        first_sequence: int,
        entries: list[dict[str, Any]],
        project_key: str,
        session_id: str,
        subpath: str | None,
        platform_session_id: str | None,
    ) -> None:
        now_ms = self._next_mtime()
        for index, entry in enumerate(entries):
            doc = {
                "_id": self._row_id(scope_id, append_id, index),
                "scope_id": scope_id,
                "append_id": append_id,
                "batch_index": index,
                "project_key": project_key,
                "session_id": session_id,
                "subpath": subpath,
                "platform_session_id": platform_session_id,
                "platform_turn_id": self._entry_str_field(
                    entry, PLATFORM_TURN_ID_ENTRY_FIELD
                ),
                "command_id": self._entry_str_field(entry, COMMAND_ID_ENTRY_FIELD),
                "dispatch_id": self._entry_str_field(entry, DISPATCH_ID_ENTRY_FIELD),
                "sandbox_turn_id": self._entry_int_field(
                    entry, SANDBOX_TURN_ID_ENTRY_FIELD
                ),
                "seq": first_sequence + index,
                "uuid": entry.get("uuid") if isinstance(entry.get("uuid"), str) else None,
                "entry_json": json.dumps(entry, ensure_ascii=False),
                "mtime": now_ms,
            }
            try:
                await collection.insert_one(doc)
            except DuplicateKeyError:
                # This row of this batch is already stored. The claim resolution
                # above proved the batch is the same one, so the stored row is
                # this row.
                continue

    async def _release_claim(
        self, scope_collection: Any, *, scope_id: str, append_id: str
    ) -> None:
        """Drop a claim once its rows are stored, so the ledger stays bounded.

        The ledger only has to cover batches whose rows might still be missing,
        which is the writers in flight — not the scope's whole history. Keeping
        every batch forever would grow the scope document without limit and
        rewrite all of it on each append.
        """
        doc_id = f"scope:{scope_id}"
        claim_key = self._digest(append_id)
        while True:
            scope_doc = await scope_collection.find_one({"_id": doc_id})
            if not isinstance(scope_doc, dict):
                return
            claimed = scope_doc.get("claimed_batches")
            if not isinstance(claimed, dict) or claim_key not in claimed:
                return
            remaining = {
                key: value for key, value in claimed.items() if key != claim_key
            }
            result = await scope_collection.update_one(
                {"_id": doc_id, "claimed_batches": claimed},
                {"$set": {"claimed_batches": remaining}},
            )
            if int(getattr(result, "modified_count", 0) or 0) == 1:
                return

    async def load_entries(
        self,
        project_key: str,
        session_id: str,
        subpath: str | None,
        *,
        platform_session_id: str | None = None,
    ) -> list[dict[str, Any]] | None:
        scope = self._scope(project_key, session_id, subpath)
        # Tenant fence: when a caller is authorized only for one platform
        # session (the capability-token path), the sandbox-derived body key
        # (project_key / session_id) is INTERSECTED with that platform session,
        # so a valid token for session X cannot read another tenant's transcript
        # by putting the victim's project_key in the body. The recovery path
        # already treats platform_session_id as the trusted scope for the same
        # reason.
        psid = str(platform_session_id or "").strip()
        if psid:
            scope = {**scope, "platform_session_id": psid}

        async def _op() -> list[dict[str, Any]]:
            collection = await self._collection()
            cursor = collection.find(scope)
            return [row async for row in cursor]

        rows = await run_mongo_with_retry(f"{self._collection_name}.load", _op)
        if not rows:
            return None
        rows.sort(key=lambda r: int(r.get("seq", 0)))
        return [json.loads(r["entry_json"]) for r in rows]

    async def load_all_entries_by_platform_session(
        self, platform_session_id: str
    ) -> list[dict[str, Any]]:
        """Export read: return ALL raw SDK transcript entries for a session from the durable
        mirror, in append (seq) order, keyed by the platform session id. Unlike
        ``load_turn_entries_by_platform_session`` this is not turn-sliced — it is the whole
        conversation transcript. The platform-only ``__astrabox_sandbox_turn_id`` stamp is stripped
        so the returned entries are clean CLI JSONL lines. Empty list when the mirror has
        nothing for the session. This is how agent_chat transcripts (whose durable truth is
        the mirror, not a network-storage .claude/*.jsonl) are materialized for batch JSONL export.
        """
        sid = str(platform_session_id or "").strip()
        if not sid:
            return []

        async def _op() -> list[dict[str, Any]]:
            collection = await self._collection()
            cursor = collection.find({"platform_session_id": sid})
            return [row async for row in cursor]

        rows = await run_mongo_with_retry(
            f"{self._collection_name}.load_all_by_platform", _op
        )
        if not rows:
            return []
        rows.sort(key=lambda r: int(r.get("seq", 0)))
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                entry = json.loads(r["entry_json"])
            except (KeyError, TypeError, ValueError):
                continue
            if not isinstance(entry, dict):
                continue
            entry.pop(SANDBOX_TURN_ID_ENTRY_FIELD, None)
            entry.pop(PLATFORM_TURN_ID_ENTRY_FIELD, None)
            entry.pop(COMMAND_ID_ENTRY_FIELD, None)
            entry.pop(DISPATCH_ID_ENTRY_FIELD, None)
            entry.pop(MIRROR_SEQ_ENTRY_FIELD, None)
            out.append(entry)
        return out

    async def load_subpath_entries_by_platform_session(
        self,
        platform_session_id: str,
        *,
        subpath: str | None,
    ) -> list[dict[str, Any]]:
        """Return one exact SessionStore scope by the platform session id."""
        sid = str(platform_session_id or "").strip()
        normalized_subpath = (
            str(subpath or "").strip() if subpath is not None else None
        )
        if not sid or (subpath is not None and not normalized_subpath):
            return []

        async def _op() -> list[dict[str, Any]]:
            collection = await self._collection()
            cursor = collection.find(
                {
                    "platform_session_id": sid,
                    "subpath": normalized_subpath,
                }
            )
            return [row async for row in cursor]

        rows = await run_mongo_with_retry(
            f"{self._collection_name}.load_subpath_by_platform",
            _op,
        )
        rows.sort(key=lambda row: int(row.get("seq", 0)))
        entries: list[dict[str, Any]] = []
        for row in rows:
            try:
                entry = json.loads(row["entry_json"])
            except (KeyError, TypeError, ValueError):
                continue
            if isinstance(entry, dict):
                entries.append(entry)
        return entries

    async def list_scopes_by_platform_session(
        self, platform_session_id: str
    ) -> list[dict[str, Any]]:
        """Every SessionStore scope the mirror holds for one platform session.

        The export read, and deliberately NOT
        :meth:`load_all_entries_by_platform_session`. A platform session's
        transcript is not one file: the main conversation and each subagent are
        separate SessionStore keys, and the SDK writes them as separate JSONL
        files (``<session-id>.jsonl`` and ``subagents/agent-<id>.jsonl``).
        Flattening every scope into one seq-ordered list — which that method
        does, because recovery wants exactly that — interleaves a subagent's
        lines into the main transcript, producing a file ``claude --resume``
        cannot read.

        Ordered by first appearance, so the main scope (``subpath`` None) leads.
        """
        sid = str(platform_session_id or "").strip()
        if not sid:
            return []

        async def _op() -> list[dict[str, Any]]:
            collection = await self._collection()
            cursor = collection.find(
                {"platform_session_id": sid},
                {"project_key": 1, "session_id": 1, "subpath": 1, "seq": 1},
            )
            return [row async for row in cursor]

        rows = await run_mongo_with_retry(
            f"{self._collection_name}.list_scopes_by_platform", _op
        )
        seen: set[tuple[str, str, str | None]] = set()
        scopes: list[dict[str, Any]] = []
        for row in sorted(rows, key=lambda r: int(r.get("seq", 0))):
            project_key = str(row.get("project_key") or "")
            session_id = str(row.get("session_id") or "")
            subpath = row.get("subpath")
            subpath = subpath if isinstance(subpath, str) and subpath else None
            if not project_key or not session_id:
                continue
            key = (project_key, session_id, subpath)
            if key in seen:
                continue
            seen.add(key)
            scopes.append(
                {"project_key": project_key, "session_id": session_id, "subpath": subpath}
            )
        return scopes

    async def load_turn_entries_by_platform_session(
        self, platform_session_id: str, sandbox_turn_id: int
    ) -> list[dict[str, Any]]:
        """Recovery read: return the raw SDK transcript entries for one turn of a session
        from the durable mirror, sliced by the business turn stamp. Keyed by the platform
        session id (always known to the platform) so recovery never needs the
        sandbox-derived project_key nor the in-sandbox WAL. Returns the entries in append
        order; empty list when the mirror has nothing for that turn (best-effort mirror may
        not have landed — the caller treats this as 'not recoverable from mirror').
        """
        sid = str(platform_session_id or "").strip()
        if not sid:
            return []
        target_turn = int(sandbox_turn_id)

        async def _op() -> list[dict[str, Any]]:
            collection = await self._collection()
            cursor = collection.find({"platform_session_id": sid})
            return [row async for row in cursor]

        rows = await run_mongo_with_retry(
            f"{self._collection_name}.load_turn_by_platform", _op
        )
        if not rows:
            return []
        rows.sort(key=lambda r: int(r.get("seq", 0)))
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                entry = json.loads(r["entry_json"])
            except (KeyError, TypeError, ValueError):
                continue
            if not isinstance(entry, dict):
                continue
            if entry.get(SANDBOX_TURN_ID_ENTRY_FIELD) == target_turn:
                entry[MIRROR_SEQ_ENTRY_FIELD] = int(r.get("seq", 0))
                out.append(entry)
        return out

    async def load_recovery_entries_by_platform_session(
        self, platform_session_id: str
    ) -> list[dict[str, Any]]:
        """Recovery read: ALL of a session's mirror entries, mirror-seq stamped.

        Turn recovery slices the tail by the Agent program's prompt user entry;
        see ``slice_turn_tail_entries``. Mirror entries do not carry a reliable
        ``platform_turn_id``, so this reads the whole session. ``seq`` is exposed
        as ``__astrabox_mirror_seq`` so the recovered projection can record how
        far the mirror advanced.
        """
        sid = str(platform_session_id or "").strip()
        if not sid:
            return []

        async def _op() -> list[dict[str, Any]]:
            collection = await self._collection()
            cursor = collection.find({"platform_session_id": sid})
            return [row async for row in cursor]

        rows = await run_mongo_with_retry(
            f"{self._collection_name}.load_recovery_by_platform", _op
        )
        if not rows:
            return []
        rows.sort(key=lambda r: int(r.get("seq", 0)))
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                entry = json.loads(r["entry_json"])
            except (KeyError, TypeError, ValueError):
                continue
            if not isinstance(entry, dict):
                continue
            entry[MIRROR_SEQ_ENTRY_FIELD] = int(r.get("seq", 0))
            out.append(entry)
        return out

    async def list_sessions(
        self, project_key: str, *, platform_session_id: str | None = None
    ) -> list[dict[str, Any]]:
        # Tenant fence (see load_entries): when authorized only for one platform
        # session, intersect it so a valid token cannot ENUMERATE another
        # tenant's sessions by supplying the victim's project_key.
        query: dict[str, Any] = {"project_key": project_key, "subpath": None}
        psid = str(platform_session_id or "").strip()
        if psid:
            query["platform_session_id"] = psid

        async def _op() -> list[dict[str, Any]]:
            collection = await self._collection()
            # Main transcripts only — subagent subpaths must not appear here.
            cursor = collection.find(query, {"session_id": 1, "mtime": 1})
            return [row async for row in cursor]

        rows = await run_mongo_with_retry(f"{self._collection_name}.list_sessions", _op)
        by_session: dict[str, int] = {}
        for row in rows:
            sid = row.get("session_id")
            if not isinstance(sid, str):
                continue
            mtime = int(row.get("mtime", 0))
            if sid not in by_session or mtime > by_session[sid]:
                by_session[sid] = mtime
        return [{"session_id": sid, "mtime": mt} for sid, mt in by_session.items()]

    async def delete(self, project_key: str, session_id: str, subpath: str | None) -> None:
        async def _op() -> None:
            collection = await self._collection()
            scope_collection = await self._scope_collection()
            if subpath is None:
                # Delete the main transcript and cascade to all its subkeys
                # (subagent transcripts), but not other sessions / projects.
                query: dict[str, Any] = {
                    "project_key": project_key,
                    "session_id": session_id,
                }
            else:
                query = self._scope(project_key, session_id, subpath)
            await collection.delete_many(query)
            # The scope document counts rows, so it goes with them: leaving it
            # behind would have the next append claim sequence numbers above
            # entries this call just removed.
            await scope_collection.delete_many(query)

        await run_mongo_with_retry(f"{self._collection_name}.delete", _op)

    async def list_subkeys(
        self,
        project_key: str,
        session_id: str,
        *,
        platform_session_id: str | None = None,
    ) -> list[str]:
        query: dict[str, Any] = {
            "project_key": project_key,
            "session_id": session_id,
            "subpath": {"$ne": None},
        }
        psid = str(platform_session_id or "").strip()
        if psid:
            query["platform_session_id"] = psid

        async def _op() -> list[dict[str, Any]]:
            collection = await self._collection()
            cursor = collection.find(
                query,
                {"subpath": 1, "seq": 1},
            )
            return [row async for row in cursor]

        rows = await run_mongo_with_retry(f"{self._collection_name}.list_subkeys", _op)
        seen: set[str] = set()
        subpaths: list[str] = []
        for row in sorted(rows, key=lambda r: int(r.get("seq", 0))):
            sp = row.get("subpath")
            if isinstance(sp, str) and sp not in seen:
                seen.add(sp)
                subpaths.append(sp)
        return subpaths

    async def delete_all_in_collection(self) -> None:
        """Test helper — wipe the (test) collection."""
        collection = await self._collection()
        await collection.delete_many({})
        scope_collection = await self._scope_collection()
        await scope_collection.delete_many({})
