#!/usr/bin/env python3
"""Bind a testbed claim to an operator-owned EFS filesystem through its CSI driver."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from typing import Any


DRIVER = "efs.csi.aws.com"


def require(condition: object, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def kubectl(*arguments: str, body: dict[str, Any] | None = None) -> str:
    completed = subprocess.run(
        ["kubectl", *arguments], input=json.dumps(body) if body is not None else None,
        text=True, capture_output=True, check=False, timeout=75,
    )
    require(completed.returncode == 0, f"kubectl {' '.join(arguments)}: {completed.stderr.strip()}")
    return completed.stdout


def read(kind: str, name: str, namespace: str) -> dict[str, Any] | None:
    output = kubectl(*(["--namespace", namespace] if kind == "pvc" else []),
                     "get", f"{kind}/{name}", "--ignore-not-found", "-o", "json")
    if not output.strip():
        return None
    document = json.loads(output)
    require(isinstance(document, dict), f"{kind}/{name} is not an object")
    return document


def manifests(volume: str, namespace: str, filesystem: str, size: str) -> tuple[dict[str, Any], dict[str, Any]]:
    require(re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", volume) and len(volume) <= 63, "invalid workspace volume")
    require(re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", namespace) and len(namespace) <= 63, "invalid namespace")
    require(re.fullmatch(r"fs-[0-9a-f]{8,40}", filesystem), "invalid AWS EFS filesystem ID")
    labels = {"app.kubernetes.io/part-of": "astrabox", "astrabox.workspace-volume": volume}
    pv = {
        "apiVersion": "v1", "kind": "PersistentVolume",
        "metadata": {"name": volume, "labels": labels},
        "spec": {
            "accessModes": ["ReadWriteMany"], "capacity": {"storage": size},
            "persistentVolumeReclaimPolicy": "Retain", "storageClassName": volume,
            "volumeMode": "Filesystem", "mountOptions": ["tls"],
            "claimRef": {"name": volume, "namespace": namespace},
            "csi": {"driver": DRIVER, "volumeHandle": filesystem,
                    "volumeAttributes": {"encryptInTransit": "true"}},
        },
    }
    pvc = {
        "apiVersion": "v1", "kind": "PersistentVolumeClaim",
        "metadata": {"name": volume, "namespace": namespace, "labels": labels},
        "spec": {"accessModes": ["ReadWriteMany"], "storageClassName": volume,
                 "volumeName": volume, "volumeMode": "Filesystem",
                 "resources": {"requests": {"storage": size}}},
    }
    return pv, pvc


def verify(document: dict[str, Any], expected: dict[str, Any]) -> None:
    name = expected["metadata"]["name"]
    kind = expected["kind"]
    metadata, spec = document.get("metadata") or {}, document.get("spec") or {}
    require(metadata.get("name") == name and metadata.get("uid") and not metadata.get("deletionTimestamp"),
            f"{kind}/{name} has no stable live identity")
    require(all((metadata.get("labels") or {}).get(key) == value for key, value in expected["metadata"]["labels"].items()),
            f"{kind}/{name} is not owned by this testbed workspace")
    common = ("accessModes", "storageClassName", "volumeMode")
    require(all(spec.get(key) == expected["spec"][key] for key in common), f"{kind}/{name} storage mode differs")
    if kind == "PersistentVolume":
        csi = spec.get("csi") or {}
        require({key: value for key, value in csi.items() if key != "readOnly"} == expected["spec"]["csi"]
                and not csi.get("readOnly", False) and spec.get("mountOptions") == ["tls"]
                and spec.get("persistentVolumeReclaimPolicy") == "Retain"
                and not spec.get("hostPath") and not spec.get("nfs"),
                f"PV/{name} does not match the configured encrypted EFS source")
        claim = spec.get("claimRef") or {}
        require(not claim or all(claim.get(key) == value for key, value in expected["spec"]["claimRef"].items()),
                f"PV/{name} belongs to another claim")
    else:
        require(metadata.get("namespace") == expected["metadata"]["namespace"]
                and spec.get("volumeName") == name, f"PVC/{name} escaped the configured claim")


def reconcile(volume: str, namespace: str, filesystem: str, size: str) -> dict[str, str]:
    pv_expected, pvc_expected = manifests(volume, namespace, filesystem, size)
    driver = read("csidriver", DRIVER, namespace)
    require(driver is not None, f"install the official {DRIVER} CSI driver before preparing EFS workspaces")
    pv, pvc = read("pv", volume, namespace), read("pvc", volume, namespace)
    # Inspect both resources before the first mutation; an occupied name is never adopted.
    for document, expected in ((pv, pv_expected), (pvc, pvc_expected)):
        if document is not None:
            verify(document, expected)
    if pv is not None and pvc is None:
        phase = (pv.get("status") or {}).get("phase")
        require(phase in {"Available", "Released"}, f"PV/{volume} is {phase}, but its claim is absent")
        if phase == "Released":
            kubectl("patch", "pv", volume, "--type=json", "-p", json.dumps([
                {"op": "test", "path": "/metadata/uid", "value": pv["metadata"]["uid"]},
                {"op": "test", "path": "/metadata/resourceVersion", "value": pv["metadata"]["resourceVersion"]},
                {"op": "remove", "path": "/spec/claimRef"},
            ]))
    if pv is None:
        kubectl("create", "-f", "-", body=pv_expected)
    if pvc is None:
        kubectl("create", "-f", "-", body=pvc_expected)
    kubectl("--namespace", namespace, "wait", "--for=jsonpath={.status.phase}=Bound", f"pvc/{volume}", "--timeout=60s")
    pv, pvc = read("pv", volume, namespace), read("pvc", volume, namespace)
    require(pv is not None and pvc is not None, "EFS claim disappeared during preparation")
    assert pv is not None and pvc is not None
    verify(pv, pv_expected)
    verify(pvc, pvc_expected)
    require((pvc.get("status") or {}).get("phase") == "Bound" and (pv.get("status") or {}).get("phase") == "Bound",
            "EFS workspace PV/PVC did not become Bound")
    require((pv.get("spec", {}).get("claimRef") or {}).get("uid") == pvc["metadata"]["uid"],
            "EFS PV bound a different PVC identity")
    return {"provider": "aws_efs", "file_system_id": filesystem, "volume": volume,
            "pv_uid": pv["metadata"]["uid"], "pvc_uid": pvc["metadata"]["uid"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--volume", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--file-system-id", required=True)
    parser.add_argument("--size", default="20Gi")
    args = parser.parse_args()
    print(json.dumps(reconcile(args.volume, args.namespace, args.file_system_id, args.size), sort_keys=True))


if __name__ == "__main__":
    main()
