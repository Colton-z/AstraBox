"""delete_many compiles a fully-pushable filter into ONE SQL DELETE.

Session cleanup deletes thousands of frame/journal rows by session_id. The
row-by-row ORM path would hold the write lock while loading and deleting every
match, blocking concurrent writers past ``busy_timeout``. A filter made entirely
of top-level string equalities — the same contract the read pushdown pins —
deletes without loading a single row; residual
predicates still verify through matches() and delete in one id-batched
statement.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from astrabox.persistence.repository.sqlite.collection import AsyncCollection


@pytest.fixture()
def db_url(tmp_path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path}/bulk.sqlite"


async def _seed(coll: AsyncCollection) -> None:
    await coll.insert_many(
        [
            {"_id": "a1", "session_id": "s-1", "n": 1},
            {"_id": "a2", "session_id": "s-1", "n": 2},
            {"_id": "b1", "session_id": "s-2", "n": 3},
        ]
    )


async def test_pushable_filter_deletes_without_loading_rows(db_url: str) -> None:
    coll = AsyncCollection("frames", db_url)
    await _seed(coll)

    async def _must_not_load(self, sess, filter):  # noqa: ANN001 - test shim
        raise AssertionError("a fully-pushable delete must not load rows")

    with patch.object(AsyncCollection, "_all_matching_rows", _must_not_load):
        result = await coll.delete_many({"session_id": "s-1"})
    assert result.deleted_count == 2
    remaining = [d["_id"] for d in await coll.find({}).to_list(None)]
    assert remaining == ["b1"]


async def test_residual_filter_still_verifies_through_matches(db_url: str) -> None:
    coll = AsyncCollection("frames", db_url)
    await _seed(coll)
    # `n` is an int — not pushable — so the load-and-verify path must engage
    # and delete exactly the matching row.
    result = await coll.delete_many({"session_id": "s-1", "n": {"$gte": 2}})
    assert result.deleted_count == 1
    remaining = sorted(d["_id"] for d in await coll.find({}).to_list(None))
    assert remaining == ["a1", "b1"]


async def test_collection_isolation_survives_the_sql_path(db_url: str) -> None:
    frames = AsyncCollection("frames", db_url)
    journal = AsyncCollection("journal", db_url)
    await _seed(frames)
    await journal.insert_one({"_id": "j1", "session_id": "s-1"})
    await frames.delete_many({"session_id": "s-1"})
    # The neighbouring collection's rows for the same session are untouched.
    assert await journal.find_one({"_id": "j1"}) is not None
