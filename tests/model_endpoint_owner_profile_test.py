"""Where a model endpoint whose credential is not the deployment's may be used.

A RuntimeProfile is refused at save when its model endpoint cannot be kept as
it requires; an Agent when its users could not complete what the endpoint asks
of them. The endpoints are the real providers, so a provider whose declared
engines, credential owner or sign-in hosts drift fails here.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator import runtime_profile_schema
from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService
from astrabox.providers.model_anthropic import (
    AnthropicAPIKeyModelEndpointProvider,
    ClaudeLoginModelEndpointProvider,
)
from astrabox.providers.model_chatgpt_plan import ChatGPTPlanModelEndpointProvider
from astrabox.seams.model import register_model_endpoint

for _provider in (
    AnthropicAPIKeyModelEndpointProvider(),
    ClaudeLoginModelEndpointProvider(),
    ChatGPTPlanModelEndpointProvider(),
):
    register_model_endpoint(_provider)

_LOGIN_HOSTS = ["claude.ai", "platform.claude.com"]


@pytest.fixture(autouse=True)
def _vault(monkeypatch: pytest.MonkeyPatch) -> None:
    """Protected credential delivery on, unless a case turns it off."""
    monkeypatch.setattr(
        "astrabox.common.utils.settings.load_astrabox_settings",
        lambda: SimpleNamespace(sandbox_credential_vault_enabled=True),
    )


def _profile(provider: str, **overrides: Any) -> dict[str, Any]:
    return {
        "endpoint_provider": provider,
        "engine_kind": "claude_code",
        "sandbox_tenancy": "conversation",
        "networking": {"type": "limited", "allowed_hosts": list(_LOGIN_HOSTS)},
        **overrides,
    }


def _refusal(payload: dict[str, Any]) -> str:
    with pytest.raises(APIError) as refused:
        runtime_profile_schema._assert_owner_credential_is_deliverable(payload)
    return refused.value.message


@pytest.mark.parametrize("provider", ["anthropic_api_key", "claude_login"])
def test_an_anthropic_endpoint_serves_claude_code_alone_in_a_box_of_its_own(provider: str) -> None:
    runtime_profile_schema._assert_owner_credential_is_deliverable(_profile(provider))
    assert "requires engine_kind 'claude_code'" in _refusal(_profile(provider, engine_kind="codex"))
    assert "requires sandbox_tenancy 'conversation'" in _refusal(
        _profile(provider, sandbox_tenancy="agent")
    )
    assert "requires engine_kind 'codex'" in _refusal(_profile("chatgpt_plan"))


def test_an_owners_api_key_needs_protected_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    """With delivery off the key would sit in the box's environment; a login has none to deliver."""
    monkeypatch.setattr(
        "astrabox.common.utils.settings.load_astrabox_settings",
        lambda: SimpleNamespace(sandbox_credential_vault_enabled=False),
    )
    assert "ASTRABOX_SANDBOX_CREDENTIAL_VAULT" in _refusal(_profile("anthropic_api_key"))
    runtime_profile_schema._assert_owner_credential_is_deliverable(_profile("claude_login"))


def test_a_login_in_the_sandbox_needs_its_sign_in_hosts_allowed() -> None:
    """Limited networking must name both hosts, and the refusal lists the missing ones."""
    message = _refusal(_profile("claude_login", networking={"type": "limited", "allowed_hosts": ["claude.ai"]}))
    assert message.endswith("needs networking.allowed_hosts to include platform.claude.com")
    message = _refusal(_profile("claude_login", networking={"type": "limited", "allowed_hosts": []}))
    assert message.endswith("include claude.ai, platform.claude.com")
    runtime_profile_schema._assert_owner_credential_is_deliverable(
        _profile("claude_login", networking={"type": "unrestricted"})
    )


def test_an_agent_on_a_login_profile_has_a_terminal_and_no_prepared_capacity() -> None:
    """Its users sign in from the terminal; a prepared box has no credential to re-point at claim."""
    login = {"endpoint_provider": "claude_login"}
    key = {"endpoint_provider": "anthropic_api_key"}
    with pytest.raises(APIError, match="terminal_panel must be true"):
        AgentConfigService._check_sign_in_terminal(False, environment=login)
    AgentConfigService._check_sign_in_terminal(True, environment=login)
    AgentConfigService._check_sign_in_terminal(False, environment=key)
    for environment in (login, key):
        with pytest.raises(APIError, match="prepared_capacity must be 0"):
            AgentConfigService._check_prepared_capacity_owner(1, environment=environment)
        AgentConfigService._check_prepared_capacity_owner(0, environment=environment)
