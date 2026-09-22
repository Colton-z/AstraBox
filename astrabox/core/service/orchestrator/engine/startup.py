"""Platform-owned runtime startup orchestration.

An engine declares the box it needs and activates its vendor protocol. The
platform alone decides whether to claim or create, prepares the workspace and
credentials, records cleanup ownership, and replenishes prepared capacity.
"""

from __future__ import annotations

import contextlib
from typing import Any

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.core.service.orchestrator.agent.prepared_boxes import (
    PLACEMENT_CONVERSATION_BOX,
    discard_prepared_box,
)
from astrabox.core.service.orchestrator.agent.prepared_slots import (
    bind_claimed_platform_mcp_binding,
    clear_claimed_slot,
    discard_prepared_slot,
    schedule_prepared_runtime_refill,
)
from astrabox.core.service.orchestrator.engine.base import (
    EngineAdapter,
    EngineStartupContext,
    EngineStartupMaterialRequest,
)
from astrabox.core.service.orchestrator.engine.provisioning import (
    engine_service_credential,
    ProvisionedEngineSandbox,
    connect_engine_sandbox,
    plan_conversation_identity,
    prepare_platform_workspace,
    provision_engine_sandbox,
    resolve_model_credential_delivery,
    resolve_sandbox_websocket_endpoint,
    resolve_session_environment_credentials,
    write_engine_env_file,
)
from astrabox.core.service.orchestrator.engine.platform_events import (
    PlatformEngineEventSink,
    PlatformResidentOutputSink,
)
from astrabox.core.service.orchestrator.runtime.mcp_credentials import (
    mcp_credential_refresher,
    resolve_mcp_credential_plan,
)
from astrabox.core.service.orchestrator.runtime.mcp_servers import (
    make_mcp_deployment_id,
    sandbox_mcp_egress_hosts,
    template_mcp_servers,
)
from astrabox.seams.egress_credentials import workload_placeholder_context
from astrabox.secrets import SecretProvider


async def _report_progress(callback: Any, stage: str) -> None:
    if callback is None:
        return
    with contextlib.suppress(Exception):
        await callback(stage)


def _workspace_capability_scope(workspace_plan: Any, user_id: str | None) -> Any:
    """Resolve the platform workspace policy before entering an engine."""

    from astrabox.core.service.orchestrator.workspace import workspace_from_subject_kind

    workspace = workspace_from_subject_kind(
        workspace_plan.subject_kind,
        user_id=user_id,
        agent_id=workspace_plan.agent_id,
        assistant_id=workspace_plan.assistant_id,
        engine_kind=workspace_plan.engine_kind,
    )
    return workspace.capability_scope()


def _startup_material_request(
    adapter: EngineAdapter,
    *,
    template: Any,
    model_access: Any,
    deployment_settings: Any,
) -> EngineStartupMaterialRequest:
    request = adapter.startup_material_request(
        template=template,
        model_access=model_access,
        deployment_settings=deployment_settings,
    )
    if not isinstance(request, EngineStartupMaterialRequest):
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=(
                f"engine {adapter.engine_kind!r} returned an invalid startup material declaration"
            ),
            status_code=500,
        )
    normalized = tuple(str(name or "").strip() for name in request.secret_names)
    if any(not name for name in normalized) or len(set(normalized)) != len(normalized):
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=(
                f"engine {adapter.engine_kind!r} declared empty or duplicate platform secret names"
            ),
            status_code=500,
        )
    return EngineStartupMaterialRequest(
        transcript_store=bool(request.transcript_store),
        runtime_state_store=bool(request.runtime_state_store),
        sandbox_death_notice=bool(request.sandbox_death_notice),
        secret_names=normalized,
    )


