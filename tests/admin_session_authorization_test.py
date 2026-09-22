from __future__ import annotations

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.admin_service import AdminService


class _AgentConfig:
    def __init__(self, access_docs: dict[str, dict]) -> None:
        self._access_docs = access_docs
        self.requests: list[str] = []

    async def get_agent_access_doc(self, agent_id: str) -> dict | None:
        self.requests.append(agent_id)
        return self._access_docs.get(agent_id)


def _service(access_docs: dict[str, dict] | None = None) -> tuple[AdminService, _AgentConfig]:
    config = _AgentConfig(access_docs or {})
    service = AdminService.__new__(AdminService)
    service._agent_config = config  # type: ignore[attr-defined]
    return service, config


def _assistant_session(*, user_id: str = "assistant-owner", hidden: bool = False) -> dict:
    return {
        "session_id": "session-1",
        "session_kind": "assistant_chat",
        "user_id": user_id,
        "agent_id": "assistant-environment",
        "workspace_ref": {
            "kind": "assistant",
            "user_id": user_id,
            "assistant_id": "asst-1",
            "engine_kind": "hermes",
        },
        "hidden": hidden,
        "owner_type": "assistant_workspace" if hidden else None,
        "owner_id": "asst-1" if hidden else None,
    }


def _agent_session(*, user_id: str = "conversation-user") -> dict:
    return {
        "session_id": "session-1",
        "session_kind": "agent_chat",
        "user_id": user_id,
        "agent_id": "agent-1",
        "workspace_ref": {"kind": "agent", "agent_id": "agent-1"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("hidden", [False, True])
async def test_assistant_owner_manages_user_and_materializer_sessions(hidden: bool) -> None:
    service, config = _service()

    await service._assert_can_manage_session(
        UserContext(user_id="assistant-owner"),
        _assistant_session(hidden=hidden),
    )

    assert config.requests == []


@pytest.mark.asyncio
async def test_platform_admin_manages_another_users_assistant_session() -> None:
    service, config = _service()

    await service._assert_can_manage_session(
        UserContext(user_id="operator", roles=["admin"]),
        _assistant_session(),
    )

    assert config.requests == []


@pytest.mark.asyncio
async def test_unrelated_user_cannot_manage_an_assistant_session() -> None:
    service, config = _service()

    with pytest.raises(APIError) as raised:
        await service._assert_can_manage_session(
            UserContext(user_id="someone-else"),
            _assistant_session(),
        )

    assert raised.value.code == "FORBIDDEN"
    assert raised.value.status_code == 403
    assert config.requests == []


@pytest.mark.asyncio
async def test_agent_session_still_uses_the_agent_management_acl() -> None:
    service, config = _service(
        {"agent-1": {"created_by": "agent-owner", "admins": []}}
    )

    with pytest.raises(APIError) as raised:
        await service._assert_can_manage_session(
            UserContext(user_id="conversation-user"),
            _agent_session(),
        )

    assert raised.value.code == "FORBIDDEN"
    assert raised.value.status_code == 403
    assert config.requests == ["agent-1"]


@pytest.mark.asyncio
async def test_agent_owner_still_manages_the_agent_session() -> None:
    service, config = _service(
        {"agent-1": {"created_by": "agent-owner", "admins": []}}
    )

    await service._assert_can_manage_session(
        UserContext(user_id="agent-owner"),
        _agent_session(),
    )

    assert config.requests == ["agent-1"]
