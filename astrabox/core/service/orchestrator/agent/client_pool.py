"""Compose an Agent's base-box pool from platform declarations.

OpenSandbox's client-pool SDK owns the distributed mechanism: leader election,
Redis coordination, replenishment, retries, atomic acquisition and retirement.
This module owns the product recipe handed to that mechanism.  It is the only
side that knows what an Agent box must contain, which workspace root it mounts,
and which runtime capabilities it must prove before becoming inventory.

The pool owns physical boxes, not Sessions, and its preparer completes every
prepared unit before a box is published: with conversation tenancy the box's
one engine, which the claiming Session takes with the whole box; with Agent
tenancy as many engine seats as the box's memory holds, which the Agent takes
with the box and hands out one per conversation. Both use the same engine
preparation and activation seam, and replenishment, the replica that does it
and idle expiry are the pool's for both.
"""

from __future__ import annotations

from astrabox.core.service.orchestrator.runtime.skill_sources import normalize_skills

import asyncio
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.core.service.orchestrator.engine.registry import get_engine_adapter
from astrabox.core.service.orchestrator.runtime.conversation_identity import (
    prepare_agent_runtime_skill_cache,
)
from astrabox.core.service.orchestrator.runtime.mcp_servers import (
    sandbox_mcp_egress_hosts,
)
from astrabox.core.service.orchestrator.runtime.packages import (
    package_egress_hosts,
)
from astrabox.core.service.orchestrator.runtime.runtime_profile import (
    probe_sandbox_runtime_profile,
    resolve_runtime_profile,
    resolve_sandbox_permission_level,
    resolve_sandbox_tenancy,
)
from astrabox.core.service.orchestrator.runtime.sandbox_client import (
    extract_sandbox_id,
    get_underlying_sandbox,
)
from astrabox.core.service.orchestrator.runtime.storage import (
    prepare_agent_runtime_plugin_cache,
)
from astrabox.providers.sandbox_image import SANDBOX_SELF_DESCRIPTION
from astrabox.seams.sandbox import (
    SANDBOX_TENANCY_AGENT,
    SANDBOX_TENANCY_CONVERSATION,
    SandboxAllocation,
    SandboxClientPoolCreator,
    SandboxClientPoolMember,
    SandboxClientPoolPreparer,
    SandboxClientPoolSpec,
    SandboxCreateSpec,
    sandbox_for_name,
    sandbox_for_template,
)

logger = get_logger(__name__)

_PREPARE_TIMEOUT_SECONDS = 600
_POOL_NAME_PART_RE = re.compile(r"[^a-z0-9-]+")

# Durable cleanup address for the supplier namespace currently serving an
# Agent.  Both values live together: a pool name is meaningful only to the
# backend that created it, especially after the Environment changes provider.
CLIENT_POOL_NAME_FIELD = "_client_pool_name"
CLIENT_POOL_BACKEND_FIELD = "_client_pool_backend"

# Platform claim facts carried by the physical box itself. OpenSandbox treats
# these as opaque metadata; their meaning and lifecycle remain in AstraBox.
CLIENT_POOL_WORKLOAD_ID_METADATA_KEY = "astrabox.prepared-workload-id"
CLIENT_POOL_WORKSPACE_ID_METADATA_KEY = "astrabox.workspace-id"


def agent_client_pool_first_member_timeout_seconds() -> float:
    """The full supplier window for publishing a pool's first member."""

    settings = load_astrabox_settings()
    return float(
        int(settings.sandbox_ready_timeout_seconds) + _PREPARE_TIMEOUT_SECONDS
    )


def _value(subject: Any, key: str, default: Any = None) -> Any:
    if isinstance(subject, dict):
        return subject.get(key, default)
    return getattr(subject, key, default)


def prepared_capacity(template: Any) -> int:
    """How many new conversations of the Agent can start at once on prepared units."""

    return int(_value(template, "prepared_capacity", 0) or 0)


