"""Prepare, claim, and discard one engine slot inside an Agent's shared box.

A prepared slot is the complete unit a Session start otherwise assembles
serially on the claim path: one isolated placement (private owner, home and
workspace), the conversation bootstrap, a running runner, and an initialized
engine process held before input by the engine adapter's ``prepare`` seam.
The measured budget behind this split is recorded in
``docs/maintainers/claude-runtime-preparation.md``.

Ownership: the provider owns placement and the runner; the engine adapter owns
the vendor process; this module composes them and owns the slot manifest on
the Agent row. One slot per Agent — the manifest is a single embedded document
under ``_prepared_slot`` and every transition is a compare-and-set against the
whole current value, so a concurrent refill or claim has exactly one winner
and the loser's work is discarded, never adopted.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

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
    release_box_admission,
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

#: The Agent-row field holding the one slot manifest (or None).
PREPARED_SLOT_FIELD = "_prepared_slot"

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

#: Receipt keys this module reads itself. Everything else an adapter returns
#: is engine evidence and rides the manifest untouched.
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

#: Session states that release a ledger entry (and its gateway key).
_TERMINAL_SESSION_STATES = {"TERMINATED", "RECOVERY_REQUIRED", "DELETED"}

#: An unclaimed slot older than this is reaped and rebuilt rather than
#: claimed: its engine child has idled across the whole window, and preparing
#: a fresh one is the cheap proof of health.
PREPARED_SLOT_TTL_SECONDS = 30 * 60

#: A claimed manifest this old is an orphan: the claiming Session either
#: cleared it within its own start or died. Reaping it frees the placement and
#: unblocks the next refill; a Session that did adopt the slot recorded the
#: placement on its own row and is unaffected by the manifest's removal.
CLAIMED_SLOT_ORPHAN_SECONDS = 10 * 60

#: Starts still building children in an Agent's shared box in this process.
#: A refill waits for them so background spare capacity cannot stack one more
#: child into the box while a foreground Session burst is still settling.
_active_agent_starts: dict[str, int] = {}

#: A wedged foreground start must not leave an Agent permanently cold.
_REFILL_SETTLE_TIMEOUT_S = 120.0

#: The supplier pool starts asynchronously. A fresh Agent has no resident box
#: until one of its published members is acquired, so the initial refill polls
#: through the supplier's declared create-and-prepare window instead of silently
#: giving up before the SDK has had a chance to publish its first member.
_CLIENT_POOL_POLL_SECONDS = 0.5


@contextlib.contextmanager
def _counted_agent_start(agent_id: str) -> Any:
    """Count one platform runtime start until its engine startup settles."""

    target = str(agent_id or "").strip()
    _active_agent_starts[target] = _active_agent_starts.get(target, 0) + 1
    try:
        yield
    finally:
        remaining = _active_agent_starts.get(target, 1) - 1
        if remaining <= 0:
            _active_agent_starts.pop(target, None)
        else:
            _active_agent_starts[target] = remaining


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _manifest_age_seconds(manifest: dict[str, Any], key: str) -> float | None:
    raw = str(manifest.get(key) or "").strip()
    if not raw:
        return None
    try:
        stamped = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - stamped).total_seconds()


#: Lead time reserved for building a replacement before the current slot's TTL
#: expires. The current slot stays claimable while its replacement is prepared.
PREPARED_SLOT_RENEWAL_BUILD_SECONDS = 120

#: How often the sweep retries building a slot for an Agent that has none. A
#: build that keeps failing (pool empty, control plane down) is retried on
#: this cadence rather than on every tick.
PREPARED_SLOT_REBUILD_RETRY_SECONDS = 300


def prepared_slot_renewal_lead_seconds() -> int:
    """How far ahead of its TTL a prepared slot is rebuilt.

    One expiration-watcher interval plus a build: the sweep runs that often,
    and the replacement it starts takes that long, so the swap lands before
    the old slot could reach the TTL that makes it unclaimable. A 5s watcher
    with a lead of one interval let a pi slot cross its TTL mid-build.
    """

    settings = load_astrabox_settings()
    interval = max(1, int(getattr(settings, "expiration_watcher_interval_seconds", 0) or 300))
    return interval + PREPARED_SLOT_RENEWAL_BUILD_SECONDS


def _manifest_is_reapable(
    manifest: dict[str, Any],
    *,
    current_runtime_generation: str,
    renewal_lead_seconds: int = 0,
) -> str | None:
    """The reason this manifest should be destroyed, or None to leave it.

    An unparsable timestamp counts as expired: a manifest whose age cannot be
    established must not be trusted to hold a healthy process forever. With a
    renewal lead, a prepared slot that would expire within that lead is
    already the reaper's, so the rebuild lands before the expiry.
    """

    state = str(manifest.get("state") or "")
    if state == "prepared":
        if (
            str(manifest.get("runtime_generation") or "")
            != current_runtime_generation
        ):
            return "runtime generation is stale"
        age = _manifest_age_seconds(manifest, "prepared_at")
        if age is None or age > PREPARED_SLOT_TTL_SECONDS:
            return "prepared slot exceeded its TTL"
        if renewal_lead_seconds > 0 and age > PREPARED_SLOT_TTL_SECONDS - renewal_lead_seconds:
            return "prepared slot is due for renewal before its TTL"
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


def prepared_slot_is_due_for_renewal(manifest: dict[str, Any]) -> str | None:
    """Why a slot manifest should be rebuilt now, or None while it holds.

    The expiration watcher's question, asked with the same rule the refill
    applies, so the sweep and the refill never disagree about what is stale:
    a prepared slot due to expire before the sweep's next build lands, or a
    claimed manifest its Session never cleared. Only age is judged here; the
    manifest's own generation stands in for the current one, because a
    generation change already schedules its own refill.
    """

    return _manifest_is_reapable(
        manifest,
        current_runtime_generation=str(manifest.get("runtime_generation") or ""),
        renewal_lead_seconds=prepared_slot_renewal_lead_seconds(),
    )


async def reap_slot_manifest_if_stale(
    template: Any,
    *,
    agent_repo: Any | None = None,
) -> str | None:
    """Destroy a stale or orphaned slot manifest; return the reason, or None.

    Runs at every refill, so orphans are collected on the next Session
    activity or Agent config write rather than waiting for a dedicated timer.
    A slot that is merely due for renewal is not reaped here: the refill
    builds its replacement first and retires it afterwards, so a Session
    arriving meanwhile still has a slot to claim. Both placement shapes are
    reaped here — the state machine and timestamps are shared; only the
    adoption evidence and the destruction differ.
    """

    agent_id = str(getattr(template, "agent_id", "") or "").strip()
    generation = str(getattr(template, "runtime_generation", "") or "").strip()
    repo = agent_repo if agent_repo is not None else AgentRepository()
    row = await repo.get_agent(agent_id)
    manifest = (row or {}).get(PREPARED_SLOT_FIELD) if isinstance(row, dict) else None
    if not isinstance(manifest, dict):
        return None
    reason = _manifest_is_reapable(
        manifest,
        current_runtime_generation=generation,
    )
    if reason is None:
        return None
    await _retire_manifest(agent_id, manifest, reason=reason, repo=repo)
    return reason


async def _retire_manifest(
    agent_id: str,
    manifest: dict[str, Any],
    *,
    reason: str,
    repo: Any,
) -> None:
    """Apply the existing adoption-aware retirement rules to one manifest."""

    from astrabox.core.service.orchestrator.agent import prepared_boxes

    is_box = (
        str(manifest.get("placement") or "")
        == prepared_boxes.PLACEMENT_CONVERSATION_BOX
    )
    adopted = (
        await prepared_boxes.claimed_session_adopted_box(manifest)
        if is_box
        else await _claimed_session_adopted_placement(manifest)
    )
    if str(manifest.get("state") or "") == "claimed" and adopted:
        # The Session owns the placement; the manifest alone is debris.
        await repo.compare_and_update_agent(
            agent_id,
            expected={PREPARED_SLOT_FIELD: manifest},
            updates={PREPARED_SLOT_FIELD: None},
        )
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
    won = await repo.compare_and_update_agent(
        agent_id,
        expected={PREPARED_SLOT_FIELD: manifest},
        updates={PREPARED_SLOT_FIELD: retiring},
    )
    if not won:
        logger.info(
            "prepared runtime retirement lost manifest ownership: agent=%s slot=%s (%s)",
            agent_id,
            manifest.get("slot_id"),
            reason,
        )
        return
    if is_box:
        await prepared_boxes.discard_prepared_box(
            agent_id, retiring, reason=reason, agent_repo=repo
        )
        return
    await discard_prepared_slot(
        agent_id, retiring, reason=reason, agent_repo=repo
    )


async def retire_prepared_runtime(
    agent_id: str,
    *,
    reason: str,
    agent_repo: Any | None = None,
) -> None:
    """Retire recorded capacity without resolving configuration for a new runtime."""

    if not agent_id:
        return
    repo = agent_repo if agent_repo is not None else AgentRepository()
    row = await repo.get_agent(agent_id)
    manifest = (row or {}).get(PREPARED_SLOT_FIELD) if isinstance(row, dict) else None
    if not isinstance(manifest, dict):
        return
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
    await _retire_manifest(agent_id, manifest, reason=reason, repo=repo)


def _template_skills(template: Any) -> list[str]:
    skills = getattr(template, "skills", None)
    if skills is None and isinstance(template, dict):
        skills = template.get("skills")
    return [str(item).strip() for item in (skills or []) if str(item).strip()]


async def ensure_manifest_field_decidable(
    repo: Any, agent_id: str, row: dict[str, Any]
) -> None:
    """Write the explicit None a publish CAS can match against.

    The publish CAS matches the field's current value exactly, and a
    document-store filter on None does not match an absent key. Concurrent
    refills that both pass this write still race the CAS, not this write.
    Shared by both placement allocators so their one-winner guarantee rests
    on the same store behaviour.
    """

    if PREPARED_SLOT_FIELD not in row:
        await repo.update_agent(agent_id, {PREPARED_SLOT_FIELD: None})


async def _place_initial_shared_slot(
    template: Any,
    *,
    runtime_manager: Any,
    repo: Any,
    provider: Any,
    lease: Any,
    identity: dict[str, Any],
    slot_id: str,
    generation: str,
) -> tuple[Any, Any | None]:
    """Place a new slot in the resident box or one claimed from the SDK pool."""

    async def confirm_candidate_destroyed(candidate_id: str) -> None:
        try:
            destruction = await provider.confirm_destroyed(candidate_id)
        except Exception as exc:
            raise APIError(
                code="SANDBOX_CLEANUP_UNCONFIRMED",
                message=(
                    f"unused Agent-box candidate {candidate_id!r} could not "
                    f"be destroyed: {type(exc).__name__}: {str(exc).strip()}"
                ),
                status_code=502,
                data={"leaked_sandbox_id": candidate_id},
            ) from exc
        if not destruction.confirmed:
            raise APIError(
                code="SANDBOX_CLEANUP_UNCONFIRMED",
                message=(
                    f"unused Agent-box candidate {candidate_id!r} could not "
                    f"be destroyed: {destruction.detail}"
                ),
                status_code=502,
                data={"leaked_sandbox_id": candidate_id},
            )

    async def place(candidate: str | None = None) -> Any:
        return await lease.place_in_agent_box(
            agent_id=str(getattr(template, "agent_id", "") or "").strip(),
            home_dir=str(identity.get("home_dir") or ""),
            workspace_dir=str(identity.get("workspace_dir") or ""),
            workspace_source_dir=str(identity.get("workspace_source_dir") or ""),
            candidate=candidate,
            expected_runtime_generation=generation,
            expected_sandbox_generation=str(
                getattr(template, "sandbox_generation", "") or ""
            ).strip() or None,
            session_id=slot_id,
        )

    binding = await place()
    if binding is not None:
        return binding, None

    from astrabox.core.service.orchestrator.agent.client_pool import (
        acquire_agent_client_pool,
        agent_client_pool_first_member_timeout_seconds,
    )

    agent_id = str(getattr(template, "agent_id", "") or "").strip()
    first_member_timeout = agent_client_pool_first_member_timeout_seconds()
    deadline = time.monotonic() + first_member_timeout
    while True:
        candidate = await acquire_agent_client_pool(
            template,
            runtime_manager=runtime_manager,
            session_id=None,
            assignment_id=slot_id,
        )
        if candidate is None:
            if time.monotonic() >= deadline:
                raise APIError(
                    code="SANDBOX_CLIENT_POOL_UNAVAILABLE",
                    message=(
                        f"the client pool for Agent {agent_id!r} published no "
                        f"box within {first_member_timeout:g} seconds; "
                        "a complete prepared runtime could not be built"
                    ),
                    status_code=503,
                )
            await asyncio.sleep(_CLIENT_POOL_POLL_SECONDS)
            # A foreground Session or another replica may have claimed the
            # supplier member while this task waited. Prefer that now-resident
            # box before asking the pool for its replacement.
            binding = await place()
            if binding is not None:
                return binding, None
            continue

        candidate_handle = candidate.sandbox
        candidate_id = candidate.sandbox_id
        try:
            binding = await place(candidate_id)
        except BaseException:
            try:
                current = await repo.get_agent(agent_id)
            except Exception as exc:
                raise APIError(
                    code="SANDBOX_CLEANUP_UNCONFIRMED",
                    message=(
                        f"could not determine whether failed prepared-slot "
                        f"candidate {candidate_id!r} was published: "
                        f"{type(exc).__name__}: {str(exc).strip()}"
                    ),
                    status_code=502,
                    data={"leaked_sandbox_id": candidate_id},
                ) from exc
            if str((current or {}).get("sandbox_id") or "").strip() != candidate_id:
                await confirm_candidate_destroyed(candidate_id)
            raise

        if binding is None:
            await confirm_candidate_destroyed(candidate_id)
            raise APIError(
                code="SANDBOX_CAPACITY_UNAVAILABLE",
                message=(
                    f"the prepared client-pool box {candidate_id!r} could not "
                    f"host Agent {agent_id!r}"
                ),
                status_code=503,
            )

        if str(binding.sandbox_id) != candidate_id:
            try:
                await confirm_candidate_destroyed(candidate_id)
            except BaseException:
                await lease.release(binding)
                raise
            return binding, None
        return binding, candidate_handle


async def prepare_slot_for_agent(
    template: Any,
    *,
    runtime_manager: Any,
    agent_repo: Any | None = None,
) -> dict[str, Any] | None:
    """Ensure the Agent holds one unclaimed prepared slot; return its manifest.

    Level-based: an existing unclaimed manifest whose runtime generation
    still matches is returned as-is. For Agent tenancy, a missing resident box
    is acquired from the official client pool before the slot is assembled;
    engines without an input-free preparation seam still return None.

    Everything created here is discarded on any failure or on losing the
    manifest CAS; a slot is adopted only through :func:`claim_prepared_slot`.
    """

    from astrabox.core.service.orchestrator.runtime.runtime_profile import (
        resolve_sandbox_tenancy,
    )
    from astrabox.core.service.orchestrator.runtime.shared_sandbox_lease import (
        SharedSandboxLease,
    )
    from astrabox.seams.sandbox import SANDBOX_TENANCY_CONVERSATION

    agent_id = str(getattr(template, "agent_id", "") or "").strip()
    engine_kind = str(getattr(template, "engine_kind", "") or "").strip()
    generation = str(getattr(template, "runtime_generation", "") or "").strip()
    if not agent_id or not engine_kind:
        raise APIError(
            code="AGENT_PREWARM_CONFIG_INVALID",
            message="slot preparation requires an Agent id and engine kind",
            status_code=500,
        )
    if not generation:
        # No published runtime generation means the Agent is not prewarmed;
        # there is nothing to key a slot to.
        return None
    if resolve_sandbox_tenancy(template) == SANDBOX_TENANCY_CONVERSATION:
        # The official client pool owns conversation-tenancy physical
        # inventory. This manifest is only for an engine process parked inside
        # an Agent-owned shared box; creating a whole box here would be a second
        # pool for the same product capability.
        return None
    adapter = get_engine_adapter(engine_kind)
    from astrabox.core.service.orchestrator.engine.base import EngineAdapter

    if (
        getattr(type(adapter), "prepare_runtime", None)
        is EngineAdapter.prepare_runtime
    ):
        # The adapter inherits the refusing default: this engine's vendor
        # lifecycle has no input-free preparation seam. A declared absence,
        # checked before any placement is built for it.
        return None

    repo = agent_repo if agent_repo is not None else AgentRepository()
    reaped = await reap_slot_manifest_if_stale(template, agent_repo=repo)
    if reaped:
        logger.info(
            "reaped slot manifest before refill: agent=%s (%s)",
            agent_id,
            reaped,
        )
    row = await repo.get_agent(agent_id)
    if not isinstance(row, dict):
        return None
    existing = row.get(PREPARED_SLOT_FIELD)
    replacing: dict[str, Any] | None = None
    if isinstance(existing, dict):
        if (
            str(existing.get("state") or "") == "prepared"
            and str(existing.get("runtime_generation") or "") == generation
        ):
            due = _manifest_is_reapable(
                existing,
                current_runtime_generation=generation,
                renewal_lead_seconds=prepared_slot_renewal_lead_seconds(),
            )
            if due is None:
                return existing
            # Due to expire before the next sweep. The slot stays published
            # — and claimable — while its replacement is built; the publish
            # below swaps the manifest, and only then is this one retired.
            # Retiring first left every Agent without a slot for the length
            # of a build, which is exactly the window a Session walks into.
            replacing = existing
        else:
            # A live claimed manifest belongs to its Session; the reap above
            # already destroyed anything stale. This refill has nothing to add.
            return None
    else:
        await ensure_manifest_field_decidable(repo, agent_id, row)

    provider = sandbox_for_template(template)
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)

    slot_id = f"slot-{uuid.uuid4().hex}"
    activation_token = secrets.token_hex(32)
    identity = build_conversation_identity(
        session_id=slot_id,
        agent_id=agent_id,
        template=template,
    )
    binding, pooled_handle = await _place_initial_shared_slot(
        template,
        runtime_manager=runtime_manager,
        repo=repo,
        provider=provider,
        lease=lease,
        identity=identity,
        slot_id=slot_id,
        generation=generation,
    )

    try:
        handle = (
            pooled_handle
            if pooled_handle is not None
            else await provider.connect(binding.sandbox_id)
        )
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
                repo=repo,
                agent_id=agent_id,
                slot_id=slot_id,
                sandbox_id=binding.sandbox_id,
                required_network_hosts=tuple(request.required_network_hosts),
            )
        )
        # Downloads use the same outbound credentials as the prepared runtime.
        # Install the plan before either cache can make its first Git request.
        await asyncio.gather(
            prepare_agent_runtime_plugin_cache(
                handle,
                template,
                get_underlying_sandbox_fn=get_underlying_sandbox,
            ),
            prepare_agent_runtime_skill_cache(
                get_underlying_sandbox(handle),
                _template_skills(template),
            ),
        )
        identity = await _bootstrap_slot_identity(
            handle,
            template,
            slot_id,
            identity=identity,
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
                    seconds=PREPARED_SLOT_TTL_SECONDS + 300
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
        await _publish_prepared_platform_mcp_binding(
            template,
            agent_id=agent_id,
            slot_id=slot_id,
            sandbox_id=binding.sandbox_id,
            sandbox_backend=str(provider.name),
        )
        from astrabox.core.service.orchestrator.agent.runtime_preparation import (
            prepare_engine_runtime,
        )

        manifest = await prepare_engine_runtime(
            EnginePreparationContext(
                template=template,
                slot_id=slot_id,
                activation_token=activation_token,
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
        await _publish_prepared_manifest(
            repo, agent_id, manifest, replacing=replacing
        )
        logger.info(
            "prepared slot published: agent=%s slot=%s box=%s runner_port=%d replaced=%s",
            agent_id,
            slot_id,
            binding.sandbox_id,
            runner_port,
            (replacing or {}).get("slot_id"),
        )
        if replacing is not None:
            # The manifest names the replacement. Discarding the old slot fails
            # its manifest CAS, preserving the replacement and releasing only
            # the old placement.
            with _suppress_and_log("replaced slot discard", str(replacing.get("slot_id"))):
                await discard_prepared_slot(
                    agent_id,
                    replacing,
                    reason="replaced by its renewal",
                    agent_repo=repo,
                )
        return manifest
    except BaseException:
        with _suppress_and_log("slot binding delete", slot_id):
            await _delete_prepared_platform_mcp_binding(slot_id)
        with _suppress_and_log("slot placement discard", slot_id):
            await lease.release(binding)
        raise


async def wait_for_agent_starts(agent_id: str) -> None:
    """Let foreground placements settle before reconciling spare capacity."""

    deadline = time.monotonic() + _REFILL_SETTLE_TIMEOUT_S
    while (
        _active_agent_starts.get(agent_id, 0) > 0
        and time.monotonic() < deadline
    ):
        await asyncio.sleep(0.5)


def schedule_prepared_runtime_refill(template: Any, runtime_manager: Any) -> None:
    """Notify the Agent reconciler that a Session consumed prepared capacity."""

    from astrabox.core.service.orchestrator.runtime.runtime_profile import (
        resolve_sandbox_tenancy,
    )
    from astrabox.seams.sandbox import (
        SANDBOX_TENANCY_CONVERSATION,
    )

    agent_id = str(getattr(template, "agent_id", "") or "").strip()
    generation = str(getattr(template, "runtime_generation", "") or "").strip()
    if (
        not agent_id
        or not generation
        or not bool(getattr(template, "prewarm_enabled", False))
    ):
        return
    if resolve_sandbox_tenancy(template) == SANDBOX_TENANCY_CONVERSATION:
        # SandboxPoolAsync replenishes its own whole-box inventory after an
        # acquire. Only Agent tenancy needs AstraBox to refill an engine child
        # inside the longer-lived shared box.
        return

    runtime_manager.schedule_agent_runtime_reconciliation(agent_id)


async def claim_prepared_slot(
    *,
    agent_id: str,
    session_id: str,
    expected_runtime_generation: str,
    agent_repo: Any | None = None,
) -> dict[str, Any] | None:
    """Atomically claim the Agent's prepared slot for one platform Session.

    Exactly one caller wins the CAS; everyone else gets None and takes the
    ordinary path. A generation mismatch also returns None — the slot is a
    stale generation and the reaper owns its destruction, not the claimer.

    Every miss says why. Going cold is a legitimate outcome of most of these
    (a first-ever claim has no manifest, a concurrent claim already took it),
    but two of them are contradictions — an Agent that wants prewarm carrying
    no runtime generation, or a prepared unit under a generation nothing reaped —
    and they are indistinguishable from the normal misses at the caller, which
    sees one None and starts cold either way. Left unnamed, an Agent that has
    silently stopped being fast can only be diagnosed by reading the row out
    of the database by hand.
    """

    target_agent = str(agent_id or "").strip()
    target_session = str(session_id or "").strip()
    expected = str(expected_runtime_generation or "").strip()
    if not target_agent or not target_session:
        return None
    if not expected:
        # The Agent's row carries no runtime generation while something asked to
        # claim against it. Nothing republishes it on its own, so this Agent
        # is cold from here until an edit reconciles it.
        logger.warning(
            "prepared slot claim skipped: agent=%s session=%s reason=%s",
            target_agent,
            target_session,
            "the Agent carries no runtime generation",
        )
        return None
    repo = agent_repo if agent_repo is not None else AgentRepository()
    row = await repo.get_agent(target_agent)
    if not isinstance(row, dict):
        return None
    manifest = row.get(PREPARED_SLOT_FIELD)
    if not isinstance(manifest, dict):
        # The ordinary miss: nothing has been prepared for this Agent yet.
        logger.info(
            "prepared slot claim missed: agent=%s session=%s reason=%s",
            target_agent,
            target_session,
            "no prepared unit",
        )
        return None
    state = str(manifest.get("state") or "")
    if state != "prepared":
        logger.info(
            "prepared slot claim missed: agent=%s session=%s reason=%s",
            target_agent,
            target_session,
            f"unit is {state!r}, not prepared",
        )
        return None
    manifest_generation = str(manifest.get("runtime_generation") or "")
    if manifest_generation != expected:
        logger.warning(
            "prepared slot claim skipped: agent=%s session=%s reason=%s "
            "manifest_generation=%s expected=%s",
            target_agent,
            target_session,
            "the prepared unit is a stale generation",
            manifest_generation[:16],
            expected[:16],
        )
        return None
    age = _manifest_age_seconds(manifest, "prepared_at")
    if age is None or age > PREPARED_SLOT_TTL_SECONDS:
        # An expired slot is the reaper's, never a Session's: the engine child
        # idled across the whole window and adopting it would gamble the
        # Session on an unproven process.
        logger.info(
            "prepared slot claim missed: agent=%s session=%s reason=%s age_s=%s",
            target_agent,
            target_session,
            "prepared unit exceeded its TTL",
            "unknown" if age is None else round(age),
        )
        return None
    claimed = {
        **manifest,
        "state": "claimed",
        "claimed_session_id": target_session,
        "claimed_at": _utcnow_iso(),
    }
    # Transfer the preparation's admission with the manifest; a separate write
    # would leave the temporary owner protecting an already claimed placement.
    for _attempt in range(_GATEWAY_LEDGER_CAS_ATTEMPTS):
        if not isinstance(row, dict) or row.get(PREPARED_SLOT_FIELD) != manifest:
            return None
        raw = row.get(BOX_ADMISSIONS)
        transferred = [
            {**entry, "session_id": target_session}
            if (
                isinstance(entry, dict)
                and entry.get("sandbox_id") == manifest.get("sandbox_id")
                and entry.get("session_id") == manifest.get("slot_id")
            ) else entry
            for entry in (raw or [])
        ]
        won = await repo.compare_and_update_agent(
            target_agent,
            expected={
                PREPARED_SLOT_FIELD: manifest,
                BOX_ADMISSIONS: raw if BOX_ADMISSIONS in row else {"$exists": False},
            },
            updates={PREPARED_SLOT_FIELD: claimed, BOX_ADMISSIONS: transferred},
        )
        if won:
            break
        row = await repo.get_agent(target_agent)
    else:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=f"agent {target_agent!r} admissions changed repeatedly during slot claim",
            status_code=503,
        )
    # The name of the directory this box was given, written onto the Session
    # that just took it. Until this line the workspace belongs to a disposable
    # box; after it, it is the conversation's, and the box built to replace this
    # one mounts the same name instead of minting a stranger.
    adopted_workspace = str(manifest.get(WORKSPACE_ID_FIELD) or "").strip()
    if adopted_workspace:
        from astrabox.persistence.repository.session_repository import (
            SessionRepository,
        )

        await SessionRepository().update_session(
            target_session, {WORKSPACE_ID_FIELD: adopted_workspace}
        )
    logger.info(
        "prepared slot claimed: agent=%s slot=%s session=%s workspace=%s",
        target_agent,
        manifest.get("slot_id"),
        target_session,
        adopted_workspace or "<none>",
    )
    return claimed


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
    the manifest is bookkeeping debris and holding it would block the next
    refill.
    """

    repo = agent_repo if agent_repo is not None else AgentRepository()
    row = await repo.get_agent(str(agent_id or "").strip())
    manifest = (row or {}).get(PREPARED_SLOT_FIELD) if isinstance(row, dict) else None
    if not isinstance(manifest, dict):
        return
    if str(manifest.get("slot_id") or "") != str(slot_id or "").strip():
        return
    await repo.compare_and_update_agent(
        str(agent_id or "").strip(),
        expected={PREPARED_SLOT_FIELD: manifest},
        updates={PREPARED_SLOT_FIELD: None},
    )


