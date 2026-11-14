"""Input custody and the reader lease."""

from __future__ import annotations

import pytest

from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.repository.session_runtime_repository import (
    FactBatch,
    OutputPart,
    SessionRuntimeRepository,
)
from tests._agent_ledger import create_agent

pytestmark = pytest.mark.usefixtures("database")

_TTL = 30.0


async def _session(session_id: str = "s1", *, state: str = "READY") -> None:
    await create_agent("a1")
    await SessionRepository().create_session(
        {
            "session_id": session_id,
            "user_id": "u1",
            "session_kind": "agent_chat",
            "agent_id": "a1",
            "state": state,
        }
    )


async def _accept(repo: SessionRuntimeRepository, client_id: str, *, owner: str | None = "u1"):
    return await repo.accept_input(
        session_id="s1",
        input_id=f"in-{client_id}",
        client_message_id=client_id,
        content=f"hello {client_id}",
        content_blocks=None,
        owner_user_id=owner,
        author_user_id=owner,
        source="console",
    )


async def test_an_input_is_accepted_once_and_only_into_the_owners_live_session() -> None:
    await _session()
    repo = SessionRuntimeRepository()

    first = await _accept(repo, "c1")
    again = await _accept(repo, "c1")
    stranger = await _accept(repo, "c2", owner="someone-else")

    assert first is not None and first[1] is True and first[0]["state"] == "pending"
    assert again is not None and again[1] is False and again[0]["input_id"] == "in-c1"
    assert stranger is None

    await SessionRepository().update_session("s1", {"state": "TERMINATED"})
    assert await _accept(repo, "c3") is None


async def test_a_reader_that_lost_its_lease_records_nothing() -> None:
    await _session()
    repo = SessionRuntimeRepository()
    old = await repo.claim_reader("s1", owner="p1", ttl_s=_TTL)
    assert old is not None
    assert await repo.claim_reader("s1", owner="p2", ttl_s=_TTL) is None

    await repo.release_reader(old)
    new = await repo.claim_reader("s1", owner="p2", ttl_s=_TTL)
    assert new is not None and new.generation == old.generation + 1

    stale = FactBatch(output=[OutputPart(1, None, {"type": "text-delta", "id": "t", "delta": "x"})])
    assert await repo.record(old, stale, ttl_s=_TTL) is False
    assert await repo.read_output("s1", after_seq=0) == []
    assert await repo.reader_owner("s1") == "p2"


async def test_the_session_list_keeps_the_sessions_whose_runtime_reports_a_given_status() -> None:
    """A watcher asks which conversations work and which wait; one that reported nothing is idle."""
    sessions = SessionRepository()
    await create_agent("a1")
    for session_id, status in (("s-idle", None), ("s-work", "working"), ("s-wait", "waiting"), ("s-bg", "background")):
        await sessions.create_session(
            {"session_id": session_id, "user_id": "u1", "session_kind": "agent_chat", "agent_id": "a1", "state": "READY"}
        )
        if status is not None:
            await sessions.update_session(session_id, {"runtime_status": status})

    async def listed(statuses: list[str] | None) -> set[str]:
        page = await sessions.list_user_sessions_page("u1", runtime_statuses=statuses)
        return {str(row["session_id"]) for row in page["sessions"]}

    assert await listed(None) == {"s-idle", "s-work", "s-wait", "s-bg"}
    assert await listed(["working", "background"]) == {"s-work", "s-bg"}
    assert await listed(["waiting"]) == {"s-wait"}
    assert await listed(["idle"]) == {"s-idle"}
