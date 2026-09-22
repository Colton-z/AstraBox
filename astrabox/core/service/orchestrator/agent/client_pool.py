"""Compose an Agent's base-box pool from platform declarations.

OpenSandbox's client-pool SDK owns the distributed mechanism: leader election,
Redis coordination, replenishment, retries, atomic acquisition and retirement.
This module owns the product recipe handed to that mechanism.  It is the only
side that knows what an Agent box must contain, which workspace root it mounts,
and which runtime capabilities it must prove before becoming inventory.

The pool owns physical boxes, not Sessions. With Agent tenancy a claimed box
becomes the Agent's shared placement; with conversation tenancy the preparer
initializes the engine before publication and the claiming Session owns the
whole box. Both placements use the same engine preparation and activation seam.
A shared box's prepared engine slot is not another physical-box pool.
"""

from __future__ import annotations

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
from astrabox.core.service.orchestrator.runtime.plugin_repos import (
    plugin_repo_egress_hosts,
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

_POOL_MAX_IDLE = 1
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

    if not bool(_value(template, "prewarm_enabled", False)):
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
    if not str(getattr(settings, "agent_prewarm_redis_url", "") or "").strip():
        raise APIError(
            code="AGENT_PREWARM_CONFIG_INVALID",
            message=(
                "Agent prewarming requires ASTRABOX_AGENT_PREWARM_REDIS_URL "
                "for the sandbox client's distributed pool"
            ),
            status_code=500,
        )

    provider = sandbox_for_template(template)
    if not bool(getattr(provider, "supports_client_pool", False)):
        raise APIError(
            code="AGENT_PREWARM_UNSUPPORTED",
            message=(
                f"sandbox backend {provider.name!r} has no maintained "
                "client-side pool for Agent prewarming"
            ),
            status_code=409,
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
        max_idle=_POOL_MAX_IDLE,
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
        credential, network_policy, vault_plan = resolve_model_credential_delivery(
            template=template,
            backend_adapter=provider,
            credential=request.credential,
            additional_vault_write=mcp_plan,
            required_hosts=(
                *tuple(request.required_network_hosts),
                *plugin_repo_egress_hosts(template),
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
        resource_limits, resource_requests = sandbox_create_resources()
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

        handle = await create_sandbox_with_storage(provider, create_spec)
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
                [
                    str(item).strip()
                    for item in (_value(template, "skills", []) or [])
                    if str(item or "").strip()
                ],
            ),
        )
        if conversation_owned:
            from astrabox.core.service.orchestrator.agent.runtime_preparation import prepare_pool_runtime

            await prepare_pool_runtime(handle, template=template, manager=runtime_manager)

    return AgentClientPoolPlan(
        backend_name=str(provider.name),
        spec=spec,
        creator=create_member,
        preparer=prepare_member,
    )


async def ensure_agent_client_pool(
    template: Any,
    *,
    runtime_manager: Any,
) -> AgentClientPoolPlan | None:
    """Start or join the supplier pool for the current Agent generation."""

    plan = await build_agent_client_pool_plan(
        template,
        runtime_manager=runtime_manager,
    )
    if plan is None:
        return None
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
