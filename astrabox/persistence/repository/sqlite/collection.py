"""``AsyncCollection``-shaped SQLite adapter — the open Mongo-collection replacement.

:func:`astrabox.persistence.repository.backend.get_async_collection` returns an
:class:`AsyncCollection` from this module when the SQLite backend is selected.
It reproduces the slice of
the (async) ``pymongo`` collection API that the 22 store-agnostic repository
bodies call — **and nothing more** — over the single ``astrabox_documents`` table
(:mod:`.engine`). Each document is a JSON blob in the ``doc`` column, partitioned
by ``collection`` and keyed by ``doc_id`` (the document's ``_id``).

Implemented surface (audited against the live tree)::

    find(filter, projection=None) -> _AsyncCursor      # .sort().skip().limit(), async-iterable
    find_one(filter, projection=None, *, sort=None)
    insert_one(doc)                  -> _InsertOneResult
    insert_many(docs, *, ordered=True) -> _InsertManyResult
    update_one(filter, update, *, upsert=False)  -> _UpdateResult
    update_many(filter, update)      -> _UpdateResult
    delete_one(filter)               -> _DeleteResult
    delete_many(filter)              -> _DeleteResult
    count_documents(filter)          -> int
    find_one_and_update(filter, update, *, return_document=ReturnDocument.BEFORE,
                        sort=None, projection=None, upsert=False)  -> doc | None
    aggregate(pipeline)              -> _AsyncCursor    # paging stages + bounded $group
    create_index(keys, *, unique=False, name=None, partialFilterExpression=None, **_)
    create_indexes(models)

Query/update operator coverage and the **fail-loud** policy live in :mod:`.query`.
Reads return the stored document **verbatim** (a fresh ``dict`` copy including
``_id``) so the boundary is byte-identical to a Mongo collection; an unsupported
operator raises rather than silently mis-matching.

Concurrency model (load-bearing — see :mod:`.engine`): every write method runs
on the WRITE engine, whose ``begin`` hook issues a real ``BEGIN IMMEDIATE`` —
the RESERVED write lock is taken BEFORE the opening SELECT, so the
read-modify-write of CAS-style ``find_one_and_update`` / guarded ``update_one``
claims is serialized: concurrent claimants block on the busy-timeout and see the
committed winner (exactly-one-owner is a hard invariant the turn-lock/lease
repos build on, pinned by ``tests/sqlite_cas_concurrency_test.py``). Pure reads
run on the separate READ engine (plain deferred ``BEGIN``): under WAL they read
a stable snapshot without ever contending for the write lock. Update payloads
are deep-copied before mutation — dotted-path ``$set``/``$inc`` must never
mutate the ORM-loaded document in place, or the flush would see old==new and
silently skip the UPDATE.

Unique-index enforcement is split by shape (see :meth:`AsyncCollection._check_unique`):
a plain (non-partial) ``create_index(..., unique=True)`` is enforced by a real
SQLite ``UNIQUE`` expression index — O(1), no collection scan; only a spec with
a ``partialFilterExpression`` (a predicate the index's ``WHERE`` clause cannot
express) falls back to a Python check, whose candidate set is pruned in SQL by
the spec's string-valued keys (the same pushdown contract as ``_load_docs``).
"""

from __future__ import annotations

import asyncio
import copy
import functools
import re
import uuid
from pathlib import PurePath
from typing import Any, Iterable, Sequence

from sqlalchemy import and_, case, delete as sa_delete, func, literal, or_, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError

from astrabox.common.logger.logger_factory import get_logger

from .._compat import DuplicateKeyError, ReturnDocument
from .engine import (
    DocumentRow,
    ensure_created,
    get_sessionmaker,
    is_postgresql_url,
    resolve_database_url,
)
from .query import (
    UnsupportedMongoOperator,
    apply_update,
    extract_set_on_insert,
    matches,
    read_path,
)

__all__ = [
    "AsyncCollection",
    "SqliteCursor",
]


logger = get_logger(__name__)

#: Rows one read may carry out of SQL and reject in Python before it is worth
#: saying so. A read narrowed to its own documents stays far below this; one
#: that names only its collection carries the collection.
_UNPUSHED_READ_ROWS = 200


def _report_unpushed_read(
    collection: str, query: dict[str, Any], *, loaded: int, kept: int
) -> None:
    """Name a read that filtered in Python what SQL could not narrow.

    Top-level string equality and ``$in`` over strings are pushed down; a query
    written in any other operator (``$ne``, ``$gt``, ``$exists``, ``$or``) or
    over a non-string value reaches SQL as its collection and is filtered here.
    That is correct and invisible: the
    caller gets the right documents. What it costs is the whole collection on
    every call, so the price grows with stored history rather than with the
    answer, and it is paid in two places at once: the database returning the
    rows and this process parsing them into dicts to reject. The database can
    report only the total, so this names the query shape that spends it.
    """

    if loaded < _UNPUSHED_READ_ROWS or loaded <= kept * 4:
        return
    # The operator, not just the key: `{"id": "x"}` and `{"id": {"$in": [...]}}`
    # name the same key and are a pushed-down index hit and a whole-collection
    # read respectively. Reporting the key alone cannot tell them apart, and
    # the first reading of this line got it wrong for exactly that reason.
    shape = sorted(
        f"{key}:{sorted(value)[0]}" if isinstance(value, dict) and value else f"{key}:="
        for key, value in query.items()
    )
    logger.warning(
        "unpushed read: collection=%s loaded=%d kept=%d predicate=%s asked_by=%s",
        collection,
        loaded,
        kept,
        shape,
        _first_caller_outside_the_store(),
    )


def _first_caller_outside_the_store() -> str:
    """The frame that issued the read, skipping this adapter's own layers.

    A collection and a predicate shape name a query but not the code that asks
    it, and grep cannot close that gap when the predicate is assembled from a
    caller's own fragment. Bounded to a few frames: this runs only on a read
    already reported as expensive.
    """

    import sys

    frame = sys._getframe(2)
    for _ in range(12):
        if frame is None:
            break
        name = frame.f_code.co_filename
        if "/persistence/repository/sqlite/" not in name:
            return f"{PurePath(name).name}:{frame.f_lineno}"
        frame = frame.f_back
    return "<store>"


