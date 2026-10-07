"""Adopt and discard a whole prepared conversation box.

Under conversation tenancy the prepared unit is a complete box from the Agent's
client pool, prepared before publication. A Session adopts the exact box it
acquired; a box whose activation fails is destroyed.
"""

from __future__ import annotations

from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.seams.sandbox import sandbox_for_name

logger = get_logger(__name__)

#: Manifest ``placement`` value distinguishing a whole-box unit from a seat in
#: an Agent-shared box.
PLACEMENT_CONVERSATION_BOX = "conversation_box"


async def destroy_slot_box(
    sandbox_id: str,
    *,
    sandbox_backend: str,
    slot_id: str | None,
) -> bool:
    """Destroy one prepared box and report whether its absence was confirmed.

    A caller may already be unwinding another failure, so this helper records
    provider failures instead of replacing that original exception.
    """

    target = str(sandbox_id or "").strip()
    backend = str(sandbox_backend or "").strip()
    if not target or not backend:
        logger.warning(
            "prepared box destroy lacks durable identity: slot=%s sandbox=%s backend=%s",
            slot_id,
            target or "<missing>",
            backend or "<missing>",
        )
        return False
    try:
        destruction = await sandbox_for_name(backend).confirm_destroyed(target)
    except Exception as exc:  # noqa: BLE001 - cleanup evidence, not control flow
        logger.warning(
            "prepared box destroy failed: slot=%s sandbox=%s err=%s",
            slot_id,
            target,
            exc,
        )
        return False
    if not destruction.confirmed:
        logger.warning(
            "prepared box destruction unconfirmed: slot=%s sandbox=%s (%s)",
            slot_id,
            target,
            destruction.detail,
        )
        return False
    if slot_id:
        from astrabox.core.service.orchestrator.agent.prepared_slots import (
            _delete_prepared_platform_mcp_binding,
        )

        await _delete_prepared_platform_mcp_binding(slot_id)
    return True


async def discard_prepared_box(
    agent_id: str,
    manifest: dict[str, Any],
    *,
    reason: str,
) -> None:
    """Destroy a claimed conversation box whose activation failed.

    The box IS the placement: destroying it takes the resident engine runtime
    and any unclaimed state with it.
    """

    logger.info(
        "discarding prepared box: agent=%s slot=%s box=%s reason=%s",
        agent_id,
        manifest.get("slot_id"),
        manifest.get("sandbox_id"),
        reason,
    )
    destroyed = await destroy_slot_box(
        str(manifest.get("sandbox_id") or ""),
        sandbox_backend=str(manifest.get("sandbox_backend") or ""),
        slot_id=str(manifest.get("slot_id") or "") or None,
    )
    if not destroyed:
        raise APIError(
            code="AGENT_PREWARM_DISCARD_UNCONFIRMED",
            message=(
                "could not confirm destruction of prepared sandbox "
                f"{str(manifest.get('sandbox_id') or '<missing>')!r}; "
                "cold fallback was refused"
            ),
            status_code=502,
        )


async def adopt_claimed_box(
    template: Any,
    manifest: dict[str, Any],
    *,
    session_id: str,
    assignment_id: str,
) -> Any:
    """Re-point a claimed box's ownership to its Session; return the handle.

    The box was created with the SLOT id as its ``astrabox.session-id``
    metadata — a slot never impersonates a Session — so adoption rewrites
    that reverse-lookup key to the claiming Session before anything records
    the allocation. Ordering matters: with the metadata re-pointed first, a
    Session whose start dies mid-claim leaves a box the manifest reaper can
    still destroy (its row never adopted the allocation), while the
    claim-gated disposal path (`SandboxProvider.claim_of`) already answers
    MINE for the Session's own cleanup.

    The metadata patch is the same one the OpenSandbox pool acquire performs
    when it assigns a pooled box (`agent_pool.OpenSandboxAgentPoolRegistry
    .acquire`); this module composes provider machinery exactly as the slot
    allocator does.
    """

    backend = str(manifest.get("sandbox_backend") or "").strip()
    if not backend:
        raise APIError(
            code="AGENT_PREWARM_CONFIG_INVALID",
            message="prepared box manifest carries no sandbox backend",
            status_code=500,
        )
    provider = sandbox_for_name(backend)
    handle = await provider.connect(str(manifest.get("sandbox_id") or ""))
    await provider.adopt_sandbox_identity(
        handle,
        session_id=str(session_id),
        assignment_id=str(assignment_id),
    )
    return handle
