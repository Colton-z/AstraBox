"""Run statements against the shared engine.

Every repository statement goes through these functions:

* :func:`fetch_one`, :func:`fetch_all`, :func:`fetch_value` and :func:`execute`
  run one statement in autocommit, which is one round trip.
* :func:`transaction` runs a callable inside ``BEGIN … COMMIT`` for work that
  must be atomic across statements.

Each call runs in its own task that the caller's cancellation cannot interrupt.
A request cancelled while asyncpg is mid-query would otherwise cancel the
driver's cleanup as well, and the connection would be dropped without being
returned to the pool. The caller still observes ``CancelledError`` immediately;
the statement finishes and returns its connection in the background.

Constraint violations surface as :class:`UniqueViolation` and
:class:`ForeignKeyViolation`, carrying the constraint name, so repositories can
tell "already exists" from a failure.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import Executable

from astrabox.common.logger.logger_factory import get_logger
from astrabox.persistence.sql.engine import get_engine, pool_report

logger = get_logger(__name__)

T = TypeVar("T")

#: Statements slower than this are logged with the pool state; a slow statement
#: on a healthy database usually means the pool is exhausted.
SLOW_STATEMENT_S = 1.0

_UNIQUE_VIOLATION = "23505"
_FOREIGN_KEY_VIOLATION = "23503"


class UniqueViolation(Exception):
    """A row with the same unique key already exists."""

    def __init__(self, constraint: str | None) -> None:
        super().__init__(f"unique constraint {constraint!r} violated")
        self.constraint = constraint


class ForeignKeyViolation(Exception):
    """The statement referenced a row that does not exist, or removed a referenced row."""

    def __init__(self, constraint: str | None) -> None:
        super().__init__(f"foreign key constraint {constraint!r} violated")
        self.constraint = constraint


async def fetch_one(statement: Executable) -> dict[str, Any] | None:
    """The first row ``statement`` returns, or ``None``."""

    async def work(conn: AsyncConnection) -> dict[str, Any] | None:
        row = (await conn.execute(statement)).first()
        return dict(row._mapping) if row is not None else None

    return await _run(work, transactional=False)


async def fetch_all(statement: Executable) -> list[dict[str, Any]]:
    """Every row ``statement`` returns."""

    async def work(conn: AsyncConnection) -> list[dict[str, Any]]:
        return [dict(row._mapping) for row in (await conn.execute(statement)).all()]

    return await _run(work, transactional=False)


async def fetch_value(statement: Executable) -> Any:
    """The first column of the first row, or ``None``."""

    async def work(conn: AsyncConnection) -> Any:
        return (await conn.execute(statement)).scalar()

    return await _run(work, transactional=False)


async def execute(statement: Executable) -> int:
    """Run ``statement``; the number of rows it affected."""

    async def work(conn: AsyncConnection) -> int:
        return int((await conn.execute(statement)).rowcount)

    return await _run(work, transactional=False)


async def transaction(work: Callable[[AsyncConnection], Awaitable[T]]) -> T:
    """Run ``work`` on one connection inside a transaction.

    The transaction commits if ``work`` returns; if ``work`` raises, nothing it
    wrote is kept. Statements inside ``work`` use the connection it is given.
    """
    return await _run(work, transactional=True)


async def _run(work: Callable[[AsyncConnection], Awaitable[T]], *, transactional: bool) -> T:
    task = asyncio.ensure_future(_execute(work, transactional=transactional))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        task.add_done_callback(_log_detached_outcome)
        raise


async def _execute(work: Callable[[AsyncConnection], Awaitable[T]], *, transactional: bool) -> T:
    engine = get_engine()
    started = time.monotonic()
    try:
        if transactional:
            async with engine.begin() as conn:
                return await work(conn)
        async with engine.connect() as conn:
            autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
            return await work(autocommit)
    except IntegrityError as exc:
        raise _translated(exc) from exc
    finally:
        elapsed = time.monotonic() - started
        if elapsed >= SLOW_STATEMENT_S:
            logger.warning("slow database operation %.2fs %s", elapsed, pool_report())


def _translated(exc: IntegrityError) -> Exception:
    original = getattr(exc, "orig", None)
    sqlstate = getattr(original, "sqlstate", None) or getattr(
        getattr(original, "__cause__", None), "sqlstate", None
    )
    constraint = getattr(original, "constraint_name", None) or getattr(
        getattr(original, "__cause__", None), "constraint_name", None
    )
    if sqlstate == _UNIQUE_VIOLATION:
        return UniqueViolation(constraint)
    if sqlstate == _FOREIGN_KEY_VIOLATION:
        return ForeignKeyViolation(constraint)
    return exc


def _log_detached_outcome(task: asyncio.Task[Any]) -> None:
    """Record how a statement whose caller was cancelled finished."""
    if task.cancelled():
        logger.warning("database operation cancelled after its caller left")
        return
    error = task.exception()
    if error is not None:
        logger.warning("database operation failed after its caller left: %s", error)
