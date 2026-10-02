"""An explicit owner for atomic operations across SQL document collections."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from ..transaction import require_uncancelled_transaction, run_owned_transaction
from .collection import AsyncCollection
from .engine import ensure_created, get_sessionmaker

T = TypeVar("T")


class SqlDocumentTransaction:
    """Collections share one session and must stay in its owning callback task.

    Prepare indexes before entering the callback. Keep network calls and other
    external side effects outside it; a database transaction cannot undo them.
    """

    def __init__(self, session: AsyncSession, db_url: str | None) -> None:
        self.session = session
        self._db_url = db_url
        self._owner = asyncio.current_task()
        self._active = True

    def check_owner(self) -> None:
        if not self._active:
            raise RuntimeError("document transaction has already ended")
        if asyncio.current_task() is not self._owner:
            raise RuntimeError("document transaction cannot be shared across tasks")

    def collection(self, name: str) -> AsyncCollection:
        self.check_owner()
        return AsyncCollection(name, self._db_url, transaction=self)


async def run_transaction(
    operation: Callable[[SqlDocumentTransaction], Awaitable[T]],
    db_url: str | None = None,
) -> T:
    """Commit callback writes together; callback failure rolls them all back.

    Individual bound operations stay in this owner's task. Cancellation stops
    that task once, then joins its rollback/connection cleanup even under
    AnyIO level cancellation. Cancellation during commit can have an uncertain
    outcome, as with any database write; it must not trigger a blind retry.
    Ordinary unbound collection operations retain their existing independent
    transaction and cancellation behavior.
    """

    async def owned() -> T:
        await ensure_created(db_url)
        async with get_sessionmaker(db_url, mode="write")() as session:
            transaction = SqlDocumentTransaction(session, db_url)
            try:
                async with session.begin():
                    result = await operation(transaction)
                    require_uncancelled_transaction()
                    return result
            finally:
                transaction._active = False

    return await run_owned_transaction(owned)