def _runtime_state_target(
    request: EngineStartupMaterialRequest,
    *,
    workspace_plan: Any,
    user_id: str | None,
    deployment_settings: Any,
) -> dict[str, Any] | None:
    """Bind database custody to the workspace, not the Session claiming it."""
    if not request.runtime_state_store:
        return None
    from dataclasses import asdict

    from astrabox.api.routes.transcript import CAPABILITY_PATH_PREFIX
    from astrabox.core.service.orchestrator.transcript_capability import (
        mint_runtime_state_capability_token,
    )
    from astrabox.core.service.orchestrator.workspace import workspace_from_subject_kind
    from astrabox.persistence.repository.runtime_state_snapshot_repository import (
        RuntimeStateOwner,
    )

    workspace = workspace_from_subject_kind(
        workspace_plan.subject_kind,
        user_id=user_id,
        agent_id=workspace_plan.agent_id,
        assistant_id=workspace_plan.assistant_id,
        engine_kind=workspace_plan.engine_kind,
    )
    ref = workspace.ref
    owner = RuntimeStateOwner(
        user_id=str(ref.user_id or user_id or ""),
        subject_kind=ref.kind,
        subject_id=str(ref.assistant_id or ref.agent_id or ""),
        engine_kind=str(workspace_plan.engine_kind),
    )
    base = str(deployment_settings.mcp_proxy_base_url or "").strip().rstrip("/")
    if not base:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message="native runtime state requires a sandbox-reachable backend URL",
            status_code=500,
        )
    token = mint_runtime_state_capability_token(owner.canonical_key())
    return {
        "base_url": f"{base}{CAPABILITY_PATH_PREFIX}/{token}/api/v1/runtime-state",
        "owner": asdict(owner),
    }


def _resolve_declared_secrets(
    adapter: EngineAdapter,
    request: EngineStartupMaterialRequest,
) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for name in request.secret_names:
        value = str(SecretProvider.get_secret(name) or "")
        if not value.strip():
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=(
                    f"engine {adapter.engine_kind!r} requires unresolved platform secret {name!r}"
                ),
                status_code=500,
            )
        resolved[name] = value
    return resolved


def _platform_access_targets(
    request: EngineStartupMaterialRequest,
    *,
    session_id: str,
    sandbox_id: str,
    deployment_settings: Any,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not request.transcript_store and not request.sandbox_death_notice:
        return None, None

    from astrabox.api.routes.transcript import (
        CAPABILITY_PATH_PREFIX,
        transcript_capability_required,
    )
    from astrabox.core.service.orchestrator.transcript_capability import (
        mint_sandbox_box_capability_token,
        mint_transcript_capability_token,
    )

    base = str(getattr(deployment_settings, "mcp_proxy_base_url", "") or "").strip().rstrip("/")
    if not base:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=(
                "an engine requested platform transcript access but no "
                "sandbox-reachable backend base url is configured"
            ),
            status_code=500,
        )

    store = None
    if request.transcript_store:
        store_base = base
        if transcript_capability_required():
            store_base = (
                f"{base}{CAPABILITY_PATH_PREFIX}/{mint_transcript_capability_token(session_id)}"
            )
        store = {"base_url": store_base}

    death_notice = None
    if request.sandbox_death_notice:
        target_sandbox = str(sandbox_id or "").strip()
        if not target_sandbox:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="sandbox death notice requires a platform sandbox id",
                status_code=500,
            )
        token = mint_sandbox_box_capability_token(target_sandbox)
        death_notice = {
            "url": (
                f"{base}{CAPABILITY_PATH_PREFIX}/{token}"
                f"/api/v1/sandbox/{target_sandbox}/terminating"
            )
        }
    return store, death_notice


