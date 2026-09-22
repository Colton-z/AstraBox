"""Docker named-volume transport for node-local mergerfs workspace views."""

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


class DockerMounts:
    """Keep the privileged filesystem helper outside the OpenSandbox container."""

    def __init__(self) -> None:
        import docker

        self.client = docker.from_env(timeout=30)

    def __enter__(self) -> DockerMounts:
        return self

    def __exit__(self, *_: Any) -> None:
        self.client.close()

    def _get(self, collection: Any, name: str) -> Any:
        from docker.errors import NotFound

        try:
            return collection.get(name)
        except NotFound:
            return None

    def _helper(self, assignment: MountAssignment) -> Any:
        container = self._get(self.client.containers, assignment.name)
        if container is None:
            raise RuntimeError(
                f"workspace helper container/{assignment.name} disappeared; mount cannot be recreated"
            )
        verify_labels(container.labels, assignment.identity(), assignment.name)
        container.reload()
        if container.status != "running" or container.attrs.get("RestartCount", 0):
            raise RuntimeError(
                f"workspace helper container/{assignment.name} stopped or restarted; resources retained"
            )
        return container

    @staticmethod
    def _exec(container: Any, command: list[str]) -> str:
        result = container.exec_run(command, demux=True)
        stdout, stderr = result.output
        output = (stdout or b"").decode("utf-8", errors="replace")
        if result.exit_code != 0:
            error = (stderr or b"").decode("utf-8", errors="replace")
            raise RuntimeError(
                f"workspace helper {container.name} command failed ({result.exit_code}): {error[-2000:]} {output[-1000:]}"
            )
        return output

    def provision(self, assignment: MountAssignment) -> None:
        """Mount views before creating the named volume handed to OpenSandbox."""
        from docker.errors import APIError

        if self._get(self.client.volumes, assignment.backing_volume) is None:
            raise RuntimeError(
                f"workspace backing Docker volume {assignment.backing_volume!r} does not exist"
            )
        container = self._get(self.client.containers, assignment.name)
        if container is None:
            if self._get(self.client.volumes, assignment.name) is not None:
                raise RuntimeError(
                    f"workspace volume/{assignment.name} lost its helper; retained for inspection"
                )
            try:
                container = self.client.containers.create(
                    image=assignment.image,
                    name=assignment.name,
                    command=assignment.command("serve"),
                    entrypoint=[],
                    labels=assignment.labels(),
                    privileged=True,
                    user="0:0",
                    network_mode="none",
                    restart_policy={"Name": "no"},
                    healthcheck={
                        "test": ["CMD", *CLI, "status", "--root", "/views"],
                        "interval": 2_000_000_000,
                        "timeout": 5_000_000_000,
                        "retries": 30,
                    },
                    volumes={
                        assignment.backing_volume: {"bind": "/data", "mode": "rw"},
                        assignment.host_path: {"bind": "/views", "mode": "rw,rshared"},
                    },
                )
            except APIError as exc:
                if exc.status_code != 409:
                    raise
                container = self._get(self.client.containers, assignment.name)
        if container is None:
            raise RuntimeError(
                f"workspace helper container/{assignment.name} creation uncertain; inspect retained resources"
            )
        verify_labels(container.labels, assignment.labels(), assignment.name)
        container.reload()
        if container.status == "created":
            container.start()
        deadline = time.monotonic() + 60
        while True:
            container = self._helper(assignment)
            health = container.attrs.get("State", {}).get("Health", {}).get("Status")
            if health == "healthy":
                verify_status(self._exec(container, assignment.command("status")), assignment)
                break
            if health == "unhealthy" or time.monotonic() >= deadline:
                raise RuntimeError(
                    f"workspace helper container/{assignment.name} not ready; resources retained"
                )
            time.sleep(0.5)
        volume = self._get(self.client.volumes, assignment.name)
        # The view root contains FUSE submounts. A nonrecursive bind exposes
        # their empty host directories instead of the mounted filesystems.
        options = {"type": "none", "o": "rbind", "device": assignment.host_path}
        if volume is None:
            volume = self.client.volumes.create(
                name=assignment.name,
                driver="local",
                driver_opts=options,
                labels=assignment.labels(),
            )
        verify_labels(volume.attrs.get("Labels"), assignment.labels(), assignment.name)
        if volume.attrs.get("Driver") != "local" or volume.attrs.get("Options") != options:
            raise RuntimeError(
                f"workspace Docker volume/{assignment.name} points at unexpected storage"
            )

    def bind(self, assignment: MountAssignment, binding: str) -> str:
        """Change the supplier's directory mapping while retaining the same mount."""
        return self._exec(self._helper(assignment), assignment.command("bind", binding))

    def release(self, assignment_id: str) -> None:
        """Remove only the view and helper; backing-volume data is never deleted."""
        digest = hashlib.sha256(assignment_id.encode()).hexdigest()
        name = "astrabox-view-" + digest[:32]
        labels = {MANAGED: "mergerfs", OWNER: digest[:63]}
        volume = self._get(self.client.volumes, name)
        if volume is not None:
            verify_labels(volume.attrs.get("Labels"), labels, name)
            volume.remove(force=False)
        container = self._get(self.client.containers, name)
        if container is not None:
            verify_labels(container.labels, labels, name)
            if container.status == "running":
                container.stop(timeout=30)
            container.remove(force=False)
        receipt = self._get(self.client.volumes, name + "-receipt")
        if receipt is not None:
            verify_labels(receipt.attrs.get("Labels"), labels, name + "-receipt")
            receipt.remove(force=False)

    def attach(self, assignment_id: str, sandbox_id: str, backend: str) -> None:
        """A separate Docker volume retains the immutable receipt after helper loss."""
        digest = hashlib.sha256(assignment_id.encode()).hexdigest()
        name = "astrabox-view-" + digest[:32] + "-receipt"
        view = self._get(self.client.volumes, name.removesuffix("-receipt"))
        if view is None:
            raise RuntimeError(
                f"cannot attach sandbox to missing workspace volume/{name.removesuffix('-receipt')}"
            )
        verify_labels(
            view.attrs.get("Labels"), {MANAGED: "mergerfs", OWNER: digest[:63]}, view.name
        )
        labels = {
            MANAGED: "mergerfs",
            OWNER: digest[:63],
            "astrabox.storage-receipt": "true",
            "astrabox.storage-id": assignment_id,
            "astrabox.sandbox-id": sandbox_id,
            "astrabox.sandbox-backend": backend,
        }
        volume = self._get(self.client.volumes, name)
        if volume is None:
            volume = self.client.volumes.create(name=name, driver="local", labels=labels)
        verify_labels(volume.attrs.get("Labels"), labels, name)

    def attached(self) -> list[tuple[str, str, str]]:
        """Enumerate durable ownership receipts, including stopped helper containers."""
        volumes = self.client.volumes.list(
            filters={"label": [MANAGED + "=mergerfs", "astrabox.storage-receipt=true"]}
        )
        result = []
        for volume in volumes:
            labels = volume.attrs.get("Labels") or {}
            result.append(
                (
                    labels["astrabox.storage-id"],
                    labels["astrabox.sandbox-backend"],
                    labels["astrabox.sandbox-id"],
                )
            )
        return result