def _has_platform_mcp_server(template: Any) -> bool:
    from astrabox.core.service.orchestrator.runtime.mcp_servers import (
        is_platform_mcp_server,
        template_mcp_servers,
    )

    return any(
        isinstance(config, dict) and is_platform_mcp_server(config)
        for config in template_mcp_servers(
            getattr(template, "mcp_servers", None)
        ).values()
    )


async def _publish_prepared_platform_mcp_binding(
    template: Any,
    *,
    agent_id: str,
    slot_id: str,
    sandbox_id: str,
    sandbox_backend: str,
) -> None:
    """Publish the slot address before an engine can connect its MCP client."""

    if not _has_platform_mcp_server(template):
        return
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

    if not _has_platform_mcp_server(template):
        return
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
) -> None:
    """Destroy an unclaimed slot's placement and clear its manifest.

    Closing the isolated sessions kills the runner and the prepared engine
    child with them (they live in the slot's PID namespace), so no separate
    process teardown is needed or attempted.
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
    await repo.compare_and_update_agent(
        agent_id,
        expected={PREPARED_SLOT_FIELD: manifest},
        updates={PREPARED_SLOT_FIELD: None},
    )


async def _publish_prepared_manifest(
    repo: Any,
    agent_id: str,
    manifest: dict[str, Any],
    *,
    replacing: dict[str, Any] | None,
) -> None:
    """Publish one manifest by CAS against the exact value the refill read.

    A first publish expects the explicit None; a renewal expects the slot it
    is replacing. Losing either means the field moved — another refill won,
    or a Session claimed the slot being replaced — and this build is
    discarded by the caller rather than overwriting the winner.
    """

    won = await repo.compare_and_update_agent(
        agent_id,
        expected={PREPARED_SLOT_FIELD: replacing},
        updates={PREPARED_SLOT_FIELD: manifest},
    )
    if not won:
        raise APIError(
            code="AGENT_PREWARM_SLOT_CONFLICT",
            message=(
                "the prepared slot moved while this one was built "
                "(claimed, or another refill published first); discarding it"
            ),
            status_code=409,
        )


async def _published_manifest_slot(repo: Any, agent_id: str) -> set[str]:
    """The slot an unclaimed box is currently published under, if any.

    An unclaimed entry survives pruning only while something names its slot, so
    every caller that prunes has to name the published one. Dropping it leaves a
    later claim with no durable owner for the Session key it mints. Both callers
    read the published slot through here so their GC keep-sets cannot diverge.
    """

    row = await repo.get_agent(str(agent_id or "").strip())
    manifest = (row or {}).get(PREPARED_SLOT_FIELD) if isinstance(row, dict) else None
    if not isinstance(manifest, dict):
        return set()
    current = str(manifest.get("slot_id") or "").strip()
    return {current} if current else set()


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


async def record_gateway_entry(
    repo: Any,
    agent_id: str,
    *,
    slot_id: str,
    session_id: str | None = None,
) -> None:
    """Append one workload's ledger owner, pruning ended Sessions.

    Preparation records an unclaimed slot; a conversation-pool acquire already
    knows its Session and records that owner in the same CAS. For a whole-box
    workload the entry is garbage-collection bookkeeping only — each box's
    binding lives in its own vault — but pruning is still what deletes a dead
    Session's gateway key. The keep-set covers this slot and whatever manifest
    is currently published, read fresh here so a concurrent refill's unclaimed
    entry is not collected between its publish and this write.
    """

    target_agent = str(agent_id or "").strip()
    target_slot = str(slot_id or "").strip()
    target_session = str(session_id or "").strip() or None
    if not target_agent or not target_slot:
        return
    keep = {target_slot} | await _published_manifest_slot(repo, target_agent)
    await _prune_gateway_entries(repo, target_agent, keep_slots=keep)
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


async def mark_gateway_entry_claimed(
    *,
    agent_id: str,
    slot_id: str,
    session_id: str,
    agent_repo: Any | None = None,
) -> None:
    """Record which Session owns this slot's gateway key."""

    target_agent = str(agent_id or "").strip()
    target_slot = str(slot_id or "").strip()
    target_session = str(session_id or "").strip()
    if not target_agent or not target_slot or not target_session:
        return
    repo = agent_repo if agent_repo is not None else AgentRepository()
    for _attempt in range(_GATEWAY_LEDGER_CAS_ATTEMPTS):
        row = await repo.get_agent(target_agent)
        if not isinstance(row, dict):
            raise APIError(
                code="AGENT_NOT_FOUND",
                message=f"agent {target_agent!r} disappeared while claiming a gateway key",
                status_code=404,
            )
        field_present = GATEWAY_ENTRIES_FIELD in row
        raw = row.get(GATEWAY_ENTRIES_FIELD)
        entries = [dict(item) for item in (raw or []) if isinstance(item, dict)]
        changed = False
        for entry in entries:
            if str(entry.get("slot_id") or "").strip() == target_slot:
                if str(entry.get("session_id") or "").strip() == target_session:
                    return
                entry["session_id"] = target_session
                changed = True
        if not changed:
            return
        expected = raw if field_present else {"$exists": False}
        if await repo.compare_and_update_agent(
            target_agent,
            expected={GATEWAY_ENTRIES_FIELD: expected},
            updates={GATEWAY_ENTRIES_FIELD: entries},
        ):
            return
    raise APIError(
        code="AGENT_RUNTIME_ERROR",
        message=(
            f"agent {target_agent!r} gateway-key ledger changed repeatedly while "
            f"slot {target_slot!r} was being claimed"
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
    repo: Any,
    agent_id: str,
    slot_id: str,
    sandbox_id: str,
    required_network_hosts: tuple[str, ...] = (),
) -> tuple[str, dict[str, str], bool, list[dict[str, Any]]]:
    """Install this new slot's credential plan in the shared box.

    The write contains model, MCP, and Agent environment credentials together.
    It names only this workload; the provider carries live siblings from its own
    revision-guarded Vault state. Only after that write lands does the Agent
    ledger record this slot for later claim and Session-key garbage collection.
    """

    from astrabox.core.service.orchestrator.runtime.mcp_credentials import (
        resolve_agent_mcp_credential_plan,
    )
    from astrabox.core.service.orchestrator.runtime.mcp_servers import (
        sandbox_mcp_egress_hosts,
    )
    from astrabox.core.service.orchestrator.runtime.plugin_repos import (
        plugin_repo_egress_hosts,
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
            *tuple(plugin_repo_egress_hosts(template)),
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

    # Ledger AFTER the vault accepted the write: claim/GC bookkeeping must not
    # name a credential the provider never installed.
    await record_gateway_entry(repo, agent_id, slot_id=slot_id)
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
    # Claim the ledger BEFORE minting or repointing the external credential.
    # An unclaimed entry is collectable; losing it during either network call
    # leaves the Session key with no durable GC owner. The provider protects
    # sibling Vault state under its own revision and this ledger records only
    # lifecycle ownership. A claim that then fails is collected through the
    # same terminal/missing-Session path as any other abandoned key.
    await mark_gateway_entry_claimed(
        agent_id=str(getattr(template, "agent_id", "") or ""),
        slot_id=slot_id,
        session_id=session_id,
    )

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
