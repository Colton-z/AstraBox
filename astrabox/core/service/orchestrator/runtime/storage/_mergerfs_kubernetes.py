"""Kubernetes PVC transport for node-local mergerfs workspace views."""

from __future__ import annotations

import hashlib
import time
from typing import Any

from astrabox.core.service.orchestrator.runtime.storage.mergerfs import (
    CLI,
    MANAGED,
    OWNER,
    MountAssignment,
    verify_labels,
    verify_status,
)

HOSTNAME = "kubernetes.io/hostname"


class KubernetesMounts:
    """Use Kubernetes scheduling and exec; no independent filesystem RPC server."""

    def __init__(self) -> None:
        from kubernetes import client, config

        from astrabox.deploy.sandbox_server import (
            kube_create_timeout_seconds,
            kube_namespace,
            kubeconfig_for_server,
        )

        path = kubeconfig_for_server()
        if path is None:
            config.load_incluster_config()
            self.client = client.ApiClient()
        else:
            self.client = config.new_client_from_config(config_file=str(path))
        self.api = client.CoreV1Api(self.client)
        self.namespace = kube_namespace()
        self.timeout = kube_create_timeout_seconds()

    def __enter__(self) -> KubernetesMounts:
        return self

    def __exit__(self, *_: Any) -> None:
        self.client.close()

    def _read(self, method: Any, name: str, *, namespaced: bool = True) -> Any:
        from kubernetes.client.exceptions import ApiException

        args = [name, self.namespace] if namespaced else [name]
        try:
            return method(*args, _request_timeout=30)
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise

    def _ensure(
        self, read: Any, create: Any, body: dict[str, Any], *, namespaced: bool = True
    ) -> Any:
        from kubernetes.client.exceptions import ApiException

        name = body["metadata"]["name"]
        resource = self._read(read, name, namespaced=namespaced)
        if resource is None:
            args = [self.namespace, body] if namespaced else [body]
            try:
                resource = create(*args, _request_timeout=30)
            except ApiException as exc:
                if exc.status != 409:
                    raise
                resource = self._read(read, name, namespaced=namespaced)
        if resource is None:
            raise RuntimeError(
                f"workspace resource create uncertain: {name}; retained for inspection"
            )
        verify_labels(resource.metadata.labels, body["metadata"]["labels"], name)
        if resource.metadata.deletion_timestamp is not None:
            raise RuntimeError(f"workspace resource {name} is terminating; cannot reuse it")
        return resource

    def _check_topology(self, assignment: MountAssignment) -> None:
        if assignment.shared:
            return
        nodes = self.api.list_node(_request_timeout=30).items
        eligible = [
            node.metadata.name
            for node in nodes
            if not node.spec.unschedulable
            and (node.metadata.labels or {}).get("kubernetes.io/os") == "linux"
            and not any(
                taint.effect in {"NoSchedule", "NoExecute"} for taint in (node.spec.taints or [])
            )
        ]
        if len(eligible) != 1:
            raise RuntimeError(
                "local workspace storage requires exactly one eligible Linux worker; "
                f"found {eligible!r}. Multi-worker deployments require shared storage."
            )

    def _hostname(self, node: str) -> str:
        """Return the helper node's unique ``kubernetes.io/hostname`` label value.

        kube-scheduler matches a PersistentVolume's node affinity against node
        labels only: ``CheckNodeAffinity`` in
        https://github.com/kubernetes/kubernetes/blob/v1.36.3/staging/src/k8s.io/component-helpers/storage/volume/helpers.go
        passes no node name, and a ``metadata.name`` field term then matches
        every node. The label must name exactly one node for the view PV to
        admit only the helper's node.
        """
        labels = {
            item.metadata.name: (item.metadata.labels or {}).get(HOSTNAME)
            for item in self.api.list_node(_request_timeout=30).items
        }
        value = labels.get(node)
        if not value or list(labels.values()).count(value) != 1:
            raise RuntimeError(
                f"helper node {node!r} has no unique {HOSTNAME} label ({value!r}); "
                "cannot pin its workspace view"
            )
        return value

    def _pod(self, assignment: MountAssignment) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": assignment.name, "labels": assignment.labels()},
            "spec": {
                "restartPolicy": "Never",
                "automountServiceAccountToken": False,
                "nodeSelector": {"kubernetes.io/os": "linux"},
                "terminationGracePeriodSeconds": 30,
                "containers": [
                    {
                        "name": "workspace-mounter",
                        "image": assignment.image,
                        "imagePullPolicy": "IfNotPresent",
                        "command": assignment.command("serve"),
                        "securityContext": {"privileged": True, "runAsUser": 0, "runAsGroup": 0},
                        "volumeMounts": [
                            {"name": "data", "mountPath": "/data"},
                            {
                                "name": "views",
                                "mountPath": "/views",
                                "mountPropagation": "Bidirectional",
                            },
                        ],
                        "readinessProbe": {
                            "exec": {"command": [*CLI, "status", "--root", "/views"]},
                            "periodSeconds": 2,
                            "timeoutSeconds": 5,
                            "failureThreshold": 1,
                        },
                        "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}},
                    }
                ],
                "volumes": [
                    {
                        "name": "data",
                        "persistentVolumeClaim": {"claimName": assignment.backing_volume},
                    },
                    {
                        "name": "views",
                        "hostPath": {"path": assignment.host_path, "type": "DirectoryOrCreate"},
                    },
                ],
            },
        }

    def _ready(self, assignment: MountAssignment) -> Any:
        deadline = time.monotonic() + self.timeout
        while True:
            pod = self._read(self.api.read_namespaced_pod, assignment.name)
            if pod is None:
                raise RuntimeError(f"workspace helper Pod/{assignment.name} disappeared")
            verify_labels(pod.metadata.labels, assignment.identity(), assignment.name)
            states = pod.status.container_statuses or []
            if pod.status.phase in {"Failed", "Succeeded"} or pod.metadata.deletion_timestamp:
                raise RuntimeError(
                    f"workspace helper Pod/{assignment.name} stopped; phase={pod.status.phase}; retained"
                )
            if any(state.restart_count for state in states):
                raise RuntimeError(
                    f"workspace helper Pod/{assignment.name} restarted; existing sandbox mounts are invalid"
                )
            if states and all(state.ready for state in states):
                return pod
            if time.monotonic() >= deadline:
                reasons = [str(state.state) for state in states]
                raise TimeoutError(
                    f"workspace helper Pod/{self.namespace}/{assignment.name} not ready; "
                    f"phase={pod.status.phase}, states={reasons}; resources retained"
                )
            time.sleep(0.5)

    def _exec(self, assignment: MountAssignment, command: list[str]) -> str:
        from kubernetes.stream import stream

        connection = stream(
            self.api.connect_get_namespaced_pod_exec,
            assignment.name,
            self.namespace,
            container="workspace-mounter",
            command=command,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
            _preload_content=False,
            _request_timeout=30,
        )
        try:
            connection.run_forever(timeout=self.timeout)
            output = connection.read_stdout()
            error = connection.read_stderr()
            if connection.is_open():
                raise TimeoutError(
                    f"workspace helper exec timed out: Pod/{assignment.name}; binding outcome unknown"
                )
            if connection.returncode != 0:
                raise RuntimeError(
                    f"workspace helper Pod/{assignment.name} command failed "
                    f"({connection.returncode}): {error[-2000:]} {output[-1000:]}"
                )
            return str(output)
        finally:
            connection.close()

    def provision(self, assignment: MountAssignment) -> None:
        """A ready helper fixes the view PV's node affinity before sandbox creation."""
        self._check_topology(assignment)
        existing_pod = self._read(self.api.read_namespaced_pod, assignment.name)
        if existing_pod is None and self._read(
            self.api.read_persistent_volume, assignment.name, namespaced=False
        ):
            raise RuntimeError(
                f"workspace view PV/{assignment.name} lost its helper; do not recreate live FUSE mounts"
            )
        self._ensure(
            self.api.read_namespaced_pod, self.api.create_namespaced_pod, self._pod(assignment)
        )
        pod = self._ready(assignment)
        verify_status(self._exec(assignment, assignment.command("status")), assignment)
        node = pod.spec.node_name
        if not node:
            raise RuntimeError(f"ready helper Pod/{assignment.name} has no node assignment")
        hostname = self._hostname(node)
        metadata = {"name": assignment.name, "labels": assignment.labels()}
        pv: dict[str, Any] = {
            "apiVersion": "v1",
            "kind": "PersistentVolume",
            "metadata": metadata,
            "spec": {
                "capacity": {"storage": "1Gi"},
                "accessModes": ["ReadWriteMany"],
                "persistentVolumeReclaimPolicy": "Retain",
                "storageClassName": "",
                "hostPath": {"path": assignment.host_path, "type": "Directory"},
                "claimRef": {"name": assignment.name, "namespace": self.namespace},
                "nodeAffinity": {
                    "required": {
                        "nodeSelectorTerms": [
                            {
                                "matchExpressions": [
                                    {"key": HOSTNAME, "operator": "In", "values": [hostname]}
                                ],
                            }
                        ]
                    }
                },
            },
        }
        volume = self._ensure(
            self.api.read_persistent_volume, self.api.create_persistent_volume, pv, namespaced=False
        )
        if (
            volume.spec.host_path is None
            or volume.spec.host_path.path != assignment.host_path
            or self.client.sanitize_for_serialization(volume.spec.node_affinity)
            != pv["spec"]["nodeAffinity"]
        ):
            raise RuntimeError(
                f"workspace PV/{assignment.name} has unexpected host path or node affinity"
            )
        pvc = {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": metadata,
            "spec": {
                "accessModes": ["ReadWriteMany"],
                "storageClassName": "",
                "volumeName": assignment.name,
                "resources": {"requests": {"storage": "1Gi"}},
            },
        }
        self._ensure(
            self.api.read_namespaced_persistent_volume_claim,
            self.api.create_namespaced_persistent_volume_claim,
            pvc,
        )
        deadline = time.monotonic() + self.timeout
        while True:
            claim = self._read(self.api.read_namespaced_persistent_volume_claim, assignment.name)
            if claim is None:
                raise RuntimeError(f"workspace PVC/{assignment.name} disappeared")
            if claim.status.phase == "Bound":
                if claim.spec.volume_name != assignment.name:
                    raise RuntimeError(f"workspace PVC/{assignment.name} bound to an unexpected PV")
                break
            if claim.status.phase == "Lost" or time.monotonic() >= deadline:
                raise RuntimeError(
                    f"workspace PVC/{assignment.name} not Bound ({claim.status.phase}); resources retained"
                )
            time.sleep(0.5)

    def bind(self, assignment: MountAssignment, binding: str) -> str:
        """Execute against the original helper; a missing helper is not recreated."""
        self._ready(assignment)
        return self._exec(assignment, assignment.command("bind", binding))

    def release(self, assignment_id: str) -> None:
        """Delete exact owned view resources; never delete the backing PVC or files."""
        digest = hashlib.sha256(assignment_id.encode()).hexdigest()
        name = "astrabox-view-" + digest[:32]
        labels = {MANAGED: "mergerfs", OWNER: digest[:63]}
        consumers = [
            pod.metadata.name
            for pod in self.api.list_namespaced_pod(self.namespace, _request_timeout=30).items
            if any(
                volume.persistent_volume_claim is not None
                and volume.persistent_volume_claim.claim_name == name
                for volume in (pod.spec.volumes or [])
            )
        ]
        if consumers:
            raise RuntimeError(
                f"workspace PVC/{name} still has sandbox Pod consumers {consumers!r}; retained"
            )
        resources = [
            (self.api.read_namespaced_pod, self.api.delete_namespaced_pod, True),
            (self.api.read_persistent_volume, self.api.delete_persistent_volume, False),
            (
                self.api.read_namespaced_persistent_volume_claim,
                self.api.delete_namespaced_persistent_volume_claim,
                True,
            ),
        ]
        for read, delete, namespaced in resources:
            resource = self._read(read, name, namespaced=namespaced)
            if resource is None:
                continue
            verify_labels(resource.metadata.labels, labels, name)
            args = [name, self.namespace] if namespaced else [name]
            delete(
                *args, body={"preconditions": {"uid": resource.metadata.uid}}, _request_timeout=30
            )

    def attach(self, assignment_id: str, sandbox_id: str, backend: str) -> None:
        """Persist receipts on the view PVC so helper loss does not lose ownership."""
        digest = hashlib.sha256(assignment_id.encode()).hexdigest()
        name = "astrabox-view-" + digest[:32]
        claim = self._read(self.api.read_namespaced_persistent_volume_claim, name)
        if claim is None:
            raise RuntimeError(f"cannot attach sandbox to missing workspace PVC/{name}")
        verify_labels(claim.metadata.labels, {MANAGED: "mergerfs", OWNER: digest[:63]}, name)
        desired = {
            "astrabox.storage-id": assignment_id,
            "astrabox.sandbox-id": sandbox_id,
            "astrabox.sandbox-backend": backend,
        }
        annotations = claim.metadata.annotations or {}
        for key, value in desired.items():
            if key in annotations and annotations[key] != value:
                raise RuntimeError(f"workspace PVC/{name} already belongs to a different sandbox")
        self.api.patch_namespaced_persistent_volume_claim(
            name,
            self.namespace,
            {
                "metadata": {
                    "resourceVersion": claim.metadata.resource_version,
                    "annotations": desired,
                }
            },
            _request_timeout=30,
        )

    def attached(self) -> list[tuple[str, str, str]]:
        """Read durable receipts without assuming the helper is currently healthy."""
        claims = self.api.list_namespaced_persistent_volume_claim(
            self.namespace,
            label_selector=MANAGED + "=mergerfs",
            _request_timeout=30,
        ).items
        result = []
        for claim in claims:
            annotations = claim.metadata.annotations or {}
            if "astrabox.sandbox-id" not in annotations:
                continue
            result.append(
                (
                    annotations["astrabox.storage-id"],
                    annotations["astrabox.sandbox-backend"],
                    annotations["astrabox.sandbox-id"],
                )
            )
        return result
