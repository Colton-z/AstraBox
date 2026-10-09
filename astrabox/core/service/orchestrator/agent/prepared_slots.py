"""Prepare, claim, and discard engine seats inside an Agent's shared boxes.

A prepared seat is the complete unit a Session start otherwise assembles
serially on the claim path: one isolated placement (private owner, home and
workspace), the conversation bootstrap, a running runner, and an initialized
engine process held before input by the engine adapter's ``prepare`` seam.
The measured budget behind this split is recorded in
``docs/maintainers/claude-runtime-preparation.md``.

Seats are prepared in the Agent's client-pool boxes before a box is published,
as many as the box's memory holds, so the pool's replenishment, its replica
election and its idle expiry cover seats exactly as they cover a conversation
box. A box leaves the pool with all of its seats; the ones still waiting are
recorded in the Agent row's seat ledger, where every claim, clear and
retirement is a compare-and-set on the ledger, so each seat has exactly one
owner.

Ownership: the provider owns placement and the runner; the engine adapter owns
the vendor process; this module composes them and owns the seat ledger.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from astrabox.common.fault_injection import pass_fault_barrier
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.core.service.orchestrator.engine import transcript_mirror
from astrabox.core.service.orchestrator.engine.base import EnginePreparationContext
from astrabox.core.service.orchestrator.engine.provisioning import (
    engine_service_credential,
    _session_log_declaration,
    environment_credential_contract,
    resolve_model_credential_delivery,
    resolve_prepared_environment_credentials,
    write_engine_env_file,
)
from astrabox.core.service.orchestrator.engine.registry import get_engine_adapter
from astrabox.core.service.orchestrator.engine.runtime_profiles import (
    SANDBOX_IMAGE_WORKLOAD_HOME,
    runner_port_for_uid,
)
from astrabox.core.service.orchestrator.runtime.conversation_identity import (
    prepare_agent_runtime_skill_cache,
)
from astrabox.core.service.orchestrator.runtime.sandbox_client import (
    extract_sandbox_id,
    get_underlying_sandbox,
)
from astrabox.core.service.orchestrator.runtime.shared_sandbox_lease import (
    BOX_ADMISSIONS,
    SharedSandboxLease,
    pending_reservations,
    release_box_admission,
    room_for_one_more,
    surviving_admissions,
)
from astrabox.core.service.orchestrator.runtime.storage import (
    bootstrap_conversation_runtime_from_agent_cache,
    prepare_agent_runtime_plugin_cache,
)
from astrabox.core.service.orchestrator.workspace.deployment_conversation import (
    build_conversation_identity,
)
from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.seams.egress_credentials import (
    ModelEgressCredentialSubstitution,
    workload_credential_name,
    workload_model_placeholder,
)
from astrabox.seams.sandbox import sandbox_for_name, sandbox_for_template

from astrabox.core.service.orchestrator.runtime.storage import (
    WORKSPACE_ID_FIELD,
)

logger = get_logger(__name__)

#: The Agent-row ledger of seats in boxes the Agent took from its client pool:
#: every prepared, claimed or retiring engine seat, each in the box it names.
#: A box leaves the pool with all of its seats, so the seats still waiting in
#: it are recorded here for any replica's next conversation to claim.
PREPARED_SLOTS_FIELD = "_prepared_slots"

#: The Agent-row ledger of Session gateway-key ownership: one entry per
#: prepared-or-claimed slot, ``{"slot_id", "session_id"}``, with session_id
#: null until claim. It is bookkeeping for claim and key garbage collection;
#: the provider's own revision-guarded Vault state is the authority for live
#: workload substitutions.
GATEWAY_ENTRIES_FIELD = "_slot_gateway_entries"

#: A ledger mutation that loses a compare-and-set re-reads the whole observed
#: list. Eight attempts matches the Agent-row cursor allocator: this is a short
#: collision loop, not a retry of an external side effect.
_GATEWAY_LEDGER_CAS_ATTEMPTS = 8

#: Session states that release a ledger entry (and its gateway key).
_TERMINAL_SESSION_STATES = {"TERMINATED", "RECOVERY_REQUIRED", "DELETED"}

#: A claimed manifest this old is an orphan: the claiming Session either
#: cleared it within its own start or died. Reaping it frees the placement and
#: releases its seat; a Session that did adopt the slot recorded the
#: placement on its own row and is unaffected by the manifest's removal.
CLAIMED_SLOT_ORPHAN_SECONDS = 10 * 60

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def slot_manifests(row: Any) -> list[dict[str, Any]]:
    """The Agent row's slot manifests, in publication order."""

    raw = row.get(PREPARED_SLOTS_FIELD) if isinstance(row, dict) else None
    return [dict(item) for item in (raw or []) if isinstance(item, dict)]


def _observed_slots(row: dict[str, Any]) -> Any:
    """The exact stored value a compare-and-set on the slot list must match."""

    return row.get(PREPARED_SLOTS_FIELD) if PREPARED_SLOTS_FIELD in row else {"$exists": False}


async def _change_slot(
    repo: Any,
    agent_id: str,
    manifest: dict[str, Any],
    replacement: dict[str, Any] | None,
    *,
    expected: dict[str, Any] | None = None,
    updates: dict[str, Any] | None = None,
) -> bool:
    """Replace or remove exactly ``manifest`` in the slot list.

    False when the list does not hold that exact manifest: another writer
    claimed, retired or replaced it, and that writer's transition stands. A
    write that loses only to a change to a different slot re-reads and retries.
    """

    slot_id = str(manifest.get("slot_id") or "")
    for _attempt in range(_GATEWAY_LEDGER_CAS_ATTEMPTS):
        row = await repo.get_agent(agent_id)
        if not isinstance(row, dict):
            return False
        current = slot_manifests(row)
        if manifest not in current:
            return False
        changed = [
            item
            for item in (
                replacement if str(item.get("slot_id") or "") == slot_id else item
                for item in current
            )
            if item is not None
        ]
        if await repo.compare_and_update_agent(
            agent_id,
            expected={**(expected or {}), PREPARED_SLOTS_FIELD: _observed_slots(row)},
            updates={**(updates or {}), PREPARED_SLOTS_FIELD: changed},
        ):
            return True
    raise APIError(
        code="AGENT_RUNTIME_ERROR",
        message=(
            f"agent {agent_id!r} slot list changed repeatedly while slot "
            f"{slot_id!r} was being updated"
        ),
        status_code=503,
    )


def _manifest_age_seconds(manifest: dict[str, Any], key: str) -> float | None:
    raw = str(manifest.get(key) or "").strip()
    if not raw:
        return None
    try:
        stamped = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamped.tzinfo is None:
        return None
    return (datetime.now(timezone.utc) - stamped).total_seconds()


def _manifest_is_reapable(
    manifest: dict[str, Any],
    *,
    current_runtime_generation: str,
) -> str | None:
    """The reason this manifest should be destroyed, or None to leave it.

    An unparsable timestamp counts as expired: a manifest whose age cannot be
    established must not be trusted to hold a healthy process forever.
    """

    state = str(manifest.get("state") or "")
    if state == "prepared":
        if (
            str(manifest.get("runtime_generation") or "")
            != current_runtime_generation
        ):
            return "runtime generation is stale"
        age = _manifest_age_seconds(manifest, "prepared_at")
        if age is None or age > prepared_seat_ttl_seconds():
            return "prepared seat outlived its idle lease"
        return None
    if state == "claimed":
        age = _manifest_age_seconds(manifest, "claimed_at")
        if age is None or age > CLAIMED_SLOT_ORPHAN_SECONDS:
            return "claimed manifest was never cleared by its Session"
        return None
    return f"unknown slot state {state!r}"


