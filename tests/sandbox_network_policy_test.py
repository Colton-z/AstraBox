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
    # The cloud-metadata always-deny leads every policy (see the dedicated test
    # below); the Environment's own allows follow it in order.
    assert [rule.target for rule in limited.egress or []] == [
        "169.254.0.0/16",
        "api.example.com",
        "10.0.0.0/24",
    ]
    assert unrestricted is not None
    assert unrestricted.default_action == "allow"
    assert [(rule.action, rule.target) for rule in unrestricted.egress or []] == [
        ("deny", "169.254.0.0/16")
    ]


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

    # Every policy built through open_sandbox_network_policy leads with the
    # cloud-metadata always-deny; the vault binding hosts follow the allows.
    assert unrestricted is not None
    assert unrestricted.default_action == "allow"
    assert [rule.target for rule in unrestricted.egress or []] == [
        "169.254.0.0/16",
        "credential-only.example",
    ]
    # A bare with_vault_binding_allows(None, ...) had no policy to carry the
    # always-deny, and None means no egress sidecar at all — not a real sandbox
    # path, since every provisioned box carries a Limited or Unrestricted policy.
    assert implicit_unrestricted is not None
    assert implicit_unrestricted.default_action == "allow"
    assert [rule.target for rule in implicit_unrestricted.egress or []] == [
        "credential-only.example"
    ]
    assert environment_policy is not None
    assert [rule.target for rule in environment_policy.egress or []] == [
        "169.254.0.0/16",
        "operator.example",
    ]
    assert limited is not None
    assert limited.default_action == "deny"
    assert [rule.target for rule in limited.egress or []] == [
        "169.254.0.0/16",
        "operator.example",
        "credential-only.example",
    ]
    assert already_allowed is not None
    assert [rule.target for rule in already_allowed.egress or []] == [
        "169.254.0.0/16",
        "credential-only.example",
    ]



