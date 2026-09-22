"""Prepare engine state once, independently of the placement's allocator."""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.base import EnginePreparationContext
from astrabox.core.service.orchestrator.engine.registry import get_engine_adapter
from astrabox.core.service.orchestrator.engine.runtime_profiles import SANDBOX_IMAGE_WORKLOAD_HOME


POOL_RUNTIME_RECEIPT = f"{SANDBOX_IMAGE_WORKLOAD_HOME}/.astrabox-prepared-runtime.json"


async def prepare_pool_runtime(sandbox: Any, *, template: Any, manager: Any) -> None:
    """Complete the same engine preparation inside an SDK-owned whole box."""

    from astrabox.common.utils.settings import load_astrabox_settings
    from astrabox.core.service.orchestrator.agent.client_pool import conversation_pool_receipt
    from astrabox.core.service.orchestrator.agent.prepared_slots import (
        _bootstrap_slot_identity,
        _delete_prepared_platform_mcp_binding,
        _publish_prepared_platform_mcp_binding,
        _suppress_and_log,
    )
    from astrabox.core.service.orchestrator.engine import transcript_mirror
    from astrabox.core.service.orchestrator.engine.provisioning import (
        engine_service_credential,
        environment_credential_contract,
        resolve_model_credential_delivery,
        resolve_prepared_environment_credentials,
        resolve_sandbox_websocket_endpoint,
    )
    from astrabox.core.service.orchestrator.runtime.mcp_credentials import resolve_agent_mcp_credential_plan
    from astrabox.core.service.orchestrator.runtime.sandbox_client import extract_sandbox_id
    from astrabox.core.service.orchestrator.workspace.deployment_conversation import build_conversation_identity
    from astrabox.seams.egress_credentials import workload_model_placeholder
    from astrabox.seams.sandbox import sandbox_for_template

    provider = sandbox_for_template(template)
    sandbox_id = extract_sandbox_id(sandbox)
    descriptor = await provider.describe_sandbox(sandbox_id)
    placement = conversation_pool_receipt(descriptor.metadata)
    if placement is None:
        raise APIError(code="AGENT_PREWARM_CONFIG_INVALID", message="pool box has no workload identity", status_code=500)
    slot_id, workspace_id = placement
    adapter = get_engine_adapter(template.engine_kind)
    model_access = manager.resolve_model_access(template.model_config or {})
    request = adapter.sandbox_request(template=template, model_access=model_access)
    settings = load_astrabox_settings()
    vault_enabled = bool(settings.sandbox_credential_vault_enabled)
    mcp_plan = await resolve_agent_mcp_credential_plan(template=template, vault_enabled=vault_enabled)
    credential, _, vault_plan = resolve_model_credential_delivery(
        template=template,
        backend_adapter=provider,
        credential=request.credential,
        additional_vault_write=mcp_plan,
        slot_id=slot_id,
    )
    vault_plan, runtime_env = await resolve_prepared_environment_credentials(
        template, slot_id=slot_id, vault_enabled=vault_enabled, vault_write=vault_plan,
    )
    identity = build_conversation_identity(session_id=slot_id, agent_id=template.agent_id, template=template)
    identity = await _bootstrap_slot_identity(sandbox, template, slot_id, identity=identity)
    identity = {**identity, "sandbox_id": sandbox_id}
    await manager.clone_default_repo(
        sandbox, template, str(identity["workspace_dir"]), slot_id,
        runtime_identity=identity,
    )
    if adapter.capabilities.session_log is not None:
        await transcript_mirror.mark_mirror_unclaimed(
            sandbox, unclaimed_for=timedelta(seconds=int(settings.sandbox_lease_seconds) + 300),
        )
    runner_port = int(request.wait_for_inbox_service_port or 0)
    runner_uri = await resolve_sandbox_websocket_endpoint(sandbox, runner_port) if runner_port else None
    try:
        await _publish_prepared_platform_mcp_binding(
            template, agent_id=template.agent_id, slot_id=slot_id,
            sandbox_id=sandbox_id, sandbox_backend=str(provider.name),
        )
        manifest = await prepare_engine_runtime(
            EnginePreparationContext(
                template=template, slot_id=slot_id, activation_token=secrets.token_hex(32),
                placement="conversation_box", sandbox=sandbox, sandbox_id=sandbox_id,
                cwd=str(identity["workspace_dir"]), runtime_identity=identity,
                model_access=model_access, model_credential=credential, runtime_env=runtime_env,
                runner_uri=runner_uri, preparation_fingerprint=template.runtime_generation,
                deployment_settings=manager.deployment_settings, workspace_id=workspace_id,
                gateway_substitution=credential == workload_model_placeholder(slot_id),
                service_credential=engine_service_credential(request, identity),
            ),
            sandbox_backend=str(provider.name), environment_contract=environment_credential_contract(vault_plan),
            runner_port=runner_port,
        )
        # This receipt belongs to this live runtime, not to supplier inventory or
        # SessionStore. Destroying the box invalidates it; no persistent volume is needed.
        await sandbox.files.write_file(
            POOL_RUNTIME_RECEIPT, json.dumps(manifest).encode(), mode=600, owner="root", group="root",
        )
    except BaseException:
        with _suppress_and_log("pool runtime binding delete", slot_id):
            await _delete_prepared_platform_mcp_binding(slot_id)
        raise


async def prepare_engine_runtime(
    context: EnginePreparationContext,
    *,
    sandbox_backend: str,
    environment_contract: list[dict[str, Any]],
    runner_port: int,
) -> dict[str, Any]:
    """Carry the vendor's preparation evidence unchanged into activation."""

    engine_kind = str(context.template.engine_kind)
    receipt = await get_engine_adapter(engine_kind).prepare_runtime(context)
    return {
        **dict(receipt),
        "slot_id": context.slot_id,
        "state": "prepared",
        "placement": context.placement,
        "engine_kind": engine_kind,
        "sandbox_id": context.sandbox_id,
        "sandbox_backend": sandbox_backend,
        "cwd": context.cwd,
        "runtime_identity": context.runtime_identity,
        "runtime_generation": context.preparation_fingerprint,
        "workspace_id": context.workspace_id,
        "activation_token": context.activation_token,
        "runner_port": runner_port,
        "gateway_substitution": context.gateway_substitution,
        "model_credential": context.model_credential,
        "runtime_env": context.runtime_env,
        "environment_credential_contract": environment_contract,
        "prepared_at": datetime.now(timezone.utc).isoformat(),
    }


async def read_pool_runtime_receipt(
    sandbox: Any, *, sandbox_id: str, workload_id: str, generation: str,
) -> dict[str, Any]:
    """Read an acquired box's transient receipt, never a second pool inventory."""

    manifest = json.loads(await sandbox.files.read_file(POOL_RUNTIME_RECEIPT))
    if not isinstance(manifest, dict) or any(
        manifest.get(key) != value
        for key, value in {
            "sandbox_id": sandbox_id,
            "slot_id": workload_id,
            "runtime_generation": generation,
            "placement": "conversation_box",
            "state": "prepared",
        }.items()
    ):
        raise APIError(
            code="AGENT_PREWARM_CONFIG_INVALID",
            message="acquired sandbox does not carry its expected prepared engine runtime",
            status_code=409,
        )
    return manifest
