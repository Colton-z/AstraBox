"""Coordinate storage resource readiness with the existing sandbox lifecycle."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.core.service.orchestrator.runtime.storage.mergerfs import OWNER, workspace_router
from astrabox.seams.sandbox import (
    SANDBOX_INSTALLATION_METADATA_KEY,
    SANDBOX_LIFECYCLE_PROBE_NOT_FOUND,
    SANDBOX_MANAGED_BY_METADATA_KEY,
    SANDBOX_MANAGED_BY_METADATA_VALUE,
    SandboxCreateSpec,
    sandbox_for_name,
    sandbox_installation_id,
)
from astrabox.seams.storage import storage_provider

logger = get_logger(__name__)

#: How long a view waits for the box it was provisioned for before
#: reconciliation may release it. The create request is bounded by the ready
#: budget, but the lifecycle server can still be pulling the image when the
#: client gives up and create the box afterwards; a view released in that
#: window would leave the late box without its workspace. This is the window
#: :meth:`RemoteAgentRuntimeManager.reap_ownerless_sandboxes` gives a box whose
#: owner rows may still be being written, for the same in-flight create.
UNCLAIMED_VIEW_GRACE_SECONDS = 600.0


async def create_sandbox_with_storage(backend: Any, spec: SandboxCreateSpec) -> Any:
    """Prepare storage before create, preserving a recovered box's original entry."""
    if not spec.workspace_mounts:
        return await backend.create_sandbox(spec)
    storage = storage_provider()
    existing = await backend.find_sandbox_by_assignment(spec.assignment_id)
    if existing is not None:
        storage_assignment = existing.metadata.get(OWNER)
        if not storage_assignment:
            raise RuntimeError(f"persistent sandbox {existing.sandbox_id!r} has no workspace route")
        handle = await backend.create_sandbox(spec)
        await workspace_router.attach_sandbox(
            storage_assignment, sandbox_id=existing.sandbox_id, sandbox_backend=backend.name
        )
        return handle

    # The supplier pool reuses its idle assignment after adoption. The member's
    # unique create-owner distinguishes the still-mounted claimed predecessor.
    # The identity travels in sandbox metadata, whose label values allow 63 chars.
    storage_assignment = hashlib.sha256(
        f"{spec.assignment_id}\0{spec.session_id}".encode()
    ).hexdigest()[:63]
    backing = await storage.provision_mounts(storage_assignment, spec.workspace_mounts)
    plan = await workspace_router.provision_mounts(
        storage_assignment, backing, sandbox_backend=backend.name
    )
    prepared = replace(
        spec,
        workspace_volume=plan.volume_name,
        workspace_volume_create_if_missing=False,
        workspace_mounts=plan.mounts,
        metadata={**spec.metadata, OWNER: storage_assignment},
    )
    # A failed create may still have allocated a box. Leave storage intact for
    # correlated recovery; never unmount a resource on an ambiguous API failure.
    # The helper's pending receipt lets reconcile_workspace_mounts release the
    # view once no box carries its storage assignment.
    handle = await backend.create_sandbox(prepared)
    from astrabox.core.service.orchestrator.runtime.sandbox_client import extract_sandbox_id

    await workspace_router.attach_sandbox(
        storage_assignment, sandbox_id=extract_sandbox_id(handle), sandbox_backend=backend.name
    )
    return handle


async def bind_prepared_workspace(
    *, backend: Any, sandbox_id: str, session_id: str, mounts: tuple[tuple[str, str], ...]
) -> None:
    """Bind storage after allocation ownership and before prepared-engine activation."""
    if not mounts:
        return
    storage = storage_provider()
    descriptor = await backend.describe_sandbox(sandbox_id)
    assignment = str(descriptor.metadata.get(OWNER) or "").strip()
    if not assignment:
        raise RuntimeError(f"prepared sandbox {sandbox_id!r} has no storage assignment receipt")
    backing = await storage.provision_mounts(assignment, mounts)
    await workspace_router.bind(assignment, backing, session_id)


async def reconcile_workspace_mounts() -> dict[str, int]:
    """Reclaim only this installation's views that no sandbox can still use.

    A view with an attached receipt is released once its recorded sandbox is
    independently confirmed absent. A view without one — its create failed,
    its provisioning stopped part way, or the receipt write failed — is
    released once it is older than :data:`UNCLAIMED_VIEW_GRACE_SECONDS` and the
    backend lists no box of this installation carrying its storage assignment.
    Either release still refuses while a consumer mounts the view (Docker will
    not remove a volume a container references; the Kubernetes driver checks
    for Pods using the claim), and one view that cannot be released does not
    stop the others.
    """
    summary = {
        "workspace_mounts_released": 0,
        "workspace_mounts_unclaimed_released": 0,
        "workspace_mount_release_failures": 0,
    }
    if not load_astrabox_settings().sandbox_workspace_volume:
        return summary
    for assignment, backend_name, sandbox_id in await workspace_router.attached_mounts():
        try:
            probe = await sandbox_for_name(backend_name).probe(sandbox_id)
            if probe.probe_status != SANDBOX_LIFECYCLE_PROBE_NOT_FOUND:
                continue
            await workspace_router.release_mounts(assignment)
            summary["workspace_mounts_released"] += 1
        except Exception:
            logger.exception(
                "workspace view release failed assignment=%s sandbox=%s", assignment, sandbox_id
            )
            summary["workspace_mount_release_failures"] += 1
    ownership = {
        SANDBOX_MANAGED_BY_METADATA_KEY: SANDBOX_MANAGED_BY_METADATA_VALUE,
        SANDBOX_INSTALLATION_METADATA_KEY: await sandbox_installation_id(),
    }
    now = datetime.now(timezone.utc)
    for assignment, backend_name, created_at in await workspace_router.unattached_mounts():
        if (now - created_at).total_seconds() < UNCLAIMED_VIEW_GRACE_SECONDS:
            continue
        try:
            carriers = await sandbox_for_name(backend_name).list_sandboxes(
                page=1, page_size=1, metadata={**ownership, OWNER: assignment}
            )
            if carriers.total_items:
                continue
            await workspace_router.release_mounts(assignment)
            summary["workspace_mounts_unclaimed_released"] += 1
            logger.info(
                "released workspace view assignment=%s: no sandbox carries it %.0fs after "
                "its helper was created",
                assignment,
                (now - created_at).total_seconds(),
            )
        except Exception:
            logger.exception("unclaimed workspace view release failed assignment=%s", assignment)
            summary["workspace_mount_release_failures"] += 1
    return summary
