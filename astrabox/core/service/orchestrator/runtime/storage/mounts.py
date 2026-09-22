"""Coordinate storage resource readiness with the existing sandbox lifecycle."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any

from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.core.service.orchestrator.runtime.storage.mergerfs import OWNER, workspace_router
from astrabox.seams.sandbox import (
    SANDBOX_LIFECYCLE_PROBE_NOT_FOUND,
    SandboxCreateSpec,
    sandbox_for_name,
)
from astrabox.seams.storage import storage_provider


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
    plan = await workspace_router.provision_mounts(storage_assignment, backing)
    prepared = replace(
        spec,
        workspace_volume=plan.volume_name,
        workspace_volume_create_if_missing=False,
        workspace_mounts=plan.mounts,
        metadata={**spec.metadata, OWNER: storage_assignment},
    )
    # A failed create may still have allocated a box. Leave storage intact for
    # correlated recovery; never unmount a resource on an ambiguous API failure.
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
    """Reclaim only storage whose recorded sandbox is independently confirmed absent."""
    released = 0
    if not load_astrabox_settings().sandbox_workspace_volume:
        return {"workspace_mounts_released": released}
    for assignment, backend_name, sandbox_id in await workspace_router.attached_mounts():
        probe = await sandbox_for_name(backend_name).probe(sandbox_id)
        if probe.probe_status != SANDBOX_LIFECYCLE_PROBE_NOT_FOUND:
            continue
        await workspace_router.release_mounts(assignment)
        released += 1
    return {"workspace_mounts_released": released}
