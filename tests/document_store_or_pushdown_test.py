"""A disjunction can narrow only when every arm does.

The stuck-session scan is three arms over one collection, each naming a state,
together selecting thirty-four rows out of four thousand — and it ran every few
seconds, reading all four thousand each time. A document matching the whole
``$or`` satisfies at least one arm, so the disjunction of one narrowing
condition per arm narrows the read.

The rule's edge is where it earns its keep: an arm with nothing pushable can
match any row, so ORing the other arms beside it would drop the rows only that
arm matches. When that happens the whole predicate must go to Python, and these
pin that it does — a narrowing that loses a row is not a narrowing, it is a
wrong answer that happens to be faster.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from astrabox.persistence.repository.sqlite import collection as collection_module


@pytest.fixture
def store(tmp_path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'docs.db'}"


DOCS: list[dict[str, Any]] = [
    {"_id": "a", "state": "PROCESSING", "phase": "NONE", "beat": "old"},
    {"_id": "b", "state": "PARKED", "phase": "NONE"},
    {"_id": "c", "state": "IDLE", "phase": "TRANSCRIPT_PENDING"},
    {"_id": "d", "state": "IDLE", "phase": "NONE"},
    {"_id": "e", "state": "IDLE", "phase": "NONE", "note": "untouched"},
]

SCAN = {
    "$or": [
        {"state": {"$in": ["PROCESSING", "STREAMING"]}},
        {"state": {"$in": ["PARKED"]}},
        {"phase": "TRANSCRIPT_PENDING"},
    ]
}


async def _seed(url: str) -> Any:
    coll = collection_module.AsyncCollection("session_snapshots", db_url=url)
    for doc in DOCS:
        await coll.insert_one(dict(doc))
    return coll


def test_each_arm_still_contributes_its_rows(store) -> None:
    async def run() -> None:
        coll = await _seed(store)
        found = await coll.find(SCAN).to_list()
        assert sorted(d["_id"] for d in found) == ["a", "b", "c"]

    asyncio.run(run())


def test_an_arm_with_nothing_pushable_disables_the_whole_narrowing(store) -> None:
    """`{"note": {"$exists": True}}` matches rows no other arm names; if the
    first two arms were pushed beside it, that row would never be loaded."""

    async def run() -> None:
        coll = await _seed(store)
        widened = {
            "$or": [
                {"state": {"$in": ["PROCESSING"]}},
                {"note": {"$exists": True}},
            ]
        }
        assert coll._or_pushdown(widened) is None
        found = await coll.find(widened).to_list()
        assert sorted(d["_id"] for d in found) == ["a", "e"]

    asyncio.run(run())


def test_conditions_inside_one_arm_are_required_together(store) -> None:
    """An arm's own keys are ANDed, so a row matching one of them but not the
    other must not come back through that arm."""

    async def run() -> None:
        coll = await _seed(store)
        query = {
            "$or": [
                {"state": "IDLE", "phase": "TRANSCRIPT_PENDING"},
                {"state": {"$in": ["PARKED"]}},
            ]
        }
        found = await coll.find(query).to_list()
        assert sorted(d["_id"] for d in found) == ["b", "c"]

    asyncio.run(run())


def test_a_malformed_or_is_left_alone(store) -> None:
    coll_any = collection_module.AsyncCollection
    inst = coll_any.__new__(coll_any)
    inst._is_postgresql = False  # type: ignore[attr-defined]
    assert inst._or_pushdown({"$or": []}) is None
    assert inst._or_pushdown({"$or": "not-a-list"}) is None
    assert inst._or_pushdown({"$or": [{"state": "X"}, "not-a-dict"]}) is None
