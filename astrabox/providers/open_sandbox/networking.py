"""Translate AstraBox sandbox networking to OpenSandbox's SDK models."""

from __future__ import annotations

import hashlib
import ipaddress
import json
from typing import Any, Sequence

from opensandbox.models.sandboxes import NetworkPolicy, NetworkRule

from astrabox.seams.sandbox import (
    SANDBOX_NETWORK_LIMITED,
    SANDBOX_NETWORK_UNRESTRICTED,
    SandboxNetworkPolicy,
)


#: Networks every sandbox's egress denies in every mode and every deployment
#: shape, independent of the Environment. ``169.254.0.0/16`` is IPv4 link-local;
#: it holds the ``169.254.169.254`` cloud metadata service, whose credentials a
#: sandbox must never reach. A platform constant, not a knob, so no Environment
#: and no deployment can turn it off; the sibling per-host bridge deny
#: (``ASTRABOX_SANDBOX_EGRESS_DENY_CIDRS``) is layered on top of it.
ALWAYS_DENIED_NETWORKS: tuple[str, ...] = ("169.254.0.0/16",)

_ALWAYS_DENIED = tuple(ipaddress.ip_network(net) for net in ALWAYS_DENIED_NETWORKS)


def _deny_targets(configured: Sequence[str]) -> list[str]:
    """The always-denied networks then the configured ones, de-duplicated."""

    targets: list[str] = []
    seen: set[Any] = set()
    for raw in (*ALWAYS_DENIED_NETWORKS, *configured):
        network = ipaddress.ip_network(str(raw).strip(), strict=False)
        if network not in seen:
            seen.add(network)
            targets.append(str(network))
    return targets


def _refuse_allow_inside_always_denied(target: str) -> None:
    """Refuse an allow entry that names an address inside an always-denied range.

    A literal IP or CIDR is judged here; a hostname is left to the enforced deny
    rule (``dns+nft`` drops the resolved IP before any allow set) and, for
    Agent-sourced hosts, to the authoring-time classifier. Failing loud beats
    emitting a policy where the metadata range is both allowed and denied and
    quietly relying on the sidecar's precedence.
    """

    try:
        block = ipaddress.ip_network(target, strict=False)
    except ValueError:
        return
    for denied in _ALWAYS_DENIED:
        if block.version == denied.version and block.overlaps(denied):
            raise ValueError(
                f"egress allow target {target!r} is inside always-denied "
                f"{denied} (the cloud metadata range) and cannot be allowed"
            )


def open_sandbox_network_policy(
    policy: SandboxNetworkPolicy | None,
    *,
    denied_networks: Sequence[str] = (),
) -> NetworkPolicy | None:
    """Map the provider-neutral reachability contract at the provider edge.

    :data:`ALWAYS_DENIED_NETWORKS` (the cloud metadata range) leads the deny
    rules in every mode, including ``Unrestricted``. ``denied_networks`` are the
    deployment's own addresses no sandbox may reach in any mode
    (``ASTRABOX_SANDBOX_EGRESS_DENY_CIDRS``: on the Docker stack, the built-in
    bridge that every other sandbox and the published sandbox ports live on),
    and follow. OpenSandbox's ``dns+nft`` egress drops every IP/CIDR deny target
    before it consults an allow set, so an Environment or Agent allow entry
    naming one of those addresses does not reopen it; an allow entry inside an
    always-denied range is refused outright rather than emitted.
    """

    if policy is None:
        return None
    denies = [
        NetworkRule(action="deny", target=network)
        for network in _deny_targets(denied_networks)
    ]
    if policy.mode == SANDBOX_NETWORK_UNRESTRICTED:
        return NetworkPolicy(defaultAction="allow", egress=denies or None)
    if policy.mode == SANDBOX_NETWORK_LIMITED:
        for host in policy.allowed_hosts:
            _refuse_allow_inside_always_denied(host)
        return NetworkPolicy(
            defaultAction="deny",
            egress=[
                *denies,
                *(NetworkRule(action="allow", target=host) for host in policy.allowed_hosts),
            ]
            or None,
        )
    raise ValueError(f"unsupported sandbox network mode {policy.mode!r}")