def _pool_size(template: Any, *, conversation_owned: bool) -> int:
    """Pool boxes that hold the Agent's prepared capacity.

    A conversation box is one unit. An Agent box carries as many seats as its
    memory held when the latest one was prepared; until a box has reported
    that, the pool keeps one, so a large capacity is not first met with one box
    per unit.
    """

    capacity = prepared_capacity(template)
    if conversation_owned:
        return capacity
    seats = int(_value(template, "seats_per_box", 0) or 0)
    return -(-capacity // seats) if seats > 0 else 1


def _pool_name(agent_id: str, epoch: str) -> str:
    safe = _POOL_NAME_PART_RE.sub("-", agent_id.lower()).strip("-") or "agent"
    return f"astrabox-agent-{safe[:16]}-{epoch}"


@dataclass(frozen=True)
class AgentClientPoolPlan:
    """One immutable SDK-pool namespace and its platform callbacks."""

    backend_name: str
    spec: SandboxClientPoolSpec
    creator: SandboxClientPoolCreator
    preparer: SandboxClientPoolPreparer


@dataclass(frozen=True)
class AgentClientPoolClaim:
    """One atomically removed supplier member and its platform claim facts."""

    sandbox: Any
    sandbox_id: str
    pool_name: str
    workload_id: str | None = None
    workspace_id: str | None = None


def conversation_pool_receipt(
    metadata: Mapping[str, str],
) -> tuple[str, str] | None:
    """Read the platform receipt from a conversation-pool box.

    No receipt means this sandbox came from the ordinary cold-create path. A
    partial receipt is corruption: neither a workspace nor a workload identity
    can safely be guessed from the other.
    """

    workload_id = str(metadata.get(CLIENT_POOL_WORKLOAD_ID_METADATA_KEY) or "").strip()
    workspace_id = str(metadata.get(CLIENT_POOL_WORKSPACE_ID_METADATA_KEY) or "").strip()
    if not workload_id and not workspace_id:
        return None
    if not workload_id or not workspace_id:
        raise APIError(
            code="AGENT_PREWARM_CONFIG_INVALID",
            message="client-pool sandbox carries an incomplete platform claim receipt",
            status_code=500,
        )
    return workload_id, workspace_id


async def build_agent_client_pool_plan(
    template: Any,
    *,
    runtime_manager: Any,
) -> AgentClientPoolPlan | None:
    """Build the one base-box pool for an enabled Agent generation."""

    if prepared_capacity(template) <= 0:
        return None
    tenancy = resolve_sandbox_tenancy(template)
    conversation_owned = tenancy == SANDBOX_TENANCY_CONVERSATION

    agent_id = str(_value(template, "agent_id", "") or "").strip()
    generation = str(_value(template, "sandbox_generation", "") or "").strip()
    epoch = str(_value(template, "client_pool_epoch", "") or "").strip()
    engine_kind = str(_value(template, "engine_kind", "") or "").strip()
    if not agent_id or not generation or not epoch or not engine_kind:
        raise APIError(
            code="AGENT_PREWARM_CONFIG_INVALID",
            message=(
                "client-pool preparation requires an Agent id, engine kind, "
                "published sandbox generation and pool lifetime"
            ),
            status_code=500,
        )
    settings = load_astrabox_settings()
    provider = sandbox_for_template(template)
    if not provider.supports_client_pool:
        raise APIError(
            code="AGENT_PREWARM_UNSUPPORTED",
            message=(
                f"sandbox backend {provider.name!r} has no maintained "
                "client-side pool for Agent prewarming"
            ),
            status_code=409,
        )
    readiness = provider.client_pool_readiness()
    if not readiness.available:
        raise APIError(
            code="AGENT_PREWARM_CONFIG_INVALID",
            message=(
                f"sandbox backend {provider.name!r} cannot run its client pool in "
                f"this deployment: {readiness.detail}"
            ),
            status_code=500,
        )
    adapter = get_engine_adapter(engine_kind)
    creation_image = str(_value(template, "runtime_template_name", "") or "").strip()
    if not creation_image:
        from astrabox.core.service.orchestrator.runtime.config_resolver import (
            resolve_runtime_template_name,
        )

        creation_image = resolve_runtime_template_name(template)
    spec = SandboxClientPoolSpec(
        pool_name=_pool_name(agent_id, epoch),
        creation_image=creation_image,
        max_idle=_pool_size(template, conversation_owned=conversation_owned),
        idle_timeout_seconds=int(settings.sandbox_lease_seconds),
        preparation_timeout_seconds=_PREPARE_TIMEOUT_SECONDS,
    )

    async def create_member(member: SandboxClientPoolMember) -> Any:
        from astrabox.core.service.orchestrator.engine import transcript_mirror
        from astrabox.core.service.orchestrator.engine.provisioning import (
            plan_workspace_mounts,
            resolve_model_credential_delivery,
            resolve_prepared_environment_credentials,
            sandbox_create_resources,
            workspace_is_ready,
            workspace_ref_for_subject,
        )
        from astrabox.core.service.orchestrator.runtime.storage import (
            mint_workspace_id,
        )
        from astrabox.core.service.orchestrator.workspace.deployment_conversation import (
            build_conversation_identity,
        )
        from astrabox.core.service.orchestrator.runtime.mcp_credentials import (
            resolve_agent_mcp_credential_plan,
        )

        model_access = runtime_manager.resolve_model_access(
            dict(_value(template, "model_config", {}) or {})
        )
        request = adapter.sandbox_request(
            template=template,
            model_access=model_access,
        )
        vault_enabled = bool(settings.sandbox_credential_vault_enabled)
        mcp_plan = await resolve_agent_mcp_credential_plan(
            template=template,
            vault_enabled=vault_enabled,
        )
        workload_id = (
            f"slot-{uuid.uuid4().hex}"
            if conversation_owned
            else member.assignment_id
        )
        from astrabox.core.service.orchestrator.engine.platform_mcp import request_with_platform_mcp
        from astrabox.core.service.orchestrator.runtime.mcp_servers import prepared_slot_mcp_deployment_id

        if conversation_owned:
            request = request_with_platform_mcp(
                request, template=template, deployment_settings=runtime_manager.deployment_settings,
                deployment_id=prepared_slot_mcp_deployment_id(workload_id),
            )
        credential, network_policy, vault_plan = resolve_model_credential_delivery(
            template=template,
            backend_adapter=provider,
            credential=request.credential,
            additional_vault_write=mcp_plan,
            required_hosts=(
                *tuple(request.required_network_hosts),
                *package_egress_hosts(template),
            ),
            mcp_hosts=tuple(sandbox_mcp_egress_hosts(_value(template, "mcp_servers", None))),
            slot_id=workload_id,
        )
        vault_plan, runtime_env = await resolve_prepared_environment_credentials(
            template,
            slot_id=workload_id,
            vault_enabled=vault_enabled,
            vault_write=vault_plan,
        )
        runtime_identity = (
            build_conversation_identity(
                session_id=workload_id,
                agent_id=agent_id,
                template=template,
            )
            if conversation_owned
            else None
        )
        workspace_id = mint_workspace_id() if conversation_owned else None
        subject_kind = (
            "deployment_conversation"
            if conversation_owned
            else "deployment_runtime"
        )
        planned_mounts = await plan_workspace_mounts(
            subject_kind=subject_kind,
            session_id="",
            workspace_id=workspace_id,
            agent_id=agent_id,
            assistant_id=None,
            user_id=None,
            runtime_identity=runtime_identity,
        )
        cwd = str(
            request.cwd
            or (runtime_identity or {}).get("workspace_dir")
            or "/workspace"
        ).strip()
        mirror_env = (
            transcript_mirror.deferred_mirror_env()
            if adapter.capabilities.session_log is not None
            else {}
        )
        resource_limits, resource_requests = provider.fit_resources(
            *sandbox_create_resources(shared=not conversation_owned)
        )
        create_spec = SandboxCreateSpec(
            session_id=member.session_id,
            assignment_id=member.assignment_id,
            resource_limits=resource_limits,
            resource_requests=resource_requests,
            image=spec.creation_image,
            entrypoint=tuple(request.entrypoint),
            cwd=cwd,
            metadata=(
                {
                    CLIENT_POOL_WORKLOAD_ID_METADATA_KEY: workload_id,
                    CLIENT_POOL_WORKSPACE_ID_METADATA_KEY: str(workspace_id),
                }
                if conversation_owned
                else {}
            ),
            env={
                **SANDBOX_SELF_DESCRIPTION,
                **dict(request.env),
                **mirror_env,
                **dict(runtime_env),
                **({request.credential_env_var: credential} if request.credential_env_var else {}),
                **({request.cwd_env_var: cwd} if request.cwd_env_var else {}),
            },
            workspace_mounts=planned_mounts,
            publish_ports=tuple(request.publish_ports),
            wait_for_inbox_service_port=request.wait_for_inbox_service_port,
            requires_command_channel=True,
            network_policy=network_policy,
            permission_level=resolve_sandbox_permission_level(template),
            credential_proxy_enabled=vault_enabled,
            vault_write=vault_plan,
        )
        from astrabox.core.service.orchestrator.runtime.storage.mounts import create_sandbox_with_storage

        from astrabox.core.service.orchestrator.agent.prepared_slots import (
            _delete_prepared_platform_mcp_binding,
            _publish_prepared_platform_mcp_binding,
            _suppress_and_log,
        )

        # Resident engines discover tools during boot, before the provider
        # returns a sandbox id. Discovery needs the Agent, never user authority.
        if conversation_owned:
            await _publish_prepared_platform_mcp_binding(
                template, agent_id=agent_id, slot_id=workload_id,
                sandbox_id="", sandbox_backend=str(provider.name),
            )
        try:
            handle = await create_sandbox_with_storage(
                provider,
                create_spec,
                reason="prepared-conversation" if conversation_owned else "prepared-shared",
            )
        except BaseException:
            if conversation_owned:
                with _suppress_and_log("pool create binding delete", workload_id):
                    await _delete_prepared_platform_mcp_binding(workload_id)
            raise
        sandbox_id = extract_sandbox_id(handle)
        try:
            await workspace_is_ready(
                handle,
                planned_mounts,
                ref=workspace_ref_for_subject(
                    subject_kind=subject_kind,
                    agent_id=agent_id,
                    assistant_id=None,
                    session_id=workload_id if conversation_owned else None,
                ),
                backend=provider,
            )
        except BaseException:
            destruction = await provider.confirm_destroyed(sandbox_id)
            if not destruction.confirmed:
                logger.error(
                    "client-pool candidate cleanup was not confirmed: pool=%s sandbox=%s detail=%s",
                    spec.pool_name,
                    sandbox_id,
                    destruction.detail,
                )
            if conversation_owned:
                with _suppress_and_log("pool candidate binding delete", workload_id):
                    await _delete_prepared_platform_mcp_binding(workload_id)
            raise
        return handle

    async def prepare_member(handle: Any) -> None:
        profile = resolve_runtime_profile(
            template,
            session_kind="agent_chat",
            engine_kind=engine_kind,
        )
        await probe_sandbox_runtime_profile(handle, profile)
        if tenancy == SANDBOX_TENANCY_AGENT:
            capability = await provider.read_isolation_capability(
                extract_sandbox_id(handle)
            )
            if not capability.available:
                raise APIError(
                    code="SANDBOX_ISOLATION_UNSUPPORTED",
                    message=(
                        f"sandbox {extract_sandbox_id(handle)!r} cannot carry the "
                        "Agent's shared conversations: "
                        f"{capability.detail or 'isolation capability unavailable'}"
                    ),
                    status_code=409,
                )
        await asyncio.gather(
            prepare_agent_runtime_plugin_cache(
                handle,
                template,
                get_underlying_sandbox_fn=get_underlying_sandbox,
            ),
            prepare_agent_runtime_skill_cache(
                get_underlying_sandbox(handle),
                normalize_skills(_value(template, "skills", [])),
            ),
        )
        if conversation_owned:
            from astrabox.core.service.orchestrator.agent.runtime_preparation import prepare_pool_runtime

            await prepare_pool_runtime(handle, template=template, manager=runtime_manager)
        else:
            from astrabox.core.service.orchestrator.agent.prepared_slots import prepare_pool_seats

            await prepare_pool_seats(handle, template=template, runtime_manager=runtime_manager)

    return AgentClientPoolPlan(
        backend_name=str(provider.name),
        spec=spec,
        creator=create_member,
        preparer=prepare_member,
    )


async def _record_agent_client_pool(
    template: Any,
    plan: AgentClientPoolPlan,
    *,
    agent_repo: Any | None,
) -> None:
    """Record a namespace as the Agent's before anything starts it.

    A started namespace keeps its boxes until it is retired, and retirement
    finds namespaces only through the Agent row. The write that records this
    namespace also queues the one it displaces, so every namespace any path
    starts stays named until it is retired, whichever Agent writes land around
    its start. A namespace for a lifetime the Agent row does not carry is
    refused: another generation superseded it.
    """

    from astrabox.persistence.repository.agent_repository import AgentRepository

    repo = agent_repo if agent_repo is not None else AgentRepository()
    agent_id = str(_value(template, "agent_id", "") or "").strip()
    epoch = str(_value(template, "client_pool_epoch", "") or "").strip()
    name = plan.spec.pool_name
    backend = plan.backend_name
    for _ in range(3):
        row = await repo.get_agent(agent_id)
        if not isinstance(row, dict):
            raise APIError(
                code="AGENT_NOT_FOUND",
                message=f"Agent {agent_id!r} disappeared before client pool {name!r} started",
                status_code=404,
            )
        if str(row.get("_client_pool_epoch") or "").strip() != epoch:
            raise APIError(
                code="AGENT_RUNTIME_GENERATION_CONFLICT",
                message=(
                    f"client pool {name!r} belongs to a superseded runtime "
                    f"generation of Agent {agent_id!r}; retry with the current "
                    "Agent definition"
                ),
                status_code=409,
            )
        recorded = str(row.get(CLIENT_POOL_NAME_FIELD) or "").strip()
        recorded_backend = str(row.get(CLIENT_POOL_BACKEND_FIELD) or "").strip()
        if recorded == name and recorded_backend == backend:
            return
        retiring = list(row.get("_retiring_client_pools") or [])
        if recorded:
            retiring.append({"name": recorded, "backend": recorded_backend})
        if await repo.compare_and_update_agent(
            agent_id,
            expected={
                key: row[key] if key in row else {"$exists": False}
                for key in (
                    "_client_pool_epoch",
                    CLIENT_POOL_NAME_FIELD,
                    CLIENT_POOL_BACKEND_FIELD,
                    "_retiring_client_pools",
                )
            },
            updates={
                CLIENT_POOL_NAME_FIELD: name,
                CLIENT_POOL_BACKEND_FIELD: backend,
                "_retiring_client_pools": retiring,
            },
        ):
            return
    raise APIError(
        code="AGENT_RUNTIME_GENERATION_CONFLICT",
        message=f"Agent {agent_id!r} changed while recording client pool {name!r}",
        status_code=409,
    )


async def ensure_agent_client_pool(
    template: Any,
    *,
    runtime_manager: Any,
    agent_repo: Any | None = None,
) -> AgentClientPoolPlan | None:
    """Record, then start or join, the supplier pool for the current Agent generation."""

    plan = await build_agent_client_pool_plan(
        template,
        runtime_manager=runtime_manager,
    )
    if plan is None:
        return None
    await _record_agent_client_pool(template, plan, agent_repo=agent_repo)
    provider = sandbox_for_name(plan.backend_name)
    await provider.ensure_client_pool(
        plan.spec,
        creator=plan.creator,
        preparer=plan.preparer,
    )
    return plan


async def acquire_agent_client_pool(
    template: Any,
    *,
    runtime_manager: Any,
    session_id: str | None,
    assignment_id: str,
) -> AgentClientPoolClaim | None:
    """Take a base box for a Session startup or an input-free Agent slot.

    ``session_id=None`` means no Session exists yet. Agent slot preparation
    retains its existing Agent placement ledger rather than inventing a Session.
    """

    plan = await ensure_agent_client_pool(
        template,
        runtime_manager=runtime_manager,
    )
    if plan is None:
        return None
    from astrabox.core.service.orchestrator.agent.runtime_generation import (
        agent_runtime_owner_id,
    )

    provider = sandbox_for_name(plan.backend_name)
    handle = await provider.acquire_client_pool(plan.spec)
    if handle is None:
        if session_id is not None and resolve_sandbox_tenancy(template) == SANDBOX_TENANCY_CONVERSATION:
            from astrabox.core.service.orchestrator.agent.prepared_capacity import (
                record_prepared_capacity_miss,
            )

            await record_prepared_capacity_miss(
                str(_value(template, "agent_id", "") or "").strip(),
                tenancy=SANDBOX_TENANCY_CONVERSATION,
                session_id=session_id,
            )
        return None
    sandbox_id = extract_sandbox_id(handle)
    try:
        # The supplier identity protects an idle box from inventory reaping.
        # Publish its startup owner before replacing that identity: its physical
        # creation time may be hours older than this acquisition.
        if session_id is not None:
            await runtime_manager.record_startup_allocation(
                session_id,
                SandboxAllocation(
                    sandbox_id=sandbox_id,
                    sandbox_backend=plan.backend_name,
                    scope="sandbox",
                ),
            )
        tenancy = resolve_sandbox_tenancy(template)
        workload_id: str | None = None
        workspace_id: str | None = None
        if tenancy == SANDBOX_TENANCY_CONVERSATION:
            target_session = str(session_id or "").strip()
            if not target_session:
                raise APIError(
                    code="AGENT_PREWARM_CONFIG_INVALID",
                    message="conversation client-pool claim requires a Session id",
                    status_code=500,
                )
            descriptor = await provider.describe_sandbox(sandbox_id)
            receipt = conversation_pool_receipt(descriptor.metadata)
            if receipt is None:
                raise APIError(
                    code="AGENT_PREWARM_CONFIG_INVALID",
                    message=(
                        f"client-pool sandbox {sandbox_id!r} carries no platform "
                        "claim receipt"
                    ),
                    status_code=500,
                )
            workload_id, workspace_id = receipt
            owner_id = target_session
        else:
            owner_id = agent_runtime_owner_id(
                str(_value(template, "agent_id", "") or "").strip()
            )
        await provider.adopt_sandbox_identity(
            handle,
            session_id=owner_id,
            assignment_id=assignment_id,
        )
        if tenancy == SANDBOX_TENANCY_AGENT:
            capability = await provider.read_isolation_capability(sandbox_id)
            if not capability.available:
                raise APIError(
                    code="SANDBOX_ISOLATION_UNSUPPORTED",
                    message=(
                        f"acquired sandbox {sandbox_id!r} no longer provides the "
                        "Agent's required shared-conversation isolation: "
                        f"{capability.detail or 'isolation capability unavailable'}"
                    ),
                    status_code=409,
                )
    except BaseException:
        destruction = await provider.confirm_destroyed(sandbox_id)
        if not destruction.confirmed:
            logger.error(
                "acquired client-pool box survived failed adoption: pool=%s sandbox=%s detail=%s",
                plan.spec.pool_name,
                sandbox_id,
                destruction.detail,
            )
        raise
    return AgentClientPoolClaim(
        sandbox=handle,
        sandbox_id=sandbox_id,
        pool_name=plan.spec.pool_name,
        workload_id=workload_id,
        workspace_id=workspace_id,
    )


async def retire_agent_client_pool(pool_name: str, *, backend_name: str) -> None:
    """Retire one obsolete supplier namespace through its owning backend."""

    name = str(pool_name or "").strip()
    backend = str(backend_name or "").strip()
    if not name:
        return
    if not backend:
        raise APIError(
            code="AGENT_PREWARM_CONFIG_INVALID",
            message=f"client pool {name!r} has no persisted sandbox backend",
            status_code=500,
        )
    await sandbox_for_name(backend).retire_client_pool(name)


__all__ = [
    "AgentClientPoolClaim",
    "AgentClientPoolPlan",
    "CLIENT_POOL_BACKEND_FIELD",
    "CLIENT_POOL_NAME_FIELD",
    "CLIENT_POOL_WORKLOAD_ID_METADATA_KEY",
    "CLIENT_POOL_WORKSPACE_ID_METADATA_KEY",
    "acquire_agent_client_pool",
    "agent_client_pool_first_member_timeout_seconds",
    "build_agent_client_pool_plan",
    "conversation_pool_receipt",
    "ensure_agent_client_pool",
    "retire_agent_client_pool",
]
