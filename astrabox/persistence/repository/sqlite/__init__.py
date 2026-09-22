"""Optional SQLite compatibility backend and collection-shim subpackage.

Re-exports the pieces the DAL ingress (``..backend``) wires together: the
``AsyncCollection`` Mongo-collection shim, the engine/session factory, and the
migration-free ``create_all`` bootstrap. Importing this package pulls in only
``sqlalchemy`` + ``aiosqlite`` (both core deps) — never ``pymongo``.

This is also the ``sqlite`` entry-point target at the
``astrabox.providers.repository`` group. Alongside the re-exports above it
exposes the same minimal module-level free-function surface as the ``mongo``
sibling (:func:`get_async_collection`, :func:`is_transient_error`,
:func:`close_for_current_loop`) so ``..backend`` can dispatch to either
provider uniformly by module, never by a hardcoded backend name.
"""

from __future__ import annotations

from typing import Any

from .collection import AsyncCollection, SqliteCursor
from .engine import (
    Base,
    DocumentRow,
    create_all,
    ensure_created,
    get_engine,
    get_sessionmaker,
    resolve_sqlite_url,
)
from .query import UnsupportedMongoOperator

__all__ = [
    "AsyncCollection",
    "SqliteCursor",
    "Base",
    "DocumentRow",
    "create_all",
    "ensure_created",
    "get_engine",
    "get_sessionmaker",
    "resolve_sqlite_url",
    "UnsupportedMongoOperator",
    "get_async_collection",
    "is_transient_error",
    "close_for_current_loop",
]


# --------------------------------------------------------------------------- #
# Uniform provider surface (mirrors ``..mongo``'s shape)                        #
# --------------------------------------------------------------------------- #
async def get_async_collection(collection_name: str) -> Any:
    """Return an :class:`AsyncCollection` bound to ``collection_name``.

    The sqlite half of the uniform ``get_async_collection(name)`` surface — the
    single-file store needs no per-call connection setup beyond the idempotent,
    cached :func:`ensure_created`.
    """
    await ensure_created(None)
    return AsyncCollection(collection_name, None)


def is_transient_error(exc: Exception) -> bool:
    """``database is locked`` is sqlite's ONE transient class; everything else
    is a real error and must surface immediately.

    Under WAL the lock error does not mean the database is broken — it means
    the single-writer queue's tail outlived ``busy_timeout`` (SQLite's own
    answer to a busy writer is to wait). A pathological hold is bounded (a
    severed connection is reaped in seconds), so the op wrapper's retries ride
    it out instead of failing a session-startup worker and leaving its session
    in CREATING.
    """
    text = str(exc).lower()
    return type(exc).__name__ == "OperationalError" and (
        "database is locked" in text or "database table is locked" in text
    )


async def close_for_current_loop(reason: str = "event_loop_shutdown") -> None:
    """No-op: the SQLite engine is process-global, disposed at process exit.

    Name + signature kept for parity with mongo's per-loop client teardown so
    ``..backend`` can call either provider's shutdown hook uniformly.
    """
    _ = reason
