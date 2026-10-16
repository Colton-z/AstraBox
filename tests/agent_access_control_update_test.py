"""Agent authoring and access control have separate, closed write contracts.

The general create/update surface accepts only fields declared by the Agent
authoring schema. Agent authorization, Vault bindings, extension assignments,
ownership and lifecycle state belong to independently authorized services.
Every misplaced or unknown field must fail loud; silently dropping one makes a
caller believe a security-semantic change took effect when it did not.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from astrabox.api.routes import agents as agent_routes
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService


class _FakeAgentRepo:
    def __init__(self, agent: dict[str, Any]) -> None:
        self.agent = dict(agent)
        self.last_updates: dict[str, Any] | None = None
        self.create_calls = 0

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        return dict(self.agent) if agent_id == self.agent["agent_id"] else None

    async def update_definition(
        self,
        agent_id: str,
        *,
        expected_version: int,
        changes: dict[str, Any],
        updated_by: str | None,
    ) -> dict[str, Any] | None:
        if agent_id != self.agent["agent_id"] or self.agent["version"] != expected_version:
            return None
        self.last_updates = dict(changes)
        self.agent.update(changes, version=expected_version + 1, updated_by=updated_by)
        return dict(self.agent)

    async def create_agent(self, doc: dict[str, Any]) -> dict[str, Any]:
        self.create_calls += 1
        self.agent = dict(doc)
        return dict(doc)


class _User:
    def __init__(self, user_id: str, roles: list[str] | None = None) -> None:
        self.user_id = user_id
        self.roles: list[str] = list(roles or [])


class _FakeEnvironmentRepo:
    # engine_options acceptance is judged by the environment's engine, so the
    # closed-contract probes need the payload's environment to resolve. It is
    # unrestricted so the example hosts in these payloads reach no resolver;
    # what a limited Environment admits is author_boundary_test's subject.
    async def get_any_by_name(self, name: str) -> dict[str, Any] | None:
        return {
            "name": name,
            "engine_kind": "claude_code",
            "networking": {"type": "unrestricted"},
        }

    get_with_secrets = get_any_by_name


def _service(agent: dict[str, Any]) -> tuple[AgentConfigService, _FakeAgentRepo]:
    from astrabox.providers import register_builtin_providers

    register_builtin_providers()
    repo = _FakeAgentRepo(agent)
    return AgentConfigService(repo, environment_repo=_FakeEnvironmentRepo()), repo  # type: ignore[arg-type]


_REQUIRED = {
    "name": "probe",
    "model": "some-model",
    "environment_name": "claude-code",
}

_BASE = {
    "agent_id": "a-1",
    **_REQUIRED,
    "created_by": "owner",
    "user_id": "owner",
    "org_id": "default",
    "version": 1,
    "state": "ACTIVE",
    "enabled": True,
    "visibility": "private",
}


_FORBIDDEN_GENERAL_FIELDS = [
    pytest.param("unexpected", "value", id="unknown"),
    pytest.param("_id", "database-id", id="internal"),
    pytest.param("agent_id", "someone-elses-agent", id="identity"),
    pytest.param("created_by", "forged-creator", id="ownership"),
    pytest.param("user_id", "forged-owner", id="user-ownership"),
    pytest.param("org_id", "forged-org", id="organization"),
    pytest.param("can_manage", True, id="derived-capability"),
    pytest.param("visibility", "public", id="access-visibility"),
    pytest.param("admins", ["attacker"], id="access-admins"),
    pytest.param("allowed_user_ids", ["attacker"], id="access-allowlist"),
    pytest.param("credential_vault_ids", ["vault-forged"], id="vault-binding"),
    pytest.param(
        "mcp_assignments",
        [{"provider": "builtin", "item_id": "mcp-forged"}],
        id="mcp-assignment",
    ),
    pytest.param(
        "extension_catalog",
        {"skill_ids": ["skill-forged"]},
        id="extension-snapshot",
    ),
    pytest.param("sandbox_id", "sandbox-someone-elses", id="sandbox-lifecycle"),
    pytest.param("state", "DELETED", id="state-lifecycle"),
    pytest.param("deleted", True, id="deleted-lifecycle"),
    pytest.param("created_at", "forged-time", id="timestamp"),
    pytest.param("conversation_uid_cursor", 59999, id="runtime-cursor"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "value"), _FORBIDDEN_GENERAL_FIELDS)
async def test_create_rejects_each_non_authoring_field(
    field: str, value: object
) -> None:
    service, repo = _service(_BASE)

    with pytest.raises(APIError) as caught:
        await service.create_agent_config(
            _User("owner"),
            {**_REQUIRED, field: value},
        )

    assert caught.value.status_code == 400
    assert field in caught.value.message
    assert repo.create_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "value"), _FORBIDDEN_GENERAL_FIELDS)
async def test_update_rejects_each_non_authoring_field(
    field: str, value: object
) -> None:
    service, repo = _service(_BASE)

    with pytest.raises(APIError) as caught:
        await service.upsert_agent_config(
            _User("owner"),
            "a-1",
            {**_REQUIRED, "version": 1, field: value},
        )

    assert caught.value.status_code == 400
    assert field in caught.value.message
    assert repo.last_updates is None


@pytest.mark.asyncio
async def test_a_non_manager_is_refused_before_a_general_write() -> None:
    service, repo = _service(_BASE)

    with pytest.raises(APIError) as caught:
        await service.upsert_agent_config(
            _User("someone-else"),
            "a-1",
            {**_REQUIRED, "model": "changed"},
        )

    assert caught.value.status_code == 403
    assert repo.last_updates is None


@pytest.mark.asyncio
async def test_access_operation_rejects_unknown_fields() -> None:
    service, repo = _service(_BASE)

    with pytest.raises(APIError) as caught:
        await service.set_agent_access(
            _User("owner"),
            "a-1",
            {"visibility": "private", "admins": [], "unknown": True},
        )

    assert caught.value.status_code == 400
    assert "unknown" in caught.value.message
    assert repo.last_updates is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "admins",
    [None, [1], [{"user_id": "forged"}], ["  "]],
)
async def test_access_operation_rejects_invalid_user_ids(
    admins: object,
) -> None:
    service, repo = _service(_BASE)

    with pytest.raises(APIError) as caught:
        await service.set_agent_access(
            _User("owner"),
            "a-1",
            {
                "visibility": "private",
                "admins": admins,
                "allowed_user_ids": [],
            },
        )

    assert caught.value.status_code == 400
    assert "admins" in caught.value.message
    assert repo.last_updates is None


@pytest.mark.asyncio
async def test_access_operation_hides_an_invisible_agent_from_a_stranger() -> None:
    service, repo = _service(_BASE)

    with pytest.raises(APIError) as caught:
        await service.set_agent_access(
            _User("stranger"),
            "a-1",
            {"visibility": "public", "admins": [], "allowed_user_ids": []},
        )

    assert caught.value.status_code == 404
    assert repo.last_updates is None


@pytest.mark.asyncio
async def test_access_operation_refuses_a_visible_agent_to_a_non_manager() -> None:
    service, repo = _service({**_BASE, "visibility": "public"})

    with pytest.raises(APIError) as caught:
        await service.set_agent_access(
            _User("stranger"),
            "a-1",
            {"visibility": "private", "admins": [], "allowed_user_ids": []},
        )

    assert caught.value.status_code == 403
    assert repo.last_updates is None


class _RouteAgentService:
    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    async def get_agent_access(
        self, user: UserContext, agent_id: str
    ) -> dict[str, Any]:
        assert user.user_id == "route-owner"
        return {
            "created_by": user.user_id,
            "visibility": "private",
            "admins": [],
            "allowed_user_ids": [],
            "agent_id": agent_id,
        }

    async def set_agent_access(
        self,
        user: UserContext,
        agent_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.writes.append(
            {"user_id": user.user_id, "agent_id": agent_id, **payload}
        )
        return payload


async def _route_user(_request: Any) -> UserContext:
    return UserContext(user_id="route-owner")


def test_access_route_forbids_extra_fields_before_the_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _RouteAgentService()
    monkeypatch.setattr(agent_routes, "_registered_on", None)
    monkeypatch.setattr(agent_routes, "get_agent_service", lambda: service)
    monkeypatch.setattr(agent_routes, "get_current_user_context", _route_user)
    app = FastAPI()
    agent_routes.register_agent_routes(app)

    with TestClient(app) as client:
        read = client.get("/api/v1/agents/a-1/access")
        rejected = client.put(
            "/api/v1/agents/a-1/access",
            json={
                "visibility": "private",
                "credential_vault_ids": ["vault-forged"],
            },
        )
        written = client.put(
            "/api/v1/agents/a-1/access",
            json={"visibility": "allowlist", "allowed_user_ids": ["member"]},
        )

    assert read.status_code == 200
    assert read.json()["data"]["agent_id"] == "a-1"
    assert rejected.status_code == 422
    assert written.status_code == 200
    assert service.writes == [
        {
            "user_id": "route-owner",
            "agent_id": "a-1",
            "visibility": "allowlist",
            "admins": [],
            "allowed_user_ids": ["member"],
        }
    ]
