"""Prepare, adopt, and discard one whole engine box per prewarmed Agent.

Under conversation tenancy the prepared unit is a complete provisioned box.
The platform builds it under slot identity, then the engine's
``prepare_runtime`` performs its native input-free preparation: that may prove
an image-resident service or create and park an engine conversation. Claim
later adopts this exact box and the engine's opaque preparation receipt.

This module shares :mod:`prepared_slots`' manifest discipline — the one
``_prepared_slot`` document per Agent, every transition a compare-and-set,
claim/clear/TTL owned there — and supplies the placement-specific halves: a
box is built by the platform, adopted by re-pointing its ownership metadata,
and discarded by destroying it. These helpers operate on an explicit Agent-row
box manifest. The SDK client pool's conversation-tenancy inventory is managed
by ``client_pool`` and prepared through ``runtime_preparation``.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.registry import get_engine_adapter
from astrabox.core.service.orchestrator.workspace.deployment_conversation import (
    build_conversation_identity,
)
from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.seams.sandbox import sandbox_for_name, sandbox_for_template

logger = get_logger(__name__)

#: Manifest ``placement`` value distinguishing a whole-box unit from the
#: default Agent-shared slot; :func:`prepared_slots.reap_slot_manifest_if_stale`
#: dispatches its discard and adoption checks on it.
PLACEMENT_CONVERSATION_BOX = "conversation_box"

#: Receipt keys this module reads itself and writes into the manifest under
#: its own names. Every other key an adapter returns is engine evidence and is
#: carried verbatim.
_PLATFORM_RECEIPT_KEYS = frozenset(
    {
        "engine_kind",
        "sandbox_id",
        "cwd",
        "spawn_fingerprint",
        "activation_mcp_servers",
        "gateway_substitution",
        "runtime_identity",
        "runtime_generation",
        "sandbox_backend",
        "workspace_id",
        "model_credential",
        "runtime_env",
        "environment_credential_contract",
    }
)


async def prepare_box_for_agent(
    template: Any,
    *,
    runtime_manager: Any,
    agent_repo: Any | None = None,
) -> dict[str, Any] | None:
    """Ensure the Agent holds one unclaimed prepared box; return its manifest.

    Level-based like the slot flow: an existing unclaimed manifest whose
    runtime generation still matches is returned as-is. Returns None when
    the engine's preparation cannot take the whole-box form
    (``EngineAdapter.prepares_conversation_box``) — the ordinary Session path
    stays available and pays the serial cost.

    The platform owns the box throughout preparation. Everything after its
    create (engine proof, ledger, manifest CAS) is cleaned up here by destroying
    that exact backend's box.
    """

    from astrabox.core.service.orchestrator.agent import prepared_slots

    agent_id = str(getattr(template, "agent_id", "") or "").strip()
    engine_kind = str(getattr(template, "engine_kind", "") or "").strip()
    generation = str(getattr(template, "runtime_generation", "") or "").strip()
    if not agent_id or not engine_kind:
        raise APIError(
            code="AGENT_PREWARM_CONFIG_INVALID",
            message="box preparation requires an Agent id and engine kind",
            status_code=500,
        )
    if not generation:
        # No published runtime generation means the Agent is not prewarmed;
        # there is nothing to key a box to.
        return None
    adapter = get_engine_adapter(engine_kind)
    from astrabox.core.service.orchestrator.engine.base import EngineAdapter

    if (
        getattr(type(adapter), "prepare_runtime", None)
        is EngineAdapter.prepare_runtime
    ) or not bool(getattr(type(adapter), "prepares_conversation_box", False)):
        # Either the engine has no input-free preparation seam at all, or its
        # prepared unit needs an Agent-shared slot (runner plus parked child)
        # that a per-conversation box does not carry. A declared absence,
        # checked before any box is built for it.
        return None

    repo = agent_repo if agent_repo is not None else AgentRepository()
    reaped = await prepared_slots.reap_slot_manifest_if_stale(
        template, agent_repo=repo
    )
    if reaped:
        logger.info(
            "reaped slot manifest before box refill: agent=%s (%s)",
            agent_id,
            reaped,
        )
    row = await repo.get_agent(agent_id)
    if not isinstance(row, dict):
        return None
    existing = row.get(prepared_slots.PREPARED_SLOT_FIELD)
    if isinstance(existing, dict):
        if (
            str(existing.get("state") or "") == "prepared"
            and str(existing.get("runtime_generation") or "") == generation
        ):
            return existing
        # A live claimed manifest belongs to its Session; the reap above
        # already destroyed anything stale. This refill has nothing to add.
        return None
    await prepared_slots.ensure_manifest_field_decidable(repo, agent_id, row)

    slot_id = f"slot-{uuid.uuid4().hex}"
    identity = build_conversation_identity(
        session_id=slot_id,
        agent_id=agent_id,
        template=template,
    )
    model_access = runtime_manager.resolve_model_access(
        getattr(template, "model_config", None) or {}
    )
    request = adapter.sandbox_request(template=template, model_access=model_access)
    provider = sandbox_for_template(template)
    sandbox_backend = str(provider.name)
    from astrabox.core.service.orchestrator.engine.base import (
        EnginePreparationContext,
    )
    from astrabox.core.service.orchestrator.engine.provisioning import (
        provision_engine_slot_sandbox,
    )

    provisioned = await provision_engine_slot_sandbox(
        runtime_manager,
        slot_id=slot_id,
        template=template,
        runtime_identity=identity,
        request=request,
    )
    sandbox_id = provisioned.sandbox_id
    identity = {**identity, "sandbox_id": sandbox_id}
    try:
        from astrabox.core.service.orchestrator.engine import transcript_mirror
        from astrabox.core.service.orchestrator.engine.provisioning import (
            _session_log_declaration,
        )

        if _session_log_declaration(template) is not None:
            await transcript_mirror.mark_mirror_unclaimed(
                provisioned.sandbox,
                unclaimed_for=timedelta(
                    seconds=prepared_slots.PREPARED_SLOT_TTL_SECONDS + 300
                ),
            )
        receipt = await adapter.prepare_runtime(
            EnginePreparationContext(
                template=template,
                slot_id=slot_id,
                # A whole-box unit has no runner barrier, so the token is not
                # persisted as though a later consumer could verify it.
                activation_token=secrets.token_hex(32),
                placement=PLACEMENT_CONVERSATION_BOX,
                sandbox=provisioned.sandbox,
                sandbox_id=sandbox_id,
                cwd=provisioned.cwd,
                runtime_identity=identity,
                model_access=model_access,
                model_credential=provisioned.model_credential,
                runtime_env=dict(provisioned.runtime_env),
                runner_uri=None,
                preparation_fingerprint=generation,
                deployment_settings=runtime_manager.deployment_settings,
                workspace_id=provisioned.workspace_id,
                gateway_substitution=provisioned.gateway_substitution,
            )
        )
        if provisioned.gateway_substitution:
            # Ledger bookkeeping only: each box's gateway binding lives in
            # that box's own vault, so no sibling writer composes from these
            # entries — but pruning them is what garbage-collects a dead
            # Session's gateway key.
            await prepared_slots.record_gateway_entry(
                repo, agent_id, slot_id=slot_id
            )
        # Everything the adapter returned that this module has no opinion
        # about travels with the manifest: what a prepared unit consists of is
        # the engine's to describe, and a platform that copies only the keys it
        # recognizes silently discards the evidence a claim needs — measured,
        # when pi's parked pipe id never reached its own claim and every claim
        # rebuilt the child it had already prepared.
        engine_evidence = {
            key: value
            for key, value in dict(receipt or {}).items()
            if key not in _PLATFORM_RECEIPT_KEYS
        }
        manifest = {
            **engine_evidence,
            "slot_id": slot_id,
            "state": "prepared",
            "placement": PLACEMENT_CONVERSATION_BOX,
            "engine_kind": engine_kind,
            "sandbox_id": sandbox_id,
            "sandbox_backend": sandbox_backend,
            "cwd": provisioned.cwd,
            "workspace_id": provisioned.workspace_id,
            "runtime_generation": generation,
            "spawn_fingerprint": str(receipt.get("spawn_fingerprint") or ""),
            "activation_mcp_servers": list(
                receipt.get("activation_mcp_servers") or []
            ),
            "gateway_substitution": provisioned.gateway_substitution,
            "model_credential": provisioned.model_credential,
            "runtime_env": dict(provisioned.runtime_env),
            "environment_credential_contract": [
                dict(item) for item in provisioned.environment_credential_contract
            ],
            "runtime_identity": identity,
            "prepared_at": datetime.now(timezone.utc).isoformat(),
        }
        won = await repo.compare_and_update_agent(
            agent_id,
            expected={prepared_slots.PREPARED_SLOT_FIELD: None},
            updates={prepared_slots.PREPARED_SLOT_FIELD: manifest},
        )
        if not won:
            raise APIError(
                code="AGENT_PREWARM_SLOT_CONFLICT",
                message=(
                    "another refill published a prepared box first; "
                    "discarding this one"
                ),
                status_code=409,
            )
        logger.info(
            "prepared box published: agent=%s slot=%s box=%s",
            agent_id,
            slot_id,
            sandbox_id,
        )
        return manifest
    except BaseException:
        await destroy_slot_box(
            sandbox_id,
            sandbox_backend=sandbox_backend,
            slot_id=slot_id,
        )
        raise


async def destroy_slot_box(
    sandbox_id: str,
    *,
    sandbox_backend: str,
    slot_id: str | None,
) -> bool:
    """Destroy one slot box and report whether its absence was confirmed.

    A preparation caller may already be unwinding another failure, so this
    helper records provider failures instead of replacing that original
    exception. A discard caller must check the result before deleting the
    manifest: without confirmed destruction, that manifest is the only durable
    evidence that prevents a cold fallback from starting a second runtime.
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
    return True


