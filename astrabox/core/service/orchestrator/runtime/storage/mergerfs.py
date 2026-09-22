"""Platform workspace routing over the medium supplied by the storage seam."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.core.service.orchestrator.runtime.sandbox_client import get_underlying_sandbox
from astrabox.core.service.orchestrator.runtime.storage._command_result import (
    _extract_command_output_text,
)
from astrabox.seams.storage import StorageMountPlan, WorkspaceRef

OWNER = "astrabox.storage-assignment"
INPUT = "astrabox.storage-input"
MANAGED = "astrabox.storage-managed"
CLI = ["python3", "/opt/astrabox/mergerfs_node.py"]


@dataclass(frozen=True)
class MountAssignment:
    """Deterministic resource identity; the backing volume is never owned here."""

    assignment_id: str
    name: str
    host_path: str
    backing_volume: str
    image: str
    shared: bool
    mounts: tuple[tuple[str, str], ...]

    @classmethod
    def configured(cls, assignment_id: str, backing: StorageMountPlan) -> MountAssignment:
        """Validate the deployment and paths before creating privileged resources."""
        settings = load_astrabox_settings()
        if not assignment_id or len(assignment_id) > 256:
            raise ValueError("workspace assignment requires an identity of 1–256 characters")
        root = PurePosixPath(settings.workspace_mount_root)
        if not root.is_absolute() or root == PurePosixPath("/") or ".." in root.parts:
            raise ValueError("workspace_mount_root must be an absolute non-root directory")
        if not settings.workspace_mounter_image or not backing.volume_name:
            raise ValueError("workspace routing requires a helper image and backing volume")
        if settings.workspace_storage_topology not in {"local", "shared"}:
            raise ValueError("workspace_storage_topology must be local or shared")
        mounts = backing.mounts
        if not mounts or len({path for path, _ in mounts}) != len(mounts):
            raise ValueError("workspace mounts must contain unique nonempty box paths")
        mounts = tuple((box_path, subpath.lstrip("/")) for box_path, subpath in mounts)
        for box_path, subpath in mounts:
            box = PurePosixPath(box_path)
            backing_path = PurePosixPath(subpath)
            if not box.is_absolute() or ".." in box.parts or box == PurePosixPath("/"):
                raise ValueError(f"invalid workspace mount path {box_path!r}")
            if not subpath or backing_path.is_absolute() or ".." in backing_path.parts:
                raise ValueError(f"invalid workspace backing subpath {subpath!r}")
            if backing_path.name == ".mergerfs":
                raise ValueError("workspace leaf cannot name the supplier's control entry")
        name = "astrabox-view-" + hashlib.sha256(assignment_id.encode()).hexdigest()[:32]
        return cls(
            assignment_id,
            name,
            str(root / name),
            backing.volume_name,
            settings.workspace_mounter_image,
            settings.workspace_storage_topology == "shared",
            mounts,
        )

    def identity(self) -> dict[str, str]:
        """Ownership labels use a digest because arbitrary assignment IDs are not labels."""
        return {
            MANAGED: "mergerfs",
            OWNER: hashlib.sha256(self.assignment_id.encode()).hexdigest()[:63],
        }

    def labels(self) -> dict[str, str]:
        """Record immutable create inputs for safe replay after an uncertain response."""
        payload = json.dumps(
            [
                self.assignment_id,
                self.host_path,
                self.backing_volume,
                self.image,
                self.shared,
                self.mounts,
            ],
            separators=(",", ":"),
        )
        return {**self.identity(), INPUT: hashlib.sha256(payload.encode()).hexdigest()[:63]}

    def command(self, action: str, binding: str | None = None) -> list[str]:
        """Invoke the helper's local control entry point, not an in-box service."""
        command = [*CLI, action, "--root", "/views"]
        if action != "status":
            command += ["--data", "/data", "--mounts", json.dumps(self.mounts)]
            if self.shared and action == "serve":
                command.append("--shared")
        if binding is not None:
            command += ["--binding", binding]
        return command


def verify_labels(actual: dict[str, str] | None, expected: dict[str, str], resource: str) -> None:
    """Refuse to adopt or delete a resource owned by another input or assignment."""
    if any((actual or {}).get(key) != value for key, value in expected.items()):
        raise RuntimeError(
            f"workspace resource ownership/input mismatch: {resource}; retained unchanged"
        )


def verify_status(output: str, assignment: MountAssignment, binding: str | None = None) -> None:
    """Read back actual branches and native passthrough before handing out a view."""
    result = json.loads(output)
    if result.get("state") != "READY":
        raise RuntimeError(f"workspace helper {assignment.name} is not READY: {output[:1000]}")
    if binding is not None and result.get("binding") != binding:
        raise RuntimeError(f"workspace helper {assignment.name} returned a different binding")
    actual = result.get("mounts", [])
    expected = list(assignment.mounts)
    if [(item.get("box_path"), item.get("storage_subpath")) for item in actual] != expected:
        raise RuntimeError(f"workspace helper {assignment.name} returned different workspace paths")
    if [item.get("consumer_subpath") for item in actual] != [
        f"{index}/{PurePosixPath(subpath).name}"
        for index, (_, subpath) in enumerate(expected)
    ]:
        raise RuntimeError(f"workspace helper {assignment.name} did not isolate the control root")
    if any(
        item.get("passthrough_io") != "rw" or item.get("cache_files") != "auto-full"
        for item in actual
    ):
        raise RuntimeError(
            f"workspace helper {assignment.name} did not enable native I/O passthrough"
        )


