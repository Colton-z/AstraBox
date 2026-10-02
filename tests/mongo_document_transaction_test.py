"""Native transaction acceptance; run with -m mongo_transaction against a replica set.

Only UUID-owned collections are created and dropped. A standalone server is
not a substitute for this lane and fails its capability prerequisite explicitly.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import anyio
import pytest

pytest.importorskip("pymongo")
pytestmark = pytest.mark.mongo_transaction

from astrabox.persistence.repository import mongo  # noqa: E402


@pytest.fixture
async def store() -> Any:
    database = None
    namespaces_started = False
    prefix = f"transaction_acceptance_{uuid.uuid4().hex}"
    parents, children = f"{prefix}_environments", f"{prefix}_agents"
    try:
        database = await mongo._get_database()
        assert database is not None, "configure a MongoDB URI for this native transaction lane"
        runner = await mongo.get_transaction_runner()
        assert runner is not None, "this lane requires a transaction-capable replica set or sharded cluster"
        namespaces_started = True
        await database[parents].insert_one({"_id": "env", "name": "env"})
        # Prepare both namespaces before entering transactions, including on
        # Mongo versions that cannot create a collection inside a transaction.
        await database[children].create_index("agent_id", unique=True)
        yield database, runner, parents, children
    finally:
        try:
            if database is not None and namespaces_started:
                await database.drop_collection(children)
                await database.drop_collection(parents)
        finally:
            await mongo.close_for_current_loop("transaction_acceptance_complete")


@pytest.mark.parametrize("abort", [False, True])
async def test_cross_collection_commit_or_rollback_is_atomic(store: Any, abort: bool) -> None:
    database, runner, parents, children = store

    async def operation(tx: Any) -> None:
        await tx.collection(parents).lock_one({"_id": "env"})
        await tx.collection(parents).delete_one({"_id": "env"})
        await tx.collection(children).insert_one({"agent_id": "new"})
        assert await tx.collection(parents).find_one({"_id": "env"}) is None
        assert await tx.collection(children).count_documents({}) == 1
        assert await database[parents].find_one({"_id": "env"}) is not None
        assert await database[children].count_documents({}) == 0
        if abort:
            raise ValueError("binding failed")

    if abort:
        with pytest.raises(ValueError, match="binding failed"):
            await runner(operation)
    else:
        await runner(operation)
    assert (await database[parents].find_one({"_id": "env"}) is not None) == abort
    assert await database[children].count_documents({}) == (0 if abort else 1)


@pytest.mark.parametrize("first", ["binding", "deletion"])
async def test_parent_guard_serializes_competing_reference_and_delete(store: Any, first: str) -> None:
    database, runner, parents, children = store
    guarded = asyncio.Event()
    competing = asyncio.Event()
    attempts: dict[str, int] = {}

    async def operation(tx: Any, kind: str) -> str:
        attempts[kind] = attempts.get(kind, 0) + 1
        if kind != first and attempts[kind] > 1:
            # Keep the first writer locked until the driver's whole-callback
            # retry proves an actual native write conflict, not serial scheduling.
            competing.set()
        parent = tx.collection(parents)
        row = await parent.lock_one({"_id": "env"})
        if kind == first:
            guarded.set()
            await competing.wait()
        if row is None:
            return "missing"
        child = tx.collection(children)
        if kind == "binding":
            await child.insert_one({"agent_id": "new", "environment_name": "env"})
            return "bound"
        if await child.count_documents({"environment_name": "env"}):
            return "in-use"
        await parent.delete_one({"_id": "env"})
        return "deleted"

    tasks = [asyncio.create_task(runner(lambda tx: operation(tx, first)))]
    try:
        await asyncio.wait_for(guarded.wait(), 5)
        second = "deletion" if first == "binding" else "binding"
        tasks.append(asyncio.create_task(runner(lambda tx: operation(tx, second))))
        outcomes = await asyncio.wait_for(asyncio.gather(*tasks), 10)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert outcomes == (["bound", "in-use"] if first == "binding" else ["deleted", "missing"])
    assert attempts[second] >= 2
    assert await database[children].count_documents({}) == (first == "binding")


@pytest.mark.parametrize("cancellation", ["asyncio", "anyio"])
async def test_cancelled_callback_rolls_back_and_releases_parent(store: Any, cancellation: str) -> None:
    database, runner, parents, children = store
    entered = asyncio.Event()

    async def operation(tx: Any) -> None:
        await tx.collection(parents).lock_one({"_id": "env"})
        await tx.collection(children).insert_one({"agent_id": "cancelled"})
        entered.set()
        await asyncio.Event().wait()

    if cancellation == "asyncio":
        task = asyncio.create_task(runner(operation))
        try:
            await asyncio.wait_for(entered.wait(), 5)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
    else:
        with anyio.fail_after(5):
            async with anyio.create_task_group() as group:
                group.start_soon(runner, operation)
                await entered.wait()
                group.cancel_scope.cancel()
    assert await database[children].count_documents({}) == 0
    # Another real writer must acquire the released parent guard.
    await asyncio.wait_for(runner(lambda tx: tx.collection(parents).lock_one({"_id": "env"})), 5)


async def test_bound_cursor_keeps_null_semantics_and_cannot_escape_callback(store: Any) -> None:
    database, runner, parents, children = store
    await database[children].insert_many([
        {"agent_id": "null", "value": None}, {"agent_id": "missing"},
    ])
    saved = []

    async def operation(tx: Any) -> None:
        collection = tx.collection(children)
        cursor = collection.find({"value": None}).sort("agent_id", string_keyed=True)
        assert [row["agent_id"] async for row in cursor] == ["null"]
        saved.append(cursor)
        with pytest.raises(RuntimeError, match="across tasks"):
            await asyncio.create_task(collection.count_documents({}))
        with pytest.raises(RuntimeError, match="prepare collection indexes"):
            await collection.create_index("anything")

    await runner(operation)
    with pytest.raises(RuntimeError, match="already ended"):
        await saved[0].to_list()
