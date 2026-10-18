"""Thin facade that composes the platform's sub-services.

All business logic lives in the sub-services: the session lifecycle (boxes),
the session runtime (readers, sending, following), the session read model,
the terminal, channels, deployments and admin. This file wires them together
and delegates, so a caller holding this facade never needs to know which
sub-service answers.
"""

from __future__ import annotations

from astrabox.core.service.orchestrator.session_browser_service import SessionBrowserService

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

from astrabox.persistence.repository.artifact_record_repository import ArtifactRecordRepository
from astrabox.persistence.repository.artifact_version_repository import ArtifactVersionRepository
from astrabox.persistence.repository.artifact_data_repository import ArtifactDataRepository
from astrabox.persistence.repository.artifact_event_repository import ArtifactEventRepository
from astrabox.persistence.repository.artifact_viewer_repository import ArtifactViewerRepository
from astrabox.persistence.repository.artifact_budget_repository import ArtifactBudgetRepository
from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.persistence.repository.deployment_repository import DeploymentRepository
from astrabox.persistence.repository.environment_repository import EnvironmentRepository
from astrabox.persistence.repository.platform_mcp_binding_repository import PlatformMCPBindingRepository
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.agent_access import is_platform_admin
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
from astrabox.core.service.orchestrator.sandbox_lifecycle import SandboxLifecycleService
from astrabox.core.service.connectors.service import ConnectorService
from astrabox.core.service.orchestrator.platform_mcp_service import PlatformMCPService
from astrabox.core.service.artifacts.service import ArtifactService
from astrabox.core.service.artifacts.origin import OriginAccess
from astrabox.core.service.artifacts.sample import ArtifactSampleService
from astrabox.core.service.artifacts.agent_runs import ArtifactAgentRuns
from astrabox.deploy.public_path import public_path_prefix
from astrabox.core.service.orchestrator.runtime_manager import RemoteAgentRuntimeManager
from astrabox.core.service.orchestrator.runtime_subject import (
    RuntimeSubjectCoordinator,
)
from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService

