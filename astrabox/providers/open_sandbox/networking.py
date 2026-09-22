"""Translate AstraBox sandbox networking to OpenSandbox's SDK models."""

from __future__ import annotations

from typing import Any

from opensandbox.models.sandboxes import NetworkPolicy, NetworkRule

from astrabox.seams.sandbox import (
    SANDBOX_NETWORK_LIMITED,
    SANDBOX_NETWORK_UNRESTRICTED,
    SandboxNetworkPolicy,
)


def open_sandbox_network_policy(
    policy: SandboxNetworkPolicy | None,
) -> NetworkPolicy | None:
    """Map the provider-neutral reachability contract at the provider edge."""

    if policy is None:
        return None
    if policy.mode == SANDBOX_NETWORK_UNRESTRICTED:
        return NetworkPolicy(defaultAction="allow", egress=None)
    if policy.mode == SANDBOX_NETWORK_LIMITED:
        return NetworkPolicy(
            defaultAction="deny",
            egress=[
                NetworkRule(action="allow", target=host)
                for host in policy.allowed_hosts
            ]
            or None,
        )
    raise ValueError(f"unsupported sandbox network mode {policy.mode!r}")


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


__all__ = ["open_sandbox_network_policy", "with_vault_binding_allows"]
