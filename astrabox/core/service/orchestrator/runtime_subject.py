"""Runtime-allocation authority for Session startup.

The lifecycle worker executes one startup state machine. Providers in this
module decide which durable subject owns the allocation and return a typed
action: create a subject runtime, attach to one, or reuse an already-prepared
binding. The worker does not branch on Agent versus Assistant. A provider may
place a Session inside an Agent-shared sandbox; the allocation remains the
Session's while the physical sandbox remains owned by the Agent.

This is separate from ``workspace`` and ``RuntimeWorkspacePlan``: those define
paths, mounts, and provisioning inside a selected sandbox. They do not decide
which durable record owns the runtime allocation or who may materialize it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeAlias

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.assistant.assistant_workspace_service import (
    get_assistant_profile_ready_marker,
)
from astrabox.common.logger.logger_factory import get_logger
from astrabox.core.service.orchestrator.engine_kind_utils import (
    resolve_session_engine_kind,
)
from astrabox.core.service.orchestrator.runtime_binding import (
    RuntimeBindingResolution,
    RuntimeSubjectKind,
    is_assistant_workspace_bootstrap,
    is_assistant_user_conversation,
    reconcile_session_runtime_binding,
    runtime_subject_kind,
)
from astrabox.core.service.orchestrator.session_workspace_plan import (
    RuntimeWorkspacePlan,
)
from astrabox.seams.sandbox_disposal import SandboxDestruction

logger = get_logger(__name__)

_RUNTIME_SUBJECT_READY_TIMEOUT_SECONDS = 300.0
_RUNTIME_SUBJECT_READY_POLL_SECONDS = 0.5

RuntimeStartupAction = Literal[
    "create_runtime",
    "attach_runtime",
    "use_ready_binding",
]
RuntimeRecoveryAction = Literal[
    "recover_session_allocation",
    "restart_session_on_subject",
]


@dataclass(frozen=True)
class RuntimeStartupTarget:
    """The runtime action and owner-derived inputs for one Session startup."""

    action: RuntimeStartupAction
    session: dict[str, Any]
    workspace_plan: RuntimeWorkspacePlan
    binding_expires_at: str | None = None
    renewal_ttl_seconds: int | None = None
    persist_runtime_identity_on_session: bool = False


@dataclass(frozen=True)
class RuntimeStartupPending:
    """A runtime owner is materializing the binding this Session needs."""

    session: dict[str, Any]
    binding: RuntimeBindingResolution
    progress: str | None = None


RuntimeStartupResolution: TypeAlias = RuntimeStartupTarget | RuntimeStartupPending


@dataclass(frozen=True)
class RuntimeStartupCleanup:
    """Cleanup verdict plus the leak, if any, attributable to this Session."""

    destruction: SandboxDestruction | None
    leaked_sandbox_id: str | None


class RuntimeSubjectProvider(Protocol):
    """Owner-specific hooks behind the common startup state machine."""

    subject_kind: RuntimeSubjectKind
    recovery_action: RuntimeRecoveryAction

    async def prepare_startup(self, session: dict[str, Any]) -> None: ...

    async def resolve_startup(
        self,
        *,
        session: dict[str, Any],
        template: Any,
        resume_engine_session_key: str | None,
    ) -> RuntimeStartupResolution: ...

    async def publish_runtime_ready(
        self,
        *,
        session: dict[str, Any],
        sandbox_id: str,
        expires_at: str | None,
        runtime_identity: dict[str, Any] | None,
    ) -> None: ...

    async def converge_startup_failure(
        self,
        *,
        session: dict[str, Any],
        failure_phase: str,
        created_sandbox_id: str | None,
        cleanup: SandboxDestruction | None,
    ) -> bool: ...


class AssistantWorkspaceLifecycle(Protocol):
    """The existing owner service action a startup policy may invoke."""

    async def acquire_workspace_for_session(
        self,
        user: UserContext,
        assistant_id: str,
        *,
        provisioning_session_id: str,
        provisioning_sandbox_generation: str,
    ) -> Any: ...

    async def publish_workspace_runtime_ready(
        self,
        user: UserContext,
        assistant_id: str,
        *,
        provisioning_session_id: str,
        provisioning_sandbox_generation: str,
        sandbox_id: str,
        expires_at: str | None,
        runtime_identity: dict[str, Any] | None,
    ) -> None: ...

    async def converge_workspace_startup_failure(
        self,
        user: UserContext,
        assistant_id: str,
        *,
        provisioning_session_id: str,
        provisioning_sandbox_generation: str,
        failure_phase: str,
        created_sandbox_id: str | None,
        cleanup: SandboxDestruction | None,
    ) -> bool: ...


def _workspace_ref(session: dict[str, Any]) -> dict[str, Any]:
    value = session.get("workspace_ref")
    return value if isinstance(value, dict) else {}


class SessionRuntimeSubject:
    """Startup policy for a Session-owned Agent conversation runtime."""

    subject_kind: RuntimeSubjectKind = "session"
    recovery_action: RuntimeRecoveryAction = "recover_session_allocation"

    def __init__(self, *, runtime_manager: Any) -> None:
        self._runtime_manager = runtime_manager

    async def prepare_startup(self, session: dict[str, Any]) -> None:
        _ = session

    async def resolve_startup(
        self,
        *,
        session: dict[str, Any],
        template: Any,
        resume_engine_session_key: str | None,
    ) -> RuntimeStartupResolution:
        session_id = str(session.get("session_id") or "").strip()
        ref = _workspace_ref(session)
        agent_id = str(ref.get("agent_id") or session.get("agent_id") or "").strip()
        if not session_id or not agent_id:
            raise APIError(
                code="RUNTIME_SUBJECT_INVALID",
                message="Agent conversation runtime requires session_id and agent_id",
                status_code=500,
            )
        plan = self._runtime_manager.plan_agent_chat_runtime_start(
            session_id=session_id,
            agent_id=agent_id,
            template=template,
            resume_engine_session_key=resume_engine_session_key,
            existing_terminal_cwd=(str(session.get("terminal_cwd") or "").strip() or None),
            runtime_identity=(
                session.get("runtime_identity")
                if isinstance(session.get("runtime_identity"), dict)
                else None
            ),
        )
        return RuntimeStartupTarget(
            action="create_runtime",
            session=dict(session),
            workspace_plan=plan,
            persist_runtime_identity_on_session=True,
        )

    async def publish_runtime_ready(
        self,
        *,
        session: dict[str, Any],
        sandbox_id: str,
        expires_at: str | None,
        runtime_identity: dict[str, Any] | None,
    ) -> None:
        _ = (session, sandbox_id, expires_at, runtime_identity)

    async def converge_startup_failure(
        self,
        *,
        session: dict[str, Any],
        failure_phase: str,
        created_sandbox_id: str | None,
        cleanup: SandboxDestruction | None,
    ) -> bool:
        _ = (session, failure_phase, created_sandbox_id, cleanup)
        return False


class AssistantWorkspaceRuntimeSubject:
    """Startup policy for an Assistant workspace and its conversations."""

    subject_kind: RuntimeSubjectKind = "assistant_workspace"
    recovery_action: RuntimeRecoveryAction = "restart_session_on_subject"

    def __init__(
        self,
        *,
        runtime_manager: Any,
        sessions_repo: Any,
        assistant_workspace_service: Any,
        lifecycle_getter: Callable[[], AssistantWorkspaceLifecycle] | None,
    ) -> None:
        self._runtime_manager = runtime_manager
        self._sessions_repo = sessions_repo
        self._workspace_service = assistant_workspace_service
        self._lifecycle_getter = lifecycle_getter

    @staticmethod
    def _identity(session: dict[str, Any]) -> tuple[str, str, str]:
        ref = _workspace_ref(session)
        return (
            str(session.get("session_id") or "").strip(),
            str(ref.get("user_id") or session.get("user_id") or "").strip(),
            str(ref.get("assistant_id") or "").strip(),
        )

    async def prepare_startup(self, session: dict[str, Any]) -> None:
        if not (
            is_assistant_workspace_bootstrap(session)
            or is_assistant_user_conversation(session)
        ):
            raise APIError(
                code="RUNTIME_SUBJECT_INVALID",
                message=(
                    "Assistant runtime subject is neither a workspace materializer nor a conversation"
                ),
                status_code=500,
            )
        if self._lifecycle_getter is None:
            raise APIError(
                code="RUNTIME_SUBJECT_UNAVAILABLE",
                message="Assistant workspace lifecycle service is unavailable",
                status_code=500,
            )
        session_id, user_id, assistant_id = self._identity(session)
        if not user_id or not assistant_id:
            raise APIError(
                code="RUNTIME_SUBJECT_INVALID",
                message=("Assistant conversation runtime requires user_id and assistant_id"),
                status_code=500,
            )
        await self._lifecycle_getter().acquire_workspace_for_session(
            UserContext(user_id=user_id),
            assistant_id,
            provisioning_session_id=session_id,
            provisioning_sandbox_generation=str(session.get("sandbox_generation") or ""),
        )

    async def resolve_startup(
        self,
        *,
        session: dict[str, Any],
        template: Any,
        resume_engine_session_key: str | None,
    ) -> RuntimeStartupResolution:
        _ = resume_engine_session_key
        session_id, user_id, assistant_id = self._identity(session)
        engine_kind = resolve_session_engine_kind(session)
        if not session_id or not user_id or not assistant_id:
            raise APIError(
                code="RUNTIME_SUBJECT_INVALID",
                message=("Assistant runtime requires session_id, user_id and assistant_id"),
                status_code=500,
            )

        workspace = await self._workspace_service.get_workspace(
            user_id=user_id,
            assistant_id=assistant_id,
        )
        owns_materialization = (
            str((workspace or {}).get("state") or "").strip() == "MATERIALIZING"
            and str((workspace or {}).get("provisioning_session_id") or "").strip() == session_id
            and str((workspace or {}).get("provisioning_sandbox_generation") or "")
            == str(session.get("sandbox_generation") or "")
        )
        if owns_materialization:
            plan = self._runtime_manager.plan_assistant_runtime_start(
                user_id=user_id,
                assistant_id=assistant_id,
                runtime_key=session_id,
                template=template,
                engine_kind=engine_kind,
            )
            renewal_seconds = int(load_astrabox_settings().agent_sandbox_renew_ttl_seconds or 0)
            return RuntimeStartupTarget(
                action="create_runtime",
                session=dict(session),
                workspace_plan=plan,
                renewal_ttl_seconds=(renewal_seconds if renewal_seconds > 0 else None),
            )
        if is_assistant_workspace_bootstrap(session):
            raise APIError(
                code="RUNTIME_SUBJECT_MATERIALIZATION_LOST",
                message=(
                    "Assistant workspace materializer no longer owns the "
                    f"startup claim: session={session_id} assistant={assistant_id}"
                ),
                status_code=409,
            )

        reconciled, binding = await reconcile_session_runtime_binding(
            session=session,
            sessions_repo=self._sessions_repo,
            assistant_workspace_service=self._workspace_service,
            persist=True,
        )
        if not binding.can_dispatch:
            progress = None
            provisioning_session_id = str(
                (workspace or {}).get("provisioning_session_id") or ""
            ).strip()
            if provisioning_session_id:
                provisioning_session = await self._sessions_repo.get_session(
                    provisioning_session_id
                )
                progress = (
                    str((provisioning_session or {}).get("startup_progress") or "").strip() or None
                )
            return RuntimeStartupPending(
                session=reconciled,
                binding=binding,
                progress=progress,
            )

        sandbox_id = str(binding.sandbox_id or "").strip()
        plan = self._runtime_manager.plan_assistant_runtime_attach(
            user_id=user_id,
            assistant_id=assistant_id,
            runtime_key=session_id,
            sandbox_id=sandbox_id,
            existing_terminal_cwd=(str(reconciled.get("terminal_cwd") or "").strip() or None),
            engine_kind=binding.engine_kind,
        )
        marker = get_assistant_profile_ready_marker(
            workspace,
            user_id=user_id,
            assistant_id=assistant_id,
            sandbox_id=sandbox_id,
        )
        action: RuntimeStartupAction = (
            "use_ready_binding" if marker is not None else "attach_runtime"
        )
        return RuntimeStartupTarget(
            action=action,
            session=reconciled,
            workspace_plan=plan,
            binding_expires_at=binding.expires_at,
        )

    async def publish_runtime_ready(
        self,
        *,
        session: dict[str, Any],
        sandbox_id: str,
        expires_at: str | None,
        runtime_identity: dict[str, Any] | None,
    ) -> None:
        session_id, user_id, assistant_id = self._identity(session)
        if not session_id or not user_id or not assistant_id or not sandbox_id:
            raise APIError(
                code="ASSISTANT_PROFILE_MARKER_INVALID",
                message=(
                    "Assistant runtime publication requires session, user, "
                    "assistant and sandbox identities"
                ),
                status_code=500,
            )
        if self._lifecycle_getter is None:
            raise APIError(
                code="RUNTIME_SUBJECT_UNAVAILABLE",
                message="Assistant workspace lifecycle service is unavailable",
                status_code=500,
            )
        await self._lifecycle_getter().publish_workspace_runtime_ready(
            UserContext(user_id=user_id),
            assistant_id,
            provisioning_session_id=session_id,
            provisioning_sandbox_generation=str(session.get("sandbox_generation") or ""),
            sandbox_id=sandbox_id,
            expires_at=expires_at,
            runtime_identity=runtime_identity,
        )

    async def converge_startup_failure(
        self,
        *,
        session: dict[str, Any],
        failure_phase: str,
        created_sandbox_id: str | None,
        cleanup: SandboxDestruction | None,
    ) -> bool:
        session_id, user_id, assistant_id = self._identity(session)
        if not user_id or not assistant_id:
            return False
        if self._lifecycle_getter is None:
            return False
        return bool(
            await self._lifecycle_getter().converge_workspace_startup_failure(
                UserContext(user_id=user_id),
                assistant_id,
                provisioning_session_id=session_id,
                provisioning_sandbox_generation=str(session.get("sandbox_generation") or ""),
                failure_phase=failure_phase,
                created_sandbox_id=created_sandbox_id,
                cleanup=cleanup,
            )
        )


class RuntimeSubjectCoordinator:
    """Select one runtime owner and drive its startup readiness action."""

    def __init__(
        self,
        *,
        runtime_manager: Any,
        sessions_repo: Any,
        assistant_workspace_service: Any,
        assistant_lifecycle_getter: (Callable[[], AssistantWorkspaceLifecycle] | None),
        ready_timeout_seconds: float = _RUNTIME_SUBJECT_READY_TIMEOUT_SECONDS,
        ready_poll_seconds: float = _RUNTIME_SUBJECT_READY_POLL_SECONDS,
    ) -> None:
        self._runtime_manager = runtime_manager
        self._sessions_repo = sessions_repo
        self._ready_timeout_seconds = max(0.01, float(ready_timeout_seconds))
        self._ready_poll_seconds = max(0.001, float(ready_poll_seconds))
        providers: tuple[RuntimeSubjectProvider, ...] = (
            SessionRuntimeSubject(runtime_manager=runtime_manager),
            AssistantWorkspaceRuntimeSubject(
                runtime_manager=runtime_manager,
                sessions_repo=sessions_repo,
                assistant_workspace_service=assistant_workspace_service,
                lifecycle_getter=assistant_lifecycle_getter,
            ),
        )
        self._providers = {provider.subject_kind: provider for provider in providers}

    def provider_for(self, session: dict[str, Any]) -> RuntimeSubjectProvider:
        try:
            subject_kind = runtime_subject_kind(session)
        except ValueError as exc:
            ref = _workspace_ref(session)
            raise APIError(
                code="RUNTIME_SUBJECT_INVALID",
                message=(
                    "Session must resolve to one runtime subject "
                    f"(session_kind={session.get('session_kind')!r}, "
                    f"workspace_kind={ref.get('kind')!r})"
                ),
                status_code=500,
            ) from exc
        return self._providers[subject_kind]

    def recovery_action_for(self, session: dict[str, Any]) -> RuntimeRecoveryAction:
        """Return the recovery action allowed by the allocation owner.

        A Session-owned allocation may be reattached or released through the
        Session runtime manager. A Session bound to a longer-lived subject must
        instead re-enter startup; that subject remains the only authority that
        may reuse, resume, or destroy its physical sandbox.
        """

        return self.provider_for(session).recovery_action

    async def acquire_startup(
        self,
        *,
        session_id: str,
        template: Any,
        sandbox_generation: str | None,
        resume_engine_session_key: str | None,
        on_progress: Callable[[str], Awaitable[None]] | None,
    ) -> RuntimeStartupTarget:
        session = await self._sessions_repo.get_session(session_id)
        if not isinstance(session, dict):
            raise APIError(
                code="RUNTIME_SUBJECT_INVALID",
                message=f"Session disappeared before runtime acquisition: {session_id}",
                status_code=500,
            )
        if (
            sandbox_generation is not None
            and session.get("sandbox_generation") != sandbox_generation
        ):
            raise APIError(
                code="RUNTIME_RECOVERY_SUPERSEDED",
                message="startup generation changed before acquisition",
                status_code=409,
            )
        provider = self.provider_for(session)
        # Where a conversation's startup spends its seconds, on the same shape
        # the turn path already reports. Without it the platform can say a box
        # took twenty-four seconds and nothing about which part did: five
        # concurrent creations that each answer in ten alone are a queue
        # somewhere, and a queue is invisible to a total.
        started_mono = time.monotonic()
        await provider.prepare_startup(session)
        prepared_ms = round((time.monotonic() - started_mono) * 1000, 3)
        attempts = 0
        last_progress: str | None = None

        deadline = time.monotonic() + self._ready_timeout_seconds
        while True:
            attempts += 1
            current = await self._sessions_repo.get_session(session_id)
            if not isinstance(current, dict):
                raise APIError(
                    code="RUNTIME_SUBJECT_INVALID",
                    message=(f"Session disappeared during runtime acquisition: {session_id}"),
                    status_code=500,
                )
            if (
                sandbox_generation is not None
                and current.get("sandbox_generation") != sandbox_generation
            ):
                raise APIError(
                    code="RUNTIME_RECOVERY_SUPERSEDED",
                    message="startup generation changed during acquisition",
                    status_code=409,
                )
            if self.provider_for(current).subject_kind != provider.subject_kind:
                raise APIError(
                    code="RUNTIME_SUBJECT_CHANGED",
                    message=f"Session changed runtime subject during startup: {session_id}",
                    status_code=409,
                )
            resolution = await provider.resolve_startup(
                session=current,
                template=template,
                resume_engine_session_key=resume_engine_session_key,
            )
            if isinstance(resolution, RuntimeStartupTarget):
                logger.info(
                    "startup_latency_observation session=%s subject=%s "
                    "prepare_ms=%s resolve_attempts=%s last_progress=%s "
                    "total_elapsed_ms=%s",
                    session_id,
                    provider.subject_kind,
                    prepared_ms,
                    attempts,
                    last_progress,
                    round((time.monotonic() - started_mono) * 1000, 3),
                )
                return resolution

            binding = resolution.binding
            if binding.status != "PROVISIONING":
                raise APIError(
                    code=binding.reason_code or "RUNTIME_SUBJECT_INVALID",
                    message=(
                        binding.reason_message
                        or "Runtime subject did not provide a dispatchable binding"
                    ),
                    status_code=409,
                    data={
                        "authority_kind": binding.authority_kind,
                        "authority_id": binding.authority_id,
                        "authority_state": binding.authority_state,
                    },
                )
            last_progress = resolution.progress or "waiting_for_startup_lease"
            if on_progress is not None:
                await on_progress(last_progress)
            if time.monotonic() >= deadline:
                raise APIError(
                    code="RUNTIME_SUBJECT_STARTUP_TIMEOUT",
                    message=(
                        f"Runtime subject {binding.authority_id} did not become "
                        f"ready within {self._ready_timeout_seconds:.0f}s"
                    ),
                    status_code=504,
                    data={
                        "authority_kind": binding.authority_kind,
                        "authority_id": binding.authority_id,
                        "authority_state": binding.authority_state,
                    },
                )
            await asyncio.sleep(self._ready_poll_seconds)

    async def publish_runtime_ready(
        self,
        *,
        session: dict[str, Any],
        sandbox_id: str,
        expires_at: str | None,
        runtime_identity: dict[str, Any] | None,
    ) -> None:
        await self.provider_for(session).publish_runtime_ready(
            session=session,
            sandbox_id=sandbox_id,
            expires_at=expires_at,
            runtime_identity=runtime_identity,
        )

    async def converge_startup_failure(
        self,
        *,
        session: dict[str, Any],
        failure_phase: str,
        created_sandbox_id: str | None = None,
        cleanup: SandboxDestruction | None = None,
    ) -> bool:
        return await self.provider_for(session).converge_startup_failure(
            session=session,
            failure_phase=failure_phase,
            created_sandbox_id=created_sandbox_id,
            cleanup=cleanup,
        )

    async def cleanup_failed_startup_runtime(
        self,
        *,
        session_id: str,
        target: RuntimeStartupTarget | None,
        sandbox_id: str | None,
    ) -> RuntimeStartupCleanup:
        """Release only a runtime allocation this startup was elected to create.

        Attach failures never destroy a shared owner's sandbox. Creation
        failures are cleaned up here, before the provider converges its durable
        owner row, so allocation release and sandbox-disposal sequencing are
        common across subjects.
        """

        if target is None or target.action != "create_runtime":
            return RuntimeStartupCleanup(
                destruction=None,
                leaked_sandbox_id=None,
            )
        resolved_sandbox_id = str(sandbox_id or "").strip() or None
        try:
            cleanup = await self._runtime_manager.cleanup_startup_allocation(
                session_id,
                fallback_sandbox_id=resolved_sandbox_id,
            )
        except BaseException as exc:
            destruction = SandboxDestruction.unconfirmed(
                resolved_sandbox_id or "",
                detail=f"startup cleanup raised: {exc}",
            )
            leaked_sandbox_id = destruction.leaked_sandbox_id
        else:
            destruction = cleanup.destruction
            leaked_sandbox_id = cleanup.leaked_sandbox_id
        return RuntimeStartupCleanup(
            destruction=destruction,
            leaked_sandbox_id=leaked_sandbox_id,
        )
