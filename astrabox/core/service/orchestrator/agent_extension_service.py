"""Agent authorization and assignment for an external extension catalog."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.common.utils.user_context import UserContext
from astrabox.common.utils.user_context import default_org_id
from astrabox.core.service.orchestrator.agent_access import can_manage_agent
from astrabox.core.service.orchestrator.mcp_assignments import (
    AGENT_MCP_ASSIGNMENTS_FIELD,
    assignments_for_provider,
    replace_provider_assignments,
)
from astrabox.persistence.repository import AgentRepository
from astrabox.seams.extensions import (
    ExtensionCatalog,
    ExtensionProvider,
    extension_provider_for_name,
)

logger = get_logger(__name__)

#: The Agent's Skill material compiled from a console selection. MCP servers are
#: deliberately absent: they are recorded as assignments, which is the one list
#: resolution reads, and the runtime binding is produced from the catalog at
#: Session start rather than frozen here.
AGENT_EXTENSION_CATALOG_FIELD = "extension_catalog"


def _failure(code: str, message: str, status: int) -> APIError:
    return APIError(code=code, message=message, status_code=status)


def _clean_ids(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        raise _failure("INVALID_REQUEST", "extension ids must be lists", 400)
    result: list[str] = []
    seen: set[str] = set()
    for value in raw:
        item_id = str(value or "").strip()
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        result.append(item_id)
    return result


def _version(agent: dict[str, Any]) -> int:
    try:
        return max(int(agent.get("version") or 1), 1)
    except (TypeError, ValueError):
        return 1


class AgentExtensionService:
    """Enforce Agent authorization around a provider-owned extension catalog."""

    def __init__(
        self,
        *,
        agent_repo: AgentRepository | None = None,
        provider: ExtensionProvider | None = None,
        schedule_runtime_reconciliation: Callable[..., None] | None = None,
    ) -> None:
        self._agents = agent_repo or AgentRepository()
        self._configured_provider = provider
        self._schedule_runtime_reconciliation = schedule_runtime_reconciliation

    def provider(self) -> ExtensionProvider:
        """Resolve the configured provider through the extension seam."""

        if self._configured_provider is not None:
            return self._configured_provider
        from astrabox.common.utils.settings import load_astrabox_settings

        settings = load_astrabox_settings()
        return extension_provider_for_name(
            str(getattr(settings, "extension_provider", "") or "").strip()
        )

    async def get_catalog(self, user: UserContext, agent_id: str) -> dict[str, Any]:
        agent = await self.assert_can_manage(user, agent_id)
        provider = self.provider()
        catalog = await provider.list_catalog(org_id=_org_id(user, agent))
        return self._view(agent, catalog, provider.name)

    async def set_assignment(
        self,
        user: UserContext,
        agent_id: str,
        *,
        mcp_server_ids: list[str],
        skill_ids: list[str],
    ) -> dict[str, Any]:
        selected_mcp = _clean_ids(mcp_server_ids)
        selected_skills = _clean_ids(skill_ids)
        # Authorization precedes the provider call so an unauthorized caller
        # cannot inspect catalog availability or consume provider capacity.
        authorized = await self.assert_can_manage(user, agent_id)
        provider = self.provider()
        catalog = await provider.list_catalog(org_id=_org_id(user, authorized))
        mcp_by_id = {item.item_id: item for item in catalog.mcp_servers}
        skills_by_id = {item.item_id: item for item in catalog.skills}
        missing_mcp = [item_id for item_id in selected_mcp if item_id not in mcp_by_id]
        missing_skills = [
            item_id for item_id in selected_skills if item_id not in skills_by_id
        ]
        if missing_mcp or missing_skills:
            parts = []
            if missing_mcp:
                parts.append(f"unknown MCP server ids: {missing_mcp}")
            if missing_skills:
                parts.append(f"unknown Skill ids: {missing_skills}")
            raise _failure("INVALID_REQUEST", "; ".join(parts), 400)

        # Skills are materialized into descriptors here because the Git
        # materializer consumes a descriptor, not a catalog id. MCP servers are
        # not: their runtime binding is produced from the catalog at Session
        # start, so a URL or transport corrected upstream takes effect without
        # anyone re-saving this form.
        runtime = provider.materialize(
            mcp_servers=(),
            skills=tuple(skills_by_id[item_id] for item_id in selected_skills),
        )
        snapshot = {
            "skill_ids": selected_skills,
            "skills": list(runtime.skill_descriptors),
        }
        for _ in range(3):
            agent = await self.assert_can_manage(user, agent_id)
            current_snapshot = (
                agent.get(AGENT_EXTENSION_CATALOG_FIELD)
                if isinstance(agent.get(AGENT_EXTENSION_CATALOG_FIELD), dict)
                else {}
            )
            current_mcp = assignments_for_provider(
                agent.get(AGENT_MCP_ASSIGNMENTS_FIELD), provider.name
            )
            if current_snapshot == snapshot and current_mcp == selected_mcp:
                return self._view(agent, catalog, provider.name)
            version = _version(agent)
            applied = await self._agents.compare_and_update_agent(
                str(agent.get("agent_id") or agent_id),
                expected={"version": version},
                updates={
                    AGENT_EXTENSION_CATALOG_FIELD: snapshot,
                    # Only this provider's rows change; servers assigned from
                    # the administrator API or another catalog stay put.
                    AGENT_MCP_ASSIGNMENTS_FIELD: replace_provider_assignments(
                        agent.get(AGENT_MCP_ASSIGNMENTS_FIELD),
                        provider.name,
                        selected_mcp,
                    ),
                    "version": version + 1,
                    "updated_at": utcnow_iso(),
                    "updated_by": user.user_id,
                },
            )
            if applied:
                updated = await self.assert_can_manage(user, agent_id)
                self._schedule_runtime_reconciliation_after_write(agent, updated)
                return self._view(updated, catalog, provider.name)
        raise _failure(
            "AGENT_VERSION_CONFLICT",
            "Agent was modified concurrently; reload and save extensions again",
            409,
        )

    def _schedule_runtime_reconciliation_after_write(
        self, before: dict[str, Any], after: dict[str, Any]
    ) -> None:
        """Extensions are baked into runtime preparation, so a changed
        assignment changes the Agent's runtime generation. The
        assignment has already committed; scheduling is the same post-commit
        optimization as an Agent write and must not fail the request.
        """

        _ = before
        trigger = self._schedule_runtime_reconciliation
        try:
            if trigger is None:
                from astrabox.core.service.orchestrator.service_registry import (
                    get_agent_service,
                )

                trigger = get_agent_service().schedule_runtime_reconciliation
            trigger(str(after.get("agent_id") or ""))
        except Exception as exc:
            logger.warning(
                "could not schedule Agent runtime reconciliation after extension "
                "assignment: "
                "agent=%s error=%s",
                after.get("agent_id"),
                exc,
                exc_info=True,
            )

    async def assert_can_manage(
        self,
        user: UserContext,
        agent_id: str,
    ) -> dict[str, Any]:
        """Return the Agent when ``user`` may manage it, or raise an API error."""

        target = str(agent_id or "").strip()
        agent = await self._agents.get_agent(target) if target else None
        if agent is None:
            raise _failure("AGENT_NOT_FOUND", "Agent not found", 404)
        if not can_manage_agent(agent, user.user_id, user.roles):
            raise _failure(
                "AGENT_MANAGEMENT_REQUIRED",
                "You may not manage this Agent",
                403,
            )
        return agent

    @staticmethod
    def _view(
        agent: dict[str, Any], catalog: ExtensionCatalog, provider_name: str
    ) -> dict[str, Any]:
        snapshot = (
            agent.get(AGENT_EXTENSION_CATALOG_FIELD)
            if isinstance(agent.get(AGENT_EXTENSION_CATALOG_FIELD), dict)
            else {}
        )
        return {
            "agent_id": str(agent.get("agent_id") or ""),
            "mcp_servers": [item.public_view() for item in catalog.mcp_servers],
            "skills": [item.public_view() for item in catalog.skills],
            # Read back from the assignment list, so what the form shows ticked
            # is what resolution will actually use.
            "selected_mcp_server_ids": assignments_for_provider(
                agent.get(AGENT_MCP_ASSIGNMENTS_FIELD), provider_name
            ),
            "selected_skill_ids": list(snapshot.get("skill_ids") or []),
        }


def _org_id(user: UserContext, agent: dict[str, Any]) -> str:
    """The tenant whose catalog applies: the Agent's, which the user manages."""
    return (
        str(agent.get("org_id") or "").strip()
        or str(getattr(user, "org_id", None) or "").strip()
        or default_org_id()
    )


__all__ = [
    "AGENT_EXTENSION_CATALOG_FIELD",
    "AgentExtensionService",
]
