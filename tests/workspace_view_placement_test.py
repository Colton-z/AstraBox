"""A workspace view PV admits only the node whose helper serves the view.

The helper Pod's FUSE mount exists on one node's filesystem. The sandbox that
mounts the view finds it only if kube-scheduler places the sandbox on that
node, and the scheduler reads that constraint from the view PV's node
affinity. These tests evaluate the affinity the way kube-scheduler's
VolumeBinding plugin evaluates a bound PV: against node labels only.
"""

from __future__ import annotations

import json
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from astrabox.core.service.orchestrator.runtime.storage._mergerfs_kubernetes import (
    KubernetesMounts,
)
from astrabox.core.service.orchestrator.runtime.storage.mergerfs import MountAssignment

WORKER = "ip-10-0-0-2.us-west-2.compute.internal"
CONTROL = "ip-10-0-0-1.us-west-2.compute.internal"


@pytest.fixture(autouse=True)
def _client_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Supply the one Kubernetes client name the driver imports inside calls.

    Unit environments install without the sandbox-server extra that carries the
    Kubernetes client. The fakes below never raise, so the type only has to exist.
    """
    exceptions = ModuleType("kubernetes.client.exceptions")
    exceptions.ApiException = type("ApiException", (Exception,), {"status": None})  # type: ignore[attr-defined]
    client = ModuleType("kubernetes.client")
    client.exceptions = exceptions  # type: ignore[attr-defined]
    package = ModuleType("kubernetes")
    package.client = client  # type: ignore[attr-defined]
    for module in (package, client, exceptions):
        monkeypatch.setitem(sys.modules, module.__name__, module)


def _labels(hostname: str) -> dict[str, str]:
    return {"kubernetes.io/os": "linux", "kubernetes.io/hostname": hostname}


def _scheduler_admits(affinity: dict[str, Any], node_labels: dict[str, str]) -> bool:
    """kube-scheduler v1.36.3 ``CheckNodeAffinity`` for a bound PV.

    It builds the node from labels alone, so a field term sees no node fields
    and is not evaluated (``nodeSelectorTerm.match`` in
    ``k8s.io/component-helpers/scheduling/corev1/nodeaffinity``). Terms are
    ORed; a term with no requirements selects nothing.
    """

    def matches(expression: dict[str, Any]) -> bool:
        key, operator = expression["key"], expression["operator"]
        if operator == "In":
            return node_labels.get(key) in expression["values"]
        if operator == "NotIn":
            return node_labels.get(key) not in expression["values"]
        if operator == "Exists":
            return key in node_labels
        if operator == "DoesNotExist":
            return key not in node_labels
        raise AssertionError(f"operator {operator!r} is outside this model")

    for term in affinity["required"]["nodeSelectorTerms"]:
        expressions = term.get("matchExpressions") or []
        if not expressions and not term.get("matchFields"):
            continue
        if all(matches(expression) for expression in expressions):
            return True
    return False


class _Cluster:
    """The CoreV1 calls ``provision`` makes, with a helper already scheduled."""

    def __init__(self, nodes: dict[str, dict[str, str]], helper_node: str) -> None:
        self.nodes = nodes
        self.helper_node = helper_node
        self.pod: Any = None
        self.pv: Any = None
        self.pvc: Any = None
        self.pv_body: dict[str, Any] | None = None

    @staticmethod
    def _metadata(body: dict[str, Any]) -> SimpleNamespace:
        return SimpleNamespace(labels=body["metadata"]["labels"], deletion_timestamp=None)

    def list_node(self, _request_timeout: int) -> SimpleNamespace:
        return SimpleNamespace(
            items=[
                SimpleNamespace(metadata=SimpleNamespace(name=name, labels=labels))
                for name, labels in self.nodes.items()
            ]
        )

    def read_namespaced_pod(self, _name: str, _namespace: str, _request_timeout: int) -> Any:
        return self.pod

    def create_namespaced_pod(
        self, _namespace: str, body: dict[str, Any], _request_timeout: int
    ) -> Any:
        self.pod = SimpleNamespace(
            metadata=self._metadata(body),
            spec=SimpleNamespace(node_name=self.helper_node),
            status=SimpleNamespace(
                phase="Running",
                container_statuses=[SimpleNamespace(ready=True, restart_count=0)],
            ),
        )
        return self.pod

    def read_persistent_volume(self, _name: str, _request_timeout: int) -> Any:
        return self.pv

    def create_persistent_volume(self, body: dict[str, Any], _request_timeout: int) -> Any:
        self.pv_body = body
        self.pv = SimpleNamespace(
            metadata=self._metadata(body),
            spec=SimpleNamespace(
                host_path=SimpleNamespace(path=body["spec"]["hostPath"]["path"]),
                node_affinity=body["spec"]["nodeAffinity"],
            ),
        )
        return self.pv

    def read_namespaced_persistent_volume_claim(
        self, _name: str, _namespace: str, _request_timeout: int
    ) -> Any:
        return self.pvc

    def create_namespaced_persistent_volume_claim(
        self, _namespace: str, body: dict[str, Any], _request_timeout: int
    ) -> Any:
        self.pvc = SimpleNamespace(
            metadata=self._metadata(body),
            spec=SimpleNamespace(volume_name=body["spec"]["volumeName"]),
            status=SimpleNamespace(phase="Bound"),
        )
        return self.pvc


def _assignment() -> MountAssignment:
    name = "astrabox-view-" + "0" * 32
    return MountAssignment(
        assignment_id="storage-assignment",
        name=name,
        host_path="/var/lib/astrabox/workspace-mounts/" + name,
        backing_volume="workspaces",
        image="workspace-mounter:test",
        shared=True,
        mounts=(("/workspace", "agents/a/workspace"),),
    )


def _driver(cluster: _Cluster, monkeypatch: pytest.MonkeyPatch) -> KubernetesMounts:
    ready = json.dumps(
        {
            "state": "READY",
            "mounts": [
                {
                    "box_path": "/workspace",
                    "storage_subpath": "agents/a/workspace",
                    "consumer_subpath": "0/workspace",
                    "passthrough_io": "rw",
                    "cache_files": "auto-full",
                }
            ],
        }
    )
    monkeypatch.setattr(KubernetesMounts, "_exec", lambda _self, _assignment, _command: ready)
    driver = object.__new__(KubernetesMounts)
    driver.api = cluster
    driver.client = SimpleNamespace(sanitize_for_serialization=lambda value: value)
    driver.namespace = "opensandbox"
    driver.timeout = 5
    return driver


def test_the_view_volume_admits_only_the_helper_node(monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = _Cluster(
        {WORKER: _labels("ip-10-0-0-2"), CONTROL: _labels("ip-10-0-0-1")},
        helper_node=WORKER,
    )

    _driver(cluster, monkeypatch).provision(_assignment())

    assert cluster.pv_body is not None and cluster.pvc is not None
    affinity = cluster.pv_body["spec"]["nodeAffinity"]
    assert _scheduler_admits(affinity, cluster.nodes[WORKER])
    assert not _scheduler_admits(affinity, cluster.nodes[CONTROL])


def test_a_hostname_shared_by_two_nodes_cannot_pin_a_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster = _Cluster(
        {WORKER: _labels("ip-10-0-0-2"), CONTROL: _labels("ip-10-0-0-2")},
        helper_node=WORKER,
    )

    with pytest.raises(RuntimeError, match="no unique kubernetes.io/hostname label"):
        _driver(cluster, monkeypatch).provision(_assignment())
    assert cluster.pv_body is None