from astrabox.core.service.orchestrator.session_service import SessionService
from astrabox.core.service.orchestrator.session_share_service import SessionShareService
from astrabox.core.service.orchestrator.session_title_service import SessionTitleService
from astrabox.core.service.orchestrator.session_lifecycle.service import (
    SessionLifecycleService,
)
from astrabox.core.service.orchestrator.terminal_box import TerminalBox
from astrabox.core.service.orchestrator.terminal_service import TerminalService
from astrabox.persistence.repository.terminal_repository import TerminalRepository
from astrabox.core.service.orchestrator.admin_service import AdminService
from astrabox.core.service.orchestrator.channel_ingress_service import (
    ChannelIngressService,
)
from astrabox.core.service.orchestrator.channel_account_links import (
    ChannelAccountLinkService,
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
from astrabox.core.service.orchestrator.session_file_service import (
    FileDownload,
    SessionFileService,
)
from astrabox.core.service.orchestrator.deployment_service import DeploymentService

logger = get_logger(__name__)

#: Deployment fields that change whom a channel's messages run as.
_CHANGES_EXECUTION = frozenset({"execution_mode", "execution_user_id"})


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
        self._build_admin_and_deployments(assistant_lifecycle_getter)

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
        self._artifact_records_repo = ArtifactRecordRepository()
        self._artifact_versions_repo = ArtifactVersionRepository()
        self._artifact_datas_repo = ArtifactDataRepository()
        self._artifact_events_repo = ArtifactEventRepository()
        self._artifact_viewers_repo = ArtifactViewerRepository()
        self._artifact_budgets_repo = ArtifactBudgetRepository()
        self._agent_repo = AgentRepository()
        self._platform_mcp_binding_repo = PlatformMCPBindingRepository()
        self._assistant_workspace_service = AssistantWorkspaceService()

    def _build_core_singletons(self, settings: Any) -> None:
        self._agent_config = AgentConfigService(
            self._agent_repo,
            self._environment_repo,
            sessions_repo=self._sessions_repo,
            spawn_background_task=self._spawn_background_task,
        )
        self._vault_service = VaultService()
        self._runtime_manager = RemoteAgentRuntimeManager(
            sessions_repo=self._sessions_repo,
            agent_service_getter=self._agent_service_getter,
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
                agent_config=self._agent_config,
                runtime_manager=self._runtime_manager,
                ttl_seconds=self._ttl_seconds,
                spawn_background_task=self._spawn_background_task,
                vault_service=self._vault_service,
                assistant_workspace_service=self._assistant_workspace_service,
            ),
        )

        self._session_file_service = self._construct(
            "session_file_service",
            lambda: SessionFileService(
                sessions_repo=self._sessions_repo,
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
        self._session_browser_service = self._construct(
            "session_browser_service",
            lambda: SessionBrowserService(
                session_service=self._session_service,
                runtime_manager=self._runtime_manager,
                expose_port_service=self._expose_port_service,
            ),
        )
        self.artifacts = self._construct(
            "artifact_service",
            lambda: ArtifactService(
                records=self._artifact_records_repo,
                versions=self._artifact_versions_repo,
                data=self._artifact_datas_repo,
                events=self._artifact_events_repo,
                viewers=self._artifact_viewers_repo,
                budgets=self._artifact_budgets_repo,
                file_service=self._session_file_service,
                sessions_repo=self._sessions_repo,
                runtime_manager=self._runtime_manager,
                public_base_url=public_path_prefix(),
            ),
        )
        origin_access = OriginAccess(agent_config=self._agent_config, agent_repo=self._agent_repo)
        self.artifact_samples = ArtifactSampleService(
            artifacts=self.artifacts, origin_access=origin_access
        )
        self.artifact_agent_runs = ArtifactAgentRuns(
            artifacts=self.artifacts,
            origin_access=origin_access,
            platform=self,
            agent_service_getter=agent_service_getter,
            assistant_service_getter=assistant_lifecycle_getter,
        )
        self._platform_mcp_service = self._construct(
            "platform_mcp_service",
            lambda: PlatformMCPService(
                sessions_repo=self._sessions_repo,
                agent_config=self._agent_config,
                binding_repo=self._platform_mcp_binding_repo,
                expose_port_service=self._expose_port_service,
                artifact_service=self.artifacts,
                connector_service=ConnectorService(),
                deployment_service_getter=lambda: self.deployment_service,
            ),
        )
        self._session_title_service = self._construct(
            "session_title_service",
            lambda: SessionTitleService(
                sessions_repo=self._sessions_repo,
                spawn=self._spawn_background_task,
            ),
        )
        self._runtime_subjects = RuntimeSubjectCoordinator(
            runtime_manager=self._runtime_manager,
            sessions_repo=self._sessions_repo,
            assistant_workspace_service=self._assistant_workspace_service,
            assistant_lifecycle_getter=assistant_lifecycle_getter,
        )
        self._session_lifecycle = SessionLifecycleService(
            sessions_repo=self._sessions_repo,
            session_service=self._session_service,
            agent_config=self._agent_config,
            runtime_manager=self._runtime_manager,
            runtime_subjects=self._runtime_subjects,
            assistant_workspace_service=self._assistant_workspace_service,
            spawn_background_task=self._spawn_background_task,
        )
        self._build_session_runtime()
        self._build_session_reads()

    def _build_session_runtime(self) -> None:
        """The session readers, their notices, and the conversation API they serve."""
        from astrabox.core.service.orchestrator.runtime_ensure import RuntimeEnsure
        from astrabox.core.service.orchestrator.session_runtime.feed import SessionFeed
        from astrabox.core.service.orchestrator.session_runtime.host import ReaderHost
        from astrabox.core.service.orchestrator.session_runtime.platform import (
            FactConsumer,
            PlatformRuntimeConnector,
            committed_facts,
            reader_hooks,
        )
        from astrabox.core.service.orchestrator.session_runtime.service import SessionRuntimeService
        from astrabox.persistence.repository.session_runtime_repository import SessionRuntimeRepository

        self._session_facts = SessionRuntimeRepository()
        self._session_feed = SessionFeed()
        #: Acted on after each committed batch of runtime facts (titles, summaries, channels).
        self.session_fact_consumers: list[FactConsumer] = []
        self._runtime_ensure = RuntimeEnsure(
            runtime_manager=self._runtime_manager,
            sessions_repo=self._sessions_repo,
            agent_config=self._agent_config,
            agent_repo=self._agent_repo,
            assistant_workspace_service=self._assistant_workspace_service,
            sandbox_lifecycle_service=self._sandbox_lifecycle_service,
        )
        self._reader_host = ReaderHost(
            repo=self._session_facts,
            feed=self._session_feed,
            connector=PlatformRuntimeConnector(
                sessions=self._sessions_repo,
                facts=self._session_facts,
                runtime_ensure=self._runtime_ensure,
                runtime_manager=self._runtime_manager,
                replace=self._replace_runtime,
            ),
            hooks=reader_hooks(
                sessions=self._sessions_repo,
                runtime_manager=self._runtime_manager,
                realign_live_sandbox_owners=self._sandbox_lifecycle_service.realign_live_sandbox_owners,
                committed=committed_facts(self.session_fact_consumers),
            ),
            committed=committed_facts(self.session_fact_consumers),
        )
        self.session_runtime = SessionRuntimeService(
            sessions=self._sessions_repo,
            repo=self._session_facts,
            feed=self._session_feed,
            host=self._reader_host,
        )
        self._session_runtime_started = False

    def _build_session_reads(self) -> None:
        """The session pages, process summaries, shares, titles and the terminal."""
        from astrabox.core.service.orchestrator.session_runtime.process_summaries import ProcessSummaries
        from astrabox.core.service.orchestrator.session_runtime.views import SessionReads
        from astrabox.core.service.orchestrator.session_title_service import ConversationTitleModel
        from astrabox.persistence.repository.process_summary_repository import ProcessSummaryRepository
        from astrabox.persistence.repository.session_read_repository import SessionReadRepository
        from astrabox.persistence.repository.session_share_repository import SessionShareRepository

        reads = SessionReadRepository()
        self.process_summaries = ProcessSummaries(
            facts=self._session_facts,
            reads=reads,
            repo=ProcessSummaryRepository(),
            model=ConversationTitleModel(),
            enabled=load_astrabox_settings().title_model_enabled,
            spawn=self._spawn_background_task,
        )
        self.session_reads = SessionReads(
            sessions=self._sessions_repo,
            facts=self._session_facts,
            reads=reads,
            runtime=self.session_runtime,
            summaries=self.process_summaries,
        )
        self.session_fact_consumers.append(self.process_summaries.on_facts)
        self.session_fact_consumers.append(self._session_title_service.on_facts)
        self._session_share_service = self._construct(
            "session_share_service",
            lambda: SessionShareService(
                sessions=self._sessions_repo,
                shares=SessionShareRepository(),
                reads=self.session_reads,
                session_file_service=self._session_file_service,
            ),
        )
        self.terminal = self._construct(
            "terminal_service",
            lambda: TerminalService(
                repo=TerminalRepository(),
                sessions=self._sessions_repo,
                feed=self._session_feed,
                box=TerminalBox(
                    runtime_manager=self._runtime_manager,
                    sessions_repo=self._sessions_repo,
                    assistant_workspace_service=self._assistant_workspace_service,
                ),
                spawn=self._spawn_background_task,
            ),
        )

    async def _replace_runtime(self, session_id: str) -> None:
        await self._session_lifecycle.replace_runtime(session_id)

    async def _start_session_runtime(self) -> None:
        if self._session_runtime_started:
            return
        await self._session_feed.start()
        self._reader_host.start()
        self._session_runtime_started = True

    def _build_admin_and_deployments(
        self, assistant_lifecycle_getter: Callable[[], Any] | None
    ) -> None:
        from astrabox.core.service.orchestrator.service_factories import (
            DeploymentServiceReplacement,
        )

        self._admin_service = self._construct(
            "admin_service",
            lambda: AdminService(
                sessions_repo=self._sessions_repo,
                session_runtime=self.session_runtime,
                agent_config=self._agent_config,
                runtime_manager=self._runtime_manager,
                sanitize_session=SessionService._sanitize_session,
                session_lifecycle=self._session_lifecycle,
            ),
        )

        self._agent_deployment_repo = DeploymentRepository()
        self._channel_credentials = ChannelCredentialService()
        self._channel_account_links = ChannelAccountLinkService()
        self._channel_ingress_service = ChannelIngressService(
            deployment_repo=self._agent_deployment_repo,
            agent_repo=self._agent_repo,
            agent_service_getter=self._agent_service_getter,
            sessions_repo=self._sessions_repo,
            session_runtime=self.session_runtime,
            session_facts=self._session_facts,
            spawn_background_task=self._spawn_background_task,
            channel_credentials=self._channel_credentials,
            assistant_service_getter=assistant_lifecycle_getter,
        )
        # Replies reach channels from the facts the session readers record.
        self.session_fact_consumers.append(self._channel_ingress_service.on_facts)
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
                sessions_repo=self._sessions_repo,
                session_runtime=self.session_runtime,
                session_facts=self._session_facts,
                agent_config=self._agent_config,
                channel_ingress=self._channel_ingress_service,
                channel_credentials=self._channel_credentials,
                assistant_service_getter=assistant_lifecycle_getter,
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

    @property
    def quiesced_reason(self) -> str | None:
        """Why this replica stopped serving, or None while it serves."""
        return self._quiesced_reason

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
        self._runtime_manager.quiesce(reason=self._quiesced_reason)
        with contextlib.suppress(Exception):
            self._expiration_watcher.quiesce()
        with contextlib.suppress(Exception):
            self._channel_spine_reconciler.quiesce()
        with contextlib.suppress(Exception):
            self._channel_source_host.quiesce()

    async def wait_closed(self) -> None:
        """Await cancelled platform tasks while their database is still available."""
        await self._reader_host.stop()
        await self._session_feed.stop()
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
        await self._start_session_runtime()
        await self._bootstrap_reconciler.ensure_bootstrap()
        if self._bootstrapped:
            self._session_list_bootstrapped = True

    async def _ensure_session_list_bootstrap(self) -> None:
        """Start list-read prerequisites without the global session reconcile."""
        self._raise_if_quiesced()
        await self._start_session_runtime()
        self._expiration_watcher.ensure_started()

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

    async def delete_environment_config(
        self, user: UserContext, name: str,
    ) -> dict[str, Any]:
        return await self._agent_config.delete_environment_config(user, name)

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
        return await self.session_reads.sessions(user)

    async def list_sessions_page(
        self,
        user: UserContext,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        await self._ensure_session_list_bootstrap()
        return await self.session_reads.sessions_page(user, limit=limit, cursor=cursor)

    async def get_session(self, user: UserContext, session_id: str) -> dict[str, Any]:
        await self.ensure_bootstrap()
        rendered = await self.session_reads.detail(user, session_id)
        session = await self._session_service.must_get_owned_session(user, session_id)
        # Which workspace surfaces this Agent declares. Platform vocabulary,
        # deliberately beside `engine_capabilities` rather than inside it: the
        # engine has no opinion about what a console shows. Read live from the
        # Agent, so it needs no rebuild to take effect.
        rendered["workspace_panels"] = await self._declared_workspace_panels(session)
        # The engine's own manifest, recorded when its runtime started.
        rendered["engine_capabilities"] = session.get("engine_capabilities")
        rendered["terminal_cwd"] = await self.terminal.directory(session_id) or session.get("terminal_cwd")
        return rendered

    async def _declared_workspace_panels(
        self, session: dict[str, Any]
    ) -> dict[str, bool]:
        """The panels this session's Agent declares, defaulting to none.

        An Assistant has no Agent record to declare them, and a Session whose
        Agent was deleted must still render; both resolve to nothing declared
        rather than to an error, because a missing declaration is the same
        answer as a declaration of `false`. Reading the declaration starts
        nothing, so a disabled Environment does not stop the Session rendering
        either; what it refuses is new work.
        """

        agent_id = str(session.get("agent_id") or "").strip()
        agent = (
            await self._agent_config.resolve_agent_harness(
                agent_id, require_enabled_environment=False
            )
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
        return await self.session_reads.messages(user, session_id, before=before, limit=limit)

    async def get_file_changes(
        self, user: UserContext, session_id: str, *, after_seq: int = 0,
    ) -> dict[str, Any]:
        """Read native changes from recorded output without a live sandbox."""
        await self.ensure_bootstrap()
        return await self.session_reads.file_changes(user, session_id, after_seq=after_seq)

    async def get_history_blocks(
        self,
        user: UserContext,
        session_id: str,
        *,
        before: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        await self.ensure_bootstrap()
        return await self.session_reads.history_blocks(user, session_id, before=before, limit=limit)

    async def get_history_block_details(
        self,
        user: UserContext,
        session_id: str,
        block_id: str,
        *,
        cursor: str,
    ) -> dict[str, Any]:
        await self.ensure_bootstrap()
        return await self.session_reads.history_block_details(user, session_id, block_id, cursor=cursor)

    async def generate_process_summary(
        self,
        user: UserContext,
        session_id: str,
        response_id: str,
        *,
        retry_failed: bool = False,
    ) -> dict[str, Any]:
        await self.ensure_bootstrap()
        return await self.session_reads.process_summary(
            user, session_id, response_id, retry_failed=retry_failed
        )

    async def list_session_child_runs(self, user: UserContext, session_id: str) -> dict[str, Any]:
        await self.ensure_bootstrap()
        return await self.session_reads.child_runs(user, session_id)

    async def get_session_child_run_messages(
        self, user: UserContext, session_id: str, child_run_id: str
    ) -> dict[str, Any]:
        await self.ensure_bootstrap()
        return await self.session_reads.child_run_messages(user, session_id, child_run_id)

    async def stop_session_child_run(
        self, user: UserContext, session_id: str, child_run_id: str
    ) -> dict[str, Any]:
        """Hand a sub-agent stop to the runtime through the session's reader."""
        await self.ensure_bootstrap()
        control_ref = await self.session_reads.child_run_stop_control(user, session_id, child_run_id)
        receipt = await self.session_runtime.stop_child_run(user, session_id, control_ref)
        return {**receipt, "child_run_id": child_run_id}

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

    async def download_shared_file(self, token: str, *, path: str) -> FileDownload:
        return await self._session_share_service.download_shared_file(token, path=path)

    async def update_session_permission_mode(
        self,
        user: UserContext,
        session_id: str,
        permission_mode: str,
    ) -> dict[str, Any]:
        await self.ensure_bootstrap()
        return await self._session_lifecycle.update_session_permission_mode(
            user,
            session_id,
            permission_mode,
        )

    async def must_own_session(self, user: UserContext, session_id: str) -> dict[str, Any]:
        await self.ensure_bootstrap()
        return await self._session_service.must_get_owned_session(user, session_id)

    async def delete_session(self, user: UserContext, session_id: str) -> dict[str, Any]:
        await self.ensure_bootstrap()
        return await self._session_lifecycle.delete_session(user, session_id)

    async def mark_session_read(self, user: UserContext, session_id: str) -> dict[str, Any]:
        """Record that the owner has read the conversation up to now."""

        await self.ensure_bootstrap()
        await self._session_service.must_get_owned_session(user, session_id)
        read_at = utcnow_iso()
        # Reading is not activity: the list stays ordered by what happened.
        await self._sessions_repo.update_session(
            session_id, {"read_at": read_at}, touch_updated_at=False
        )
        return {"session_id": session_id, "read_at": read_at}

    async def archive_session(self, user: UserContext, session_id: str) -> dict[str, Any]:
        await self.ensure_bootstrap()
        return await self._session_lifecycle.archive_session(user, session_id)

    async def terminate_sandbox(self, user: UserContext, session_id: str) -> dict[str, Any]:
        await self.ensure_bootstrap()
        return await self._session_lifecycle.terminate_sandbox(user, session_id)

    async def end_conversation(self, user: UserContext, session_id: str) -> dict[str, Any]:
        await self.ensure_bootstrap()
        return await self._session_lifecycle.end_conversation(user, session_id)

    async def recover_session(self, user: UserContext, session_id: str) -> dict[str, Any]:
        await self.ensure_bootstrap()
        return await self._session_lifecycle.recover_session(user, session_id)

    async def get_webshell_url(self, user: UserContext, session_id: str) -> dict[str, Any]:
        await self.ensure_bootstrap()
        return await self._session_service.get_webshell_url(user, session_id)

    # ── Turn execution (delegated to session_kernel) ─────────────────

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
    ) -> FileDownload:
        await self.ensure_bootstrap()
        session = await self._session_service.must_get_owned_session(user, session_id)
        return await self._session_file_service.download_file(
            user,
            session_id,
            path=path,
        )

    async def open_session_browser(self, user: UserContext, session_id: str) -> dict[str, str]:
        return await self._session_browser_service.open_browser(user, session_id)

    async def preview_session_file(
        self, user: UserContext, session_id: str, *, path: str,
    ) -> dict[str, Any]:
        await self.ensure_bootstrap()
        return await self._session_file_service.preview_file(
            user, session_id, path=path, expose_port=self._expose_port_service,
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

    async def list_personal_schedules(
        self, user: UserContext, *, source_assistant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self._deployment_service.list_personal_schedules(
            user, source_assistant_id=source_assistant_id,
        )

    async def create_personal_schedule(
        self, user: UserContext, *, agent_id: str, source_assistant_id: str,
        name: str, prompt_prefix: str, schedule: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._deployment_service.create_personal_schedule(
            user, agent_id=agent_id, source_assistant_id=source_assistant_id,
            name=name, prompt_prefix=prompt_prefix, schedule=schedule,
        )

    async def update_personal_schedule(
        self, user: UserContext, deployment_id: str, *, patch: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._deployment_service.update_personal_schedule(user, deployment_id, patch=patch)

    async def delete_personal_schedule(self, user: UserContext, deployment_id: str) -> dict[str, Any]:
        await self._deployment_service.delete_personal_schedule(user, deployment_id)
        return {"deployment_id": deployment_id, "deleted": True}

    async def list_personal_schedule_runs(
        self, user: UserContext, deployment_id: str, *, limit: int = 50,
    ) -> list[dict[str, Any]]:
        return await self._deployment_service.list_personal_schedule_runs(user, deployment_id, limit=limit)

    async def trigger_personal_schedule(self, user: UserContext, deployment_id: str) -> dict[str, Any]:
        return await self._deployment_service.trigger_personal_schedule(user, deployment_id)

    async def replay_personal_schedule_run(
        self, user: UserContext, deployment_id: str, run_id: str,
    ) -> dict[str, Any]:
        return await self._deployment_service.replay_personal_schedule_run(user, deployment_id, run_id)

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
        return await self._create_deployment(
            user, payload, callback_base_url=callback_base_url, agent_id=agent_id
        )

    async def _create_deployment(
        self,
        user: UserContext,
        payload: dict[str, Any],
        *,
        callback_base_url: str | None,
        agent_id: str = "",
        assistant_id: str = "",
    ) -> dict[str, Any]:
        created = await self._deployment_service.create(
            agent_id=agent_id,
            assistant_id=assistant_id,
            creator_user_id=user.user_id,
            scene=str(payload.get("scene") or ""),
            name=str(payload.get("name") or ""),
            prompt_prefix=str(payload.get("prompt_prefix") or ""),
            secret=payload.get("secret"),
            attention_policy=payload.get("attention_policy"),
            response_instruction=payload.get("response_instruction"),
            channel_config=payload.get("channel_config"),
            credentials=payload.get("credentials"),
            callback_base_url=callback_base_url,
            schedule=payload.get("schedule"),
            allowed_participants=payload.get("allowed_participants"),
            **self._execution_identity(payload, user),
        )
        await self._channel_source_host.reconcile()
        return created

    # ── Assistant trigger bindings (owner-only) ─────────────────────────────
    async def list_assistant_deployments(
        self, user: UserContext, assistant_id: str
    ) -> list[dict[str, Any]]:
        await self._deployment_service.assert_can_manage_assistant(user, assistant_id)
        return await self._deployment_service.list_for_assistant(assistant_id)

    async def create_assistant_deployment(
        self,
        user: UserContext,
        assistant_id: str,
        payload: dict[str, Any],
        *,
        callback_base_url: str | None = None,
    ) -> dict[str, Any]:
        await self._deployment_service.assert_can_manage_assistant(user, assistant_id)
        return await self._create_deployment(
            user, payload, callback_base_url=callback_base_url, assistant_id=assistant_id
        )

    async def update_assistant_deployment(
        self, user: UserContext, assistant_id: str, deployment_id: str, patch: dict[str, Any]
    ) -> dict[str, Any]:
        await self._deployment_service.assert_can_manage_assistant(user, assistant_id)
        updated = await self._deployment_service.update(
            deployment_id, assistant_id=assistant_id, patch=patch, actor=user
        )
        await self._channel_source_host.reconcile()
        return updated

    async def delete_assistant_deployment(
        self, user: UserContext, assistant_id: str, deployment_id: str
    ) -> dict[str, Any]:
        await self._deployment_service.assert_can_manage_assistant(user, assistant_id)
        await self._deployment_service.delete(deployment_id, assistant_id=assistant_id)
        await self._channel_source_host.reconcile()
        return {"deployment_id": deployment_id, "deleted": True}

    async def delete_assistant_conversations(self, user: UserContext, assistant_id: str) -> None:
        """Delete the owner's conversations with an Assistant that is being deleted.

        They belong to the Assistant: the console lists them only under its
        row, and none of them can continue once it is gone.
        """
        await self.ensure_bootstrap()
        for session in await self._sessions_repo.list_assistant_conversations(user.user_id, assistant_id):
            await self._session_lifecycle.delete_session(user, str(session["session_id"]))

    async def delete_target_deployments(
        self, *, agent_id: str = "", assistant_id: str = ""
    ) -> None:
        """Remove an Agent's or Assistant's Deployments as it is deleted, so none keeps firing.

        The one call an Agent or Assistant deletion makes. An Assistant's
        Deployments include the personal schedules created from it.
        """
        await self._deployment_service.delete_for_target(
            agent_id=agent_id, assistant_id=assistant_id
        )
        await self._channel_source_host.reconcile()

    async def list_assistant_deployment_runs(
        self, user: UserContext, assistant_id: str, deployment_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        await self._deployment_service.assert_can_manage_assistant(user, assistant_id)
        return await self._deployment_service.list_runs_for_deployment(
            deployment_id, assistant_id=assistant_id, limit=limit
        )

    async def trigger_assistant_deployment_run(
        self, user: UserContext, assistant_id: str, deployment_id: str
    ) -> dict[str, Any]:
        await self._deployment_service.assert_can_manage_assistant(user, assistant_id)
        return await self._deployment_service.trigger_now(
            deployment_id, assistant_id=assistant_id
        )

    async def replay_assistant_deployment_run(
        self, user: UserContext, assistant_id: str, deployment_id: str, run_id: str
    ) -> dict[str, Any]:
        await self._deployment_service.assert_can_manage_assistant(user, assistant_id)
        return await self._deployment_service.replay_run(
            run_id, deployment_id=deployment_id, assistant_id=assistant_id
        )

    async def list_channel_providers(self, user: UserContext) -> list[dict[str, Any]]:
        from astrabox.seams.channel import registered_channels

        return [
            {
                **provider.describe().to_dict(),
                "supports_source": provider.supports_source,
                "uses_trigger_secret": provider.uses_trigger_secret,
                "can_configure_execution_user": is_platform_admin(user.roles),
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
            **({"actor": user} if _CHANGES_EXECUTION.intersection(patch) else {}),
        )
        await self._channel_source_host.reconcile()
        return updated

    @staticmethod
    def _execution_identity(payload: dict[str, Any], user: UserContext) -> dict[str, Any]:
        """Execution keywords for a create, only when the request names any.

        A request without them keeps the replacement contract's original
        keyword shape.
        """
        named = {key: payload[key] for key in _CHANGES_EXECUTION if key in payload}
        return {**named, "actor": user} if named else {}

    async def _agent_channel_binding(
        self, user: UserContext, agent_id: str, deployment_id: str
    ) -> dict[str, Any]:
        await self._deployment_service.assert_can_manage_agent(user, agent_id)
        for binding in await self._deployment_service.list_for_agent(agent_id):
            if str(binding.get("deployment_id") or "") == deployment_id:
                return binding
        raise APIError(code="NOT_FOUND", message="deployment not found", status_code=404)

    async def list_agent_deployment_account_links(
        self, user: UserContext, agent_id: str, deployment_id: str
    ) -> list[dict[str, Any]]:
        binding = await self._agent_channel_binding(user, agent_id, deployment_id)
        return await self._channel_account_links.links(binding, actor=user)

    async def put_agent_deployment_account_link(
        self, user: UserContext, agent_id: str, deployment_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        binding = await self._agent_channel_binding(user, agent_id, deployment_id)
        return await self._channel_account_links.link(
            binding, participant=payload.get("participant"), user_id=payload.get("user_id"),
            actor=user,
        )

    async def delete_agent_deployment_account_link(
        self, user: UserContext, agent_id: str, deployment_id: str, participant: str
    ) -> dict[str, Any]:
        binding = await self._agent_channel_binding(user, agent_id, deployment_id)
        return await self._channel_account_links.unlink(
            binding, participant=participant, actor=user,
        )

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
        response_id: str | None = None,
        message_limit: int = 100,
        part_limit: int = 500,
    ) -> dict[str, Any]:
        return await self._admin_service.admin_get_session_trace(
            user,
            session_id,
            response_id=response_id,
            message_limit=message_limit,
            part_limit=part_limit,
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

    async def admin_import_session_history(
        self, session_id: str, messages: Any,
    ) -> dict[str, int]:
        return await self._admin_service.admin_import_session_history(session_id, messages)

    async def admin_kill_session(self, user: UserContext, session_id: str) -> dict[str, Any]:
        return await self._admin_service.admin_kill_session(user, session_id)

    def admin_system_overview(self) -> dict[str, Any]:
        return self._admin_service.admin_system_overview()
