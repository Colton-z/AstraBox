"""Environment networking stays provider-neutral and fail-closed.

The Environment document carries no provider SDK shape and no duplicated Vault
hosts. Each provider maps that document once, then composes the exact
destinations authorized by attached credential bindings into its effective
policy.
"""

from __future__ import annotations

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.model import AgentView
from astrabox.core.service.orchestrator.environment_networking import (
    normalized_environment_networking,
    parse_environment_networking,
)
from astrabox.core.service.orchestrator.runtime.config_resolver import (
    resolve_network_policy,
)
from astrabox.seams.sandbox import (
    SANDBOX_NETWORK_LIMITED,
    SANDBOX_NETWORK_UNRESTRICTED,
    SandboxNetworkPolicy,
)
from astrabox.seams.egress_credentials import (
    ModelEgressCredential,
    SandboxEgressCredentialPlan,
)


def _resolve(
    document: object,
    *,
    required_hosts: tuple[str, ...] = (),
    mcp_hosts: tuple[str, ...] = (),
) -> SandboxNetworkPolicy:
    return resolve_network_policy(
        AgentView(agent_id=None, name="test", networking=document),
        required_hosts=required_hosts,
        mcp_hosts=mcp_hosts,
    )


def test_omitted_networking_has_one_secure_product_default(monkeypatch) -> None:
    monkeypatch.setattr(
        "astrabox.core.service.orchestrator.runtime.config_resolver."
        "platform_callback_egress_targets",
        lambda: [],
    )

    assert _resolve(None) == SandboxNetworkPolicy(mode=SANDBOX_NETWORK_LIMITED)
    assert _resolve({}) == SandboxNetworkPolicy(mode=SANDBOX_NETWORK_LIMITED)


def test_unrestricted_networking_has_no_provider_fields_or_host_list() -> None:
    policy = _resolve(
        {
            "type": "unrestricted",
            "allowed_hosts": ["ignored.example"],
            "allow_mcp_servers": True,
        },
        required_hosts=("also-ignored.example",),
        mcp_hosts=("mcp.example",),
    )

    assert policy == SandboxNetworkPolicy(mode=SANDBOX_NETWORK_UNRESTRICTED)


def test_limited_networking_composes_only_reachability_inputs(monkeypatch) -> None:
    monkeypatch.setattr(
        "astrabox.core.service.orchestrator.runtime.config_resolver."
        "platform_callback_egress_targets",
        lambda: ["platform.internal"],
    )
    policy = _resolve(
        {
            "type": "limited",
            "allowed_hosts": ["operator.example", "MODEL.EXAMPLE"],
            "allow_mcp_servers": True,
        },
        required_hosts=("model.example", "github.com"),
        mcp_hosts=("mcp.example",),
    )

    assert policy.mode == SANDBOX_NETWORK_LIMITED
    assert policy.allowed_hosts == (
        "operator.example",
        "model.example",
        "platform.internal",
        "github.com",
        "mcp.example",
    )


def test_limited_environment_must_explicitly_admit_agent_mcp() -> None:
    with pytest.raises(APIError) as caught:
        _resolve(
            {"type": "limited", "allow_mcp_servers": False},
            mcp_hosts=("mcp.example",),
        )

    assert caught.value.code == "AGENT_MCP_NETWORK_ACCESS_DISABLED"
    assert "mcp.example" in str(caught.value)


def test_environment_networking_is_canonicalized_before_storage() -> None:
    assert normalized_environment_networking(
        {
            "type": "LIMITED",
            "allowed_hosts": [
                "API.Example.COM.",
                "api.example.com",
                "10.0.0.4",
                "10.0.0.0/24",
            ],
            "allow_mcp_servers": True,
        }
    ) == {
        "type": "limited",
        "allowed_hosts": [
            "api.example.com",
            "10.0.0.4",
            "10.0.0.0/24",
        ],
        "allow_mcp_servers": True,
    }


