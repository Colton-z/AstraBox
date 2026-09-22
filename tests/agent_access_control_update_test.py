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

    async def compare_and_update_agent(
        self, agent_id: str, *, expected: dict[str, Any], updates: dict[str, Any]
    ) -> bool:
        _ = agent_id, expected
        self.last_updates = dict(updates)
        self.agent.update(updates)
        return True

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
    # closed-contract probes need the payload's environment to resolve.
    async def get_any_by_name(self, name: str) -> dict[str, Any] | None:
        return {"name": name, "engine_kind": "claude_code"}


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

_EDITABLE_REQUEST_FIELDS = frozenset(
    {
        "name",
        "display_meta",
        "description",
        "use_cases",
        "model",
        "system",
        "engine_options",
        "skills",
        "mcp_servers",
        "default_repo",
        "plugin_repos",
        "environment_name",
        "exposure_mode",
        "idle_hibernate_seconds",
        "prewarm_enabled",
        "enabled",
    }
)

_ALL_EDITABLE_FIELDS = {
    "name": "probe",
    "display_meta": {
        "display_name": "Audit probe",
        "icon": "bot",
        "tags": ["audit"],
    },
    "description": "Boundary probe",
    "use_cases": ["review"],
    "model": "some-model",
    "system": "Inspect carefully.",
    "engine_options": {"sdk_options": {"max_turns": 4}},
    "skills": ["local-skill"],
    "mcp_servers": {"local": {"command": "local-mcp"}},
    "default_repo": {
        "url": "https://example.test/source.git",
        "protocol": "https",
        "branch": "main",
        "depth": 1,
    },
    "plugin_repos": [
        {
            "url": "https://example.test/plugins.git",
            "protocol": "https",
            "plugin_paths": ["plugins/audit"],
        }
    ],
    "environment_name": "claude-code",
    "exposure_mode": "chat_only",
    "idle_hibernate_seconds": 120,
    "prewarm_enabled": False,
    "enabled": True,
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
async def test_create_accepts_every_declared_agent_field() -> None:
    service, repo = _service(_BASE)

    await service.create_agent_config(_User("owner"), dict(_ALL_EDITABLE_FIELDS))

    assert repo.create_calls == 1
    assert _EDITABLE_REQUEST_FIELDS <= repo.agent.keys()
    assert repo.agent["visibility"] == "private"


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
async def test_create_rejects_the_update_only_version_field() -> None:
    service, repo = _service(_BASE)

    with pytest.raises(APIError) as caught:
        await service.create_agent_config(
            _User("owner"),
            {**_REQUIRED, "version": 1},
        )

    assert caught.value.status_code == 400
    assert "version" in caught.value.message
    assert repo.create_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "unexpected"),
    [
        (
            "display_meta",
            {"display_name": "Probe", "unexpected": True},
            "display_meta.unexpected",
        ),
        (
            "default_repo",
            {"url": "https://example.test/source.git", "unexpected": True},
            "default_repo.unexpected",
        ),
        (
            "plugin_repos",
            [{"url": "https://example.test/plugins.git", "unexpected": True}],
            "plugin_repos[0].unexpected",
        ),
    ],
)
async def test_create_rejects_unknown_fields_in_fixed_nested_models(
    field: str, value: object, unexpected: str
) -> None:
    service, repo = _service(_BASE)

    with pytest.raises(APIError) as caught:
        await service.create_agent_config(
            _User("owner"),
            {**_REQUIRED, field: value},
        )

    assert caught.value.status_code == 400
    assert unexpected in caught.value.message
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
async def test_update_accepts_version_only_as_the_concurrency_condition() -> None:
    service, repo = _service(_BASE)

    await service.upsert_agent_config(
        _User("owner"),
        "a-1",
        {**_REQUIRED, "model": "changed", "version": 1},
    )

    assert repo.last_updates is not None
    assert repo.agent["model"] == "changed"
    assert repo.agent["version"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [None, True, "1", 1.0, 0, -1])
async def test_update_rejects_a_non_positive_integer_version(
    version: object,
) -> None:
    service, repo = _service(_BASE)

    with pytest.raises(APIError) as caught:
        await service.upsert_agent_config(
            _User("owner"),
            "a-1",
            {**_REQUIRED, "version": version},
        )

    assert caught.value.status_code == 400
    assert "version" in caught.value.message
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


_UNCLAIMED = {
    **_BASE,
    "created_by": "system",
    "user_id": "system",
    "admins": [],
}


@pytest.mark.asyncio
async def test_a_platform_admin_may_edit_an_agent_they_did_not_create() -> None:
    service, repo = _service(_UNCLAIMED)

    await service.upsert_agent_config(
        _User("admin-user", ["admin"]),
        "a-1",
        {**_REQUIRED, "model": "changed", "version": 1},
    )

    assert repo.agent["model"] == "changed"


@pytest.mark.asyncio
async def test_the_creator_still_manages_without_the_platform_role() -> None:
    service, repo = _service(_BASE)

    await service.upsert_agent_config(
        _User("owner"),
        "a-1",
        {**_REQUIRED, "model": "changed", "version": 1},
    )

    assert repo.agent["model"] == "changed"


@pytest.mark.asyncio
async def test_access_changes_use_the_dedicated_authorized_operation() -> None:
    service, repo = _service(_BASE)

    result = await service.set_agent_access(
        _User("owner"),
        "a-1",
        {
            "visibility": "allowlist",
            "admins": ["co-admin"],
            "allowed_user_ids": ["member"],
        },
    )

    assert result == {
        "created_by": "owner",
        "visibility": "allowlist",
        "admins": ["co-admin"],
        "allowed_user_ids": ["member"],
    }
    assert repo.agent["visibility"] == "allowlist"


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


@pytest.mark.asyncio
async def test_platform_admin_may_use_the_dedicated_access_operation() -> None:
    service, repo = _service(_UNCLAIMED)

    await service.set_agent_access(
        _User("platform-admin", ["admin"]),
        "a-1",
        {"visibility": "private", "admins": [], "allowed_user_ids": []},
    )

    assert repo.agent["visibility"] == "private"


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