def _log_detached_operation_outcome(task: "asyncio.Task[Any]") -> None:
    """Retrieve a detached operation's result so a late failure leaves a trail
    instead of an 'exception was never retrieved' warning."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning(
            "persistence operation completed detached from a cancelled caller and failed: %s",
            exc,
        )


def _cancel_shielded(fn: Any) -> Any:
    """Run a SQL operation to completion even if its caller is cancelled.

    An AnyIO request cancellation can otherwise land inside an asyncpg query or
    SQLAlchemy session exit. The driver's terminate then inherits that cancelled
    scope, and the connection reaches garbage collection without having checked
    in. SQLite has the equivalent failure with a cancelled transaction retaining
    its WAL write lock. Giving the operation its own task makes cancellation land
    between transactions for both backends: the caller observes
    ``CancelledError`` immediately while the operation finishes and returns its
    connection in the background.
    """

    @functools.wraps(fn)
    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        inner = asyncio.ensure_future(fn(self, *args, **kwargs))
        try:
            return await asyncio.shield(inner)
        except asyncio.CancelledError:
            inner.add_done_callback(_log_detached_operation_outcome)
            raise

    return wrapper


def _new_object_id() -> str:
    """Generate an ``_id`` for an inserted document that omits one.

    The repos that *do* set ``_id`` always pass a string (``user_id``,
    ``fence_id``, idempotency keys); the rest let Mongo assign one. A uuid4 hex is
    stable, string-typed, and JSON-serialisable, which the ``doc`` column needs.
    """
    return uuid.uuid4().hex


def _doc_id_of(doc: dict[str, Any]) -> str:
    """Return the ``_id`` of a document as a string key (generating one if absent)."""
    raw = doc.get("_id")
    if raw is None:
        return _new_object_id()
    return raw if isinstance(raw, str) else str(raw)


def _project(doc: dict[str, Any], projection: dict[str, Any] | None) -> dict[str, Any]:
    """Apply a Mongo *inclusion* projection (the only form the tree uses).

    ``{"field": 1, "a.b": 1}`` keeps just those paths (``_id`` is included by
    default unless ``{"_id": 0}`` is given). Dotted include paths are
    reconstructed into nested dicts, matching how the repos read them back.
    Exclusion projections other than ``_id:0`` are not used and raise.
    """
    if not projection:
        return dict(doc)

    includes = {k: v for k, v in projection.items() if k != "_id"}
    include_id = projection.get("_id", 1) != 0

    if any(v == 0 for v in includes.values()):
        raise UnsupportedMongoOperator(
            "field-exclusion projection is not supported by the SQLite collection "
            f"backend (only inclusion + optional _id:0); got {projection!r}"
        )

    out: dict[str, Any] = {}
    for path in includes:
        value = read_path(doc, path)
        if value is not None and value is not _MISSING_SENTINEL:
            # reconstruct nested path
            if "." not in path:
                out[path] = value
            else:
                cur = out
                parts = path.split(".")
                for part in parts[:-1]:
                    cur = cur.setdefault(part, {})
                cur[parts[-1]] = value
    if include_id and "_id" in doc:
        out["_id"] = doc["_id"]
    return out


# read_path returns the query.MISSING sentinel for absent paths; import-bind it
# without re-importing the symbol name into the projection logic above.
from .query import MISSING as _MISSING_SENTINEL  # noqa: E402

#: Keys whose top-level string equality may be pushed into SQL. Identifier
#: shape only: a dot means a nested path (different match semantics), and
#: anything fancier is not worth the json_extract path-quoting risk.
_PUSHDOWN_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: AstraBox's singular identity/reference fields are scalar by domain contract.
#: Keeping stored arrays in their SQL candidate set defeats the expression
#: indexes that make session-scoped reads bounded: PostgreSQL has to scan every
#: document to evaluate the array arm. Plural ``*_ids`` fields deliberately do
#: not match; their scalar queries use Mongo's array-membership semantics.
_SCALAR_ID_KEY_RE = re.compile(r"^(?:id|[A-Za-z_][A-Za-z0-9_]*_id)$")


# --------------------------------------------------------------------------- #
# Result objects — expose the attributes the repos read (modified_count, …)     #
# --------------------------------------------------------------------------- #
class _UpdateResult:
    """Mirror of ``pymongo.results.UpdateResult`` (the attrs the repos read)."""

    __slots__ = ("matched_count", "modified_count", "upserted_id")

    def __init__(
        self, matched_count: int, modified_count: int, upserted_id: Any = None
    ) -> None:
        self.matched_count = matched_count
        self.modified_count = modified_count
        self.upserted_id = upserted_id

    @property
    def raw_result(self) -> dict[str, Any]:
        return {
            "n": self.matched_count,
            "nModified": self.modified_count,
            "upserted": self.upserted_id,
        }


class _InsertOneResult:
    """Mirror of ``pymongo.results.InsertOneResult``."""

    __slots__ = ("inserted_id",)

    def __init__(self, inserted_id: Any) -> None:
        self.inserted_id = inserted_id


class _InsertManyResult:
    """Mirror of ``pymongo.results.InsertManyResult``."""

    __slots__ = ("inserted_ids",)

    def __init__(self, inserted_ids: list[Any]) -> None:
        self.inserted_ids = inserted_ids


class _DeleteResult:
    """Mirror of ``pymongo.results.DeleteResult``."""

    __slots__ = ("deleted_count",)

    def __init__(self, deleted_count: int) -> None:
        self.deleted_count = deleted_count


# --------------------------------------------------------------------------- #
# Cursor — built synchronously by find()/aggregate(), then async-iterated        #
# --------------------------------------------------------------------------- #
class SqliteCursor:
    """Lazy, chainable, async-iterable cursor over an in-memory result list.

    ``find()``/``aggregate()`` return this **synchronously** (no ``await``), so the
    repos' ``collection.find(q).sort(...).limit(n)`` chaining works; the actual
    SQLite read happens on first iteration (``async for`` / ``await`` /
    ``to_list``). ``.sort()/.skip()/.limit()`` mutate and return ``self``, matching
    the pymongo cursor builder. The cursor is also awaitable so
    ``backend.collect_async_cursor`` (which awaits an awaitable cursor) works.
    """

    def __init__(
        self,
        collection: "AsyncCollection",
        query: dict[str, Any] | None,
        projection: dict[str, Any] | None = None,
        *,
        pipeline: list[dict[str, Any]] | None = None,
    ) -> None:
        self._collection = collection
        self._query = query
        self._projection = projection
        self._pipeline = pipeline
        self._sort: list[tuple[str, int]] = []
        self._sort_in_sql = False
        self._skip = 0
        self._limit: int | None = None
        self._cached: list[dict[str, Any]] | None = None

    # ---- builder (chainable, returns self) --------------------------------
    def sort(
        self,
        key_or_list: Any,
        direction: int | None = None,
        *,
        string_keyed: bool = False,
    ) -> "SqliteCursor":
        """``cursor.sort("field", -1)`` or ``cursor.sort([("a", 1), ("b", -1)])``.

        ``string_keyed`` is the caller stating that every one of these keys holds
        a string or is absent throughout this collection. Only then may the
        order be evaluated in SQL, where comparison is lexicographic on text and
        would disagree with Mongo's cross-type ordering if a number or an object
        ever appeared. The store cannot check that without reading every
        document, which is the cost the flag exists to avoid, so the fact stays
        with the caller that owns the schema — and the ordering is asserted
        against the Python path in the tests.
        """
        if isinstance(key_or_list, str):
            self._sort = [(key_or_list, int(direction) if direction is not None else 1)]
        else:
            self._sort = [(str(k), int(d)) for k, d in key_or_list]
        self._sort_in_sql = bool(string_keyed) and all(
            _PUSHDOWN_KEY_RE.match(key) for key, _ in self._sort
        )
        self._cached = None
        return self

    def skip(self, n: int) -> "SqliteCursor":
        self._skip = max(0, int(n))
        self._cached = None
        return self

    def limit(self, n: int) -> "SqliteCursor":
        self._limit = max(0, int(n)) if n is not None else None
        self._cached = None
        return self

    # ---- materialise ------------------------------------------------------
    async def _materialise(self) -> list[dict[str, Any]]:
        if self._cached is not None:
            return self._cached
        query = self._query
        pipeline = list(self._pipeline) if self._pipeline is not None else None
        # A leading match is equivalent to the cursor query and lets the SQL
        # adapter apply its safe pushdowns before decoding candidate documents.
        if query is None and pipeline and set(pipeline[0]) == {"$match"}:
            query = pipeline.pop(0)["$match"]
        if pipeline is None and self._sort_in_sql and self._limit is not None:
            # The whole point: a listing that pages twenty rows must not parse
            # every document its user owns. Sorting in Python requires holding
            # them all, so the order goes to SQL and the read stops as soon as
            # enough rows have passed the Python matcher.
            docs = await self._collection._load_docs_page(
                query, self._sort, self._skip + self._limit
            )
            docs = docs[self._skip :]
            self._cached = [_project(d, self._projection) for d in docs]
            return self._cached
        docs = await self._collection._load_docs(query)
        if pipeline is not None:
            docs = self._collection._run_pipeline(docs, pipeline)
        else:
            docs = self._collection._apply_sort(docs, self._sort)
            if self._skip:
                docs = docs[self._skip :]
            if self._limit is not None:
                docs = docs[: self._limit]
        self._cached = [_project(d, self._projection) for d in docs]
        return self._cached

    async def to_list(self, length: int | None = None) -> list[dict[str, Any]]:
        """Collect the cursor (``length`` caps the count, ``None`` = all)."""
        rows = await self._materialise()
        return rows if length is None else rows[:length]

    def __aiter__(self) -> "SqliteCursor":
        self._iter_index = 0
        self._iter_rows = None
        return self

    async def __anext__(self) -> dict[str, Any]:
        if getattr(self, "_iter_rows", None) is None:
            self._iter_rows = await self._materialise()
            self._iter_index = 0
        if self._iter_index >= len(self._iter_rows):
            raise StopAsyncIteration
        row = self._iter_rows[self._iter_index]
        self._iter_index += 1
        return row

    def __await__(self):
        # Lets `await collection.find(...)` / collect_async_cursor work: awaiting
        # the cursor yields the cursor itself (already async-iterable), matching
        # how pymongo-async awaitable cursors are consumed by collect_async_cursor.
        async def _self() -> "SqliteCursor":
            await self._materialise()
            return self

        return _self().__await__()


# --------------------------------------------------------------------------- #
# Collection                                                                    #
# --------------------------------------------------------------------------- #
class AsyncCollection:
    """SQLite-backed, async, Mongo-collection-shaped document store (one namespace)."""

    def __init__(self, name: str, db_url: str | None = None) -> None:
        self._name = name
        self._db_url = db_url
        self._resolved_db_url = resolve_database_url(db_url)
        self._is_postgresql = is_postgresql_url(self._resolved_db_url)
        # Writes (and CAS read-modify-writes) serialize on the BEGIN IMMEDIATE
        # engine; pure reads take WAL snapshots on the read engine (never
        # contending for the write lock). See .engine for why this split exists.
        self._sessionmaker = get_sessionmaker(db_url, mode="write")
        self._read_sessionmaker = get_sessionmaker(db_url, mode="read")

    @property
    def name(self) -> str:
        return self._name

    # ---- internal read ----------------------------------------------------
    @_cancel_shielded
    async def _load_docs(self, query: dict[str, Any] | None) -> list[dict[str, Any]]:
        """Load this collection's docs that match ``query`` (in insertion order).

        SQL filters by ``collection``, by ``doc_id`` when the query is a bare
        ``{"_id": x}`` equality, and by ``json_extract`` for every top-level
        STRING equality in the query — the pushdown that keeps a
        session-scoped read from materialising the whole collection through
        SQLAlchemy + json.loads (without it, every poll's cost grows with
        total DB size and the sqlite worker thread becomes the deployment's
        top CPU consumer). Python's :func:`.query.matches`
        remains the SEMANTIC authority: the pushdown only ever narrows with
        conditions ``matches`` would also require, and every surviving row
        still passes through it. Only plain string values on identifier-shaped
        keys are pushed down — ``None`` must match documents missing the field
        entirely, non-string scalars have JSON/SQLite comparison edge cases,
        and dotted keys are nested paths with different semantics.
        """
        await ensure_created(self._db_url)
        async with self._read_sessionmaker() as sess:
            stmt = select(DocumentRow.doc).where(DocumentRow.collection == self._name)
            fast_id = self._extract_id_equality(query)
            if fast_id is not None:
                stmt = stmt.where(DocumentRow.doc_id == fast_id)
            for key, value in self._string_equality_pushdowns(query):
                stmt = stmt.where(self._string_equality_candidate(key, value))
            stmt = stmt.order_by(DocumentRow.seq.asc())
            rows = (await sess.execute(stmt)).scalars().all()
        if not query:
            return [dict(r) for r in rows]
        kept = [dict(r) for r in rows if matches(r, query)]
        _report_unpushed_read(self._name, query, loaded=len(rows), kept=len(kept))
        return kept

    async def _load_docs_page(
        self,
        query: dict[str, Any] | None,
        sort: list[tuple[str, int]],
        need: int,
    ) -> list[dict[str, Any]]:
        """The first ``need`` matches of ``query`` in ``sort`` order.

        The SQL filter only ever narrows with conditions :func:`.query.matches`
        would also require, so the matcher below remains the semantic authority
        and every row it rejects is simply skipped — the order is what SQL is
        trusted with, not the meaning. Rows are streamed rather than collected,
        so a page that fills early never parses the rest of the collection.

        Missing sorts before present, as Mongo orders it: a rank column carries
        that, because SQL's own NULL placement differs by dialect and by
        direction.
        """
        await ensure_created(self._db_url)
        stmt = select(DocumentRow.doc).where(DocumentRow.collection == self._name)
        fast_id = self._extract_id_equality(query)
        if fast_id is not None:
            stmt = stmt.where(DocumentRow.doc_id == fast_id)
        for key, value in self._string_equality_pushdowns(query):
            stmt = stmt.where(
                or_(
                    self._json_string_value(key) == value,
                    self._json_array_contains(key, value),
                )
            )
        for key, members in self._string_in_pushdowns(query):
            stmt = stmt.where(
                or_(
                    *(
                        condition
                        for member in members
                        for condition in (
                            self._json_string_value(key) == member,
                            self._json_array_contains(key, member),
                        )
                    )
                )
            )
        or_clause = self._or_pushdown(query)
        if or_clause is not None:
            stmt = stmt.where(or_clause)
        order: list[Any] = []
        for key, direction in sort:
            value = self._json_string_value(key)
            present = case((value.is_(None), 0), else_=1)
            order.extend(
                [present.asc(), value.asc()]
                if direction >= 0
                else [present.desc(), value.desc()]
            )
        # `seq` breaks ties the way the unordered read did, so two documents
        # that sort equally keep insertion order rather than the dialect's.
        order.append(DocumentRow.seq.asc())
        stmt = stmt.order_by(*order)

        matched: list[dict[str, Any]] = []
        async with self._read_sessionmaker() as sess:
            result = await sess.stream(stmt)
            async for row in result.scalars():
                doc = dict(row)
                if query and not matches(doc, query):
                    continue
                matched.append(doc)
                if len(matched) >= need:
                    break
        return matched

    @staticmethod
    def _extract_id_equality(query: dict[str, Any] | None) -> str | None:
        """If ``query`` pins ``_id`` to a scalar equality, return it for an index hit."""
        if not query:
            return None
        val = query.get("_id")
        if val is None or isinstance(val, dict):
            return None
        return val if isinstance(val, str) else str(val)

    @staticmethod
    def _string_equality_pushdowns(
        query: dict[str, Any] | None,
    ) -> list[tuple[str, str]]:
        """Top-level ``{key: "literal"}`` pairs that are safe to filter in SQL."""
        if not query:
            return []
        return [
            (key, value)
            for key, value in query.items()
            if key != "_id"
            and isinstance(value, str)
            and _PUSHDOWN_KEY_RE.match(key)
        ]

    def _string_equality_candidate(self, key: str, value: str) -> Any:
        """Narrow one string equality without unbounding identity lookups."""
        scalar_equality = self._json_string_value(key) == value
        if _SCALAR_ID_KEY_RE.fullmatch(key):
            return scalar_equality
        return or_(
            scalar_equality,
            # Mongo's {field: "x"} also matches a stored array that contains
            # "x". Non-identity fields retain that candidate set; matches()
            # below performs the exact membership check.
            self._json_value_type(key) == "array",
        )

    @staticmethod
    def _string_in_pushdowns(
        query: dict[str, Any] | None,
    ) -> list[tuple[str, list[str]]]:
        """Top-level ``{key: {"$in": ["a", "b"]}}`` over strings.

        Same narrowing rule as the equality pushdown, one level out: Mongo's
        ``$in`` matches a scalar field equal to any element and an array field
        containing any element, so each element contributes the same pair of
        conditions and the whole disjunction is what SQL is given. A list with
        a non-string element is left to Python entirely rather than narrowed on
        the strings alone, which would drop the rows the other elements match.

        Reading a batch by id is the shape that pays for this: fifty ids
        against a collection of four thousand loaded all four thousand and kept
        the fifty.
        """

        if not query:
            return []
        found: list[tuple[str, list[str]]] = []
        for key, value in query.items():
            if key == "_id" or not _PUSHDOWN_KEY_RE.match(key):
                continue
            if not isinstance(value, dict) or set(value) != {"$in"}:
                continue
            members = value["$in"]
            if not isinstance(members, list) or not members:
                continue
            if not all(isinstance(member, str) for member in members):
                continue
            found.append((key, list(members)))
        return found

    def _or_pushdown(self, query: dict[str, Any] | None) -> Any | None:
        """A top-level ``$or`` narrowed to the disjunction of its arms.

        A document matching the ``$or`` satisfies at least one arm, so the
        disjunction of one narrowing condition per arm is itself a narrowing —
        provided EVERY arm yields one. An arm with nothing pushable could match
        any row, and ORing the others beside it would drop the rows only that
        arm matches, so the whole predicate is then left to Python.

        Within an arm the pushable conditions are ANDed, which the arm requires
        anyway. Keys the arm carries that are not pushable are simply not
        represented; the Python matcher remains the authority over all of it.

        The periodic stuck-session scan is what this is for: three arms over
        one collection, each naming a state, together selecting thirty-four
        rows out of four thousand — and, before this, reading all four thousand
        every few seconds.
        """

        if not query:
            return None
        arms = query.get("$or")
        if not isinstance(arms, list) or not arms:
            return None
        clauses: list[Any] = []
        for arm in arms:
            if not isinstance(arm, dict):
                return None
            conditions = [
                or_(
                    self._json_string_value(key) == value,
                    self._json_array_contains(key, value),
                )
                for key, value in self._string_equality_pushdowns(arm)
            ]
            conditions.extend(
                or_(
                    *(
                        condition
                        for member in members
                        for condition in (
                            self._json_string_value(key) == member,
                            self._json_array_contains(key, member),
                        )
                    )
                )
                for key, members in self._string_in_pushdowns(arm)
            )
            if not conditions:
                return None
            clauses.append(and_(*conditions))
        return or_(*clauses)

    @staticmethod
    def _apply_sort(
        docs: list[dict[str, Any]], sort: list[tuple[str, int]]
    ) -> list[dict[str, Any]]:
        """Stable multi-key sort honouring Mongo's missing-sorts-first convention.

        Applied least-significant key first (Python's stable sort composes), so a
        ``[("a",1),("b",-1)]`` spec sorts by ``a`` asc then ``b`` desc. Absent
        values sort before present ones (Mongo treats missing as smallest).
        """
        if not sort:
            return docs
        ordered = list(docs)
        for field, direction in reversed(sort):
            reverse = direction < 0
            ordered.sort(
                key=lambda d, f=field: _SortKey(read_path(d, f)),
                reverse=reverse,
            )
        return ordered

    @staticmethod
    def _group_scalar_counts(
        docs: list[dict[str, Any]], spec: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Implement the tree's bounded ``$group`` shape.

        The repository needs one portable distribution query: group on a
        scalar field and count rows with ``$sum: 1``. Supporting any wider
        Mongo expression language here would make the compatibility seam claim
        semantics it does not have, so every other shape fails loudly.
        """
        group_expr = spec.get("_id")
        if (
            not isinstance(group_expr, str)
            or not group_expr.startswith("$")
            or len(group_expr) == 1
        ):
            raise UnsupportedMongoOperator(
                "the SQLite collection backend supports $group only with a scalar "
                "field reference such as {'_id': '$state'}"
            )
        accumulators = {key: value for key, value in spec.items() if key != "_id"}
        if not accumulators:
            raise UnsupportedMongoOperator(
                "the SQLite collection backend requires at least one $group accumulator"
            )
        for name, accumulator in accumulators.items():
            if (
                not isinstance(accumulator, dict)
                or set(accumulator) != {"$sum"}
                or not isinstance(accumulator["$sum"], int)
                or isinstance(accumulator["$sum"], bool)
                or accumulator["$sum"] != 1
            ):
                raise UnsupportedMongoOperator(
                    f"unsupported $group accumulator {name!r}: {accumulator!r}; "
                    "only {$sum: 1} row counts are implemented"
                )

        groups: dict[Any, dict[str, Any]] = {}
        field = group_expr[1:]
        for doc in docs:
            key = read_path(doc, field)
            if key is _MISSING_SENTINEL:
                key = None
            if isinstance(key, (dict, list)):
                raise UnsupportedMongoOperator(
                    f"$group field {field!r} resolved to a non-scalar value"
                )
            row = groups.setdefault(
                key,
                {"_id": key, **{name: 0 for name in accumulators}},
            )
            for name in accumulators:
                row[name] += 1
        return list(groups.values())

    def _run_pipeline(
        self, docs: list[dict[str, Any]], pipeline: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Run the supported aggregation stages.

        ``$group`` is deliberately bounded to scalar-field ``$sum: 1`` counts;
        any other stage or accumulator fails loud rather than silently drifting
        from the Mongo backend.
        """
        result = list(docs)
        for stage in pipeline:
            if len(stage) != 1:
                raise UnsupportedMongoOperator(
                    f"aggregation stage must have exactly one operator, got {stage!r}"
                )
            (op, spec), = stage.items()
            if op == "$match":
                result = [d for d in result if matches(d, spec)]
            elif op == "$sort":
                sort_spec = [(k, int(v)) for k, v in spec.items()]
                result = self._apply_sort(result, sort_spec)
            elif op == "$limit":
                result = result[: int(spec)]
            elif op == "$skip":
                result = result[int(spec) :]
            elif op == "$group":
                result = self._group_scalar_counts(result, spec)
            else:
                raise UnsupportedMongoOperator(
                    f"unsupported aggregation stage {op!r}; the SQLite collection backend "
                    "implements $match/$sort/$limit/$skip and bounded $group counts only "
                    "(no $lookup/$project/$unwind)"
                )
        return result

    # ---- read API ---------------------------------------------------------
    def find(
        self, filter: dict[str, Any] | None = None, projection: dict[str, Any] | None = None
    ) -> SqliteCursor:
        """Return a chainable async cursor (no DB hit until iterated)."""
        return SqliteCursor(self, filter, projection)

    async def find_one(
        self,
        filter: dict[str, Any] | None = None,
        projection: dict[str, Any] | None = None,
        *,
        sort: Sequence[tuple[str, int]] | None = None,
    ) -> dict[str, Any] | None:
        """Return the first matching document (after ``sort``), projected, or ``None``."""
        docs = await self._load_docs(filter)
        if sort:
            docs = self._apply_sort(docs, [(str(k), int(d)) for k, d in sort])
        if not docs:
            return None
        return _project(docs[0], projection)

    @_cancel_shielded
    async def count_documents(self, filter: dict[str, Any] | None = None) -> int:
        """Count matching documents. (Uses a fast COUNT for the empty filter.)"""
        if not filter:
            await ensure_created(self._db_url)
            async with self._read_sessionmaker() as sess:
                stmt = select(func.count()).select_from(DocumentRow).where(
                    DocumentRow.collection == self._name
                )
                return int((await sess.execute(stmt)).scalar_one())
        return len(await self._load_docs(filter))

    def aggregate(self, pipeline: list[dict[str, Any]]) -> SqliteCursor:
        """Return a cursor over the supported aggregation stages."""
        # The initial query is None; cursor materialisation promotes a leading
        # $match into the collection read so its safe SQL pushdowns still apply.
        return SqliteCursor(self, None, projection=None, pipeline=list(pipeline))

    # ---- write API --------------------------------------------------------
    @_cancel_shielded
    async def insert_one(self, document: dict[str, Any]) -> _InsertOneResult:
        """Insert one document; raise :class:`DuplicateKeyError` on an ``_id`` clash."""
        await ensure_created(self._db_url)
        doc = dict(document)
        doc_id = _doc_id_of(doc)
        doc["_id"] = doc_id
        async with self._sessionmaker() as sess:
            try:
                async with sess.begin():
                    await self._check_unique(sess, doc, exclude_id=None)
                    sess.add(DocumentRow(collection=self._name, doc_id=doc_id, doc=doc))
            except IntegrityError as exc:
                raise DuplicateKeyError(
                    f"duplicate key in collection {self._name!r}: _id={doc_id!r}"
                ) from exc
        return _InsertOneResult(doc_id)

    @_cancel_shielded
    async def insert_many(
        self, documents: Iterable[dict[str, Any]], *, ordered: bool = True
    ) -> _InsertManyResult:
        """Insert many documents in order.

        With ``ordered=True`` (the only form the tree uses) the first duplicate
        aborts the batch and raises — surfaced as :class:`DuplicateKeyError` so the
        repos' ``except DuplicateKeyError`` per-doc fallback engages exactly as it
        does against Mongo. Each document is inserted in its own transaction so the
        ones before the failure persist (matching ordered-insert semantics).
        """
        inserted: list[Any] = []
        docs = [dict(d) for d in documents]
        for index, doc in enumerate(docs):
            doc_id = _doc_id_of(doc)
            doc["_id"] = doc_id
            await ensure_created(self._db_url)
            async with self._sessionmaker() as sess:
                try:
                    async with sess.begin():
                        await self._check_unique(sess, doc, exclude_id=None)
                        sess.add(
                            DocumentRow(collection=self._name, doc_id=doc_id, doc=doc)
                        )
                except IntegrityError as exc:
                    if ordered:
                        raise DuplicateKeyError(
                            f"duplicate key in collection {self._name!r} at batch "
                            f"index {index}: _id={doc_id!r}"
                        ) from exc
                    # unordered: record and continue
                    continue
            inserted.append(doc_id)
        return _InsertManyResult(inserted)

    @_cancel_shielded
    async def update_one(
        self,
        filter: dict[str, Any],
        update: dict[str, Any],
        *,
        upsert: bool = False,
    ) -> _UpdateResult:
        """Update the first matching document; optionally upsert if none matched."""
        await ensure_created(self._db_url)
        async with self._sessionmaker() as sess:
            try:
                async with sess.begin():
                    row = await self._first_matching_row(sess, filter)
                    if row is None:
                        if not upsert:
                            return _UpdateResult(0, 0, None)
                        new_id = await self._do_upsert_insert(sess, filter, update)
                        return _UpdateResult(0, 0, new_id)
                    # Deep copies: dotted-path $set/$inc mutate nested dicts in
                    # place; a shallow copy would mutate the ORM-loaded row.doc
                    # too, the flush would see old==new, and the UPDATE would be
                    # silently skipped (and `modified` mis-reported).
                    before = copy.deepcopy(row.doc)
                    after = apply_update(copy.deepcopy(row.doc), update)
                    self._reassign_doc(row, after)
                    await self._check_unique(sess, after, exclude_id=row.doc_id)
                    modified = 1 if after != before else 0
                    return _UpdateResult(1, modified, None)
            except IntegrityError as exc:
                raise DuplicateKeyError(
                    f"duplicate key on update in collection {self._name!r}"
                ) from exc

    @_cancel_shielded
    async def update_many(
        self, filter: dict[str, Any], update: dict[str, Any]
    ) -> _UpdateResult:
        """Apply ``update`` to every matching document."""
        await ensure_created(self._db_url)
        matched = 0
        modified = 0
        async with self._sessionmaker() as sess:
            try:
                async with sess.begin():
                    for row in await self._all_matching_rows(sess, filter):
                        matched += 1
                        before = copy.deepcopy(row.doc)
                        after = apply_update(copy.deepcopy(row.doc), update)
                        self._reassign_doc(row, after)
                        await self._check_unique(sess, after, exclude_id=row.doc_id)
                        if after != before:
                            modified += 1
            except IntegrityError as exc:
                raise DuplicateKeyError(
                    f"duplicate key on update_many in collection {self._name!r}"
                ) from exc
        return _UpdateResult(matched, modified, None)

    @_cancel_shielded
    async def delete_one(self, filter: dict[str, Any]) -> _DeleteResult:
        """Delete the first matching document."""
        await ensure_created(self._db_url)
        async with self._sessionmaker() as sess:
            async with sess.begin():
                row = await self._first_matching_row(sess, filter)
                if row is None:
                    return _DeleteResult(0)
                await sess.delete(row)
        return _DeleteResult(1)

    @_cancel_shielded
    async def delete_many(self, filter: dict[str, Any]) -> _DeleteResult:
        """Delete all matching documents (``{}`` deletes the whole collection)."""
        await ensure_created(self._db_url)
        async with self._sessionmaker() as sess:
            async with sess.begin():
                stmt = sa_delete(DocumentRow).where(
                    DocumentRow.collection == self._name
                )
                if not filter:
                    result = await sess.execute(stmt)
                    return _DeleteResult(int(result.rowcount or 0))
                pushdowns = self._string_equality_pushdowns(filter)
                if len(pushdowns) == len(filter):
                    # Fully expressible in SQL: one DELETE with no row loading.
                    # Session cleanup can remove thousands of frame and journal
                    # rows, so SQL pushdown bounds write-lock duration.
                    for key, value in pushdowns:
                        stmt = stmt.where(self._json_string_value(key) == value)
                    result = await sess.execute(stmt)
                    return _DeleteResult(int(result.rowcount or 0))
                # Residual predicates: load the pushdown-pruned candidates,
                # verify with matches(), then delete in ONE statement by id.
                rows = await self._all_matching_rows(sess, filter)
                if not rows:
                    return _DeleteResult(0)
                doc_ids = [row.doc_id for row in rows]
                result = await sess.execute(
                    sa_delete(DocumentRow).where(
                        DocumentRow.collection == self._name,
                        DocumentRow.doc_id.in_(doc_ids),
                    )
                )
                return _DeleteResult(int(result.rowcount or 0))

    @_cancel_shielded
    async def find_one_and_update(
        self,
        filter: dict[str, Any],
        update: dict[str, Any],
        *,
        return_document: Any = ReturnDocument.BEFORE,
        sort: Sequence[tuple[str, int]] | None = None,
        projection: dict[str, Any] | None = None,
        upsert: bool = False,
    ) -> dict[str, Any] | None:
        """Atomically find one doc (by ``filter``+``sort``), update it, return before/after.

        This is the CAS primitive the lease/claim repos depend on: the
        read-modify-write runs in one transaction so two workers cannot both win a
        lease. Returns the matched document (``BEFORE``) or the updated one
        (``AFTER``); ``None`` when nothing matched and ``upsert`` is False.
        """
        await ensure_created(self._db_url)
        async with self._sessionmaker() as sess:
            try:
                async with sess.begin():
                    rows = await self._all_matching_rows(sess, filter)
                    if sort and rows:
                        sort_spec = [(str(k), int(d)) for k, d in sort]
                        ordered_docs = self._apply_sort([r.doc for r in rows], sort_spec)
                        first_doc_id = ordered_docs[0].get("_id")
                        row = next(
                            (r for r in rows if r.doc.get("_id") == first_doc_id), rows[0]
                        )
                    else:
                        row = rows[0] if rows else None

                    if row is None:
                        if not upsert:
                            return None
                        new_doc = self._build_upsert_doc(filter, update)
                        new_id = new_doc["_id"]
                        await self._check_unique(sess, new_doc, exclude_id=None)
                        sess.add(
                            DocumentRow(
                                collection=self._name, doc_id=new_id, doc=new_doc
                            )
                        )
                        if return_document == ReturnDocument.AFTER:
                            return _project(new_doc, projection)
                        return None

                    before = copy.deepcopy(row.doc)
                    after = apply_update(copy.deepcopy(row.doc), update)
                    self._reassign_doc(row, after)
                    await self._check_unique(sess, after, exclude_id=row.doc_id)
                    chosen = after if return_document == ReturnDocument.AFTER else before
                    return _project(chosen, projection)
            except IntegrityError as exc:
                raise DuplicateKeyError(
                    f"duplicate key on find_one_and_update in collection {self._name!r}"
                ) from exc

    # ---- write helpers ----------------------------------------------------
    async def _first_matching_row(
        self, sess: Any, filter: dict[str, Any]
    ) -> DocumentRow | None:
        rows = await self._all_matching_rows(sess, filter)
        return rows[0] if rows else None

    async def _all_matching_rows(
        self, sess: Any, filter: dict[str, Any] | None
    ) -> list[DocumentRow]:
        stmt = select(DocumentRow).where(DocumentRow.collection == self._name)
        fast_id = self._extract_id_equality(filter)
        if fast_id is not None:
            stmt = stmt.where(DocumentRow.doc_id == fast_id)
        else:
            # Use the read path's top-level string-equality pushdown inside the
            # write transaction. It avoids deserializing the whole collection
            # while holding BEGIN IMMEDIATE; matches() still re-verifies every
            # candidate, so the pushdown only prunes.
            for key, value in self._string_equality_pushdowns(filter or {}):
                stmt = stmt.where(self._json_string_value(key) == value)
        if self._is_postgresql:
            # Every mutation is a Python read-modify-write. PostgreSQL must lock
            # the candidate rows before evaluating the guard, otherwise two
            # concurrent CAS claimants can both read the old document and win.
            stmt = stmt.with_for_update()
        stmt = stmt.order_by(DocumentRow.seq.asc())
        rows = (await sess.execute(stmt)).scalars().all()
        if not filter:
            return list(rows)
        return [r for r in rows if matches(r.doc, filter)]

    @staticmethod
    def _reassign_doc(row: DocumentRow, new_doc: dict[str, Any]) -> None:
        """Persist a mutated doc back onto the row (re-assign so SA tracks the change).

        ``_id`` is immutable: an update doc that omits/changes ``_id`` keeps the
        row's existing one (Mongo forbids mutating ``_id``).
        """
        new_doc["_id"] = row.doc_id
        row.doc = new_doc

    def _build_upsert_doc(
        self, filter: dict[str, Any], update: dict[str, Any]
    ) -> dict[str, Any]:
        """Seed an upserted document from the filter equalities + operator effects."""
        seed: dict[str, Any] = {}
        for key, value in (filter or {}).items():
            if key.startswith("$") or isinstance(value, dict):
                continue  # operator clauses don't seed concrete values
            from .query import set_path

            set_path(seed, key, value)
        seed.update(extract_set_on_insert(update))
        seed["_id"] = _doc_id_of(seed)
        return seed

    async def _do_upsert_insert(
        self, sess: Any, filter: dict[str, Any], update: dict[str, Any]
    ) -> Any:
        new_doc = self._build_upsert_doc(filter, update)
        await self._check_unique(sess, new_doc, exclude_id=None)
        sess.add(DocumentRow(collection=self._name, doc_id=new_doc["_id"], doc=new_doc))
        return new_doc["_id"]

    # ---- unique-index enforcement ----------------------------------------
    async def _check_unique(
        self, sess: Any, doc: dict[str, Any], *, exclude_id: str | None
    ) -> None:
        """Enforce ONLY the unique indexes a plain SQL index cannot express.

        ``create_index(..., unique=True)`` (see :meth:`_create_sqlite_expression_index`)
        already builds a real ``CREATE UNIQUE INDEX ... WHERE collection = <name>``
        expression index for every registered spec — SQL's own "NULL is never
        equal, not even to another NULL" rule means a document missing one of the
        keyed fields is automatically exempt from that constraint, which is
        exactly the "absent -> not constrained" treatment a plain (non-partial)
        unique/sparse index needs. So for a spec with **no**
        ``partialFilterExpression`` the real index is the sole enforcer: a
        violation raises ``IntegrityError`` at flush/commit, translated to
        :class:`DuplicateKeyError` by every write method's enclosing ``except``
        (exactly like the table's own ``_id`` ``UNIQUE`` constraint) — no
        collection scan needed, so this is an O(1) index lookup, not O(collection
        size) per write.

        A ``partialFilterExpression`` spec (the registry contains one shape:
        ``transcript_entries``' ``ux_transcript_scope_uuid``, gated on
        ``{"uuid": {"$type": "string"}}``) is NOT translated into the SQL index's
        WHERE clause — only ``collection = <name>`` is — because compiling an
        arbitrary Mongo partial-filter predicate into a SQL boolean expression is
        unbounded in general. Those specs still need the doc-by-doc scan below,
        evaluated with the same :func:`.query.matches` the read path uses, so the
        partial semantics are honoured exactly.
        """
        specs = [
            (fields, partial)
            for fields, partial in _INDEX_REGISTRY.unique_specs(self._name)
            if partial is not None
        ]
        if not specs:
            return
        for fields, partial in specs:
            if not matches(doc, partial):
                continue
            key_vals = [read_path(doc, f) for f in fields]
            # Prune candidates in SQL: a collision must share EVERY key value,
            # so each string-valued top-level key becomes a json_extract
            # equality — the same pushdown contract as _load_docs /
            # _all_matching_rows. This runs INSIDE the write transaction on
            # every insert/update of the collection. Collections such as
            # transcript_entries grow continuously with mirror traffic, so the
            # pushdown bounds time under the write lock. Python below still
            # verifies exact partial and non-pushed-field semantics.
            stmt = select(DocumentRow.doc_id, DocumentRow.doc).where(
                DocumentRow.collection == self._name
            )
            for field, value in zip(fields, key_vals):
                if isinstance(value, str) and _PUSHDOWN_KEY_RE.match(field):
                    stmt = stmt.where(self._json_string_value(field) == value)
            existing = (await sess.execute(stmt)).all()
            for other_id, other in existing:
                if other_id == exclude_id:
                    continue
                if not matches(other, partial):
                    continue
                if [read_path(other, f) for f in fields] == key_vals:
                    raise DuplicateKeyError(
                        f"duplicate key in collection {self._name!r} on unique index "
                        f"{fields}: {key_vals!r}"
                    )

    # ---- index API (DDL — builds real expression indexes + registers specs) --
    async def create_index(
        self,
        keys: Any,
        *,
        unique: bool = False,
        name: str | None = None,
        sparse: bool = False,
        partialFilterExpression: dict[str, Any] | None = None,
        **_ignored: Any,
    ) -> str:
        """Create an index over ``json_extract`` columns and register unique specs.

        Accepts the pymongo key forms ``"field"`` and ``[("a", 1), ("b", -1)]``.
        Builds a SQLite expression index and, when ``unique=True``, registers the
        key-spec so :meth:`_check_unique` can fall back to it for the shapes SQL
        cannot express (see that method). For a plain (non-partial) unique spec,
        this expression index is the ONLY enforcement — so, unlike the other
        methods that lazily create the table on first use, this one must
        :func:`ensure_created` the ``astrabox_documents`` table itself BEFORE
        issuing the ``CREATE INDEX`` DDL: called on a brand-new database (the
        common case — index setup runs before any insert), the table would not
        exist yet, the DDL would fail, and that failure is swallowed as
        best-effort (see :meth:`_create_sqlite_expression_index`) — silently
        leaving the collection with no unique enforcement at all. ``name`` is
        returned like pymongo. The ``name``/``key``/``unique``/``sparse`` shape is
        recorded so :meth:`list_indexes` can report it (callers that verify an
        expected unique index rely on this). Other unknown kwargs (``background``,
        ``expireAfterSeconds``…) are accepted and ignored — no SQLite analogue.
        """
        await ensure_created(self._db_url)
        fields = self._normalise_index_keys(keys)
        index_name = name or ("ux_" if unique else "ix_") + self._name + "_" + "_".join(fields)
        if unique:
            _INDEX_REGISTRY.register_unique(self._name, fields, partialFilterExpression)
        _INDEX_REGISTRY.register_spec(
            self._name, index_name, fields, unique=unique, sparse=sparse
        )
        await self._create_sqlite_expression_index(
            index_name,
            fields,
            unique=unique,
            partial_filter=partialFilterExpression,
        )
        return index_name

    async def list_indexes(self) -> list[dict[str, Any]]:
        """Report indexes in the pymongo ``list_indexes`` shape.

        The implicit ``_id_`` index plus every registered index that ACTUALLY
        exists in the schema. Presence is read from ``sqlite_master``, not the
        in-process spec registry — a registered spec whose ``CREATE INDEX`` was
        swallowed (locked db, disk full, unsupported expression) is absent here.
        The registry only supplies the reported key/unique/sparse shape for the
        indexes that are really present. This is what lets
        ``ensure_unique_index``'s create-then-verify contract fail loud on a
        real DDL failure instead of trusting a spec the create never landed.
        """
        real_names = await self._existing_index_names()
        specs: list[dict[str, Any]] = [
            {"name": "_id_", "key": {"_id": 1}, "unique": True}
        ]
        for spec in _INDEX_REGISTRY.list_specs(self._name):
            if spec.get("name") in real_names:
                specs.append(spec)
        return specs

    async def _existing_index_names(self) -> set[str]:
        """Index names that actually exist on the shared documents table."""
        from .engine import get_engine

        engine = get_engine(self._db_url)
        async with engine.begin() as conn:
            if self._is_postgresql:
                query = (
                    "SELECT indexname FROM pg_indexes "
                    "WHERE schemaname = current_schema() AND tablename = :tbl"
                )
            else:
                query = (
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'index' AND tbl_name = :tbl"
                )
            result = await conn.execute(text(query), {"tbl": DocumentRow.__tablename__})
            return {row[0] for row in result}

    async def drop_index(self, name: str) -> None:
        """Drop an index by name; absent is success, as in pymongo's ``DROP_INDEX``.

        A unique index outlives the code that asked for it: the SQL object stays
        in the schema once created, and keeps rejecting writes the running code
        considers legal. Retiring one is therefore a schema change a migration
        performs, which is why dropping it needs a call of its own rather than
        an absent ``create_index``.
        """
        _INDEX_REGISTRY.forget_spec(self._name, name)
        from .engine import get_engine

        engine = get_engine(self._db_url)
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP INDEX IF EXISTS {name}"))

    async def create_indexes(self, models: Sequence[Any]) -> list[str]:
        """Create several indexes (accepts pymongo ``IndexModel``-like objects)."""
        created: list[str] = []
        for model in models:
            document = getattr(model, "document", None)
            if isinstance(document, dict):
                keys = list(document.get("key", {}).items())
                created.append(
                    await self.create_index(
                        keys,
                        unique=bool(document.get("unique", False)),
                        name=document.get("name"),
                        sparse=bool(document.get("sparse", False)),
                        partialFilterExpression=document.get("partialFilterExpression"),
                    )
                )
        return created

    @staticmethod
    def _normalise_index_keys(keys: Any) -> list[str]:
        if isinstance(keys, str):
            return [keys]
        out: list[str] = []
        for entry in keys:
            if isinstance(entry, str):
                out.append(entry)
            else:
                out.append(str(entry[0]))
        return out

    async def _create_sqlite_expression_index(
        self,
        index_name: str,
        fields: list[str],
        *,
        unique: bool,
        partial_filter: dict[str, Any] | None = None,
    ) -> None:
        """Create the real JSON expression index for the active SQL dialect.

        All collections share one physical ``astrabox_documents`` table, so an index
        must be scoped to *this* collection's namespace. A ``UNIQUE`` index is scoped
        with a **partial** ``WHERE collection = '<name>'`` predicate — NOT by adding
        ``collection`` as a leading key column. The difference is load-bearing: a
        ``UNIQUE (collection, <expr>)`` index makes the *pair* unique, which wrongly
        forbids two rows in an append-style collection (e.g. ``session_events``,
        which has many events per ``session_id``) from sharing the keyed value — the
        ``ux_sessions_session_id``-style "one row per session_id in `sessions`" index
        would then also forbid a second ``session_events`` event for that session.
        The partial predicate confines uniqueness to the owning collection, matching
        the per-collection scope :meth:`_check_unique` enforces in Python
        (``unique_specs(self._name)``) for the shapes SQL cannot express. Non-unique
        indexes keep ``collection`` as a leading key column (a pure read
        optimisation, no cross-collection semantics). For a spec with NO
        ``partialFilterExpression`` this index (plus the ``IntegrityError`` →
        ``DuplicateKeyError`` translation at every write call site) is the sole,
        authoritative enforcer, so :meth:`_check_unique` does not re-scan the
        collection for those. Index creation is still best-effort here (a
        malformed expression must never break app bring-up):
        for the plain unique-index shapes the tree actually uses this always
        succeeds (see ``ensure_unique_index``'s create-then-verify contract in
        ``index_verification.py``, which is how a deployment finds out loud if it
        somehow didn't); a swallowed failure would only silently lose enforcement
        for a shape unusual enough to fail this DDL, which is why the
        ``partialFilterExpression`` path keeps its Python fallback regardless of
        what this method manages to build.
        """
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", index_name):
            raise ValueError(f"unsafe SQL index name {index_name!r}")
        invalid_fields = [field for field in fields if not _PUSHDOWN_KEY_RE.fullmatch(field)]
        if invalid_fields:
            raise ValueError(f"SQL JSON indexes require top-level identifier fields: {invalid_fields!r}")
        if self._is_postgresql:
            exprs = [f"(doc ->> '{field}')" for field in fields]
        else:
            exprs = [f"json_extract(doc, '$.{field}')" for field in fields]
        # ``collection`` is an internal, controlled namespace literal (e.g.
        # ``sessions`` / ``session_events``), never user input; embed it as a quoted
        # SQL string literal since a partial-index WHERE clause cannot be parameterised.
        coll_literal = "'" + self._name.replace("'", "''") + "'"
        where_parts = [f"collection = {coll_literal}"]
        if partial_filter:
            where_parts.extend(self._partial_index_predicates(partial_filter))
        if unique:
            ddl = (
                f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} "
                f"ON {DocumentRow.__tablename__} ({', '.join(exprs)}) "
                f"WHERE {' AND '.join(where_parts)}"
            )
        else:
            ddl = (
                f"CREATE INDEX IF NOT EXISTS {index_name} "
                f"ON {DocumentRow.__tablename__} (collection, {', '.join(exprs)})"
            )
        from .engine import get_engine

        engine = get_engine(self._db_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(text(ddl))
        except Exception:
            # DDL is advisory here (do not let an unsupported expression-index
            # shape abort startup); for a plain (non-partial) unique spec this
            # index is the actual enforcer, but ensure_unique_index()'s
            # create-then-verify contract is what turns a real failure here into
            # a loud RuntimeError at bring-up rather than a silent gap.
            return

    def _json_string_value(self, key: str) -> Any:
        """SQL expression for a top-level JSON string value."""
        if self._is_postgresql:
            return DocumentRow.doc[key].as_string()
        return func.json_extract(DocumentRow.doc, f"$.{key}")

    def _json_value_type(self, key: str) -> Any:
        """SQL expression returning the active dialect's JSON type name."""
        if self._is_postgresql:
            return func.jsonb_typeof(DocumentRow.doc[key])
        return func.json_type(DocumentRow.doc, f"$.{key}")

    def _json_array_contains(self, key: str, value: str) -> Any:
        """Rows whose ``key`` is an array holding ``value``.

        Mongo's ``{field: "x"}`` also matches a stored array containing "x", and
        that half of the predicate has to be expressed in a way an index can
        serve. ``jsonb_typeof(...) = 'array'`` cannot be: it is a function of
        every row, so ORing it beside the equality turns an index scan into a
        scan of the whole collection — one session lookup measures 476ms
        against 0.07ms, reading 3,164 rows to return one.

        Containment is both indexable, through the GIN index on ``doc``, and
        narrower: it demands the element be present rather than admitting every
        array-valued row for the Python matcher to reject. SQLite has no
        containment operator and keeps the type test; its deployments are
        development-scale, where the scan is affordable.
        """
        if self._is_postgresql:
            # The column is declared JSON with a JSONB variant, and a variant
            # changes the DDL, not the comparator: `.contains()` resolves
            # against JSON, which has no containment, and compiles to a LIKE
            # over the document's text. Emit the operator and type the bind, so
            # what reaches PostgreSQL is `doc @> $1::jsonb`.
            return DocumentRow.doc.op("@>", is_comparison=True)(
                literal({key: [value]}, JSONB)
            )
        return self._json_value_type(key) == "array"

    def _partial_index_predicates(self, partial_filter: dict[str, Any]) -> list[str]:
        """Compile the one partial-index shape used by AstraBox.

        Supporting arbitrary Mongo predicates here would create a second query
        compiler. The repository currently needs only ``$type: string``; any
        future shape fails loudly until it has a tested SQL translation.
        """
        predicates: list[str] = []
        for field, condition in partial_filter.items():
            if not _PUSHDOWN_KEY_RE.fullmatch(field) or condition != {"$type": "string"}:
                raise UnsupportedMongoOperator(
                    "SQL partial indexes support only top-level {$type: 'string'} "
                    f"predicates, got {partial_filter!r}"
                )
            if self._is_postgresql:
                predicates.append(f"jsonb_typeof(doc -> '{field}') = 'string'")
            else:
                predicates.append(f"json_type(doc, '$.{field}') = 'text'")
        return predicates


class _SortKey:
    """Wrapper making mixed/absent values orderable (absent + cross-type → smallest).

    Mongo sorts absent fields before present ones and has a fixed BSON type order;
    the repos only ever sort homogeneously-typed comparable fields (timestamps,
    seq ints), so this only needs to (a) place :data:`MISSING` first and (b) avoid
    a ``TypeError`` when a stray ``None`` meets a string.
    """

    __slots__ = ("value", "_rank")

    def __init__(self, value: Any) -> None:
        self.value = value
        self._rank = self._type_rank(value)

    @staticmethod
    def _type_rank(value: Any) -> int:
        if value is _MISSING_SENTINEL or value is None:
            return 0
        if isinstance(value, bool):
            return 1
        if isinstance(value, (int, float)):
            return 2
        if isinstance(value, str):
            return 3
        return 4

    def __lt__(self, other: "_SortKey") -> bool:
        if self._rank != other._rank:
            return self._rank < other._rank
        if self._rank in (0,):
            return False
        try:
            return bool(self.value < other.value)
        except TypeError:
            return False


# --------------------------------------------------------------------------- #
# Unique-index registry — process-wide spec store for create_index(unique=True) #
# --------------------------------------------------------------------------- #
class _IndexRegistry:
    """Remembers ``create_index(unique=True)`` key-specs per collection.

    Specs are ``(fields, partialFilterExpression)``. They drive
    :meth:`AsyncCollection._check_unique`, which is the *authoritative* uniqueness
    enforcement (the SQLite expression index is an optimisation that may not be
    creatable for every shape). Process-wide because index creation and the writes
    they constrain may run against the same logical database from different
    sessions.
    """

    def __init__(self) -> None:
        self._unique: dict[str, list[tuple[tuple[str, ...], dict[str, Any] | None]]] = {}
        self._specs: dict[str, dict[str, dict[str, Any]]] = {}

    def register_unique(
        self,
        collection: str,
        fields: list[str],
        partial: dict[str, Any] | None,
    ) -> None:
        bucket = self._unique.setdefault(collection, [])
        spec = (tuple(fields), partial)
        if spec not in bucket:
            bucket.append(spec)

    def unique_specs(
        self, collection: str
    ) -> list[tuple[tuple[str, ...], dict[str, Any] | None]]:
        return self._unique.get(collection, [])

    def forget_spec(self, collection: str, name: str) -> None:
        """Forget a dropped index, including its Python-side unique enforcement."""
        spec = self._specs.get(collection, {}).pop(name, None)
        if spec is None or not spec.get("unique"):
            return
        fields = tuple(str(field) for field in (spec.get("key") or {}))
        bucket = self._unique.get(collection)
        if bucket is None:
            return
        self._unique[collection] = [
            entry for entry in bucket if entry[0] != fields
        ]

    def register_spec(
        self,
        collection: str,
        name: str,
        fields: list[str],
        *,
        unique: bool,
        sparse: bool,
    ) -> None:
        """Remember an index's pymongo-shaped spec so ``list_indexes`` can report it."""
        bucket = self._specs.setdefault(collection, {})
        bucket[name] = {
            "name": name,
            "key": {field: 1 for field in fields},
            "unique": bool(unique),
            "sparse": bool(sparse),
        }

    def list_specs(self, collection: str) -> list[dict[str, Any]]:
        return [dict(spec) for spec in self._specs.get(collection, {}).values()]


_INDEX_REGISTRY = _IndexRegistry()
