"""A page costs a page, and it is the same page the Python path would return.

The session list is the console's most-polled endpoint. Sorting a page in
Python requires holding every document the query can match, so `limit` and
`projection` cannot help: both apply after the parse. At three thousand
sessions that is tens of megabytes of JSON per poll, on the one event loop that
also dispatches turns — five open console pages stretch turn dispatch from one
second to twenty, which is not a load the product should need.

So the order goes to SQL and the read stops once the page is full. SQL is
trusted with the ORDER BY only; `matches` still decides every row, because the
SQL filter narrows with conditions `matches` would also require. These tests
pin both halves: the rows are the ones the Python path returns, in the same
order, and a page's work is bounded by the page rather than by the collection.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from astrabox.persistence.repository.sqlite import collection as collection_module


@pytest.fixture
def store(tmp_path) -> str:
    """A document store on its own file."""
    return f"sqlite+aiosqlite:///{tmp_path / 'docs.db'}"


def _sessions(count: int) -> list[dict[str, Any]]:
    return [
        {
            "_id": f"s{index:04d}",
            "session_id": f"s{index:04d}",
            "user_id": "u1" if index % 3 else "u2",
            "updated_at": f"2026-09-01T00:{index % 60:02d}:00Z",
            "deleted": False,
            "title": "x" * 200,
        }
        for index in range(count)
    ]


async def _seed(url: str, name: str, docs: list[dict[str, Any]]) -> Any:
    coll = collection_module.AsyncCollection(name, db_url=url)
    for doc in docs:
        await coll.insert_one(dict(doc))
    return coll


QUERY = {
    "user_id": "u1",
    "deleted": {"$ne": True},
    "$or": [{"hidden": {"$exists": False}}, {"hidden": False}],
}
SORT = [("updated_at", -1), ("session_id", -1)]


def test_the_sql_ordered_page_is_the_page_python_would_have_returned(store) -> None:
    async def run() -> None:
        coll = await _seed(store, "sessions", _sessions(60))
        pushed = await (
            coll.find(QUERY).sort(SORT, string_keyed=True).limit(20)
        ).to_list()
        plain = await (coll.find(QUERY).sort(SORT).limit(20)).to_list()
        assert [d["session_id"] for d in pushed] == [d["session_id"] for d in plain]
        assert pushed == plain

    asyncio.run(run())


def test_a_later_page_agrees_too(store) -> None:
    """Skip is applied after the ordered read, so page two must line up as well."""

    async def run() -> None:
        coll = await _seed(store, "sessions", _sessions(60))
        pushed = await (
            coll.find(QUERY).sort(SORT, string_keyed=True).skip(20).limit(20)
        ).to_list()
        plain = await (coll.find(QUERY).sort(SORT).skip(20).limit(20)).to_list()
        assert [d["session_id"] for d in pushed] == [d["session_id"] for d in plain]

    asyncio.run(run())


def test_missing_values_sort_where_mongo_puts_them(store) -> None:
    """Absent sorts before present. SQL's own NULL placement does not say that.

    It differs by dialect and flips with direction, which is why the ordered
    read carries an explicit rank rather than relying on it.
    """

    async def run() -> None:
        docs = _sessions(10)
        for doc in docs[:3]:
            doc.pop("updated_at")
        coll = await _seed(store, "sessions", docs)
        for direction in (1, -1):
            sort = [("updated_at", direction), ("session_id", 1)]
            pushed = await (
                coll.find(QUERY).sort(sort, string_keyed=True).limit(10)
            ).to_list()
            plain = await (coll.find(QUERY).sort(sort).limit(10)).to_list()
            assert [d["session_id"] for d in pushed] == [
                d["session_id"] for d in plain
            ], direction

    asyncio.run(run())


def test_a_page_does_not_parse_the_documents_behind_it(store) -> None:
    """The defect this exists for: cost per poll grew with the collection.

    Counted as documents handed to the Python matcher, which is where the
    parsing and the filtering happen. Ten times the sessions must not mean ten
    times the work for the same twenty rows.
    """

    async def run() -> None:
        seen: list[int] = []
        real_matches = collection_module.matches

        def counting_matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
            seen[-1] += 1
            return real_matches(doc, query)

        collection_module.matches = counting_matches  # type: ignore[assignment]
        try:
            costs = []
            for total in (100, 1000):
                coll = await _seed(store, f"sessions_{total}", _sessions(total))
                seen.append(0)
                rows = await (
                    coll.find(QUERY).sort(SORT, string_keyed=True).limit(20)
                ).to_list()
                assert len(rows) == 20
                costs.append(seen[-1])
        finally:
            collection_module.matches = real_matches  # type: ignore[assignment]

        small, large = costs
        assert large <= small * 2, (
            f"a twenty-row page examined {small} documents out of 100 and "
            f"{large} out of 1000; the page is still paying for the collection"
        )
        assert large < 200, f"a twenty-row page examined {large} documents"

    asyncio.run(run())


def test_an_unmarked_sort_still_takes_the_python_path(store) -> None:
    """Without the caller's statement the order stays in Python.

    A key that might hold a number orders differently in SQL, and the store
    cannot tell without reading everything — which is the cost the flag avoids.
    """

    async def run() -> None:
        coll = await _seed(store, "sessions", _sessions(30))
        cursor = coll.find(QUERY).sort(SORT).limit(5)
        assert cursor._sort_in_sql is False
        marked = coll.find(QUERY).sort(SORT, string_keyed=True).limit(5)
        assert marked._sort_in_sql is True
        # A dotted path is a nested value, not a top-level key: it cannot be
        # pushed even when the caller says the values are strings.
        nested = coll.find(QUERY).sort([("share.token", 1)], string_keyed=True).limit(5)
        assert nested._sort_in_sql is False

    asyncio.run(run())
