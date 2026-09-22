"""Model access crosses the engine seam once, without vendor wire names."""

from __future__ import annotations

from types import SimpleNamespace

import astrabox.providers.model  # noqa: F401
from astrabox.core.service.orchestrator.engine.platform import EnginePlatform
from astrabox.core.service.orchestrator.engine.claude_code_config import (
    build_claude_model_config_kwargs,
)
from astrabox.core.service.orchestrator.runtime.config_resolver import (
    RuntimeConfigResolver,
)
from astrabox.core.service.orchestrator.session_title_service import (
    ConversationTitleModel,
)
from astrabox.seams.model import (
    ModelEndpoint,
    ModelEndpointProvider,
    ResolvedModelAccess,
    register_model_endpoint,
)


class _PassthroughProvider(ModelEndpointProvider):
    name = "model-access-test"

    def resolve(self, *, requested: ModelEndpoint, settings=None) -> ModelEndpoint:
        _ = settings
        return requested


register_model_endpoint(_PassthroughProvider())


def test_runtime_resolves_one_engine_neutral_model_access_value(
    monkeypatch,
) -> None:
    for name in (
        "ASTRABOX_MODEL_API_KEY",
        "ASTRABOX_MODEL_API_KEY_SECRET_NAME",
        "ANTHROPIC_AUTH_TOKEN",
        "ASTRABOX_LLM_AUTH_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-direct")
    resolver = RuntimeConfigResolver(
        SimpleNamespace(
            model_api_key="sk-direct",
            model_api_key_secret_name="",
            model_base_url="https://api.example.test/v1/messages",
            model_name="model-a",
            model_endpoint_provider="model-access-test",
            mcp_proxy_base_url="https://astrabox.example.test",
            sandbox_credential_vault_enabled=False,
        )
    )

    access = resolver.resolve_model_access(
        {
            "base_url": "https://api.example.test/v1/messages",
            "model_name": "model-a",
        }
    )

    assert isinstance(access, ResolvedModelAccess)
    assert access.credential == "sk-direct"
    assert access.credential_kind == "api_key"
    assert access.base_url == "https://api.example.test/v1/messages"
    assert access.model_name == "model-a"
    assert access.endpoint_provider == "model-access-test"


def test_engine_platform_exposes_one_model_access_operation() -> None:
    members = vars(EnginePlatform)

    assert "resolve_model_access" in members
    assert "resolve_model_config" not in members
    assert "resolve_model_api_key" not in members
    assert "resolve_model_base_url" not in members


def test_claude_adapter_maps_neutral_access_to_its_native_contract() -> None:
    kwargs = build_claude_model_config_kwargs(
        ResolvedModelAccess(
            configuration={},
            base_url="https://api.anthropic.test/v1/messages",
            model_name="claude-model",
            credential="sk-ant-test",
            credential_kind="api_key",
            endpoint_provider="direct-test",
        )
    )

    assert kwargs == {
        "api_key": "sk-ant-test",
        "base_url": "https://api.anthropic.test",
        "model_name": "claude-model",
        "credential_header": "ANTHROPIC_API_KEY",
        "endpoint_provider": "direct-test",
    }


def test_title_model_uses_neutral_defaults_and_cohesive_title_overrides() -> None:
    settings = SimpleNamespace(
        title_model_base_url="https://title.example.test/v1/chat/completions",
        title_model_name="title-model",
        title_model_api_key="title-key",
        title_model_api_key_secret_name="",
        title_model_max_tokens=96,
        title_model_request_timeout_seconds=60.0,
    )
    model = ConversationTitleModel(settings=settings)
    model._resolver = SimpleNamespace(
        resolve_model_access=lambda raw: ResolvedModelAccess(
            configuration=dict(raw),
            base_url="https://main.example.test/v1",
            model_name="main-model",
            credential="main-key",
            credential_kind="bearer",
            endpoint_provider="litellm",
        )
    )

    request = model.resolve_request_config()

    assert request.base_url == "https://title.example.test/v1"
    assert request.model_name == "title-model"
    assert request.api_key == "title-key"
    assert request.max_tokens == 96