async def _claimed_session_adopted_placement(manifest: dict[str, Any]) -> bool:
    """Whether the claiming Session took ownership of the slot's placement.

    Decides which half of an orphaned claimed manifest is safe: a Session
    that adopted the placement recorded the slot's isolated session on its
    own row, and destroying the placement would kill its live conversation —
    only the manifest is debris then. A claim that died before adoption left
    no such record, and the placement itself is the leak.
    """

    session_id = str(manifest.get("claimed_session_id") or "").strip()
    if not session_id:
        return False
    from astrabox.persistence.repository.session_repository import (
        SessionRepository,
    )

    session = await SessionRepository().get_session(session_id)
    if not isinstance(session, dict):
        return False
    slot_iso = str(manifest.get("isolated_session_id") or "").strip()
    identity = session.get("runtime_identity")
    if isinstance(identity, dict) and (
        str(identity.get("isolated_session_id") or "").strip() == slot_iso
    ):
        return True
    allocation = session.get("startup_allocation")
    if isinstance(allocation, dict):
        recorded = allocation.get("isolated_session_ids")
        if isinstance(recorded, (list, tuple)) and slot_iso in {
            str(item or "").strip() for item in recorded
        }:
            return True
    return False


def prepared_seat_is_stale(manifest: dict[str, Any]) -> str | None:
    """Why a seat manifest should be reaped now, or None while it holds.

    The expiration watcher's question, asked with the reaper's own rule: a
    prepared seat past its idle lease, or a claimed manifest its Session never
    cleared. The manifest's own generation stands in for the current one
    because a generation change retires the old seats itself.
    """

    return _manifest_is_reapable(
        manifest,
        current_runtime_generation=str(manifest.get("runtime_generation") or ""),
    )


async def reap_stale_slot_manifests(
    template: Any,
    *,
    runtime_manager: Any = None,
    agent_repo: Any | None = None,
) -> list[str]:
    """Destroy every stale or orphaned slot manifest; return the reasons.

    Runs at every reconciliation of the Agent's prepared capacity, which the
    expiration watcher schedules when it sees a stale seat and an Agent config
    write schedules itself.
    """

    agent_id = str(getattr(template, "agent_id", "") or "").strip()
    generation = str(getattr(template, "runtime_generation", "") or "").strip()
    repo = agent_repo if agent_repo is not None else AgentRepository()
    reasons: list[str] = []
    for manifest in slot_manifests(await repo.get_agent(agent_id)):
        reason = _manifest_is_reapable(manifest, current_runtime_generation=generation)
        if reason is None:
            continue
        await _retire_manifest(
            agent_id, manifest, reason=reason, repo=repo, runtime_manager=runtime_manager
        )
        reasons.append(reason)
    return reasons


async def _retire_manifest(
    agent_id: str,
    manifest: dict[str, Any],
    *,
    reason: str,
    repo: Any,
    runtime_manager: Any = None,
) -> None:
    """Apply the adoption-aware retirement rules to one slot manifest."""

    adopted = await _claimed_session_adopted_placement(manifest)
    if str(manifest.get("state") or "") == "claimed" and adopted:
        # The Session owns the placement; the manifest alone is debris.
        await _change_slot(repo, agent_id, manifest, None)
        logger.info(
            "cleared adopted slot manifest: agent=%s slot=%s (%s)",
            agent_id,
            manifest.get("slot_id"),
            reason,
        )
        return

    # Own the manifest before touching the resource. A Session claims this same
    # document with a CAS; if it won after the retirement caller's read, this
    # transition loses and the Session's hand-off remains untouched. Keeping the
    # cleanup address in a ``retiring`` manifest also lets a later reaper retry
    # when the provider side effect fails or this worker exits midway through it.
    retiring = {
        **manifest,
        "state": "retiring",
        "retire_reason": reason,
        "retiring_at": _utcnow_iso(),
    }
    if manifest.get("state") != "retiring" and not await _change_slot(
        repo, agent_id, manifest, retiring
    ):
        logger.info(
            "prepared runtime retirement lost manifest ownership: agent=%s slot=%s (%s)",
            agent_id,
            manifest.get("slot_id"),
            reason,
        )
        return
    await discard_prepared_slot(
        agent_id,
        retiring if manifest.get("state") != "retiring" else manifest,
        reason=reason,
        agent_repo=repo,
        runtime_manager=runtime_manager,
    )


async def retire_prepared_runtime(
    agent_id: str,
    *,
    reason: str,
    agent_repo: Any | None = None,
    runtime_manager: Any = None,
) -> None:
    """Retire recorded capacity without resolving configuration for a new runtime."""

    if not agent_id:
        return
    repo = agent_repo if agent_repo is not None else AgentRepository()
    for manifest in slot_manifests(await repo.get_agent(agent_id)):
        await _retire_unless_handing_off(
            agent_id, manifest, reason=reason, repo=repo, runtime_manager=runtime_manager
        )


async def _retire_unless_handing_off(
    agent_id: str,
    manifest: dict[str, Any],
    *,
    reason: str,
    repo: Any,
    runtime_manager: Any = None,
) -> None:
    if str(manifest.get("state") or "") == "claimed":
        age = _manifest_age_seconds(manifest, "claimed_at")
        if age is not None and age <= CLAIMED_SLOT_ORPHAN_SECONDS:
            # The claim CAS is the ownership hand-off. Until its orphan fence
            # expires, neither an Agent generation change nor disabling prewarm
            # may interpret the Session's not-yet-published allocation as an
            # unowned unit. Normal claim cleanup clears it; the stale-manifest
            # reaper applies adoption evidence after the bounded fence.
            logger.info(
                "retaining claimed runtime during Session hand-off: "
                "agent=%s slot=%s session=%s age_s=%d (%s)",
                agent_id,
                manifest.get("slot_id"),
                manifest.get("claimed_session_id"),
                max(0, round(age)),
                reason,
            )
            return
    await _retire_manifest(
        agent_id, manifest, reason=reason, repo=repo, runtime_manager=runtime_manager
    )


def _template_skills(template: Any) -> list[str]:
    skills = getattr(template, "skills", None)
    if skills is None and isinstance(template, dict):
        skills = template.get("skills")
    return [str(item).strip() for item in (skills or []) if str(item).strip()]


#: Where a pool box keeps the seats prepared in it until an Agent takes it. The
#: receipt belongs to the live box: destroying the box invalidates it.
POOL_SEATS_RECEIPT = f"{SANDBOX_IMAGE_WORKLOAD_HOME}/.astrabox-prepared-seats.json"

#: Agent-row field holding how many seats the Agent's latest pool box held,
#: which sizes the pool for the Agent's prepared capacity.
SEATS_PER_BOX_FIELD = "_seats_per_box"


def prepared_seat_ttl_seconds() -> int:
    """How long a prepared seat stays claimable after it was prepared.

    The pool discards an idle box once its idle lease ends, and a seat taken out
    of the pool with its box keeps the same bound, so a prepared unit of either
    tenancy waits at most one lease for its conversation.
    """

    return int(load_astrabox_settings().sandbox_lease_seconds)


