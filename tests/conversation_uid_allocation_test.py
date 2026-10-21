"""Allocating the POSIX owner a conversation runs as.

Under the shared-sandbox mode many conversations live in ONE box, and the thing
that keeps one out of another's files is the numeric owner OpenSandbox enforces
per isolated session. That enforcement belongs to OpenSandbox; the allocation
belongs to this module, and it has exactly two properties worth testing
because both fail silently:

* a number is never handed out twice. Not after a conversation ends, not when
  conversations start together, not by wrapping at the end of the range —
  files outlive the conversations that wrote them, so a recycled owner hands a
  live conversation read access to a dead one's data.
* running out is an error, not a wrap.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import update

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.runtime.conversation_identity import (
    CONVERSATION_UID_BASE,
    CONVERSATION_UID_CEILING,
    allocate_agent_scoped_uid,
)
from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.persistence.repository.runtime_profile_repository import RuntimeProfileRepository
from astrabox.persistence.sql import execute
from astrabox.persistence.sql.models.agents import AgentRuntime

pytestmark = pytest.mark.usefixtures("database")


async def _agent(*, cursor: int | None = None, deleted: bool = False) -> AgentRepository:
    await RuntimeProfileRepository().put(
        "env",
        {"engine_kind": "claude_code", "idle_action": "terminate", "networking": {"type": "unrestricted"}},
        updated_by="test",
    )
    repo = AgentRepository()
    await repo.create_agent(
        {
            "agent_id": "a1", "name": "a1", "model": "model", "runtime_profile_name": "env",
            "created_by": "owner", "state": "DELETED" if deleted else "ACTIVE",
        }
    )
    if cursor is not None:
        await execute(
            update(AgentRuntime.__table__)
            .where(AgentRuntime.__table__.c.agent_id == "a1")
            .values(last_conversation_uid=cursor)
        )
    return repo


async def test_conversations_starting_together_never_share_a_number() -> None:
    repo = await _agent()

    allocated = await asyncio.gather(*(allocate_agent_scoped_uid(repo, "a1") for _ in range(25)))

    assert sorted(allocated) == list(range(CONVERSATION_UID_BASE, CONVERSATION_UID_BASE + 25))


async def test_a_released_conversation_does_not_give_its_number_back() -> None:
    """A uid is safe to reuse only once nothing it owns is on disk, which this
    layer cannot know. So the cursor never moves backwards."""
    repo = await _agent(cursor=2400)
    assert await allocate_agent_scoped_uid(repo, "a1") == 2401
    assert await allocate_agent_scoped_uid(repo, "a1") == 2402


async def test_running_out_refuses_instead_of_wrapping() -> None:
    repo = await _agent(cursor=CONVERSATION_UID_CEILING)
    with pytest.raises(APIError) as caught:
        await allocate_agent_scoped_uid(repo, "a1")
    assert "exhausted" in str(caught.value.message)
    # And nothing was written — a refused allocation must not move the cursor.
    stored = await repo.get_agent("a1")
    assert stored is not None and stored["conversation_uid_cursor"] == CONVERSATION_UID_CEILING


async def test_a_cursor_below_the_base_cannot_drag_an_owner_into_system_range() -> None:
    """A row carrying a stale or hand-edited low cursor must not hand out a uid
    that collides with the image's own accounts."""
    repo = await _agent(cursor=12)
    assert await allocate_agent_scoped_uid(repo, "a1") == CONVERSATION_UID_BASE
