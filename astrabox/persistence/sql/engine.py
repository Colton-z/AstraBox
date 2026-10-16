"""The PostgreSQL engine every repository shares.

AstraBox keeps its records in PostgreSQL through SQLAlchemy's asyncio extension
and the asyncpg driver. Each event loop has a fixed repository pool and a
separate runtime-lock pool with the same limit. :mod:`astrabox.persistence.sql.ops`
runs repository statements through the first pool. asyncpg connections belong
to the loop that opened them, so engines are never shared across loops.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DisconnectionError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

#: The limit for each pool. The runtime-lock pool permits this many additional
#: connections without consuming repository slots. Overflow is disabled: closing
#: overflow connections could otherwise fall to a cancelled caller.
POOL_SIZE = 30

#: A caller that cannot obtain a connection within this bound is waiting behind
#: the pool rather than the database; the error names the holders
#: (:func:`pool_report`).
POOL_TIMEOUT_S = 10.0

#: A connection idle longer than this is pinged before it is handed out; one
#: used more recently is not, so a busy path pays no ping. pgxpool pings
#: connections idle for over one second, and HikariCP skips validation inside
#: its alive-bypass window, for the same reason. The pool hands out the most
#: recently used connection first, which keeps a busy path on connections that
#: need no ping.
PING_AFTER_IDLE_S = 1.0

#: asyncpg prepares every statement and caches the plan per connection. Each
#: repository statement is fixed SQL text with bound values, so the cache holds
#: all of them once warm.
STATEMENT_CACHE_SIZE = 1024

_ENGINES: dict[asyncio.AbstractEventLoop, AsyncEngine] = {}
_RUNTIME_LOCK_ENGINES: dict[asyncio.AbstractEventLoop, AsyncEngine] = {}

#: Live checkouts, keyed by pool-record identity, with the checkout time and
#: the task that holds the connection. A pool that runs dry reports only a
#: count; this names who holds it.
_CHECKOUTS: dict[int, tuple[float, str]] = {}


def database_url(url: str | None = None) -> str:
    """The configured PostgreSQL URL, on the asyncpg driver.

    Raises ``ValueError`` for any other database: PostgreSQL is the only store.
    """
    if url is None:
        from astrabox.config.settings import get_settings

        url = get_settings().resolved_db_url
    raw = url.strip()
    for prefix in ("postgres://", "postgresql://"):
        if raw.startswith(prefix):
            raw = "postgresql+asyncpg://" + raw[len(prefix):]
            break
    if not raw.startswith("postgresql+asyncpg://"):
        raise ValueError(
            "AstraBox stores its records in PostgreSQL; ASTRABOX_DB_URL must be a "
            f"postgresql:// URL, got {redacted_url(raw)!r}"
        )
    return raw


def redacted_url(url: str) -> str:
    """``url`` with its password hidden, for logs and errors."""
    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001 - an unparseable URL is shown without credentials
        return url.split("@", 1)[-1]


def get_engine(*, runtime_lock: bool = False) -> AsyncEngine:
    """The running loop's repository or separately bounded runtime-lock engine."""
    loop = asyncio.get_running_loop()
    engines = _RUNTIME_LOCK_ENGINES if runtime_lock else _ENGINES
    engine = engines.get(loop)
    if engine is None:
        engine = create_async_engine(
            database_url(),
            pool_size=POOL_SIZE,
            max_overflow=0,
            pool_timeout=POOL_TIMEOUT_S,
            pool_use_lifo=True,
            connect_args={
                "prepared_statement_cache_size": STATEMENT_CACHE_SIZE,
                "server_settings": {"application_name": "astrabox"},
            },
        )
        _install_idle_ping(engine)
        _install_pool_diagnostics(engine)
        engines[loop] = engine
    return engine


async def dispose_engine() -> None:
    """Close both pools for the running loop; next use creates them again."""
    loop = asyncio.get_running_loop()
    for engines in (_ENGINES, _RUNTIME_LOCK_ENGINES):
        engine = engines.pop(loop, None)
        if engine is not None:
            await engine.dispose()


def _install_idle_ping(engine: AsyncEngine) -> None:
    """Ping a connection before reuse only when it has sat idle.

    A failed ping raises ``DisconnectionError``; the pool discards that
    connection and checks out another.
    """

    @event.listens_for(engine.sync_engine, "checkin")
    def _on_checkin(_dbapi_connection: Any, record: Any) -> None:
        record.info["astrabox_idle_since"] = time.monotonic()

    @event.listens_for(engine.sync_engine, "checkout")
    def _on_checkout(dbapi_connection: Any, record: Any, _proxy: Any) -> None:
        idle_since = record.info.get("astrabox_idle_since")
        if idle_since is None or time.monotonic() - idle_since <= PING_AFTER_IDLE_S:
            return
        try:
            dbapi_connection.ping()
        except Exception as exc:
            raise DisconnectionError("an idle PostgreSQL connection failed its ping") from exc


def _install_pool_diagnostics(engine: AsyncEngine) -> None:
    @event.listens_for(engine.sync_engine, "checkout")
    def _on_checkout(_dbapi_connection: Any, record: Any, _proxy: Any) -> None:
        _CHECKOUTS[id(record)] = (time.monotonic(), _current_holder())

    @event.listens_for(engine.sync_engine, "checkin")
    def _on_checkin(_dbapi_connection: Any, record: Any) -> None:
        _CHECKOUTS.pop(id(record), None)

    @event.listens_for(engine.sync_engine, "close")
    def _on_close(_dbapi_connection: Any, record: Any) -> None:
        # A connection the pool discards never checks in; without this its entry
        # would be reported forever as the oldest holder.
        _CHECKOUTS.pop(id(record), None)


def _current_holder() -> str:
    """The task holding a connection, named by its coroutine."""
    try:
        task = asyncio.current_task()
    except RuntimeError:
        return "no-loop"
    if task is None:
        return "no-task"
    coro = task.get_coro()
    return f"{task.get_name()}:{getattr(coro, '__qualname__', '?')}"


def pool_report(*, holders: int = 5) -> str:
    """One line on the running loop's pool and its longest-held connections."""
    try:
        engine = _ENGINES.get(asyncio.get_running_loop())
    except RuntimeError:
        engine = None
    if engine is None:
        return "no-pool"
    now = time.monotonic()
    held = sorted(((now - at, holder) for at, holder in _CHECKOUTS.values()), reverse=True)
    oldest = ",".join(f"{age:.1f}s/{holder}" for age, holder in held[:holders]) or "-"
    pool: Any = engine.pool
    # Composed rather than taken from pool.status(): with overflow disabled
    # that string leads with a meaningless negative overflow counter.
    return (
        f"pool(size={pool.size()} idle={pool.checkedin()} out={pool.checkedout()}) "
        f"held=[{oldest}]"
    )