async def prepare_pool_seats(
    handle: Any,
    *,
    template: Any,
    runtime_manager: Any,
    agent_repo: Any | None = None,
) -> list[dict[str, Any]]:
    """Fill one pool box with as many prepared seats as its memory holds.

    Runs in the client pool's preparer, before the box is published, exactly
    where a conversation box prepares its engine: the pool replenishes, elects
    the replica that does it and expires idle boxes, and a box arrives at its
    Agent already carrying its seats. Each seat reserves the growth of the
    conversation it is waiting for, so the box only opens a seat while the
    seats before it leave room for another conversation. The count is written
    on the Agent row, where it sizes the pool for the Agent's capacity.
    """

    agent_id = str(getattr(template, "agent_id", "") or "").strip()
    engine_kind = str(getattr(template, "engine_kind", "") or "").strip()
    generation = str(getattr(template, "runtime_generation", "") or "").strip()
    if not agent_id or not engine_kind or not generation:
        raise APIError(
            code="AGENT_PREWARM_CONFIG_INVALID",
            message="seat preparation requires an Agent id, engine kind and runtime generation",
            status_code=500,
        )
    repo = agent_repo if agent_repo is not None else AgentRepository()
    provider = sandbox_for_template(template)
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)
    sandbox_id = extract_sandbox_id(handle)
    seats: list[dict[str, Any]] = []
    # A claim checks the box with the seat's own engine already running in
    # it, so a seat fits only while the box keeps a conversation's reserve
    # once that engine is up. The engine the previous seat started stands in
    # for the next one's.
    footprint = 0
    try:
        reading = await lease.read_memory(sandbox_id)
        while reading is not None and room_for_one_more(
            sandbox_id, reading[0], reading[1] + footprint, pending=len(seats)
        ):
            seats.append(
                await _prepare_seat(
                    handle,
                    template=template,
                    runtime_manager=runtime_manager,
                    provider=provider,
                    lease=lease,
                    agent_id=agent_id,
                    engine_kind=engine_kind,
                    generation=generation,
                )
            )
            after = await lease.read_memory(sandbox_id)
            if after is not None:
                footprint = max(0, after[1] - reading[1])
            reading = after
        if not seats:
            raise APIError(
                code="SANDBOX_CAPACITY_UNAVAILABLE",
                message=(
                    f"pool box {sandbox_id!r} has no room for one conversation of "
                    f"Agent {agent_id!r}"
                ),
                status_code=503,
            )
        await handle.files.write_file(
            POOL_SEATS_RECEIPT,
            json.dumps(seats).encode(),
            mode=600,
            owner="root",
            group="root",
        )
    except BaseException:
        for seat in seats:
            with _suppress_and_log("pool seat binding delete", str(seat.get("slot_id"))):
                await _delete_prepared_platform_mcp_binding(str(seat.get("slot_id") or ""))
        raise
    row = await repo.get_agent(agent_id)
    if isinstance(row, dict) and row.get(SEATS_PER_BOX_FIELD) != len(seats):
        await repo.update_agent(agent_id, {SEATS_PER_BOX_FIELD: len(seats)})
        # The pool was sized for the previous count; resize it.
        runtime_manager.schedule_agent_runtime_reconciliation(agent_id)
    logger.info(
        "pool box prepared with seats: agent=%s box=%s seats=%d",
        agent_id,
        sandbox_id,
        len(seats),
    )
    return seats


async def _prepare_seat(
    handle: Any,
    *,
    template: Any,
    runtime_manager: Any,
    provider: Any,
    lease: Any,
    agent_id: str,
    engine_kind: str,
    generation: str,
) -> dict[str, Any]:
    """Open one seat in a pool box and prepare its engine; undo it on failure."""

    slot_id = f"slot-{uuid.uuid4().hex}"
    identity = build_conversation_identity(
        session_id=slot_id,
        agent_id=agent_id,
        template=template,
    )
    binding = await lease.open_unclaimed_slot(
        agent_id=agent_id,
        sandbox_id=extract_sandbox_id(handle),
        home_dir=str(identity.get("home_dir") or ""),
        workspace_dir=str(identity.get("workspace_dir") or ""),
        workspace_source_dir=str(identity.get("workspace_source_dir") or ""),
    )

    try:
        identity = {
            **identity,
            "sandbox_id": binding.sandbox_id,
            "uid": int(binding.uid),
            "gid": int(binding.gid),
            "isolated_session_id": binding.isolated_session_id,
            "terminal_isolated_session_id": binding.terminal_isolated_session_id,
        }
        adapter = get_engine_adapter(engine_kind)
        underlying = get_underlying_sandbox(handle)
        model_access = runtime_manager.resolve_model_access(
            getattr(template, "model_config", None) or {}
        )
        request = adapter.sandbox_request(
            template=template,
            model_access=model_access,
        )
        (
            model_credential,
            runtime_env,
            gateway_substitution,
            environment_contract,
        ) = (
            await _compose_gateway_vault(
                template,
                backend_provider=provider,
                credential_request=request.credential,
                slot_id=slot_id,
                sandbox_id=binding.sandbox_id,
                required_network_hosts=tuple(request.required_network_hosts),
            )
        )
        identity = await _bootstrap_slot_identity(
            handle,
            template,
            slot_id,
            identity=identity,
        )
        from astrabox.core.service.orchestrator.engine.platform_mcp import request_with_platform_mcp
        from astrabox.core.service.orchestrator.runtime.mcp_servers import prepared_slot_mcp_deployment_id

        request = request_with_platform_mcp(
            request, template=template, deployment_settings=runtime_manager.deployment_settings,
            deployment_id=prepared_slot_mcp_deployment_id(slot_id),
        )
        await _publish_prepared_platform_mcp_binding(
            template, agent_id=agent_id, slot_id=slot_id,
            sandbox_id=binding.sandbox_id, sandbox_backend=str(provider.name),
        )
        # The engine environment lands before either the service or engine
        # starts. Its protected values are placeholders whose complete Vault
        # plan was accepted before the ledger named this slot.
        await write_engine_env_file(
            underlying,
            request=request,
            credential=model_credential,
            cwd=str(identity.get("workspace_dir") or ""),
            runtime_identity=identity,
            owner_label=f"slot {slot_id!r}",
            additional_env=runtime_env,
        )
        if _session_log_declaration(template) is not None:
            await transcript_mirror.mark_mirror_unclaimed(
                underlying,
                unclaimed_for=timedelta(
                    seconds=prepared_seat_ttl_seconds() + 300
                ),
                target_file=(
                    str(identity.get("home_dir") or "").rstrip("/")
                    + "/.astrabox-mirror-target"
                ),
                owner=str(identity.get("linux_user") or "") or None,
            )
        runner_port = int(
            await lease.start_runner(
                binding,
                launch=adapter.shared_conversation_service_launch(
                    home=binding.home_dir,
                    workspace=binding.workspace_dir,
                    port=runner_port_for_uid(binding.uid),
                ),
                engine_label=engine_kind,
            )
        )
        runner_uri = await _runner_uri_for(handle, runner_port)
        if gateway_substitution:
            # The adapter reads this to spawn the child with the slot's own
            # placeholder; without it the child keeps the shared placeholder
            # and its calls attribute to the box identity.
            identity = {**identity, "gateway_substitution": True}
        from astrabox.core.service.orchestrator.agent.runtime_preparation import (
            prepare_engine_runtime,
        )

        manifest = await prepare_engine_runtime(
            EnginePreparationContext(
                template=template,
                slot_id=slot_id,
                placement="shared_slot",
                sandbox=underlying,
                sandbox_id=binding.sandbox_id,
                cwd=str(identity.get("workspace_dir") or ""),
                runtime_identity=identity,
                model_access=model_access,
                model_credential=model_credential,
                runtime_env=runtime_env,
                service_credential=engine_service_credential(request, identity),
                runner_uri=runner_uri,
                preparation_fingerprint=generation,
                deployment_settings=runtime_manager.deployment_settings,
                workspace_id=None,
                gateway_substitution=gateway_substitution,
            ),
            sandbox_backend=str(provider.name),
            environment_contract=environment_contract,
            runner_port=runner_port,
        )
        manifest.update({
            "isolated_session_id": binding.isolated_session_id,
            "terminal_isolated_session_id": binding.terminal_isolated_session_id,
            "uid": int(binding.uid),
            "gid": int(binding.gid),
            "home_dir": binding.home_dir,
            "workspace_dir": binding.workspace_dir,
            "workspace_source_dir": binding.workspace_source_dir,
        })
        logger.info(
            "prepared seat: agent=%s slot=%s box=%s runner_port=%d",
            agent_id,
            slot_id,
            binding.sandbox_id,
            runner_port,
        )
        return manifest
    except BaseException:
        with _suppress_and_log("slot binding delete", slot_id):
            await _delete_prepared_platform_mcp_binding(slot_id)
        with _suppress_and_log("slot placement discard", slot_id):
            await lease.release(binding)
        raise


