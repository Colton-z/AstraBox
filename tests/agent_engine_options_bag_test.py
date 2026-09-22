"""The Agent's engine_options bag: declared by the engine, judged at write.

The bag's keys mean nothing to the platform. The adapter declares the shape
(``EngineRuntimeCapabilities.engine_options_schema``), registration refuses a
malformed declaration, the Agent write path refuses a bag the declaration does
not cover, and the stored values travel to the adapter verbatim. Each gate is
fed its own violation here so a green run proves the gate reads, not just that
the tree is clean.
"""

from __future__ import annotations

from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService
from astrabox.core.service.orchestrator.engine.capabilities import (
    capabilities_for_engine_kind,
    validate_engine_options_schema,
)
from astrabox.core.service.orchestrator.engine.claude_code_options import (
    CLAUDE_ENGINE_OPTION_KEYS,
    CLAUDE_PLATFORM_OPTION_KEYS,
    CLAUDE_RUNNER_CONTROLLED_KEYS,
)
from astrabox.core.service.orchestrator.engine.claude_code_config import (
    apply_claude_engine_options,
)
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

    async def compare_and_update_agent(
        self, agent_id: str, *, expected: dict[str, Any], updates: dict[str, Any]
    ) -> bool:
        _ = agent_id, expected
        assert self.agent is not None
        self.agent.update(updates)
        return True


class _FakeEnvironmentRepo:
    def __init__(self, environments: dict[str, dict[str, Any]]) -> None:
        self.environments = environments

    async def get_any_by_name(self, name: str) -> dict[str, Any] | None:
        env = self.environments.get(name)
        return dict(env) if env else None


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


# ── Registration gate: a malformed declaration dies before any Agent can ──


def test_registration_refuses_malformed_declarations() -> None:
    ok = ({"key": "settings", "type": "object", "protected_keys": ["sessionId"]},)
    validate_engine_options_schema("probe", ok)

    with pytest.raises(ValueError, match="unknown declaration keys"):
        validate_engine_options_schema("probe", ({"key": "a", "type": "object", "secret": True},))
    with pytest.raises(ValueError, match="snake_case"):
        validate_engine_options_schema("probe", ({"key": "Not-Snake", "type": "object"},))
    with pytest.raises(ValueError, match="duplicates"):
        validate_engine_options_schema(
            "probe", ({"key": "a", "type": "object"}, {"key": "a", "type": "object"})
        )
    with pytest.raises(ValueError, match="object"):
        validate_engine_options_schema("probe", ({"key": "a", "type": "secret"},))
    with pytest.raises(ValueError, match="object"):
        validate_engine_options_schema("probe", ({"key": "a", "type": "enum"},))
    with pytest.raises(ValueError, match="enum"):
        validate_engine_options_schema(
            "probe", ({"key": "a", "type": "object", "enum": ["x"]},)
        )
    with pytest.raises(ValueError, match="item_schema"):
        validate_engine_options_schema(
            "probe",
            ({"key": "a", "type": "object", "item_schema": [{"key": "b", "type": "string"}]},),
        )


# ── Write gate: three fail-loud cases and the valid round-trip ──


async def test_valid_bag_is_stored_verbatim_and_reaches_sdk_kwargs() -> None:
    service, repo = _service()
    native = {
        "allowed_tools": ["Bash"],
        "max_turns": 7,
        "strict_mcp_config": True,
        "output_format": {
            "type": "json_schema",
            "schema": {"type": "object", "properties": {"answer": {"type": "string"}}},
        },
        "agents": {
            "reviewer": {
                "description": "Reviews the proposed change",
                "prompt": "Review the change for correctness.",
            }
        },
    }
    bag = {"sdk_options": native}
    stored = await service.create_agent_config(
        _User(), _payload(engine_options=bag)
    )
    assert stored["engine_options"] == bag
    assert repo.agent is not None and repo.agent["engine_options"] == bag

    # The runtime mapping interprets nothing: stored keys land in SDK kwargs
    # as written (nulls dropped), because admissibility was settled above.
    from types import SimpleNamespace

    kwargs: dict[str, Any] = {}
    apply_claude_engine_options(kwargs, SimpleNamespace(engine_options=bag))
    assert kwargs == native


def test_adapter_declares_native_block_and_platform_boundary_only() -> None:
    register_builtin_providers()
    declared = capabilities_for_engine_kind("claude_code").engine_options_schema
    declared_keys = frozenset(str(field["key"]) for field in declared)
    assert declared_keys == CLAUDE_ENGINE_OPTION_KEYS
    assert declared_keys == {"sdk_options"}
    assert declared[0]["type"] == "object"
    assert "item_schema" not in declared[0]
    assert set(declared[0]["protected_keys"]) == (
        CLAUDE_PLATFORM_OPTION_KEYS | CLAUDE_RUNNER_CONTROLLED_KEYS
    )


async def test_sdk_user_is_not_agent_configurable() -> None:
    service, _repo = _service()
    with pytest.raises(APIError) as raised:
        await service.create_agent_config(
            _User(), _payload(engine_options={"sdk_options": {"user": "root"}})
        )
    assert raised.value.status_code == 400
    assert "user" in str(raised.value.message)


async def test_platform_composed_option_is_not_agent_configurable() -> None:
    # The other half of the same rule: a field the ADAPTER composes is refused
    # rather than accepted-and-overwritten, which would read back to the author
    # as configuration and is not.
    service, _repo = _service()
    with pytest.raises(APIError) as raised:
        await service.create_agent_config(
            _User(), _payload(engine_options={"sdk_options": {"cwd": "/tmp/elsewhere"}})
        )
    assert raised.value.status_code == 400
    assert "cwd" in str(raised.value.message)


