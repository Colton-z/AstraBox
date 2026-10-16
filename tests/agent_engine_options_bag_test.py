"""An Agent's engine options cannot choose the account the agent CLI runs as.

The SDK's ``user`` switch would run the CLI as another local account, root
included, so the Agent write path refuses it.
"""

from __future__ import annotations

from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService
from astrabox.providers import register_builtin_providers


class _User:
    def __init__(self, user_id: str = "owner") -> None:
        self.user_id = user_id
        self.roles: list[str] = []


class _FakeAgentRepo:
    def __init__(self, agent: dict[str, Any] | None = None) -> None:
        self.agent = dict(agent) if agent else None

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        if self.agent and agent_id == self.agent.get("agent_id"):
            return dict(self.agent)
        return None

    async def create_agent(self, doc: dict[str, Any]) -> dict[str, Any]:
        self.agent = dict(doc)
        return dict(doc)

    async def update_definition(
        self,
        agent_id: str,
        *,
        expected_version: int,
        changes: dict[str, Any],
        updated_by: str | None,
    ) -> dict[str, Any] | None:
        assert self.agent is not None
        if int(self.agent.get("version") or 1) != expected_version:
            return None
        self.agent.update(changes, version=expected_version + 1, updated_by=updated_by)
        return dict(self.agent)


class _FakeEnvironmentRepo:
    def __init__(self, environments: dict[str, dict[str, Any]]) -> None:
        self.environments = environments

    async def get_any_by_name(self, name: str) -> dict[str, Any] | None:
        env = self.environments.get(name)
        return dict(env) if env else None

    get_with_secrets = get_any_by_name


def _service(
    *,
    agent: dict[str, Any] | None = None,
    environments: dict[str, dict[str, Any]] | None = None,
) -> tuple[AgentConfigService, _FakeAgentRepo]:
    register_builtin_providers()
    repo = _FakeAgentRepo(agent)
    env_repo = _FakeEnvironmentRepo(
        environments
        or {
            "claude-env": {"name": "claude-env", "engine_kind": "claude_code"},
            "hermes-env": {"name": "hermes-env", "engine_kind": "assistant"},
            "dsh-env": {
                "name": "dsh-env",
                "engine_kind": "deepseek_harness",
            },
        }
    )
    return AgentConfigService(repo, environment_repo=env_repo), repo  # type: ignore[arg-type]


def _payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "name": "bag-probe",
        "model": "some-model",
        "environment_name": "claude-env",
    }
    payload.update(overrides)
    return payload


# ── Write gate: three fail-loud cases and the valid round-trip ──


async def test_sdk_user_is_not_agent_configurable() -> None:
    service, _repo = _service()
    with pytest.raises(APIError) as raised:
        await service.create_agent_config(
            _User(), _payload(engine_options={"sdk_options": {"user": "root"}})
        )
    assert raised.value.status_code == 400
    assert "user" in str(raised.value.message)
