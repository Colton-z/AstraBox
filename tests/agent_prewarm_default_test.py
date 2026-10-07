"""A new Agent keeps capacity ready unless its author or the deployment says no.

The default is written into the record at create time because the capacity
sweep selects Agents whose stored ``prepared_capacity`` is positive: a create that
leaves the key out would read as off everywhere. It follows the deployment,
since prewarming needs ``ASTRABOX_AGENT_PREWARM_REDIS_URL`` and an Agent
created with it on where that store is missing could only record
``AGENT_PREWARM_CONFIG_INVALID``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from astrabox.api.routes import agents as agent_routes
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService
from astrabox.core.service.orchestrator.agent_schema import get_agent_schema
from astrabox.providers import register_builtin_providers

_REDIS_URL = "redis://redis:6379/0"


@pytest.fixture
def deployment_can_prewarm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRABOX_AGENT_PREWARM_REDIS_URL", _REDIS_URL)


@pytest.fixture
def deployment_cannot_prewarm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ASTRABOX_AGENT_PREWARM_REDIS_URL", raising=False)


class _User:
    user_id = "owner"
    roles: list[str] = []
    org_id = None


class _AgentRepo:
    def __init__(self, agent: dict[str, Any] | None = None, *, others: int = 0) -> None:
        self.agent = dict(agent) if agent else None
        self.others = others

    async def total_prepared_capacity(self, *, excluding_agent_id: str = "") -> int:
        _ = excluding_agent_id
        return self.others

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        if self.agent and agent_id == self.agent.get("agent_id"):
            return dict(self.agent)
        return None

    async def create_agent(self, doc: dict[str, Any]) -> dict[str, Any]:
        self.agent = dict(doc)
        return dict(doc)

    async def compare_and_update_agent(
        self, agent_id: str, *, expected: dict[str, Any], updates: dict[str, Any]
    ) -> bool:
        _ = agent_id, expected
        assert self.agent is not None
        self.agent.update(updates)
        return True


class _EnvironmentRepo:
    async def get_any_by_name(self, name: str) -> dict[str, Any] | None:
        if name != "claude-env":
            return None
        return {"name": name, "engine_kind": "claude_code"}


def _service(
    agent: dict[str, Any] | None = None, *, others: int = 0
) -> tuple[AgentConfigService, _AgentRepo]:
    register_builtin_providers()
    repo = _AgentRepo(agent, others=others)
    service = AgentConfigService(repo, environment_repo=_EnvironmentRepo())  # type: ignore[arg-type]
    return service, repo


def _payload(**overrides: Any) -> dict[str, Any]:
    return {
        "name": "prewarm-default",
        "model": "some-model",
        "environment_name": "claude-env",
        **overrides,
    }


def _stored_prewarm(repo: _AgentRepo) -> object:
    assert repo.agent is not None
    assert "prepared_capacity" in repo.agent, (
        "the create must store its prepared capacity; readers treat an absent key as zero"
    )
    return repo.agent["prepared_capacity"]


@pytest.mark.usefixtures("deployment_can_prewarm")
async def test_create_without_a_choice_stores_prewarming_on() -> None:
    service, repo = _service()

    created = await service.create_agent_config(_User(), _payload())

    assert _stored_prewarm(repo) == 1
    assert created["prepared_capacity"] == 1


@pytest.mark.usefixtures("deployment_cannot_prewarm")
async def test_create_without_a_choice_stays_off_where_the_deployment_cannot_prewarm() -> None:
    service, repo = _service()

    await service.create_agent_config(_User(), _payload())

    assert _stored_prewarm(repo) == 0


@pytest.mark.usefixtures("deployment_can_prewarm")
async def test_create_keeps_an_explicit_off() -> None:
    service, repo = _service()

    await service.create_agent_config(_User(), _payload(prepared_capacity=0))

    assert _stored_prewarm(repo) == 0


@pytest.mark.usefixtures("deployment_can_prewarm")
async def test_an_update_leaves_an_existing_agents_prewarm_alone() -> None:
    """Agents created before the default existed keep what they have."""
    existing = {
        "agent_id": "agent-1",
        "name": "existing",
        "created_by": "owner",
        "model": "some-model",
        "environment_name": "claude-env",
        "version": 1,
    }
    service, repo = _service(existing)

    await service.upsert_agent_config(
        _User(),
        "agent-1",
        _payload(name="existing", description="edited after the default shipped"),
    )

    assert repo.agent is not None
    assert repo.agent["description"] == "edited after the default shipped"
    assert "prepared_capacity" not in repo.agent


@pytest.mark.parametrize(
    ("redis_url", "expected"),
    [(_REDIS_URL, 1), ("", 0)],
    ids=["deployment-can-prewarm", "deployment-cannot-prewarm"],
)
def test_the_form_schema_serves_the_deployments_default(
    redis_url: str, expected: int
) -> None:
    schema = get_agent_schema(SimpleNamespace(agent_prewarm_redis_url=redis_url))

    defaults = {f["key"]: f["default"] for f in schema["fields"] if "default" in f}
    assert defaults["prepared_capacity"] == expected


@pytest.mark.parametrize(
    ("redis_url", "expected"),
    [(_REDIS_URL, 1), (None, 0)],
    ids=["deployment-can-prewarm", "deployment-cannot-prewarm"],
)
def test_the_schema_route_carries_the_default_to_the_console(
    monkeypatch: pytest.MonkeyPatch, redis_url: str | None, expected: int
) -> None:
    """The response model must not drop ``default`` on the way out."""
    if redis_url is None:
        monkeypatch.delenv("ASTRABOX_AGENT_PREWARM_REDIS_URL", raising=False)
    else:
        monkeypatch.setenv("ASTRABOX_AGENT_PREWARM_REDIS_URL", redis_url)

    async def _user(_request: object) -> UserContext:
        return UserContext(user_id="author")

    monkeypatch.setattr(agent_routes, "get_agent_service", lambda: object())
    monkeypatch.setattr(agent_routes, "get_current_user_context", _user)
    app = FastAPI()
    agent_routes.register_agent_routes(app)

    with TestClient(app) as client:
        response = client.get("/api/v1/agent-configuration/schema")

    assert response.status_code == 200, response.text
    fields = {f["key"]: f for f in response.json()["data"]["fields"]}
    assert fields["prepared_capacity"]["default"] == expected
    assert "default" not in fields["model"]


@pytest.mark.usefixtures("deployment_can_prewarm")
async def test_capacity_above_the_deployment_limit_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrabox.common.utils.errors import APIError

    monkeypatch.setenv("ASTRABOX_PREPARED_CAPACITY_LIMIT", "10")
    within, _ = _service(others=6)
    await within.create_agent_config(_User(), _payload(prepared_capacity=4))

    beyond, repo = _service(others=7)
    with pytest.raises(APIError) as refused:
        await beyond.create_agent_config(_User(), _payload(prepared_capacity=4))
    assert refused.value.code == "PREPARED_CAPACITY_LIMIT_EXCEEDED"
    assert repo.agent is None
