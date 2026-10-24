"""Docker compute evidence for the same live journeys as the Kubernetes fixtures.

The selected runner declares its substrate. Names, published endpoint ports and
immutable container ids come from Docker, rather than the platform projection.
Abrupt loss follows the existing browser sandboxOps fixture: SIGKILL the owned
workload without a lifecycle API call or an orderly native-state save.
"""

from __future__ import annotations

import json
import os
import subprocess
from urllib.parse import urlparse


def selected() -> bool:
    return os.getenv("ASTRABOX_E2E_SANDBOX_RUNTIME") == "docker"


def inspect(name: str) -> dict:
    rows = json.loads(subprocess.check_output(
        ["docker", "inspect", name], text=True, timeout=20,
    ))
    assert len(rows) == 1 and rows[0]["Name"].startswith("/sandbox-"), name
    return rows[0]


def image_at(endpoint: str) -> str:
    target = urlparse(endpoint)
    ids = subprocess.check_output(
        ["docker", "ps", "--filter", "name=^sandbox-", "-q"], text=True, timeout=20,
    ).split()
    assert ids, "the selected Docker daemon has no live sandbox workload"
    rows = json.loads(subprocess.check_output(
        ["docker", "inspect", *ids], text=True, timeout=20,
    ))
    matches = [row for row in rows if any(
        str(binding["HostPort"]) == str(target.port)
        for bindings in row["NetworkSettings"]["Ports"].values()
        for binding in bindings or []
    ) or any(
        network["IPAddress"] == target.hostname
        for network in row["NetworkSettings"]["Networks"].values()
    )]
    assert len(matches) == 1, f"endpoint {endpoint} resolves to {len(matches)} Docker workloads"
    assert matches[0]["State"]["Running"], matches[0]["State"]
    return str(matches[0]["Config"]["Image"])


def durable_mounts(name: str) -> list[dict]:
    """Exclude only OpenSandbox's private, uniquely per-box control volume."""
    row = inspect(name)
    sandbox_id = str(row["Name"]).removeprefix("/sandbox-")
    return [mount for mount in row["Mounts"] if not (
        mount["Type"] == "volume"
        and mount["Destination"] == "/opt/opensandbox"
        and mount["Name"] == "opensandbox-runtime-" + sandbox_id
    )]


def kill(sandbox_id: str, *, workload: str | None = None) -> str:
    row = inspect("sandbox-" + sandbox_id)
    if workload is not None:
        assert inspect(workload)["Id"] == row["Id"], "terminal and Session name different compute"
    assert row["State"]["Running"], row["State"]
    subprocess.run(["docker", "kill", "--signal", "KILL", row["Id"]],
                   capture_output=True, text=True, check=True, timeout=30)
    assert not inspect(row["Id"])["State"]["Running"], "killed compute still runs"
    return str(row["Id"])
