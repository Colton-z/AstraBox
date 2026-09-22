"""Supply an existing deployment volume to the platform's workspace router.

``provision_mounts`` returns the backing volume and directory plan. The common
mergerfs router creates the view mounted into the sandbox and binds prepared
entries before engine activation. ``prepare`` checks that the workload path
is on a mount; the router separately checks that it is the expected mergerfs
view. A missing mount is refused so user files cannot silently land on the
container's ephemeral root filesystem.

Writes go through the mounted filesystem without a provider copy or flush
step. Persistent workspace storage is optional and separate from the native
SessionStore mirrored to the platform database.
"""

from __future__ import annotations

import shlex
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.core.service.orchestrator.runtime.sandbox_client import (
    get_underlying_sandbox,
)
from astrabox.core.service.orchestrator.runtime.storage._command_result import (
    _extract_command_output_text,
)
from astrabox.seams.storage import StorageMountPlan, StorageProvider, WorkspaceRef, register_storage

logger = get_logger(__name__)


class MountedVolumeStorage(StorageProvider):
    """A medium the sandbox backend mounted before the box started."""

    name = "mounted_volume"

    async def provision_mounts(
        self, assignment_id: str, mounts: tuple[tuple[str, str], ...]
    ) -> StorageMountPlan:
        """Use the deployment's existing volume without per-box storage resources."""
        from astrabox.common.utils.settings import load_astrabox_settings

        volume = str(load_astrabox_settings().sandbox_workspace_volume or "").strip()
        if not volume:
            raise RuntimeError("persistent workspace mount requested without a volume")
        return StorageMountPlan(volume_name=volume, mounts=mounts)

    async def prepare(
        self,
        ref: WorkspaceRef,
        *,
        box: Any,
        box_path: str,
        owner: str | None = None,
        group: str | None = None,
    ) -> None:
        """Confirm the medium is present at `box_path`.

        Nothing is materialised: the mount was established when the box was
        created, and there is no in-box remount for this backend. What remains
        is to refuse a box that does not have it, because an agent dispatched
        over the container's own disk writes files the next box will not find,
        and the writing looks exactly like success.
        """

        # `owner` and `group` are not applied here: this medium arrived with
        # its ownership already set, by the backend, at create. There is no
        # in-box remount to change it through, and a chown of a mounted volume
        # would rewrite the medium every other box shares.
        # Resolve through workload-facing symlinks, then ask which filesystem
        # contains the path. A shared box mounts the Agent's home root before
        # any Session exists, so its conversation paths are children of that
        # mount point. A directory listing
        # answers a different question: the directory exists on the container
        # root filesystem too, which is precisely the unsafe state to refuse.
        underlying = get_underlying_sandbox(box)
        commands = getattr(underlying, "commands", None)
        if commands is None or not callable(getattr(commands, "run", None)):
            raise RuntimeError(
                f"cannot confirm the workspace mount for {ref.key()!r}: this "
                f"sandbox exposes no command channel"
            )
        quoted = shlex.quote(box_path)
        probe = (
            f"p=$(readlink -f {quoted} 2>/dev/null || echo {quoted}); "
            'target=$(findmnt -n -o TARGET -T "$p" 2>/dev/null || true); '
            'case "$target" in ""|/) echo UNMOUNTED ;; '
            '*) case "$p" in "$target"|"$target"/*) '
            'echo "MOUNTED:$target" ;; *) echo UNMOUNTED ;; esac ;; esac'
        )
        result = await commands.run(f"sh -c {shlex.quote(probe)}")
        output = _extract_command_output_text(result)
        if "MOUNTED" not in output or "UNMOUNTED" in output:
            raise RuntimeError(
                f"workspace {ref.key()!r} is not mounted at {box_path!r} in this "
                f"sandbox; work done here would be written to the box's own disk, "
                f"which the next box will not have. probe={output.strip()[:400]!r}"
            )
        logger.info(
            "workspace mount confirmed workspace=%s path=%s", ref.key(), box_path
        )

register_storage(MountedVolumeStorage.name, MountedVolumeStorage())
