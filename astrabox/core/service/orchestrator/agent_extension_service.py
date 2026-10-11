"""Agent authorization and assignment for an external extension catalog."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
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
    normalize_assignments,
    replace_provider_assignments,
)
from astrabox.persistence.repository import AgentRepository
from astrabox.seams.extensions import (
    EXTENSION_PROVIDER_NONE,
    ExtensionCatalog,
    ExtensionProvider,
    selected_extension_provider,
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


def configured_extension_provider() -> ExtensionProvider | None:
    """The deployment's extension provider, or ``None`` when it runs no catalog.

    Resolved from ``ASTRABOX_EXTENSION_PROVIDER`` through the extension seam on
    every call; ``none`` answers ``None``.
    """

    from astrabox.common.utils.settings import load_astrabox_settings

    settings = load_astrabox_settings()
    return selected_extension_provider(
        str(getattr(settings, "extension_provider", "") or "").strip()
    )


def extension_catalog_enabled() -> bool:
    """Whether the deployment runs an extension catalog at all.

    Read from the setting alone, so a caller that only needs this answer does
    not resolve a provider; startup has already refused a setting that names
    no installed provider.
    """

    from astrabox.common.utils.settings import load_astrabox_settings

    setting = str(getattr(load_astrabox_settings(), "extension_provider", "") or "")
    return setting.strip().lower() != EXTENSION_PROVIDER_NONE


@dataclass(frozen=True)
class CatalogSelection:
    """What a record has selected from extension catalogs, as it stores it.

    ``mcp_servers`` are the ``(provider, item_id)`` assignment rows and
    ``skill_ids`` / ``skills`` the Skill snapshot a catalog selection compiled.
    Agents, Assistants and the Assistant-defaults layer all store these two
    fields.
    """

    mcp_servers: tuple[tuple[str, str], ...] = ()
    skill_ids: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.mcp_servers or self.skill_ids or self.skills)

    def describe(self) -> str:
        """The selection in words, for a message that names what was refused."""

        parts: list[str] = []
        providers = sorted({provider for provider, _item_id in self.mcp_servers})
        if providers:
            parts.append("MCP servers from " + ", ".join(repr(name) for name in providers))
        if self.skill_ids or self.skills:
            parts.append("catalog Skills")
        return " and ".join(parts)


def _stored_strings(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(text for text in (str(value or "").strip() for value in raw) if text)


def catalog_selection(record: Mapping[str, Any]) -> CatalogSelection:
    """Read the catalog selection a stored Agent, Assistant or layer holds."""

    snapshot = record.get(AGENT_EXTENSION_CATALOG_FIELD)
    skills = snapshot if isinstance(snapshot, Mapping) else {}
    return CatalogSelection(
        mcp_servers=tuple(normalize_assignments(record.get(AGENT_MCP_ASSIGNMENTS_FIELD))),
        skill_ids=_stored_strings(skills.get("skill_ids")),
        skills=_stored_strings(skills.get("skills")),
    )


def extension_catalog_disabled(message: str) -> APIError:
    """``EXTENSION_CATALOG_DISABLED``: the deployment runs no extension catalog."""

    return APIError(code="EXTENSION_CATALOG_DISABLED", message=message, status_code=409)


def catalog_selection_in_use(
    record: Mapping[str, Any],
    *,
    owner: str,
    admits_work: bool,
) -> bool:
    """Whether ``record``'s catalog selection takes part in a runtime view.

    It always does while the deployment runs an extension catalog. Without one
    nothing can resolve it, so a view that admits new work (a conversation, a
    prepared runtime) refuses with ``EXTENSION_CATALOG_DISABLED`` rather than
    running with fewer tools than the record names, and a view that only reads
    leaves the selection out, so existing conversations stay readable. ``owner``
    names the record in that refusal.
    """

    selection = catalog_selection(record)
    if not selection or extension_catalog_enabled():
        return True
    if admits_work:
        raise extension_catalog_disabled(
            f"the catalog selection of {owner} ({selection.describe()}) needs an "
            "extension catalog, and this deployment runs none "
            "(ASTRABOX_EXTENSION_PROVIDER=none). Remove the selection on its "
            "console page, or select an extension provider."
        )
    return False


def clear_catalog_selection(*, mcp_server_ids: list[str], skill_ids: list[str]) -> dict[str, Any]:
    """The fields a save writes when the deployment runs no extension catalog.

    The only selection a record can then be given is none, and saving it
    removes every catalog selection the record holds, whichever provider it
    came from. Anything else is refused with ``EXTENSION_CATALOG_DISABLED``.
    """

    if _clean_ids(mcp_server_ids) or _clean_ids(skill_ids):
        raise extension_catalog_disabled(
            "this deployment runs no extension catalog (ASTRABOX_EXTENSION_PROVIDER=none); "
            "only an empty selection, which removes the stored one, can be saved"
        )
    return {
        AGENT_EXTENSION_CATALOG_FIELD: {"skill_ids": [], "skills": []},
        AGENT_MCP_ASSIGNMENTS_FIELD: [],
    }


def refuse_catalog_read() -> APIError:
    """The refusal a catalog read answers when the deployment runs no catalog."""

    return extension_catalog_disabled(
        "this deployment runs no extension catalog (ASTRABOX_EXTENSION_PROVIDER=none)"
    )


async def warn_about_catalog_selections_without_a_catalog() -> None:
    """Name each record that still holds a catalog selection while no catalog runs.

    Startup proceeds: an operator may stop running a catalog while keeping the
    records that selected from it. Each enabled Agent, each Assistant and the
    deployment's Assistant defaults named here fail new work with
    ``EXTENSION_CATALOG_DISABLED`` until the selection is removed; this names
    them before anyone meets that.
    """

    from astrabox.persistence.repository.assistant_catalog_repository import (
        AssistantCatalogRepository,
    )
    from astrabox.persistence.repository.assistant_defaults_repository import (
        DEPLOYMENT_SCOPE,
        AssistantDefaultsRepository,
    )

    if extension_catalog_enabled():
        return
    named: list[tuple[str, str, CatalogSelection]] = []
    for agent in await AgentRepository().list_catalog_selections():
        selection = catalog_selection(agent)
        if selection and agent.get("enabled") is not False:
            named.append((
                f"Agent {agent.get('name')!r} ({agent.get('agent_id')})",
                "its new conversations fail",
                selection,
            ))
    for assistant in await AssistantCatalogRepository().list_catalog_selections():
        selection = catalog_selection(assistant)
        if selection:
            named.append((
                f"Assistant {assistant.get('display_name')!r} ({assistant.get('assistant_id')})",
                "its new conversations fail",
                selection,
            ))
    layer = await AssistantDefaultsRepository().get_layer(DEPLOYMENT_SCOPE) or {}
    selection = catalog_selection(layer)
    if selection:
        named.append((
            "The deployment's Assistant defaults",
            "every Assistant's new conversations fail",
            selection,
        ))
    for owner, consequence, selected in named:
        logger.warning(
            "%s selected %s, but ASTRABOX_EXTENSION_PROVIDER=none runs no "
            "extension catalog: %s with EXTENSION_CATALOG_DISABLED until that "
            "selection is removed on its console page or an extension provider "
            "is selected",
            owner,
            selected.describe(),
            consequence,
        )


async def compile_extension_selection(
    provider: ExtensionProvider,
    *,
    org_id: str,
    mcp_server_ids: list[str],
    skill_ids: list[str],
) -> tuple[ExtensionCatalog, dict[str, Any], list[str]]:
    """Check a selection against the catalog; return it with the record's snapshot.

    Agents and Assistants store the same two things: the selected MCP ids as
    this provider's assignment rows, and the selected Skills as descriptors.
    Skills are materialized into descriptors here because the Git
    materializer consumes a descriptor, not a catalog id. MCP servers are
    not: their runtime binding is produced from the catalog at Session start,
    so a URL or transport corrected upstream takes effect without anyone
    re-saving the form.
    """

    selected_mcp = _clean_ids(mcp_server_ids)
    selected_skills = _clean_ids(skill_ids)
    catalog = await provider.list_catalog(org_id=org_id)
    mcp_by_id = {item.item_id: item for item in catalog.mcp_servers}
    skills_by_id = {item.item_id: item for item in catalog.skills}
    missing_mcp = [item_id for item_id in selected_mcp if item_id not in mcp_by_id]
    missing_skills = [item_id for item_id in selected_skills if item_id not in skills_by_id]
    if missing_mcp or missing_skills:
        parts = []
        if missing_mcp:
            parts.append(f"unknown MCP server ids: {missing_mcp}")
        if missing_skills:
            parts.append(f"unknown Skill ids: {missing_skills}")
        raise _failure("INVALID_REQUEST", "; ".join(parts), 400)
    runtime = provider.materialize(
        mcp_servers=(),
        skills=tuple(skills_by_id[item_id] for item_id in selected_skills),
    )
    snapshot = {
        "skill_ids": selected_skills,
        "skills": list(runtime.skill_descriptors),
    }
    return catalog, snapshot, selected_mcp


def extension_catalog_view(
    record: dict[str, Any], catalog: ExtensionCatalog, provider_name: str
) -> dict[str, Any]:
    """The catalog with a record's selection marked, read back from what it stores."""

    snapshot = (
        record.get(AGENT_EXTENSION_CATALOG_FIELD)
        if isinstance(record.get(AGENT_EXTENSION_CATALOG_FIELD), dict)
        else {}
    )
    return {
        "mcp_servers": [item.public_view() for item in catalog.mcp_servers],
        "skills": [item.public_view() for item in catalog.skills],
        # Read back from the assignment list, so what the form shows ticked
        # is what resolution will actually use.
        "selected_mcp_server_ids": assignments_for_provider(
            record.get(AGENT_MCP_ASSIGNMENTS_FIELD), provider_name
        ),
        "selected_skill_ids": list(snapshot.get("skill_ids") or []),
    }


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

    def provider(self) -> ExtensionProvider | None:
        """Resolve the configured provider through the extension seam.

        ``None`` when the deployment runs no extension catalog.
        """

        if self._configured_provider is not None:
            return self._configured_provider
        return configured_extension_provider()

    async def get_catalog(self, user: UserContext, agent_id: str) -> dict[str, Any]:
        agent = await self.assert_can_manage(user, agent_id)
        provider = self.provider()
        if provider is None:
            raise refuse_catalog_read()
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
        # A malformed body is refused before anything else is looked up.
        _clean_ids(mcp_server_ids)
        _clean_ids(skill_ids)
        # Authorization precedes the provider call so an unauthorized caller
        # cannot inspect catalog availability or consume provider capacity.
        authorized = await self.assert_can_manage(user, agent_id)
        provider = self.provider()
        if provider is None:
            return await self._clear_selection(
                user, agent_id, clear_catalog_selection(
                    mcp_server_ids=mcp_server_ids, skill_ids=skill_ids
                )
            )
        catalog, snapshot, selected_mcp = await compile_extension_selection(
            provider,
            org_id=_org_id(user, authorized),
            mcp_server_ids=mcp_server_ids,
            skill_ids=skill_ids,
        )
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

    async def _clear_selection(
        self, user: UserContext, agent_id: str, cleared: dict[str, Any]
    ) -> dict[str, Any]:
        """Remove every catalog selection from the Agent; no catalog runs to check it."""

        for _ in range(3):
            agent = await self.assert_can_manage(user, agent_id)
            if not catalog_selection(agent):
                return self._view(agent, ExtensionCatalog(), "")
            version = _version(agent)
            applied = await self._agents.compare_and_update_agent(
                str(agent.get("agent_id") or agent_id),
                expected={"version": version},
                updates={
                    **cleared,
                    "version": version + 1,
                    "updated_at": utcnow_iso(),
                    "updated_by": user.user_id,
                },
            )
            if applied:
                updated = await self.assert_can_manage(user, agent_id)
                self._schedule_runtime_reconciliation_after_write(agent, updated)
                return self._view(updated, ExtensionCatalog(), "")
        raise _failure(
            "AGENT_VERSION_CONFLICT",
            "Agent was modified concurrently; reload and remove its catalog selection again",
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
        return {
            "agent_id": str(agent.get("agent_id") or ""),
            **extension_catalog_view(agent, catalog, provider_name),
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
    "CatalogSelection",
    "catalog_selection",
    "catalog_selection_in_use",
    "clear_catalog_selection",
    "compile_extension_selection",
    "configured_extension_provider",
    "extension_catalog_disabled",
    "extension_catalog_enabled",
    "extension_catalog_view",
    "refuse_catalog_read",
    "warn_about_catalog_selections_without_a_catalog",
]