async def _publish_platform_mcp_binding(
    *,
    template: Any,
    workspace_plan: Any,
    runtime_identity: dict[str, Any] | None,
    sandbox_id: str,
    user_id: str | None,
) -> str | None:
    """Publish an Assistant workspace's platform-MCP route.

    This is orchestration state, so the platform writes it before the engine
    receives the route identifier. Agent conversations use their Session or
    prepared-slot binding paths and deliberately do not enter this branch.
    """

    if not template_mcp_servers(getattr(template, "mcp_servers", None)):
        return None
    if str(getattr(workspace_plan, "subject_kind", "") or "") != "assistant_runtime":
        return None
    effective_user_id = str(getattr(workspace_plan, "user_id", None) or user_id or "").strip()
    assistant_id = str(getattr(workspace_plan, "assistant_id", "") or "").strip()
    target_sandbox = str(sandbox_id or "").strip()
    identity = dict(runtime_identity or {})
    if not effective_user_id or not assistant_id or not target_sandbox:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message="Assistant platform-MCP routing identity is incomplete",
            status_code=500,
        )
    deployment_id = make_mcp_deployment_id(
        scope_kind="assistant_workspace",
        owner_id=assistant_id,
        user_id=effective_user_id,
    )
    from astrabox.persistence.repository.platform_mcp_binding_repository import (
        PlatformMCPBindingRepository,
    )

    await PlatformMCPBindingRepository().upsert_binding(
        {
            "deployment_id": deployment_id,
            "scope_kind": "assistant_workspace",
            "template_name": str(getattr(template, "name", "") or "").strip(),
            "user_id": effective_user_id,
            "conversation_user_id": (
                str(getattr(workspace_plan, "user_id", None) or "").strip() or None
            ),
            "assistant_id": assistant_id,
            "sandbox_id": target_sandbox,
            "root_path": str(
                identity.get("file_root_source_dir") or identity.get("workspace_source_dir") or ""
            ).strip(),
            "config_dir": str(identity.get("config_dir") or "").strip(),
            "source": "platform_runtime",
        }
    )
    return deployment_id


async def _discard_claimed_unit(
    template: Any,
    manifest: dict[str, Any],
    *,
    reason: str,
) -> None:
    agent_id = str(getattr(template, "agent_id", "") or "").strip()
    if str(manifest.get("placement") or "") == PLACEMENT_CONVERSATION_BOX:
        await discard_prepared_box(agent_id, manifest, reason=reason)
        return
    if str(manifest.get("placement") or "") == "shared_slot":
        await discard_prepared_slot(agent_id, manifest, reason=reason)
        return
    raise APIError(
        code="AGENT_PREWARM_CONFIG_INVALID",
        message=(
            "claimed prepared runtime has no platform-owned placement kind; "
            "it cannot be cleaned up safely"
        ),
        status_code=500,
    )


