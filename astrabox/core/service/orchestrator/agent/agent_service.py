"""Agent lifecycle management — the only entry point for controllers."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.persistence.repository.agent_repository import NAME_SORT_KEY_FIELD
from astrabox.persistence.repository.keyset import (
    SWEEP_PAGE_SIZE,
    InvalidListCursor,
    decode_list_cursor,
    encode_list_cursor,
    iter_keyset_pages,
)
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext, default_org_id
from astrabox.core.service.orchestrator.agent import agent_catalog
from astrabox.core.model import AgentState
from astrabox.core.service.orchestrator.agent_access import (
    can_manage_agent,
    can_view_agent,
    is_platform_admin,
)
from astrabox.core.service.orchestrator.agent_schema import (
    AGENT_PRIVATE_STORED_FIELDS,
    agent_artifacts_enabled,
)
from astrabox.core.service.orchestrator.sandbox_names import keep_name_updates
from astrabox.seams.sandbox import sandbox_name_for_template
from astrabox.seams.sandbox_disposal import SandboxDestruction

logger = get_logger(__name__)

#: How long a caller waits for a new conversation to leave ``CREATING``
#: (``agent_mcp_service`` and the artifact runner).
_AGENT_STARTUP_TIMEOUT_SECONDS = 300

#: The status narrowings the Agent list page accepts.
AGENT_LIST_STATUSES = frozenset({"all", "enabled", "disabled"})
_AGENT_LIST_CURSOR_KEYS = ("name_key", "agent_id")


class AgentService:
    """Facade for all Agent operations."""

    def __init__(
        self,
        *,
        platform_service: Any,
        sessions_repo: Any,
        runtime_manager: Any,
        agent_config: Any,
        agent_repo: Any | None = None,
    ) -> None:
        self._platform = platform_service
        self._sessions_repo = sessions_repo
        self._runtime_manager = runtime_manager
        self._agent_config = agent_config
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
            async for row in self._live_agents():
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
        return self._sanitize(created)

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
        return self._sanitize(updated)

    async def set_connector_servers(
        self, user: UserContext, agent_id: str, servers: dict[str, str]
    ) -> dict[str, Any]:
        """Record which connector hub servers the Agent offers its callers."""

        self._raise_if_quiesced()
        updated = await self._agent_config.set_agent_connector_servers(user, agent_id, servers)
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
        scheduled = 0
        async for row in iter_keyset_pages(
            lambda after: self._environment_agent_ids_page(target, after),
            key="agent_id",
        ):
            self._schedule_runtime_reconciliation(str(row["agent_id"]))
            scheduled += 1
        return scheduled

    async def _environment_agent_ids_page(
        self, environment_name: str, after: str | None
    ) -> list[dict[str, Any]]:
        ids = await self._agent_repo.list_agent_ids_by_environment(
            environment_name, after_agent_id=after, limit=SWEEP_PAGE_SIZE
        )
        return [{"agent_id": agent_id} for agent_id in ids]

    def _live_agents(self) -> AsyncIterator[dict[str, Any]]:
        """Every Agent that is not deleted, read in pages.

        Preparation must reach every Agent; the display listing is capped.
        """

        return iter_keyset_pages(
            lambda after: self._agent_repo.list_live_agents_page(
                after_agent_id=after, limit=SWEEP_PAGE_SIZE
            ),
            key="agent_id",
        )

    async def list_agents(self, user: UserContext) -> list[dict[str, Any]]:
        """The agents this caller may see — public ones, plus their own.

        This read answers the agent picker, so the per-agent visibility has to
        be applied here as well as in the console's management list. Enforcing
        it only there would leave `private` as decoration on a record every user
        could still enumerate through this call.
        """
        await self.ensure_bootstrap()
        rows = await self._agent_repo.list_visible_agents(
            user.user_id, platform_admin=is_platform_admin(user.roles)
        )
        favorites = await agent_catalog.favorite_repository().agent_ids_for(user.user_id)
        return [
            {**self._user_view(user, r), "favorited": str(r.get("agent_id") or "") in favorites}
            for r in rows
        ]

    async def set_favorite(self, user: UserContext, agent_id: str, *, favorite: bool) -> dict[str, Any]:
        """Favorite or unfavorite an Agent the caller may see; repeating either is a no-op."""
        await self._must_view(user, agent_id)
        repo = agent_catalog.favorite_repository()
        if favorite:
            await repo.add(user.user_id, agent_id)
        else:
            await repo.remove(user.user_id, agent_id)
        return {"agent_id": agent_id, "favorited": favorite}

    async def list_agent_tags(self, user: UserContext) -> list[dict[str, Any]]:
        """The tags of the Agents the caller may see, most used first."""
        return await self._agent_repo.visible_tag_counts(
            user.user_id, platform_admin=is_platform_admin(user.roles)
        )

    async def list_agents_page(
        self,
        user: UserContext,
        *,
        limit: int,
        cursor: str | None = None,
        query: str = "",
        status: str = "all",
    ) -> dict[str, Any]:
        """One page of the Agents this caller may see, by name regardless of case.

        ``query`` matches the name, display name and model, case-insensitively;
        ``status`` keeps enabled or disabled Agents. Both apply before the page
        is cut, so a page is short only at the end of the list. The first page
        (no ``cursor``) also counts every Agent the caller may see, and how
        many of them are enabled, whatever the narrowing.
        """
        if status not in AGENT_LIST_STATUSES:
            raise APIError(
                code="INVALID_REQUEST",
                message=f"status must be one of {sorted(AGENT_LIST_STATUSES)}",
                status_code=400,
            )
        try:
            after = decode_list_cursor(cursor, _AGENT_LIST_CURSOR_KEYS)
        except InvalidListCursor as exc:
            raise APIError(
                code="INVALID_REQUEST",
                message="cursor is not one this list returned",
                status_code=400,
            ) from exc
        await self.ensure_bootstrap()
        platform_admin = is_platform_admin(user.roles)
        fetched = await self._agent_repo.list_visible_agents_page(
            user.user_id,
            platform_admin=platform_admin,
            after=(after[0], after[1]) if after is not None else None,
            limit=limit + 1,
            needle=query,
            enabled={"enabled": True, "disabled": False}.get(status),
        )
        rows, has_more = fetched[:limit], len(fetched) > limit
        page: dict[str, Any] = {
            "agents": [self._user_view(user, row) for row in rows],
            "has_more": has_more,
            "next_cursor": (
                encode_list_cursor(
                    {
                        "name_key": str(rows[-1].get(NAME_SORT_KEY_FIELD) or ""),
                        "agent_id": str(rows[-1].get("agent_id") or ""),
                    }
                )
                if has_more and rows
                else None
            ),
        }
        if after is None:
            page["total"], page["enabled"] = await self._agent_repo.count_visible_agents(
                user.user_id, platform_admin=platform_admin
            )
        return page

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
        return await self._prepared_runtime_status(agent_id)

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

    async def _prepared_runtime_status(self, agent_id: str) -> dict[str, Any]:
        from astrabox.core.service.orchestrator.agent.prepared_capacity import LAST_MISS_FIELD
        from astrabox.core.service.orchestrator.agent.prepared_slots import (
            _manifest_is_reapable, slot_manifests,
        )
        from astrabox.core.service.orchestrator.runtime.runtime_profile import (
            resolve_sandbox_tenancy,
        )
        from astrabox.seams.sandbox import (
            SANDBOX_TENANCY_CONVERSATION,
            sandbox_for_name,
        )

        agent = await self._agent_repo.read_ledger(agent_id)
        if agent is None:
            raise APIError(code="AGENT_NOT_FOUND", message="agent not found", status_code=404)
        target = int(agent.get("prepared_capacity") or 0)
        enabled = target > 0 and await self._runtime_enabled(agent)
        last_miss_at = str(agent.get(LAST_MISS_FIELD) or "").strip() or None
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
                    "target": target,
                    "ready": False,
                    "prepared_count": 0,
                    "last_miss_at": last_miss_at,
                    "state": "preparing",
                    "client_pool_name": pool_name or None,
                    "last_error": agent.get("_prepared_runtime_error"),
                }
        conversation_owned = (
            template is not None
            and resolve_sandbox_tenancy(template) == SANDBOX_TENANCY_CONVERSATION
        )
        pool = None
        if enabled and pool_name:
            if not pool_backend:
                raise APIError(
                    code="AGENT_PREWARM_CONFIG_INVALID",
                    message=f"client pool {pool_name!r} has no persisted sandbox backend",
                    status_code=500,
                )
            pool = await sandbox_for_name(pool_backend).describe_client_pool(pool_name)
        # A pool box holds one conversation box's unit, or the seats an Agent
        # box was prepared with; seats already taken out with their box wait in
        # the Agent's ledger.
        units_per_box = 1 if conversation_owned else int(agent.get("_seats_per_box") or 0)
        pooled = pool.idle_count * units_per_box if pool is not None and pool.ready else 0
        generation = str(agent.get("_prepared_runtime_generation") or "")
        slots = slot_manifests(agent)
        usable = [
            manifest
            for manifest in slots
            if manifest.get("state") == "prepared"
            and _manifest_is_reapable(manifest, current_runtime_generation=generation) is None
        ]
        # A seat of the current generation that is not usable has outlived its
        # idle lease and waits for the reaper; say so rather than "prepared".
        expired = any(
            manifest.get("state") == "prepared"
            and manifest.get("runtime_generation") == generation
            and manifest not in usable
            for manifest in slots
        )
        # The seat the next claim takes: the oldest usable one.
        upcoming = min(usable, key=lambda m: str(m.get("prepared_at") or ""), default={})
        newest = max(slots, key=lambda m: str(m.get("prepared_at") or ""), default={})
        prepared_count = pooled + len(usable) if enabled else 0
        idle_box = pool.idle_sandbox_ids[0] if pool is not None and pool.idle_sandbox_ids else None
        return {
            "enabled": enabled,
            "target": target if enabled else 0,
            "ready": prepared_count > 0,
            "prepared_count": prepared_count,
            "last_miss_at": last_miss_at,
            "state": (
                "prepared" if prepared_count
                else "expired" if expired
                else pool.lifecycle_state if pool is not None
                else str(newest.get("state") or "").strip() or None
            ),
            "placement": "conversation_box" if conversation_owned else "shared_slot",
            "runtime_generation": (str(agent.get("_runtime_generation") or "").strip() or None),
            "client_pool_name": pool_name or None,
            "sandbox_id": str(upcoming.get("sandbox_id") or "").strip() or idle_box,
            "prepared_at": str(upcoming.get("prepared_at") or "").strip() or None,
            "last_error": (
                str(agent.get("_prepared_runtime_error") or "").strip()
                or (
                    "Sandbox provider reported a prewarm preparation failure."
                    if pool is not None and pool.last_error
                    else None
                )
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
        if int(agent.get("prepared_capacity") or 0) <= 0 or not await self._runtime_enabled(agent):
            raise APIError(
                code="AGENT_PREWARM_CONFIG_INVALID",
                message="enable the Agent and its Environment and give it prepared capacity before requesting a refresh",
                status_code=409,
            )
        if not await self._agent_repo.request_prewarm_refresh(
            agent_id,
            version=int(agent["version"]),
            prepared_capacity=int(agent.get("prepared_capacity") or 0),
            enabled=agent.get("enabled") is not False,
            prewarm_revision=agent.get("_prewarm_revision"),
            new_revision=uuid.uuid4().hex,
        ):
            raise APIError(
                code="AGENT_RUNTIME_GENERATION_CONFLICT",
                message="Agent changed while requesting prewarm refresh; reload its configuration",
                status_code=409,
            )
        self.schedule_runtime_reconciliation(agent_id)
        return await self._prepared_runtime_status(agent_id)

    async def delete_agent(self, user: UserContext, agent_id: str) -> dict[str, Any]:
        agent = await self._must_view(user, agent_id)
        if not can_manage_agent(agent, user.user_id, user.roles):
            raise APIError(
                code="FORBIDDEN",
                message="only the creator or an admin may delete this agent",
                status_code=403,
            )
        await self._agent_repo.mark_deleting(agent_id)
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
        # the delete could not confirm gone therefore has to be recorded for the
        # Agent before it stops being findable, or the box outlives every way of
        # naming it.
        keep = keep_name_updates(destruction, row=agent)
        if keep and destruction is not None and destruction.leaked_sandbox_id:
            logger.error(
                "agent delete could not confirm the destruction of agent=%s "
                "sandbox=%s; it is recorded as undestroyed for the Agent: %s",
                agent_id,
                sandbox_id,
                destruction.detail,
            )
            await self._agent_repo.record_undestroyed_sandbox(
                agent_id, destruction.leaked_sandbox_id
            )
            agent.update(keep)
        await self._reconcile_agent_runtime(agent_id)
        retirement = self._pool_retirement_tasks.get(agent_id)
        if retirement is not None:
            await retirement
        await self._release_records_naming(agent_id)
        await self._agent_repo.soft_delete(agent_id)
        agent["state"] = AgentState.DELETED.value
        agent["expires_at"] = None
        # The pointer is cleared only on a confirmed destruction, by the same
        # rule every other row obeys.
        if destruction is not None and destruction.confirmed:
            agent["sandbox_id"] = None
        return self._sanitize(agent)

    async def _release_records_naming(self, agent_id: str) -> None:
        """Remove what other records keep for an Agent being deleted.

        The one place an Agent deletion reaches records outside the Agent's own
        rows. It runs before the Agent is hidden, so a failure here leaves the
        Agent to be deleted again; the Agent's own rows stay for the
        conversations that name it.
        """
        # Its schedules, webhooks and channel bindings must stop firing.
        await self._platform.delete_target_deployments(agent_id=agent_id)
        await agent_catalog.favorite_repository().remove_agent(agent_id)

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

        while True:
            pending = await self._agent_repo.list_retiring_client_pools(agent_id)
            if not pending:
                return
            retired = pending[0]
            await retire_agent_client_pool(retired["name"], backend_name=retired["backend"])
            await self._agent_repo.remove_retiring_client_pool(agent_id, retired["name"])

    async def _reconcile_agent_runtime(self, agent_id: str) -> None:
        """Publish one platform generation and prepare its configured capacity."""

        from astrabox.core.service.orchestrator.agent.client_pool import (
            CLIENT_POOL_BACKEND_FIELD,
            CLIENT_POOL_NAME_FIELD,
            ensure_agent_client_pool,
            retire_agent_client_pool,
        )
        from astrabox.core.service.orchestrator.agent.prepared_slots import (
            reap_stale_slot_manifests,
            retire_prepared_runtime,
        )
        from astrabox.core.service.orchestrator.agent.runtime_generation import (
            reconcile_runtime_generation,
        )

        target = str(agent_id or "").strip()
        lock = self._runtime_reconciliation_locks.setdefault(target, asyncio.Lock())
        async with lock:
            row = await self._agent_repo.read_ledger(target)
            if not isinstance(row, dict):
                return
            previous_pool_name = str(row.get(CLIENT_POOL_NAME_FIELD) or "").strip()
            previous_pool_backend = str(row.get(CLIENT_POOL_BACKEND_FIELD) or "").strip()
            # What this pass reports is recorded only for the definition and
            # rebuild request it started from.
            version = int(row["version"])
            revision = row.get("_prewarm_revision")
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
                if not enabled or int(row.get("prepared_capacity") or 0) <= 0:
                    await retire_prepared_runtime(
                        target,
                        reason="Agent runtime preparation is disabled",
                        agent_repo=self._agent_repo,
                        runtime_manager=self._runtime_manager,
                    )
                    if previous_pool_name:
                        await retire_agent_client_pool(
                            previous_pool_name,
                            backend_name=previous_pool_backend,
                        )
                        # A Session start may have recorded a newer namespace
                        # meanwhile; it stays named, and another pass retires it.
                        cleared = await self._agent_repo.clear_client_pool(
                            target, name=previous_pool_name, backend=previous_pool_backend
                        )
                        if not cleared:
                            self._reconciliation_requested.add(target)
                else:
                    refreshed = await self._agent_config.resolve_agent_harness(target)
                    if refreshed is None:
                        return
                    if isinstance(refreshed, dict):
                        refreshed["runtime_generation"] = generation
                    else:
                        refreshed.runtime_generation = generation
                    # The namespace is recorded, and the one it displaces queued
                    # for retirement, before it starts; an edit landing during
                    # this pass leaves it named for the next pass to retire.
                    await ensure_agent_client_pool(
                        refreshed,
                        runtime_manager=self._runtime_manager,
                        agent_repo=self._agent_repo,
                    )
                    if await self._agent_repo.list_retiring_client_pools(target):
                        self._schedule_pool_retirement(target)
                    from astrabox.core.service.orchestrator.runtime.runtime_profile import (
                        resolve_sandbox_tenancy,
                    )
                    from astrabox.seams.sandbox import SANDBOX_TENANCY_CONVERSATION

                    if resolve_sandbox_tenancy(refreshed) == SANDBOX_TENANCY_CONVERSATION:
                        # Seats belong to shared boxes; an Agent that moved to
                        # conversation boxes has none left to hand out.
                        await retire_prepared_runtime(
                            target,
                            reason="conversation capacity moved to the client pool",
                            agent_repo=self._agent_repo,
                            runtime_manager=self._runtime_manager,
                        )
                    else:
                        # The client pool prepares and replenishes seats; the
                        # ledger only drops the ones that went stale.
                        await reap_stale_slot_manifests(
                            refreshed,
                            runtime_manager=self._runtime_manager,
                            agent_repo=self._agent_repo,
                        )
                await self._agent_repo.record_prepared_runtime(
                    target, version=version, prewarm_revision=revision, generation=generation
                )
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                await self._agent_repo.record_prepared_runtime(
                    target,
                    version=version,
                    prewarm_revision=revision,
                    error=f"{type(exc).__name__}: {str(exc).strip()}",
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
        source_type: str = "agent",
    ) -> dict[str, Any]:
        """Create a new user-visible conversation session with an agent.

        The conversation is provisioned through the session lifecycle's
        ``create_session``, whose startup plans the Agent conversation's
        runtime subject.
        """
        agent = await self._must_access(agent_id)
        harness = await self._agent_config.resolve_agent_harness(
            agent_id, viewer_user_id=user.user_id, viewer_roles=user.roles
        )
        if harness is None:
            raise APIError(code="TEMPLATE_NOT_ALLOWED", message="agent not found", status_code=403)
        return await self._create_conversation(user, agent_id, agent, idempotency_key=idempotency_key, source_type=source_type)

    async def start_integration_conversation(
        self, user: UserContext, agent_id: str,
    ) -> dict[str, Any]:
        """Start an authenticated, manager-configured channel as its delegated user.

        Channel configuration owns delegation authorization. This internal entry
        grants use of that Agent without granting the execution user any roles
        or changing the browser's Agent visibility checks.
        """
        agent = await self._must_access(agent_id)
        return await self._create_conversation(user, agent_id, agent, source_type="channel")

    async def _create_conversation(
        self, user: UserContext, agent_id: str, agent: dict[str, Any], *,
        idempotency_key: str | None = None,
        source_type: str = "agent",
    ) -> dict[str, Any]:
        create_options: dict[str, Any] = {
            # No permission_mode: creation falls to the product default for
            # (agent_chat, engine) declared once in engine/capabilities.py.
            "session_kind": "agent_chat",
            "workspace_ref": {"kind": "agent", "agent_id": agent_id},
            "source_type": source_type,
            "agent_id": agent_id,
            "deployment_name": agent.get("name"),
            "title": agent.get("name"),
        }
        if idempotency_key is not None:
            create_options["idempotency_key"] = idempotency_key
        response = await self._platform._session_lifecycle.create_session(user, **create_options)
        session_id = str((response or {}).get("session_id") or "").strip()
        return {
            "session_id": session_id,
            "agent_id": agent_id,
            "deployment_name": agent.get("name"),
        }


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
        # a stored document may carry but no client may see. The Artifacts
        # switch is reported as it takes effect, so an Agent stored without
        # it reads as on rather than as a missing value a form shows as off.
        return {
            **{
                k: v
                for k, v in doc.items()
                if not k.startswith("_") and k not in AGENT_PRIVATE_STORED_FIELDS
            },
            "artifacts_enabled": agent_artifacts_enabled(doc),
        }
