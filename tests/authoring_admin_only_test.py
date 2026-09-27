"""ASTRABOX_AUTHORING_ADMIN_ONLY reserves creating Agents and Assistants.

What any author's fields may reach is bounded by ``author_boundary``. This
setting answers a different question, who authors at all: a deployment whose
users should only use the Agents an administrator published (an open or
public one) sets it so only administrators create them. Without it every
authenticated user may create, which is the default.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService
from astrabox.core.service.orchestrator.assistant.assistant_service import AssistantService

_AGENT = {"name": "probe", "model": "some-model", "environment_name": "claude-code"}
_ASSISTANT = {"display_name": "Jarvis", "environment_name": "env-1"}


class _AgentRepo:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    async def create_agent(self, doc: dict[str, Any]) -> dict[str, Any]:
        self.created.append(dict(doc))
        return dict(doc)


class _EnvironmentRepo:
    async def get_any_by_name(self, name: str) -> dict[str, Any] | None:
        return {"name": name, "engine_kind": "claude_code"}


class _CatalogRepo:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    async def create_assistant(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.created.append(dict(payload))
        return dict(payload)


class _Environments:
    async def get_environment(self, name: str) -> dict[str, Any]:
        return {"name": name, "enabled": True, "engine_kind": "assistant"}


def _agent_service() -> tuple[AgentConfigService, _AgentRepo]:
    from astrabox.providers import register_builtin_providers

    register_builtin_providers()
    repo = _AgentRepo()
    return AgentConfigService(repo, environment_repo=_EnvironmentRepo()), repo  # type: ignore[arg-type]


def _assistant_service() -> tuple[AssistantService, _CatalogRepo]:
    from astrabox.providers import register_builtin_providers

    register_builtin_providers()
    catalog = _CatalogRepo()
    service = AssistantService(
        agent_config=_Environments(),
        session_kernel=None,
        catalog_repo=catalog,  # type: ignore[arg-type]
        workspace_service=SimpleNamespace(),  # type: ignore[arg-type]
        spawn_background_task=lambda coroutine, **_: coroutine.close(),
    )
    return service, catalog


def _user(*roles: str) -> Any:
    return SimpleNamespace(user_id="visitor-1", org_id="default", roles=list(roles))


async def test_a_non_administrator_cannot_create_an_agent_when_authoring_is_reserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_AUTHORING_ADMIN_ONLY", "true")
    service, repo = _agent_service()

    with pytest.raises(APIError) as raised:
        await service.create_agent_config(_user(), dict(_AGENT))

    assert raised.value.status_code == 403
    assert raised.value.code == "FORBIDDEN"
    assert repo.created == []


async def test_an_administrator_still_creates_agents_when_authoring_is_reserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_AUTHORING_ADMIN_ONLY", "true")
    service, repo = _agent_service()

    created = await service.create_agent_config(_user("admin"), dict(_AGENT))

    assert created["name"] == "probe"
    assert [doc["created_by"] for doc in repo.created] == ["visitor-1"]


async def test_every_user_creates_agents_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ASTRABOX_AUTHORING_ADMIN_ONLY", raising=False)
    service, repo = _agent_service()

    await service.create_agent_config(_user(), dict(_AGENT))

    assert len(repo.created) == 1


async def test_a_non_administrator_cannot_create_an_assistant_when_authoring_is_reserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_AUTHORING_ADMIN_ONLY", "true")
    service, catalog = _assistant_service()

    with pytest.raises(APIError) as raised:
        await service.create_assistant(_user(), dict(_ASSISTANT))

    assert raised.value.status_code == 403
    assert raised.value.code == "FORBIDDEN"
    assert catalog.created == []


async def test_an_administrator_still_creates_assistants_when_authoring_is_reserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_AUTHORING_ADMIN_ONLY", "true")
    service, catalog = _assistant_service()

    await service.create_assistant(_user("admin"), dict(_ASSISTANT))

    assert [row["owner_id"] for row in catalog.created] == ["visitor-1"]