async def start_platform_runtime(
    platform: Any,
    adapter: EngineAdapter,
    *,
    session_id: str,
    assignment_id: str,
    template: Any,
    workspace_plan: Any,
    user_id: str | None,
    permission_mode: str | None,
    progress_callback: Any,
    callback_url: str | None,
) -> Any:
    """Run the one platform startup line, then hand activation to an engine."""

    model_access = platform.resolve_model_access(template.model_config or {})
    material_request = _startup_material_request(
        adapter,
        template=template,
        model_access=model_access,
        deployment_settings=platform.deployment_settings,
    )
    platform_secrets = _resolve_declared_secrets(adapter, material_request)
    request = adapter.sandbox_request(
        template=template,
        model_access=model_access,
    )
    await _report_progress(progress_callback, "provisioning_container")
    provisioned: ProvisionedEngineSandbox = await provision_engine_sandbox(
        platform,
        session_id=session_id,
        assignment_id=assignment_id,
        template=template,
        workspace_plan=workspace_plan,
        user_id=user_id,
        callback_url=callback_url,
        request=request,
        progress_callback=progress_callback,
    )
    prepared = provisioned.prepared_manifest
    runtime = None
    try:
        if prepared is not None:
            # The route must name the Session before a retained child is
            # released from its activation barrier and starts connecting.
            await bind_claimed_platform_mcp_binding(
                template,
                prepared,
                session_id=session_id,
                user_id=user_id,
            )
        await _report_progress(progress_callback, "starting_agent")
        platform_mcp_deployment_id = await _publish_platform_mcp_binding(
            template=template,
            workspace_plan=workspace_plan,
            runtime_identity=provisioned.runtime_identity,
            sandbox_id=provisioned.sandbox_id,
            user_id=user_id,
        )
        transcript_store, sandbox_death_notice = _platform_access_targets(
            material_request,
            session_id=session_id,
            sandbox_id=provisioned.sandbox_id,
            deployment_settings=platform.deployment_settings,
        )
        runtime = await adapter.activate_runtime(
            EngineStartupContext(
                session_id=session_id,
                template=template,
                workspace_plan=workspace_plan,
                sandbox=provisioned.sandbox,
                sandbox_id=provisioned.sandbox_id,
                cwd=provisioned.cwd,
                runtime_identity=provisioned.runtime_identity,
                model_access=model_access,
                model_credential=provisioned.model_credential,
                resume_session_key=provisioned.resume_session_key,
                runtime_env=dict(provisioned.runtime_env),
                prepare_engine_input=provisioned.prepare_engine_input,
                prepared_manifest=prepared,
                service_credential=engine_service_credential(
                    request, provisioned.runtime_identity
                ),
                runner_uri=provisioned.runner_uri,
                user_id=user_id,
                permission_mode=permission_mode,
                deployment_settings=platform.deployment_settings,
                capability_scope=_workspace_capability_scope(
                    workspace_plan,
                    user_id,
                ),
                event_sink=PlatformEngineEventSink(session_id),
                resident_output_sink=PlatformResidentOutputSink(
                    session_id,
                    broker=getattr(platform, "event_broker", None),
                ),
                transcript_store=transcript_store,
                runtime_state_store=_runtime_state_target(
                    material_request,
                    workspace_plan=workspace_plan,
                    user_id=user_id,
                    deployment_settings=platform.deployment_settings,
                ),
                sandbox_death_notice=sandbox_death_notice,
                platform_secrets=platform_secrets,
                platform_mcp_deployment_id=platform_mcp_deployment_id,
            )
        )
        if prepared is not None:
            await clear_claimed_slot(
                agent_id=str(getattr(template, "agent_id", "") or ""),
                slot_id=str(prepared.get("slot_id") or ""),
            )
    except BaseException as exc:
        if runtime is not None:
            close = getattr(getattr(runtime, "engine_client", None), "close", None)
            if callable(close):
                with contextlib.suppress(BaseException):
                    await close()
        if prepared is not None:
            # A bad prepared unit is never retried as a cold start in this
            # request. Destroy exactly what was claimed and let the caller
            # report the failed startup; replenishment is a later platform job.
            await _discard_claimed_unit(
                template,
                prepared,
                reason=f"activation failed: {type(exc).__name__}: {exc}",
            )
            schedule_prepared_runtime_refill(template, platform)
        raise

    schedule_prepared_runtime_refill(template, platform)
    return runtime