class _SeatClaims:
    """The Sessions of one Agent generation in this process waiting for a seat."""

    def __init__(self) -> None:
        self.waiting: list[tuple[str, asyncio.Future[dict[str, Any] | None]]] = []
        self.serving: asyncio.Task[None] | None = None


_SEAT_CLAIMS: dict[tuple[str, str], _SeatClaims] = {}


async def claim_prepared_seat(
    template: Any,
    *,
    session_id: str,
    runtime_manager: Any,
    agent_repo: Any | None = None,
) -> dict[str, Any] | None:
    """Claim a prepared seat for one Session, together with concurrent claims.

    Concurrent claims of one Agent in this process are served together: one
    write hands each its own seat, and when seats run short, as many boxes as
    the shortfall needs are taken from the Agent's client pool at once and
    their seats handed out in turn. A claimer is never left cold while seats
    taken for it are on their way; a pool with nothing published is the
    defined miss, and the Session starts cold.
    """

    agent_id = str(getattr(template, "agent_id", "") or "").strip()
    generation = str(getattr(template, "runtime_generation", "") or "").strip()
    target_session = str(session_id or "").strip()
    if not _claimable(agent_id, generation, [target_session]):
        return None
    repo = agent_repo if agent_repo is not None else AgentRepository()
    key = (agent_id, generation)
    claims = _SEAT_CLAIMS.setdefault(key, _SeatClaims())
    seat: asyncio.Future[dict[str, Any] | None] = asyncio.get_running_loop().create_future()
    claims.waiting.append((target_session, seat))
    if claims.serving is None or claims.serving.done():
        claims.serving = asyncio.create_task(
            _serve_seat_claims(
                key, claims, template, runtime_manager=runtime_manager, repo=repo
            )
        )
    return await seat


