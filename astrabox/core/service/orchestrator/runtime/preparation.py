"""Coordinate Agent resources and deliver bound runtime materials."""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import re
import time
import uuid
from typing import Any, Mapping

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.base import EngineStartupContext
from astrabox.core.service.orchestrator.engine.registry import get_engine_adapter
from astrabox.core.service.orchestrator.runtime.packages import template_packages
from astrabox.core.service.orchestrator.runtime.skill_sources import skill_cache_dir
from astrabox.core.service.orchestrator.runtime.storage._plugin_cache import (
    _build_plugin_repo_cache_checkout_dir,
)
from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.seams.runtime_preparation import (
    AgentPreparationContext,
    AgentPreparationOperation,
    PreparedPackage,
    RuntimeFile,
    RuntimeMaterials,
    PreparationUserActionRequired,
    SessionPreparationContext,
    runtime_material_path,
    runtime_preparation_plan,
    runtime_preparation_provider,
    validated_user_action,
)
from astrabox.seams.sandbox import SandboxHandle

logger = get_logger(__name__)

_OPERATION = "_prepared_resources_operation"


def _config(template: Any) -> dict[str, Any]:
    value = dataclasses.asdict(template) if dataclasses.is_dataclass(template) else (
        template if isinstance(template, dict) else vars(template)
    )
    return copy.deepcopy(value)


def _generation(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": config.get("version"),
        "_prewarm_revision": config.get("prewarm_revision"),
        "_client_pool_epoch": config.get("client_pool_epoch"),
        "_runtime_generation": config.get("runtime_generation"),
        "_sandbox_generation": config.get("sandbox_generation"),
    }


def _failure(*, code: str) -> APIError:
    messages = {
        "AGENT_RUNTIME_GENERATION_CONFLICT": "Agent resources belong to a superseded preparation; retry the current Agent.",
        "AGENT_RESOURCE_SYNC_BUSY": "Another resource synchronization is still running for this Agent; preparation will retry.",
        "AGENT_RESOURCE_SYNC_UNRESOLVED": "An interrupted resource synchronization requires provider recovery before preparation can continue.",
        "AGENT_RESOURCE_SYNC_FAILED": "Prepared resource synchronization failed; inspect the deployment provider using the Agent and operation ids.",
    }
    return APIError(code=code, message=messages[code], status_code=409 if code != "AGENT_RESOURCE_SYNC_FAILED" else 502)


async def _current(repo: Any, config: dict[str, Any]) -> dict[str, Any]:
    row = await repo.get_agent(config["agent_id"])
    if not isinstance(row, dict) or any(row.get(k) != v for k, v in _generation(config).items()):
        raise _failure(code="AGENT_RUNTIME_GENERATION_CONFLICT")
    return row


async def assert_prepared_resources_current(template: Any, *, agent_repo: Any = None) -> None:
    if runtime_preparation_provider() is not None:
        await _current(agent_repo if agent_repo is not None else AgentRepository(), _config(template))


async def synchronize_prepared_resources(
    template: Any, sandbox: SandboxHandle, *, timeout_seconds: float, agent_repo: Any = None,
) -> None:
    provider = runtime_preparation_provider()
    if provider is None:
        return
    config = _config(template)
    repo = agent_repo if agent_repo is not None else AgentRepository()
    agent_id = str(config["agent_id"])
    row = await _current(repo, config)
    pending = row.get(_OPERATION)
    if pending:
        if pending["provider"] != provider.name:
            raise _failure(code="AGENT_RESOURCE_SYNC_UNRESOLVED")
        if float(pending["deadline"]) > time.time():
            raise _failure(code="AGENT_RESOURCE_SYNC_BUSY")
        try:
            async with asyncio.timeout(timeout_seconds):
                recovered = await provider.recover(operation=AgentPreparationOperation(
                    agent_id=agent_id, operation_id=pending["operation_id"],
                ))
        except Exception:
            raise _failure(code="AGENT_RESOURCE_SYNC_UNRESOLVED") from None
        if recovered is not True:
            raise _failure(code="AGENT_RESOURCE_SYNC_UNRESOLVED")
        # Expiry only permits recovery. It never grants permission to repeat an
        # external write whose outcome or future execution remains unknown.
        await repo.compare_and_update_agent(
            agent_id, expected={_OPERATION: pending}, updates={_OPERATION: None},
        )
        row = await _current(repo, config)
        if row.get(_OPERATION):
            raise _failure(code="AGENT_RESOURCE_SYNC_BUSY")
    operation = {
        "provider": provider.name, "operation_id": uuid.uuid4().hex,
        "deadline": time.time() + timeout_seconds,
    }
    expected = {
        key: row[key] if key in row else {"$exists": False}
        for key in (*_generation(config), _OPERATION)
    }
    context = AgentPreparationContext(
        operation=AgentPreparationOperation(agent_id=agent_id, operation_id=operation["operation_id"]),
        creator_user_id=str(row.get("user_id") or ""),
        config_version=int(config["version"]),
        preparation_id=str(config["client_pool_epoch"]),
        runtime_generation=str(config["runtime_generation"]),
        config=copy.deepcopy(config),
        sandbox=sandbox,
        packages=tuple(PreparedPackage(
            source=repo_spec, checkout_path=_build_plugin_repo_cache_checkout_dir(index, repo_spec),
        ) for index, repo_spec in enumerate(template_packages(config))),
        skills_path=skill_cache_dir(config["skills"]) if config.get("skills") else None,
    )
    if not await repo.compare_and_update_agent(
        agent_id, expected=expected, updates={_OPERATION: operation},
    ):
        await _current(repo, config)
        raise _failure(code="AGENT_RESOURCE_SYNC_BUSY")
    try:
        async with asyncio.timeout(timeout_seconds):
            await provider.synchronize(context=context)
    except BaseException as exc:
        logger.warning(
            "Prepared resource synchronization failed: agent=%s operation=%s error_type=%s",
            agent_id, operation["operation_id"], type(exc).__name__,
        )
        # Preserve ownership after cancellation too. Recovery must establish
        # that a delayed provider request cannot overwrite a later generation.
        await repo.compare_and_update_agent(
            agent_id, expected={_OPERATION: operation},
            updates={_OPERATION: {**operation, "deadline": 0}},
        )
        if not isinstance(exc, Exception):
            raise
        raise _failure(code="AGENT_RESOURCE_SYNC_FAILED") from None
    # A successful return proves completion; releasing this guard cannot let a
    # later callback overtake a still-running external write.
    released = await repo.compare_and_update_agent(
        agent_id, expected={_OPERATION: operation}, updates={_OPERATION: None},
    )
    if not released:
        raise _failure(code="AGENT_RESOURCE_SYNC_UNRESOLVED")
    await _current(repo, config)


