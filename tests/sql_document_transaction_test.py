"""Cross-collection writes keep their atomic boundary, including cancellation."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import anyio
import pytest
from sqlalchemy import text

from astrabox.persistence.repository.sqlite.collection import AsyncCollection
from astrabox.persistence.repository.sqlite.transaction import run_transaction
from astrabox.persistence.repository.sqlite.engine import dispose_engines, get_sessionmaker
from tests._postgresql_support import resolve_postgresql_test_url


@pytest.fixture
def collections() -> dict[str, str]:
    prefix = f"transaction_acceptance_{uuid.uuid4().hex}"
    return {name: f"{prefix}_{name}" for name in ("environments", "agents")}


@pytest.fixture(params=[
    pytest.param("sqlite", id="sqlite"),
    pytest.param("postgresql", id="postgresql", marks=pytest.mark.postgresql),
])
async def db_url(request: Any, tmp_path: Any, collections: dict[str, str]) -> Any:
    url = (
        resolve_postgresql_test_url() if request.param == "postgresql"
        else f"sqlite+aiosqlite:///{tmp_path}/transaction.sqlite"
    )
    try:
        yield url
    finally:
        try:
            # Only this test's UUID namespaces are removed, including on a
            # shared developer database. Other collection rows stay intact.
            for name in collections.values():
                await AsyncCollection(name, url).delete_many({})
        finally:
            await dispose_engines(url)


async def test_collections_see_own_writes_but_outsiders_only_see_commit(db_url: str, collections: dict[str, str]) -> None:
    environments = AsyncCollection(collections["environments"], db_url)
    agents = AsyncCollection(collections["agents"], db_url)
    await environments.insert_one({"_id": "env", "name": "original"})

    async def write(tx: Any) -> str:
        env = tx.collection(collections["environments"])
        bound_agents = tx.collection(collections["agents"])
        await env.update_one({"_id": "env"}, {"$set": {"name": "changed"}})
        await bound_agents.insert_one({"_id": "agent", "environment_name": "changed"})
        assert (await env.find_one({"_id": "env"}))["name"] == "changed"
        assert await bound_agents.count_documents({}) == 1
        assert (await environments.find_one({"_id": "env"}))["name"] == "original"
        assert await agents.count_documents({}) == 0
        return "committed"

    assert await run_transaction(write, db_url) == "committed"
    assert (await environments.find_one({"_id": "env"}))["name"] == "changed"
    assert await agents.count_documents({}) == 1


async def test_exception_rolls_back_every_collection(db_url: str, collections: dict[str, str]) -> None:
    env = AsyncCollection(collections["environments"], db_url)
    await env.insert_one({"_id": "env"})

    async def write(tx: Any) -> None:
        await tx.collection(collections["agents"]).insert_one({"_id": "new"})
        await tx.collection(collections["environments"]).delete_one({"_id": "env"})
        raise ValueError("binding failed")

    with pytest.raises(ValueError, match="binding failed"):
        await run_transaction(write, db_url)
    assert await env.find_one({"_id": "env"}) is not None
    assert await AsyncCollection(collections["agents"], db_url).count_documents({}) == 0


@pytest.mark.parametrize("cancellation", ["asyncio", "anyio"])
async def test_cancellation_rolls_back_and_releases_lock(db_url: str, collections: dict[str, str], cancellation: str) -> None:
    env = AsyncCollection(collections["environments"], db_url)
    await env.insert_one({"_id": "env"})
    entered = asyncio.Event()

    async def write(tx: Any) -> None:
        await tx.collection(collections["environments"]).delete_one({"_id": "env"})
        await tx.collection(collections["agents"]).insert_one({"_id": "new"})
        entered.set()
        await asyncio.Event().wait()

    if cancellation == "asyncio":
        task = asyncio.create_task(run_transaction(write, db_url))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    else:
        with anyio.fail_after(2):
            async with anyio.create_task_group() as group:
                group.start_soon(run_transaction, write, db_url)
                await entered.wait()
                group.cancel_scope.cancel()

    assert await env.find_one({"_id": "env"}) is not None
    assert await AsyncCollection(collections["agents"], db_url).count_documents({}) == 0
    # A new writer acquires the released lock, not just a WAL reader snapshot.
    await asyncio.wait_for(env.insert_one({"_id": "after-cancel"}), 2)


async def test_bound_handles_reject_other_tasks_and_use_after_end(db_url: str, collections: dict[str, str]) -> None:
    handles = []
    cursors = []

    async def write(tx: Any) -> None:
        coll = tx.collection(collections["agents"])
        handles.append(coll)
        with pytest.raises(RuntimeError, match="across tasks"):
            await asyncio.create_task(coll.insert_one({"_id": "wrong-task"}))
        await coll.insert_one({"_id": "owner"})
        cursor = coll.find({})
        assert len(await cursor.to_list()) == 1
        cursors.append(cursor)

    await run_transaction(write, db_url)
    with pytest.raises(RuntimeError, match="already ended"):
        await handles[0].find_one({})
    with pytest.raises(RuntimeError, match="already ended"):
        await cursors[0].to_list()
    assert await AsyncCollection(collections["agents"], db_url).count_documents({}) == 1


async def test_callback_cannot_commit_by_swallowing_cancellation(db_url: str, collections: dict[str, str]) -> None:
    entered = asyncio.Event()

    async def write(tx: Any) -> None:
        await tx.collection(collections["agents"]).insert_one({"_id": "new"})
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass

    task = asyncio.create_task(run_transaction(write, db_url))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert await AsyncCollection(collections["agents"], db_url).count_documents({}) == 0


async def test_schema_changes_are_rejected_before_they_escape_the_transaction(db_url: str, collections: dict[str, str]) -> None:
    async def write(tx: Any) -> None:
        coll = tx.collection(collections["agents"])
        with pytest.raises(RuntimeError, match="prepare collection indexes"):
            await coll.create_index("name", unique=True)
        with pytest.raises(RuntimeError, match="prepare collection indexes"):
            await coll.drop_index("anything")

    await run_transaction(write, db_url)


@pytest.mark.parametrize("first", ["binding", "deletion"])
async def test_parent_guard_serializes_binding_and_deletion(db_url: str, collections: dict[str, str], first: str) -> None:
    env = AsyncCollection(collections["environments"], db_url)
    await env.insert_one({"_id": "env"})
    guarded = asyncio.Event()
    competitor_started = asyncio.Event()
    competitor_acquired = asyncio.Event()
    release = asyncio.Event()
    postgres = db_url.startswith("postgresql")
    backend_pids: dict[str, int] = {}

    async def operation(tx: Any, kind: str) -> str:
        if postgres:
            backend_pids[kind] = int(await tx.session.scalar(text("SELECT pg_backend_pid()")))
        if kind != first:
            competitor_started.set()
        parent = tx.collection(collections["environments"])
        row = await parent.lock_one({"_id": "env"})
        if kind == first:
            guarded.set()
            await release.wait()
        else:
            competitor_acquired.set()
        if row is None:
            return "missing"
        children = tx.collection(collections["agents"])
        if kind == "binding":
            await children.insert_one({"_id": "agent", "environment_name": "env"})
            return "bound"
        if await children.count_documents({"environment_name": "env"}):
            return "in-use"
        await parent.delete_one({"_id": "env"})
        return "deleted"

    tasks = [asyncio.create_task(run_transaction(lambda tx: operation(tx, first), db_url))]
    try:
        await asyncio.wait_for(guarded.wait(), 3)
        second = "deletion" if first == "binding" else "binding"
        tasks.append(asyncio.create_task(run_transaction(lambda tx: operation(tx, second), db_url)))
        await asyncio.wait_for(competitor_started.wait(), 3)
        if postgres:
            async def observe_native_blocker() -> None:
                async with get_sessionmaker(db_url, mode="read")() as observer:
                    while True:
                        blockers = await observer.scalar(
                            text("SELECT pg_blocking_pids(:waiting_pid)"),
                            {"waiting_pid": backend_pids[second]},
                        )
                        if backend_pids[first] in blockers:
                            return
                        assert not competitor_acquired.is_set(), "parent guard did not block the competing writer"
                        await asyncio.sleep(0.01)

            # Observe the database's actual lock dependency before allowing
            # the first transaction to finish; task scheduling is not proof.
            await asyncio.wait_for(observe_native_blocker(), 3)
        else:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(competitor_acquired.wait(), 0.1)
        assert not competitor_acquired.is_set()
        release.set()
        outcomes = await asyncio.wait_for(asyncio.gather(*tasks), 3)
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert outcomes == (["bound", "in-use"] if first == "binding" else ["deleted", "missing"])
    assert (await env.find_one({"_id": "env"}) is not None) == (first == "binding")
    assert await AsyncCollection(collections["agents"], db_url).count_documents({}) == (first == "binding")
