"""The pushdown's array half must be something an index can serve.

Mongo's ``{field: "x"}`` matches a stored array containing "x", so the SQL that
narrows a read has to admit those rows too. Expressed as
``jsonb_typeof(field) = 'array'`` it is a function of every row, so ORing it
beside the equality leaves the planner no index to use for the whole predicate:
one session lookup against a store of three thousand sessions reads every one of
them, measured at 476ms where the indexed equality alone answers in 0.07ms. That
cost lands on every read the product makes, and five concurrent conversations
were enough to stretch turn dispatch from one second to twenty.

Containment says the same thing in a form a GIN index serves, and says it more
precisely: it demands the element be present rather than admitting every
array-valued row for the Python matcher to reject afterwards.

These tests pin the semantics on both dialects, and pin the SQL shape on the one
where the index exists — behaviour alone cannot distinguish a predicate that
scans from one that seeks, and the scan is the defect.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from astrabox.persistence.repository.sqlite import collection as collection_module
from astrabox.persistence.repository.sqlite import engine as engine_module


@pytest.fixture
def store(tmp_path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'docs.db'}"


DOCS: list[dict[str, Any]] = [
    {"_id": "a1", "agent_id": "a1", "admins": ["alice", "bob"]},
    {"_id": "a2", "agent_id": "a2", "admins": ["carol"]},
    {"_id": "a3", "agent_id": "a3", "admins": []},
    # The same key holding a scalar: the equality half must still find it.
    {"_id": "a4", "agent_id": "a4", "admins": "alice"},
    {"_id": "a5", "agent_id": "a5"},
]


async def _seed(url: str) -> Any:
    coll = collection_module.AsyncCollection("agents", db_url=url)
    for doc in DOCS:
        await coll.insert_one(dict(doc))
    return coll


def test_a_scalar_query_still_matches_the_array_that_contains_it(store) -> None:
    """The reason the array half exists: dropping it loses real references."""

    async def run() -> None:
        coll = await _seed(store)
        found = await coll.find({"admins": "alice"}).to_list()
        assert sorted(d["agent_id"] for d in found) == ["a1", "a4"]

        found = await coll.find({"admins": "carol"}).to_list()
        assert [d["agent_id"] for d in found] == ["a2"]

        assert await coll.find({"admins": "dave"}).to_list() == []

    asyncio.run(run())


def test_an_empty_array_does_not_match(store) -> None:
    """Containment is narrower than the type test, and this is where it shows."""

    async def run() -> None:
        coll = await _seed(store)
        found = await coll.find({"admins": "alice"}).to_list()
        assert "a3" not in [d["agent_id"] for d in found]

    asyncio.run(run())


def test_postgres_narrows_with_containment_not_a_type_test() -> None:
    """The SQL shape is the point; behaviour cannot tell a seek from a scan.

    Compiled against the PostgreSQL dialect without a server: what matters is
    which operator the statement carries, and `@>` is the one a GIN index can
    answer.
    """
    from sqlalchemy.dialects import postgresql

    coll = collection_module.AsyncCollection.__new__(
        collection_module.AsyncCollection
    )
    coll._is_postgresql = True  # type: ignore[attr-defined]
    compiled = str(
        coll._json_array_contains("admins", "alice").compile(
            dialect=postgresql.dialect()
        )
    )
    assert "@>" in compiled, compiled
    assert "jsonb_typeof" not in compiled, compiled

    coll._is_postgresql = False  # type: ignore[attr-defined]
    sqlite_form = str(coll._json_array_contains("admins", "alice"))
    assert "json_type" in sqlite_form, sqlite_form


def test_postgres_creates_the_index_that_serves_it() -> None:
    """A containment predicate with no GIN index scans exactly as before.

    The two are one change: the operator is only worth emitting because the
    index exists to answer it.
    """
    source = engine_module.__file__
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    assert "USING gin (doc jsonb_path_ops)" in text
    assert "ix_documents_doc_gin" in text