async def attach_platform_runtime(
    platform: Any,
    adapter: EngineAdapter,
    *,
    session_id: str,
    sandbox_id: str,
    template: Any,
    workspace_plan: Any,
    user_id: str | None,
    engine_session_key: str | None,
    permission_mode: str | None,
    runtime_identity: dict[str, Any] | None,
    attach_mode: str,
) -> Any:
    """Rebuild one engine client after the platform re-adopts its placement.

    A reattach is not an engine-owned shortcut around startup. The platform
    reconnects and verifies the box, refreshes every protected credential,
    restores an isolated placement when one exists, prepares the workspace on
    the full path, and resolves the service endpoint. The engine receives the
    same prepared context as an ordinary activation and chooses only its
    vendor reconnect handshake.
    """

    if attach_mode not in {"full", "lightweight"}:
        raise APIError(
            code="ENGINE_ATTACH_MODE_INVALID",
            message=f"unsupported runtime attach_mode: {attach_mode!r}",
            status_code=500,
        )
    target_sandbox = str(sandbox_id or "").strip()
    if not target_sandbox:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message="runtime attach requires an existing sandbox id",
            status_code=400,
        )

    user_id = str(workspace_plan.user_id or user_id or "").strip() or None
    model_access = platform.resolve_model_access(template.model_config or {})
    material_request = _startup_material_request(
        adapter,
        template=template,
        model_access=model_access,
        deployment_settings=platform.deployment_settings,
    )
    platform_secrets = _resolve_declared_secrets(adapter, material_request)
    request = adapter.sandbox_request(template=template, model_access=model_access)
    persisted_backend = await platform.resolve_runtime_sandbox_backend(
        session_id,
        workspace_plan=workspace_plan,
    )
    from astrabox.seams.sandbox import sandbox_for_name

    backend_adapter = sandbox_for_name(persisted_backend)
    identity = dict(runtime_identity or {})
    if request.plan_identity and not identity:
        identity = dict(
            plan_conversation_identity(
                workspace_plan=workspace_plan,
                template=template,
                session_id=session_id,
                user_id=user_id,
            )
            or {}
        )
    identity["sandbox_id"] = target_sandbox
    identity["session_id"] = session_id
    cwd = (
        str(identity.get("workspace_dir") or "").strip()
        or str(request.cwd or "").strip()
        or str(getattr(workspace_plan, "cwd", "") or "").strip()
    )
    if not cwd:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=f"runtime attach for Session {session_id!r} has no workspace",
            status_code=500,
        )

    vault_enabled = bool(
        getattr(load_astrabox_settings(), "sandbox_credential_vault_enabled", False)
    )
    mcp_vault_write = await resolve_mcp_credential_plan(
        platform,
        session_id=session_id,
        template=template,
        vault_enabled=vault_enabled,
    )
    credential_slot_id = str(identity.get("credential_slot_id") or "").strip()
    credential, _network_policy, vault_write = resolve_model_credential_delivery(
        template=template,
        backend_adapter=backend_adapter,
        credential=request.credential,
        additional_vault_write=mcp_vault_write,
        required_hosts=tuple(request.required_network_hosts),
        mcp_hosts=tuple(sandbox_mcp_egress_hosts(getattr(template, "mcp_servers", None))),
        slot_id=credential_slot_id or None,
    )
    vault_write, runtime_env = await resolve_session_environment_credentials(
        platform,
        session_id=session_id,
        vault_enabled=vault_enabled,
        vault_write=vault_write,
        placeholder_context=(
            workload_placeholder_context(credential_slot_id)
            if credential_slot_id
            else None
        ),
    )

    sandbox = await connect_engine_sandbox(
        platform,
        sandbox_id=target_sandbox,
        template=template,
    )
    try:
        if vault_write is not None and not vault_write.is_empty:
            await backend_adapter.apply_credential_vault(
                sandbox,
                vault_write=vault_write,
                create_if_missing=False,
            )
        if credential_slot_id:
            from astrabox.core.service.orchestrator.agent.prepared_slots import (
                repoint_slot_gateway_credential,
            )

            # Re-composition above restores the prepared workload's shared
            # placeholder binding. Reapply the Session binding before the
            # resumed engine can send input, using the same claim path as the
            # initial host rather than letting attach invent another one.
            await repoint_slot_gateway_credential(
                sandbox,
                backend_provider=backend_adapter,
                credential_request=request.credential,
                template=template,
                claimed={
                    "slot_id": credential_slot_id,
                    "gateway_substitution": True,
                },
                session_id=session_id,
                user_id=user_id,
            )

        shared = (
            workspace_plan.subject_kind == "deployment_conversation"
            and str(identity.get("sandbox_tenancy") or "").strip() == "agent"
        )
        placement = None
        if shared:
            from astrabox.core.service.orchestrator.runtime.shared_sandbox_lease import (
                SharedSandboxLease,
            )
            from astrabox.persistence.repository.agent_repository import AgentRepository

            required_identity = {
                "isolated_session_id",
                "home_dir",
                "workspace_dir",
                "workspace_source_dir",
                "uid",
                "gid",
            }
            missing = sorted(key for key in required_identity if identity.get(key) in (None, ""))
            if missing:
                raise APIError(
                    code="AGENT_RUNTIME_ERROR",
                    message=(
                        f"shared runtime attach has no persisted placement facts: {missing!r}"
                    ),
                    status_code=500,
                )
            lease = SharedSandboxLease(
                agent_repo=AgentRepository(),
                provider=backend_adapter,
            )
            placement = await lease.restore_existing(
                sandbox_id=target_sandbox,
                isolated_session_id=str(identity["isolated_session_id"]),
                terminal_isolated_session_id=str(
                    identity.get("terminal_isolated_session_id") or ""
                ),
                home_dir=str(identity["home_dir"]),
                workspace_dir=str(identity["workspace_dir"]),
                workspace_source_dir=str(identity["workspace_source_dir"]),
                uid=int(identity["uid"]),
                gid=int(identity["gid"]),
            )
            identity.update(
                {
                    "isolated_session_id": placement.isolated_session_id,
                    "terminal_isolated_session_id": (placement.terminal_isolated_session_id),
                    "uid": placement.uid,
                    "gid": placement.gid,
                }
            )

        if attach_mode == "full":
            identity_result, cwd = await prepare_platform_workspace(
                platform,
                sandbox,
                session_id=session_id,
                template=template,
                workspace_plan=workspace_plan,
                user_id=user_id,
                runtime_identity=identity or None,
                cwd=cwd,
            )
            identity = dict(identity_result or identity)

        if identity:
            await platform.record_attached_runtime_identity(
                session_id,
                sandbox_id=target_sandbox,
                runtime_identity=identity,
            )

        runner_uri = None
        if placement is not None:
            await write_engine_env_file(
                sandbox,
                request=request,
                credential=credential,
                cwd=cwd,
                runtime_identity=identity,
                owner_label=f"Session {session_id!r}",
                additional_env=runtime_env,
            )
            from astrabox.core.service.orchestrator.engine.runtime_profiles import (
                runner_port_for_uid,
            )

            port = int(
                await lease.start_runner(
                    placement,
                    launch=adapter.shared_conversation_service_launch(
                        home=placement.home_dir,
                        workspace=placement.workspace_dir,
                        port=runner_port_for_uid(placement.uid),
                    ),
                    engine_label=adapter.engine_kind,
                )
            )
            runner_uri = await resolve_sandbox_websocket_endpoint(sandbox, port)
        elif request.wait_for_inbox_service_port is not None:
            runner_uri = await resolve_sandbox_websocket_endpoint(
                sandbox,
                request.wait_for_inbox_service_port,
            )

        prepare_engine_input = (
            mcp_credential_refresher(
                platform,
                session_id=session_id,
                template=template,
                backend_adapter=backend_adapter,
                sandbox=sandbox,
                initial_credential_plan=mcp_vault_write,
                vault_enabled=vault_enabled,
            )
            if mcp_vault_write is not None
            else None
        )
        platform_mcp_deployment_id = await _publish_platform_mcp_binding(
            template=template,
            workspace_plan=workspace_plan,
            runtime_identity=identity or None,
            sandbox_id=target_sandbox,
            user_id=user_id,
        )
        transcript_store, sandbox_death_notice = _platform_access_targets(
            material_request,
            session_id=session_id,
            sandbox_id=target_sandbox,
            deployment_settings=platform.deployment_settings,
        )
        runtime = await adapter.activate_runtime(
            EngineStartupContext(
                session_id=session_id,
                template=template,
                workspace_plan=workspace_plan,
                sandbox=sandbox,
                sandbox_id=target_sandbox,
                cwd=cwd,
                runtime_identity=identity or None,
                model_access=model_access,
                model_credential=credential,
                resume_session_key=engine_session_key,
                runtime_env=dict(runtime_env),
                service_credential=engine_service_credential(request, identity),
                prepare_engine_input=prepare_engine_input,
                runner_uri=runner_uri,
                user_id=user_id,
                permission_mode=permission_mode,
                deployment_settings=platform.deployment_settings,
                capability_scope=_workspace_capability_scope(
                    workspace_plan,
                    user_id,
                ),
                event_sink=PlatformEngineEventSink(session_id),
                resident_output_sink=PlatformResidentOutputSink(
                    session_id,
                    broker=getattr(platform, "event_broker", None),
                ),
                transcript_store=transcript_store,
                runtime_state_store=_runtime_state_target(
                    material_request,
                    workspace_plan=workspace_plan,
                    user_id=user_id,
                    deployment_settings=platform.deployment_settings,
                ),
                sandbox_death_notice=sandbox_death_notice,
                platform_secrets=platform_secrets,
                platform_mcp_deployment_id=platform_mcp_deployment_id,
                attach_mode=attach_mode,
            )
        )
    except BaseException:
        with contextlib.suppress(BaseException):
            await sandbox.close()
        raise
    return runtime


__all__ = ["attach_platform_runtime", "start_platform_runtime"]
