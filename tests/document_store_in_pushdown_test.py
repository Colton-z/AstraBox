"""Reading a batch by id must not read the collection.

``$in`` was left to the Python matcher, so a query naming fifty ids against a
collection of four thousand documents loaded all four thousand and kept the
fifty — and the two costs land together, once in the database returning the
rows and once in this process parsing them into dicts to reject. The price
grows with stored history rather than with the answer, which is why it stays
invisible until concurrency makes it the node's largest consumer.

Narrowing it may not change what the query means. Mongo's ``$in`` matches a
scalar equal to any element AND an array containing any element, and a list
holding a non-string element must stay in Python entirely rather than be
narrowed on its strings alone — narrowing on half a disjunction drops the rows
the other half matches. These pin the meaning; the last one pins that SQL was
actually given the predicate, because behaviour alone cannot tell a narrowed
read from one the matcher rescued.
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
    {"_id": "s1", "session_id": "s1", "tags": ["red", "blue"]},
    {"_id": "s2", "session_id": "s2", "tags": ["green"]},
    {"_id": "s3", "session_id": "s3", "tags": []},
    {"_id": "s4", "session_id": "s4", "tags": "red"},
    {"_id": "s5", "session_id": "s5"},
]


async def _seed(url: str) -> Any:
    coll = collection_module.AsyncCollection("session_snapshots", db_url=url)
    for doc in DOCS:
        await coll.insert_one(dict(doc))
    return coll


def test_in_selects_exactly_the_named_documents(store) -> None:
    async def run() -> None:
        coll = await _seed(store)
        found = await coll.find({"session_id": {"$in": ["s2", "s4"]}}).to_list()
        assert sorted(d["session_id"] for d in found) == ["s2", "s4"]

        assert await coll.find({"session_id": {"$in": ["absent"]}}).to_list() == []

    asyncio.run(run())


def test_in_still_matches_an_array_that_contains_a_member(store) -> None:
    """The same rule the scalar pushdown obeys: {field: "x"} finds the array
    holding "x", so {field: {"$in": ["x"]}} has to as well."""

    async def run() -> None:
        coll = await _seed(store)
        found = await coll.find({"tags": {"$in": ["red"]}}).to_list()
        assert sorted(d["session_id"] for d in found) == ["s1", "s4"]

        found = await coll.find({"tags": {"$in": ["green", "red"]}}).to_list()
        assert sorted(d["session_id"] for d in found) == ["s1", "s2", "s4"]

        # An empty array contains nothing, and a missing field is not a match.
        assert "s3" not in [d["session_id"] for d in found]
        assert "s5" not in [d["session_id"] for d in found]

    asyncio.run(run())


def test_a_mixed_list_is_left_to_python_whole(store) -> None:
    """Narrowing on the strings alone would drop what the other members match."""

    async def run() -> None:
        coll = await _seed(store)
        coll_any = collection_module.AsyncCollection
        assert coll_any._string_in_pushdowns({"session_id": {"$in": ["s1", 7]}}) == []
        # And the answer is still right, because Python decides it.
        found = await coll.find({"session_id": {"$in": ["s1", 7]}}).to_list()
        assert [d["session_id"] for d in found] == ["s1"]

    asyncio.run(run())


def test_in_beside_another_operator_is_still_pushed(store) -> None:
    """The batch read carries a soft-delete guard beside its ids; the guard is
    not pushable and must not stop the ids from being."""

    coll_any = collection_module.AsyncCollection
    pushed = coll_any._string_in_pushdowns(
        {"session_id": {"$in": ["s1", "s2"]}, "deleted": {"$ne": True}}
    )
    assert pushed == [("session_id", ["s1", "s2"])]


def test_only_a_bare_in_is_pushed() -> None:
    """``{"$in": [...], "$ne": x}`` is a conjunction on one key; pushing the
    ``$in`` half alone would still only narrow, but the shape is not one the
    repositories write, and admitting it here would need the other half proved
    narrowing too."""

    coll_any = collection_module.AsyncCollection
    assert coll_any._string_in_pushdowns(
        {"session_id": {"$in": ["s1"], "$ne": "s9"}}
    ) == []
    assert coll_any._string_in_pushdowns({"session_id": {"$in": []}}) == []
