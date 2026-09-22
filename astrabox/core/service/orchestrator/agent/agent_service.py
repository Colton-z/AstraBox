"""Agent lifecycle management — the only entry point for controllers."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

from astrabox.persistence.repository import AgentRepository
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext, default_org_id
from astrabox.core.model import AgentState
from astrabox.core.service.orchestrator.agent_access import can_manage_agent, can_view_agent
from astrabox.core.service.orchestrator.agent_schema import (
    AGENT_PRIVATE_STORED_FIELDS,
)
from astrabox.core.service.orchestrator.sandbox_names import keep_name_updates
from astrabox.seams.sandbox import sandbox_name_for_template
from astrabox.seams.sandbox_disposal import SandboxDestruction

logger = get_logger(__name__)

# Bounds the post-wake ACTIVE-poll loop in
# agent_mcp_service._ensure_active_agent.
_AGENT_STARTUP_TIMEOUT_SECONDS = 300


class AgentService:
    """Facade for all Agent operations."""

    def __init__(
        self,
        *,
        platform_service: Any,
        sessions_repo: Any,
        runtime_manager: Any,
        agent_config: Any,
        turn_service: Any,
        broker: Any,
        agent_repo: Any | None = None,
    ) -> None:
        self._platform = platform_service
        self._sessions_repo = sessions_repo
        self._runtime_manager = runtime_manager
        self._agent_config = agent_config
        self._turn_service = turn_service
        self._broker = broker
        self._agent_repo = agent_repo if agent_repo is not None else AgentRepository()

        self._bootstrap_lock = asyncio.Lock()
        self._bootstrapped = False
        self._quiesced_reason: str | None = None
        self._preparation_tasks: set[asyncio.Task[Any]] = set()
        #: agent_id → the reconciliation still running for it, so a repeat
        #: request while one is in flight coalesces into a subsequent pass.
        self._reconciliations_in_flight: dict[str, asyncio.Task[Any]] = {}
        self._reconciliation_requested: set[str] = set()
        self._runtime_reconciliation_locks: dict[str, asyncio.Lock] = {}
        self._pool_retirement_tasks: dict[str, asyncio.Task[Any]] = {}

    def quiesce(self, *, reason: str) -> None:
        if self._quiesced_reason:
            logger.info(
                "agent service already closing for shutdown: previous=%s current=%s",
                self._quiesced_reason,
                reason,
            )
            return
        self._quiesced_reason = str(reason or "shutdown").strip() or "shutdown"
        for task in list(self._preparation_tasks):
            task.cancel("agent_service_shutdown")
        logger.warning(
            "closing agent service for shutdown: reason=%s",
            self._quiesced_reason,
        )

    def _raise_if_quiesced(self) -> None:
        reason = str(self._quiesced_reason or "").strip()
        if not reason:
            return
        raise APIError(
            code="ASTRABOX_RELEASING",
            message=f"agent service is closing for shutdown: {reason}",
            status_code=503,
        )

    async def ensure_bootstrap(self) -> None:
        """Schedule persisted Agent preparation without gating API availability."""

        self._raise_if_quiesced()
        if self._bootstrapped:
            return
        async with self._bootstrap_lock:
            self._raise_if_quiesced()
            if self._bootstrapped:
                return
            rows = await self._agent_repo.list_all_agents()
            for row in rows:
                self._schedule_runtime_reconciliation(str(row.get("agent_id") or ""))
            self._bootstrapped = True

    async def create_agent(self, user: UserContext, config: dict[str, Any]) -> dict[str, Any]:
        """Create one Agent with its model, prompt, extensions, and Environment.

        There is no separate template resource. The configuration service
        validates the fields, mints ``agent_id``, and starts ``version`` at 1.
        """
        self._raise_if_quiesced()
        created = await self._agent_config.create_agent_config(user, config)
        self._schedule_runtime_reconciliation(str(created.get("agent_id") or ""))
        return created

    async def update_agent(
        self,
        user: UserContext,
        agent_id: str,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        """Update one Agent."""

        self._raise_if_quiesced()
        updated = await self._agent_config.upsert_agent_config(user, agent_id, config)
        self._schedule_runtime_reconciliation(agent_id)
        return updated

    def schedule_runtime_reconciliation(self, agent_id: str) -> bool:
        """Reconcile current configuration after a write or capacity claim."""

        return self._schedule_runtime_reconciliation(agent_id)

    async def reconcile_environment_runtimes(self, environment_name: str) -> int:
        """Refresh prepared runtimes for Agents using an Environment.

        An Environment owns runtime inputs such as the sandbox image, network
        policy and permission. Changing any of them changes an Agent's runtime
        generation even when the Agent document itself is unchanged.
        """

        target = str(environment_name or "").strip()
        if not target or self._quiesced_reason:
            return 0
        rows = await self._agent_repo.list_all_agents()
        scheduled = 0
        for row in rows:
            if str(row.get("environment_name") or "").strip() != target:
                continue
            agent_id = str(row.get("agent_id") or "").strip()
            if not agent_id:
                continue
            self._schedule_runtime_reconciliation(agent_id)
            scheduled += 1
        return scheduled

    async def list_agents(self, user: UserContext) -> list[dict[str, Any]]:
        """The agents this caller may see — public ones, plus their own.

        This read answers the agent picker, so the per-agent visibility has to
        be applied here as well as in the console's management list. Enforcing
        it only there would leave `private` as decoration on a record every user
        could still enumerate through this call.
        """
        await self.ensure_bootstrap()
        rows = await self._agent_repo.list_all_agents()
        return [
            self._user_view(user, r) for r in rows if can_view_agent(r, user.user_id, user.roles)
        ]

    async def list_all_agents(self) -> list[dict[str, Any]]:
        """Every agent, unfiltered — for internal paths that already hold their
        own authorization (runtime startup resolving an existing session)."""
        rows = await self._agent_repo.list_all_agents()
        return [self._sanitize(r) for r in rows]

    async def get_agent(self, user: UserContext, agent_id: str) -> dict[str, Any]:
        agent = await self._must_view(user, agent_id)
        return self._user_view(user, agent)

    async def get_agent_access(self, user: UserContext, agent_id: str) -> dict[str, Any]:
        """Read Agent authorization settings through their dedicated service."""

        return await self._agent_config.get_agent_access(user, agent_id)

    async def set_agent_access(
        self,
        user: UserContext,
        agent_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Write Agent authorization settings outside general Agent update."""

        return await self._agent_config.set_agent_access(user, agent_id, payload)

    async def get_prepared_runtime_status(self, user: UserContext, agent_id: str) -> dict[str, Any]:
        """Return platform-prepared capacity for an Agent manager."""

        agent = await self._must_view(user, agent_id)
        if not can_manage_agent(agent, user.user_id, user.roles):
            raise APIError(
                code="FORBIDDEN",
                message=(
                    "only an Agent manager or platform admin may view prepared runtime status"
                ),
                status_code=403,
            )
        return await self._prepared_runtime_status(agent)

    async def _runtime_enabled(self, agent: dict[str, Any]) -> bool:
        """Read runtime eligibility without constructing a startup configuration."""

        enabled = agent.get("enabled", True)
        if (
            enabled is False
            or str(enabled).strip().lower() == "false"
            or agent.get("state") in {AgentState.DELETING.value, AgentState.DELETED.value}
        ):
            return False
        environment = await self._agent_config.get_environment(str(agent.get("environment_name") or ""))
        return environment is not None and environment.get("enabled") is not False

    async def _prepared_runtime_status(self, agent: dict[str, Any]) -> dict[str, Any]:
        from astrabox.core.service.orchestrator.runtime.runtime_profile import (
            resolve_sandbox_tenancy,
        )
        from astrabox.seams.sandbox import (
            SANDBOX_TENANCY_CONVERSATION,
            sandbox_for_name,
        )

        manifest = agent.get("_prepared_slot")
        manifest = dict(manifest) if isinstance(manifest, dict) else {}
        enabled = bool(agent.get("prewarm_enabled")) and await self._runtime_enabled(agent)
        pool_name = str(agent.get("_client_pool_name") or "").strip()
        pool_backend = str(agent.get("_client_pool_backend") or "").strip()
        template = (
            await self._agent_config.resolve_agent_harness(str(agent.get("agent_id") or "").strip())
            if enabled else None
        )
        if enabled and template is not None:
            from astrabox.core.service.orchestrator.agent.runtime_generation import runtime_generations

            requested_generation, _ = await runtime_generations(
                template, runtime_manager=self._runtime_manager
            )
            if requested_generation != agent.get("_prepared_runtime_generation"):
                return {
                    "enabled": True,
                    "ready": False,
                    "prepared_count": 0,
                    "state": "preparing",
                    "last_error": agent.get("_prepared_runtime_error"),
                }
        if (
            template is not None
            and resolve_sandbox_tenancy(template) == SANDBOX_TENANCY_CONVERSATION
            and enabled
            and pool_name
        ):
            if not pool_backend:
                raise APIError(
                    code="AGENT_PREWARM_CONFIG_INVALID",
                    message=f"client pool {pool_name!r} has no persisted sandbox backend",
                    status_code=500,
                )
            pool = await sandbox_for_name(pool_backend).describe_client_pool(pool_name)
            sandbox_id = pool.idle_sandbox_ids[0] if pool.idle_sandbox_ids else None
            ready = bool(pool.ready and sandbox_id)
            return {
                "enabled": True,
                "ready": ready,
                "prepared_count": pool.idle_count if ready else 0,
                "state": pool.lifecycle_state,
                "placement": "conversation_box",
                "runtime_generation": (
                    str(agent.get("_runtime_generation") or "").strip() or None
                ),
                "client_pool_name": pool_name,
                "sandbox_id": sandbox_id,
                "last_error": (
                    str(agent.get("_prepared_runtime_error") or "").strip()
                    or ("Sandbox provider reported a prewarm preparation failure." if pool.last_error else None)
                ),
            }
        state = str(manifest.get("state") or "").strip() or None
        ready = (
            enabled and state == "prepared"
            and manifest.get("runtime_generation") == agent.get("_prepared_runtime_generation")
        )
        return {
            "enabled": enabled,
            "ready": ready,
            "prepared_count": 1 if ready else 0,
            "state": state,
            "placement": str(manifest.get("placement") or "").strip() or None,
            "runtime_generation": (str(agent.get("_runtime_generation") or "").strip() or None),
            "client_pool_name": pool_name or None,
            "sandbox_id": str(manifest.get("sandbox_id") or "").strip() or None,
            "last_error": (
                str(agent.get("_prepared_runtime_error") or "").strip() or None
            ),
        }

    async def refresh_prepared_runtime(self, user: UserContext, agent_id: str) -> dict[str, Any]:
        """Persist an explicit rebuild request; existing Sessions keep their boxes."""

        self._raise_if_quiesced()
        agent = await self._must_view(user, agent_id)
        if not can_manage_agent(agent, user.user_id, user.roles):
            raise APIError(
                code="FORBIDDEN",
                message="only an Agent manager or platform admin may refresh prewarming",
                status_code=403,
            )
        if not agent.get("prewarm_enabled") or not await self._runtime_enabled(agent):
            raise APIError(
                code="AGENT_PREWARM_CONFIG_INVALID",
                message="enable the Agent, its Environment and prewarming before requesting a refresh",
                status_code=409,
            )
        updates = {"_prewarm_revision": uuid.uuid4().hex, "_prepared_runtime_error": None}
        updated = await self._agent_repo.compare_and_update_agent(
            agent_id,
            expected={
                key: agent.get(key) if key in agent else {"$exists": False}
                for key in ("version", "prewarm_enabled", "enabled", "_prewarm_revision")
            },
            updates=updates,
        )
        if not updated:
            raise APIError(
                code="AGENT_RUNTIME_GENERATION_CONFLICT",
                message="Agent changed while requesting prewarm refresh; reload its configuration",
                status_code=409,
            )
        agent.update(updates)
        self.schedule_runtime_reconciliation(agent_id)
        return await self._prepared_runtime_status(agent)

    async def delete_agent(self, user: UserContext, agent_id: str) -> dict[str, Any]:
        agent = await self._must_view(user, agent_id)
        if not can_manage_agent(agent, user.user_id, user.roles):
            raise APIError(
                code="FORBIDDEN",
                message="only the creator or an admin may delete this agent",
                status_code=403,
            )
        await self._agent_repo.update_agent(
            agent_id,
            {"state": AgentState.DELETING.value},
        )
        agent["state"] = AgentState.DELETING.value
        runtime_key = self._agent_runtime_key(agent_id)
        sandbox_id = str(agent.get("sandbox_id") or "").strip() or None
        destruction: SandboxDestruction | None = None
        try:
            destruction = await self._runtime_manager.terminate_runtime(
                runtime_key, fallback_sandbox_id=sandbox_id
            )
        except Exception as exc:
            logger.warning("failed to terminate agent sandbox: %s", exc)
        # Soft-delete hides this row from `find_agent_by_sandbox_id`, which is
        # the only place a by-id destroy can learn this box's backend. Whatever
        # the delete could not confirm gone therefore has to be written onto the
        # row before it stops being findable, or the box outlives every way of
        # naming it.
        keep = keep_name_updates(destruction, row=agent)
        if keep:
            logger.error(
                "agent delete could not confirm the destruction of agent=%s "
                "sandbox=%s; it is recorded as undestroyed on the row: %s",
                agent_id,
                sandbox_id,
                destruction.detail if destruction is not None else "the destroy raised",
            )
            await self._agent_repo.update_agent(agent_id, keep)
            agent.update(keep)
        await self._reconcile_agent_runtime(agent_id)
        retirement = self._pool_retirement_tasks.get(agent_id)
        if retirement is not None:
            await retirement
        await self._agent_repo.soft_delete(agent_id, user.user_id)
        agent["state"] = AgentState.DELETED.value
        agent["expires_at"] = None
        # The pointer is cleared only on a confirmed destruction, by the same
        # rule every other row obeys.
        if destruction is not None and destruction.confirmed:
            agent["sandbox_id"] = None
        return self._sanitize(agent)

    def _schedule_runtime_reconciliation(self, agent_id: str) -> bool:
        """Reconcile persisted configuration without delaying its caller.

        The Agent document has already committed by this point. Preparation is
        a latency optimization; a failure is recorded and retried at bootstrap,
        while ordinary Session startup remains the authoritative path. Returns
        whether a reconciliation was started; an in-flight one queues another
        pass to resolve any configuration written while it was preparing.
        """

        target = str(agent_id or "").strip()
        if not target or self._quiesced_reason:
            return False
        running = self._reconciliations_in_flight.get(target)
        if running is not None and not running.done():
            # Coalesce writes during a build; its captured template cannot
            # acknowledge the latest configuration without another pass.
            self._reconciliation_requested.add(target)
            return False
        coro = self._reconcile_agent_runtime(target)
        spawn = getattr(self._platform, "_spawn_background_task", None)
        try:
            task = (
                spawn(coro, name=f"agent-runtime-reconcile:{target}")
                if callable(spawn)
                else asyncio.create_task(coro, name=f"agent-runtime-reconcile:{target}")
            )
        except BaseException:
            coro.close()
            raise
        self._preparation_tasks.add(task)
        self._reconciliations_in_flight[target] = task

        def _done(completed: asyncio.Task[Any]) -> None:
            self._preparation_tasks.discard(completed)
            if self._reconciliations_in_flight.get(target) is completed:
                self._reconciliations_in_flight.pop(target, None)
            if completed.cancelled():
                return
            try:
                completed.result()
            except Exception as exc:
                logger.warning(
                    "could not reconcile Agent runtime: agent=%s error=%s",
                    target,
                    exc,
                    exc_info=True,
                )
            if target in self._reconciliation_requested:
                self._reconciliation_requested.discard(target)
                self._schedule_runtime_reconciliation(target)

        task.add_done_callback(_done)
        return True

    def _schedule_pool_retirement(self, agent_id: str) -> None:
        running = self._pool_retirement_tasks.get(agent_id)
        if self._quiesced_reason or (running is not None and not running.done()):
            return
        task = asyncio.create_task(
            self._retire_obsolete_pools(agent_id), name=f"agent-pool-retirement:{agent_id}"
        )
        self._pool_retirement_tasks[agent_id] = task
        self._preparation_tasks.add(task)

        def _done(completed: asyncio.Task[Any]) -> None:
            self._preparation_tasks.discard(completed)
            if self._pool_retirement_tasks.get(agent_id) is completed:
                self._pool_retirement_tasks.pop(agent_id, None)
            if not completed.cancelled():
                try:
                    completed.result()
                except Exception:
                    logger.exception("Agent pool retirement remains pending: agent=%s", agent_id)

        task.add_done_callback(_done)

    async def _retire_obsolete_pools(self, agent_id: str) -> None:
        """Clean retained supplier addresses without gating new preparation."""
        from astrabox.core.service.orchestrator.agent.client_pool import retire_agent_client_pool

        lock = self._runtime_reconciliation_locks.setdefault(agent_id, asyncio.Lock())
        while True:
            row = await self._agent_repo.get_agent(agent_id)
            pending = (row or {}).get("_retiring_client_pools") or []
            if not pending:
                return
            retired = pending[0]
            await retire_agent_client_pool(retired["name"], backend_name=retired["backend"])
            async with lock:
                row = await self._agent_repo.get_agent(agent_id)
                pending = (row or {}).get("_retiring_client_pools") or []
                await self._agent_repo.compare_and_update_agent(
                    agent_id,
                    expected={"_retiring_client_pools": pending},
                    updates={"_retiring_client_pools": [item for item in pending if item != retired]},
                )

    async def _reconcile_agent_runtime(self, agent_id: str) -> None:
        """Publish one platform generation and prepare its configured capacity."""

        from astrabox.core.service.orchestrator.agent.client_pool import (
            CLIENT_POOL_BACKEND_FIELD,
            CLIENT_POOL_NAME_FIELD,
            ensure_agent_client_pool,
            retire_agent_client_pool,
        )
        from astrabox.core.service.orchestrator.agent.prepared_slots import (
            prepare_slot_for_agent,
            retire_prepared_runtime,
            wait_for_agent_starts,
        )
        from astrabox.core.service.orchestrator.agent.runtime_generation import (
            reconcile_runtime_generation,
        )

        target = str(agent_id or "").strip()
        lock = self._runtime_reconciliation_locks.setdefault(target, asyncio.Lock())
        async with lock:
            await wait_for_agent_starts(target)
            row = await self._agent_repo.get_agent(target)
            if not isinstance(row, dict):
                return
            previous_pool_name = str(row.get(CLIENT_POOL_NAME_FIELD) or "").strip()
            previous_pool_backend = str(row.get(CLIENT_POOL_BACKEND_FIELD) or "").strip()
            version = row.get("version") if "version" in row else {"$exists": False}
            revision = row.get("_prewarm_revision")
            expected = {
                "version": version,
                "_prewarm_revision": revision if "_prewarm_revision" in row else {"$exists": False},
            }
            try:
                if row.get("_retiring_client_pools"):
                    self._schedule_pool_retirement(target)
                generation = row.get("_prepared_runtime_generation")
                enabled = await self._runtime_enabled(row)
                if enabled:
                    template = await self._agent_config.resolve_agent_harness(target)
                    if template is None:
                        return
                    generation = await reconcile_runtime_generation(
                        template,
                        runtime_manager=self._runtime_manager,
                        agent_repo=self._agent_repo,
                    )
                if not enabled or not row.get("prewarm_enabled"):
                    await retire_prepared_runtime(
                        target,
                        reason="Agent runtime preparation is disabled",
                        agent_repo=self._agent_repo,
                    )
                    if previous_pool_name:
                        await retire_agent_client_pool(
                            previous_pool_name,
                            backend_name=previous_pool_backend,
                        )
                        await self._agent_repo.update_agent(
                            target,
                            {
                                CLIENT_POOL_NAME_FIELD: None,
                                CLIENT_POOL_BACKEND_FIELD: None,
                                "_client_pool_epoch": None,
                            },
                        )
                else:
                    refreshed = await self._agent_config.resolve_agent_harness(target)
                    if refreshed is None:
                        return
                    if isinstance(refreshed, dict):
                        refreshed["runtime_generation"] = generation
                    else:
                        refreshed.runtime_generation = generation
                    pool_plan = await ensure_agent_client_pool(
                        refreshed,
                        runtime_manager=self._runtime_manager,
                    )
                    current_pool_name = pool_plan.spec.pool_name if pool_plan is not None else ""
                    current_pool_backend = pool_plan.backend_name if pool_plan is not None else ""
                    if (
                        previous_pool_name != current_pool_name
                        or previous_pool_backend != current_pool_backend
                    ):
                        retiring_pools = list(row.get("_retiring_client_pools") or [])
                        if previous_pool_name:
                            retiring_pools.append(
                                {"name": previous_pool_name, "backend": previous_pool_backend}
                            )
                        published = await self._agent_repo.compare_and_update_agent(
                            target,
                            expected={
                                **expected,
                                **{
                                    key: row[key] if key in row else {"$exists": False}
                                    for key in (
                                        CLIENT_POOL_NAME_FIELD, CLIENT_POOL_BACKEND_FIELD,
                                        "_retiring_client_pools",
                                    )
                                },
                            },
                            updates={
                                CLIENT_POOL_NAME_FIELD: current_pool_name or None,
                                CLIENT_POOL_BACKEND_FIELD: (current_pool_backend or None),
                                "_retiring_client_pools": retiring_pools,
                            },
                        )
                        if not published:
                            raise APIError(
                                code="AGENT_RUNTIME_GENERATION_CONFLICT",
                                message="Agent changed while publishing prepared capacity",
                                status_code=409,
                            )
                        if retiring_pools:
                            self._schedule_pool_retirement(target)
                    from astrabox.core.service.orchestrator.runtime.runtime_profile import (
                        resolve_sandbox_tenancy,
                    )
                    from astrabox.seams.sandbox import SANDBOX_TENANCY_CONVERSATION

                    if resolve_sandbox_tenancy(refreshed) == SANDBOX_TENANCY_CONVERSATION:
                        # Older releases kept a second AstraBox-owned whole-box
                        # queue in this manifest. Once the supplier pool is
                        # running, retire that obsolete physical inventory.
                        await retire_prepared_runtime(
                            target,
                            reason="conversation capacity moved to the client pool",
                            agent_repo=self._agent_repo,
                        )
                    else:
                        await prepare_slot_for_agent(
                            refreshed,
                            runtime_manager=self._runtime_manager,
                            agent_repo=self._agent_repo,
                        )
                await self._agent_repo.compare_and_update_agent(
                    target,
                    expected=expected,
                    updates={
                        "_prepared_runtime_error": None,
                        "_prepared_runtime_generation": generation,
                    },
                )
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                await self._agent_repo.compare_and_update_agent(
                    target,
                    expected=expected,
                    updates={
                        "_prepared_runtime_error": (f"{type(exc).__name__}: {str(exc).strip()}")
                    },
                )
                raise

    async def wake_agent(self, user: UserContext, agent_id: str) -> dict[str, Any]:
        agent = await self._must_view(user, agent_id)
        if not can_manage_agent(agent, user.user_id, user.roles):
            raise APIError(
                code="FORBIDDEN",
                message="only the creator or an admin may prepare this agent",
                status_code=403,
            )
        if str(agent.get("state") or "").strip() != AgentState.ACTIVE.value:
            await self._agent_repo.update_agent(agent_id, {"state": AgentState.ACTIVE.value})
            agent["state"] = AgentState.ACTIVE.value
        return self._sanitize(agent)

    async def hibernate_agent(self, user: UserContext, agent_id: str) -> dict[str, Any]:
        agent = await self._must_view(user, agent_id)
        if not can_manage_agent(agent, user.user_id, user.roles):
            raise APIError(
                code="FORBIDDEN",
                message="only the creator or an admin may hibernate this agent",
                status_code=403,
            )
        return self._sanitize(agent)

    async def start_conversation(
        self,
        user: UserContext,
        agent_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a new user-visible conversation session with an agent.

        The conversation is provisioned through the same
        ``kernel.create_session`` → ``StartSessionStartup`` path as chat
        (lifecycle_worker's agent_chat branch plans an ``agent_conversation``
        subject).
        """
        agent = await self._must_access(agent_id)
        harness = await self._agent_config.resolve_agent_harness(
            agent_id, viewer_user_id=user.user_id, viewer_roles=user.roles
        )
        if harness is None:
            raise APIError(code="TEMPLATE_NOT_ALLOWED", message="agent not found", status_code=403)
        kernel = getattr(self._platform, "_session_kernel", None)
        if kernel is None:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="session kernel unavailable for agent conversation",
                status_code=500,
            )
        create_options: dict[str, Any] = {
            # No permission_mode: creation falls to the product default for
            # (agent_chat, engine) declared once in engine/capabilities.py.
            "session_kind": "agent_chat",
            "workspace_ref": {"kind": "agent", "agent_id": agent_id},
            "source_type": "agent",
            "agent_id": agent_id,
            "deployment_name": agent.get("name"),
            "title": agent.get("name"),
        }
        if idempotency_key is not None:
            create_options["idempotency_key"] = idempotency_key
        response = await kernel.create_session(
            user,
            # The Session resolves the Agent by ``agent_id``; this positional
            # value is only the display/grouping label expected by the kernel.
            agent_id,
            **create_options,
        )
        session_id = str((response or {}).get("session_id") or "").strip()
        return {
            "session_id": session_id,
            "agent_id": agent_id,
            "deployment_name": agent.get("name"),
        }

    async def chat_stream(
        self,
        user: UserContext,
        agent_id: str,
        content: str,
    ) -> AsyncIterator[dict[str, Any]]:
        _ = (user, agent_id, content)
        raise APIError(
            code="AGENT_DIRECT_CHAT_DISABLED",
            message="Agents don't support a primary chat session — create a conversation first",
            status_code=409,
        )
        if False:
            yield {}

    async def _must_access(self, agent_id: str) -> dict[str, Any]:
        """Resolve by id with no visibility check — internal paths only.

        Callers acting for a user want :meth:`_must_view`.
        """
        agent = await self._agent_repo.get_agent(agent_id)
        if agent is None:
            raise APIError(code="AGENT_NOT_FOUND", message="agent not found", status_code=404)
        return agent

    async def _must_view(self, user: UserContext, agent_id: str) -> dict[str, Any]:
        """Resolve an agent this user is allowed to see.

        Missing and invisible share one 404: a separate 403 would confirm that
        an agent by that id exists, which is exactly what `private` is for. The
        deployment-binding gate already answers this way.
        """
        agent = await self._must_access(agent_id)
        if not can_view_agent(agent, user.user_id, user.roles):
            raise APIError(code="AGENT_NOT_FOUND", message="agent not found", status_code=404)
        return agent

    @staticmethod
    def _agent_runtime_key(agent_id: str) -> str:
        from astrabox.core.service.orchestrator.agent.runtime_generation import (
            agent_runtime_owner_id,
        )

        return agent_runtime_owner_id(agent_id)

    def _user_view(self, user: UserContext, doc: dict[str, Any]) -> dict[str, Any]:
        """Public Agent projection plus the server-computed management capability."""

        return {
            **self._sanitize(doc),
            "can_manage": can_manage_agent(doc, user.user_id, user.roles),
        }

    @staticmethod
    def _sanitize(doc: dict[str, Any]) -> dict[str, Any]:
        # The API response carries only the public Agent shape: every
        # leading-underscore internal field is dropped, plus the named keys
        # a stored document may carry but no client may see.
        return {
            k: v
            for k, v in doc.items()
            if not k.startswith("_") and k not in AGENT_PRIVATE_STORED_FIELDS
        }
