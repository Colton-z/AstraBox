"""Native Mongo transactions with explicit session-bound collection handles."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from ..transaction import require_uncancelled_transaction, run_owned_transaction
from . import _AUTO_RETRY_METHODS, _FILTER_METHODS, _FindCursor, _explicit_nulls, _with_explicit_nulls

T = TypeVar("T")


class _TransactionCursor:
    """A lazy cursor may only read inside its originating callback task."""

    def __init__(self, cursor: Any, transaction: MongoDocumentTransaction) -> None:
        self._cursor = cursor
        self._transaction = transaction

    def sort(self, *args: Any, **kwargs: Any) -> _TransactionCursor:
        self._transaction.check_owner()
        self._cursor.sort(*args, **kwargs)
        return self

    def skip(self, count: int) -> _TransactionCursor:
        self._transaction.check_owner()
        self._cursor.skip(count)
        return self

    def limit(self, count: int) -> _TransactionCursor:
        self._transaction.check_owner()
        self._cursor.limit(count)
        return self

    def __aiter__(self) -> _TransactionCursor:
        self._transaction.check_owner()
        return self

    async def __anext__(self) -> dict[str, Any]:
        self._transaction.check_owner()
        return await self._cursor.__anext__()

    async def to_list(self, length: int | None = None) -> list[dict[str, Any]]:
        self._transaction.check_owner()
        return await self._cursor.to_list(length)

    async def close(self) -> None:
        self._transaction.check_owner()
        await self._cursor.close()


class _TransactionCollection:
    """Pass one native session to every operation, without command-level retry."""

    def __init__(self, collection: Any, transaction: MongoDocumentTransaction) -> None:
        self._collection = collection
        self._transaction = transaction

    @property
    def name(self) -> str:
        return str(self._collection.name)

    async def lock_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        self._transaction.check_owner()
        from pymongo import ReturnDocument

        # Mongo's snapshot reads alone do not serialize reference checks with
        # concurrent writes. A fresh private value forces a real parent write,
        # acquiring the native transaction lock; it has no lease/expiry role.
        return await self._collection.find_one_and_update(
            _explicit_nulls(query),
            {"$set": {"_transaction_guard": uuid.uuid4().hex}},
            return_document=ReturnDocument.AFTER,
            session=self._transaction.session,
        )

    def find(self, *args: Any, **kwargs: Any) -> _TransactionCursor:
        self._transaction.check_owner()
        args, kwargs = _with_explicit_nulls(args, kwargs)
        cursor = self._collection.find(*args, **{**kwargs, "session": self._transaction.session})
        return _TransactionCursor(_FindCursor(cursor), self._transaction)

    async def aggregate(self, *args: Any, **kwargs: Any) -> _TransactionCursor:
        self._transaction.check_owner()
        cursor = await self._collection.aggregate(
            *args, **{**kwargs, "session": self._transaction.session},
        )
        return _TransactionCursor(cursor, self._transaction)

    def __getattr__(self, name: str) -> Any:
        if name in {"create_index", "create_indexes", "drop_index", "list_indexes"}:
            raise RuntimeError("prepare collection indexes before the document transaction")
        if name not in _AUTO_RETRY_METHODS:
            raise AttributeError(name)

        async def call(*args: Any, **kwargs: Any) -> Any:
            self._transaction.check_owner()
            if name in _FILTER_METHODS:
                args, kwargs = _with_explicit_nulls(args, kwargs)
            # The native driver retries the complete transaction after an
            # abort. Refreshing this collection/client would sever its session.
            return await getattr(self._collection, name)(
                *args, **{**kwargs, "session": self._transaction.session},
            )

        return call


class MongoDocumentTransaction:
    """One callback attempt owns one native session and its bound handles."""

    def __init__(self, database: Any, session: Any) -> None:
        self._database = database
        self.session = session
        self._owner = asyncio.current_task()
        self._active = True

    def check_owner(self) -> None:
        if not self._active:
            raise RuntimeError("document transaction has already ended")
        if asyncio.current_task() is not self._owner:
            raise RuntimeError("document transaction cannot be shared across tasks")

    def collection(self, name: str) -> _TransactionCollection:
        self.check_owner()
        return _TransactionCollection(self._database.get_collection(name), self)


async def run_transaction(
    operation: Callable[[MongoDocumentTransaction], Awaitable[T]], database: Any,
) -> T:
    """Use the driver's native abort, whole-callback retry and commit resolution."""
    from pymongo import ReadPreference
    from pymongo.read_concern import ReadConcern
    from pymongo.write_concern import WriteConcern

    async def owned() -> T:
        async with database.client.start_session() as session:
            async def attempt(native_session: Any) -> T:
                transaction = MongoDocumentTransaction(database, native_session)
                try:
                    result = await operation(transaction)
                    require_uncancelled_transaction()
                    return result
                finally:
                    transaction._active = False

            return await session.with_transaction(
                attempt,
                read_concern=ReadConcern("snapshot"),
                write_concern=WriteConcern("majority"),
                read_preference=ReadPreference.PRIMARY,
            )

    return await run_owned_transaction(owned)
