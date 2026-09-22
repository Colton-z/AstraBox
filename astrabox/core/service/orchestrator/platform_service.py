"""Thin facade that composes SessionService, TurnService, TerminalService, AdminService.

All business logic lives in the sub-services. This file only wires them
together and delegates every public method, so a caller holding this facade
never needs to know which sub-service answers.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import inspect
import json
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrabox.core.service.orchestrator.service_factories import (
        DeploymentServiceReplacement,
    )

from astrabox.persistence.repository import (
    AgentRepository,
    DeploymentRepository,
    ArtifactRepository,
    EnvironmentRepository,
    InteractionSnapshotRepository,
    MessageRepository,
    PlatformMCPBindingRepository,
    SessionRepository,
    SessionEventRepository,
    SessionSnapshotRepository,
    TranscriptEntryRepository,
)
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.event_broker import SessionEventBroker
from astrabox.core.service.orchestrator.vault_service import VaultService
from astrabox.seams.sandbox import sandbox_for_template
from astrabox.core.service.orchestrator.assistant.assistant_workspace_service import (
    AssistantWorkspaceService,
)
from astrabox.core.service.orchestrator.bootstrap_reconciler import BootstrapReconciler
from astrabox.core.service.orchestrator.expiration_watcher import (
    ExpirationWatcher,
)
from astrabox.core.service.orchestrator.expose_port_service import ExposePortService
from astrabox.core.service.orchestrator.sandbox_lifecycle import (
    SandboxLifecycleService,
    terminal_session_updates,
)
from astrabox.core.service.orchestrator.platform_mcp_service import PlatformMCPService
from astrabox.core.service.orchestrator.runtime_manager import RemoteAgentRuntimeManager
from astrabox.core.service.orchestrator.runtime_subject import (
    RuntimeSubjectCoordinator,
)
from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService
from astrabox.core.service.orchestrator.engine.base import (
    bound_engine_client_manifest,
)

from astrabox.core.service.orchestrator.session_service import SessionService
from astrabox.core.service.orchestrator.session_message_view import SessionMessageView
from astrabox.core.service.orchestrator.session_public_projection import (
    project_owner_session,
    project_public_message_page,
)
from astrabox.core.service.orchestrator.session_share_service import SessionShareService
from astrabox.core.service.orchestrator.session_title_service import SessionTitleService
from astrabox.core.service.orchestrator.session_kernel.service import (
    SessionKernelService,
)
from astrabox.core.service.orchestrator.turn_service import TurnService
from astrabox.core.service.orchestrator.terminal_service import TerminalService
from astrabox.core.service.orchestrator.admin_service import AdminService
from astrabox.core.service.orchestrator.channel_ingress_service import (
    ChannelIngressService,
)
from astrabox.core.service.orchestrator.channel_credentials import (
    ChannelCredentialService,
)
from astrabox.core.service.orchestrator.channel_source_host import (
    ChannelSourceHost,
)
from astrabox.core.service.orchestrator.channel_spine_reconciler import (
    ChannelSpineReconciler,
)
from astrabox.core.service.orchestrator.session_file_service import SessionFileService
from astrabox.core.service.orchestrator.deployment_service import DeploymentService

logger = get_logger(__name__)


class AgentPlatformService:
    def __init__(
        self,
        *,
        agent_service_getter: Callable[[], Any] | None = None,
        assistant_lifecycle_getter: Callable[[], Any] | None = None,
    ) -> None:
        self._agent_service_getter = agent_service_getter
        from astrabox.core.service.orchestrator.service_factories import (
            load_service_factory_overrides,
        )

        self._service_factory_overrides = load_service_factory_overrides()
        self._build_repositories()
        settings = load_astrabox_settings()
        self._build_core_singletons(settings)
        self._build_sub_services(agent_service_getter, assistant_lifecycle_getter)
        self._build_admin_and_deployments()

    def _construct(self, name: str, build_default: Callable[[], Any]) -> Any:
        """Build sub-service ``name`` — through its deployment override if one
        is registered at the ``astrabox.service_factories`` entry-point group
        (see :mod:`astrabox.core.service.orchestrator.service_factories`),
        else the stock implementation."""
        override = self._service_factory_overrides.get(name)
        return override(build_default) if override is not None else build_default()

    def _build_repositories(self) -> None:
        self._environment_repo = EnvironmentRepository()
        self._sessions_repo = SessionRepository()
        self._messages_repo = MessageRepository()
        self._session_events_repo = SessionEventRepository()
        self._session_message_view = SessionMessageView(self._session_events_repo)
        self._session_snapshots_repo = SessionSnapshotRepository()
        self._interaction_snapshots_repo = InteractionSnapshotRepository()
        self._artifacts_repo = ArtifactRepository()
        self._transcript_entries_repo = TranscriptEntryRepository()
        self._agent_repo = AgentRepository()
        self._platform_mcp_binding_repo = PlatformMCPBindingRepository()
        self._assistant_workspace_service = AssistantWorkspaceService()

    def _build_core_singletons(self, settings: Any) -> None:
        self._agent_config = AgentConfigService(
            self._agent_repo,
            self._environment_repo,
            spawn_background_task=self._spawn_background_task,
        )
        self._vault_service = VaultService()
        self._broker = SessionEventBroker()
        self._runtime_manager = RemoteAgentRuntimeManager(
            sessions_repo=self._sessions_repo,
            agent_service_getter=self._agent_service_getter,
            event_broker=self._broker,
        )
        self._sandbox_lifecycle_service = SandboxLifecycleService(platform_service=self)

        self._ttl_seconds = settings.session_ttl_seconds
        self._bootstrapped = False
        self._session_list_bootstrapped = False
        self._quiesced_reason: str | None = None
        self._background_tasks: set[asyncio.Task] = set()
        self._bootstrap_lock = asyncio.Lock()
        self._supports_task_context = "context" in inspect.signature(asyncio.create_task).parameters
        self._expiration_watcher = ExpirationWatcher(platform_service=self)
        self._bootstrap_reconciler = BootstrapReconciler(platform_service=self)

    # ── Sub-services ─────────────────────────────────────────────────────
    #
    # Every construction below routes through _construct(), so a deployment
    # can wrap or replace any sub-service via the astrabox.service_factories
    # entry-point group without forking this file. Construction order is part
    # of the contract (later services receive earlier ones).
    def _build_sub_services(
        self,
        agent_service_getter: Callable[[], Any] | None,
        assistant_lifecycle_getter: Callable[[], Any] | None,
    ) -> None:
        self._session_service = self._construct(
            "session_service",
            lambda: SessionService(
                sessions_repo=self._sessions_repo,
                messages_repo=self._messages_repo,
                agent_config=self._agent_config,
                runtime_manager=self._runtime_manager,
                broker=self._broker,
                ttl_seconds=self._ttl_seconds,
                spawn_background_task=self._spawn_background_task,
                vault_service=self._vault_service,
                assistant_workspace_service=self._assistant_workspace_service,
            ),
        )
        self._session_service._session_snapshots_repo = self._session_snapshots_repo
        self._session_service._interaction_snapshots_repo = self._interaction_snapshots_repo

        self._turn_service = self._construct(
            "turn_service",
            lambda: TurnService(
                sessions_repo=self._sessions_repo,
                messages_repo=self._messages_repo,
                agent_config=self._agent_config,
                runtime_manager=self._runtime_manager,
                broker=self._broker,
                must_get_owned_session=self._session_service.must_get_owned_session,
                interaction_snapshots_repo=self._interaction_snapshots_repo,
                session_snapshots_repo=self._session_snapshots_repo,
                session_events_repo=self._session_events_repo,
                message_view=self._session_message_view,
                agent_repo=self._agent_repo,
                agent_service_getter=agent_service_getter,
                assistant_workspace_service=self._assistant_workspace_service,
                sandbox_lifecycle_service=self._sandbox_lifecycle_service,
            ),
        )
        self._terminal_service = self._construct(
            "terminal_service",
            lambda: TerminalService(
                runtime_manager=self._runtime_manager,
                must_get_owned_session=self._session_service.must_get_owned_session,
                sessions_repo=self._sessions_repo,
                assistant_workspace_service=self._assistant_workspace_service,
            ),
        )
        self._session_file_service = self._construct(
            "session_file_service",
            lambda: SessionFileService(
                sessions_repo=self._sessions_repo,
                session_snapshots_repo=self._session_snapshots_repo,
                agent_repo=self._agent_repo,
                runtime_manager=self._runtime_manager,
                assistant_workspace_service=self._assistant_workspace_service,
            ),
        )
        self._expose_port_service = self._construct(
            "expose_port_service",
            lambda: ExposePortService(
                sessions_repo=self._sessions_repo,
                binding_repo=self._platform_mcp_binding_repo,
                assistant_workspace_service=self._assistant_workspace_service,
            ),
        )
        self._platform_mcp_service = self._construct(
            "platform_mcp_service",
            lambda: PlatformMCPService(
                sessions_repo=self._sessions_repo,
                agent_config=self._agent_config,
                binding_repo=self._platform_mcp_binding_repo,
                expose_port_service=self._expose_port_service,
            ),
        )
        self._session_title_service = self._construct(
            "session_title_service",
            lambda: SessionTitleService(
                sessions_repo=self._sessions_repo,
                message_view=self._session_message_view,
            ),
        )
        self._runtime_subjects = RuntimeSubjectCoordinator(
            runtime_manager=self._runtime_manager,
            sessions_repo=self._sessions_repo,
            assistant_workspace_service=self._assistant_workspace_service,
            assistant_lifecycle_getter=assistant_lifecycle_getter,
        )
        self._session_kernel = self._construct(
            "session_kernel",
            lambda: SessionKernelService(
                sessions_repo=self._sessions_repo,
                turn_service=self._turn_service,
                session_service=self._session_service,
                terminal_service=self._terminal_service,
                runtime_manager=self._runtime_manager,
                broker=self._broker,
                session_events_repo=self._session_events_repo,
                session_snapshots_repo=self._session_snapshots_repo,
                interaction_snapshots_repo=self._interaction_snapshots_repo,
                message_view=self._session_message_view,
                artifacts_repo=self._artifacts_repo,
                transcript_entries_repo=self._transcript_entries_repo,
                agent_repo=self._agent_repo,
                assistant_workspace_service=self._assistant_workspace_service,
                runtime_subjects=self._runtime_subjects,
                title_service=self._session_title_service,
                must_get_owned_session=self._session_service.must_get_owned_session,
                spawn_background_task=self._spawn_background_task,
            ),
        )
        self._session_share_service = self._construct(
            "session_share_service",
            lambda: SessionShareService(
                sessions_repo=self._sessions_repo,
                session_service=self._session_service,
                session_kernel=self._session_kernel,
                session_file_service=self._session_file_service,
            ),
        )

    def _build_admin_and_deployments(self) -> None:
        from astrabox.core.service.orchestrator.service_factories import (
            DeploymentServiceReplacement,
        )

        self._admin_service = self._construct(
            "admin_service",
            lambda: AdminService(
                sessions_repo=self._sessions_repo,
                message_view=self._session_message_view,
                session_events_repo=self._session_events_repo,
                agent_config=self._agent_config,
                runtime_manager=self._runtime_manager,
                sanitize_session=SessionService._sanitize_session,
            ),
        )

        self._agent_deployment_repo = DeploymentRepository()
        self._channel_credentials = ChannelCredentialService()
        self._channel_ingress_service = ChannelIngressService(
            deployment_repo=self._agent_deployment_repo,
            agent_repo=self._agent_repo,
            agent_service_getter=self._agent_service_getter,
            stream_message_events_ds=self.stream_message_events_ds,
            resume_command_stream=self.resume_command_stream,
            sessions_repo=self._sessions_repo,
            session_events_repo=self._session_events_repo,
            message_view=self._session_message_view,
            session_detail_getter=self.get_session,
            supersede_pending_interaction=self.supersede_pending_interaction,
            spawn_background_task=self._spawn_background_task,
            channel_credentials=self._channel_credentials,
        )
        self._channel_spine_reconciler = ChannelSpineReconciler(
            ingress_service=self._channel_ingress_service,
            spawn_background_task=self._spawn_background_task,
        )
        self._channel_source_host = ChannelSourceHost(
            ingress_service=self._channel_ingress_service,
            deployment_repo=self._agent_deployment_repo,
            spawn_background_task=self._spawn_background_task,
        )
        deployment_service = self._construct(
            "deployment_service",
            lambda: DeploymentService(
                deployment_repo=self._agent_deployment_repo,
                agent_repo=self._agent_repo,
                agent_service_getter=self._agent_service_getter,
                stream_message_events_ds=self.stream_message_events_ds,
                dispatch_turn_input=self.dispatch_turn_input,
                sessions_repo=self._sessions_repo,
                spawn_background_task=self._spawn_background_task,
                agent_config=self._agent_config,
                channel_ingress=self._channel_ingress_service,
                channel_credentials=self._channel_credentials,
                session_snapshots_repo=self._session_snapshots_repo,
                session_events_repo=self._session_events_repo,
            ),
        )
        if not isinstance(deployment_service, DeploymentServiceReplacement):
            raise RuntimeError(
                "deployment_service replacement does not satisfy "
                "DeploymentServiceReplacement (including deployment-scoped "
                "update/delete methods)"
            )
        self._deployment_service = deployment_service

    @property
    def deployment_service(self) -> "DeploymentServiceReplacement":
        return self._deployment_service

    @property
    def channel_ingress_service(self) -> ChannelIngressService:
        """The channel spine's single typed ingress (docs/channel-spine.md)."""
        return self._channel_ingress_service

    @property
    def channel_spine_reconciler(self) -> ChannelSpineReconciler:
        return self._channel_spine_reconciler

    # ── Background task helper ───────────────────────────────────────────

    def close(self) -> None:
        self.quiesce(reason="lifecycle_cleanup")

    def quiesce(self, *, reason: str) -> None:
        if self._quiesced_reason:
            logger.info(
                "agent platform already closing for shutdown: previous=%s current=%s",
                self._quiesced_reason,
                reason,
            )
            return
        self._quiesced_reason = str(reason or "shutdown").strip() or "shutdown"
        self._bootstrapped = False
        self._session_list_bootstrapped = False
        logger.warning(
            "closing agent platform service for shutdown: reason=%s",
            self._quiesced_reason,
        )
        for task in list(getattr(self, "_background_tasks", set())):
            if not task.done():
                task.cancel("shutdown")
        self._session_kernel.quiesce(reason=self._quiesced_reason)
        self._runtime_manager.quiesce(reason=self._quiesced_reason)
        with contextlib.suppress(Exception):
            self._expiration_watcher.quiesce()
        with contextlib.suppress(Exception):
            self._channel_spine_reconciler.quiesce()
        with contextlib.suppress(Exception):
            self._channel_source_host.quiesce()

    async def wait_closed(self) -> None:
        """Await cancelled platform tasks while their database is still available."""
        tasks = tuple(self._background_tasks)
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for task, result in zip(tasks, results, strict=True):
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                logger.error(
                    "platform task shutdown failed: task=%s error=%s",
                    task.get_name(), result,
                    exc_info=(type(result), result, result.__traceback__),
                )

    def _raise_if_quiesced(self) -> None:
        reason = str(self._quiesced_reason or "").strip()
        if not reason:
            return
        raise APIError(
            code="ASTRABOX_RELEASING",
            message=f"agent platform service is closing for shutdown: {reason}",
            status_code=503,
        )

    def _spawn_background_task(
        self,
        coro,
        *,
        name: str | None = None,
    ) -> asyncio.Task:
        if self._quiesced_reason:
            if inspect.iscoroutine(coro):
                with contextlib.suppress(Exception):
                    coro.close()
        self._raise_if_quiesced()
        kwargs: dict[str, Any] = {}
        if name:
            kwargs["name"] = name
        supports_task_context = getattr(self, "_supports_task_context", None)
        if supports_task_context is None:
            supports_task_context = "context" in inspect.signature(asyncio.create_task).parameters
            self._supports_task_context = supports_task_context
        if supports_task_context:
            # Detach from request-scoped cancel/user context so the background
            # task is not cancelled when the request ends.
            kwargs["context"] = contextvars.Context()
        task = asyncio.create_task(coro, **kwargs)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    # ── Bootstrap (delegated to bootstrap_reconciler) ─────────────────────

    async def ensure_bootstrap(self) -> None:
        await self._bootstrap_reconciler.ensure_bootstrap()
        if self._bootstrapped:
            self._session_list_bootstrapped = True

    async def _ensure_session_list_bootstrap(self) -> None:
        """Start list-read prerequisites without the global session reconcile."""
        self._raise_if_quiesced()
        self._session_kernel.ensure_background_tasks_started()
        if not self._bootstrapped and not self._session_list_bootstrapped:
            async with self._bootstrap_lock:
                self._raise_if_quiesced()
                self._session_kernel.ensure_background_tasks_started()
                if not self._bootstrapped and not self._session_list_bootstrapped:
                    await self._session_kernel.ensure_bootstrap()
                    await self._sessions_repo.ensure_indexes()
                    self._session_list_bootstrapped = True
        self._expiration_watcher.ensure_started()

    async def _sync_lifecycle_projection_from_session(
        self,
        *,
        session: dict[str, Any],
        session_id: str,
        updates: dict[str, Any],
        reason: str,
    ) -> None:
        # sandbox_lifecycle.py's SandboxLifecycleService (constructed with
        # platform_service=self, same as ExpirationWatcher/BootstrapReconciler)
        # reaches into self._platform._sync_lifecycle_projection_from_session
        # directly, so this name must stay resolvable on the facade; it
        # delegates to bootstrap_reconciler.
        await self._bootstrap_reconciler._sync_lifecycle_projection_from_session(
            session=session,
            session_id=session_id,
            updates=updates,
            reason=reason,
        )

    @property
    def platform_mcp(self) -> PlatformMCPService:
        return self._platform_mcp_service

    async def resolve_platform_mcp_server(
        self,
        deployment_id: str,
        server_name: str,
    ) -> dict[str, Any] | None:
        return await self._platform_mcp_service.resolve_server(deployment_id, server_name)

    async def refresh_exposed_port_url(
        self,
        *,
        deployment_id: str,
        port: int,
        user: UserContext,
    ) -> str:
        return await self._expose_port_service.refresh_port_url(
            deployment_id=deployment_id,
            port=port,
            user=user,
        )

    # ── Agent config management (delegated to agent_config) ──────────────

    async def list_agent_configs(self, user: UserContext) -> list[dict[str, Any]]:
        return await self._agent_config.list_agent_configs(user)

    async def create_agent_config(
        self, user: UserContext, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._agent_config.create_agent_config(user, payload)

    async def upsert_agent_config(
        self,
        user: UserContext,
        agent_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._agent_config.upsert_agent_config(user, agent_id, payload)

    async def list_environment_configs(self, user: UserContext) -> list[dict[str, Any]]:
        return await self._agent_config.list_environment_configs(user)

    async def list_agent_environment_options(self) -> list[dict[str, Any]]:
        return await self._agent_config.list_agent_environment_options()

    async def list_agent_environment_models(self, name: str) -> list[str]:
        return await self._agent_config.list_agent_environment_models(name)

    async def list_environment_models(self, name: str) -> list[str]:
        return await self._agent_config.list_environment_models(name)

    async def upsert_environment_config(
        self,
        user: UserContext,
        name: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        result = await self._agent_config.upsert_environment_config(user, name, payload)
        if self._agent_service_getter is not None:
            try:
                scheduled = await self._agent_service_getter().reconcile_environment_runtimes(
                    name
                )
                if scheduled:
                    logger.info(
                        "scheduled Agent runtime reconciliation after Environment update: "
                        "environment=%s agents=%s",
                        name,
                        scheduled,
                    )
            except Exception as exc:
                # The Environment write has committed. Prepared capacity is an
                # optimization and authoritative Session startup resolves the
                # same generation, so a transient controller failure must not
                # turn this into a misleading failed write.
                logger.warning(
                    "could not schedule Agent runtime reconciliation after "
                    "Environment update: "
                    "environment=%s error=%s",
                    name,
                    exc,
                    exc_info=exc,
                )
        return result

    # ── Session lifecycle ────────────────────────────────────────────────

    async def list_sessions(self, user: UserContext) -> list[dict[str, Any]]:
        await self.ensure_bootstrap()
        sessions = await self._session_kernel.list_sessions(user)
        return [project_owner_session(session) for session in sessions]

    async def list_sessions_page(
        self,
        user: UserContext,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        await self._ensure_session_list_bootstrap()
        page = await self._session_kernel.list_sessions_page(
            user,
            limit=limit,
            cursor=cursor,
        )
        return {
            "sessions": [
                project_owner_session(session)
                for session in page.get("sessions") or []
            ],
            "has_more": bool(page.get("has_more")),
            "next_cursor": page.get("next_cursor"),
        }

    async def get_session(self, user: UserContext, session_id: str) -> dict[str, Any]:
        await self.ensure_bootstrap()
        session = await self._session_service.must_get_owned_session(user, session_id)
        rendered = await self._session_kernel.get_session(
            user,
            session_id,
            session=session,
        )
        rendered.setdefault("engine_capabilities", None)
        # Which workspace surfaces this Agent declares. Platform vocabulary,
        # deliberately beside `engine_capabilities` rather than inside it: the
        # engine has no opinion about what a console shows, and folding a
        # platform choice into the engine's manifest is the drift that rule
        # guards against. Read live from the Agent rather than frozen with the
        # runtime — it changes nothing a running sandbox depends on, so it must
        # not need a rebuild to take effect.
        rendered["workspace_panels"] = await self._declared_workspace_panels(session)
        runtime = self._runtime_manager.get_runtime(
            session_id,
            sandbox_id=str(session.get("sandbox_id") or "").strip() or None,
        )
        if runtime is None:
            return project_owner_session(rendered)

        # Engine vocabulary crosses this boundary without a platform-owned
        # translation table. The manifest was bound before the runtime became
        # visible, so rendering does not depend on a healthy live transport.
        try:
            manifest = bound_engine_client_manifest(runtime)
        except TypeError as exc:
            raise APIError(
                code="ENGINE_CAPABILITY_CONTRACT_VIOLATION",
                message=str(exc),
                status_code=502,
            ) from exc
        rendered["engine_capabilities"] = asdict(manifest)
        return project_owner_session(rendered)

    async def _declared_workspace_panels(
        self, session: dict[str, Any]
    ) -> dict[str, bool]:
        """The panels this session's Agent declares, defaulting to none.

        An Assistant has no Agent record to declare them, and a Session whose
        Agent was deleted must still render; both resolve to nothing declared
        rather than to an error, because a missing declaration is the same
        answer as a declaration of `false`.
        """

        agent_id = str(session.get("agent_id") or "").strip()
        agent = (
            await self._agent_config.resolve_agent_harness(agent_id)
            if agent_id
            else None
        )
        return {
            "terminal": bool(agent is not None and agent.terminal_panel),
            "diff": bool(agent is not None and agent.diff_panel),
        }

    async def get_messages(
        self, user: UserContext, session_id: str, *, before: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        await self.ensure_bootstrap()
        session = await self._session_service.must_get_owned_session(user, session_id)
        page = await self._session_kernel.get_messages(
            user,
            session_id,
            before=before,
            limit=limit,
            session=session,
        )
        return project_public_message_page(page)

    async def get_history_blocks(
        self,
        user: UserContext,
        session_id: str,
        *,
        before: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        await self.ensure_bootstrap()
        session = await self._session_service.must_get_owned_session(user, session_id)
        page = await self._session_kernel.get_history_blocks(
            user,
            session_id,
            before=before,
            limit=limit,
            session=session,
        )
        return project_public_message_page(page)

    async def get_history_block_details(
        self,
        user: UserContext,
        session_id: str,
        block_id: str,
        *,
        cursor: str,
    ) -> dict[str, Any]:
        await self.ensure_bootstrap()
        session = await self._session_service.must_get_owned_session(user, session_id)
        page = await self._session_kernel.get_history_block_details(
            user,
            session_id,
            block_id=block_id,
            cursor=cursor,
            session=session,
        )
        return {
            "messages": project_public_message_page(page)["messages"],
            "has_more": bool(page.get("has_more")),
        }

    async def generate_process_summary(
        self,
        user: UserContext,
        session_id: str,
        message_id: str,
        *,
        retry_failed: bool = False,
    ) -> dict[str, Any]:
        await self.ensure_bootstrap()
        session = await self._session_service.must_get_owned_session(user, session_id)
        row = await self._session_kernel.generate_process_summary(
            user,
            session_id,
            message_id,
            retry_failed=retry_failed,
            session=session,
        )
        # The stored row also carries the checkpoint the label was written
        # against, which is this package's bookkeeping and not part of the
        # answer a reader asked for.
        return {
            "status": str(row.get("status") or ""),
            "summary": row.get("summary"),
            "error": row.get("error"),
            "turn_completed": row.get("turn_completed"),
        }

    async def list_session_child_runs(
        self,
        user: UserContext,
        session_id: str,
    ) -> dict[str, Any]:
        await self.ensure_bootstrap()
        session = await self._session_service.must_get_owned_session(user, session_id)
        return await self._session_kernel.list_child_runs(
            user,
            session_id,
            session=session,
        )

    async def get_session_child_run_messages(
        self,
        user: UserContext,
        session_id: str,
        child_run_id: str,
    ) -> dict[str, Any]:
        await self.ensure_bootstrap()
        session = await self._session_service.must_get_owned_session(user, session_id)
        return await self._session_kernel.get_child_run_messages(
            user,
            session_id,
            child_run_id,
            session=session,
        )

    async def stop_session_child_run(
        self,
        user: UserContext,
        session_id: str,
        child_run_id: str,
    ) -> dict[str, Any]:
        await self.ensure_bootstrap()
        return await self._session_kernel.stop_child_run(
            user,
            session_id,
            child_run_id,
        )

    # ── Session sharing (delegated to session_share_service) ─────────────────
    async def create_session_share(
        self,
        user: UserContext,
        session_id: str,
        *,
        expires_in_seconds: int | None = None,
        allow_download: bool = False,
    ) -> dict[str, Any]:
        await self.ensure_bootstrap()
        return await self._session_share_service.create_session_share(
            user,
            session_id,
            expires_in_seconds=expires_in_seconds,
            allow_download=allow_download,
        )

    async def revoke_session_share(self, user: UserContext, session_id: str) -> dict[str, Any]:
        await self.ensure_bootstrap()
        return await self._session_share_service.revoke_session_share(user, session_id)

    async def get_session_share(self, user: UserContext, session_id: str) -> dict[str, Any]:
        await self.ensure_bootstrap()
        return await self._session_share_service.get_session_share(user, session_id)

    async def get_shared_session(self, token: str) -> dict[str, Any]:
        return await self._session_share_service.get_shared_session(token)

    async def get_shared_messages(
        self, token: str, *, before: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        return await self._session_share_service.get_shared_messages(
            token, before=before, limit=limit
        )

    async def list_shared_files(self, token: str, *, path: str | None = None) -> dict[str, Any]:
        return await self._session_share_service.list_shared_files(token, path=path)

    async def get_shared_history_blocks(
        self, token: str, *, before: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        return await self._session_share_service.get_shared_history_blocks(
            token, before=before, limit=limit
        )

    async def get_shared_history_block_details(
        self, token: str, block_id: str, *, cursor: str
    ) -> dict[str, Any]:
        return await self._session_share_service.get_shared_history_block_details(
            token, block_id, cursor=cursor
        )

    async def download_shared_file(self, token: str, *, path: str) -> tuple[bytes, str]:
        return await self._session_share_service.download_shared_file(token, path=path)

    async def update_session_permission_mode(
        self,
        user: UserContext,
        session_id: str,
        permission_mode: str,
    ) -> dict[str, Any]:
        await self.ensure_bootstrap()
        session = await self._session_service.must_get_owned_session(user, session_id)
        return await self._session_kernel.update_session_permission_mode(
            user,
            session_id,
            permission_mode,
        )

    async def must_own_session(self, user: UserContext, session_id: str) -> dict[str, Any]:
        await self.ensure_bootstrap()
        return await self._session_service.must_get_owned_session(user, session_id)

    async def delete_session(self, user: UserContext, session_id: str) -> dict[str, Any]:
        await self.ensure_bootstrap()
        session = await self._session_service.must_get_owned_session(user, session_id)
        return await self._session_kernel.delete_session(user, session_id)

    async def archive_session(self, user: UserContext, session_id: str) -> dict[str, Any]:
        await self.ensure_bootstrap()
        session = await self._session_service.must_get_owned_session(user, session_id)
        return await self._session_kernel.archive_session(user, session_id)

    async def terminate_sandbox(self, user: UserContext, session_id: str) -> dict[str, Any]:
        await self.ensure_bootstrap()
        session = await self._session_service.must_get_owned_session(user, session_id)
        return await self._session_kernel.terminate_sandbox(user, session_id)

    async def end_conversation(self, user: UserContext, session_id: str) -> dict[str, Any]:
        await self.ensure_bootstrap()
        session = await self._session_service.must_get_owned_session(user, session_id)
        return await self._session_kernel.end_conversation(user, session_id)

    async def recover_session(self, user: UserContext, session_id: str) -> dict[str, Any]:
        await self.ensure_bootstrap()
        session = await self._session_service.must_get_owned_session(user, session_id)
        recovered = await self._session_kernel.recover_session(user, session_id)
        return project_owner_session(recovered)

    async def get_webshell_url(self, user: UserContext, session_id: str) -> dict[str, Any]:
        await self.ensure_bootstrap()
        return await self._session_service.get_webshell_url(user, session_id)

    # ── Turn execution (delegated to session_kernel) ─────────────────

    async def stream_message_events_ds(
        self,
        user: UserContext,
        session_id: str,
        content: str,
        interaction_response: dict[str, Any] | None = None,
        permission_mode: str | None = None,
        client_message_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Data Stream Protocol: stream a single turn via session_kernel."""
        await self.ensure_bootstrap()
        session = await self._session_service.must_get_owned_session(user, session_id)
        async for event in self._session_kernel.stream_ai_stream(
            user,
            session_id,
            content,
            interaction_response=interaction_response,
            permission_mode=permission_mode,
            client_message_id=client_message_id,
        ):
            yield event

    async def dispatch_turn_input(
        self,
        user: UserContext,
        session_id: str,
        content: str,
        *,
        content_blocks: list[dict[str, Any]] | None = None,
        permission_mode: str | None = None,
        client_message_id: str | None = None,
    ) -> dict[str, Any]:
        """Accept a turn and answer with its receipt; deliver nothing."""
        await self.ensure_bootstrap()
        session = await self._session_service.must_get_owned_session(user, session_id)
        return await self._session_kernel.dispatch_turn_input(
            user,
            session_id,
            content,
            content_blocks=content_blocks,
            permission_mode=permission_mode,
            client_message_id=client_message_id,
        )

    async def follow_session_stream(
        self,
        user: UserContext,
        session_id: str,
        *,
        after_seq: int = -1,
    ) -> AsyncIterator[dict[str, Any]]:
        """One terminal-bounded response from the session output subscription."""
        await self.ensure_bootstrap()
        await self._session_service.must_get_owned_session(user, session_id)
        async for frame in self._session_kernel.follow_session_stream(
            user,
            session_id,
            after_seq=after_seq,
        ):
            yield frame

    async def resume_command_stream(
        self, user: UserContext, session_id: str, *, command_id: str
    ) -> AsyncIterator[dict[str, Any]]:
        """Follow the original command's result without admitting another input."""
        await self.ensure_bootstrap()
        await self._session_service.must_get_owned_session(user, session_id)
        async for frame in self._session_kernel.resume_command_stream(
            session_id, command_id=command_id,
        ):
            yield frame

    async def stream_message_events_ds_resume(
        self,
        user: UserContext,
        session_id: str,
        *,
        after_seq: int = -1,
    ) -> AsyncIterator[dict[str, Any]] | None:
        await self.ensure_bootstrap()
        session = await self._session_service.must_get_owned_session(user, session_id)
        return await self._session_kernel.resume_ai_stream(
            user,
            session_id,
            after_seq=after_seq,
        )

    async def interrupt(self, user: UserContext, session_id: str) -> dict[str, Any]:
        await self.ensure_bootstrap()
        session = await self._session_service.must_get_owned_session(user, session_id)
        return await self._session_kernel.interrupt(user, session_id)

    async def answer_pending_interaction(
        self,
        user: UserContext,
        session_id: str,
        interaction_id: str,
        answer: dict[str, Any],
    ) -> dict[str, Any]:
        await self.ensure_bootstrap()
        session = await self._session_service.must_get_owned_session(user, session_id)
        return await self._session_kernel.answer_pending_interaction(
            user,
            session_id,
            interaction_id,
            answer,
        )

    async def supersede_pending_interaction(
        self,
        user: UserContext,
        session_id: str,
        interaction_id: str,
    ) -> dict[str, Any]:
        await self.ensure_bootstrap()
        await self._session_service.must_get_owned_session(user, session_id)
        return await self._session_kernel.supersede_pending_interaction(
            user,
            session_id,
            interaction_id,
        )

    # ── Terminal (delegated to terminal_service) ─────────────────────────

    async def run_terminal_command(
        self,
        user: UserContext,
        session_id: str,
        command: str,
        cwd: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        await self.ensure_bootstrap()
        session = await self._session_service.must_get_owned_session(user, session_id)
        async for event in self._session_kernel.run_terminal_command(
            user,
            session_id,
            command,
            cwd,
        ):
            yield event

    async def list_session_files(
        self,
        user: UserContext,
        session_id: str,
        *,
        path: str | None = None,
    ) -> dict[str, Any]:
        await self.ensure_bootstrap()
        session = await self._session_service.must_get_owned_session(user, session_id)
        return await self._session_file_service.list_entries(
            user,
            session_id,
            path=path,
        )

    async def upload_session_files(
        self,
        user: UserContext,
        session_id: str,
        *,
        path: str | None,
        files: list[Any],
    ) -> dict[str, Any]:
        await self.ensure_bootstrap()
        session = await self._session_service.must_get_owned_session(user, session_id)
        return await self._session_file_service.upload_files(
            user,
            session_id,
            path=path,
            files=files,
        )

    async def create_session_directory(
        self,
        user: UserContext,
        session_id: str,
        *,
        path: str,
    ) -> dict[str, Any]:
        await self.ensure_bootstrap()
        session = await self._session_service.must_get_owned_session(user, session_id)
        return await self._session_file_service.create_directory(
            user,
            session_id,
            path=path,
        )

    async def move_session_file(
        self,
        user: UserContext,
        session_id: str,
        *,
        src_path: str,
        dest_path: str,
    ) -> dict[str, Any]:
        await self.ensure_bootstrap()
        session = await self._session_service.must_get_owned_session(user, session_id)
        return await self._session_file_service.move_path(
            user,
            session_id,
            src_path=src_path,
            dest_path=dest_path,
        )

    async def delete_session_files(
        self,
        user: UserContext,
        session_id: str,
        *,
        paths: list[str],
    ) -> dict[str, Any]:
        await self.ensure_bootstrap()
        session = await self._session_service.must_get_owned_session(user, session_id)
        return await self._session_file_service.delete_paths(
            user,
            session_id,
            paths=paths,
        )

    async def download_session_file(
        self,
        user: UserContext,
        session_id: str,
        *,
        path: str,
    ) -> tuple[AsyncIterator[bytes], str]:
        await self.ensure_bootstrap()
        session = await self._session_service.must_get_owned_session(user, session_id)
        return await self._session_file_service.download_file(
            user,
            session_id,
            path=path,
        )

    # ── Admin (delegated to admin_service) ───────────────────────────────

    async def admin_navigation_summary(self, user: UserContext) -> dict[str, int]:
        return await self._admin_service.admin_navigation_summary(user)

    async def admin_session_totals(self, user: UserContext) -> dict[str, Any]:
        return await self._admin_service.admin_session_totals(user)

    async def admin_list_sessions_page(
        self,
        user: UserContext,
        *,
        page: int = 1,
        page_size: int = 50,
        agent_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> dict[str, Any]:
        return await self._admin_service.admin_list_sessions_page(
            user, page=page, page_size=page_size, agent_id=agent_id, since=since, until=until
        )

    async def admin_list_agent_sessions(
        self, user: UserContext, agent_id: str, *, limit: int = 500
    ) -> list[dict[str, Any]]:
        return await self._admin_service.admin_list_agent_sessions(user, agent_id, limit=limit)

    # ── Webhook management ───────────────────────────────────────────────────
    async def list_agent_deployments(
        self, user: UserContext, agent_id: str
    ) -> list[dict[str, Any]]:
        await self._deployment_service.assert_can_manage_agent(user, agent_id)
        return await self._deployment_service.list_for_agent(agent_id)

    async def list_deployments(
        self, user: UserContext
    ) -> list[dict[str, Any]]:
        return await self._deployment_service.list_manageable(user)

    async def create_agent_deployment(
        self,
        user: UserContext,
        agent_id: str,
        payload: dict[str, Any],
        *,
        callback_base_url: str | None = None,
    ) -> dict[str, Any]:
        await self._deployment_service.assert_can_manage_agent(user, agent_id)
        created = await self._deployment_service.create(
            agent_id=agent_id,
            creator_user_id=user.user_id,
            scene=str(payload.get("scene") or ""),
            name=str(payload.get("name") or ""),
            prompt_prefix=str(payload.get("prompt_prefix") or ""),
            secret=payload.get("secret"),
            attention_policy=payload.get("attention_policy"),
            channel_config=payload.get("channel_config"),
            credentials=payload.get("credentials"),
            callback_base_url=callback_base_url,
            schedule=payload.get("schedule"),
        )
        await self._channel_source_host.reconcile()
        return created

    async def list_channel_providers(self, user: UserContext) -> list[dict[str, Any]]:
        _ = user
        from astrabox.seams.channel import registered_channels

        return [
            {
                **provider.describe().to_dict(),
                "supports_source": provider.supports_source,
                "uses_trigger_secret": provider.uses_trigger_secret,
            }
            for _, provider in sorted(registered_channels().items())
        ]

    async def update_agent_deployment(
        self, user: UserContext, agent_id: str, deployment_id: str, patch: dict[str, Any]
    ) -> dict[str, Any]:
        await self._deployment_service.assert_can_manage_agent(user, agent_id)
        updated = await self._deployment_service.update(
            deployment_id,
            agent_id=agent_id,
            patch=patch,
        )
        await self._channel_source_host.reconcile()
        return updated

    async def delete_agent_deployment(
        self, user: UserContext, agent_id: str, deployment_id: str
    ) -> dict[str, Any]:
        await self._deployment_service.assert_can_manage_agent(user, agent_id)
        await self._deployment_service.delete(deployment_id, agent_id=agent_id)
        await self._channel_source_host.reconcile()
        return {"deployment_id": deployment_id, "deleted": True}

    async def trigger_deployment(
        self, deployment_id: str, *, headers: dict[str, str], raw_body: bytes
    ) -> dict[str, Any]:
        return await self._deployment_service.trigger(
            deployment_id, headers=headers, raw_body=raw_body
        )

    async def forward_channel_callback(
        self,
        deployment_id: str,
        *,
        method: str,
        path: str,
        query: str,
        headers: dict[str, str],
        raw_body: bytes,
    ) -> Any:
        return await self._deployment_service.forward_channel_callback(
            deployment_id,
            method=method,
            path=path,
            query=query,
            headers=headers,
            raw_body=raw_body,
        )

    async def list_deployment_runs(
        self,
        user: UserContext,
        agent_id: str,
        deployment_id: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        await self._deployment_service.assert_can_manage_agent(user, agent_id)
        return await self._deployment_service.list_runs_for_deployment(
            deployment_id, agent_id=agent_id, limit=limit
        )

    async def trigger_deployment_run(
        self, user: UserContext, agent_id: str, deployment_id: str
    ) -> dict[str, Any]:
        await self._deployment_service.assert_can_manage_agent(user, agent_id)
        return await self._deployment_service.trigger_now(deployment_id, agent_id=agent_id)

    async def replay_deployment_run(
        self,
        user: UserContext,
        agent_id: str,
        deployment_id: str,
        run_id: str,
    ) -> dict[str, Any]:
        await self._deployment_service.assert_can_manage_agent(user, agent_id)
        return await self._deployment_service.replay_run(
            run_id, deployment_id=deployment_id, agent_id=agent_id
        )

    async def admin_list_global_sessions(self, limit: int = 200) -> list[dict[str, Any]]:
        return await self._admin_service.admin_list_global_sessions(limit)

    async def admin_list_errors(self, limit: int = 200) -> dict[str, Any]:
        return await self._admin_service.admin_list_errors(limit)

    def admin_process_health(self) -> dict[str, Any]:
        return self._admin_service.admin_process_health()

    async def admin_get_session_detail(self, user: UserContext, session_id: str) -> dict[str, Any]:
        return await self._admin_service.admin_get_session_detail(user, session_id)

    async def admin_get_session_trace(
        self,
        user: UserContext,
        session_id: str,
        *,
        turn_id: str | None = None,
        message_limit: int = 100,
        frame_limit: int = 500,
    ) -> dict[str, Any]:
        return await self._admin_service.admin_get_session_trace(
            user,
            session_id,
            turn_id=turn_id,
            message_limit=message_limit,
            frame_limit=frame_limit,
        )

    async def admin_session_transcript_files(
        self, user: UserContext, session_id: str
    ) -> list[dict[str, Any]]:
        return await self._admin_service.admin_session_transcript_files(user, session_id)

    def admin_iter_batch_transcript_files(
        self,
        user: UserContext,
        *,
        agent_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        # Not `async def` + `await`: the callee is an async generator, so this
        # returns it rather than awaiting it. Writing this the same shape as the
        # forward above would hand the route a coroutine it cannot `async for`.
        return self._admin_service.admin_iter_batch_transcript_files(
            user, agent_id=agent_id, since=since, until=until
        )

    async def admin_kill_session(self, user: UserContext, session_id: str) -> dict[str, Any]:
        return await self._admin_service.admin_kill_session(user, session_id)

    def admin_system_overview(self) -> dict[str, Any]:
        return self._admin_service.admin_system_overview()
