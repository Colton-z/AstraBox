from __future__ import annotations

from astrabox.seams.skills import SkillDescriptor
from astrabox.core.service.orchestrator.runtime.skill_sources import normalize_skills

import hashlib
import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, is_dataclass, replace
from typing import Any

from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.persistence.repository.environment_repository import EnvironmentRepository
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.repository.agent_repository import AgentReferenceMissing
from astrabox.persistence.repository.assistant_catalog_repository import (
    AssistantCatalogRepository,
)
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext, default_org_id
from astrabox.core.model import AgentView
from astrabox.core.service.orchestrator.agent_access import (
    VISIBILITY_PRIVATE,
    agent_admins,
    agent_allowed_user_ids,
    agent_created_by,
    assert_may_author,
    can_manage_agent,
    can_view_agent,
    is_platform_admin,
    normalize_visibility,
    sanitize_access_control_payload,
)
from astrabox.core.service.orchestrator.schema_validation import (
    invalid_request,
    validate_declared_config_bag,
)
from astrabox.core.service.orchestrator.agent.agent_catalog import normalize_catalog_fields
from astrabox.core.service.orchestrator.agent_schema import (
    AGENT_PRIVATE_STORED_FIELDS,
    agent_artifacts_enabled,
    default_prepared_capacity,
    validate_agent_payload,
)
from astrabox.core.service.orchestrator.author_boundary import (
    refuse_reserved_mcp_fields,
    validate_author_declarations,
)
from astrabox.core.service.orchestrator.environment_schema import (
    normalize_environment_payload,
    validate_environment_payload,
)
from astrabox.core.service.orchestrator.engine.capabilities import (
    capabilities_for_engine_kind,
    engine_allowed_for_session_kind,
    unsupported_engine_configuration_inputs,
)
from astrabox.core.service.orchestrator.engine.registry import (
    EngineKindNotRegistered,
)
from astrabox.core.service.orchestrator.engine.extensions import extensions_projection
from astrabox.core.service.orchestrator.agent_extension_service import (
    catalog_selection_in_use,
)
from astrabox.core.service.orchestrator.mcp_assignments import (
    AGENT_MCP_ASSIGNMENTS_FIELD,
    normalize_assignments,
)
from astrabox.core.service.orchestrator.runtime.packages import normalize_packages
from astrabox.core.service.orchestrator.sandbox_names import read_undestroyed
from astrabox.core.service.orchestrator.runtime.mcp_servers import CONNECTOR_PLATFORM_SERVER
from astrabox.seams.extensions import extension_provider_for_name

# What of an Assistant its runtime view is built from (``_resolve_assistant_harness``),
# and so what its revision covers. The rest of the row is bookkeeping or
# presentation: writing it prepares no runtime again.
_ASSISTANT_RUNTIME_FIELDS = (
    "owner_id",
    "org_id",
    "engine_kind",
    "environment_name",
    "display_name",
    "system",
    "timezone",
    "model_config_override",
    "mcp_servers",
    "skills",
    "packages",
    "engine_options",
    "extension_catalog",
    "mcp_assignments",
    "connector_servers",
    "credential_vault_ids",
)


def _session_has_runtime_reference(session: dict[str, Any]) -> bool:
    return bool(
        str(session.get("sandbox_id") or "").strip()
        or read_undestroyed(session)
        or session.get("state") == "CREATING"
        # Failed or superseded startup may retain scope without a main pointer.
        or session.get("startup_allocation")
        or session.get("retained_startup_allocations")
    )


def _authored_change(existing: dict[str, Any], updates: dict[str, Any]) -> bool:
    """Whether a write changes any of the authored fields it carries.

    An Agent's ``version``, ``updated_at`` and ``updated_by`` move with every
    change to its definition. Saving a form unchanged changes no definition and
    writes nothing.
    """

    return any(existing.get(key) != value for key, value in updates.items())


#: How many times a write that replaces a whole field re-reads after another
#: write moved the Agent's version. Such a write does not depend on what it
#: read, so it may be applied again on top of the newer definition.
_REPLACING_WRITE_ATTEMPTS = 3


def _version_conflict() -> APIError:
    return APIError(
        code="AGENT_VERSION_CONFLICT",
        message="agent was modified concurrently; retry with the latest version",
        status_code=409,
    )


def _reference_missing(error: AgentReferenceMissing) -> APIError:
    """The API error for an Agent write naming a record that does not exist."""
    if error.reference == "environment":
        return APIError(
            code="ENVIRONMENT_NOT_FOUND",
            message="environment no longer exists; reload its configuration",
            status_code=404,
        )
    if error.reference == "category":
        return invalid_request("an Agent category this Agent names was deleted; reload the categories")
    return invalid_request(f"the Agent names a {error.reference} that does not exist")


