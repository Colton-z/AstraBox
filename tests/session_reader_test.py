"""Concurrent startup requests leave one reader holding the session lease."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from astrabox.core.service.orchestrator.session_runtime.feed import SessionFeed
from astrabox.core.service.orchestrator.session_runtime.host import ReaderHost, RuntimeNotReady
from astrabox.core.service.orchestrator.session_runtime.reader import ReaderHooks
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.repository.session_runtime_repository import SessionRuntimeRepository
from tests._agent_ledger import create_agent

pytestmark = pytest.mark.usefixtures("database")


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