async def _serve_seat_claims(
    key: tuple[str, str],
    claims: _SeatClaims,
    template: Any,
    *,
    runtime_manager: Any,
    repo: Any,
) -> None:
    """Serve every waiting claim of one Agent generation until none is left.

    A claimer cancelled after its seat was written leaves that seat claimed;
    the orphan fence collects it like any startup that died after its claim.
    """

    agent_id, generation = key
    batch: list[tuple[str, asyncio.Future[dict[str, Any] | None]]] = []
    try:
        while claims.waiting:
            batch = [(session, seat) for session, seat in claims.waiting if not seat.done()]
            claims.waiting.clear()
            if not batch:
                continue
            try:
                assigned = await claim_prepared_slots(
                    agent_id=agent_id,
                    session_ids=[session for session, _ in batch],
                    expected_runtime_generation=generation,
                    agent_repo=repo,
                )
            except Exception as exc:
                for _, seat in batch:
                    if not seat.done():
                        seat.set_exception(exc)
                continue
            short: list[tuple[str, asyncio.Future[dict[str, Any] | None]]] = []
            for session, seat in batch:
                if seat.done():
                    continue
                if session in assigned:
                    seat.set_result(assigned[session])
                else:
                    short.append((session, seat))
            if not short:
                continue
            seats_per_box = max(1, int(getattr(template, "seats_per_box", 0) or 0))
            outcomes = await asyncio.gather(
                *(
                    _adopt_pool_box(template, runtime_manager=runtime_manager, repo=repo)
                    for _ in range(-(-len(short) // seats_per_box))
                ),
                return_exceptions=True,
            )
            failures = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
            for failure in failures:
                logger.error(
                    "taking a pool box for waiting claims failed: agent=%s",
                    agent_id,
                    exc_info=failure,
                )
            if any(outcome is True for outcome in outcomes):
                claims.waiting[:0] = short
                continue
            for _, seat in short:
                if seat.done():
                    continue
                if failures:
                    seat.set_exception(failures[0])
                else:
                    seat.set_result(None)
        batch = []
    except BaseException:
        for _, seat in [*batch, *claims.waiting]:
            if not seat.done():
                seat.cancel()
        claims.waiting.clear()
        raise
    finally:
        if _SEAT_CLAIMS.get(key) is claims and not claims.waiting:
            del _SEAT_CLAIMS[key]


def _claimable(agent_id: str, generation: str, session_ids: list[str]) -> bool:
    """Whether a claim names an Agent, a Session and a generation to match.

    An Agent row with no runtime generation while something asks to claim
    against it is a contradiction: nothing republishes it on its own, so the
    Agent is cold from here until an edit reconciles it, and that is said.
    """

    if not agent_id or not any(session_ids):
        return False
    if not generation:
        logger.warning(
            "prepared slot claim skipped: agent=%s sessions=%s reason=%s",
            agent_id,
            ",".join(session_ids),
            "the Agent carries no runtime generation",
        )
        return False
    return True


async def _adopt_pool_box(template: Any, *, runtime_manager: Any, repo: Any) -> bool:
    """Take one box from the client pool and enter its seats in the Agent ledger.

    The box becomes the Agent's resident box when the Agent holds none, so the
    Agent's cold conversations can join it too. A box whose seats cannot be
    entered is destroyed rather than left owned by the Agent with nothing
    pointing at it.
    """

    from astrabox.core.service.orchestrator.agent.client_pool import (
        acquire_agent_client_pool,
    )

    agent_id = str(getattr(template, "agent_id", "") or "").strip()
    acquired = await acquire_agent_client_pool(
        template,
        runtime_manager=runtime_manager,
        session_id=None,
        assignment_id=f"seats-{uuid.uuid4().hex}",
    )
    if acquired is None:
        return False
    provider = sandbox_for_template(template)
    try:
        seats = json.loads(await acquired.sandbox.files.read_file(POOL_SEATS_RECEIPT))
        if not isinstance(seats, list) or not seats or any(
            not isinstance(seat, dict) or seat.get("sandbox_id") != acquired.sandbox_id
            for seat in seats
        ):
            raise APIError(
                code="AGENT_PREWARM_CONFIG_INVALID",
                message=(
                    f"pool box {acquired.sandbox_id!r} carries no seat receipt for itself"
                ),
                status_code=500,
            )
        for _attempt in range(_GATEWAY_LEDGER_CAS_ATTEMPTS):
            row = await repo.get_agent(agent_id)
            if not isinstance(row, dict):
                raise APIError(
                    code="AGENT_NOT_FOUND",
                    message=f"agent {agent_id!r} disappeared while taking a pool box",
                    status_code=404,
                )
            updates: dict[str, Any] = {
                PREPARED_SLOTS_FIELD: [*slot_manifests(row), *seats],
            }
            expected: dict[str, Any] = {PREPARED_SLOTS_FIELD: _observed_slots(row)}
            if not str(row.get("sandbox_id") or "").strip():
                updates.update({
                    "sandbox_id": acquired.sandbox_id,
                    "sandbox_backend": str(provider.name),
                    "_resident_sandbox_generation": str(
                        getattr(template, "sandbox_generation", "") or ""
                    ).strip() or None,
                })
                expected["sandbox_id"] = row.get("sandbox_id") if "sandbox_id" in row else {"$exists": False}
            if await repo.compare_and_update_agent(
                agent_id, expected=expected, updates=updates
            ):
                logger.info(
                    "took pool box with seats: agent=%s box=%s seats=%d",
                    agent_id,
                    acquired.sandbox_id,
                    len(seats),
                )
                return True
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=f"agent {agent_id!r} slots changed repeatedly while taking a pool box",
            status_code=503,
        )
    except BaseException:
        destruction = await provider.confirm_destroyed(acquired.sandbox_id)
        if not destruction.confirmed:
            logger.error(
                "pool box whose seats could not be entered survived: agent=%s box=%s: %s",
                agent_id,
                acquired.sandbox_id,
                destruction.detail,
            )
        raise


async def claim_prepared_slots(
    *,
    agent_id: str,
    session_ids: list[str],
    expected_runtime_generation: str,
    agent_repo: Any | None = None,
) -> dict[str, dict[str, Any]]:
    """Atomically give each Session its own prepared seat, as far as seats go.

    One compare-and-set on the seat list hands out every seat a call assigns,
    so the Sessions of one call never race each other's write; a Session left
    out gets no entry and takes the ordinary path. Seats go oldest first. A
    stale-generation seat is the reaper's to destroy, not the claimer's, and so
    is one past its idle lease. A seat whose box has no room left for one more
    conversation is withdrawn in the same write and the next one is tried:
    each box is read once per attempt, and every seat already assigned in it
    counts against it.

    Every miss says why. Going cold is a legitimate outcome of most of these
    (a first-ever claim has no seat, a concurrent claim already took it), but
    a prepared unit under a generation nothing reaped is a contradiction, and
    it is indistinguishable from the normal misses at the caller, which sees no
    seat and starts cold either way. Left unnamed, an Agent that has silently
    stopped being fast can only be diagnosed by reading the row out of the
    database by hand.
    """

    target_agent = str(agent_id or "").strip()
    targets = [session for session in (str(item or "").strip() for item in session_ids) if session]
    expected = str(expected_runtime_generation or "").strip()
    if not _claimable(target_agent, expected, targets):
        return {}
    repo = agent_repo if agent_repo is not None else AgentRepository()
    sessions = ",".join(targets)
    for _attempt in range(_GATEWAY_LEDGER_CAS_ATTEMPTS):
        row = await repo.get_agent(target_agent)
        if not isinstance(row, dict):
            return {}
        slots = slot_manifests(row)
        prepared = [manifest for manifest in slots if manifest.get("state") == "prepared"]
        current = [
            manifest
            for manifest in prepared
            if str(manifest.get("runtime_generation") or "") == expected
        ]
        if len(current) < len(prepared):
            logger.warning(
                "prepared slot claim skipped %d unit(s): agent=%s sessions=%s reason=%s "
                "expected=%s",
                len(prepared) - len(current),
                target_agent,
                sessions,
                "the prepared unit is a stale generation",
                expected[:16],
            )
        # An expired seat is the reaper's, never a Session's: the engine child
        # idled across the whole window and adopting it would gamble the
        # Session on an unproven process.
        fresh = sorted(
            (manifest for manifest in current if _seat_is_fresh(manifest)),
            key=lambda item: str(item.get("prepared_at") or ""),
        )
        if not fresh:
            # The ordinary miss: nothing prepared is left for this Agent, or
            # what is left has outlived its idle lease and is the reaper's.
            logger.info(
                "prepared slot claim missed: agent=%s sessions=%s reason=%s units=%d",
                target_agent,
                sessions,
                (
                    "prepared units outlived their idle lease"
                    if current
                    else "no prepared unit"
                ),
                len(slots),
            )
            return {}
        leases: dict[str, SharedSandboxLease] = {}
        boxes: list[tuple[str, str]] = []
        for manifest in fresh:
            backend = str(manifest.get("sandbox_backend") or "")
            if backend not in leases:
                leases[backend] = SharedSandboxLease(
                    agent_repo=repo, provider=sandbox_for_name(backend)
                )
            box = (backend, str(manifest.get("sandbox_id") or ""))
            if box not in boxes:
                boxes.append(box)
        readings = dict(zip(boxes, await asyncio.gather(
            *(leases[backend].read_memory(sandbox_id) for backend, sandbox_id in boxes)
        )))
        # The reads take time, and a seat that crossed its idle lease during
        # them is the reaper's: pass it over and leave it as it is.
        fresh = [manifest for manifest in fresh if _seat_is_fresh(manifest)]
        now = time.time()
        seats = list(slots)
        admissions = surviving_admissions(row)
        planned = {**row, PREPARED_SLOTS_FIELD: seats, BOX_ADMISSIONS: admissions}
        waiting = list(targets)
        assigned: dict[str, dict[str, Any]] = {}
        withdrawn = 0
        for manifest in fresh:
            if not waiting:
                break
            slot_id = str(manifest.get("slot_id") or "")
            backend = str(manifest.get("sandbox_backend") or "")
            sandbox_id = str(manifest.get("sandbox_id") or "")
            reading = readings[(backend, sandbox_id)]
            position = seats.index(manifest)
            if reading is None or not room_for_one_more(
                sandbox_id,
                *reading,
                pending=pending_reservations(planned, sandbox_id, excluding=slot_id, now=now),
            ):
                # Withdraw the promise; the reaper owns the cleanup and active
                # siblings keep their box.
                seats[position] = {
                    **manifest,
                    "state": "retiring",
                    "retire_reason": (
                        "shared sandbox no longer has room for the prepared conversation"
                    ),
                    "retiring_at": _utcnow_iso(),
                }
                withdrawn += 1
                continue
            session = waiting.pop(0)
            claimed = {
                **manifest,
                "state": "claimed",
                "claimed_session_id": session,
                "claimed_at": _utcnow_iso(),
            }
            seats[position] = claimed
            # A waiting process can be old while the conversation it receives
            # is new. Reserve its growth again, atomically with the hand-off,
            # and transfer the preparation's admission with the seat: a
            # separate write would leave the temporary owner protecting a
            # claimed placement.
            admissions[:] = [
                entry for entry in admissions
                if not (entry.get("sandbox_id") == sandbox_id and entry.get("session_id") == slot_id)
            ]
            admissions.append({"sandbox_id": sandbox_id, "session_id": session, "at": now})
            assigned[session] = claimed
        if not assigned and not withdrawn:
            return {}
        if await repo.compare_and_update_agent(
            target_agent,
            expected={
                PREPARED_SLOTS_FIELD: _observed_slots(row),
                BOX_ADMISSIONS: row.get(BOX_ADMISSIONS) if BOX_ADMISSIONS in row else {"$exists": False},
            },
            updates={PREPARED_SLOTS_FIELD: seats, BOX_ADMISSIONS: admissions},
        ):
            break
    else:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=(
                f"agent {target_agent!r} slots or admissions changed repeatedly "
                "during slot claim"
            ),
            status_code=503,
        )
    for session, claimed in assigned.items():
        # The name of the directory this box was given, written onto the
        # Session that just took it. Until this line the workspace belongs to
        # a disposable box; after it, it is the conversation's, and the box
        # built to replace this one mounts the same name instead of minting a
        # stranger.
        adopted_workspace = str(claimed.get(WORKSPACE_ID_FIELD) or "").strip()
        if adopted_workspace:
            from astrabox.persistence.repository.session_repository import (
                SessionRepository,
            )

            await SessionRepository().update_session(
                session, {WORKSPACE_ID_FIELD: adopted_workspace}
            )
        logger.info(
            "prepared slot claimed: agent=%s slot=%s session=%s workspace=%s",
            target_agent,
            claimed.get("slot_id"),
            session,
            adopted_workspace or "<none>",
        )
    return assigned


