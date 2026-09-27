"""A long-lived box is widened to what its runtime now needs to reach.

An Assistant's box is created once and serves the Assistant through many
edits. Its egress policy was fixed from what the runtime needed at creation,
so an MCP server added later was unreachable, and the attach failed before
that: the sidecar refuses a Vault binding for a host its policy does not
allow. The provider now merges the missing allows into the live policy through
the sidecar's runtime policy API before the vault is refreshed.

The fake stands in for the SDK ``Sandbox`` with the vendor's own
``NetworkPolicy``/``NetworkRule`` models, the shapes ``get_egress_policy`` and
``patch_egress_rules`` exchange.
"""

from __future__ import annotations

import pytest
from opensandbox.models.sandboxes import NetworkPolicy, NetworkRule

from astrabox.providers.open_sandbox.sandbox import (
    OpenSandboxHandle,
    OpenSandboxSandboxProvider,
)
from astrabox.seams.sandbox import SandboxNetworkPolicy


class _Sidecar:
    def __init__(self, live: NetworkPolicy) -> None:
        self.id = "sb-1"
        self.live = live
        self.patched: list[list[NetworkRule]] = []

    async def get_egress_policy(self) -> NetworkPolicy:
        return self.live

    async def patch_egress_rules(self, rules: list[NetworkRule]) -> None:
        self.patched.append(list(rules))


def _created_for(*hosts: str) -> NetworkPolicy:
    return NetworkPolicy(
        defaultAction="deny",
        egress=[
            NetworkRule(action="deny", target="169.254.0.0/16"),
            *(NetworkRule(action="allow", target=host) for host in hosts),
        ],
    )


async def _admit(sidecar: _Sidecar, *hosts: str) -> None:
    await OpenSandboxSandboxProvider().admit_runtime_egress(
        OpenSandboxHandle(sidecar),  # type: ignore[arg-type]
        network_policy=SandboxNetworkPolicy(mode="limited", allowed_hosts=hosts),
        vault_write=None,
    )


@pytest.mark.asyncio
async def test_a_host_the_runtime_gained_is_added_to_the_live_policy() -> None:
    sidecar = _Sidecar(_created_for("gateway.test"))

    await _admit(sidecar, "gateway.test", "mcp.example.com")

    assert [[(r.action, r.target) for r in patch] for patch in sidecar.patched] == [
        [("allow", "mcp.example.com")]
    ]


@pytest.mark.asyncio
async def test_a_box_that_already_admits_everything_is_left_alone() -> None:
    sidecar = _Sidecar(_created_for("gateway.test", "mcp.example.com"))

    await _admit(sidecar, "gateway.test", "mcp.example.com")

    assert sidecar.patched == []


@pytest.mark.asyncio
async def test_a_host_the_runtime_dropped_stays_admitted() -> None:
    """Another conversation on the same box may still be using it."""

    sidecar = _Sidecar(_created_for("gateway.test", "mcp.example.com"))

    await _admit(sidecar, "gateway.test")

    assert sidecar.patched == []


@pytest.mark.asyncio
async def test_a_policy_that_allows_by_default_needs_no_rules() -> None:
    sidecar = _Sidecar(NetworkPolicy(defaultAction="allow", egress=None))

    await _admit(sidecar, "gateway.test", "mcp.example.com")

    assert sidecar.patched == []
