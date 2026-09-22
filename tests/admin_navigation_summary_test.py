from __future__ import annotations

from typing import Any

from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.admin_service import AdminService
from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService


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


class _AgentConfig:
    def __init__(self) -> None:
        self.agent_reads = 0
        self.environment_counts = 0

    async def list_agent_access_docs(self) -> list[dict[str, Any]]:
        self.agent_reads += 1
        return list(_AGENTS)

    async def count_environment_configs(self) -> int:
        self.environment_counts += 1
        return 7


class _Sessions:
    def __init__(self) -> None:
        self.scopes: list[list[str]] = []

    async def count_all_sessions(self, *, template_names: list[str]) -> int:
        self.scopes.append(template_names)
        return 11


def _service() -> tuple[AdminService, _AgentConfig, _Sessions]:
    agents = _AgentConfig()
    sessions = _Sessions()
    service = AdminService.__new__(AdminService)
    service._agent_config = agents  # type: ignore[attr-defined]
    service._sessions_repo = sessions  # type: ignore[attr-defined]
    return service, agents, sessions


async def test_navigation_summary_keeps_each_collection_read_scope() -> None:
    service, agents, sessions = _service()

    summary = await service.admin_navigation_summary(UserContext(user_id="owner"))

    assert summary == {"agents": 4, "environments": 7, "sessions": 11}
    assert sessions.scopes == [["coadmin", "owned"]]
    assert agents.agent_reads == 1
    assert agents.environment_counts == 1


async def test_platform_admin_summary_includes_every_agent_session_scope() -> None:
    service, _, sessions = _service()

    summary = await service.admin_navigation_summary(
        UserContext(user_id="operator", roles=["admin"])
    )

    assert summary["agents"] == len(_AGENTS)
    assert sessions.scopes == [["allowed", "coadmin", "hidden", "owned", "public"]]


async def test_missing_identity_never_turns_the_session_count_unscoped() -> None:
    service, _, sessions = _service()

    await service.admin_navigation_summary(UserContext(user_id=""))

    assert sessions.scopes == [[]]


class _CountOnlyEnvironmentRepository:
    def __init__(self) -> None:
        self.calls = 0

    async def count_all(self) -> int:
        self.calls += 1
        return 9


async def test_environment_count_does_not_load_environment_documents() -> None:
    repository = _CountOnlyEnvironmentRepository()
    service = AgentConfigService(
        agent_repo=object(),  # type: ignore[arg-type]
        environment_repo=repository,  # type: ignore[arg-type]
        assistant_repo=object(),  # type: ignore[arg-type]
    )

    assert await service.count_environment_configs() == 9
    assert repository.calls == 1