def _seat_is_fresh(manifest: dict[str, Any]) -> bool:
    age = _manifest_age_seconds(manifest, "prepared_at")
    return age is not None and age <= prepared_seat_ttl_seconds()


async def release_claimed_slot_allocation(
    session_id: str, claimed: dict[str, Any]
) -> None:
    """Sever the Session's reference to the discarded slot's box.

    Only when the Session still names that exact box: a placement that has
    already moved on owns whatever it recorded, and clearing a stranger's
    allocation would erase the last durable address of a live resource. The
    box itself is the discard's to dispose of; this drops the pointer that
    would otherwise refuse the cold start's own allocation.
    """

    from astrabox.persistence.repository.session_repository import (
        SessionRepository,
    )

    box = str(claimed.get("sandbox_id") or "").strip()
    if not box:
        return
    repository = SessionRepository()
    row = await repository.get_session_including_deleted(session_id)
    allocation = (row or {}).get("startup_allocation")
    if not isinstance(allocation, dict):
        return
    if str(allocation.get("sandbox_id") or "").strip() != box:
        return
    await repository.clear_startup_allocation(session_id, allocation=allocation)
    logger.info(
        "released the discarded slot's startup allocation: session=%s box=%s",
        session_id,
        box,
    )


async def clear_claimed_slot(
    *,
    agent_id: str,
    slot_id: str,
    agent_repo: Any | None = None,
) -> None:
    """Remove a claimed manifest once its Session owns the placement.

    The Session records the binding on its own row at activation; after that
    the manifest is bookkeeping debris.
    """

    repo = agent_repo if agent_repo is not None else AgentRepository()
    target = str(agent_id or "").strip()
    for manifest in slot_manifests(await repo.get_agent(target)):
        if str(manifest.get("slot_id") or "") == str(slot_id or "").strip():
            await _change_slot(repo, target, manifest, None)
            return


async def _publish_prepared_platform_mcp_binding(
    template: Any,
    *,
    agent_id: str,
    slot_id: str,
    sandbox_id: str,
    sandbox_backend: str,
) -> None:
    """Publish the slot address before an engine can connect its MCP client."""

    from astrabox.core.service.orchestrator.runtime.mcp_servers import (
        prepared_slot_mcp_deployment_id,
    )
    from astrabox.persistence.repository.platform_mcp_binding_repository import (
        PlatformMCPBindingRepository,
    )

    await PlatformMCPBindingRepository().upsert_binding(
        {
            "deployment_id": prepared_slot_mcp_deployment_id(slot_id),
            "scope_kind": "session",
            "template_name": agent_id,
            "sandbox_id": sandbox_id,
            "sandbox_backend": sandbox_backend,
            "slot_id": slot_id,
            "source": "prepared_slot",
        }
    )


async def bind_claimed_platform_mcp_binding(
    template: Any,
    claimed: dict[str, Any],
    *,
    session_id: str,
    user_id: str | None,
) -> None:
    """Bind a prepared slot's stable MCP address to its claiming Session."""

    from astrabox.core.service.orchestrator.runtime.mcp_servers import (
        prepared_slot_mcp_deployment_id,
    )
    from astrabox.persistence.repository.platform_mcp_binding_repository import (
        PlatformMCPBindingRepository,
    )

    await PlatformMCPBindingRepository().upsert_binding(
        {
            "deployment_id": prepared_slot_mcp_deployment_id(
                str(claimed.get("slot_id") or "")
            ),
            "session_id": str(session_id or "").strip(),
            "user_id": str(user_id or "").strip(),
        }
    )


async def _delete_prepared_platform_mcp_binding(slot_id: str) -> None:
    from astrabox.core.service.orchestrator.runtime.mcp_servers import (
        prepared_slot_mcp_deployment_id,
    )
    from astrabox.persistence.repository.platform_mcp_binding_repository import (
        PlatformMCPBindingRepository,
    )

    await PlatformMCPBindingRepository().delete_binding(
        prepared_slot_mcp_deployment_id(slot_id)
    )


async def discard_prepared_slot(
    agent_id: str,
    manifest: dict[str, Any],
    *,
    reason: str,
    agent_repo: Any | None = None,
    runtime_manager: Any = None,
) -> None:
    """Destroy an unclaimed slot's placement and remove its manifest.

    Closing the isolated sessions kills the runner and the prepared engine
    child with them (they live in the slot's PID namespace), so no separate
    process teardown is needed or attempted. A box that is not the
    Agent's resident box, and that this slot was the last occupant of, is
    destroyed too: nothing else would find it before its lease ran out.
    """

    from astrabox.core.service.orchestrator.runtime.shared_sandbox_lease import (
        SharedSandboxBinding,
        SharedSandboxLease,
    )

    repo = agent_repo if agent_repo is not None else AgentRepository()
    sandbox_backend = str(manifest.get("sandbox_backend") or "").strip()
    if not sandbox_backend:
        raise APIError(
            code="AGENT_PREWARM_CONFIG_INVALID",
            message="prepared slot manifest carries no sandbox backend",
            status_code=500,
        )
    provider = sandbox_for_name(sandbox_backend)
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)
    binding = SharedSandboxBinding(
        sandbox_id=str(manifest.get("sandbox_id") or ""),
        isolated_session_id=str(manifest.get("isolated_session_id") or ""),
        terminal_isolated_session_id=str(
            manifest.get("terminal_isolated_session_id") or ""
        ),
        uid=int(manifest.get("uid") or 0),
        gid=int(manifest.get("gid") or 0),
        home_dir=str(manifest.get("home_dir") or ""),
        workspace_dir=str(manifest.get("workspace_dir") or ""),
        workspace_source_dir=str(manifest.get("workspace_source_dir") or ""),
    )
    logger.info(
        "discarding prepared slot: agent=%s slot=%s reason=%s",
        agent_id,
        manifest.get("slot_id"),
        reason,
    )
    # Do not clear the manifest unless both isolated sessions were closed.
    # It is the durable fence that prevents the caller's cold fallback from
    # starting a second runtime while the claimed one may still be alive.
    await lease.release(binding)
    await release_box_admission(
        agent_repo=repo,
        agent_id=agent_id,
        sandbox_id=binding.sandbox_id,
        session_id=str(manifest.get("claimed_session_id") or manifest.get("slot_id") or ""),
    )
    with _suppress_and_log("slot binding delete", str(manifest.get("slot_id"))):
        await _delete_prepared_platform_mcp_binding(
            str(manifest.get("slot_id") or "")
        )
    await _change_slot(repo, agent_id, manifest, None)
    if runtime_manager is not None:
        await _dispose_vacated_box(
            repo,
            runtime_manager,
            agent_id=agent_id,
            sandbox_id=binding.sandbox_id,
            excluding=str(manifest.get("slot_id") or ""),
        )


async def _dispose_vacated_box(
    repo: Any,
    runtime_manager: Any,
    *,
    agent_id: str,
    sandbox_id: str,
    excluding: str,
) -> None:
    """Destroy a non-resident Agent box once nothing occupies it any more."""

    row = await repo.get_agent(agent_id)
    if not sandbox_id or not isinstance(row, dict):
        return
    if str(row.get("sandbox_id") or "").strip() == sandbox_id:
        # The resident box takes the next conversations and slots; the
        # resident-box reaper decides when it is abandoned.
        return
    if await runtime_manager.agent_box_has_other_occupants(
        sandbox_id, excluding=excluding, agent_id=agent_id
    ):
        return
    destruction = await runtime_manager.destroy_sandbox_by_id(sandbox_id)
    if destruction.confirmed:
        logger.info(
            "destroyed vacated Agent box: agent=%s box=%s", agent_id, sandbox_id
        )
    else:
        logger.warning(
            "vacated Agent box destruction unconfirmed: agent=%s box=%s: %s",
            agent_id,
            sandbox_id,
            destruction.detail,
        )


