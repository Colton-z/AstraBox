"""One live shared-repository journey, bound to a provider's real store.

Downstream subclasses RelationalPersistenceContractSuite and supplies the
relational_store fixture. Its native observer and connection faults are required;
an unavailable backend or fault is not a passing or skipped contract. PostgreSQL
passing this journey establishes nothing about another database or topology.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import traceback
import uuid
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from multiprocessing.connection import Connection
from typing import Any, Protocol

from sqlalchemy import update

from astrabox.config.settings import get_settings
from astrabox.core.service.orchestrator.session_runtime.feed import Notice, Resync, SessionFeed
from astrabox.core.service.orchestrator.session_runtime.follow import follow_output
from astrabox.persistence.provider import get_persistence
from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.persistence.repository.environment_repository import EnvironmentRepository
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.repository.session_runtime_repository import (
    FactBatch,
    OutputPart,
    ReaderLease,
    ResponseStart,
    SessionRuntimeRepository,
)
from astrabox.persistence.sql import transaction
from astrabox.persistence.sql.models.sessions import Session
from astrabox.seams.persistence_coordination import SessionNotice


class NativePersistenceObservation(Protocol):
    """Independent vendor reads/faults, scoped to the binding's own database."""

    async def snapshot(self, session_id: str) -> dict[str, Any]: ...
    async def notice_sessions(self) -> set[str]: ...
    async def subscriber_waiting(self) -> None: ...
    async def runtime_contender_waiting(self) -> None: ...
    async def disconnect_runtime_holder(self) -> None: ...
    def listener_gap(self) -> AbstractAsyncContextManager[None]: ...


@dataclass
class RelationalContractStore:
    """The parent is configured; the replica gets independent provider settings."""

    replica_environment: Mapping[str, str]
    native: NativePersistenceObservation
    passed: bool = False


class _Rollback(Exception):
    pass


async def _receive(pipe: Connection, expected: str) -> Any:
    assert await asyncio.to_thread(pipe.poll, 30), f"replica did not reach {expected}"
    kind, value = pipe.recv()
    assert kind == expected, f"expected {expected}, received {kind}: {value}"
    return value


async def _accept(sid: str, candidate: str) -> Any:
    return await SessionRuntimeRepository().accept_input(
        session_id=sid, input_id=candidate, client_message_id="same-message",
        content="Keep these exact bytes: 雪\n", content_blocks=None,
        owner_user_id="contract-owner", author_user_id="contract-owner", source="console",
    )


def _batch(start: int, *parts: dict[str, Any]) -> FactBatch:
    return FactBatch(
        output=[OutputPart(start + index, None, part) for index, part in enumerate(parts)],
        cursor={"position": start + len(parts) - 1},
    )


def _replica(environment: Mapping[str, str], pipe: Connection, sid: str) -> None:
    os.environ.update(environment)
    get_settings.cache_clear()
    get_persistence.cache_clear()
    try:
        asyncio.run(_replica_journey(pipe, sid))
    except BaseException:
        pipe.send(("failure", traceback.format_exc()))
        raise
    finally:
        pipe.close()


