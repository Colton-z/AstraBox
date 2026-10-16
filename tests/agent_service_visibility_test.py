"""Reads and deletes on the agent surface answer to the same visibility rules.

An agent's `visibility` is set in the console and was applied only where the
console listed agents to manage. The routes users actually reach —
`GET /api/v1/agents`, `GET /api/v1/agents/{id}`, `DELETE /api/v1/agents/{id}` —
took a `user` and immediately discarded it (`_ = user`), so `private` decorated
a record that any signed-in user could still enumerate, read, and delete.

Delete is the one that hurts: no ownership check at all meant any account could
destroy any agent, including one it could not see.
"""

from __future__ import annotations

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.agent.agent_service import AgentService
from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.persistence.repository.environment_repository import EnvironmentRepository

pytestmark = pytest.mark.usefixtures("database")


class _User:
    def __init__(self, user_id: str, roles: list[str] | None = None) -> None:
        self.user_id = user_id
        self.roles: list[str] = list(roles or [])
        self.org_id = "org"


PUBLIC = {
    "agent_id": "pub", "name": "Public", "visibility": "public", "created_by": "owner",
    "updated_at": "2026-01-02T00:00:00+00:00",
}
PRIVATE = {
    "agent_id": "prv", "name": "Private", "visibility": "private", "created_by": "owner",
    "updated_at": "2026-01-01T00:00:00+00:00",
}


async def _service() -> tuple[AgentService, AgentRepository]:
    await EnvironmentRepository().put(
        "env",
        {"engine_kind": "claude_code", "idle_action": "terminate", "networking": {"type": "unrestricted"}},
        updated_by="test",
    )
    repo = AgentRepository()
    for row in (PUBLIC, PRIVATE):
        await repo.create_agent({**row, "model": "model", "environment_name": "env"})
    service = AgentService.__new__(AgentService)  # no runtime/sandbox deps needed here
    service._agent_repo = repo  # type: ignore[attr-defined]
    service.ensure_bootstrap = _noop  # type: ignore[assignment]
    service._sanitize = lambda doc: doc  # type: ignore[assignment]
    return service, repo


async def _noop() -> None:
    return None


@pytest.mark.asyncio
async def test_the_picker_hides_an_agent_the_user_may_not_see() -> None:
    service, _ = await _service()
    agents = await service.list_agents(_User("stranger"))
    names = [a["name"] for a in agents]
    assert names == ["Public"]
    assert agents[0]["can_manage"] is False


@pytest.mark.asyncio
async def test_reading_an_invisible_agent_by_id_is_a_404_not_a_403() -> None:
    # A 403 would confirm the id names something real, which is the whole point
    # of `private`. Missing and invisible must be indistinguishable.
    service, _ = await _service()
    with pytest.raises(APIError) as caught:
        await service.get_agent(_User("stranger"), "prv")
    assert caught.value.status_code == 404


@pytest.mark.asyncio
async def test_a_stranger_cannot_delete_an_agent_they_can_see() -> None:
    service, repo = await _service()
    with pytest.raises(APIError) as caught:
        await service.delete_agent(_User("stranger"), "pub")
    assert caught.value.status_code == 403
    assert await repo.get_agent("pub") is not None


@pytest.mark.asyncio
async def test_a_stranger_cannot_delete_an_agent_they_cannot_even_see() -> None:
    service, repo = await _service()
    with pytest.raises(APIError) as caught:
        await service.delete_agent(_User("stranger"), "prv")
    assert caught.value.status_code == 404
    assert await repo.get_agent("prv") is not None


@pytest.mark.asyncio
async def test_delegated_channel_start_keeps_the_execution_user_without_opening_browser_access() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from astrabox.common.utils.user_context import UserContext

    service, _ = await _service()
    service._agent_config = SimpleNamespace(resolve_agent_harness=AsyncMock(return_value=None))
    lifecycle = SimpleNamespace(create_session=AsyncMock(return_value={"session_id": "delegated"}))
    service._platform = SimpleNamespace(_session_lifecycle=lifecycle)
    user = UserContext(user_id="executor")
    with pytest.raises(APIError) as error:
        await service.start_conversation(user, "prv")
    assert error.value.status_code == 403
    lifecycle.create_session.assert_not_awaited()
    started = await service.start_integration_conversation(user, "prv")
    assert started["session_id"] == "delegated"
    assert lifecycle.create_session.call_args.args == (user,)
    assert user.roles == []
    assert lifecycle.create_session.call_args.kwargs["agent_id"] == "prv"