async def discard_prepared_box(
    agent_id: str,
    manifest: dict[str, Any],
    *,
    reason: str,
    agent_repo: Any | None = None,
) -> None:
    """Destroy a box manifest's sandbox and clear the manifest.

    The box IS the placement: destroying it takes the resident engine runtime
    and any unclaimed state with it. The manifest clear uses the same
    compare-and-set as every other transition, so a concurrent claim that won
    first keeps its manifest.
    """

    from astrabox.core.service.orchestrator.agent import prepared_slots

    repo = agent_repo if agent_repo is not None else AgentRepository()
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
                "the manifest was retained and cold fallback was refused"
            ),
            status_code=502,
        )
    await repo.compare_and_update_agent(
        agent_id,
        expected={prepared_slots.PREPARED_SLOT_FIELD: manifest},
        updates={prepared_slots.PREPARED_SLOT_FIELD: None},
    )


async def claimed_session_adopted_box(manifest: dict[str, Any]) -> bool:
    """Whether the claiming Session took ownership of the manifest's box.

    Same question as the slot flow's adoption check, answered by the box
    shape: a claim records the box on the Session row as its whole-sandbox
    startup allocation, and destroying an adopted box would kill the live
    conversation — only the manifest is debris then.
    """

    session_id = str(manifest.get("claimed_session_id") or "").strip()
    box = str(manifest.get("sandbox_id") or "").strip()
    if not session_id or not box:
        return False
    from astrabox.persistence.repository.session_repository import (
        SessionRepository,
    )

    session = await SessionRepository().get_session(session_id)
    if not isinstance(session, dict):
        return False
    if str(session.get("sandbox_id") or "").strip() == box:
        return True
    allocation = session.get("startup_allocation")
    return isinstance(allocation, dict) and (
        str(allocation.get("sandbox_id") or "").strip() == box
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
