"""SQLite writes release their locks even when the caller is cancelled.

The cancellation contract has two layers, and both are exercised here:

* every write method is ``@_cancel_shielded``: the caller still observes its
  ``CancelledError`` immediately, but the transaction runs to completion in the
  background and releases the lock;
* ``database is locked`` is the backend's one transient error class, so the
  operation wrapper retries lock contention while surfacing other failures.
"""

from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import patch

import pytest

from astrabox.persistence.repository.sqlite import is_transient_error
from astrabox.persistence.repository.sqlite.collection import AsyncCollection


@pytest.fixture()
def db_url(tmp_path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path}/cancel.sqlite"


async def test_cancelled_caller_still_lands_the_write(db_url: str) -> None:
    coll = AsyncCollection("docs", db_url)
    await coll.insert_one({"_id": "row", "value": 0})

    entered = asyncio.Event()
    release = asyncio.Event()
    original = AsyncCollection._first_matching_row

    async def gated(self, sess, filter):  # noqa: ANN001 - test shim
        entered.set()
        await release.wait()
        return await original(self, sess, filter)

    with patch.object(AsyncCollection, "_first_matching_row", gated):
        task = asyncio.create_task(
            coll.update_one({"_id": "row"}, {"$set": {"value": 1}})
        )
        await entered.wait()
        task.cancel()
        # The CALLER observes its cancel promptly — shielding must not turn
        # cancellation into a blocking wait for the transaction.
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()

    # The write itself runs to completion detached, releasing the lock and
    # committing — poll briefly for the background transaction to land.
    for _ in range(100):
        row = await coll.find_one({"_id": "row"})
        if row is not None and row.get("value") == 1:
            break
        await asyncio.sleep(0.02)
    row = await coll.find_one({"_id": "row"})
    assert row is not None and row.get("value") == 1

    # And the write lock is free: a fresh write succeeds immediately.
    result = await coll.update_one({"_id": "row"}, {"$set": {"value": 2}})
    assert result.modified_count == 1


def test_locked_is_the_one_transient_class() -> None:
    from sqlalchemy.exc import OperationalError as SAOperationalError

    assert is_transient_error(sqlite3.OperationalError("database is locked"))
    assert is_transient_error(
        SAOperationalError("BEGIN IMMEDIATE", {}, sqlite3.OperationalError("database is locked"))
    )
    # Real errors keep surfacing immediately — no retry papering.
    assert not is_transient_error(sqlite3.OperationalError("no such table: astrabox_documents"))
    assert not is_transient_error(sqlite3.IntegrityError("UNIQUE constraint failed"))
    assert not is_transient_error(RuntimeError("database is locked"))