class MergerfsWorkspaceRouter:
    """A common platform route; it does not select or implement a storage medium."""

    def validate_configuration(self) -> None:
        """Require routing infrastructure whenever persistent workspaces are enabled."""
        settings = load_astrabox_settings()
        if not settings.sandbox_workspace_volume:
            return
        MountAssignment.configured(
            "configuration-check",
            StorageMountPlan(settings.sandbox_workspace_volume, (("/workspace", "configuration-check"),)),
        )
        from astrabox.deploy.sandbox_server import sandbox_runtime

        sandbox_runtime()

    @staticmethod
    def _driver() -> Any:
        from astrabox.deploy.sandbox_server import sandbox_runtime

        if sandbox_runtime() == "kubernetes":
            from astrabox.core.service.orchestrator.runtime.storage._mergerfs_kubernetes import KubernetesMounts

            return KubernetesMounts()
        from astrabox.core.service.orchestrator.runtime.storage._mergerfs_docker import DockerMounts

        return DockerMounts()

    async def provision_mounts(
        self, assignment_id: str, backing: StorageMountPlan
    ) -> StorageMountPlan:
        """Schedule a helper, then expose its ready views through a standard volume."""
        assignment = MountAssignment.configured(assignment_id, backing)
        await asyncio.to_thread(self._provision, assignment)
        return StorageMountPlan(
            volume_name=assignment.name,
            # Export the workspace child, never the FUSE root containing the
            # supplier's privileged .mergerfs runtime-control entry.
            mounts=tuple(
                (path, f"{index}/{PurePosixPath(subpath).name}")
                for index, (path, subpath) in enumerate(backing.mounts)
            ),
        )

    def _provision(self, assignment: MountAssignment) -> None:
        try:
            with self._driver() as driver:
                driver.provision(assignment)
        except Exception as exc:
            raise RuntimeError(
                f"workspace provisioning failed; assignment={assignment.assignment_id!r}, "
                f"helper/view={assignment.name!r}, host_path={assignment.host_path!r}; "
                f"created resources are retained for diagnosis: {exc}"
            ) from exc

    async def bind(
        self, assignment_id: str, backing: StorageMountPlan, binding: str
    ) -> None:
        """Select the workspace while the lifecycle still owns the unclaimed box."""
        assignment = MountAssignment.configured(assignment_id, backing)
        await asyncio.to_thread(self._bind, assignment, binding)

    def _bind(self, assignment: MountAssignment, binding: str) -> None:
        with self._driver() as driver:
            verify_status(driver.bind(assignment, binding), assignment, binding)

    async def release_mounts(self, assignment_id: str) -> None:
        """Release helper resources only after the caller confirms sandbox destruction."""
        await asyncio.to_thread(self._release, assignment_id)

    async def attach_sandbox(
        self, assignment_id: str, *, sandbox_id: str, sandbox_backend: str
    ) -> None:
        """Record which supplier box must disappear before a view can be released."""
        await asyncio.to_thread(self._attach, assignment_id, sandbox_id, sandbox_backend)

    def _attach(self, assignment_id: str, sandbox_id: str, backend: str) -> None:
        with self._driver() as driver:
            driver.attach(assignment_id, sandbox_id, backend)

    async def attached_mounts(self) -> list[tuple[str, str, str]]:
        """Return storage/backend/box receipts for the existing lifecycle reconciler."""
        return await asyncio.to_thread(self._attached)

    def _attached(self) -> list[tuple[str, str, str]]:
        with self._driver() as driver:
            result: list[tuple[str, str, str]] = driver.attached()
            return result

    def _release(self, assignment_id: str) -> None:
        # Cleanup cannot depend on mutable Agent mount settings. Only assignment
        # ownership is checked, and backing data is never a deletion target.
        with self._driver() as driver:
            driver.release(assignment_id)

    async def prepare(
        self,
        ref: WorkspaceRef,
        *,
        box: Any,
        box_path: str,
        owner: str | None = None,
        group: str | None = None,
    ) -> None:
        """Require the promised filesystem, not merely any mounted directory."""
        underlying = get_underlying_sandbox(box)
        if underlying is None:
            raise RuntimeError("cannot confirm mergerfs without a sandbox command channel")
        commands = underlying.commands
        result = await commands.run("findmnt -n -o FSTYPE -T " + shlex.quote(box_path))
        if _extract_command_output_text(result).strip() != "fuse.mergerfs":
            raise RuntimeError(f"workspace {ref.key()!r} at {box_path!r} is not a mergerfs view")


workspace_router = MergerfsWorkspaceRouter()
