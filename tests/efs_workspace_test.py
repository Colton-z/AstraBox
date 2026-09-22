"""scripts/efs-workspace.py binds a sandbox namespace to an existing EFS file system.

scripts/k8s-testbed.sh calls it when the workspace storage provider is aws_efs;
it creates only a retained, TLS-mounted CSI PersistentVolume and its claim.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


REPO = Path(__file__).resolve().parents[1]


def load(path: Path) -> ModuleType:
    name = "efs_workspace_" + path.stem.replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_efs_reconcile_requires_driver_before_any_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load(REPO / "scripts/efs-workspace.py")
    calls: list[tuple[str, ...]] = []

    def absent_driver(*args: str, **_kwargs: Any) -> str:
        calls.append(args)
        assert args[0] == "get"
        return ""

    monkeypatch.setattr(module, "kubectl", absent_driver)
    with pytest.raises(RuntimeError, match="install the official efs.csi.aws.com"):
        module.reconcile("candidate-workspaces", "opensandbox", "fs-0123456789abcdef0", "20Gi")
    assert len(calls) == 1


def test_efs_reconcile_creates_only_retained_encrypted_csi_objects_and_reuses_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load(REPO / "scripts/efs-workspace.py")
    live: dict[str, dict[str, Any]] = {"csidriver/efs.csi.aws.com": {"metadata": {"name": "efs.csi.aws.com"}}}
    created: list[dict[str, Any]] = []

    def kube(*args: str, body: dict[str, Any] | None = None) -> str:
        if "get" in args:
            value = live.get(args[args.index("get") + 1])
            return json.dumps(value) if value is not None else ""
        if args[0] == "create":
            assert body is not None
            item = copy.deepcopy(body)
            created.append(item)
            kind = "pv" if item["kind"] == "PersistentVolume" else "pvc"
            item["metadata"]["uid"] = kind + "-uid"
            live[kind + "/candidate-workspaces"] = item
            return ""
        assert "wait" in args, args
        live["pv/candidate-workspaces"]["status"] = {"phase": "Bound"}
        live["pvc/candidate-workspaces"]["status"] = {"phase": "Bound"}
        live["pv/candidate-workspaces"]["spec"]["claimRef"]["uid"] = "pvc-uid"
        return ""

    monkeypatch.setattr(module, "kubectl", kube)
    first = module.reconcile("candidate-workspaces", "opensandbox", "fs-0123456789abcdef0", "20Gi")
    assert first == {"provider": "aws_efs", "file_system_id": "fs-0123456789abcdef0", "volume": "candidate-workspaces", "pv_uid": "pv-uid", "pvc_uid": "pvc-uid"}
    assert [item["kind"] for item in created] == ["PersistentVolume", "PersistentVolumeClaim"]
    pv = created[0]["spec"]
    assert pv["persistentVolumeReclaimPolicy"] == "Retain"
    assert pv["csi"] == {"driver": "efs.csi.aws.com", "volumeHandle": "fs-0123456789abcdef0", "volumeAttributes": {"encryptInTransit": "true"}}
    assert pv["mountOptions"] == ["tls"]
    assert not {"hostPath", "nfs"}.intersection(pv)
    assert module.reconcile("candidate-workspaces", "opensandbox", "fs-0123456789abcdef0", "20Gi") == first
    assert len(created) == 2
    with pytest.raises(RuntimeError, match="configured encrypted EFS source"):
        module.reconcile("candidate-workspaces", "opensandbox", "fs-11111111111111111", "20Gi")
    assert len(created) == 2
