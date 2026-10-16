"""The shipped sandbox-ingress NetworkPolicy keeps its security shape.

Live enforcement is proven on k3s (a co-tenant Pod is refused on every sandbox
port while the lifecycle server still drives a box); that needs a
NetworkPolicy-enforcing CNI and lives with the K8s testbed. What is pinned here
is what a reviewer cannot see at a glance and what a careless edit would break:
the policy restricts ingress (not egress), it selects sandbox Pods by the exact
label the platform stamps on them, and it never opens a hole to the whole
cluster. A weakening — dropping the Ingress restriction, selecting nothing, or
adding an allow-all peer — must fail this test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

from astrabox.seams.sandbox import (  # noqa: E402
    SANDBOX_MANAGED_BY_METADATA_KEY,
    SANDBOX_MANAGED_BY_METADATA_VALUE,
)

_MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "containers/kubernetes/sandbox-ingress-networkpolicy.yaml"
)


@pytest.fixture(scope="module")
def policy() -> dict:
    return yaml.safe_load(_MANIFEST.read_text(encoding="utf-8"))


def test_it_is_a_networkpolicy_in_the_sandbox_namespace(policy: dict) -> None:
    assert policy["apiVersion"] == "networking.k8s.io/v1"
    assert policy["kind"] == "NetworkPolicy"
    assert policy["metadata"]["namespace"] == "opensandbox"


def test_it_restricts_ingress_only(policy: dict) -> None:
    # Egress belongs to the sandbox's own sidecar; this policy must not touch
    # it, or a sandbox could lose its model/callback path.
    assert policy["spec"]["policyTypes"] == ["Ingress"]
    assert "egress" not in policy["spec"]


def test_it_selects_sandbox_pods_by_the_platform_label(policy: dict) -> None:
    # The selector must track the label the platform actually stamps, so it
    # keeps matching sandbox Pods (and keeps NOT matching the model-gateway and
    # other fixtures, which do not carry it). An empty selector would match
    # every Pod in the namespace, including those fixtures.
    selector = policy["spec"]["podSelector"]
    assert selector.get("matchLabels") == {
        SANDBOX_MANAGED_BY_METADATA_KEY: SANDBOX_MANAGED_BY_METADATA_VALUE
    }


def test_the_default_shipped_policy_allows_only_the_gateway_namespace(policy: dict) -> None:
    # As shipped it admits only the OpenSandbox ingress-gateway namespace. A
    # direct-routing deployment adds its own platform-source ipBlock at apply
    # time; the manifest itself must not ship one, and must never ship an
    # allow-all.
    peers = [peer for rule in policy["spec"]["ingress"] for peer in rule["from"]]
    assert peers, "an ingress rule with no peers admits everything"
    assert peers == [
        {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "opensandbox-system"}}}
    ]


def test_no_ingress_rule_opens_the_whole_cluster(policy: dict) -> None:
    for rule in policy["spec"]["ingress"]:
        # A rule with no `from` (or an empty one) admits every source.
        assert rule.get("from"), "an ingress rule without peers admits everything"
        for peer in rule["from"]:
            cidr = peer.get("ipBlock", {}).get("cidr")
            assert cidr not in ("0.0.0.0/0", "::/0"), "policy must not admit the whole internet"
            # A pod/namespace selector that is an empty dict selects everything.
            for key in ("podSelector", "namespaceSelector"):
                if key in peer:
                    assert peer[key] != {}, f"empty {key} admits everything"
