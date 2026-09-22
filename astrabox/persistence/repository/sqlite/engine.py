"""Async SQLAlchemy engine + session factory for the SQL document store.

The persistence ingress (:mod:`astrabox.persistence.repository.backend`) is *per
collection-name*: every ``get_async_collection(name)`` returns a shim bound to
one logical "collection" (a row family in one SQL table). PostgreSQL is the
deployment backend; SQLite remains available for isolated compatibility tests.
All shims share an async engine/sessionmaker per database URL.

A "collection" is a row in the single ``astrabox_documents`` table partitioned by
its ``collection`` column (see :mod:`.collection`). One table + a ``(collection,
doc_id)`` unique key keeps the *fresh-file, no-migrations* promise:
``create_all`` issues one ``CREATE TABLE IF NOT EXISTS`` and the store
works. Per-collection indexes are added lazily over JSONB expressions on
PostgreSQL and JSON expressions on SQLite.

Open deps only: ``sqlalchemy>=2.0``, ``asyncpg`` and the compatibility
``aiosqlite`` driver.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from sqlalchemy import (
    BigInteger,
    JSON,
    Integer,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from astrabox.common.logger.logger_factory import get_logger

logger = get_logger(__name__)

#: Per-connection lock-wait before ``database is locked`` surfaces. A writer that
#: took the RESERVED lock via ``BEGIN IMMEDIATE`` holds it only for the duration
#: of one read-modify-write — but those queue: under five concurrent live
#: sessions (the parallel e2e, or a small team on one box) the write queue's
#: TAIL can exceed a few seconds, and 5s surfaced as ``database is locked``
#: mid-turn. SQLite's answer to a single-writer queue is to
#: wait, not to error, so this is sized for the queue's tail rather than for
#: one claim. Deployments that outgrow the wait belong on the mongo backend.
_BUSY_TIMEOUT_MS = 30_000

#: Cap the retained WAL allocation after a successful restart/truncate.
_JOURNAL_SIZE_LIMIT_BYTES = 64 * 1024 * 1024

#: Background checkpoint cadence. PASSIVE copies whatever the oldest reader
#: allows — cheap, incremental, and OFF the commit path; TRUNCATE is attempted
#: far less often and only succeeds in a quiet moment, reclaiming the file.
_CHECKPOINT_INTERVAL_S = 5.0
_TRUNCATE_EVERY_N_PASSES = 24

#: Fixed pools avoid closing burst-only overflow connections in a cancelled
#: request task. PostgreSQL uses one fixed 30-connection pool, preserving the
#: former 20 + 10 concurrency ceiling without burst-only connections. SQLite
#: writes serialize on its RESERVED lock, so they use a short queue while WAL
#: readers use the wider pool. These are on-demand ceilings, not preallocated
#: connections.
_POOL_SIZE = {"write": 8, "read": 24}
_POSTGRES_POOL_SIZE = 30

#: The checkout timeout is shorter than ``busy_timeout``. A connection may hold
#: a legitimate lock wait, but a caller that cannot obtain any connection within
#: this bound is waiting behind the pool rather than SQLite's lock. The raised
#: error includes :func:`pool_report` diagnostics.
_POOL_TIMEOUT_S = 10.0

#: Live checkouts, keyed by pool-record identity. When the pool runs dry, the
#: pool reports only a count. Record the pool and owning task at checkout and
#: remove them at checkin so :func:`pool_report` can identify parked holders.
_CHECKOUTS: dict[int, tuple[int, float, str]] = {}

__all__ = [
    "Base",
    "DocumentRow",
    "get_engine",
    "get_sessionmaker",
    "create_all",
    "dispose_engines",
    "ensure_created",
    "is_postgresql_url",
    "pool_report",
    "resolve_database_url",
    "resolve_sqlite_url",
]


class Base(DeclarativeBase):
    """Declarative base for the single document table."""


class DocumentRow(Base):
    """One stored document, addressed by ``(collection, doc_id)``.

    The full free-form document lives in the ``doc`` JSON column and is returned
    **verbatim** at the collection boundary (so the shim is byte-identical to a
    Mongo collection). ``collection`` namespaces the row family (``sessions``,
    ``messages`` …); ``doc_id`` is the document's ``_id``
    (Mongo's primary key) — auto-generated when the inserted document omits it.

    ``seq`` is a monotonic insertion-order tiebreaker so that a Mongo query with
    no explicit sort returns rows in stable insertion order (Mongo's natural
    order), which several repos depend on implicitly.
    """

    __tablename__ = "astrabox_documents"
    __table_args__ = (
        UniqueConstraint("collection", "doc_id", name="ux_documents_collection_id"),
    )

    # PostgreSQL receives BIGSERIAL: every logical collection shares this
    # insertion-order counter, so a 32-bit sequence would be an avoidable
    # lifetime limit. SQLite needs the exact INTEGER PRIMARY KEY spelling for
    # rowid autoincrement semantics, hence the dialect variant.
    seq: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    collection: Mapped[str] = mapped_column(String, index=True, nullable=False)
    doc_id: Mapped[str] = mapped_column(String, nullable=False)
    doc: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False, default=dict
    )


# --------------------------------------------------------------------------- #
# Engine / sessionmaker cache — one WRITE + one READ engine per URL (shared by  #
# every collection shim). Writers take BEGIN IMMEDIATE (serialized RMW); readers #
# use plain deferred BEGIN so WAL keeps them lock-free alongside the writer.    #
# --------------------------------------------------------------------------- #
_ENGINES: dict[tuple[str, str], AsyncEngine] = {}
_SESSIONMAKERS: dict[tuple[str, str], async_sessionmaker[AsyncSession]] = {}
_CREATED_URLS: set[str] = set()


def resolve_database_url(db_url: str | None) -> str:
    """Resolve a configured SQL URL onto an async SQLAlchemy driver.

    PostgreSQL URLs use ``asyncpg``. SQLite conversion is retained so the
    existing isolated unit suite can keep using temporary files while runtime
    deployments use PostgreSQL.
    """
    if db_url:
        url = db_url.strip()
    else:
        from astrabox.config.settings import get_settings

        url = get_settings().resolved_db_url.strip()
    if url.startswith("sqlite:///"):
        return "sqlite+aiosqlite:///" + url[len("sqlite:///") :]
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    if url.startswith(("sqlite+aiosqlite:", "postgresql+asyncpg:")):
        return url
    raise ValueError(
        "the SQL document backend requires a postgresql+asyncpg or "
        f"sqlite+aiosqlite URL, got {url!r}; no implicit driver fallback"
    )


def is_postgresql_url(db_url: str | None) -> bool:
    """Whether ``db_url`` resolves to PostgreSQL."""
    return resolve_database_url(db_url).startswith("postgresql+asyncpg:")


def resolve_sqlite_url(db_url: str | None) -> str:
    """Resolve and validate an async SQLite URL.

    * ``None`` → :func:`astrabox.config.settings.get_settings`'s
      ``resolved_db_url`` (``sqlite+aiosqlite:///<state_dir>/astrabox.sqlite``).
    * a bare ``sqlite:///`` URL is upgraded to the async
      ``sqlite+aiosqlite:///`` driver.
    * any non-sqlite URL raises — the SQLite shim only speaks SQLite; other
      stores go through their own backend (the ``[mongo]`` extra).
    """
    url = resolve_database_url(db_url)
    if not url.startswith("sqlite+aiosqlite:"):
        raise ValueError(
            f"the SQLite collection backend requires a sqlite+aiosqlite URL, got {url!r}; "
            "use the PostgreSQL backend for PostgreSQL URLs"
        )
    return url


def _ensure_parent_dir(url: str) -> None:
    """Create the parent directory of a file-backed SQLite URL if absent.

    A ``sqlite+aiosqlite:///path/astrabox.sqlite`` URL cannot open if the parent
    directory is missing; ``:memory:`` and bare relative URLs need no directory.
    """
    prefix = "sqlite+aiosqlite:///"
    if not url.startswith(prefix):
        return
    raw_path = url[len(prefix) :]
    if not raw_path or raw_path.startswith(":memory:"):
        return
    parent = Path(raw_path).expanduser().parent
    if str(parent):
        parent.mkdir(parents=True, exist_ok=True)


def _install_sqlite_txn_discipline(engine: AsyncEngine, *, mode: str) -> None:
    """Give the engine real transaction boundaries (pysqlite's are broken).

    This is the SQLAlchemy-documented fix for pysqlite's legacy transaction
    handling, and it is load-bearing for correctness — the CAS/lease primitives
    (``find_one_and_update``, ``update_one`` with a guard filter, ``$inc``
    fencing) are read-modify-write cycles that MUST be serialized:

    * By default pysqlite emits an implicit ``BEGIN`` only just before a DML
      statement, so the SELECT that opens a read-modify-write runs OUTSIDE the
      transaction (deferred). Two workers then both read the pre-update row, both
      compute, and both write — last-writer-wins, and two claimants both "win" a
      lease that must have exactly one owner.
    * Setting the DBAPI ``isolation_level = None`` disables pysqlite's implicit
      transaction management (autocommit at the driver); the ``begin`` hook then
      owns the boundary. On the **write** engine it issues ``BEGIN IMMEDIATE``,
      taking SQLite's RESERVED write lock UP FRONT — before the opening SELECT —
      so a concurrent claimant blocks on the busy-timeout and, on acquiring the
      lock, sees the committed write: the read-modify-write is atomic.
    * The **read** engine issues a plain deferred ``BEGIN`` instead: under WAL a
      read transaction runs lock-free against a stable snapshot, so reads never
      queue behind the writer (and never contend for the RESERVED lock).

    ``busy_timeout`` and WAL are set per-connection on BOTH engines so every
    pooled connection waits on lock contention instead of erroring immediately.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_on_connect(dbapi_connection, _record):  # type: ignore[no-untyped-def]
        # DBAPI autocommit leaves transaction boundaries to the SQLAlchemy
        # ``begin`` hook. It also keeps the PRAGMAs below outside a transaction;
        # ``journal_mode`` cannot change inside one.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            # Commits never do checkpoint work. The default (auto-checkpoint
            # at 1000 pages) runs the checkpoint INSIDE whichever commit
            # crosses the threshold — and under continuous concurrent reads
            # the passive attempts keep losing to the oldest-reader mark, the
            # WAL grows without a successful checkpoint, and a commit that
            # finally advances can pay the backlog while holding the write
            # lock. The background checkpointer (see checkpoint_loop) owns that
            # work instead; journal_size_limit lets a successful restart shrink
            # the file.
            cursor.execute("PRAGMA wal_autocheckpoint=0")
            cursor.execute(f"PRAGMA journal_size_limit={_JOURNAL_SIZE_LIMIT_BYTES}")
        finally:
            cursor.close()

    if mode == "write":

        @event.listens_for(engine.sync_engine, "begin")
        def _sqlite_begin_immediate(conn):  # type: ignore[no-untyped-def]
            # Eager write-lock BEGIN so the read half of a read-modify-write is
            # already inside the write txn.
            conn.exec_driver_sql("BEGIN IMMEDIATE")

    else:

        @event.listens_for(engine.sync_engine, "begin")
        def _sqlite_begin_deferred(conn):  # type: ignore[no-untyped-def]
            # A real (deferred) txn per read gives snapshot consistency under
            # WAL without touching the write lock.
            conn.exec_driver_sql("BEGIN")

def _install_pool_diagnostics(engine: AsyncEngine) -> None:
    """Track checked-out connections for every SQL backend."""
    pool_id = id(engine.pool)

    @event.listens_for(engine.sync_engine, "checkout")
    def _on_checkout(_dbapi_connection, record, _proxy):  # type: ignore[no-untyped-def]
        _CHECKOUTS[id(record)] = (pool_id, time.monotonic(), _current_holder())

    @event.listens_for(engine.sync_engine, "checkin")
    def _on_checkin(_dbapi_connection, record):  # type: ignore[no-untyped-def]
        _CHECKOUTS.pop(id(record), None)

    @event.listens_for(engine.sync_engine, "close")
    def _on_close(_dbapi_connection, record):  # type: ignore[no-untyped-def]
        # A connection the pool discards never checks in, so without this the
        # entry would outlive it and be reported forever as an ancient holder.
        _CHECKOUTS.pop(id(record), None)


def _current_holder() -> str:
    """Name the coroutine holding a connection, cheaply and best-effort.

    The task's own repr is the diagnostic: a leaked checkout is a task parked
    somewhere it should not be, and its coroutine qualname says where.
    """
    try:
        task = asyncio.current_task()
    except RuntimeError:  # no running loop (sync setup path)
        return "no-loop"
    if task is None:
        return "no-task"
    coro = getattr(task, "get_coro", lambda: None)()
    return f"{task.get_name()}:{getattr(coro, '__qualname__', '?')}"


def pool_report(*, holders: int = 5) -> str:
    """One line describing every live pool and its longest-held connections.

    Slow-persistence diagnostics use it to report pool occupancy and the
    coroutines holding connections longest.
    """
    now = time.monotonic()
    parts: list[str] = []
    for (_url, mode), engine in _ENGINES.items():
        pool = engine.pool
        pool_id = id(pool)
        held = sorted(
            (
                (now - at, holder)
                for entry_pool_id, at, holder in _CHECKOUTS.values()
                if entry_pool_id == pool_id
            ),
            reverse=True,
        )[:holders]
        oldest = ",".join(f"{age:.1f}s/{holder}" for age, holder in held) or "-"
        # Composed rather than taken from ``pool.status()``: that string leads
        # with an overflow counter which, with overflow disabled, is always a
        # negative number that means nothing — and this line gets read in a
        # hurry, by someone whose server has stopped answering.
        size = getattr(pool, "size", lambda: -1)()
        idle = getattr(pool, "checkedin", lambda: -1)()
        out = getattr(pool, "checkedout", lambda: -1)()
        parts.append(f"{mode}(size={size} idle={idle} out={out}) held=[{oldest}]")
    return " | ".join(parts) or "no-pools"


def _pool_kwargs(url: str, mode: str) -> dict[str, Any]:
    """Pool arguments for a file-backed URL; nothing for ``:memory:``.

    An in-memory database resolves to a single-connection pool that accepts no
    sizing arguments at all, and passing them is a hard error rather than a
    no-op — so the shape is chosen by the URL, not by a flag.
    """
    if ":memory:" in url:
        return {}
    if url.startswith("postgresql+asyncpg:"):
        return {
            "pool_size": _POSTGRES_POOL_SIZE,
            "max_overflow": 0,
            "pool_timeout": _POOL_TIMEOUT_S,
            "pool_pre_ping": True,
        }
    return {
        "pool_size": _POOL_SIZE[mode],
        "max_overflow": 0,
        "pool_timeout": _POOL_TIMEOUT_S,
    }


def get_engine(db_url: str | None = None, *, mode: str = "write") -> AsyncEngine:
    """Return (creating once per URL×mode) the shared async engine.

    ``mode="write"`` (default) serializes transactions with ``BEGIN IMMEDIATE``
    — every mutation and CAS read-modify-write goes through it. ``mode="read"``
    is the WAL snapshot-read engine for the pure-read paths.
    """
    if mode not in ("write", "read"):
        raise ValueError(f"unknown SQL engine mode {mode!r} (write|read)")
    url = resolve_database_url(db_url)
    postgres = is_postgresql_url(url)
    # PostgreSQL provides MVCC and row locks through one normal pool. SQLite
    # needs distinct engines because their BEGIN hooks intentionally differ.
    key = (url, "shared" if postgres else mode)
    engine = _ENGINES.get(key)
    if engine is None:
        if not postgres:
            _ensure_parent_dir(url)
        engine = create_async_engine(url, future=True, **_pool_kwargs(url, mode))
        if not postgres:
            # The transaction hooks provide BEGIN IMMEDIATE for SQLite writes
            # and deferred snapshots for reads. PostgreSQL uses native MVCC and
            # SELECT FOR UPDATE in the collection adapter instead.
            _install_sqlite_txn_discipline(engine, mode=mode)
        _install_pool_diagnostics(engine)
        _ENGINES[key] = engine
    return engine


def _run_checkpoint(db_path: str, mode: str) -> tuple[int, int, int]:
    """One checkpoint pass on a DEDICATED raw connection.

    Deliberately not the write engine: its transaction discipline BEGINs
    IMMEDIATE on connect-begin, and a checkpoint cannot run inside its own
    connection's transaction — the first wired deployment failed every pass
    with "database table is locked" for exactly that reason. A bare stdlib
    connection with autocommit (isolation_level=None) and a short busy_timeout
    is the whole requirement; opened and closed per pass so the checkpointer
    can never pin the WAL it exists to drain.
    """
    import sqlite3

    conn = sqlite3.connect(db_path, timeout=1.0, isolation_level=None)
    try:
        row = conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
    finally:
        conn.close()
    busy, log_pages, done_pages = (int(row[0]), int(row[1]), int(row[2])) if row else (1, -1, -1)
    return busy, log_pages, done_pages


async def checkpoint_loop(db_url: str | None = None) -> None:
    """Own the WAL checkpoint work, forever, off the commit path.

    Runs PASSIVE every few seconds — each pass copies as much as the oldest
    live reader permits, so the WAL stays near-empty instead of accumulating
    into a backlog some commit must one day swallow. Every Nth pass attempts
    TRUNCATE, which succeeds only in a reader-free moment and shrinks the file
    (journal_size_limit bounds what a restart leaves behind).

    Uses its own connection so a checkpoint can never sit inside the write
    engine's transaction discipline. Cancellation is the only exit; errors log
    and back off rather than kill the loop — a missed pass is recoverable, a
    dead checkpointer recreates the unbounded-WAL failure this exists to end.
    """
    url = resolve_sqlite_url(db_url)
    db_path = url.split("///", 1)[-1]
    passes = 0
    while True:
        try:
            await asyncio.sleep(_CHECKPOINT_INTERVAL_S)
            passes += 1
            mode = "TRUNCATE" if passes % _TRUNCATE_EVERY_N_PASSES == 0 else "PASSIVE"
            busy, log_pages, done_pages = await asyncio.to_thread(
                _run_checkpoint, db_path, mode
            )
            if mode == "TRUNCATE":
                if busy:
                    # Busy is expected under load; passive keeps draining.
                    logger.debug("wal TRUNCATE busy (readers active); passive continues")
                else:
                    logger.info(
                        "wal TRUNCATE reclaimed the file (log=%s checkpointed=%s)",
                        log_pages,
                        done_pages,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("wal checkpoint pass failed; backing off", exc_info=True)
            await asyncio.sleep(_CHECKPOINT_INTERVAL_S * 4)


def get_sessionmaker(
    db_url: str | None = None, *, mode: str = "write"
) -> async_sessionmaker[AsyncSession]:
    """Return (creating once per URL×mode) the shared async session factory."""
    url = resolve_database_url(db_url)
    key = (url, mode)
    maker = _SESSIONMAKERS.get(key)
    if maker is None:
        maker = async_sessionmaker(get_engine(url, mode=mode), expire_on_commit=False)
        _SESSIONMAKERS[key] = maker
    return maker


async def create_all(db_url: str | None = None) -> None:
    """Migration-free bootstrap: ``CREATE TABLE IF NOT EXISTS`` for the document table.

    This is the analogue of the Mongo ``ensure_indexes`` pass and the
    *fresh-.sqlite-works-with-no-migrations* guarantee. Idempotent and cheap; safe
    to call at every app start and is also called lazily on first collection use.

    WAL + busy-timeout are set per-connection by the ``connect`` hook in
    :func:`_install_sqlite_txn_discipline` (so every pooled connection gets them,
    not just this one). They are not set here because ``engine.begin()`` runs
    as ``BEGIN IMMEDIATE`` and ``journal_mode`` cannot be changed inside a
    transaction.
    """
    url = resolve_database_url(db_url)
    engine = get_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Expression index for the universal scoping key. Hot reads select one
        # session's rows within a collection, so this avoids a full collection
        # scan as the database grows. It pairs with the json_extract equality
        # pushdown in AsyncCollection._load_docs.
        if is_postgresql_url(url):
            await conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_documents_collection_session_id "
                "ON astrabox_documents (collection, (doc ->> 'session_id'))"
            )
            # The equality index above only decides a read when the WHOLE
            # predicate can use an index. Every pushdown is `the value equals x
            # OR the field is an array holding x`, and without this the second
            # half forces a scan of the collection whatever the first half
            # could have done: one session lookup measures 476ms against
            # 0.07ms, reading 3,164 rows to return one. With both halves
            # indexed the planner takes their bitmap union and touches six
            # pages.
            await conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_documents_doc_gin "
                "ON astrabox_documents USING gin (doc jsonb_path_ops)"
            )
        else:
            await conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_documents_collection_session_id "
                "ON astrabox_documents "
                "(collection, json_extract(doc, '$.session_id'))"
            )
    _CREATED_URLS.add(url)


async def ensure_created(db_url: str | None = None) -> None:
    """Idempotent, *cached* ``create_all`` — runs the DDL at most once per URL.

    Hot-path callers (every ``get_async_collection``) use this so the first call
    creates the schema and subsequent calls are a set-membership check, not a
    round-trip to SQLite.
    """
    url = resolve_database_url(db_url)
    if url in _CREATED_URLS:
        return
    await create_all(url)


async def dispose_engines(db_url: str | None = None) -> None:
    """Dispose cached SQL pools, normally during application shutdown."""
    resolved = resolve_database_url(db_url) if db_url is not None else None
    keys = [key for key in _ENGINES if resolved is None or key[0] == resolved]
    engines = {_ENGINES.pop(key) for key in keys}
    for key in list(_SESSIONMAKERS):
        if resolved is None or key[0] == resolved:
            _SESSIONMAKERS.pop(key, None)
    if resolved is None:
        _CREATED_URLS.clear()
    else:
        _CREATED_URLS.discard(resolved)
    for engine in engines:
        await engine.dispose()
