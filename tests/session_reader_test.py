"""A reader that takes over a session's lease numbers its events the way the previous
reader did.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncIterator
from typing import Any

import pytest

from astrabox.core.service.orchestrator.engine.base import EngineCapabilityManifest
from astrabox.core.service.orchestrator.engine.runtime import (
    ResponseEnded,
    ResponseOutput,
    ResponseStarted,
    RuntimeCursor,
    RuntimeCursorExpired,
    RuntimeEvent,
    RuntimeInput,
    RuntimeInputRefused,
    RuntimeState,
)
from astrabox.core.service.orchestrator.session_runtime.feed import SessionFeed
from astrabox.core.service.orchestrator.session_runtime.host import ReaderHost, RuntimeNotReady
from astrabox.core.service.orchestrator.session_runtime.reader import ReaderHooks, SessionReader
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.repository.session_runtime_repository import SessionRuntimeRepository
from tests._agent_ledger import create_agent

pytestmark = pytest.mark.usefixtures("database")

_TTL = 30.0


class FakeRuntime:
    """A runtime whose journal is a list of events the test appends to."""

    engine_kind = "fake"

    def __init__(self, generation: str = "g1") -> None:
        self.generation = generation
        self.journal: list[RuntimeEvent] = []
        self.grew = asyncio.Event()
        self.submitted: list[str] = []
        self.answers: list[tuple[str, dict[str, Any]]] = []
        self.refuse: set[str] = set()
        self.ended = False

    def add(self, *events: Any) -> None:
        for event in events:
            index = len(self.journal)
            self.journal.append(dataclasses.replace(event, cursor=RuntimeCursor(self.generation, {"i": index})))
        self.grew.set()

    @property
    def is_live(self) -> bool:
        return not self.ended

    @property
    def engine_session_key(self) -> str | None:
        return "native"

    async def submit(self, item: RuntimeInput) -> None:
        if item.input_id in self.refuse:
            raise RuntimeInputRefused("ENGINE_BUSY", "refused")
        self.submitted.append(item.input_id)

    async def events(self, *, after: RuntimeCursor | None) -> AsyncIterator[RuntimeEvent]:
        if after is not None and after.generation != self.generation:
            raise RuntimeCursorExpired(after.generation)
        index = int(after.position["i"]) + 1 if after is not None else 0
        while True:
            while index < len(self.journal):
                yield self.journal[index]
                index += 1
            if self.ended:
                return
            self.grew.clear()
            await self.grew.wait()

    async def state(self) -> RuntimeState:
        return RuntimeState(status="idle", cursor=RuntimeCursor(self.generation, {"i": len(self.journal) - 1}))

    async def answer(self, interaction_id: str, response: dict[str, Any]) -> None:
        self.answers.append((interaction_id, response))

    async def interrupt(self, *, cancel_queued: bool = False) -> None:
        return None

    async def committed(self, cursor: RuntimeCursor) -> None:
        return None

    async def capabilities(self) -> EngineCapabilityManifest:
        return EngineCapabilityManifest(engine_kind="fake")

    async def close(self) -> None:
        self.ended = True
        self.grew.set()


def _hooks(committed: list[Any] | None = None) -> ReaderHooks:
    async def nothing(*_args: Any) -> None:
        return None

    async def record(batch: Any) -> None:
        if committed is not None:
            committed.append(batch)

    return ReaderHooks(prepare=nothing, committed=record, status=nothing, permission_mode=nothing)


@pytest.fixture
async def feed(database: str) -> AsyncIterator[SessionFeed]:
    instance = SessionFeed()
    await instance.start()
    yield instance
    await instance.stop()


@pytest.fixture
async def session_id() -> str:
    await create_agent("a1")
    await SessionRepository().create_session(
        {"session_id": "s1", "user_id": "u1", "session_kind": "agent_chat", "agent_id": "a1", "state": "READY"}
    )
    return "s1"


def _response(n: int, *texts: str) -> list[Any]:
    rid = f"r{n}"
    events: list[Any] = [ResponseStarted(None, rid, "engine"), ResponseOutput(None, rid, {"type": "start", "messageId": rid})]
    events += [ResponseOutput(None, rid, {"type": "text-delta", "id": "t", "delta": text}) for text in texts]
    events += [ResponseOutput(None, rid, {"type": "finish"}), ResponseEnded(None, rid, "completed", "end_turn")]
    return events


async def _reader(
    repo: SessionRuntimeRepository, feed: SessionFeed, runtime: FakeRuntime, owner: str, committed: list[Any] | None = None
) -> asyncio.Task[Any]:
    lease = await repo.claim_reader("s1", owner=owner, ttl_s=_TTL)
    assert lease is not None
    reader = SessionReader(lease=lease, client=runtime, repo=repo, feed=feed, hooks=_hooks(committed))  # type: ignore[arg-type]
    return asyncio.create_task(reader.run())


async def _until(predicate: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not await predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition not reached"
        await asyncio.sleep(0.02)


async def test_a_reader_that_takes_over_numbers_the_same_events_the_same_way(
    feed: SessionFeed, session_id: str
) -> None:
    repo = SessionRuntimeRepository()
    runtime = FakeRuntime()
    runtime.add(*_response(1, "a", "b", "c"))
    first = await _reader(repo, feed, runtime, "p1")
    await _until(lambda: _committed(repo, 5))
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    runtime.add(*_response(2, "d"))
    await repo.release_reader((await _lease(repo)))

    second = await _reader(repo, feed, runtime, "p2")
    await _until(lambda: _committed(repo, 8))
    await runtime.close()
    await asyncio.wait_for(second, 5)

    rows = await repo.read_output(session_id, after_seq=0)
    assert [row["seq"] for row in rows] == list(range(1, 9))
    deltas = [row["part"].get("delta") for row in rows if row["part"]["type"] == "text-delta"]
    assert deltas == ["a", "b", "c", "d"]


async def _committed(repo: SessionRuntimeRepository, seq: int) -> bool:
    return (await repo.resume_point("s1"))[0] >= seq


async def _lease(repo: SessionRuntimeRepository) -> Any:
    from astrabox.persistence.repository.session_runtime_repository import ReaderLease
    from astrabox.persistence.sql import fetch_one
    from astrabox.persistence.sql.models.sessions import SessionReader as Row
    from sqlalchemy import select

    row = await fetch_one(select(Row.__table__))
    assert row is not None
    return ReaderLease(row["session_id"], row["owner"], row["generation"])


class _RuntimeNotReadyYet:
    """A connector whose runtime is not ready until the test says so."""

    def __init__(self) -> None:
        self.asked = asyncio.Event()
        self.ready = asyncio.Event()

    async def connect(self, session_id: str) -> Any:
        self.asked.set()
        await self.ready.wait()
        raise RuntimeNotReady(session_id)

    async def replace(self, session_id: str) -> None:
        return None

    async def release(self, session_id: str) -> None:
        return None

    async def sessions_needing_readers(self) -> list[str]:
        return []


async def test_concurrent_starts_leave_the_one_reader_holding_its_lease(feed: SessionFeed, session_id: str) -> None:
    """A startup notice and the sweep can ask for a session's reader at once.

    A claim by the same host always succeeds and starts a new generation, so
    two claims would revoke the lease of the reader the first one started and
    leave the session with no reader until the lease expired.
    """

    async def committed(*_args: Any) -> None:
        return None

    repo = SessionRuntimeRepository()
    connector = _RuntimeNotReadyYet()
    host = ReaderHost(repo=repo, feed=feed, connector=connector, hooks=lambda *_: _hooks(), committed=committed)
    await asyncio.gather(host.ensure(session_id), host.ensure(session_id))
    await asyncio.wait_for(connector.asked.wait(), 5)

    lease = await _lease(repo)
    assert (lease.owner, lease.generation) == (host.owner, 1)
    assert host.reads(session_id)
    connector.ready.set()
    await host.stop()
