"""PostgreSQL persistence provider.

The repositories use a Mongo-shaped document collection contract. PostgreSQL
implements that contract with the shared SQLAlchemy adapter and stores documents
as JSONB in ``astrabox_documents``. Mutating reads take row locks, so guarded
updates and lease claims remain atomic across server processes.
"""

from __future__ import annotations

from sqlalchemy.exc import (
    DBAPIError,
    DisconnectionError,
    InterfaceError,
    OperationalError,
    TimeoutError as SQLAlchemyTimeoutError,
)

from ..sqlite.collection import AsyncCollection, SqliteCursor
from ..sqlite.engine import (
    Base,
    DocumentRow,
    create_all,
    dispose_engines,
    ensure_created,
    get_engine,
    get_sessionmaker,
    resolve_database_url,
)
from ..sqlite.query import UnsupportedMongoOperator

SqlCursor = SqliteCursor

__all__ = [
    "AsyncCollection",
    "Base",
    "DocumentRow",
    "SqlCursor",
    "UnsupportedMongoOperator",
    "close_for_current_loop",
    "create_all",
    "ensure_created",
    "get_async_collection",
    "get_engine",
    "get_sessionmaker",
    "is_transient_error",
    "resolve_database_url",
]


async def get_async_collection(collection_name: str) -> AsyncCollection:
    """Return a collection bound to the configured PostgreSQL database."""
    await ensure_created(None)
    return AsyncCollection(collection_name, None)


def is_transient_error(exc: Exception) -> bool:
    """Classify connection loss, pool timeout and retryable PG transactions."""
    if isinstance(
        exc,
        (DisconnectionError, InterfaceError, OperationalError, SQLAlchemyTimeoutError),
    ):
        return True
    if isinstance(exc, DBAPIError) and bool(exc.connection_invalidated):
        return True
    current: BaseException | None = exc
    retryable_names = {
        "CannotConnectNowError",
        "ConnectionDoesNotExistError",
        "ConnectionFailureError",
        "DeadlockDetectedError",
        "SerializationError",
        "TooManyConnectionsError",
    }
    while current is not None:
        if type(current).__name__ in retryable_names:
            return True
        current = current.__cause__ or current.__context__
    return False


async def close_for_current_loop(reason: str = "event_loop_shutdown") -> None:
    """Close PostgreSQL pools during the normal service shutdown path."""
    _ = reason
    await dispose_engines()
