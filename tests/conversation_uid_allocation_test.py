"""Allocating the POSIX owner a conversation runs as.

Under the shared-sandbox mode many conversations live in ONE box, and the thing
that keeps one out of another's files is the numeric owner OpenSandbox enforces
per isolated session. That enforcement belongs to OpenSandbox; the allocation
belongs to this module, and it has exactly two properties worth testing
because both fail silently:

* a number is never handed out twice. Not after a conversation ends, not after
  the cursor is read concurrently, not by wrapping at the end of the range —
  files outlive the conversations that wrote them, so a recycled owner hands a
  live conversation read access to a dead one's data.
* running out is an error, not a wrap.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.runtime.conversation_identity import (
    CONVERSATION_UID_BASE,
    CONVERSATION_UID_CEILING,
    allocate_agent_scoped_uid,
)


class _FakeAgentRepo:
    """An agent row with a compare-and-set that behaves like the real one."""

    def __init__(self, agent: dict[str, Any] | None = None) -> None:
        self.agent = agent if agent is not None else {"agent_id": "a1"}
        self.cas_calls: list[dict[str, Any]] = []
        #: Set to have the NEXT compare-and-set lose its race, exactly once.
        self.steal_once: int | None = None

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        return dict(self.agent) if self.agent else None

    async def compare_and_update_agent(
        self, agent_id: str, *, expected: dict[str, Any], updates: dict[str, Any]
    ) -> bool:
        self.cas_calls.append({"expected": dict(expected), "updates": dict(updates)})
        if self.steal_once is not None:
            # Another caller claimed it between this read and this write.
            self.agent["conversation_uid_cursor"] = self.steal_once
            self.steal_once = None
            return False
        want = expected.get("conversation_uid_cursor")
        held = self.agent.get("conversation_uid_cursor")
        if isinstance(want, dict):
            if "conversation_uid_cursor" in self.agent:
                return False
        elif held != want:
            return False
        self.agent.update(updates)
        return True


def test_the_first_conversation_gets_the_base() -> None:
    repo = _FakeAgentRepo()
    assert asyncio.run(allocate_agent_scoped_uid(repo, "a1")) == CONVERSATION_UID_BASE
    # A row with no cursor yet must be claimed on ABSENCE, or two first-ever
    # conversations both read "nothing" and both take the base.
    assert repo.cas_calls[0]["expected"] == {
        "conversation_uid_cursor": {"$exists": False}
    }


def test_numbers_are_never_handed_out_twice() -> None:
    repo = _FakeAgentRepo()
    seen = [asyncio.run(allocate_agent_scoped_uid(repo, "a1")) for _ in range(25)]
    assert seen == sorted(seen)
    assert len(set(seen)) == len(seen)
    assert seen[0] == CONVERSATION_UID_BASE


def test_a_lost_race_retries_and_takes_the_next_one() -> None:
    """The whole point of the compare-and-set: the loser must not reuse."""
    repo = _FakeAgentRepo({"agent_id": "a1", "conversation_uid_cursor": 2005})
    repo.steal_once = 2006  # a concurrent claim lands between the read and the write
    allocated = asyncio.run(allocate_agent_scoped_uid(repo, "a1"))
    assert allocated == 2007, "must step past the number the racer took, not reuse it"
    assert len(repo.cas_calls) == 2


def test_a_released_conversation_does_not_give_its_number_back() -> None:
    """A uid is safe to reuse only once nothing it owns is on disk, which this
    layer cannot know. So the cursor never moves backwards."""
    repo = _FakeAgentRepo({"agent_id": "a1", "conversation_uid_cursor": 2400})
    assert asyncio.run(allocate_agent_scoped_uid(repo, "a1")) == 2401
    # Whatever a caller does with 2401 afterwards, the cursor stays where it is.
    assert repo.agent["conversation_uid_cursor"] == 2401
    assert asyncio.run(allocate_agent_scoped_uid(repo, "a1")) == 2402


def test_running_out_refuses_instead_of_wrapping() -> None:
    repo = _FakeAgentRepo(
        {"agent_id": "a1", "conversation_uid_cursor": CONVERSATION_UID_CEILING}
    )
    with pytest.raises(APIError) as caught:
        asyncio.run(allocate_agent_scoped_uid(repo, "a1"))
    assert "exhausted" in str(caught.value.message)
    # And nothing was written — a refused allocation must not move the cursor.
    assert repo.agent["conversation_uid_cursor"] == CONVERSATION_UID_CEILING


def test_a_cursor_below_the_base_cannot_drag_an_owner_into_system_range() -> None:
    """A row carrying a stale or hand-edited low cursor must not hand out a uid
    that collides with the image's own accounts."""
    repo = _FakeAgentRepo({"agent_id": "a1", "conversation_uid_cursor": 12})
    assert asyncio.run(allocate_agent_scoped_uid(repo, "a1")) == CONVERSATION_UID_BASE


def test_a_missing_agent_is_an_error_not_a_default_uid() -> None:
    repo = _FakeAgentRepo({})  # an agent id that resolves to no row
    with pytest.raises(APIError) as caught:
        asyncio.run(allocate_agent_scoped_uid(repo, "gone"))
    assert caught.value.status_code == 404


def test_an_empty_agent_id_is_refused_before_any_read() -> None:
    repo = _FakeAgentRepo()
    with pytest.raises(APIError):
        asyncio.run(allocate_agent_scoped_uid(repo, "  "))
    assert repo.cas_calls == []


def test_endless_contention_fails_loud_rather_than_looping() -> None:
    class _AlwaysStolen(_FakeAgentRepo):
        async def compare_and_update_agent(self, agent_id: str, **kwargs: Any) -> bool:
            self.cas_calls.append(dict(kwargs))
            return False

    repo = _AlwaysStolen({"agent_id": "a1", "conversation_uid_cursor": 2005})
    with pytest.raises(APIError) as caught:
        asyncio.run(allocate_agent_scoped_uid(repo, "a1"))
    assert caught.value.status_code == 503