class AgentConfigService:
    """Store Agent configuration and resolve it into runtime views.

    One ``agents`` collection, keyed by ``agent_id``, holds the model, system
    prompt, extensions, authorization, and selected Environment. This service
    creates and updates those records and resolves the in-memory
    :class:`AgentView` consumed by the runtime.

    Resolution merges the agent doc with its named environment: the environment
    contributes the sandbox runtime fields and ``provider_access``, from which
    ``AgentView.model_config`` is synthesized (see the class docstring on
    ``AgentView``). An **assistant** is not an agent — it names an environment and
    carries its own overrides; :meth:`resolve_session_harness` builds its view
    from those.
    """

    def __init__(
        self,
        agent_repo: AgentRepository,
        environment_repo: EnvironmentRepository,
        *,
        assistant_repo: AssistantCatalogRepository | None = None,
        sessions_repo: SessionRepository | None = None,
        spawn_background_task: Callable[..., Any] | None = None,
    ) -> None:
        self._agent_repo = agent_repo
        self._environment_repo = environment_repo
        self._sessions_repo = sessions_repo or SessionRepository()
        # The runtime-view resolver reads the Assistant catalog directly. An
        # Assistant view combines its Environment with its own overrides; using
        # the repository here avoids a service-construction cycle.
        self._assistant_repo = assistant_repo or AssistantCatalogRepository()
        # A bound method of the still-constructing AgentPlatformService facade
        # (same inversion-of-control wiring as DeploymentService's
        # stream_message_events_ds/spawn_background_task pair).
        self._spawn_background_task = spawn_background_task

    # ── Resolution: agent/assistant doc → runtime AgentView ────────────────

    async def resolve_agent_harness(
        self,
        agent_id: str,
        *,
        viewer_user_id: str | None = None,
        viewer_roles: Sequence[str] | None = None,
        require_enabled_environment: bool = True,
    ) -> AgentView | None:
        """Resolve one Agent's runtime view by ``agent_id``.

        With ``viewer_user_id`` set, an agent the viewer can't see resolves to
        ``None``. Create/start gates translate that result into the literal
        ``TEMPLATE_NOT_ALLOWED`` authorization error code.
        """
        target = str(agent_id or "").strip()
        if not target:
            return None
        item = await self._agent_repo.get_agent(target)
        if item is None:
            return None
        if viewer_user_id is not None and not can_view_agent(
            item, viewer_user_id, viewer_roles or ()
        ):
            return None
        return await self._build_agent_view(
            item, {}, require_enabled_environment=require_enabled_environment
        )

    async def resolve_session_harness(
        self, session: dict[str, Any], *, require_enabled_environment: bool = True
    ) -> AgentView | None:
        """Resolve the runtime view for an existing Session.

        A Session resolves its Agent or Assistant by identity, not by a mutable
        display name: an Agent Session carries ``agent_id`` (resolved from the
        ``agents`` collection); an Assistant Session carries an Assistant
        ``workspace_ref`` (resolved from the Assistant's Environment and
        overrides). This is the one entry point the session kernel
        (startup / recover / runtime_ensure) calls.
        """
        if not isinstance(session, dict):
            return None
        workspace_ref = session.get("workspace_ref")
        kind = ""
        if isinstance(workspace_ref, dict):
            kind = str(workspace_ref.get("kind") or "").strip()
        assistant_id = ""
        if kind == "assistant":
            assistant_id = str(
                (workspace_ref or {}).get("assistant_id")
                or session.get("owner_id")
                or ""
            ).strip()
        if assistant_id:
            return await self._resolve_assistant_harness(
                assistant_id, require_enabled_environment=require_enabled_environment
            )
        agent_id = str(
            (workspace_ref or {}).get("agent_id")
            if isinstance(workspace_ref, dict)
            else ""
        ).strip() or str(session.get("agent_id") or "").strip()
        if not agent_id:
            return None
        view = await self.resolve_agent_harness(
            agent_id, require_enabled_environment=require_enabled_environment
        )
        if view is None:
            return None
        # The engine withholds what a channel conversation may not use
        # (runtime.mcp_servers.binding_mcp_server_definitions); the view stays
        # the Agent's, which its runtime generation is computed from.
        return replace(view, session_source_type=session.get("source_type"))

    async def _resolve_assistant_harness(
        self, assistant_id: str, *, require_enabled_environment: bool = True
    ) -> AgentView | None:
        """Build an Assistant Session's runtime view from its layers, Environment and own fields.

        The layers above the Assistant (assistant_layers) come first and its
        own fields stack on top; see that module for how each field combines.
        A catalog selection the deployment cannot resolve, on a layer or on the
        Assistant, is treated as in :meth:`_build_agent_view`.
        """
        from astrabox.core.service.orchestrator.assistant.assistant_layers import (
            LAYER_RUNTIME_FIELDS,
            inherited_layers,
            joined_instructions,
            merged_engine_options,
            narrowest,
        )

        assistant = await self._assistant_repo.get_assistant(assistant_id)
        if assistant is None:
            return None
        env_name = str(assistant.get("environment_name") or "").strip()
        env = await self._require_environment(
            env_name, ref=f"assistant '{assistant_id}'", require_enabled=require_enabled_environment
        )
        layers = await inherited_layers()
        model_override = assistant.get("model_config_override") or {}
        model = (
            str(model_override.get("model_name") or "").strip()
            or narrowest(layers, "model_name")
            or None
        )
        own_mcp_servers = self._normalize_mcp_servers(assistant.get("mcp_servers"))
        assistant_ref = f"assistant '{assistant_id}'"
        refuse_reserved_mcp_fields(own_mcp_servers, owner=assistant_ref)
        # Each record's catalog selection is checked before anything resolves,
        # so a deployment without a catalog refuses the view whole.
        selecting = [
            record
            for label, record in [*layers, (assistant_ref, assistant)]
            if catalog_selection_in_use(
                record, owner=label, admits_work=require_enabled_environment
            )
        ]
        sources: list[tuple[str, dict[str, Any]]] = []
        for label, layer in layers:
            layer_servers = self._normalize_mcp_servers(layer.get("mcp_servers")) or {}
            refuse_reserved_mcp_fields(layer_servers, owner=label)
            sources.append((label, layer_servers))
            if any(layer is record for record in selecting):
                sources.extend(await self._resolve_assigned_mcp_servers(layer))
        mcp_servers = self._merge_mcp_servers(
            [
                *sources,
                ("the Assistant's own MCP servers", own_mcp_servers or {}),
                *(
                    await self._resolve_assigned_mcp_servers(assistant)
                    if any(assistant is record for record in selecting)
                    else []
                ),
                ("its connectors", self._connector_servers(assistant)),
            ],
            ref=assistant_ref,
        ) or None
        records = [layer for _label, layer in layers] + [assistant]
        catalog_skills = normalize_skills([
            skill
            for record in selecting
            for skill in self._normalize_skills(
                (record.get("extension_catalog") if isinstance(record.get("extension_catalog"), dict) else {})
                .get("skills")
            )
        ])
        view = AgentView(
            agent_id=None,
            assistant_id=assistant_id,
            name=str(assistant.get("display_name") or assistant_id),
            model=model,
            model_config=self._synthesize_model_config(model, env),
            system=joined_instructions(layers, assistant.get("system")),
            timezone=str(assistant.get("timezone") or "").strip() or None,
            mcp_servers=mcp_servers,
            skills=normalize_skills(
                [
                    *(skill for record in records for skill in self._normalize_skills(record.get("skills"))),
                    *catalog_skills,
                ]
            ),
            catalog_skills=tuple(catalog_skills),
            packages=[
                package for record in records for package in normalize_packages(record.get("packages"))
            ],
            engine_options=merged_engine_options(layers, assistant.get("engine_options")),
            credential_vault_ids=self._dedupe_strings(
                [
                    vault_id
                    for record in records
                    for vault_id in self._normalize_credential_vault_ids(record.get("credential_vault_ids"))
                ]
            ),
            environment_name=env_name,
            assistant_revision=self._assistant_revision(
                assistant,
                env,
                layers=[
                    {field: layer.get(field) for field in LAYER_RUNTIME_FIELDS}
                    for _label, layer in layers
                ],
            ),
        )
        self._overlay_environment_runtime(view, env)
        return view

    @staticmethod
    def _assistant_revision(
        assistant: dict[str, Any], env: dict[str, Any], *, layers: list[dict[str, Any]] | None = None
    ) -> str:
        """The identity of what an Assistant's runtime is prepared from.

        The Assistant's fields its runtime view is built from
        (``_ASSISTANT_RUNTIME_FIELDS``), the runtime fields of the layers it
        inherits as the caller passes them, and the Environment's revision.
        Bookkeeping (the main conversation, the workspace id, the primary mark,
        versions and timestamps) is left out, so writing it prepares nothing
        again. It says only that the definition moved; which change matters to
        the Agent program's profile is the adapter's call when it prepares that
        profile again.
        """

        definition = {field: assistant.get(field) for field in _ASSISTANT_RUNTIME_FIELDS}
        payload = json.dumps(
            {
                "assistant": definition,
                "layers": list(layers or []),
                "environment_updated_at": str(env.get("updated_at") or ""),
            },
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    async def _build_agent_view(
        self, item: dict[str, Any], env_cache: dict[str, dict[str, Any]],
        *, require_enabled_environment: bool = True,
    ) -> AgentView:
        """Merge an agent doc with its named environment into an AgentView.

        ``require_enabled_environment=False`` builds a view that only reads (a
        session page, an admin detail, the idle sweep). Such a view also leaves
        out a catalog selection the deployment cannot resolve, which a view that
        admits work refuses (``catalog_selection_in_use``).

        Raises ``AGENT_MODEL_REQUIRED`` for an Agent that names no model, blank
        included. The resolver fills a configuration without a model from the
        deployment's default (``RuntimeConfigResolver.default_model_name``):
        right for an Assistant, while for an Agent it would run a model nobody
        chose for it.
        """
        agent_ref = f"agent '{item.get('agent_id') or item.get('name')}'"
        env_name = str(item.get("environment_name") or "").strip()
        if env_name not in env_cache:
            env_cache[env_name] = await self._require_environment(
                env_name, ref=agent_ref, require_enabled=require_enabled_environment,
            )
        env = env_cache[env_name]
        display_meta = item.get("display_meta") or {}
        model = str(item.get("model") or "").strip()
        if not model:
            raise APIError(
                code="AGENT_MODEL_REQUIRED",
                message=f"{agent_ref} names no model; set the Agent's model",
                status_code=409,
            )
        extension_catalog = (
            item.get("extension_catalog")
            if isinstance(item.get("extension_catalog"), dict)
            else {}
        )
        own_mcp_servers = self._normalize_mcp_servers(item.get("mcp_servers")) or {}
        # Only a catalog assignment below may carry `provider` or
        # `credential_target_url`. Saving refuses them on the Agent's own
        # servers; this refuses a definition saved before that check.
        refuse_reserved_mcp_fields(own_mcp_servers, owner=agent_ref)
        selecting = catalog_selection_in_use(
            item, owner=agent_ref, admits_work=require_enabled_environment
        )
        resolved_mcp_servers = self._merge_mcp_servers(
            [
                ("the Agent's own mcp_servers", own_mcp_servers),
                *(await self._resolve_assigned_mcp_servers(item) if selecting else []),
                ("its connectors", self._connector_servers(item)),
            ],
            ref=agent_ref,
        )
        catalog_skills = (
            self._normalize_skills(extension_catalog.get("skills")) if selecting else []
        )
        resolved_skills = normalize_skills(
            [
                *self._normalize_skills(item.get("skills")),
                *catalog_skills,
            ]
        )
        view = AgentView(
            agent_id=str(item.get("agent_id") or "").strip() or None,
            name=str(item.get("name") or "").strip(),
            description=item.get("description"),
            model=model,
            model_config=self._synthesize_model_config(model, env),
            system=item.get("system"),
            mcp_servers=resolved_mcp_servers or None,
            artifacts_enabled=agent_artifacts_enabled(item),
            skills=resolved_skills,
            engine_options=item.get("engine_options"),
            default_repo=self._normalize_default_repo(item.get("default_repo")),
            packages=normalize_packages(item.get("packages")),
            catalog_skills=tuple(catalog_skills),
            credential_vault_ids=self._normalize_credential_vault_ids(
                item.get("credential_vault_ids")
            ),
            environment_name=env_name,
            exposure_mode=str(item.get("exposure_mode") or "").strip() or None,
            idle_hibernate_seconds=self._coerce_positive_int(item.get("idle_hibernate_seconds")),
            terminal_panel=bool(item.get("terminal_panel", False)),
            diff_panel=bool(item.get("diff_panel", False)),
            prepared_capacity=int(item.get("prepared_capacity") or 0),
            seats_per_box=int(item.get("_seats_per_box") or 0),
            version=self._coerce_positive_int(item.get("version")),
            display_name=display_meta.get("display_name") or str(item.get("name") or ""),
            icon=display_meta.get("icon"),
            use_cases=self._normalize_use_cases(item.get("use_cases")),
            runtime_generation=(
                str(item.get("_runtime_generation") or "").strip() or None
            ),
            sandbox_generation=(
                str(item.get("_sandbox_generation") or "").strip() or None
            ),
            client_pool_epoch=(
                str(item.get("_client_pool_epoch") or "").strip() or None
            ),
            prewarm_revision=(
                str(item.get("_prewarm_revision") or "").strip() or None
            ),
        )
        self._overlay_environment_runtime(view, env)
        return view

    def _overlay_environment_runtime(self, view: AgentView, env: dict[str, Any]) -> None:
        """Overlay the environment's sandbox runtime fields onto the view."""
        # Environment writes and the built-in seed validate this required field.
        # The read projection carries that established invariant; it is not a
        # second schema validator whose answer can drift from the write path.
        view.engine_kind = str(env["engine_kind"]).strip()
        # Carried as written, empty included: empty means "follow the deployment"
        # and only resolve_runtime_template_name decides what that resolves to.
        # Substituting the environment's own name here would hand the create
        # path a container image called "claude-code".
        view.runtime_template_name = str(
            env.get("runtime_template_name") or ""
        ).strip()
        view.sandbox_backend = self._normalize_sandbox_backend(env.get("sandbox_backend"))
        # Runtime resolvers are the one place that decide what an unset or
        # unknown value means. An overlay that defaulted either field here
        # would be a second answer to that question.
        view.sandbox_tenancy = str(env.get("sandbox_tenancy") or "").strip() or None
        view.sandbox_permission_level = (
            str(env.get("sandbox_permission_level") or "").strip() or None
        )
        networking = env.get("networking")
        view.networking = networking if isinstance(networking, dict) else None
        # One parser decides what a valid tracing document is, and it runs at
        # write time and again at session start.
        tracing = env.get("tracing")
        view.tracing = tracing if isinstance(tracing, dict) else None
        view.endpoint_provider = str(env.get("endpoint_provider") or "").strip() or None
        # Empty stays None rather than becoming the default here: the sweeper is
        # the one place that resolves "the environment does not say" against the
        # deployment's own setting, so an overlay that filled it in would be a
        # second answer to the same question.
        view.idle_action = str(env.get("idle_action") or "").strip().lower() or None
        view.environment_updated_at = str(env.get("updated_at") or "").strip() or None

    @staticmethod
    def _synthesize_model_config(
        model: str | None, env: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Synthesize the runtime model_config transport dict from the model id +
        the environment's provider_access + its endpoint_provider. This is the
        resolution-boundary insulation: config_resolver / claude_code_runtime /
        hermes consume a ``model_config`` dict, so where its values are stored
        can change without reaching them (``docs/domain-model.md``).
        ``endpoint_provider`` rides in the dict so the model-endpoint override is
        a per-environment single read, with no extra parameter threaded through
        the runtime."""
        provider = env.get("provider_access") if isinstance(env, dict) else None
        provider = provider if isinstance(provider, dict) else {}
        model_config: dict[str, Any] = {}
        if model:
            model_config["model_name"] = model
        base_url = str(provider.get("base_url") or "").strip()
        if base_url:
            model_config["base_url"] = base_url
        api_key = str(provider.get("api_key") or "").strip()
        if api_key:
            model_config["api_key"] = api_key
        secret_name = str(provider.get("api_key_secret_name") or "").strip()
        if secret_name:
            model_config["api_key_secret_name"] = secret_name
        endpoint_provider = str(env.get("endpoint_provider") or "").strip() if isinstance(env, dict) else ""
        if endpoint_provider:
            model_config["endpoint_provider"] = endpoint_provider
        return model_config or None

    async def _require_environment(
        self, env_name: str, *, ref: str, require_enabled: bool = True
    ) -> dict[str, Any]:
        if not env_name:
            raise APIError(
                code="AGENT_ENVIRONMENT_REQUIRED",
                message=f"{ref} has no environment_name",
                status_code=500,
            )
        env = await self._environment_repo.get_with_secrets(env_name)
        if not env:
            raise APIError(
                code="AGENT_ENVIRONMENT_MISSING",
                message=f"{ref} references environment '{env_name}' which is missing",
                status_code=500,
            )
        if require_enabled and env.get("enabled") is False:
            raise APIError(
                code="AGENT_ENVIRONMENT_DISABLED",
                message=f"{ref} references disabled environment '{env_name}'",
                status_code=409,
            )
        return env

    # ── Access-doc reads (raw docs, ownership/visibility intact) ───────────

    async def get_agent_access_doc(self, agent_id: str) -> dict[str, Any] | None:
        """Return the raw agent doc (with created_by/admins/visibility) by id.

        Unlike :meth:`resolve_agent_harness` (a runtime-resolved ``AgentView``
        stripped of access-control fields), this returns the stored document so
        authorization checks can read its ownership/visibility. No viewer filter —
        the caller decides access from the returned fields.
        """
        target = str(agent_id or "").strip()
        if not target:
            return None
        return await self._agent_repo.get_agent(target)

    async def get_agent_access(
        self, user: UserContext, agent_id: str
    ) -> dict[str, Any]:
        """Return Agent authorization settings after the management check."""

        agent = await self._must_manage_agent_access(user, agent_id)
        return self._agent_access_view(agent)

    async def set_agent_access(
        self,
        user: UserContext,
        agent_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Write named access fields through the dedicated policy operation.

        The request replaces the named fields whole, so a write that loses the
        version race to another edit re-reads and applies it again.
        """

        updates = sanitize_access_control_payload(payload)
        for _ in range(_REPLACING_WRITE_ATTEMPTS):
            agent = await self._must_manage_agent_access(user, agent_id)
            if not _authored_change(agent, updates):
                return self._agent_access_view(agent)
            written = await self._agent_repo.update_definition(
                str(agent["agent_id"]),
                expected_version=int(agent["version"]),
                changes=updates,
                updated_by=user.user_id,
            )
            if written is not None:
                return self._agent_access_view(written)
        raise _version_conflict()

    async def _must_manage_agent_access(
        self, user: UserContext, agent_id: str
    ) -> dict[str, Any]:
        target = str(agent_id or "").strip()
        if not target:
            raise APIError(
                code="INVALID_REQUEST",
                message="agent_id is required",
                status_code=400,
            )
        agent = await self._agent_repo.get_agent(target)
        if agent is None or not can_view_agent(
            agent, user.user_id, user.roles
        ):
            raise APIError(
                code="AGENT_NOT_FOUND",
                message="agent not found",
                status_code=404,
            )
        if not can_manage_agent(agent, user.user_id, user.roles):
            raise APIError(
                code="FORBIDDEN",
                message="only an Agent manager may edit access",
                status_code=403,
            )
        return agent

    @staticmethod
    def _agent_access_view(agent: dict[str, Any]) -> dict[str, Any]:
        return {
            "created_by": agent_created_by(agent),
            "visibility": normalize_visibility(agent.get("visibility")),
            "admins": agent_admins(agent),
            "allowed_user_ids": agent_allowed_user_ids(agent),
        }

    async def count_visible_agents(self, user: UserContext | None) -> int:
        """How many Agents ``user`` may see."""
        total, _enabled = await self._agent_repo.count_visible_agents(
            str(getattr(user, "user_id", "") or ""),
            platform_admin=is_platform_admin(getattr(user, "roles", ()) or ()),
        )
        return total

    async def manageable_agent_ids(self, user: UserContext | None) -> list[str]:
        """The ids of the Agents ``user`` may manage."""
        return await self._agent_repo.manageable_agent_ids(
            str(getattr(user, "user_id", "") or ""),
            platform_admin=is_platform_admin(getattr(user, "roles", ()) or ()),
        )

    async def get_environment(self, name: str) -> dict[str, Any] | None:
        """Return the environment doc by name, credentials masked (None if missing)."""
        target = str(name or "").strip()
        if not target:
            return None
        return await self._environment_repo.get_any_by_name(target)

    async def list_environment_models(self, name: str) -> list[str]:
        """The model ids the console can offer for agents on this environment.

        Resolves the environment's ``endpoint_provider`` and asks it to
        enumerate. Providers without an authoritative catalog return ``[]``,
        which enables free-text entry. The blocking provider call runs off the
        event loop.
        """
        import asyncio

        from astrabox.common.utils.settings import load_astrabox_settings
        from astrabox.seams.model import model_endpoint_for_name

        target = str(name or "").strip()
        env = await self._environment_repo.get_with_secrets(target) if target else None
        if not env:
            return []
        settings = load_astrabox_settings()
        provider_name = (
            str(env.get("endpoint_provider") or "").strip()
            or str(getattr(settings, "model_endpoint_provider", "") or "").strip()
        )
        try:
            provider = model_endpoint_for_name(provider_name)
        except Exception:
            return []
        return await asyncio.to_thread(
            provider.list_models,
            provider_access=env.get("provider_access"),
            settings=settings,
        )

    @staticmethod
    def sanitize_agent_doc(doc: Any | None) -> dict[str, Any]:
        if not doc:
            return {}
        if isinstance(doc, dict):
            clean = dict(doc)
        elif is_dataclass(doc) and not isinstance(doc, type):
            clean = asdict(doc)
        else:
            clean = dict(doc)
        return {
            key: value
            for key, value in clean.items()
            if not str(key).startswith("_") and key not in AGENT_PRIVATE_STORED_FIELDS
        }

    async def list_agent_configs(self, user: UserContext) -> list[dict[str, Any]]:
        # Management console: only agents the user can manage — the deployment's
        # admins, plus each agent's own creator/admins.
        configs = await self._agent_repo.list_manageable_agents(
            user.user_id, platform_admin=is_platform_admin(user.roles)
        )
        return [self.sanitize_agent_doc(c) for c in configs]

    # ── Mutation: create (mint id, version=1) + update (version CAS) ────────

    async def _validate_engine_configuration(
        self,
        *,
        environment_name: str,
        engine_options: Any,
        configuration_inputs: dict[str, Any],
    ) -> dict[str, Any]:
        """Write-side gate for the Agent's Environment and engine inputs.

        The bag's keys mean nothing to the platform; the environment's engine
        declares what it accepts (``EngineExtensions.native_config``)
        and carries accepted values verbatim. Platform configuration fields are
        also checked against the adapter's declared consumers so a saved value
        cannot become an inert knob. Every Agent write checks the Environment
        even when those optional inputs are empty: an engine that cannot run
        ``agent_chat`` must be refused before it becomes a broken Agent record.
        """
        bag = engine_options if isinstance(engine_options, dict) else {}
        env_name = str(environment_name or "").strip()
        if not env_name:
            raise invalid_request(
                "Agent configuration requires environment_name"
            )
        env = await self._environment_repo.get_any_by_name(env_name)
        if not isinstance(env, dict):
            raise invalid_request(
                f"Agent environment '{env_name}' does not exist"
            )
        engine_kind = str(env.get("engine_kind") or "").strip()
        if not engine_allowed_for_session_kind(engine_kind, "agent_chat"):
            raise invalid_request(
                f"environment '{env_name}' uses engine '{engine_kind}', which "
                "does not support Agent sessions (session_kind='agent_chat')"
            )
        capabilities = capabilities_for_engine_kind(engine_kind)
        if bag:
            validate_declared_config_bag(
                bag,
                capabilities.extensions.native_config,
                bag_label="engine_options",
                owner_label=f"engine '{engine_kind}'",
            )
        unsupported = unsupported_engine_configuration_inputs(
            engine_kind,
            configuration_inputs,
        )
        if unsupported:
            raise invalid_request(
                f"engine '{engine_kind}' does not consume Agent configuration "
                f"fields: {', '.join(unsupported)}"
            )
        return env

    async def create_agent_config(
        self, user: UserContext, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Create one Agent, mint ``agent_id``, and start ``version`` at 1."""
        from astrabox.common.utils.settings import load_astrabox_settings

        assert_may_author(
            user.roles, admin_only=load_astrabox_settings().authoring_admin_only
        )
        name = str(payload.get("name") or "").strip()
        if not name:
            raise APIError(code="INVALID_REQUEST", message="name is required", status_code=400)
        editable = await normalize_catalog_fields(validate_agent_payload({**payload, "name": name}))
        packages = editable.get("packages")
        if packages is not None:
            editable["packages"] = normalize_packages(packages)
        env = await self._validate_engine_configuration(
            environment_name=str(editable.get("environment_name") or ""),
            engine_options=editable.get("engine_options"),
            configuration_inputs={
                "mcp_servers": editable.get("mcp_servers"),
                "skills": editable.get("skills"),
                "packages": editable.get("packages"),
            },
        )
        await validate_author_declarations(
            owner=f"Agent {name!r}",
            networking=env.get("networking"),
            skills=editable.get("skills"),
            packages=editable.get("packages"),
            mcp_servers=editable.get("mcp_servers"),
            default_repo=editable.get("default_repo"),
        )
        if editable.get("prepared_capacity") is None:
            # Written, not assumed on read: the capacity sweep selects Agents
            # whose stored value is positive, so an absent key would stay cold.
            editable["prepared_capacity"] = default_prepared_capacity(
                str(env.get("sandbox_backend") or "")
            )
        self._check_prepared_capacity_backend(
            int(editable["prepared_capacity"]),
            sandbox_backend=str(env.get("sandbox_backend") or ""),
        )
        await self._check_prepared_capacity_limit(
            int(editable["prepared_capacity"]), agent_id=""
        )
        self._check_terminal_panel_backend(bool(editable.get("terminal_panel")), environment=env)

        agent_id = str(uuid.uuid4())
        doc = {
            "agent_id": agent_id,
            "enabled": True,
            "created_by": user.user_id,
            "org_id": str(getattr(user, "org_id", None) or "").strip() or default_org_id(),
            "updated_by": user.user_id,
            "visibility": VISIBILITY_PRIVATE,
            **editable,
        }
        try:
            stored = await self._agent_repo.create_agent(doc)
        except AgentReferenceMissing as exc:
            raise _reference_missing(exc) from None
        return self.sanitize_agent_doc(stored)

    async def set_agent_connector_servers(
        self, user: UserContext, agent_id: str, servers: dict[str, str]
    ) -> dict[str, Any]:
        """Record the connector hub's server URL for each connector the Agent offers.

        Only the connector service writes this field, with the hub's own URLs.
        A change is a new Agent version, as any change to what its runtime
        resolves is, so prepared runtimes are replaced.
        """

        target = str(agent_id or "").strip()
        existing = await self._agent_repo.get_agent(target)
        if existing is None:
            raise APIError(code="AGENT_NOT_FOUND", message="agent not found", status_code=404)
        if not can_manage_agent(existing, user.user_id, user.roles):
            raise APIError(
                code="FORBIDDEN",
                message="only the creator or an admin may edit this agent",
                status_code=403,
            )
        value = {str(name): str(url) for name, url in servers.items()} or None
        if existing.get("connector_servers") == value:
            return self.sanitize_agent_doc(existing)
        written = await self._agent_repo.update_definition(
            target,
            expected_version=int(existing["version"]),
            changes={"connector_servers": value},
            updated_by=user.user_id,
        )
        if written is None:
            raise _version_conflict()
        return self.sanitize_agent_doc(written)

    async def upsert_agent_config(
        self,
        user: UserContext,
        agent_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Update one Agent by id with optimistic concurrency.

        Only the creator or an admin may edit. ``version`` moves with every
        change to the definition; a save that changes nothing writes nothing.
        If the payload supplies a ``version``, it must match the stored version
        (409 on mismatch); either way the write applies only while the Agent is
        still at the version this request read.
        """
        target = str(agent_id or "").strip()
        if not target:
            raise APIError(code="INVALID_REQUEST", message="agent_id is required", status_code=400)

        existing = await self._agent_repo.get_agent(target)
        if existing is None:
            raise APIError(code="AGENT_NOT_FOUND", message="agent not found", status_code=404)
        me = user.user_id
        if not can_manage_agent(existing, me, user.roles):
            raise APIError(
                code="FORBIDDEN",
                message="only the creator or an admin may edit this agent",
                status_code=403,
            )

        name = str(payload.get("name") or existing.get("name") or "").strip()
        updates = await normalize_catalog_fields(
            validate_agent_payload({**payload, "name": name}, allow_version=True)
        )
        if "name" not in payload:
            updates.pop("name", None)
        # The effective (environment, bag) pair after this update must satisfy
        # the engine's declaration: an environment switch alone can pair the
        # stored bag with an engine that declares none of its keys.
        effective_environment = str(
            updates.get("environment_name")
            or existing.get("environment_name")
            or ""
        )
        effective_bag = (
            updates["engine_options"]
            if "engine_options" in updates
            else existing.get("engine_options")
        )
        if "packages" in updates:
            updates["packages"] = normalize_packages(
                updates["packages"]
            )
        effective = {
            field: updates[field] if field in updates else existing.get(field)
            for field in ("mcp_servers", "skills", "packages", "default_repo")
        }
        env = await self._validate_engine_configuration(
            environment_name=effective_environment,
            engine_options=effective_bag,
            configuration_inputs={
                field: effective[field]
                for field in ("mcp_servers", "skills", "packages")
            },
        )
        if "prepared_capacity" in updates or "environment_name" in updates:
            self._check_prepared_capacity_backend(
                int(
                    updates["prepared_capacity"]
                    if "prepared_capacity" in updates
                    else existing.get("prepared_capacity") or 0
                ),
                sandbox_backend=str(env.get("sandbox_backend") or ""),
            )
        if "prepared_capacity" in updates:
            await self._check_prepared_capacity_limit(
                int(updates["prepared_capacity"]), agent_id=target
            )
        if "terminal_panel" in updates or "environment_name" in updates:
            self._check_terminal_panel_backend(
                bool(
                    updates["terminal_panel"]
                    if "terminal_panel" in updates
                    else existing.get("terminal_panel")
                ),
                environment=env,
            )
        # The whole effective definition, not only the changed fields: moving
        # an Agent to a stricter Environment must meet that Environment too.
        await validate_author_declarations(
            owner=f"Agent {name!r}",
            networking=env.get("networking"),
            skills=effective["skills"],
            packages=effective["packages"],
            mcp_servers=effective["mcp_servers"],
            default_repo=effective["default_repo"],
        )
        stored_version = self._coerce_positive_int(existing.get("version")) or 1
        supplied_version = payload.get("version")
        if supplied_version is not None:
            try:
                supplied_int = int(supplied_version)
            except (TypeError, ValueError):
                supplied_int = -1
            if supplied_int != stored_version:
                raise APIError(
                    code="AGENT_VERSION_CONFLICT",
                    message=(
                        f"agent version mismatch: supplied {supplied_version}, "
                        f"stored {stored_version}"
                    ),
                    status_code=409,
                )

        if not _authored_change(existing, updates):
            return self.sanitize_agent_doc(existing)
        try:
            written = await self._agent_repo.update_definition(
                target,
                expected_version=stored_version,
                changes=updates,
                updated_by=me,
            )
        except AgentReferenceMissing as exc:
            raise _reference_missing(exc) from None
        if written is None:
            # A concurrent writer moved the version between the read and this
            # write, so nothing here was written, a name or a category no less
            # than a runtime field.
            raise _version_conflict()
        return self.sanitize_agent_doc(written)

    @staticmethod
    def _check_prepared_capacity_backend(capacity: int, *, sandbox_backend: str) -> None:
        """Refuse prepared capacity where the Agent's backend cannot pool boxes.

        Checked when capacity is set or the Agent moves to another Environment,
        so an Agent is never saved with capacity its backend cannot prepare,
        which would only record ``AGENT_PREWARM_CONFIG_INVALID`` on every try.
        """

        if capacity <= 0:
            return
        from astrabox.seams.sandbox import sandbox_for_name, unsupported_sandbox_feature

        provider = sandbox_for_name(sandbox_backend)
        refusal = unsupported_sandbox_feature(
            provider, feature="prepared capacity", flags=("supports_client_pool",)
        )
        if refusal is None:
            readiness = provider.client_pool_readiness()
            if readiness.available:
                return
            refusal = (
                f"sandbox backend {provider.name!r} cannot run its client pool in "
                f"this deployment: {readiness.detail}"
            )
        raise APIError(
            code="AGENT_PREWARM_UNSUPPORTED",
            message=f"{refusal}; set prepared_capacity to 0",
            status_code=409,
        )

    @staticmethod
    def _check_terminal_panel_backend(
        terminal_panel: bool, *, environment: dict[str, Any]
    ) -> None:
        """Refuse a terminal panel where a conversation's own box cannot keep its shell.

        The terminal of a conversation with its own box is one of the box's
        long-lived processes, which the next command attaches to again; under
        shared tenancy it is an isolated session instead, which the
        Environment's own check already requires. Checked when the panel is
        turned on or the Agent moves to another Environment.
        """

        if not terminal_panel:
            return
        from astrabox.seams.sandbox import (
            SANDBOX_TENANCY_CONVERSATION,
            sandbox_for_name,
            unsupported_sandbox_feature,
        )

        tenancy = str(
            environment.get("sandbox_tenancy") or SANDBOX_TENANCY_CONVERSATION
        ).strip().lower()
        if tenancy != SANDBOX_TENANCY_CONVERSATION:
            return
        refusal = unsupported_sandbox_feature(
            sandbox_for_name(str(environment.get("sandbox_backend") or "")),
            feature="the terminal panel of a conversation with its own box",
            flags=("supports_process_sessions",),
        )
        if refusal is None:
            return
        raise APIError(
            code="INVALID_REQUEST",
            message=f"{refusal}; set terminal_panel to false",
            status_code=400,
        )

    async def _check_prepared_capacity_limit(self, capacity: int, *, agent_id: str) -> None:
        """Refuse a capacity that would lift the deployment's sum over its ceiling."""

        from astrabox.common.utils.settings import load_astrabox_settings

        limit = int(load_astrabox_settings().prepared_capacity_limit)
        if not limit or capacity == 0:
            return
        others = await self._agent_repo.total_prepared_capacity(excluding_agent_id=agent_id)
        if others + capacity > limit:
            raise APIError(
                code="PREPARED_CAPACITY_LIMIT_EXCEEDED",
                message=(
                    f"prepared_capacity {capacity} would raise this deployment's total "
                    f"to {others + capacity}, above ASTRABOX_PREPARED_CAPACITY_LIMIT={limit}; "
                    f"other Agents hold {others}"
                ),
                status_code=409,
            )

    # ── Environment config (carries provider_access; credentials are write-only) ──

    async def list_environment_configs(self, user: UserContext) -> list[dict[str, Any]]:
        # The repository masks every stored credential, so these documents
        # never carry a credential's value to the client.
        return [
            self._environment_with_engine_capabilities(config)
            for config in await self._environment_repo.list_all()
        ]

    async def count_environment_configs(self) -> int:
        return await self._environment_repo.count_all()

    @staticmethod
    def _environment_with_engine_capabilities(
        environment: dict[str, Any],
    ) -> dict[str, Any]:
        """Attach installed-engine facts without exposing engine internals.

        Stored environments survive plugin removal, so the admin catalogue
        must still render them.  ``engine_available=False`` explains why one
        cannot currently be selected; it never silently turns that environment
        into a Claude environment.
        """

        rendered = dict(environment)
        engine_kind = str(rendered.get("engine_kind") or "").strip()
        try:
            capabilities = capabilities_for_engine_kind(engine_kind)
        except EngineKindNotRegistered:
            rendered["engine_available"] = False
            rendered["supported_session_kinds"] = []
            rendered["permission_modes"] = []
            return rendered
        rendered["engine_available"] = True
        rendered["supported_session_kinds"] = sorted(
            capabilities.supported_session_kinds
        )
        # The modes a form may offer for this Environment's program, in the
        # program's own vocabulary; empty means the program has none to choose.
        rendered["permission_modes"] = list(capabilities.permission_modes)
        # What the program lets an Agent or Assistant add to it, so a form
        # renders only the extension fields this Environment's program reads.
        rendered["engine_options_schema"] = [
            dict(field) for field in capabilities.extensions.native_config
        ]
        rendered["extensions"] = extensions_projection(capabilities.extensions)
        return rendered

    async def list_agent_environment_options(self) -> list[dict[str, Any]]:
        """Return the small, secret-free Environment catalog Agent forms need.

        Agent authors need to choose a runnable Environment, but they must not
        receive the administrator's complete Environment documents (network
        policy, provider settings, and write-only credential metadata).  Keep
        this projection deliberately narrow and exclude disabled environments
        whose installed engine does not declare the Agent product.
        """
        configs = await self._environment_repo.list_all()
        options: list[dict[str, Any]] = []
        for config in configs:
            if not isinstance(config, dict) or config.get("enabled") is False:
                continue
            engine_kind = str(config.get("engine_kind") or "").strip()
            if not engine_allowed_for_session_kind(engine_kind, "agent_chat"):
                continue
            name = str(config.get("name") or "").strip()
            if not name:
                continue
            options.append(
                {
                    "name": name,
                    "display_name": str(config.get("display_name") or "").strip() or name,
                    "engine_kind": engine_kind,
                    "enabled": True,
                    "engine_available": True,
                    "supported_session_kinds": ["agent_chat"],
                    # The engine's declared engine_options shape, carried so
                    # the Agent form can render the bag's fields for the
                    # chosen environment. Empty means the engine takes no bag
                    # and the form shows no group.
                    "engine_options_schema": [
                        dict(field)
                        for field in capabilities_for_engine_kind(
                            engine_kind
                        ).extensions.native_config
                    ],
                    # What else the engine accepts, so the form shows only
                    # the extension fields this Environment's engine reads.
                    "extensions": extensions_projection(
                        capabilities_for_engine_kind(engine_kind).extensions
                    ),
                }
            )
        return sorted(options, key=lambda item: (str(item["display_name"]).lower(), item["name"]))

    async def list_agent_environment_models(self, name: str) -> list[str]:
        """List models only for an Environment offered by the Agent form."""
        target = str(name or "").strip()
        environment = await self.get_environment(target)
        if (
            not environment
            or environment.get("enabled") is False
            or not engine_allowed_for_session_kind(
                str(environment.get("engine_kind") or "").strip(),
                "agent_chat",
            )
        ):
            return []
        return await self.list_environment_models(target)

    async def upsert_environment_config(
        self,
        user: UserContext,
        name: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if not name:
            raise APIError(
                code="INVALID_REQUEST",
                message="environment name is required",
                status_code=400,
            )

        # Persist a concrete idle_action before validation because sandbox
        # cleanup reads this field directly from the stored Environment.
        payload = normalize_environment_payload(payload)
        # Schema validation (types / required / enum / no undeclared fields);
        # does not rewrite the payload.
        validate_environment_payload({**payload, "name": name})
        # The request is the whole Environment: a field it leaves out is
        # cleared, and a credential sent back masked keeps the stored one.
        return await self._environment_repo.put(
            name,
            {key: value for key, value in payload.items() if key != "name"},
            updated_by=user.user_id,
        )

    async def delete_environment_config(
        self, user: UserContext, name: str,
    ) -> dict[str, Any]:
        """Remove an unreferenced preset through the administrator HTTP surface.

        The Environment row stays locked from the reference check to the
        delete (``EnvironmentRepository.delete_after_reference_check``), and
        every owner that names an Environment does so through a foreign key, so
        a binding either commits before the check reads or waits for the delete.
        """
        target = str(name or "").strip()
        if not target:
            raise invalid_request("environment name is required")

        async def check_references(connection: Any) -> None:
            agents = await self._agent_repo.list_agents_by_environment(target, conn=connection)
            deleted_agent_ids = [
                str(agent["agent_id"]) for agent in agents if agent.get("state") == "DELETED"
            ]
            sessions = await self._sessions_repo.list_agent_runtime_references(
                deleted_agent_ids, connection=connection,
            )
            agents_with_session_runtime = {
                str(session["agent_id"]) for session in sessions
                if _session_has_runtime_reference(session)
            }
            agents = [
                agent for agent in agents
                if agent.get("state") != "DELETED"
                # A claimed manifest may survive Agent deletion while its
                # Session publishes ownership; pools may still be retiring.
                or agent["holds_compute"]
                or str(agent["agent_id"]) in agents_with_session_runtime
            ]
            # A deleted Assistant holds no workspace (its deletion removes it), so
            # the compute it can still own is in the conversations it left.
            assistants = await self._assistant_repo.list_assistants_by_environment(target)
            deleted_assistant_ids = [
                str(assistant["assistant_id"]) for assistant in assistants if assistant.get("deleted") is True
            ]
            assistant_sessions = await self._sessions_repo.list_assistant_runtime_references(
                deleted_assistant_ids, connection=connection,
            )
            assistants_with_runtime = {
                str(session["assistant_id"]) for session in assistant_sessions
                if _session_has_runtime_reference(session)
            }
            assistants = [
                assistant for assistant in assistants
                if assistant.get("deleted") is not True
                or str(assistant["assistant_id"]) in assistants_with_runtime
            ]
            holders = [
                {
                    "target_type": "agent",
                    "target_id": str(agent["agent_id"]),
                    "target_name": str(
                        (agent.get("display_meta") or {}).get("display_name")
                        or agent.get("name") or agent["agent_id"]
                    ),
                }
                for agent in agents
            ]
            holders.extend(
                {
                    "target_type": "assistant",
                    "target_id": str(assistant["assistant_id"]),
                    "target_name": str(assistant.get("display_name") or assistant["assistant_id"]),
                }
                for assistant in assistants
            )
            from astrabox.persistence.repository.assistant_defaults_repository import (
                AssistantDefaultsRepository,
            )

            holders.extend(
                {
                    "target_type": "assistant_defaults",
                    "target_id": str(layer["scope"]),
                    "target_name": "Assistant defaults",
                }
                for layer in await AssistantDefaultsRepository().list_by_environment(target)
            )
            if holders:
                shown = ", ".join(
                    f"{holder['target_type']} '{holder['target_name']}'" for holder in holders[:5]
                )
                suffix = f", and {len(holders) - 5} more" if len(holders) > 5 else ""
                raise APIError(
                    code="ENVIRONMENT_IN_USE",
                    message=(
                        f"Environment is still used by {shown}{suffix}. "
                        "Move or delete these Agents and Assistants, point the Assistant "
                        "defaults elsewhere, and finish any pending sandbox cleanup, before "
                        "deleting the Environment."
                    ),
                    status_code=409,
                    data={"holders": holders},
                )

        await self._environment_repo.delete_after_reference_check(target, check_references)
        return {"name": target, "deleted": True}

    # ── Normalizers ────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_mcp_servers(raw: Any) -> dict[str, Any] | None:
        """The stored mcp_servers is a name-keyed map of server defs. Accept a
        bare map, or the ``{"mcp_servers": {...}}`` wrapper an assistant override
        may nest them under, and return the flat name→def map."""
        if not isinstance(raw, dict):
            return None
        inner = raw.get("mcp_servers") if isinstance(raw.get("mcp_servers"), dict) else raw
        servers = {
            str(name).strip(): cfg
            for name, cfg in inner.items()
            if str(name).strip()
        }
        return servers or None

    async def _resolve_assigned_mcp_servers(
        self, item: dict[str, Any]
    ) -> list[tuple[str, dict[str, Any]]]:
        """Resolve the Agent's assignments, one labelled map per provider.

        Every catalog is reached through the extension seam, including the one
        AstraBox stores itself, so a deployment's own MCP registry resolves by
        the same path as the bundled ones. Only the providers this Agent is
        actually assigned from are contacted.
        """
        assignments = normalize_assignments(item.get(AGENT_MCP_ASSIGNMENTS_FIELD))
        if not assignments:
            return []
        org_id = str(item.get("org_id") or "").strip() or default_org_id()
        by_provider: dict[str, list[str]] = {}
        for provider_name, item_id in assignments:
            by_provider.setdefault(provider_name, []).append(item_id)

        resolved: list[tuple[str, dict[str, Any]]] = []
        for provider_name, item_ids in by_provider.items():
            try:
                provider = extension_provider_for_name(provider_name)
            except RuntimeError as exc:
                # The Agent names a catalog this deployment does not have.
                # Starting without those servers would run the Agent with fewer
                # tools than it is configured for and no indication why.
                raise APIError(
                    code="AGENT_EXTENSION_PROVIDER_MISSING",
                    message=(
                        f"agent '{item.get('agent_id') or item.get('name')}' is "
                        f"assigned MCP servers from '{provider_name}', which is "
                        "not registered in this deployment"
                    ),
                    status_code=500,
                ) from exc
            servers: dict[str, Any] = {}
            for server in await provider.resolve_mcp_servers(
                org_id=org_id, item_ids=item_ids
            ):
                config: dict[str, Any] = {
                    # Which catalog resolved this entry. The runtime asks that
                    # provider — and only that one — for the credential its
                    # gateway needs, so a deployment running one provider never
                    # has to have another one configured.
                    "provider": provider_name,
                    "type": server.transport,
                    "url": server.url,
                }
                if server.credential_target_url is not None:
                    config["credential_target_url"] = server.credential_target_url
                if server.headers:
                    config["headers"] = dict(server.headers)
                servers[server.name] = config
            if servers:
                resolved.append((f"the '{provider_name}' catalog", servers))
        return resolved

    @staticmethod
    def _connector_servers(record: dict[str, Any]) -> dict[str, Any]:
        """The connectors an Agent or Assistant offers, as platform MCP servers.

        The connector service records each connector's virtual server in the
        hub; the platform's MCP route relays to it as the Session's person.
        """

        return {
            str(connector_id): {"platform_server": CONNECTOR_PLATFORM_SERVER, "hub_server": str(server_id)}
            for connector_id, server_id in (record.get("connector_servers") or {}).items()
        }

    @staticmethod
    def _merge_mcp_servers(
        sources: list[tuple[str, dict[str, Any]]], *, ref: str
    ) -> dict[str, Any]:
        """Fold labelled server maps into one, refusing a name claimed twice.

        A tool call names one server, so two sources offering the same name is
        not a precedence question — it is a configuration the runtime cannot
        carry out. Picking a winner here would hand the Agent a different tool
        than the assignment says, visible only as wrong results inside a
        conversation.
        """
        resolved: dict[str, Any] = {}
        claimed_by: dict[str, str] = {}
        for label, servers in sources:
            for name, config in servers.items():
                if name in resolved:
                    raise APIError(
                        code="AGENT_MCP_NAME_CONFLICT",
                        message=(
                            f"{ref} resolves two MCP servers named '{name}': one "
                            f"from {claimed_by[name]}, one from {label}. Rename or "
                            "unassign one of them."
                        ),
                        status_code=500,
                    )
                resolved[name] = config
                claimed_by[name] = label
        return resolved

    @staticmethod
    def _coerce_positive_int(raw: Any) -> int | None:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    @staticmethod
    def _normalize_sandbox_backend(raw: Any) -> str | None:
        value = str(raw or "").strip().lower()
        return value or None

    @staticmethod
    def _normalize_use_cases(raw: Any) -> list[str]:
        if isinstance(raw, list):
            return [str(s).strip().lower() for s in raw if str(s).strip()]
        if isinstance(raw, str) and raw.strip():
            return [raw.strip().lower()]
        return []

    @staticmethod
    def _normalize_skills(raw: Any) -> list[SkillDescriptor]:
        return normalize_skills(raw)

    @staticmethod
    def _dedupe_strings(raw: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in raw:
            item = str(value or "").strip()
            if not item or item in seen:
                continue
            seen.add(item)
            result.append(item)
        return result

    @staticmethod
    def _normalize_credential_vault_ids(raw: Any) -> list[str]:
        if not isinstance(raw, list):
            return []
        result: list[str] = []
        seen: set[str] = set()
        for value in raw:
            vault_id = str(value or "").strip()
            if not vault_id or vault_id in seen:
                continue
            seen.add(vault_id)
            result.append(vault_id)
        return result

    @staticmethod
    def _normalize_default_repo(raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        url = str(raw.get("url") or "").strip()
        if not url:
            return None
        protocol = str(raw.get("protocol") or "ssh").strip().lower()
        secret_name = str(raw.get("deploy_key_secret_name") or "").strip()
        normalized: dict[str, Any] = {
            "url": url,
            "protocol": protocol,
            "deploy_key_secret_name": secret_name,
        }
        branch = str(raw.get("branch") or "").strip()
        if branch:
            normalized["branch"] = branch
        depth = raw.get("depth")
        if isinstance(depth, int) and depth > 0:
            normalized["depth"] = depth
        return normalized
