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

from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.agent.agent_service import AgentService


class _User:
    def __init__(self, user_id: str, roles: list[str] | None = None) -> None:
        self.user_id = user_id
        self.roles: list[str] = list(roles or [])
        self.org_id = "org"


PUBLIC = {"agent_id": "pub", "name": "Public", "visibility": "public", "created_by": "owner"}
PRIVATE = {"agent_id": "prv", "name": "Private", "visibility": "private", "created_by": "owner"}


class _FakeRepo:
    def __init__(self) -> None:
        self.rows = [dict(PUBLIC), dict(PRIVATE)]
        self.deleted: list[str] = []

    async def list_all_agents(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.rows]

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        for r in self.rows:
            if r["agent_id"] == agent_id:
                return dict(r)
        return None


def _service() -> tuple[AgentService, _FakeRepo]:
    repo = _FakeRepo()
    service = AgentService.__new__(AgentService)  # no runtime/sandbox deps needed here
    service._agent_repo = repo  # type: ignore[attr-defined]
    service.ensure_bootstrap = _noop  # type: ignore[assignment]
    service._sanitize = lambda doc: doc  # type: ignore[assignment]
    return service, repo


async def _noop() -> None:
    return None


@pytest.mark.asyncio
async def test_the_picker_hides_an_agent_the_user_may_not_see() -> None:
    service, _ = _service()
    agents = await service.list_agents(_User("stranger"))
    names = [a["name"] for a in agents]
    assert names == ["Public"]
    assert agents[0]["can_manage"] is False


@pytest.mark.asyncio
async def test_the_creator_still_sees_their_private_agent() -> None:
    service, _ = _service()
    agents = await service.list_agents(_User("owner"))
    names = [a["name"] for a in agents]
    assert names == ["Public", "Private"]
    assert all(agent["can_manage"] is True for agent in agents)


@pytest.mark.asyncio
async def test_a_platform_admin_sees_every_agent() -> None:
    service, _ = _service()
    agents = await service.list_agents(_User("ops", ["admin"]))
    names = [a["name"] for a in agents]
    assert names == ["Public", "Private"]
    assert all(agent["can_manage"] is True for agent in agents)


@pytest.mark.asyncio
async def test_reading_an_invisible_agent_by_id_is_a_404_not_a_403() -> None:
    # A 403 would confirm the id names something real, which is the whole point
    # of `private`. Missing and invisible must be indistinguishable.
    service, _ = _service()
    with pytest.raises(APIError) as caught:
        await service.get_agent(_User("stranger"), "prv")
    assert caught.value.status_code == 404


@pytest.mark.asyncio
async def test_a_stranger_cannot_delete_an_agent_they_can_see() -> None:
    service, repo = _service()
    with pytest.raises(APIError) as caught:
        await service.delete_agent(_User("stranger"), "pub")
    assert caught.value.status_code == 403
    assert repo.deleted == []


@pytest.mark.asyncio
async def test_a_stranger_cannot_delete_an_agent_they_cannot_even_see() -> None:
    service, repo = _service()
    with pytest.raises(APIError) as caught:
        await service.delete_agent(_User("stranger"), "prv")
    assert caught.value.status_code == 404
    assert repo.deleted == []


@pytest.mark.asyncio
async def test_delegated_channel_start_keeps_the_execution_user_without_opening_browser_access() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from astrabox.common.utils.user_context import UserContext

    service, _ = _service()
    service._agent_config = SimpleNamespace(resolve_agent_harness=AsyncMock(return_value=None))
    kernel = SimpleNamespace(create_session=AsyncMock(return_value={"session_id": "delegated"}))
    service._platform = SimpleNamespace(_session_kernel=kernel)
    user = UserContext(user_id="executor")
    with pytest.raises(APIError) as error:
        await service.start_conversation(user, "prv")
    assert error.value.status_code == 403
    kernel.create_session.assert_not_awaited()
    started = await service.start_integration_conversation(user, "prv")
    assert started["session_id"] == "delegated"
    assert kernel.create_session.call_args.args == (user, "prv")
    assert user.roles == []
    assert kernel.create_session.call_args.kwargs["agent_id"] == "prv"
