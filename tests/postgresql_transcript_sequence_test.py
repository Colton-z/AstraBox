"""Concurrent transcript appends, held to a contiguous sequence on real PostgreSQL.

The scope's sequence is claimed by a compare-and-set, and a compare-and-set is
only as good as the row locking underneath it. SQLite serializes every write on
one ``BEGIN IMMEDIATE`` lock, so a claim with no fence at all still produces a
perfect sequence there — a green that proves nothing about the mechanism. Only a
server that admits genuinely concurrent writers can tell the two apart, so this
case runs on PostgreSQL and is the one that fails if the fence is dropped.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from astrabox.persistence.repository.postgresql import create_all, dispose_engines
from astrabox.persistence.repository.transcript_entry_repository import (
    TranscriptEntryRepository,
)
from tests._postgresql_support import resolve_postgresql_test_url

pytestmark = pytest.mark.postgresql

WRITERS = 12
ENTRIES_PER_WRITER = 4


@pytest.fixture(autouse=True)
async def _postgresql_backend(monkeypatch: pytest.MonkeyPatch):
    from astrabox.config.settings import get_settings

    postgres_url = resolve_postgresql_test_url()
    monkeypatch.setenv("ASTRABOX_DB_BACKEND", "postgresql")
    monkeypatch.setenv("ASTRABOX_DB_URL", postgres_url)
    get_settings.cache_clear()
    await create_all(postgres_url)
    yield
    await dispose_engines(postgres_url)
    get_settings.cache_clear()


async def test_concurrent_appends_claim_a_contiguous_sequence() -> None:
    repo = TranscriptEntryRepository(
        collection_name=f"pg_transcript_{uuid.uuid4().hex[:8]}"
    )
    await repo.delete_all_in_collection()
    project_key, session_id = "proj", f"sdk-{uuid.uuid4().hex[:8]}"

    async def append(writer: int) -> int:
        return await repo.append_entries(
            project_key,
            session_id,
            None,
            [{"type": "assistant", "writer": writer, "i": i}
             for i in range(ENTRIES_PER_WRITER)],
            append_id=str(uuid.uuid4()),
            platform_session_id="plat",
        )

    returned = await asyncio.gather(*(append(n) for n in range(WRITERS)))

    collection = await repo._collection()
    stored: list[int] = []
    async for row in collection.find(
        {"project_key": project_key, "session_id": session_id}
    ):
        stored.append(int(row["seq"]))
    total = WRITERS * ENTRIES_PER_WRITER

    assert sorted(stored) == list(range(1, total + 1)), (
        "concurrent claimants must partition 1..N between them: a duplicate means "
        "two writers took the same numbers, a gap means one took a range it never "
        "filled"
    )
    assert sorted(returned) == sorted(set(returned)), (
        "no two batches may report the same end position"
    )
    assert max(returned) == total

    loaded = await repo.load_entries(
        project_key, session_id, None, platform_session_id="plat"
    )
    assert loaded is not None and len(loaded) == total
    await repo.delete_all_in_collection()


async def test_subkey_enumeration_uses_the_platform_session_fence() -> None:
    """The resume lookup must keep its tenant predicate on PostgreSQL JSONB."""

    repo = TranscriptEntryRepository(
        collection_name=f"pg_transcript_subkeys_{uuid.uuid4().hex[:8]}"
    )
    await repo.delete_all_in_collection()
    project_key = "shared-project"
    sdk_session_id = f"sdk-{uuid.uuid4().hex[:8]}"
    await repo.append_entries(
        project_key,
        sdk_session_id,
        "subagents/agent-victim",
        [{"type": "assistant", "uuid": "victim-child"}],
        platform_session_id="victim-platform-session",
    )

    assert await repo.list_subkeys(
        project_key,
        sdk_session_id,
        platform_session_id="attacker-platform-session",
    ) == []
    assert await repo.list_subkeys(
        project_key,
        sdk_session_id,
        platform_session_id="victim-platform-session",
    ) == ["subagents/agent-victim"]
    await repo.delete_all_in_collection()