async def _published_manifest_slot(repo: Any, agent_id: str) -> set[str]:
    """The slots an unclaimed gateway entry may still belong to.

    An unclaimed entry survives pruning only while something names its slot, so
    every caller that prunes has to name the published ones. Dropping one leaves
    a later claim with no durable owner for the Session key it mints. Both
    callers read the published slots through here so their GC keep-sets cannot
    diverge.
    """

    return {
        str(manifest.get("slot_id") or "").strip()
        for manifest in slot_manifests(await repo.get_agent(str(agent_id or "").strip()))
        if str(manifest.get("slot_id") or "").strip()
    }


async def _prune_gateway_entries(
    repo: Any,
    agent_id: str,
    *,
    keep_slots: set[str],
) -> list[dict[str, Any]]:
    """Drop ledger entries whose Session ended (or whose slot is gone).

    A dropped claimed entry also garbage-collects its Session's gateway key.
    Unclaimed entries survive only for the slots the caller names — the
    current manifest slot and the one being prepared.
    """

    from astrabox.persistence.repository.session_repository import (
        SessionRepository,
    )
    from astrabox.seams.model import ModelRequestContext, model_endpoint_for_name

    for _attempt in range(_GATEWAY_LEDGER_CAS_ATTEMPTS):
        row = await repo.get_agent(agent_id)
        if not isinstance(row, dict):
            raise APIError(
                code="AGENT_NOT_FOUND",
                message=f"agent {agent_id!r} disappeared while pruning gateway keys",
                status_code=404,
            )
        field_present = GATEWAY_ENTRIES_FIELD in row
        raw = row.get(GATEWAY_ENTRIES_FIELD)
        entries = [dict(item) for item in (raw or []) if isinstance(item, dict)]
        kept: list[dict[str, Any]] = []
        released: list[tuple[str, str | None, str]] = []
        for entry in entries:
            session_id = str(entry.get("session_id") or "").strip()
            slot_id = str(entry.get("slot_id") or "").strip()
            if not slot_id:
                continue
            if session_id:
                session = await SessionRepository().get_session(session_id)
                state = str((session or {}).get("state") or "").strip()
                if session is not None and state not in _TERMINAL_SESSION_STATES:
                    kept.append(entry)
                    continue
                released.append(
                    (
                        session_id,
                        str(entry.get("endpoint_provider") or "").strip() or None,
                        slot_id,
                    )
                )
                continue
            if slot_id in keep_slots:
                kept.append(entry)
        if kept == entries:
            return kept
        expected = raw if field_present else {"$exists": False}
        if not await repo.compare_and_update_agent(
            agent_id,
            expected={GATEWAY_ENTRIES_FIELD: expected},
            updates={GATEWAY_ENTRIES_FIELD: kept},
        ):
            continue
        # A losing pruner must not delete a key whose ledger owner it failed to
        # remove. External cleanup begins only after this exact observed list
        # was replaced successfully.
        for session_id, endpoint_provider, slot_id in released:
            with _suppress_and_log("session gateway credential release", slot_id):
                await model_endpoint_for_name(
                    endpoint_provider
                ).release_session_credential(
                    context=ModelRequestContext(conversation_id=session_id)
                )
        return kept
    raise APIError(
        code="AGENT_RUNTIME_ERROR",
        message=(
            f"agent {agent_id!r} gateway-key ledger changed repeatedly while "
            "terminal entries were being pruned"
        ),
        status_code=503,
    )


async def gateway_ledger_entries(
    agent_id: str,
    *,
    agent_repo: Any | None = None,
) -> list[dict[str, Any]]:
    """The raw ledger rows: ``{"slot_id", "session_id"}`` per live slot."""

    target = str(agent_id or "").strip()
    if not target:
        return []
    repo = agent_repo if agent_repo is not None else AgentRepository()
    row = await repo.get_agent(target)
    raw = (row or {}).get(GATEWAY_ENTRIES_FIELD) if isinstance(row, dict) else None
    return [
        dict(entry)
        for entry in (raw or [])
        if isinstance(entry, dict) and str(entry.get("slot_id") or "").strip()
    ]


_GATEWAY_PRUNE_TASKS: set[asyncio.Task[Any]] = set()


def schedule_gateway_entry_prune(agent_id: str) -> None:
    """Prune the Agent's gateway-key ledger in the background.

    Uses the same keep-set as :func:`record_gateway_entry`: the published
    manifest slot, read fresh, plus every entry whose Session has not ended. A
    failed prune is logged; the next prune for the Agent collects what this one
    left.
    """

    target_agent = str(agent_id or "").strip()
    if not target_agent:
        return

    async def _prune() -> None:
        repo = AgentRepository()
        keep = await _published_manifest_slot(repo, target_agent)
        await _prune_gateway_entries(repo, target_agent, keep_slots=keep)

    task = asyncio.create_task(_prune(), name=f"gateway-entry-prune:{target_agent}")
    _GATEWAY_PRUNE_TASKS.add(task)

    def _done(completed: asyncio.Task[Any]) -> None:
        _GATEWAY_PRUNE_TASKS.discard(completed)
        if completed.cancelled():
            return
        error = completed.exception()
        if error is not None:
            logger.warning(
                "gateway-key ledger prune failed: agent=%s error=%s", target_agent, error
            )

    task.add_done_callback(_done)


async def record_gateway_entry(
    repo: Any,
    agent_id: str,
    *,
    slot_id: str,
    session_id: str,
) -> None:
    """Record which Session owns a claimed workload's gateway key.

    Every prepared unit, a conversation box or a seat, is prepared in a pool
    box before any Agent ledger names it, so its entry is written by the claim
    that gives it a Session. For a whole-box workload the entry is
    garbage-collection bookkeeping only — each box's binding lives in its own
    vault — but pruning is still what deletes a dead Session's gateway key.

    The claim is on that Session's startup path, and pruning costs a Session
    read and a gateway-key deletion per Session that ended since the last
    prune, so the claim appends its entry and leaves the prune to
    :func:`schedule_gateway_entry_prune`; its entry names a live Session, so
    the later prune keeps it.
    """

    target_agent = str(agent_id or "").strip()
    target_slot = str(slot_id or "").strip()
    target_session = str(session_id or "").strip() or None
    if not target_agent or not target_slot or target_session is None:
        return
    for _attempt in range(_GATEWAY_LEDGER_CAS_ATTEMPTS):
        row = await repo.get_agent(target_agent)
        if not isinstance(row, dict):
            raise APIError(
                code="AGENT_NOT_FOUND",
                message=f"agent {target_agent!r} disappeared while recording a gateway key",
                status_code=404,
            )
        field_present = GATEWAY_ENTRIES_FIELD in row
        raw = row.get(GATEWAY_ENTRIES_FIELD)
        entries = [dict(item) for item in (raw or []) if isinstance(item, dict)]
        existing = next(
            (
                entry
                for entry in entries
                if str(entry.get("slot_id") or "").strip() == target_slot
            ),
            None,
        )
        if existing is not None:
            current_session = str(existing.get("session_id") or "").strip() or None
            if target_session is None or current_session == target_session:
                return
            if current_session is not None:
                raise APIError(
                    code="AGENT_RUNTIME_ERROR",
                    message=(
                        f"gateway workload {target_slot!r} is already owned by "
                        f"Session {current_session!r}"
                    ),
                    status_code=409,
                )
            existing["session_id"] = target_session
            replacement = entries
        else:
            replacement = [
                *entries,
                {"slot_id": target_slot, "session_id": target_session},
            ]
        expected = raw if field_present else {"$exists": False}
        if await repo.compare_and_update_agent(
            target_agent,
            expected={GATEWAY_ENTRIES_FIELD: expected},
            updates={GATEWAY_ENTRIES_FIELD: replacement},
        ):
            return
    raise APIError(
        code="AGENT_RUNTIME_ERROR",
        message=(
            f"agent {target_agent!r} gateway-key ledger changed repeatedly while "
            f"slot {target_slot!r} was being recorded"
        ),
        status_code=503,
    )