def test_runtime_refuses_a_stored_option_outside_the_adapter_contract() -> None:
    from types import SimpleNamespace

    from astrabox.core.service.orchestrator.engine.claude_code_config import (
        apply_claude_engine_options,
    )

    with pytest.raises(APIError) as raised:
        apply_claude_engine_options(
            {}, SimpleNamespace(engine_options={"sdk_options": {"cwd": "/tmp/elsewhere"}})
        )
    assert raised.value.status_code == 500
    assert raised.value.code == "CLAUDE_OPTIONS_INVALID"


async def test_unknown_key_fails_loud_at_create() -> None:
    service, _repo = _service()
    with pytest.raises(APIError) as raised:
        await service.create_agent_config(
            _User(), _payload(engine_options={"presets": "x"})
        )
    assert raised.value.status_code == 400
    assert "presets" in str(raised.value.message)


async def test_unknown_native_fields_are_stored_and_forwarded_unchanged() -> None:
    from types import SimpleNamespace

    service, _repo = _service()
    native = {"futureVendorOption": {"nested": [True, None, {"max_turns": "vendor-owned"}]}}
    bag = {"sdk_options": native}
    stored = await service.create_agent_config(_User(), _payload(engine_options=bag))
    assert stored["engine_options"] == bag
    kwargs: dict[str, Any] = {}
    apply_claude_engine_options(kwargs, SimpleNamespace(engine_options=bag))
    assert kwargs == native


async def test_type_mismatch_fails_loud_at_create() -> None:
    service, _repo = _service()
    with pytest.raises(APIError) as raised:
        await service.create_agent_config(
            _User(), _payload(engine_options={"sdk_options": "not-an-object"})
        )
    assert raised.value.status_code == 400
    assert "sdk_options" in str(raised.value.message)


async def test_engine_without_declaration_refuses_any_bag() -> None:
    service, _repo = _service()
    with pytest.raises(APIError) as raised:
        await service.create_agent_config(
            _User(),
            _payload(environment_name="hermes-env", engine_options={"max_turns": 1}),
        )
    assert raised.value.status_code == 400
    assert "does not support Agent sessions" in str(raised.value.message)


async def test_agent_write_refuses_an_assistant_environment_without_a_bag() -> None:
    service, repo = _service()

    with pytest.raises(APIError) as raised:
        await service.create_agent_config(
            _User(), _payload(environment_name="hermes-env")
        )

    assert raised.value.status_code == 400
    assert "session_kind='agent_chat'" in str(raised.value.message)
    assert repo.agent is None


async def test_environment_switch_revalidates_the_stored_bag() -> None:
    # Switching only the environment can strand a valid bag on an engine that
    # declares none of its keys; the update judges the EFFECTIVE pair.
    agent = {
        "agent_id": "a-1",
        "name": "bag-probe",
        "model": "some-model",
        "environment_name": "claude-env",
        "engine_options": {"sdk_options": {"max_turns": 7}},
        "created_by": "owner",
        "user_id": "owner",
        "version": 1,
    }
    service, _repo = _service(agent=agent)
    with pytest.raises(APIError) as raised:
        await service.upsert_agent_config(
            _User(),
            "a-1",
            {"model": "some-model", "environment_name": "hermes-env"},
        )
    assert raised.value.status_code == 400
    assert "does not support Agent sessions" in str(raised.value.message)

    # Clearing the bag removes that conflict, but cannot turn an Assistant-only
    # engine into one that supports Agent Sessions.
    with pytest.raises(APIError) as cleared:
        await service.upsert_agent_config(
            _User(),
            "a-1",
            {
                "model": "some-model",
                "environment_name": "hermes-env",
                "engine_options": {},
            },
        )
    assert cleared.value.status_code == 400
    assert "session_kind='agent_chat'" in str(cleared.value.message)


async def test_engine_without_plugin_support_refuses_plugin_repos_at_write() -> None:
    service, _repo = _service()

    with pytest.raises(APIError) as raised:
        await service.create_agent_config(
            _User(),
            _payload(
                environment_name="dsh-env",
                plugin_repos=[
                    {
                        "url": "https://example.invalid/plugin.git",
                        "protocol": "https",
                    }
                ],
            ),
        )

    assert raised.value.status_code == 400
    assert "plugin_repos" in raised.value.message
    assert "deepseek_harness" in raised.value.message


async def test_environment_switch_revalidates_stored_configuration_inputs() -> None:
    agent = {
        "agent_id": "a-1",
        "name": "plugin-probe",
        "model": "some-model",
        "environment_name": "claude-env",
        "plugin_repos": [
            {
                "url": "https://example.invalid/plugin.git",
                "protocol": "https",
            }
        ],
        "created_by": "owner",
        "user_id": "owner",
        "version": 1,
    }
    service, _repo = _service(agent=agent)

    with pytest.raises(APIError) as raised:
        await service.upsert_agent_config(
            _User(),
            "a-1",
            {"model": "some-model", "environment_name": "dsh-env"},
        )
    assert raised.value.status_code == 400
    assert "plugin_repos" in raised.value.message

    updated = await service.upsert_agent_config(
        _User(),
        "a-1",
        {
            "model": "some-model",
            "environment_name": "dsh-env",
            "plugin_repos": [],
        },
    )
    assert updated["environment_name"] == "dsh-env"