async def _replica_journey(pipe: Connection, sid: str) -> None:
    provider = get_persistence()
    feed, repo = SessionFeed(), SessionRuntimeRepository()
    parts: list[dict[str, Any]] = []
    prefix, resynced, input_seen = asyncio.Event(), asyncio.Event(), asyncio.Event()
    inputs: list[dict[str, Any]] = []

    async def follow() -> None:
        async for part in follow_output(feed, repo, sid, after_seq=0):
            parts.append(part)
            if part.get("delta") == "before gap ":
                prefix.set()

    async def observe() -> None:
        with feed.subscribe(sid) as subscription:
            while True:
                item = await subscription.get()
                if isinstance(item, Resync):
                    resynced.set()
                elif isinstance(item, Notice) and item.kind == "input":
                    inputs[:] = await repo.undelivered_inputs(sid)
                    input_seen.set()

    async def contend() -> None:
        async with provider.coordination.runtime_lock(sid) as check:
            await check()

    tasks: list[asyncio.Task[Any]] = []
    try:
        await feed.start()
        tasks = [asyncio.create_task(follow()), asyncio.create_task(observe())]
        pipe.send(("ready", type(provider).__module__))
        await _receive(pipe, "race")
        lock = asyncio.create_task(contend())
        tasks.append(lock)
        accepted = await _accept(sid, "replica-candidate")
        pipe.send(("accepted", accepted))
        await asyncio.wait_for(input_seen.wait(), 30)
        pipe.send(("input-wakeup", inputs))
        await asyncio.wait_for(prefix.wait(), 30)
        pipe.send(("prefix", list(parts)))
        await lock
        pipe.send(("lock-released", None))
        await _receive(pipe, "handoff")
        lease = await repo.claim_reader(sid, owner="replica", ttl_s=60)
        assert lease is not None
        pipe.send(("lease", lease))
        await _receive(pipe, "gap-write")
        assert await repo.record(lease, _batch(
            4, {"type": "text-delta", "id": "text", "delta": "after gap"},
            {"type": "text-end", "id": "text"}, {"type": "finish"},
        ), ttl_s=60)
        pipe.send(("gap-committed", list(parts)))
        await asyncio.wait_for(resynced.wait(), 30)
        await asyncio.wait_for(tasks[0], 30)
        pipe.send(("done", parts))
        await repo.release_reader(lease)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await feed.stop()
        await provider.dispose()