async def claimed_gateway_credential(
    *,
    template: Any,
    session_id: str,
    user_id: str | None,
    shared_credential: str,
) -> str:
    """Resolve the credential a claimed slot spends at its model endpoint.

    Endpoint providers may add per-Session attribution by issuing a dedicated
    credential. ``None`` has the seam's declared meaning: this endpoint has no
    such identity, so the claim keeps the shared credential. It is not an
    authorization failure and must not disable prewarming for other providers.
    """

    model_config = getattr(template, "model_config", None) or {}
    endpoint_provider = (
        str(model_config.get("endpoint_provider") or "").strip() or None
        if isinstance(model_config, dict)
        else None
    )
    from astrabox.seams.model import ModelRequestContext, model_endpoint_for_name

    session_credential = await model_endpoint_for_name(
        endpoint_provider
    ).ensure_session_credential(
        context=ModelRequestContext(
            conversation_id=str(session_id or "").strip(),
            user_id=str(user_id or "").strip() or None,
        )
    )
    resolved = str(session_credential or shared_credential or "").strip()
    if not resolved:
        raise APIError(
            code="AGENT_PREWARM_CONFIG_INVALID",
            message="claimed slot has neither a Session nor shared model credential",
            status_code=500,
        )
    return resolved


async def _compose_gateway_vault(
    template: Any,
    *,
    backend_provider: Any,
    credential_request: Any,
    slot_id: str,
    sandbox_id: str,
    required_network_hosts: tuple[str, ...] = (),
) -> tuple[str, dict[str, str], bool, list[dict[str, Any]]]:
    """Install this new seat's credential plan in the shared box.

    The write contains model, MCP, and Agent environment credentials together.
    It names only this workload; the provider carries live siblings from its own
    revision-guarded Vault state.
    """

    from astrabox.core.service.orchestrator.runtime.mcp_credentials import (
        resolve_agent_mcp_credential_plan,
    )
    from astrabox.core.service.orchestrator.runtime.mcp_servers import (
        sandbox_mcp_egress_hosts,
    )
    from astrabox.core.service.orchestrator.runtime.packages import (
        package_egress_hosts,
    )

    vault_enabled = bool(
        getattr(load_astrabox_settings(), "sandbox_credential_vault_enabled", False)
    )
    gateway_plan = await resolve_agent_mcp_credential_plan(
        template=template,
        vault_enabled=vault_enabled,
    )
    credential, _network_policy, vault_write = resolve_model_credential_delivery(
        template=template,
        backend_adapter=backend_provider,
        credential=credential_request,
        additional_vault_write=gateway_plan,
        required_hosts=(
            *required_network_hosts,
            *tuple(package_egress_hosts(template)),
        ),
        mcp_hosts=tuple(
            sandbox_mcp_egress_hosts(getattr(template, "mcp_servers", None))
        ),
        slot_id=slot_id,
    )
    vault_write, runtime_env = await resolve_prepared_environment_credentials(
        template,
        slot_id=slot_id,
        vault_enabled=vault_enabled,
        vault_write=vault_write,
    )
    if vault_write is None:
        raise APIError(
            code="AGENT_PREWARM_UNSUPPORTED",
            message="prepared slot produced no protected credential plan",
            status_code=409,
        )
    handle = await backend_provider.connect(sandbox_id)
    await backend_provider.apply_credential_vault(handle, vault_write=vault_write)
    # A seat waits in a pool box no Agent ledger names yet, so its gateway
    # ledger entry is written by the claim that gives it a Session, exactly as
    # for a conversation box.
    return (
        credential,
        runtime_env,
        credential == workload_model_placeholder(slot_id),
        environment_credential_contract(vault_write),
    )


async def repoint_slot_gateway_credential(
    sandbox: Any,
    *,
    backend_provider: Any,
    credential_request: Any,
    template: Any,
    claimed: dict[str, Any],
    session_id: str,
    user_id: str | None,
) -> None:
    """Swap the slot credential for this Session's own gateway key, once.

    Before input can flow: a first call racing the sidecar's snapshot refresh
    (~0.8s measured) authenticates under the box's shared identity instead of
    failing — the slot credential was written with the shared key at prepare
    for exactly this window. One implementation for every engine's claim: the
    four adapters carried byte-identical copies of this block, which is how a
    fix to one of them would have missed the rest.

    ``credential_request`` is the engine's own model wire, because the request
    paths a placeholder must be substituted on are the engine's (dsh dials
    chat/completions, the claude runner dials v1). The write replaces one
    credential value inside the binding the vault already holds, so it never
    creates.
    """

    if not bool(claimed.get("gateway_substitution")):
        return

    slot_id = str(claimed.get("slot_id") or "")
    session_key = await claimed_gateway_credential(
        template=template,
        session_id=session_id,
        user_id=user_id,
        shared_credential=str(
            getattr(getattr(credential_request, "access", None), "credential", "") or ""
        ),
    )

    from astrabox.core.service.orchestrator.engine.provisioning import (
        model_credential_plan,
    )

    await backend_provider.apply_credential_vault(
        sandbox,
        vault_write=model_credential_plan(
            credential_request,
            substitutions=(
                ModelEgressCredentialSubstitution(
                    name=workload_credential_name(slot_id),
                    secret_value=session_key,
                    placeholder=workload_model_placeholder(slot_id),
                ),
            ),
        ),
        create_if_missing=False,
    )


async def _bootstrap_slot_identity(
    handle: Any,
    template: Any,
    slot_id: str,
    *,
    identity: dict[str, Any],
) -> dict[str, Any]:
    transport = sandbox_for_template(template).conversation_bootstrap_transport
    return await bootstrap_conversation_runtime_from_agent_cache(
        handle,
        template,
        slot_id,
        get_underlying_sandbox_fn=get_underlying_sandbox,
        runtime_identity=identity,
        skills=_template_skills(template),
        bootstrap_transport=transport,
    )


async def _runner_uri_for(handle: Any, port: int) -> str:
    """The runner's websocket URI, signed when the endpoint needs a header.

    Mirrors the executor's endpoint handling: RunnerLink carries one URI and
    no headers, so a Secure-Access endpoint is exchanged for its signed form.
    """

    endpoint = await handle.get_endpoint(port)
    if dict(getattr(endpoint, "headers", None) or {}):
        settings = load_astrabox_settings()
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=int(settings.sandbox_endpoint_url_ttl_seconds)
        )
        endpoint = await handle.get_signed_endpoint(port, expires_at=expires_at)
        if dict(getattr(endpoint, "headers", None) or {}):
            raise RuntimeError(
                "signed runner endpoint still requires routing headers the "
                f"runner link cannot carry (sandbox={handle.sandbox_id})"
            )
    base = str(endpoint.endpoint).rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :]
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :]
    raise RuntimeError(f"runner endpoint has no http(s) scheme: {base!r}")


class _suppress_and_log:
    """Suppress cleanup exceptions but leave operator evidence."""

    def __init__(self, action: str, slot_id: str | None) -> None:
        self._action = action
        self._slot_id = slot_id

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc is not None:
            logger.warning(
                "%s failed for slot %s: %s", self._action, self._slot_id, exc
            )
        return True
