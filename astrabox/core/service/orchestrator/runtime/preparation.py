"""Coordinate Agent resources and deliver bound runtime materials."""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import re
import time
import uuid
from typing import Any, Awaitable, Callable, Mapping

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.base import EngineStartupContext
from astrabox.core.service.orchestrator.engine.registry import get_engine_adapter
from astrabox.core.service.orchestrator.runtime.packages import template_packages
from astrabox.core.service.orchestrator.runtime.sandbox_client import get_underlying_sandbox
from astrabox.core.service.orchestrator.runtime.skill_sources import (
    normalize_skills,
    prepare_agent_runtime_skill_cache,
    skill_cache_dir,
)
from astrabox.core.service.orchestrator.runtime.storage._plugin_cache import (
    _build_plugin_repo_cache_checkout_dir,
    prepare_agent_runtime_plugin_cache,
)
from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.core.service.orchestrator.runtime_binding import is_assistant_workspace_bootstrap
from astrabox.seams.runtime_preparation import (
    PreparationSubject,
    PreparedPackage,
    ResourcePreparationContext,
    ResourcePreparationOperation,
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
_ASSISTANT_SYNC_TIMEOUT_SECONDS = 600.0
_ASSISTANT_SYNC_WAIT_SECONDS = 1.0


def preparation_subject(session: Mapping[str, Any] | None) -> PreparationSubject:
    """The Agent or Assistant a Session belongs to, from its persisted binding.

    Read as the Session's configuration is resolved
    (``AgentConfigService.resolve_session_harness``): an Assistant Session by
    its ``workspace_ref``, any other by the Agent its ``workspace_ref`` or its
    own ``agent_id`` names.
    """
    ref = (session or {}).get("workspace_ref")
    ref = ref if isinstance(ref, Mapping) else {}
    if str(ref.get("kind") or "").strip() == "assistant":
        assistant_id = str(ref.get("assistant_id") or (session or {}).get("owner_id") or "").strip()
        if assistant_id:
            return PreparationSubject(kind="assistant", id=assistant_id)
    else:
        agent_id = str(ref.get("agent_id") or (session or {}).get("agent_id") or "").strip()
        if agent_id:
            return PreparationSubject(kind="agent", id=agent_id)
    raise APIError(code="RUNTIME_PREPARATION_FAILED", status_code=409,
                   message="Runtime preparation requires the Session's Agent or Assistant binding")


def subject_revision(subject: PreparationSubject, config: Mapping[str, Any]) -> str:
    """The configuration revision a subject's resources were prepared from."""
    value = config.get("assistant_revision") if subject.kind == "assistant" else config.get("version")
    if value is None or not str(value).strip():
        raise APIError(code="RUNTIME_PREPARATION_FAILED", status_code=409,
                       message=f"Runtime preparation requires the {subject.kind}'s configuration revision")
    return str(value)


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


def _failure(*, code: str, subject: PreparationSubject | None = None) -> APIError:
    kind = subject.kind.capitalize() if subject is not None else "Agent"
    messages = {
        "AGENT_RUNTIME_GENERATION_CONFLICT": "Agent resources belong to a superseded preparation; retry the current Agent.",
        "ASSISTANT_REVISION_CONFLICT": "Assistant resources were prepared from a superseded configuration; retry with the current Assistant.",
        "RESOURCE_SYNC_BUSY": f"Another resource synchronization is still running for this {kind}; preparation will retry.",
        "RESOURCE_SYNC_UNRESOLVED": "An interrupted resource synchronization requires provider recovery before preparation can continue.",
        "RESOURCE_SYNC_FAILED": f"Prepared resource synchronization failed; inspect the deployment provider using the {kind} and operation ids.",
    }
    return APIError(code=code, message=messages[code], status_code=409 if code != "RESOURCE_SYNC_FAILED" else 502)


async def _current(repo: Any, config: dict[str, Any]) -> dict[str, Any]:
    row = await repo.get_agent(config["agent_id"])
    if not isinstance(row, dict) or any(row.get(k) != v for k, v in _generation(config).items()):
        raise _failure(code="AGENT_RUNTIME_GENERATION_CONFLICT")
    return row


async def assert_prepared_resources_current(template: Any, *, agent_repo: Any = None) -> None:
    if runtime_preparation_provider() is not None:
        await _current(agent_repo if agent_repo is not None else AgentRepository(), _config(template))


@dataclasses.dataclass(frozen=True)
class _SyncGuard:
    """Where one subject's synchronization is serialized, and how it is fenced.

    ``current`` reads the row holding the operation and raises when the
    preparation it belongs to is superseded; ``starting`` is the predicate a
    new operation is written under; ``write`` is the row's compare-and-set.
    """
    subject: PreparationSubject
    current: Callable[[], Awaitable[dict[str, Any]]]
    starting: Callable[[dict[str, Any]], dict[str, Any]]
    write: Callable[[dict[str, Any], dict[str, Any]], Awaitable[bool]]
    context: Callable[[dict[str, Any], ResourcePreparationOperation], ResourcePreparationContext]


async def _synchronize(provider: Any, guard: _SyncGuard, *, timeout_seconds: float) -> None:
    subject = guard.subject
    row = await guard.current()
    pending = row.get(_OPERATION)
    if pending:
        if pending["provider"] != provider.name:
            raise _failure(code="RESOURCE_SYNC_UNRESOLVED", subject=subject)
        if float(pending["deadline"]) > time.time():
            raise _failure(code="RESOURCE_SYNC_BUSY", subject=subject)
        try:
            async with asyncio.timeout(timeout_seconds):
                recovered = await provider.recover(operation=ResourcePreparationOperation(
                    subject=subject, operation_id=pending["operation_id"],
                ))
        except Exception:
            raise _failure(code="RESOURCE_SYNC_UNRESOLVED", subject=subject) from None
        if recovered is not True:
            raise _failure(code="RESOURCE_SYNC_UNRESOLVED", subject=subject)
        # Expiry only permits recovery. It never grants permission to repeat an
        # external write whose outcome or future execution remains unknown.
        await guard.write({_OPERATION: pending}, {_OPERATION: None})
        row = await guard.current()
        if row.get(_OPERATION):
            raise _failure(code="RESOURCE_SYNC_BUSY", subject=subject)
    operation = {
        "provider": provider.name, "operation_id": uuid.uuid4().hex,
        "deadline": time.time() + timeout_seconds,
    }
    if not await guard.write(guard.starting(row), {_OPERATION: operation}):
        await guard.current()
        raise _failure(code="RESOURCE_SYNC_BUSY", subject=subject)
    try:
        context = guard.context(row, ResourcePreparationOperation(
            subject=subject, operation_id=operation["operation_id"],
        ))
        async with asyncio.timeout(timeout_seconds):
            await provider.synchronize(context=context)
    except BaseException as exc:
        logger.warning(
            "Prepared resource synchronization failed: %s=%s operation=%s error_type=%s",
            subject.kind, subject.id, operation["operation_id"], type(exc).__name__,
        )
        # Preserve ownership after cancellation too. Recovery must establish
        # that a delayed provider request cannot overwrite a later generation.
        await guard.write({_OPERATION: operation}, {_OPERATION: {**operation, "deadline": 0}})
        if not isinstance(exc, Exception):
            raise
        raise _failure(code="RESOURCE_SYNC_FAILED", subject=subject) from None
    # A successful return proves completion; releasing this guard cannot let a
    # later callback overtake a still-running external write.
    if not await guard.write({_OPERATION: operation}, {_OPERATION: None}):
        raise _failure(code="RESOURCE_SYNC_UNRESOLVED", subject=subject)
    await guard.current()


def _prepared_paths(config: Mapping[str, Any]) -> tuple[tuple[PreparedPackage, ...], str | None]:
    """Where the subject's packages and Skills are cached in its box."""
    packages = tuple(PreparedPackage(
        source=repo_spec, checkout_path=_build_plugin_repo_cache_checkout_dir(index, repo_spec),
    ) for index, repo_spec in enumerate(template_packages(config)))
    return packages, skill_cache_dir(config["skills"]) if config.get("skills") else None


async def synchronize_prepared_resources(
    template: Any, sandbox: SandboxHandle, *, timeout_seconds: float, agent_repo: Any = None,
) -> None:
    """Synchronize an Agent's prepared resources before its inventory is published."""
    provider = runtime_preparation_provider()
    if provider is None:
        return
    config = _config(template)
    repo = agent_repo if agent_repo is not None else AgentRepository()
    agent_id = str(config["agent_id"])
    packages, skills_path = _prepared_paths(config)

    def context(row: dict[str, Any], operation: ResourcePreparationOperation) -> ResourcePreparationContext:
        from astrabox.core.service.orchestrator.runtime.preparation_commit import callback_commit_guard

        owner_user_id = str(row.get("created_by") or "")
        revision = str(config["version"])
        preparation_id = str(config["client_pool_epoch"])
        return ResourcePreparationContext(
            operation=operation,
            owner_user_id=owner_user_id,
            revision=revision,
            preparation_id=preparation_id,
            runtime_generation=str(config["runtime_generation"]),
            config=copy.deepcopy(config), sandbox=sandbox,
            packages=packages, skills_path=skills_path,
            commit_guard=callback_commit_guard(
                operation=operation, owner_user_id=owner_user_id, revision=revision,
                provider=provider.name, config=config, preparation_id=preparation_id,
            ),
        )

    await _synchronize(provider, _SyncGuard(
        subject=PreparationSubject(kind="agent", id=agent_id),
        current=lambda: _current(repo, config),
        starting=lambda row: {
            **{key: row.get(key) for key in _generation(config)},
            _OPERATION: row.get(_OPERATION),
        },
        # A new operation is fenced on the generation it starts under; the
        # writes that settle an operation name only the operation.
        write=lambda expected, updates: repo.swap_prepared_operation(
            agent_id,
            expected=expected[_OPERATION],
            replacement=updates[_OPERATION],
            generation={key: value for key, value in expected.items() if key != _OPERATION} or None,
        ),
        context=context,
    ), timeout_seconds=timeout_seconds)


async def _synchronize_assistant_resources(
    context: EngineStartupContext, *, provider: Any, subject: PreparationSubject,
    revision: str, config: dict[str, Any], owner_user_id: str,
    timeout_seconds: float = _ASSISTANT_SYNC_TIMEOUT_SECONDS,
) -> None:
    """Place an Assistant's extensions, then synchronize them before Hermes runs this revision.

    The caches are the same ones Hermes places into its profile; filling them
    here first lets the provider read the actual prepared files, and Hermes'
    own fill is then a cache hit. A second conversation preparing the same
    Assistant waits for a running synchronization instead of failing.
    """
    from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService
    from astrabox.persistence.repository.environment_repository import EnvironmentRepository
    from astrabox.persistence.repository.assistant_workspace_repository import (
        AssistantWorkspaceRepository,
    )
    from astrabox.persistence.repository.session_repository import SessionRepository

    await asyncio.gather(
        prepare_agent_runtime_skill_cache(
            get_underlying_sandbox(context.sandbox),
            normalize_skills(getattr(context.template, "skills", None)),
        ),
        prepare_agent_runtime_plugin_cache(
            context.sandbox, context.template, get_underlying_sandbox_fn=get_underlying_sandbox,
        ),
    )
    workspaces = AssistantWorkspaceRepository()
    sessions = SessionRepository()
    configuration = AgentConfigService(
        AgentRepository(), EnvironmentRepository(), sessions_repo=sessions,
    )
    packages, skills_path = _prepared_paths(config)

    async def current() -> dict[str, Any]:
        row = await workspaces.get_workspace(subject.id)
        session = await sessions.get_session(context.session_id)
        view = await configuration.resolve_session_harness(session or {})
        if not isinstance(row, dict) or view is None or str(view.assistant_revision) != revision:
            raise _failure(code="ASSISTANT_REVISION_CONFLICT", subject=subject)
        # _synchronize reads the operation under the key the Agent row uses.
        return {**row, _OPERATION: row.get("prepared_operation")}

    def build(_row: dict[str, Any], operation: ResourcePreparationOperation) -> ResourcePreparationContext:
        from astrabox.core.service.orchestrator.runtime.preparation_commit import callback_commit_guard

        return ResourcePreparationContext(
            operation=operation, owner_user_id=owner_user_id, revision=revision,
            preparation_id=context.sandbox_id,
            runtime_generation=str(config.get("runtime_generation") or ""),
            config=copy.deepcopy(config), sandbox=get_underlying_sandbox(context.sandbox),
            packages=packages, skills_path=skills_path,
            commit_guard=callback_commit_guard(
                operation=operation, owner_user_id=owner_user_id, revision=revision,
                provider=provider.name, config=config, preparation_id=context.sandbox_id,
            ),
        )

    guard = _SyncGuard(
        subject=subject, current=current,
        starting=lambda row: {_OPERATION: row.get(_OPERATION)},
        write=lambda expected, updates: workspaces.swap_prepared_operation(
            subject.id, expected=expected[_OPERATION], replacement=updates[_OPERATION],
        ),
        context=build,
    )
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            await _synchronize(provider, guard, timeout_seconds=timeout_seconds)
            return
        except APIError as exc:
            if exc.code != "RESOURCE_SYNC_BUSY" or time.monotonic() >= deadline:
                raise
        await asyncio.sleep(_ASSISTANT_SYNC_WAIT_SECONDS)


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
    subject = preparation_subject(session)
    revision = subject_revision(subject, config)
    if subject.kind == "assistant" and context.attach_mode != "lightweight":
        # An Agent synchronizes where its waiting inventory is prepared; an
        # Assistant has none, so it does here, before Hermes runs this revision.
        await _synchronize_assistant_resources(
            context, provider=provider, subject=subject, revision=revision,
            config=config, owner_user_id=principal,
        )
    if is_assistant_workspace_bootstrap(session):
        # A workspace prepared without a conversation has no Session to bind
        # materials to; each conversation receives its own when it attaches.
        return context
    request = SessionPreparationContext(
        session_id=context.session_id, principal_id=principal,
        subject=subject, revision=revision,
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
