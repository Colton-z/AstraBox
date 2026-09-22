"""The admin session listing: narrowing belongs in the query, on both backends.

Two failures this file exists for, both found on real infrastructure rather than
here:

* narrowing AFTER the fetch — take five hundred rows, keep the ones under an
  agent the caller administers — returns, at a hundred agents and a thousand
  conversations a day each, an arbitrary sliver of one page and says nothing
  about what it dropped;
* the state distribution and total share one bounded ``$group`` query. Separate
  counts make endpoint latency scale with the number of enum members and do
  repeated collection work under concurrent console load.

The fake implements only that bounded aggregation shape. A repository that
adds another stage or accumulator fails here instead of drifting by backend.
"""

from __future__ import annotations

from typing import Any

import pytest

from astrabox.persistence.repository import session_repository as repo_module
from astrabox.persistence.repository.session_repository import SessionRepository


class _Cursor:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    def sort(self, *_a, **_k) -> "_Cursor":
        return self

    def skip(self, n: int) -> "_Cursor":
        return _Cursor(self._rows[n:])

    def limit(self, n: int) -> "_Cursor":
        return _Cursor(self._rows[:n])

    def __aiter__(self):
        async def _gen():
            for r in self._rows:
                yield r

        return _gen()


class _Collection:
    """A collection that implements only what the compat layer really has."""

    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows
        self.queries: list[dict[str, Any]] = []
        self.pipelines: list[list[dict[str, Any]]] = []

    @staticmethod
    def _matches(row: dict[str, Any], query: dict[str, Any]) -> bool:
        for key, cond in query.items():
            value = row.get(key)
            if isinstance(cond, dict):
                if "$ne" in cond and value == cond["$ne"]:
                    return False
                if "$in" in cond and value not in cond["$in"]:
                    return False
                if "$gte" in cond and not (value and value >= cond["$gte"]):
                    return False
                if "$lte" in cond and not (value and value <= cond["$lte"]):
                    return False
            elif value != cond:
                return False
        return True

    def find(self, query: dict[str, Any]) -> _Cursor:
        self.queries.append(query)
        return _Cursor([r for r in self.rows if self._matches(r, query)])

    async def count_documents(self, query: dict[str, Any]) -> int:
        self.queries.append(query)
        return len([r for r in self.rows if self._matches(r, query)])

    def aggregate(self, pipeline: list[dict[str, Any]]) -> _Cursor:
        self.pipelines.append(pipeline)
        assert len(pipeline) == 2
        assert set(pipeline[0]) == {"$match"}
        assert pipeline[1] == {"$group": {"_id": "$state", "count": {"$sum": 1}}}
        query = pipeline[0]["$match"]
        self.queries.append(query)
        counts: dict[Any, int] = {}
        for row in self.rows:
            if not self._matches(row, query):
                continue
            state = row.get("state")
            counts[state] = counts.get(state, 0) + 1
        return _Cursor([{"_id": state, "count": count} for state, count in counts.items()])


ROWS = [
    {"session_id": "a", "template_name": "Mine", "agent_id": "ag-1", "state": "READY",
     "created_at": "2026-08-05T10:00:00+00:00", "deleted": False},
    {"session_id": "b", "template_name": "Mine", "agent_id": "ag-2", "state": "TERMINATED",
     "created_at": "2026-08-06T10:00:00+00:00", "deleted": False},
    {"session_id": "c", "template_name": "Theirs", "agent_id": "ag-9", "state": "READY",
     "created_at": "2026-08-06T10:00:00+00:00", "deleted": False},
    {"session_id": "d", "template_name": "Mine", "agent_id": "ag-1", "state": "READY",
     "created_at": "2026-08-06T10:00:00+00:00", "deleted": True},
]


@pytest.fixture()
def collection(monkeypatch: pytest.MonkeyPatch) -> _Collection:
    coll = _Collection(list(ROWS))

    async def _get(_name: str) -> _Collection:
        return coll

    monkeypatch.setattr(repo_module, "get_async_collection", _get)

    async def _run(_label: str, op, **_k):
        return await op()

    monkeypatch.setattr(repo_module, "run_mongo_with_retry", _run)
    return coll


async def test_the_owner_scope_is_a_query_term(collection: _Collection) -> None:
    rows = await SessionRepository().list_all_sessions(template_names=["Mine"])

    assert [r["session_id"] for r in rows] == ["a", "b"]
    assert collection.queries[-1]["template_name"] == {"$in": ["Mine"]}


async def test_an_empty_owner_scope_matches_nothing_rather_than_everything(
    collection: _Collection,
) -> None:
    """`[]` is a real answer: someone who administers no agents sees none.

    `None` is the unscoped case and must not be what an empty scope collapses to
    — that would be a global leak wearing a filter's name.
    """
    assert await SessionRepository().list_all_sessions(template_names=[]) == []
    assert await SessionRepository().count_all_sessions(template_names=[]) == 0


async def test_the_time_window_is_over_when_a_session_started(
    collection: _Collection,
) -> None:
    """`created_at`, not `updated_at`: a window over the latter drags a
    months-old session into "yesterday" the moment a sweep touches its row."""
    rows = await SessionRepository().list_all_sessions(
        template_names=["Mine"], since="2026-08-06T00:00:00+00:00"
    )

    assert [r["session_id"] for r in rows] == ["b"]
    assert "created_at" in collection.queries[-1]


async def test_deleted_rows_stay_out_of_every_narrowing(collection: _Collection) -> None:
    rows = await SessionRepository().list_all_sessions(template_names=["Mine"], agent_id="ag-1")

    assert [r["session_id"] for r in rows] == ["a"]


async def test_the_count_is_over_the_collection_not_the_page(collection: _Collection) -> None:
    page = await SessionRepository().list_all_sessions(limit=1, template_names=["Mine"])
    total = await SessionRepository().count_all_sessions(template_names=["Mine"])

    assert len(page) == 1
    assert total == 2


async def test_the_state_distribution_and_total_share_one_query(
    collection: _Collection,
) -> None:
    totals = await SessionRepository().count_session_totals(template_names=["Mine"])

    assert totals == {"total": 2, "by_state": {"READY": 1, "TERMINATED": 1}}
    assert len(collection.pipelines) == 1


async def test_a_state_the_enum_does_not_model_is_absent_from_the_distribution(
    collection: _Collection,
) -> None:
    """And therefore the total must not be the sum of it.

    The distribution can miss a bucket; the total is counted on its own so it
    cannot be shrunk by one.
    """
    collection.rows.append(
        {"session_id": "e", "template_name": "Mine", "agent_id": "ag-3",
         "state": "NOT_A_MODELLED_STATE", "created_at": "2026-08-06T11:00:00+00:00",
         "deleted": False}
    )

    totals = await SessionRepository().count_session_totals(template_names=["Mine"])

    assert sum(totals["by_state"].values()) == 2
    assert totals["total"] == 3
