"""Concurrent appends to one transcript, held to the store's contract on PostgreSQL.

The box's sender re-sends a batch until the platform confirms it, so two
deliveries of one batch can be in flight together; and a session's spool and
its mirror append separate batches to one scope at the same time. Each
delivery decides in one statement whether its scope already holds the batch
and where the batch goes, so only writers whose statements are in flight
together can show a second copy of a batch or a gap in the numbering.

Each case makes that overlap certain rather than likely: a transaction inserts
the scope and stays open until every append has started and is waiting on it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncConnection

from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.repository.transcript_entry_repository import (
    TranscriptEntryRepository,
)
from astrabox.persistence.sql import fetch_value, transaction
from astrabox.persistence.sql.models.sessions import TranscriptScope

pytestmark = pytest.mark.usefixtures("database")

WRITERS = 12
ENTRIES_PER_BATCH = 4
SCOPE = ("project", "native-session", None)


async def _session() -> str:
    await SessionRepository().create_session(
        {"session_id": "s1", "user_id": "u1", "session_kind": "agent_chat", "state": "READY"}
    )
    return "s1"


@asynccontextmanager
async def _scope_held_open(session_id: str) -> AsyncIterator[None]:
    """Insert the scope in a transaction that commits only when the block ends.

    Every append started inside the block takes its snapshot and then waits on
    that uncommitted row, so all of them run with snapshots taken before any
    of them stored anything.
    """
    inserted, release = asyncio.Event(), asyncio.Event()

    async def hold(conn: AsyncConnection) -> None:
        project_key, native_session_id, _ = SCOPE
        await conn.execute(
            insert(TranscriptScope.__table__).values(
                platform_session_id=session_id,
                project_key=project_key,
                native_session_id=native_session_id,
                last_sequence=0,
            )
        )
        inserted.set()
        await release.wait()

    holder = asyncio.create_task(transaction(hold))
    await inserted.wait()
    try:
        yield
    finally:
        release.set()
        await holder


async def _until_waiting(count: int) -> None:
    """Return once ``count`` statements of this database wait on a lock."""
    deadline = asyncio.get_running_loop().time() + 20
    while True:
        waiting = await fetch_value(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )
        )
        if int(waiting or 0) >= count:
            return
        assert asyncio.get_running_loop().time() < deadline, (
            f"only {waiting} of {count} appends reached the database together"
        )
        await asyncio.sleep(0.05)


async def test_concurrent_deliveries_of_one_batch_store_it_once() -> None:
    session_id = await _session()
    repo = TranscriptEntryRepository()
    batch = [{"type": "summary", "n": index} for index in range(ENTRIES_PER_BATCH)]

    async with _scope_held_open(session_id):
        deliveries = [
            asyncio.create_task(
                repo.append_entries(
                    *SCOPE, batch, append_id="batch-1", platform_session_id=session_id
                )
            )
            for _ in range(WRITERS)
        ]
        await _until_waiting(WRITERS)
    positions = await asyncio.gather(*deliveries)

    assert positions == [ENTRIES_PER_BATCH] * WRITERS, (
        "every delivery answers the position the batch reached, including the "
        "ones that lost the race to store it"
    )
    assert await repo.load_entries(*SCOPE, platform_session_id=session_id) == batch, (
        "the batch is stored once: a second copy would replay its lines on resume"
    )
    assert await repo.current_sequence(*SCOPE, platform_session_id=session_id) == ENTRIES_PER_BATCH


async def test_concurrent_batches_partition_one_contiguous_sequence() -> None:
    session_id = await _session()
    repo = TranscriptEntryRepository()
    batches = {
        writer: [{"writer": writer, "i": index} for index in range(ENTRIES_PER_BATCH)]
        for writer in range(WRITERS)
    }

    async with _scope_held_open(session_id):
        appends = [
            asyncio.create_task(
                repo.append_entries(
                    *SCOPE, batch, append_id=f"batch-{writer}", platform_session_id=session_id
                )
            )
            for writer, batch in batches.items()
        ]
        await _until_waiting(WRITERS)
    positions = await asyncio.gather(*appends)

    total = WRITERS * ENTRIES_PER_BATCH
    assert sorted(positions) == list(range(ENTRIES_PER_BATCH, total + 1, ENTRIES_PER_BATCH)), (
        "concurrent batches partition 1..N between them: a repeated end position "
        "means two batches took the same numbers"
    )
    stored = await repo.load_entries(*SCOPE, platform_session_id=session_id)
    assert stored is not None and len(stored) == total, "no entry is missing or doubled"
    for (writer, batch), position in zip(batches.items(), positions):
        assert stored[position - ENTRIES_PER_BATCH:position] == batch, (
            f"batch {writer} occupies the entries that end at the position it was "
            "answered, in its own order"
        )