class RelationalPersistenceContractSuite:
    """Bind relational_store to a real provider, native observer and owned faults."""

    async def test_transactional_wakeup_and_reader_handoff(
        self, relational_store: RelationalContractStore,
    ) -> None:
        store, repo = relational_store, SessionRuntimeRepository()
        provider = get_persistence()
        expected = [
            {"type": "start", "messageId": "response"},
            {"type": "text-start", "id": "text"},
            {"type": "text-delta", "id": "text", "delta": "before gap "},
            {"type": "text-delta", "id": "text", "delta": "after gap"},
            {"type": "text-end", "id": "text"},
            {"type": "finish"},
        ]
        sid, rolled_back = uuid.uuid4().hex, uuid.uuid4().hex
        await EnvironmentRepository().put("contract", {
            "engine_kind": "claude_code", "idle_action": "terminate",
            "networking": {"type": "unrestricted"},
        }, updated_by="contract-owner")
        await AgentRepository().create_agent({
            "agent_id": sid, "name": "Relational contract", "model": "unused",
            "environment_name": "contract", "created_by": "contract-owner",
        })
        for value in (sid, rolled_back):
            await SessionRepository().create_session({
                "session_id": value, "user_id": "contract-owner", "agent_id": sid,
                "session_kind": "agent_chat", "state": "READY", "title": "original",
            })

        held = asyncio.Event()
        lost_check: list[BaseException] = []

        async def holder() -> None:
            async with provider.coordination.runtime_lock(sid) as check:
                held.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    try:
                        await check()
                    except (Exception, asyncio.CancelledError) as error:
                        lost_check.append(error)
                    raise

        parent, child = multiprocessing.get_context("spawn").Pipe()
        process = multiprocessing.get_context("spawn").Process(
            target=_replica, args=(store.replica_environment, child, sid),
        )
        lock = asyncio.create_task(holder())
        process.start()
        child.close()
        try:
            await asyncio.wait_for(held.wait(), 30)
            assert await _receive(parent, "ready") == type(provider).__module__
            await store.native.subscriber_waiting()

            async def abort(connection: Any) -> None:
                await connection.execute(update(Session).where(
                    Session.session_id == rolled_back,
                ).values(title="must roll back"))
                await provider.coordination.notify(
                    connection, SessionNotice(rolled_back, "session", 0),
                )
                assert (await store.native.snapshot(rolled_back))["session"]["title"] == "original"
                raise _Rollback

            try:
                await transaction(abort)
            except _Rollback:
                pass
            else:
                raise AssertionError("the transaction did not propagate its refusal")
            assert (await store.native.snapshot(rolled_back))["session"]["title"] == "original"
            parent.send(("race", None))
            accepted = await _accept(sid, "parent-candidate")
            replica = await _receive(parent, "accepted")
            assert accepted is not None and replica is not None
            assert accepted[0]["input_id"] == replica[0]["input_id"]
            assert sorted([accepted[1], replica[1]]) == [False, True]
            wake = await _receive(parent, "input-wakeup")
            assert len(wake) == 1 and wake[0]["content"] == "Keep these exact bytes: 雪\n"
            native = await store.native.snapshot(sid)
            assert len(native["inputs"]) == 1
            assert native["inputs"][0]["input_id"] == accepted[0]["input_id"]
            assert native["inputs"][0]["content"] == "Keep these exact bytes: 雪\n"
            assert native["inputs"][0]["author_user_id"] == "contract-owner"
            assert native["session"]["user_id"] == "contract-owner"
            await store.native.runtime_contender_waiting()
            lease = await repo.claim_reader(sid, owner="parent", ttl_s=60)
            assert lease is not None
            assert await repo.claim_reader(sid, owner="replica", ttl_s=60) is None
            assert await repo.record(lease, _batch(
                1, {"type": "start", "messageId": "response"},
                {"type": "text-start", "id": "text"},
                {"type": "text-delta", "id": "text", "delta": "before gap "},
            ), ttl_s=60)
            prefix = await _receive(parent, "prefix")
            assert [part for part in prefix if not part.get("transient")] == expected[:3]
            await store.native.disconnect_runtime_holder()
            try:
                await asyncio.wait_for(asyncio.shield(lock), 30)
            except asyncio.CancelledError:
                pass
            except Exception as error:
                assert not isinstance(error, TimeoutError), "ownership loss did not stop the holder"
            assert lost_check and lock.done() and (lock.cancelled() or lock.exception() is not None), (
                "lost ownership did not refuse the holder's check/context success"
            )
            await _receive(parent, "lock-released")
            await repo.release_reader(lease)
            parent.send(("handoff", None))
            newer: ReaderLease = await _receive(parent, "lease")
            assert newer.generation > lease.generation
            before = await store.native.snapshot(sid)
            stale = _batch(4, {"type": "text-delta", "id": "text", "delta": "stale"})
            stale.status = "working"
            stale.facts.append(ResponseStart("stale-response", "engine"))
            assert not await repo.record(lease, stale, ttl_s=60)
            assert await store.native.snapshot(sid) == before
            async with store.native.listener_gap():
                parent.send(("gap-write", None))
                pending = await _receive(parent, "gap-committed")
                assert not any(part.get("delta") == "after gap" for part in pending)
                gap = await store.native.snapshot(sid)
                assert gap["session"]["reader_cursor"] == {"position": 6}
                assert [row["seq"] for row in gap["output"]] == list(range(1, 7))
            parts = await _receive(parent, "done")
            assert [part for part in parts if not part.get("transient")] == expected
            assert parts[-1]["data"]["seq"] == 6
            notices = await store.native.notice_sessions()
            assert sid in notices and rolled_back not in notices
            await asyncio.to_thread(process.join, 10)
            assert process.exitcode == 0
            final = await store.native.snapshot(sid)
            assert final["session"]["reader_cursor"] == {"position": 6}
            assert final["reader"][0]["generation"] == newer.generation
            assert [row["input_id"] for row in final["inputs"]] == [accepted[0]["input_id"]]
            assert [row["part"] for row in final["output"]] == expected
            store.passed = True
        finally:
            lock.cancel()
            await asyncio.gather(lock, return_exceptions=True)
            if process.is_alive():
                process.terminate()
                await asyncio.to_thread(process.join, 5)
            parent.close()