async def deliver_runtime_files(
    sandbox: Any,
    *,
    runtime_identity: Mapping[str, Any],
    session_id: str,
    sandbox_id: str,
    files: tuple[RuntimeFile, ...],
) -> None:
    """Write provider files 0600 as the workload account, in its private home.

    A write that has started completes even when the caller is cancelled, so
    a refreshed credential is never left half-written for the engine.
    """
    owner = str(runtime_identity.get("linux_user") or "").strip()
    if not owner:
        raise ValueError("runtime files require a private workload account")
    names: set[str] = set()
    for item in files:
        if not isinstance(item, RuntimeFile) or not isinstance(item.content, bytes) or item.name in names:
            raise ValueError("invalid or duplicate runtime file")
        names.add(item.name)
        path = runtime_material_path(
            runtime_identity, session_id=session_id, sandbox_id=sandbox_id, name=item.name,
        )
        write = asyncio.create_task(sandbox.files.write_bytes(
            path, item.content, mode=0o600, owner=owner, group=owner,
        ))
        try:
            await asyncio.shield(write)
        except asyncio.CancelledError:
            await write
            raise


async def prepare_session_runtime_context(context: EngineStartupContext) -> EngineStartupContext:
    """Finish Session materials before the engine sees its startup context.

    Later inputs are prepared by the provider's ``prepare_turn`` under the
    turn guard; this startup environment stays fixed for the runtime's life.
    """
    provider = runtime_preparation_provider()
    prepared = context.prepared_manifest
    if prepared is not None and not prepared["engine_initialized"]:
        context = dataclasses.replace(context, prepared_manifest=None)
    if provider is None:
        return context

    from astrabox.persistence.repository.session_repository import SessionRepository

    session = await SessionRepository().get_session(context.session_id)
    principal = str((session or {}).get("user_id") or "").strip()
    identity = copy.deepcopy(context.runtime_identity or {})
    if not principal or principal != context.user_id:
        raise APIError(code="RUNTIME_PREPARATION_FAILED", status_code=409,
                       message="Runtime preparation requires the persisted Session owner and workload identity")
    config = _config(context.template)
    request = SessionPreparationContext(
        session_id=context.session_id, principal_id=principal,
        agent_id=str((session or {}).get("agent_id") or "").strip() or None,
        config_version=int(config.get("version") or 0),
        runtime_generation=str(config.get("runtime_generation") or ""),
        sandbox_id=context.sandbox_id, runtime_identity=identity, config=config,
        sandbox=context.sandbox,
    )
    try:
        async with asyncio.timeout(30.0):
            materials = await provider.prepare_session(context=request)
            if not isinstance(materials, RuntimeMaterials):
                raise TypeError("runtime preparation requires RuntimeMaterials")
            environment = dict(materials.environment)
            if any(not isinstance(k, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k)
                   or not isinstance(v, str) or "\0" in v for k, v in environment.items()):
                raise ValueError("invalid runtime environment")
            if runtime_preparation_plan(config).session_bound and not materials.files and not environment:
                raise ValueError("Session-bound preparation returned no materials")
            if environment:
                adapter = get_engine_adapter(str(config.get("engine_kind") or ""))
                if not adapter.supports_startup_environment():
                    raise ValueError("engine cannot deliver Session startup environment")
                request_env = adapter.sandbox_request(
                    template=context.template, model_access=context.model_access,
                )
                protected = set(request_env.env) | set(context.runtime_env or {})
                protected.update(value for value in (
                    request_env.credential_env_var, request_env.cwd_env_var,
                ) if value)
                if protected & set(environment):
                    raise ValueError("runtime materials conflict with platform environment")
            if materials.files:
                await deliver_runtime_files(
                    context.sandbox, runtime_identity=identity, session_id=context.session_id,
                    sandbox_id=context.sandbox_id, files=materials.files,
                )
    except Exception as exc:
        logger.warning("Runtime preparation failed: session=%s error_type=%s",
                       context.session_id, type(exc).__name__)
        if isinstance(exc, PreparationUserActionRequired):
            action = validated_user_action({
                "message": exc.message, "url": exc.url, "status": exc.status,
            })
            if action is not None:
                raise APIError(
                    code="TURN_PREPARATION_USER_ACTION_REQUIRED", status_code=409,
                    message=action["message"], evidence={
                        "reason": "user_action_required", "user_action": action,
                        "session_id": context.session_id,
                    },
                ) from None
        raise APIError(code="RUNTIME_PREPARATION_FAILED", status_code=502,
                       message="Session runtime preparation failed; inspect the deployment integration") from None
    return dataclasses.replace(context, runtime_env={**dict(context.runtime_env or {}), **environment})