@pytest.mark.parametrize(
    "document",
    [
        "limited",
        {"type": "unknown"},
        {"type": "limited", "extra": True},
        {"type": "limited", "allowed_hosts": "api.example.com"},
        {"type": "limited", "allowed_hosts": ["https://api.example.com"]},
        {"type": "limited", "allowed_hosts": ["api.example.com:443"]},
        {"type": "limited", "allowed_hosts": ["api.example.com/v1"]},
        {"type": "limited", "allow_mcp_servers": "yes"},
    ],
)
def test_malformed_networking_is_refused_not_dropped(document: object) -> None:
    with pytest.raises(ValueError):
        parse_environment_networking(document)
    with pytest.raises(APIError):
        _resolve(document)


def test_the_neutral_seam_rejects_an_impossible_unrestricted_shape() -> None:
    with pytest.raises(ValueError):
        SandboxNetworkPolicy(
            mode=SANDBOX_NETWORK_UNRESTRICTED,
            allowed_hosts=("api.example.com",),
        )


def test_open_sandbox_translation_exists_only_at_its_provider_edge() -> None:
    from astrabox.providers.open_sandbox.networking import (
        open_sandbox_network_policy,
    )

    limited = open_sandbox_network_policy(
        SandboxNetworkPolicy(
            mode=SANDBOX_NETWORK_LIMITED,
            allowed_hosts=("api.example.com", "10.0.0.0/24"),
        )
    )
    unrestricted = open_sandbox_network_policy(
        SandboxNetworkPolicy(mode=SANDBOX_NETWORK_UNRESTRICTED)
    )

    assert limited is not None
    assert limited.default_action == "deny"
    assert [rule.target for rule in limited.egress or []] == [
        "api.example.com",
        "10.0.0.0/24",
    ]
    assert unrestricted is not None
    assert unrestricted.default_action == "allow"
    assert unrestricted.egress is None


def test_open_sandbox_effective_policy_admits_exact_vault_binding_hosts() -> None:
    from astrabox.providers.open_sandbox.credential_vault import (
        open_sandbox_vault_write,
    )
    from astrabox.providers.open_sandbox.networking import (
        open_sandbox_network_policy,
        with_vault_binding_allows,
    )

    plan = SandboxEgressCredentialPlan(
        model=(
            ModelEgressCredential(
                name="model",
                secret_value="secret",
                credential_header="Authorization",
                base_url="https://credential-only.example",
                request_methods=("POST",),
                request_paths=("/v1/chat",),
            ),
        )
    )
    vault_write = open_sandbox_vault_write(plan)
    unrestricted = with_vault_binding_allows(
        open_sandbox_network_policy(
            SandboxNetworkPolicy(mode=SANDBOX_NETWORK_UNRESTRICTED)
        ),
        vault_write,
    )
    implicit_unrestricted = with_vault_binding_allows(None, vault_write)
    environment_policy = open_sandbox_network_policy(
        SandboxNetworkPolicy(
            mode=SANDBOX_NETWORK_LIMITED,
            allowed_hosts=("operator.example",),
        )
    )
    limited = with_vault_binding_allows(environment_policy, vault_write)
    already_allowed = with_vault_binding_allows(
        open_sandbox_network_policy(
            SandboxNetworkPolicy(
                mode=SANDBOX_NETWORK_LIMITED,
                allowed_hosts=("credential-only.example",),
            )
        ),
        vault_write,
    )

    assert unrestricted is not None
    assert unrestricted.default_action == "allow"
    assert [rule.target for rule in unrestricted.egress or []] == [
        "credential-only.example"
    ]
    assert implicit_unrestricted is not None
    assert implicit_unrestricted.default_action == "allow"
    assert [rule.target for rule in implicit_unrestricted.egress or []] == [
        "credential-only.example"
    ]
    assert environment_policy is not None
    assert [rule.target for rule in environment_policy.egress or []] == [
        "operator.example"
    ]
    assert limited is not None
    assert limited.default_action == "deny"
    assert [rule.target for rule in limited.egress or []] == [
        "operator.example",
        "credential-only.example",
    ]
    assert already_allowed is not None
    assert [rule.target for rule in already_allowed.egress or []] == [
        "credential-only.example"
    ]


def test_network_enforcement_is_an_opt_in_provider_capability() -> None:
    from astrabox.providers.open_sandbox.sandbox import OpenSandboxSandboxProvider
    from astrabox.seams.sandbox import SandboxProvider

    assert SandboxProvider.supports_create_network_policy is False
    assert OpenSandboxSandboxProvider.supports_create_network_policy is True
