from __future__ import annotations

import pytest

from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.admin_service import AdminService
from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService
from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.persistence.repository.runtime_profile_repository import RuntimeProfileRepository

pytestmark = pytest.mark.usefixtures("database")

_AGENTS = [
    {"name": "public", "visibility": "public", "created_by": "other"},
    {"name": "owned", "visibility": "private", "created_by": "owner"},
    {
        "name": "allowed",
        "visibility": "allowlist",
        "created_by": "other",
        "allowed_user_ids": ["owner"],
    },
    {"name": "hidden", "visibility": "private", "created_by": "other"},
    {
        "name": "coadmin",
        "visibility": "private",
        "created_by": "other",
        "admins": ["owner"],
    },
]


class _Sessions:
    def __init__(self) -> None:
        self.scopes: list[list[str]] = []

    async def count_all_sessions(self, *, agent_ids: list[str]) -> int:
        self.scopes.append(agent_ids)
        return 11


async def _service() -> tuple[AdminService, _Sessions]:
    runtime_profiles = RuntimeProfileRepository()
    for name in ("one", "two", "three", "four", "five", "six", "seven"):
        await runtime_profiles.put(
            name,
            {"engine_kind": "claude_code", "idle_action": "terminate", "networking": {"type": "unrestricted"}},
            updated_by="test",
        )
    agents = AgentRepository()
    for index, agent in enumerate(_AGENTS):
        await agents.create_agent(
            {"agent_id": f"agent-{index}", "model": "model", "runtime_profile_name": "one", **agent}
        )
    sessions = _Sessions()
    service = AdminService.__new__(AdminService)
    service._agent_config = AgentConfigService(agents, runtime_profiles)  # type: ignore[attr-defined]
    service._sessions_repo = sessions  # type: ignore[attr-defined]
    return service, sessions


async def test_navigation_summary_keeps_each_collection_read_scope() -> None:
    service, sessions = await _service()

    summary = await service.admin_navigation_summary(UserContext(user_id="owner"))

    assert summary == {"agents": 4, "runtime_profiles": 7, "sessions": 11}
    # The Agents ``owner`` created ("owned") and co-administers ("coadmin").
    assert sessions.scopes == [["agent-1", "agent-4"]]


async def test_a_managers_sessions_leave_out_another_agent_with_the_same_name() -> None:
    """Agent names are not unique; a manager's scope is the Agents they manage, by id."""
    from astrabox.persistence.repository.session_repository import SessionRepository

    runtime_profiles = RuntimeProfileRepository()
    await runtime_profiles.put(
        "one",
        {"engine_kind": "claude_code", "idle_action": "terminate", "networking": {"type": "unrestricted"}},
        updated_by="test",
    )
    agents = AgentRepository()
    for agent_id, creator in (("mine", "owner"), ("theirs", "other")):
        await agents.create_agent({
            "agent_id": agent_id, "name": "Research", "model": "model",
            "runtime_profile_name": "one", "created_by": creator,
        })
    for agent_id in ("mine", "theirs"):
        await SessionRepository().create_session({
            "session_id": f"session-{agent_id}", "user_id": "someone", "agent_id": agent_id,
            "session_kind": "agent_chat", "engine_kind": "claude_code", "state": "READY",
        })
    service = AdminService.__new__(AdminService)
    service._agent_config = AgentConfigService(agents, runtime_profiles)  # type: ignore[attr-defined]
    service._sessions_repo = SessionRepository()  # type: ignore[attr-defined]

    listed = [
        row["session_id"]
        async for row in service.admin_iter_manageable_sessions(UserContext(user_id="owner"))
    ]

    assert listed == ["session-mine"]