def test_deployment_denied_networks_lead_every_mode_ahead_of_every_allow() -> None:
    """The bridge deny must hold in Unrestricted AND against an allow entry.

    OpenSandbox's dns+nft egress drops IP/CIDR deny targets before it consults
    an allow set, so these rules keep a sandbox off other sandboxes and off the
    published sandbox ports even when an Environment is Unrestricted, or when
    an allow-list entry (or a Vault binding host) names a bridge address.
    """
    from astrabox.providers.open_sandbox.credential_vault import (
        open_sandbox_vault_write,
    )
    from astrabox.providers.open_sandbox.networking import (
        open_sandbox_network_policy,
        with_vault_binding_allows,
    )

    denied = ("172.17.0.0/28", "172.17.0.16/32")
    unrestricted = open_sandbox_network_policy(
        SandboxNetworkPolicy(mode=SANDBOX_NETWORK_UNRESTRICTED),
        denied_networks=denied,
    )
    limited = open_sandbox_network_policy(
        SandboxNetworkPolicy(
            mode=SANDBOX_NETWORK_LIMITED,
            allowed_hosts=("api.example.com", "172.17.0.1"),
        ),
        denied_networks=denied,
    )
    with_vault = with_vault_binding_allows(
        limited,
        open_sandbox_vault_write(
            SandboxEgressCredentialPlan(
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
        ),
    )

    # The cloud-metadata always-deny leads, then the configured bridge denies,
    # then the allows.
    assert unrestricted is not None
    assert unrestricted.default_action == "allow"
    assert [(rule.action, rule.target) for rule in unrestricted.egress or []] == [
        ("deny", "169.254.0.0/16"),
        ("deny", "172.17.0.0/28"),
        ("deny", "172.17.0.16/32"),
    ]
    assert limited is not None
    assert limited.default_action == "deny"
    assert [(rule.action, rule.target) for rule in limited.egress or []] == [
        ("deny", "169.254.0.0/16"),
        ("deny", "172.17.0.0/28"),
        ("deny", "172.17.0.16/32"),
        ("allow", "api.example.com"),
        ("allow", "172.17.0.1"),
    ]
    assert with_vault is not None
    assert [(rule.action, rule.target) for rule in with_vault.egress or []][:3] == [
        ("deny", "169.254.0.0/16"),
        ("deny", "172.17.0.0/28"),
        ("deny", "172.17.0.16/32"),
    ]



def test_cloud_metadata_is_denied_in_every_mode_even_with_no_configured_denies() -> None:
    """169.254.0.0/16 leads the deny rules in Limited and Unrestricted alike.

    The always-deny is a platform constant, so it holds on every deployment
    shape, including the ones (Kubernetes, external server) where the per-host
    bridge deny list is empty.
    """
    from astrabox.providers.open_sandbox.networking import (
        ALWAYS_DENIED_NETWORKS,
        open_sandbox_network_policy,
    )

    assert ALWAYS_DENIED_NETWORKS == ("169.254.0.0/16",)
    unrestricted = open_sandbox_network_policy(
        SandboxNetworkPolicy(mode=SANDBOX_NETWORK_UNRESTRICTED)
    )
    limited = open_sandbox_network_policy(
        SandboxNetworkPolicy(mode=SANDBOX_NETWORK_LIMITED, allowed_hosts=("api.example.com",))
    )
    assert unrestricted is not None
    assert unrestricted.default_action == "allow"
    assert [(r.action, r.target) for r in unrestricted.egress or []] == [
        ("deny", "169.254.0.0/16")
    ]
    assert limited is not None
    assert [(r.action, r.target) for r in limited.egress or []] == [
        ("deny", "169.254.0.0/16"),
        ("allow", "api.example.com"),
    ]


def test_the_metadata_deny_leads_the_configured_bridge_denies_without_duplication() -> None:
    from astrabox.providers.open_sandbox.networking import open_sandbox_network_policy

    policy = open_sandbox_network_policy(
        SandboxNetworkPolicy(mode=SANDBOX_NETWORK_UNRESTRICTED),
        # An operator list that already repeats the metadata range must not
        # produce two identical deny rules.
        denied_networks=("172.17.0.0/16", "169.254.0.0/16"),
    )
    assert policy is not None
    assert [(r.action, r.target) for r in policy.egress or []] == [
        ("deny", "169.254.0.0/16"),
        ("deny", "172.17.0.0/16"),
    ]


def test_an_allow_entry_inside_the_metadata_range_is_refused() -> None:
    """A literal metadata IP or CIDR in an Environment allow-list fails loud."""
    from astrabox.providers.open_sandbox.networking import open_sandbox_network_policy

    for target in ("169.254.169.254", "169.254.0.0/16", "169.254.170.2/31"):
        with pytest.raises(ValueError, match="metadata"):
            open_sandbox_network_policy(
                SandboxNetworkPolicy(
                    mode=SANDBOX_NETWORK_LIMITED, allowed_hosts=(target,)
                )
            )
    # A hostname is not judged here (no DNS at this layer); the enforced deny
    # rule blocks it after resolution.
    ok = open_sandbox_network_policy(
        SandboxNetworkPolicy(
            mode=SANDBOX_NETWORK_LIMITED, allowed_hosts=("metadata.example.com",)
        )
    )
    assert ok is not None


def test_a_vault_binding_inside_the_metadata_range_is_refused() -> None:
    """A Vault binding host inside the metadata range is refused when admitted.

    The binding is built directly here — a metadata ``base_url`` never reaches
    :func:`open_sandbox_vault_write`, which rejects it earlier — so this isolates
    the refusal in :func:`with_vault_binding_allows`, which is where an
    admin-bound Vault destination becomes an egress allow.
    """
    from types import SimpleNamespace

    from astrabox.providers.open_sandbox.networking import (
        open_sandbox_network_policy,
        with_vault_binding_allows,
    )

    vault_write = (
        [],
        [SimpleNamespace(match=SimpleNamespace(hosts=["169.254.169.254"]))],
    )
    with pytest.raises(ValueError, match="metadata"):
        with_vault_binding_allows(
            open_sandbox_network_policy(
                SandboxNetworkPolicy(mode=SANDBOX_NETWORK_UNRESTRICTED)
            ),
            vault_write,
        )


def test_denied_networks_come_from_the_validated_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic import ValidationError

    from astrabox.common.utils.settings import AstraBoxRuntimeSettings
    from astrabox.providers.open_sandbox.networking import configured_denied_networks

    monkeypatch.setenv(
        "ASTRABOX_SANDBOX_EGRESS_DENY_CIDRS", " 172.17.0.0/17, 172.17.128.0/18 ,"
    )
    assert configured_denied_networks(AstraBoxRuntimeSettings()) == (
        "172.17.0.0/17",
        "172.17.128.0/18",
    )
    monkeypatch.delenv("ASTRABOX_SANDBOX_EGRESS_DENY_CIDRS")
    assert configured_denied_networks(AstraBoxRuntimeSettings()) == ()
    monkeypatch.setenv("ASTRABOX_SANDBOX_EGRESS_DENY_CIDRS", "172.17.0.1/16")
    with pytest.raises(ValidationError, match="ASTRABOX_SANDBOX_EGRESS_DENY_CIDRS"):
        AstraBoxRuntimeSettings()


def test_network_enforcement_is_an_opt_in_provider_capability() -> None:
    from astrabox.providers.open_sandbox.sandbox import OpenSandboxSandboxProvider
    from astrabox.seams.sandbox import SandboxProvider

    assert SandboxProvider.supports_create_network_policy is False
    assert OpenSandboxSandboxProvider.supports_create_network_policy is True


# ── the network wiring a box records at create ──────────────────────────────


def _wiring_settings(**overrides: str) -> object:
    from types import SimpleNamespace

    fields = {
        "mcp_proxy_base_url": "http://172.17.0.10:8000",
        "sandbox_egress_dns_upstream": "172.17.0.9:53",
        "sandbox_egress_deny_cidrs": "172.17.0.0/29,172.17.0.8/31",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


@pytest.mark.parametrize(
    "moved",
    [
        {"mcp_proxy_base_url": "http://172.17.0.4:8000"},
        {"sandbox_egress_dns_upstream": "172.17.0.5:53"},
        {"sandbox_egress_deny_cidrs": "172.17.0.0/30"},
    ],
    ids=["edge", "dns-edge", "deny-list"],
)
def test_a_moved_address_makes_a_recorded_box_stale(moved: dict[str, str]) -> None:
    # Each is fixed into the box at create; a box that recorded the old value
    # resolves, filters or calls back with addresses the platform does not serve.
    from astrabox.providers.open_sandbox.networking import (
        SANDBOX_NETWORK_WIRING_METADATA_KEY,
        recorded_network_wiring_is_current,
        sandbox_network_wiring,
    )

    before = sandbox_network_wiring(_wiring_settings())
    record = {SANDBOX_NETWORK_WIRING_METADATA_KEY: before}
    assert recorded_network_wiring_is_current(record, _wiring_settings()) is True
    assert sandbox_network_wiring(_wiring_settings(**moved)) != before
    assert recorded_network_wiring_is_current(record, _wiring_settings(**moved)) is False


def test_the_wiring_record_fits_the_metadata_label_grammar() -> None:
    import re

    from astrabox.providers.open_sandbox.networking import sandbox_network_wiring

    value = sandbox_network_wiring(_wiring_settings())
    assert len(value) <= 63
    assert re.fullmatch(r"[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?", value)


def test_a_box_without_a_record_is_not_judged_stale() -> None:
    # Created by a release that wrote no record: its wiring cannot be judged,
    # and an upgrade that kept the edges running left it working.
    from astrabox.providers.open_sandbox.networking import (
        recorded_network_wiring_is_current,
    )

    assert recorded_network_wiring_is_current({}, _wiring_settings()) is True
