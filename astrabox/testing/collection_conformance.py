"""Reusable conformance suite for ``AsyncDocumentCollection`` implementations.

A persistence plugin (Tier A of :mod:`astrabox.seams.repository`) proves its
collection is a valid substrate for the shared repository classes — including
every turn-lock / lease / epoch-fencing invariant built on them — by running
this suite in its OWN test tree:

.. code-block:: python

    # your_plugin/tests/conformance_test.py
    from astrabox.testing.collection_conformance import CollectionContractSuite

    class TestMyBackendCollectionContract(CollectionContractSuite):
        async def make_collection(self, name):
            return MyCollection(name, self.your_backend_url)

pytest (with pytest-asyncio in auto mode) collects the inherited ``test_*``
methods. The in-tree binding for the SQLite compatibility backend is
``tests/sqlite_collection_conformance_test.py`` — the same invariants, enforced
on the reference implementation on every CI run.

This module imports only the stdlib and ``astrabox.persistence.repository
._compat`` (the exception/sentinel vocabulary) — it must not require pytest at
import time, because it ships inside the package.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from astrabox.persistence.repository._compat import (
    BulkWriteError,
    DuplicateKeyError,
    ReturnDocument,
)

__all__ = ["CollectionContractSuite"]


class CollectionContractSuite:
    """Subclass and implement :meth:`make_collection` to run the contract."""

    async def make_collection(self, name: str) -> Any:
        """Return a fresh, empty collection under test for ``name``.

        Called once per test; distinct names never share documents. The
        returned object must satisfy
        :class:`astrabox.seams.repository.AsyncDocumentCollection`.
        """
        raise NotImplementedError("bind the suite: override make_collection()")

    # ── CAS atomicity — the load-bearing invariant ───────────────────────────

    async def test_cas_claim_has_exactly_one_winner(self) -> None:
        coll = await self.make_collection("conf_locks")
        await coll.insert_one({"_id": "lock", "owner": None})

        async def claim(worker: str):
            return await coll.find_one_and_update(
                {"_id": "lock", "owner": None},
                {"$set": {"owner": worker}},
                return_document=ReturnDocument.AFTER,
            )

        results = await asyncio.gather(*(claim(f"w{i}") for i in range(8)))
        winners = [r for r in results if r is not None]
        assert len(winners) == 1, (
            f"CAS claim admitted {len(winners)} winners (must be exactly 1) — "
            "the backend's find_one_and_update is not an atomic read-modify-write"
        )

    async def test_concurrent_inc_never_collapses(self) -> None:
        coll = await self.make_collection("conf_epochs")
        await coll.insert_one({"_id": "chk", "epoch": 0})

        async def bump() -> int:
            doc = await coll.find_one_and_update(
                {"_id": "chk"},
                {"$inc": {"epoch": 1}},
                return_document=ReturnDocument.AFTER,
            )
            assert doc is not None
            return int(doc["epoch"])

        epochs = await asyncio.gather(*(bump() for _ in range(8)))
        stored = await coll.find_one({"_id": "chk"})
        assert stored is not None and stored["epoch"] == 8, "$inc updates collapsed"
        assert sorted(epochs) == list(range(1, 9)), (
            f"two claimants observed the same epoch: {sorted(epochs)} — "
            "epoch fencing would hand two workers the same lease generation"
        )

    async def test_guarded_update_one_is_atomic(self) -> None:
        coll = await self.make_collection("conf_guards")
        await coll.insert_one({"_id": "g", "state": "idle"})

        async def transition(worker: str) -> bool:
            result = await coll.update_one(
                {"_id": "g", "state": "idle"},
                {"$set": {"state": "busy", "holder": worker}},
            )
            return bool(result.modified_count == 1)

        outcomes = await asyncio.gather(*(transition(f"w{i}") for i in range(8)))
        assert sum(outcomes) == 1, f"guarded update won {sum(outcomes)}×, want 1"

    async def test_top_level_equality_semantics_survive_storage_pushdown(self) -> None:
        """``{field: "x"}`` narrows exactly; ``{field: None}`` matches the
        explicit null only.

        The ``None`` rule is this seam's DELIBERATE divergence from pymongo
        (where null equality also matches a missing field): repos test
        ``{"owner_id": None}`` to mean "explicitly null" — see the sqlite
        backend's ``query._eq``. Pinned here because an implementation may
        push top-level equalities into its storage engine (the sqlite backend
        does, via ``json_extract``, for the hot session-scoped reads), and a
        pushdown must never change what matches in either direction.
        """
        coll = await self.make_collection("conf_eq")
        await coll.insert_one({"_id": "a", "session_id": "s1", "kind": "x"})
        await coll.insert_one({"_id": "b", "session_id": "s2"})
        await coll.insert_one({"_id": "c", "session_id": None})
        await coll.insert_one({"_id": "d"})

        hits = await coll.find({"session_id": "s1"}).to_list(None)
        assert [d["_id"] for d in hits] == ["a"]
        none_hits = await coll.find({"session_id": None}).to_list(None)
        assert [d["_id"] for d in none_hits] == ["c"], (
            "None equality matches the explicit null only (this seam's "
            "documented contract) — a missing field is not a null"
        )
        assert await coll.find_one({"session_id": "s1", "kind": "x"}) is not None
        assert await coll.find_one({"session_id": "s2", "kind": "x"}) is None

    # ── update semantics ─────────────────────────────────────────────────────

    async def test_dotted_set_persists_on_existing_nested_parent(self) -> None:
        coll = await self.make_collection("conf_dotted")
        await coll.insert_one({"_id": "d", "meta": {"phase": "a", "keep": 1}})
        result = await coll.update_one({"_id": "d"}, {"$set": {"meta.phase": "b"}})
        assert result.matched_count == 1 and result.modified_count == 1
        stored = await coll.find_one({"_id": "d"})
        assert stored is not None and stored["meta"] == {"phase": "b", "keep": 1}

    async def test_return_document_before_and_after(self) -> None:
        coll = await self.make_collection("conf_retdoc")
        await coll.insert_one({"_id": "r", "v": 1})
        before = await coll.find_one_and_update(
            {"_id": "r"}, {"$set": {"v": 2}}, return_document=ReturnDocument.BEFORE
        )
        assert before is not None and before["v"] == 1, "BEFORE leaked new values"
        after = await coll.find_one_and_update(
            {"_id": "r"}, {"$set": {"v": 3}}, return_document=ReturnDocument.AFTER
        )
        assert after is not None and after["v"] == 3

    async def test_upsert_seeds_filter_equalities_and_set_on_insert(self) -> None:
        coll = await self.make_collection("conf_upsert")
        await coll.update_one(
            {"key": "k1"},
            {"$set": {"v": 1}, "$setOnInsert": {"created": "yes"}},
            upsert=True,
        )
        doc = await coll.find_one({"key": "k1"})
        assert doc is not None and doc["v"] == 1 and doc["created"] == "yes"
        # second call matches → $setOnInsert must NOT re-apply
        await coll.update_one(
            {"key": "k1"},
            {"$set": {"v": 2}, "$setOnInsert": {"created": "no"}},
            upsert=True,
        )
        doc = await coll.find_one({"key": "k1"})
        assert doc is not None and doc["v"] == 2 and doc["created"] == "yes"

    # ── uniqueness / idempotency vocabulary ──────────────────────────────────

    async def test_unique_index_violation_raises_duplicate_key_error(self) -> None:
        coll = await self.make_collection("conf_unique")
        await coll.create_index([("fence_id", 1)], unique=True, name="ux_fence")
        await coll.insert_one({"fence_id": "f1"})
        try:
            await coll.insert_one({"fence_id": "f1"})
        except DuplicateKeyError:
            pass
        else:
            raise AssertionError(
                "duplicate on a unique index must raise DuplicateKeyError — the "
                "repos' idempotency guards catch exactly that type"
            )

    # ── query semantics ──────────────────────────────────────────────────────

    async def test_operator_matching(self) -> None:
        coll = await self.make_collection("conf_ops")
        await coll.insert_one({"_id": "a", "n": 1, "tag": "x"})
        await coll.insert_one({"_id": "b", "n": 5, "tag": "y"})
        await coll.insert_one({"_id": "c", "n": 9})

        assert (await coll.find_one({"n": {"$lt": 3}}))["_id"] == "a"
        assert (await coll.find_one({"tag": {"$exists": False}}))["_id"] == "c"
        assert (await coll.find_one({"n": {"$in": [5, 6]}}))["_id"] == "b"
        assert (await coll.find_one({"tag": {"$ne": "x"}, "n": {"$gte": 5}}))[
            "_id"
        ] == "b"
        two = [
            d
            async for d in coll.find({"$or": [{"n": {"$lt": 3}}, {"n": {"$gt": 8}}]})
        ]
        assert sorted(d["_id"] for d in two) == ["a", "c"]

    async def test_scalar_equality_matches_an_array_member(self) -> None:
        """Mongo field equality treats an array as a set of candidate values.

        Repository queries use this for managed-runtime references, for example
        ``{"credential_vault_ids": "vlt-a"}``. A backend that compares the
        whole list to the string lets an assigned Vault be deleted because it
        reports no referencing Agent.
        """
        coll = await self.make_collection("conf_array_member_eq")
        await coll.insert_one(
            {"_id": "array-hit", "credential_vault_ids": ["vlt-a", "vlt-b"]}
        )
        await coll.insert_one(
            {"_id": "array-miss", "credential_vault_ids": ["vlt-c"]}
        )
        await coll.insert_one(
            {"_id": "scalar-hit", "credential_vault_ids": "vlt-a"}
        )
        await coll.insert_one({"_id": "missing"})

        hits = await coll.find(
            {"credential_vault_ids": "vlt-a"}
        ).to_list(None)
        assert [doc["_id"] for doc in hits] == ["array-hit", "scalar-hit"]
        assert await coll.count_documents(
            {"credential_vault_ids": "vlt-a"}
        ) == 2

    async def test_natural_order_is_insertion_order(self) -> None:
        coll = await self.make_collection("conf_order")
        for i in range(5):
            await coll.insert_one({"_id": f"i{i}", "i": i})
        docs = [d async for d in coll.find({})]
        assert [d["_id"] for d in docs] == [f"i{i}" for i in range(5)]

    async def test_reads_return_isolated_copies(self) -> None:
        coll = await self.make_collection("conf_copies")
        await coll.insert_one({"_id": "x", "meta": {"a": 1}})
        doc = await coll.find_one({"_id": "x"})
        assert doc is not None
        doc["meta"]["a"] = 999  # mutating the returned doc…
        fresh = await coll.find_one({"_id": "x"})
        assert fresh is not None and fresh["meta"]["a"] == 1, (
            "…must not mutate the store"
        )

    # ── cursor chain / projection / counting ─────────────────────────────────
    #
    # Pagination and projection are where store dialects diverge first — the
    # ``find()`` cursor's ``.sort().skip().limit()`` chain and the Mongo
    # inclusion-projection shape are what every list endpoint is built on
    # (e.g. SessionRepository's page reads and SessionEventRepository's ordered
    # tails), so the contract pins them explicitly.

    async def test_cursor_sort_skip_limit_chain(self) -> None:
        coll = await self.make_collection("conf_cursor_chain")
        for i, score in enumerate((30, 10, 50, 20, 40)):
            await coll.insert_one({"_id": f"d{i}", "score": score})

        docs = [
            d
            async for d in coll.find({})
            .sort([("score", -1)])
            .skip(1)
            .limit(2)
        ]
        assert [d["score"] for d in docs] == [40, 30], (
            "sort desc + skip 1 + limit 2 over scores (10..50) must yield "
            f"[40, 30]; got {[d.get('score') for d in docs]}"
        )

        ascending = [
            d async for d in coll.find({}).sort([("score", 1)]).limit(3)
        ]
        assert [d["score"] for d in ascending] == [10, 20, 30]

    async def test_find_inclusion_projection(self) -> None:
        coll = await self.make_collection("conf_projection")
        await coll.insert_one({"_id": "p1", "keep": "yes", "drop": "no", "n": 1})

        # Inclusion projection: named fields plus _id (present by default).
        docs = [d async for d in coll.find({"_id": "p1"}, {"keep": 1})]
        assert docs and docs[0] == {"_id": "p1", "keep": "yes"}, (
            f"inclusion projection must keep only _id + named fields; got {docs!r}"
        )

        # _id can be opted out explicitly.
        doc = await coll.find_one({"_id": "p1"}, {"keep": 1, "_id": 0})
        assert doc == {"keep": "yes"}, (
            f"projection with _id:0 must drop _id; got {doc!r}"
        )

        # find_one honors the same projection shape as find().
        doc = await coll.find_one({"_id": "p1"}, {"n": 1})
        assert doc == {"_id": "p1", "n": 1}

    async def test_count_documents_with_and_without_filter(self) -> None:
        coll = await self.make_collection("conf_count")
        for i in range(4):
            await coll.insert_one({"_id": f"c{i}", "bucket": "a" if i < 3 else "b"})

        assert await coll.count_documents({}) == 4
        assert await coll.count_documents({"bucket": "a"}) == 3
        assert await coll.count_documents({"bucket": "missing"}) == 0

    # ── aggregate / batch operations ──────────────────────────────────────────
    #
    # These cover the production code paths that use aggregate/insert_many/
    # update_many/delete_many: session paging and overview counts, plus the
    # several repos' batch-write calls.

    @staticmethod
    async def _drain(cursor_or_awaitable: Any) -> list[dict[str, Any]]:
        """Materialise a cursor that may itself need an ``await`` first.

        Mirrors ``astrabox.persistence.repository.backend.collect_async_cursor``
        without importing ``backend`` (this module stays a light, ``_compat``
        -only leaf, per its own docstring): the SQLite shim's ``aggregate()``
        returns an already-iterable cursor synchronously, while a real (async)
        pymongo collection's ``aggregate()`` returns a coroutine that resolves
        to one — a bare ``async for`` over the raw return value would work on
        one backend and raise ``TypeError`` on the other.
        """
        cursor = (
            await cursor_or_awaitable
            if inspect.isawaitable(cursor_or_awaitable)
            else cursor_or_awaitable
        )
        return [doc async for doc in cursor]

    async def test_aggregate_match_sort_limit_pipeline(self) -> None:
        coll = await self.make_collection("conf_aggregate")
        await coll.insert_one({"_id": "a", "owner": "u1", "score": 3})
        await coll.insert_one({"_id": "b", "owner": "u1", "score": 1})
        await coll.insert_one({"_id": "c", "owner": "u1", "score": 2})
        await coll.insert_one({"_id": "d", "owner": "u2", "score": 9})

        # Representative of the tree's one real aggregate call
        # (SessionRepository.list_user_sessions_page): filter, then order,
        # then cap the page.
        pipeline = [
            {"$match": {"owner": "u1"}},
            {"$sort": {"score": -1}},
            {"$limit": 2},
        ]
        docs = await self._drain(coll.aggregate(pipeline))
        assert [d["_id"] for d in docs] == ["a", "c"], (
            f"expected the top-2 u1 docs by descending score (a=3, c=2), got "
            f"{[d['_id'] for d in docs]}"
        )

    async def test_aggregate_groups_scalar_values_and_counts_rows(self) -> None:
        coll = await self.make_collection("conf_aggregate_group")
        await coll.insert_one({"_id": "a", "owner": "u1", "state": "READY"})
        await coll.insert_one({"_id": "b", "owner": "u1", "state": "READY"})
        await coll.insert_one({"_id": "c", "owner": "u1", "state": "BUSY"})
        await coll.insert_one({"_id": "d", "owner": "u2", "state": "READY"})

        docs = await self._drain(
            coll.aggregate(
                [
                    {"$match": {"owner": "u1"}},
                    {"$group": {"_id": "$state", "count": {"$sum": 1}}},
                ]
            )
        )
        assert {row["_id"]: row["count"] for row in docs} == {
            "READY": 2,
            "BUSY": 1,
        }

    async def test_insert_many_ordered_stops_at_first_duplicate(self) -> None:
        coll = await self.make_collection("conf_insert_many")
        await coll.insert_one({"_id": "dup", "v": 0})

        docs = [
            {"_id": "before", "v": 1},
            {"_id": "dup", "v": 2},  # collides with the pre-existing doc
            {"_id": "after", "v": 3},  # ordered=True: must never be attempted
        ]
        try:
            await coll.insert_many(docs, ordered=True)
        except DuplicateKeyError:
            pass  # the sqlite shim's shape for an ordered insert_many duplicate
        except BulkWriteError as exc:
            # the real-pymongo shape for the same condition — the tree's own
            # session_event_repository._is_duplicate_error already treats this
            # and DuplicateKeyError as equivalent "a duplicate happened" signals.
            write_errors = (
                exc.details.get("writeErrors") if isinstance(exc.details, dict) else None
            )
            assert write_errors, (
                "BulkWriteError from an ordered duplicate must carry "
                "details['writeErrors'] — the repos inspect it to confirm a "
                "duplicate (vs. some other batch failure)"
            )
        else:
            raise AssertionError(
                "insert_many(ordered=True) hitting a duplicate key must raise "
                "DuplicateKeyError or BulkWriteError"
            )

        assert (await coll.find_one({"_id": "before"})) is not None, (
            "ordered=True: the doc before the failing one must be committed"
        )
        assert (await coll.find_one({"_id": "after"})) is None, (
            "ordered=True: the doc after the failing one must never be attempted"
        )

    async def test_update_many_updates_every_match(self) -> None:
        coll = await self.make_collection("conf_update_many")
        await coll.insert_one({"_id": "a", "grp": "x", "v": 1})
        await coll.insert_one({"_id": "b", "grp": "x", "v": 1})
        await coll.insert_one({"_id": "c", "grp": "y", "v": 1})

        result = await coll.update_many({"grp": "x"}, {"$set": {"v": 2}})
        assert result.matched_count == 2, f"expected 2 matched, got {result.matched_count}"
        assert result.modified_count == 2, f"expected 2 modified, got {result.modified_count}"

        remaining = {d["_id"]: d["v"] async for d in coll.find({})}
        assert remaining == {"a": 2, "b": 2, "c": 1}, (
            "update_many must apply to every matching doc and leave "
            f"non-matching docs untouched, got {remaining}"
        )

    async def test_delete_many_removes_every_match(self) -> None:
        coll = await self.make_collection("conf_delete_many")
        await coll.insert_one({"_id": "a", "grp": "x"})
        await coll.insert_one({"_id": "b", "grp": "x"})
        await coll.insert_one({"_id": "c", "grp": "y"})

        result = await coll.delete_many({"grp": "x"})
        assert result.deleted_count == 2, f"expected 2 deleted, got {result.deleted_count}"

        remaining = sorted([d["_id"] async for d in coll.find({})])
        assert remaining == ["c"], (
            f"delete_many must remove every matching doc only, got {remaining}"
        )
