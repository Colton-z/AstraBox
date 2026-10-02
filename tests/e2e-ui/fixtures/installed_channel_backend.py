#!/usr/bin/env python3
"""Temporarily serve the candidate with a separately installed channel wheel.

The original container stays intact and stopped while the extended image uses
its address and database. Only one server may claim the plugin's channel work.
The receipt retains identities, never the copied private environment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from urllib.parse import urlparse

from add_server_replica import _clone_arguments, _docker, _inspect

LABEL = "astrabox.probe.installed-channel"


def _save(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")
    path.chmod(0o600)


def restore(receipt_path: Path) -> dict:
    receipt = json.loads(receipt_path.read_text())
    source = _inspect(receipt["source"])
    if source["Id"] != receipt["source_id"] or source["Image"] != receipt["base_image_id"]:
        raise RuntimeError("the original server identity changed during the plugin test")
    matches = _docker("ps", "-aq", "--filter", f"name=^/{receipt['name']}$").split()
    try:
        if matches:
            clone = _inspect(receipt["name"])
            if (
                clone["Image"] != receipt["installed_image_id"]
                or (clone["Config"].get("Labels") or {}).get(LABEL) != source["Id"]
            ):
                raise RuntimeError("refusing to remove a container outside this plugin test")
            _docker("rm", "-f", clone["Id"])
    finally:
        _docker("start", receipt["source"])
    receipt["state"] = "RESTORED"
    _save(receipt_path, receipt)
    return receipt


def start(source_name: str, name: str, origin: str, receipt_path: Path) -> dict:
    if receipt_path.exists():
        raise RuntimeError("refusing to overwrite an existing plugin receipt")
    if _docker("ps", "-aq", "--filter", f"name=^/{name}$").strip():
        raise RuntimeError("refusing to replace an existing plugin container")
    source = _inspect(source_name)
    if not source["State"]["Running"]:
        raise RuntimeError("the candidate must be running before installing the test plugin")
    base = json.loads(_docker("image", "inspect", source["Image"]))[0]
    base_ref = str(source["Config"]["Image"])
    if json.loads(_docker("image", "inspect", base_ref))[0]["Id"] != source["Image"]:
        raise RuntimeError("the candidate image reference no longer names its running image")
    package = Path(__file__).with_name("installed-channel")
    digest = hashlib.sha256(source["Image"].encode())
    for path in sorted(package.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(package)).encode())
            digest.update(path.read_bytes())
    image = "astrabox-e2e-installed-channel:" + digest.hexdigest()[:20]
    build = subprocess.run(
        ["docker", "build", "--pull=false", "--tag", image,
         "--build-arg", "BASE_IMAGE=" + base_ref,
         "--build-arg", "RUNTIME_USER=" + (base["Config"].get("User") or "0"), str(package)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if build.returncode:
        raise RuntimeError("plugin image build failed: " + (build.stdout + build.stderr)[-5000:])
    installed = json.loads(_docker("image", "inspect", image))[0]
    layers = base["RootFS"]["Layers"]
    if installed["RootFS"]["Layers"][:len(layers)] != layers:
        raise RuntimeError("the plugin image does not extend the exact candidate image")
    metadata = json.loads(_docker(
        "run", "--rm", "--entrypoint", "python", image, "-c",
        "import json; from importlib.metadata import distribution; "
        "d=distribution('astrabox-test-channel'); "
        "print(json.dumps({'version':d.version,'entry_points':"
        "[{'group':e.group,'name':e.name,'value':e.value} for e in d.entry_points]}))",
    ))
    address = urlparse(origin)
    mappings = [
        (port, bindings) for port, bindings in source["NetworkSettings"]["Ports"].items()
        if bindings and any(
            binding["HostIp"] == address.hostname and int(binding["HostPort"]) == address.port
            for binding in bindings
        )
    ]
    if len(mappings) != 1 or address.scheme != "http":
        raise RuntimeError("the test origin must name the candidate's private HTTP listener")
    container_port, bindings = mappings[0]
    clone_source = {**source, "Config": {**source["Config"], "Image": image}}
    run_args, networks = _clone_arguments(
        clone_source, name=name, host_port=int(address.port),
        container_port=int(container_port.split("/")[0]), bind_ip=str(address.hostname),
    )
    extra = ["--label", f"{LABEL}={source['Id']}"]
    for binding in bindings:
        if binding["HostIp"] != address.hostname:
            extra += ["--publish", f"{binding['HostIp']}:{binding['HostPort']}:{container_port}"]
    run_args[1:1] = extra
    receipt = {
        "state": "PREPARED", "source": source_name, "source_id": source["Id"],
        "base_image_id": source["Image"], "base_image_ref": base_ref,
        "name": name, "installed_image_id": installed["Id"],
        "installed_image": image, "package_sha256": digest.hexdigest(),
        "metadata": metadata, "origin": origin,
    }
    _save(receipt_path, receipt)
    try:
        _docker("stop", "--time", "10", source_name)
        receipt["container_id"] = _docker(*run_args).strip()
        for network in networks:
            _docker("network", "connect", network, name)
        if _inspect(source_name)["State"]["Running"]:
            raise RuntimeError("the original server is still claiming plugin work")
        receipt["state"] = "STARTED"
        _save(receipt_path, receipt)
        return receipt
    except BaseException:
        restore(receipt_path)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "restore"))
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--source")
    parser.add_argument("--name")
    parser.add_argument("--origin")
    args = parser.parse_args()
    if args.action == "start":
        if not all((args.source, args.name, args.origin)):
            parser.error("start requires --source, --name and --origin")
        receipt = start(args.source, args.name, args.origin, args.receipt)
    else:
        receipt = restore(args.receipt)
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