def configured_denied_networks(settings: Any) -> tuple[str, ...]:
    """The deployment's ``ASTRABOX_SANDBOX_EGRESS_DENY_CIDRS``, as a tuple."""

    raw = str(getattr(settings, "sandbox_egress_deny_cidrs", "") or "")
    return tuple(item for item in (part.strip() for part in raw.split(",")) if item)


#: Metadata key recording the network wiring a sandbox was created with (see
#: :func:`sandbox_network_wiring`).
SANDBOX_NETWORK_WIRING_METADATA_KEY = "astrabox.network-wiring"


def sandbox_network_wiring(settings: Any) -> str:
    """The deployment's sandbox-facing network wiring, as a label value.

    A create fixes three deployment addresses into the box for its whole life:
    the egress sidecar's DNS upstream (``OPENSANDBOX_EGRESS_DNS_UPSTREAM``, read
    once when the sidecar starts), the deny rules of its policy, and the
    callback base its engine and in-box services call. On the Docker stack all
    three are derived from the sandbox edges' addresses on Docker's built-in
    bridge, which Docker assigns anew when an edge restarts. A box whose
    recorded wiring differs from the running deployment's resolves names
    through, and calls back to, addresses the platform does not serve, so it
    cannot reach the model or the platform. The digest is compared when a box
    is connected to; the value fits OpenSandbox's metadata (label) grammar.
    """

    payload = {
        "callback_base": str(getattr(settings, "mcp_proxy_base_url", "") or "").strip(),
        "dns_upstream": str(
            getattr(settings, "sandbox_egress_dns_upstream", "") or ""
        ).strip(),
        "denied_networks": _deny_targets(configured_denied_networks(settings)),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:32]


def recorded_network_wiring_is_current(metadata: Any, settings: Any) -> bool:
    """Whether a box's recorded wiring is the running deployment's.

    A box without a record was created by a release that did not write one. Its
    wiring cannot be judged, so it is not treated as stale: an upgrade that
    leaves the sandbox edges running keeps those boxes working, and destroying
    them would cost their workspace files for nothing.
    """

    recorded = str(dict(metadata or {}).get(SANDBOX_NETWORK_WIRING_METADATA_KEY) or "").strip()
    return not recorded or recorded == sandbox_network_wiring(settings)


def with_vault_binding_allows(
    policy: NetworkPolicy | None,
    vault_write: tuple[list[Any], list[Any]] | None,
) -> NetworkPolicy | None:
    """Admit the exact destinations authorized by attached Vault bindings.

    A Vault assignment is a platform-managed grant to use its request-scoped
    credentials. OpenSandbox requires every binding host to be an explicit
    network allow, so its effective policy is the Environment policy plus those
    exact hosts. The Environment document remains unchanged. Under
    ``defaultAction=allow`` the rules are representationally redundant; under
    ``defaultAction=deny`` they are the reachability granted by the binding.
    """

    if vault_write is None:
        return policy
    hosts: list[str] = []
    seen: set[str] = set()
    for binding in vault_write[1]:
        match = getattr(binding, "match", None)
        for raw_host in list(getattr(match, "hosts", None) or []):
            host = str(raw_host or "").strip()
            if host and host not in seen:
                seen.add(host)
                hosts.append(host)
    if not hosts:
        return policy
    for host in hosts:
        _refuse_allow_inside_always_denied(host)
    existing = list(getattr(policy, "egress", None) or []) if policy is not None else []
    existing_targets = {
        str(getattr(rule, "target", "") or "").strip() for rule in existing
    }
    return NetworkPolicy(
        # No provider policy means unrestricted reachability. Otherwise retain
        # the Environment-derived action and add only the binding destinations.
        defaultAction=(
            getattr(policy, "default_action", None) if policy is not None else "allow"
        ),
        egress=[
            *existing,
            *(
                NetworkRule(action="allow", target=host)
                for host in hosts
                if host not in existing_targets
            ),
        ],
    )


__all__ = [
    "ALWAYS_DENIED_NETWORKS",
    "SANDBOX_NETWORK_WIRING_METADATA_KEY",
    "configured_denied_networks",
    "open_sandbox_network_policy",
    "recorded_network_wiring_is_current",
    "sandbox_network_wiring",
    "with_vault_binding_allows",
]
