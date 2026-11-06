"""Satori feed captures neither queue on the feed's row lock nor number an event twice.

The property is about row locks and database connections, which only a real
database shows, so this runs against PostgreSQL. A second connection holds the
feed row the way a concurrent capture's commit does. An identity with nothing
new must still be captured while that lock is held. With new output, the
process's connections of one feed share a single capture, so exactly one
database session waits on the lock, whatever the number of connections; once
it is released, every part is in exactly one event and the numbers have no gap.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json

import psycopg
import pytest
from sqlalchemy import insert

from astrabox.core.service.orchestrator import satori_feed
from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.persistence.repository.runtime_profile_repository import RuntimeProfileRepository
from astrabox.persistence.repository.satori_repository import SatoriRepository
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.sql.engine import get_engine
from astrabox.persistence.sql.models.sessions import SessionOutput
from astrabox.persistence.sql.ops import transaction

pytestmark = pytest.mark.usefixtures("database")

_USER, _AGENT = "employee-a", "agent-satori"
_CONNECTIONS = 10


async def _seed() -> None:
    await RuntimeProfileRepository().put(
        "one", {"engine_kind": "claude_code", "idle_action": "terminate", "networking": {"type": "unrestricted"}},
        updated_by="test",
    )
    await AgentRepository().create_agent(
        {"agent_id": _AGENT, "name": "Satori", "model": "model", "runtime_profile_name": "one", "created_by": _USER}
    )
    for sid in ("session-a", "session-b"):
        await SessionRepository().create_session({
            "session_id": sid, "user_id": _USER, "agent_id": _AGENT,
            "session_kind": "agent_chat", "engine_kind": "claude_code", "state": "READY",
        })


async def _output(sid: str, response_id: str, first: int, kinds: list[str]) -> None:
    rows = [{"session_id": sid, "seq": first + i, "response_id": response_id, "part": {"type": kind}}
            for i, kind in enumerate(kinds)]
    await transaction(lambda conn: conn.execute(insert(SessionOutput), rows))


def _hold_feed_row(url: str) -> psycopg.Connection:
    """Lock the feed row as a capture's commit does, and keep the transaction open."""
    feed_id = hashlib.sha256(json.dumps([_USER, _AGENT]).encode()).hexdigest()
    conn = psycopg.connect(url)
    conn.execute("SELECT last_sn FROM satori_feeds WHERE feed_id = %s FOR UPDATE", (feed_id,)).fetchone()
    return conn


def _lock_waiters(url: str) -> int:
    with psycopg.connect(url, autocommit=True) as conn:
        row = conn.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock'"
        ).fetchone()
    assert row is not None
    return int(row[0])


def _covered(events: list[dict], sid: str) -> list[int]:
    seqs: list[int] = []
    for event in events:
        if event["session_id"] == sid and event["descriptor"]["kind"] == "output":
            seqs.extend(range(event["descriptor"]["first_seq"], event["descriptor"]["through_seq"] + 1))
    return seqs


async def _settle(read, done, what: str, timeout: float = 5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    value = await read()
    while not done(value):
        assert asyncio.get_running_loop().time() < deadline, f"{what}: {value!r}"
        await asyncio.sleep(0.05)
        value = await read()
    return value


async def test_captures_do_not_queue_on_the_feed_lock_and_number_each_event_once(database: str) -> None:
    repo = SatoriRepository()
    await _seed()
    await _output("session-a", "response-1", 1, ["start", "text-delta", "finish"])

    # ── the feed's first captures race to create and number it ──────────────
    numbers = await asyncio.gather(*[repo.capture(_USER, _AGENT) for _ in range(8)])
    events = await repo.events(_USER, _AGENT, after=0, limit=1000)
    first = len(events)
    assert [event["sn"] for event in events] == list(range(1, first + 1))
    assert max(numbers) == first == await repo.last_sn(_USER, _AGENT)
    assert sorted(_covered(events, "session-a")) == [1, 2, 3]
    assert sorted(e["session_id"] for e in events if e["descriptor"]["kind"] == "state") == ["session-a", "session-b"]

    # ── nothing new: a capture reads past a held feed lock and writes nothing ─
    holder = await asyncio.to_thread(_hold_feed_row, database)
    try:
        assert await asyncio.wait_for(repo.capture(_USER, _AGENT), timeout=5) == first
        assert await asyncio.to_thread(_lock_waiters, database) == 0
    finally:
        await asyncio.to_thread(holder.close)

    # ── new output: the process's connections share one capture ─────────────
    async with contextlib.AsyncExitStack() as stack:
        watches = [await stack.enter_async_context(satori_feed.watch(repo, _USER, _AGENT)) for _ in range(_CONNECTIONS)]
        assert all(watch is watches[0] for watch in watches)
        assert await asyncio.wait_for(watches[0].beyond(first - 1), timeout=5) == first
        holder = await asyncio.to_thread(_hold_feed_row, database)
        try:
            await _output("session-a", "response-2", 4, ["start", "text-delta", "text-delta", "finish"])
            waiting = [asyncio.create_task(watch.beyond(first)) for watch in watches]
            await _settle(lambda: asyncio.to_thread(_lock_waiters, database), lambda n: n >= 1,
                          "the capture never reached the feed lock")
            await asyncio.sleep(1.0)
            assert await asyncio.to_thread(_lock_waiters, database) == 1
            assert not any(task.done() for task in waiting)
        finally:
            await asyncio.to_thread(holder.close)
        latest = set(await asyncio.wait_for(asyncio.gather(*waiting), timeout=5))
        assert len(latest) == 1 and latest.pop() > first

    events = await repo.events(_USER, _AGENT, after=0, limit=1000)
    assert [event["sn"] for event in events] == list(range(1, len(events) + 1))
    assert sorted(_covered(events, "session-a")) == list(range(1, 8))
    assert len(events) == await repo.last_sn(_USER, _AGENT)

    # ── the last connection's exit stops the loop and returns its connection ─
    await _settle(lambda: asyncio.sleep(0, result=get_engine().pool.checkedout()), lambda n: n == 0,  # type: ignore[attr-defined]
                  "a database connection is still checked out after the feed's connections ended")
    assert satori_feed._WATCHES == {}
