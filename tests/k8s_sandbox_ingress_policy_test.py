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


# k8s-testbed.sh applies the shipped policy plus its own platform sources. Its
# lifecycle server runs on one node and reaches sandbox Pod IPs directly; a
# sandbox on another node sees that traffic arrive over the flannel overlay
# from the platform node's flannel.1 address, which must be admitted or every
# box scheduled there fails its health check.
_POLICY_HARNESS = r"""
set -uo pipefail
. "$SCRIPT"
hostname() { printf '%s\n' "$HOST_ADDRESSES"; }
ip() { [ -n "$OVERLAY" ] && printf '5: flannel.1    inet %s/32 scope global flannel.1\n' "$OVERLAY"; }
kubectl() {
  case " $* " in
    *" apply -f - "*) cat > "$APPLIED" ;;
    *" get nodes -o jsonpath="*) printf '%s\n' $NODE_ADDRESSES ;;
    *" get nodes --no-headers "*) for node in $NODE_ADDRESSES; do printf '%s Ready\n' "$node"; done ;;
  esac
}
ensure_sandbox_ingress_policy
"""

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/k8s-testbed.sh"


def _apply(tmp_path: Path, *, nodes: str, host: str, overlay: str):
    import subprocess

    applied = tmp_path / "applied.yaml"
    done = subprocess.run(
        ["bash", "-c", _POLICY_HARNESS],
        env={
            "PATH": "/usr/bin:/bin",
            "KUBECONFIG_PATH": str(tmp_path / "kubeconfig"),
            "SCRIPT": str(_SCRIPT),
            "APPLIED": str(applied),
            "NODE_ADDRESSES": nodes,
            "HOST_ADDRESSES": host,
            "OVERLAY": overlay,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    peers = None
    if applied.exists():
        document = yaml.safe_load(applied.read_text(encoding="utf-8"))
        peers = [peer for rule in document["spec"]["ingress"] for peer in rule["from"]]
    return done, peers


def _gateway_peer() -> dict:
    return {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "opensandbox-system"}}}


def test_a_second_node_admits_the_platform_nodes_overlay_address(tmp_path: Path) -> None:
    # The API lists the other node first; the policy must name this host's node.
    done, peers = _apply(
        tmp_path,
        nodes="192.0.2.11 192.0.2.10",
        host="192.0.2.10 172.17.0.1",
        overlay="10.42.0.0",
    )

    assert done.returncode == 0, done.stderr
    assert peers == [
        _gateway_peer(),
        {"ipBlock": {"cidr": "172.16.0.0/12"}},
        {"ipBlock": {"cidr": "192.0.2.10/32"}},
        {"ipBlock": {"cidr": "10.42.0.0/32"}},
    ]


def test_a_single_node_admits_no_overlay_address(tmp_path: Path) -> None:
    done, peers = _apply(tmp_path, nodes="192.0.2.10", host="192.0.2.10", overlay="10.42.0.0")

    assert done.returncode == 0, done.stderr
    assert peers == [
        _gateway_peer(),
        {"ipBlock": {"cidr": "172.16.0.0/12"}},
        {"ipBlock": {"cidr": "192.0.2.10/32"}},
    ]


def test_a_second_node_without_an_overlay_address_refuses_to_apply(tmp_path: Path) -> None:
    done, peers = _apply(
        tmp_path, nodes="192.0.2.11 192.0.2.10", host="192.0.2.10", overlay=""
    )

    assert done.returncode != 0
    assert peers is None, "a policy that would refuse the platform on the other node was applied"
    assert "flannel.1" in done.stderr


def test_a_host_that_is_not_a_node_refuses_to_apply(tmp_path: Path) -> None:
    done, peers = _apply(tmp_path, nodes="192.0.2.11", host="10.9.9.9", overlay="10.42.0.0")

    assert done.returncode != 0
    assert peers is None
