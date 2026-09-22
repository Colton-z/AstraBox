"""Unique-index enforcement is O(1) per write, not O(collection size).

``create_index(..., unique=True)`` builds a real SQLite ``UNIQUE``
expression index (partial-scoped to the owning collection — see
``sqlite/collection.py``'s ``_create_sqlite_expression_index``), and SQL's own
NULL-is-never-equal rule already gives an absent keyed field the same
"not constrained" treatment a plain unique/sparse index needs. A spec with no
``partialFilterExpression`` is therefore enforced by that SQL index alone (its
``IntegrityError`` translated to ``DuplicateKeyError`` at every write call
site). The Python scan remains only for the shape SQL cannot express: a
spec that DOES carry a ``partialFilterExpression`` (``transcript_entries``'
``ux_transcript_scope_uuid`` is the one real example in the tree).

This file pins:
* the O(1)-not-O(n) cost property for a plain unique index;
* duplicate-key correctness for a plain unique index;
* the Python path for a partial-filter index still honours Mongo's
  partial semantics (missing/excluded docs are exempt; matching docs collide);
* the physical ``astrabox_documents`` table is shared by every collection, so
  the SQL index's per-collection ``WHERE`` scoping must not leak a "unique"
  value across two different collections.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import event

from astrabox.persistence.repository._compat import DuplicateKeyError
from astrabox.persistence.repository.sqlite.collection import AsyncCollection
from astrabox.persistence.repository.sqlite.engine import get_engine


@pytest.fixture()
def db_url(tmp_path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path}/unique_hot_path.sqlite"


async def test_plain_unique_index_insert_cost_does_not_grow_with_collection_size(
    db_url: str,
) -> None:
    coll = AsyncCollection("hot_path_messages", db_url)
    await coll.create_index(
        [("session_id", 1), ("turn_id", 1)], unique=True, name="ux_scope"
    )

    engine = get_engine(db_url, mode="write")
    statements: list[str] = []

    def _record(conn: Any, cursor: Any, statement: str, *_a: Any, **_k: Any) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        statements.clear()
        await coll.insert_one({"session_id": "s-first", "turn_id": "t0"})
        cost_on_empty_collection = len(statements)

        # Grow the collection well past a handful of rows.
        for i in range(300):
            await coll.insert_one({"session_id": f"s{i}", "turn_id": "t0"})

        statements.clear()
        await coll.insert_one({"session_id": "s-last", "turn_id": "t0"})
        cost_with_300_existing_docs = len(statements)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)

    assert cost_with_300_existing_docs == cost_on_empty_collection, (
        f"insert_one issued {cost_on_empty_collection} statement(s) against an "
        f"empty collection but {cost_with_300_existing_docs} once it held 300 "
        "docs -- a plain unique index must be enforced by the SQL index (O(1)), "
        "not by a Python scan of the whole collection per write"
    )
    # And no full-collection read snuck in disguised as a same-count-but-heavier
    # statement: every statement text must be free of the tell-tale "scan every
    # doc in this collection" WHERE shape (collection = ? with no doc_id/other
    # predicate narrowing it further).
    assert not any(
        "astrabox_documents.doc" in s and "doc_id" not in s for s in statements
    ), f"a collection-wide doc scan reappeared: {statements!r}"


async def test_plain_unique_index_still_raises_duplicate_key_error(db_url: str) -> None:
    coll = AsyncCollection("hot_path_dupes", db_url)
    await coll.create_index(
        [("session_id", 1), ("turn_id", 1)], unique=True, name="ux_scope"
    )
    await coll.insert_one({"session_id": "s1", "turn_id": "t1"})
    with pytest.raises(DuplicateKeyError):
        await coll.insert_one({"session_id": "s1", "turn_id": "t1"})
    # A field left out of the unique key never collides.
    await coll.insert_one({"session_id": "s1", "turn_id": "t2"})


async def test_plain_unique_index_missing_field_is_not_constrained(db_url: str) -> None:
    """SQL NULL-is-never-equal must give the same "absent -> exempt" treatment
    declared by a sparse unique index."""
    coll = AsyncCollection("hot_path_sparse", db_url)
    await coll.create_index([("idempotency_key", 1)], unique=True, sparse=True, name="ux_idem")
    await coll.insert_one({"other": 1})
    await coll.insert_one({"other": 2})  # both omit idempotency_key -> must not collide
    await coll.insert_one({"idempotency_key": "k1"})
    with pytest.raises(DuplicateKeyError):
        await coll.insert_one({"idempotency_key": "k1"})


async def test_partial_filter_unique_index_still_enforced_in_python(db_url: str) -> None:
    """The one shape SQL cannot express (transcript_entries' uuid partial index)."""
    coll = AsyncCollection("hot_path_transcript_like", db_url)
    await coll.create_index(
        [("session_id", 1), ("uuid", 1)],
        unique=True,
        name="ux_transcript_scope_uuid",
        partialFilterExpression={"uuid": {"$type": "string"}},
    )

    # Entries without a uuid (titles/tags/mode markers) always append.
    await coll.insert_one({"session_id": "s1", "uuid": None, "text": "title"})
    await coll.insert_one({"session_id": "s1", "uuid": None, "text": "tag"})

    await coll.insert_one({"session_id": "s1", "uuid": "u-1", "text": "msg1"})
    with pytest.raises(DuplicateKeyError):
        await coll.insert_one({"session_id": "s1", "uuid": "u-1", "text": "retry"})

    # Same uuid, different scope (session_id) -> not a duplicate.
    await coll.insert_one({"session_id": "s2", "uuid": "u-1", "text": "other-session"})

    assert await coll.count_documents({}) == 4


async def test_unique_value_does_not_collide_across_different_collections(
    db_url: str,
) -> None:
    """All collections share one physical table; a per-collection unique index
    must not turn into a cross-collection one (see _create_sqlite_expression_index's
    WHERE-scoping, as opposed to a leading `collection` key column).

    The default generated name gives each collection its own physical SQLite
    index. Reusing one explicit ``name=`` across collections would instead let
    ``CREATE UNIQUE INDEX IF NOT EXISTS`` keep the first collection's scoped
    definition; no in-tree caller does that.
    """
    coll_a = AsyncCollection("scope_a", db_url)
    coll_b = AsyncCollection("scope_b", db_url)
    await coll_a.create_index([("fence_id", 1)], unique=True)
    await coll_b.create_index([("fence_id", 1)], unique=True)

    await coll_a.insert_one({"fence_id": "shared-value"})
    # Same value, different collection -> must succeed.
    await coll_b.insert_one({"fence_id": "shared-value"})
    # But within coll_b it is still enforced.
    with pytest.raises(DuplicateKeyError):
        await coll_b.insert_one({"fence_id": "shared-value"})
