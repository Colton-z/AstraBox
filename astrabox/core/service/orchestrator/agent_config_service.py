from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, is_dataclass
from typing import Any

from astrabox.persistence.repository import (
    AgentRepository,
    EnvironmentRepository,
    SessionRepository,
)
from astrabox.persistence.repository.assistant_catalog_repository import (
    AssistantCatalogRepository,
)
from astrabox.persistence.repository.assistant_workspace_repository import AssistantWorkspaceRepository
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.time_utils import utcnow_iso
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
    normalize_visibility,
    sanitize_access_control_payload,
)
from astrabox.core.service.orchestrator.schema_validation import (
    invalid_request,
    validate_declared_config_bag,
)
from astrabox.core.service.orchestrator.agent_schema import (
    AGENT_PRIVATE_STORED_FIELDS,
    agents_prewarm_by_default,
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
from astrabox.core.service.orchestrator.mcp_assignments import (
    AGENT_MCP_ASSIGNMENTS_FIELD,
    normalize_assignments,
)
from astrabox.core.service.orchestrator.runtime.plugin_repos import normalize_plugin_repos
from astrabox.core.service.orchestrator.sandbox_names import read_undestroyed
from astrabox.seams.extensions import extension_provider_for_name

# The masked stand-in for a set ``provider_access.api_key`` in client-facing
# environment reads — the plaintext model key never leaves the server. On write,
# this exact value means "unchanged, keep the stored key" (a real key is never
# this sentinel). Resolution uses the raw doc (``get_environment``), never this.
ENV_API_KEY_SENTINEL = "••••••••"

# Runtime-affecting Agent fields whose change bumps ``agent.version``.
# Cosmetic, access, and lifecycle fields (display_meta, created_by, state, and
# timestamps) are excluded because they do not change a prepared runtime.
_VERSIONED_HARNESS_FIELDS = (
    "model",
    "system",
    "skills",
    "mcp_servers",
    "mcp_assignments",
    "extension_catalog",
    "plugin_repos",
    "default_repo",
    "engine_options",
    "environment_name",
    "exposure_mode",
    "idle_hibernate_seconds",
    "terminal_panel",
    "diff_panel",
    "prewarm_enabled",
)

def _session_has_runtime_reference(session: dict[str, Any]) -> bool:
    return bool(
        str(session.get("sandbox_id") or "").strip()
        or read_undestroyed(session)
        or session.get("state") == "CREATING"
        # Failed or superseded startup may retain scope without a main pointer.
        or session.get("startup_allocation")
        or session.get("_retained_startup_allocations")
    )


def _authored_change(existing: dict[str, Any], updates: dict[str, Any]) -> bool:
    """Whether a write changes any of the authored fields it carries.

    ``updated_at`` and ``updated_by`` record who changed a definition and
    when. Saving a form unchanged, or the runtime writing its own state to the
    same row, changes no definition and must not move them.
    """

    return any(existing.get(key) != value for key, value in updates.items())


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
        assistant_workspace_repo: AssistantWorkspaceRepository | None = None,
        spawn_background_task: Callable[..., Any] | None = None,
    ) -> None:
        self._agent_repo = agent_repo
        self._environment_repo = environment_repo
        self._sessions_repo = sessions_repo or SessionRepository()
        self._assistant_workspace_repo = assistant_workspace_repo or AssistantWorkspaceRepository()
        # The runtime-view resolver reads the Assistant catalog directly. An
        # Assistant view combines its Environment with its own overrides; using
        # the repository here avoids a service-construction cycle.
        self._assistant_repo = assistant_repo or AssistantCatalogRepository()
        # A bound method of the still-constructing AgentPlatformService facade
        # (same inversion-of-control wiring as DeploymentService's
        # stream_message_events_ds/spawn_background_task pair).
        self._spawn_background_task = spawn_background_task

    # ── Resolution: agent/assistant doc → runtime AgentView ────────────────

    async def list_agents(
        self,
        *,
        use_case: str | None = None,
        viewer_user_id: str | None = None,
        viewer_roles: Sequence[str] | None = None,
    ) -> list[AgentView]:
        """List enabled Agents as runtime-resolved views.

        When ``viewer_user_id`` is provided, only agents that user may *see/use*
        are returned (per-agent visibility: public / private / allowlist). When
        it is ``None`` the caller is an internal/already-authorized path (e.g.
        runtime startup loading an existing Session's Agent) and no
        visibility filter is applied.
        """
        rows = await self._agent_repo.list_all_agents()
        # Environment presets are the single source of runtime config; cache each
        # env once per call so N agents don't trigger N identical reads.
        env_cache: dict[str, dict[str, Any]] = {}
        views: list[AgentView] = []
        for item in rows:
            if str(item.get("enabled")) == "False" or item.get("enabled") is False:
                continue
            if viewer_user_id is not None and not can_view_agent(
                item, viewer_user_id, viewer_roles or ()
            ):
                continue
            use_cases = self._normalize_use_cases(item.get("use_cases"))
            if use_case and use_cases and use_case not in use_cases:
                continue
            views.append(await self._build_agent_view(item, env_cache))
        return views

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
        return await self.resolve_agent_harness(
            agent_id, require_enabled_environment=require_enabled_environment
        )

    async def _resolve_assistant_harness(
        self, assistant_id: str, *, require_enabled_environment: bool = True
    ) -> AgentView | None:
        """Build an Assistant Session's runtime view from its Environment and overrides."""
        assistant = await self._assistant_repo.get_assistant(assistant_id)
        if assistant is None:
            return None
        env_name = str(assistant.get("environment_name") or "").strip()
        env = await self._require_environment(
            env_name, ref=f"assistant '{assistant_id}'", require_enabled=require_enabled_environment
        )
        model_override = assistant.get("model_config_override") or {}
        model = str(model_override.get("model_name") or "").strip() or None
        mcp_servers = self._normalize_mcp_servers(assistant.get("mcp_config_override"))
        refuse_reserved_mcp_fields(mcp_servers, owner=f"assistant '{assistant_id}'")
        view = AgentView(
            agent_id=None,
            name=str(assistant.get("display_name") or assistant_id),
            model=model,
            model_config=self._synthesize_model_config(model, env),
            system=str(assistant.get("system") or "").strip() or None,
            mcp_servers=mcp_servers,
            skills=self._normalize_skills(assistant.get("skill_manifest_override")),
            plugin_repos=normalize_plugin_repos(assistant.get("plugin_repos_override")),
            credential_vault_ids=self._normalize_credential_vault_ids(
                assistant.get("credential_vault_ids")
            ),
            environment_name=env_name,
            assistant_revision=self._assistant_revision(assistant, env),
        )
        self._overlay_environment_runtime(view, env)
        return view

    @staticmethod
    def _assistant_revision(assistant: dict[str, Any], env: dict[str, Any]) -> str:
        """The identity of what an Assistant's runtime is prepared from.

        Every stored field of the Assistant except its bookkeeping, plus the
        Environment's revision. It says only that the definition moved; which
        change matters to the Agent program's profile is the adapter's call
        when it prepares that profile again.
        """

        definition = {
            key: value
            for key, value in assistant.items()
            if key not in {"_id", "created_at", "updated_at", "deleted"}
        }
        payload = json.dumps(
            {
                "assistant": definition,
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
        """Merge an agent doc with its named environment into an AgentView."""
        env_name = str(item.get("environment_name") or "").strip()
        if env_name not in env_cache:
            env_cache[env_name] = await self._require_environment(
                env_name, ref=f"agent '{item.get('agent_id') or item.get('name')}'",
                require_enabled=require_enabled_environment,
            )
        env = env_cache[env_name]
        display_meta = item.get("display_meta") or {}
        model = str(item.get("model") or "").strip() or None
        extension_catalog = (
            item.get("extension_catalog")
            if isinstance(item.get("extension_catalog"), dict)
            else {}
        )
        agent_ref = f"agent '{item.get('agent_id') or item.get('name')}'"
        own_mcp_servers = self._normalize_mcp_servers(item.get("mcp_servers")) or {}
        # Only a catalog assignment below may carry `provider` or
        # `credential_target_url`. Saving refuses them on the Agent's own
        # servers; this refuses a definition saved before that check.
        refuse_reserved_mcp_fields(own_mcp_servers, owner=agent_ref)
        resolved_mcp_servers = self._merge_mcp_servers(
            [
                ("the Agent's own mcp_servers", own_mcp_servers),
                *await self._resolve_assigned_mcp_servers(item),
            ],
            ref=agent_ref,
        )
        catalog_skills = self._normalize_skills(extension_catalog.get("skills"))
        resolved_skills = self._dedupe_strings(
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
            skills=resolved_skills,
            engine_options=item.get("engine_options"),
            default_repo=self._normalize_default_repo(item.get("default_repo")),
            plugin_repos=normalize_plugin_repos(item.get("plugin_repos")),
            catalog_skills=tuple(catalog_skills),
            credential_vault_ids=self._normalize_credential_vault_ids(
                item.get("credential_vault_ids")
            ),
            environment_name=env_name,
            exposure_mode=str(item.get("exposure_mode") or "").strip() or None,
            idle_hibernate_seconds=self._coerce_positive_int(item.get("idle_hibernate_seconds")),
            terminal_panel=bool(item.get("terminal_panel", False)),
            diff_panel=bool(item.get("diff_panel", False)),
            prewarm_enabled=bool(item.get("prewarm_enabled", False)),
            version=self._coerce_positive_int(item.get("version")),
            status=item.get("status"),
            scope=item.get("scope"),
            display_name=display_meta.get("display_name") or str(item.get("name") or ""),
            icon=display_meta.get("icon"),
            tags=self._normalize_tags(display_meta.get("tags") or item.get("tags")),
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
        env = await self._environment_repo.get_any_by_name(env_name)
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
        """Write named access fields through the dedicated policy operation."""

        agent = await self._must_manage_agent_access(user, agent_id)
        updates = sanitize_access_control_payload(payload)
        if _authored_change(agent, updates):
            updates.update(updated_at=utcnow_iso(), updated_by=user.user_id)
        applied = await self._agent_repo.compare_and_update_agent(
            str(agent["agent_id"]),
            expected={},
            updates=updates,
        )
        if not applied:
            raise APIError(
                code="AGENT_NOT_FOUND",
                message="agent not found",
                status_code=404,
            )
        return self._agent_access_view({**agent, **updates})

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

    async def list_agent_access_docs(self) -> list[dict[str, Any]]:
        """Return only the stored fields needed for view/manage-scope checks."""
        return await self._agent_repo.list_agent_access_docs()

    async def get_environment(self, name: str) -> dict[str, Any] | None:
        """Return the raw environment doc by name (None if missing)."""
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

        env = await self.get_environment(name)
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
        configs = await self._agent_repo.list_all_agents()
        me = user.user_id
        roles = user.roles
        return [
            self.sanitize_agent_doc(c)
            for c in configs
            if can_manage_agent(c, me, roles)
        ]

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
        declares what it accepts (``EngineRuntimeCapabilities.engine_options_schema``)
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
                capabilities.engine_options_schema,
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
        editable = validate_agent_payload({**payload, "name": name})
        if editable.get("prewarm_enabled") is None:
            # Written, not assumed on read: the capacity sweep selects Agents
            # whose stored value is true, so an absent key would stay cold.
            editable["prewarm_enabled"] = agents_prewarm_by_default(
                load_astrabox_settings()
            )
        plugin_repos = editable.get("plugin_repos")
        if plugin_repos is not None:
            editable["plugin_repos"] = normalize_plugin_repos(plugin_repos)
        env = await self._validate_engine_configuration(
            environment_name=str(editable.get("environment_name") or ""),
            engine_options=editable.get("engine_options"),
            configuration_inputs={
                "mcp_servers": editable.get("mcp_servers"),
                "skills": editable.get("skills"),
                "plugin_repos": editable.get("plugin_repos"),
            },
        )
        await validate_author_declarations(
            owner=f"Agent {name!r}",
            networking=env.get("networking"),
            skills=editable.get("skills"),
            plugin_repos=editable.get("plugin_repos"),
            mcp_servers=editable.get("mcp_servers"),
            default_repo=editable.get("default_repo"),
        )

        agent_id = str(uuid.uuid4())
        doc = {
            "agent_id": agent_id,
            "enabled": True,
            "version": 1,
            "state": "ACTIVE",
            "user_id": user.user_id,
            "created_by": user.user_id,
            "org_id": str(getattr(user, "org_id", None) or "").strip() or default_org_id(),
            "updated_by": user.user_id,
            "visibility": VISIBILITY_PRIVATE,
            **editable,
        }
        stored = await self._agent_repo.create_agent(doc)
        return self.sanitize_agent_doc(stored)

    async def upsert_agent_config(
        self,
        user: UserContext,
        agent_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Update one Agent by id with optimistic concurrency.

        Only the creator or an admin may edit. ``version`` auto-increments when a
        runtime-affecting Agent field changes (a cosmetic or no-op update does
        not bump it). If
        the payload supplies a ``version``, it must match the stored version
        (409 on mismatch); if it omits one, the update applies without the fence.
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
        updates = validate_agent_payload(
            {**payload, "name": name},
            allow_version=True,
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
        if "plugin_repos" in updates:
            updates["plugin_repos"] = normalize_plugin_repos(
                updates["plugin_repos"]
            )
        effective = {
            field: updates[field] if field in updates else existing.get(field)
            for field in ("mcp_servers", "skills", "plugin_repos", "default_repo")
        }
        env = await self._validate_engine_configuration(
            environment_name=effective_environment,
            engine_options=effective_bag,
            configuration_inputs={
                field: effective[field]
                for field in ("mcp_servers", "skills", "plugin_repos")
            },
        )
        # The whole effective definition, not only the changed fields: moving
        # an Agent to a stricter Environment must meet that Environment too.
        await validate_author_declarations(
            owner=f"Agent {name!r}",
            networking=env.get("networking"),
            skills=effective["skills"],
            plugin_repos=effective["plugin_repos"],
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

        changed = self._harness_changed(existing, updates)
        if changed:
            updates["version"] = stored_version + 1
        if _authored_change(existing, updates):
            updates.update(updated_at=utcnow_iso(), updated_by=me)

        applied = await self._agent_repo.compare_and_update_agent(
            target,
            expected={"version": stored_version},
            updates=updates,
        )
        if not applied and changed:
            # A concurrent writer bumped the version between the read and the CAS.
            raise APIError(
                code="AGENT_VERSION_CONFLICT",
                message="agent was modified concurrently; retry with the latest version",
                status_code=409,
            )
        refreshed = await self._agent_repo.get_agent(target)
        return self.sanitize_agent_doc(refreshed or {**existing, **updates})

    @staticmethod
    def _harness_changed(existing: dict[str, Any], updates: dict[str, Any]) -> bool:
        """Return whether a runtime-affecting Agent field differs."""
        for key in _VERSIONED_HARNESS_FIELDS:
            if key in updates and updates[key] != existing.get(key):
                return True
        return False

    # ── Environment config (carries provider_access; api_key is write-only) ──

    async def list_environment_configs(self, user: UserContext) -> list[dict[str, Any]]:
        configs = await self._environment_repo.list_all()
        # NEVER ship the plaintext model api_key to the client — a masked
        # placeholder only (a UI mask over a leaked key protects nothing).
        return [
            self._environment_with_engine_capabilities(
                self._redact_env_secret(self.sanitize_agent_doc(c))
            )
            for c in configs
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
                        ).engine_options_schema
                    ],
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

    #: Environment secrets that never leave the server in a client-facing read,
    #: as (containing object, field) pairs. Both are write-only in the same way,
    #: so they are masked and restored by one rule rather than two that can
    #: disagree about which fields are secret.
    _ENV_SECRET_FIELDS: tuple[tuple[str, str], ...] = (
        ("provider_access", "api_key"),
        ("tracing", "auth_token"),
    )

    @classmethod
    def _redact_env_secret(cls, doc: dict[str, Any]) -> dict[str, Any]:
        """A copy of an environment doc with every stored secret masked.

        A set value becomes :data:`ENV_API_KEY_SENTINEL`; an unset one is left
        empty/absent. The real values stay server-side (``get_environment`` /
        resolution keep the raw doc). The tracing credential is here for the same
        reason the model key is: an admin read is still a read, and a collector
        token is as reusable as a gateway key."""
        if not isinstance(doc, dict):
            return doc
        out = doc
        for holder, field_name in cls._ENV_SECRET_FIELDS:
            block = out.get(holder)
            if not isinstance(block, dict):
                continue
            if not str(block.get(field_name) or "").strip():
                continue
            out = {**out, holder: {**block, field_name: ENV_API_KEY_SENTINEL}}
        return out

    async def _preserve_env_secret_on_unchanged(
        self, name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Write-only secrets: if the client sent a masked sentinel back (loaded
        redacted, left untouched), restore the stored plaintext rather than
        overwriting it with the mask. A real new value passes through; empty
        clears. Masking a field without this half is worse than not masking it:
        one form round-trip would save the mask over the real credential."""
        pending = [
            (holder, field_name)
            for holder, field_name in self._ENV_SECRET_FIELDS
            if isinstance(payload.get(holder), dict)
            and str(payload[holder].get(field_name) or "") == ENV_API_KEY_SENTINEL
        ]
        if not pending:
            return payload
        existing = await self._environment_repo.get_any_by_name(name)
        out = payload
        for holder, field_name in pending:
            block = {**out[holder]}
            stored_block = (existing or {}).get(holder) if isinstance(existing, dict) else None
            stored_value = (
                str((stored_block or {}).get(field_name) or "")
                if isinstance(stored_block, dict)
                else ""
            )
            if stored_value:
                block[field_name] = stored_value
            else:
                block.pop(field_name, None)
            out = {**out, holder: block}
        return out

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
        # Schema validation (types / required / enum); does not rewrite the payload.
        validate_environment_payload({"name": name, **payload})
        # Keep the stored api_key when the client left the mask untouched.
        payload = await self._preserve_env_secret_on_unchanged(name, payload)

        doc = {
            "name": name,
            "updated_at": utcnow_iso(),
            "updated_by": user.user_id,
            **{k: v for k, v in payload.items() if k not in ("name",)},
        }
        result = await self._environment_repo.upsert(name, doc)
        return self._redact_env_secret(self.sanitize_agent_doc(result))

    async def delete_environment_config(
        self, user: UserContext, name: str,
    ) -> dict[str, Any]:
        """Remove an unreferenced preset through the administrator HTTP surface."""
        target = str(name or "").strip()
        if not target:
            raise invalid_request("environment name is required")

        async def check_references(transaction: Any) -> None:
            agents = await self._agent_repo.list_agents_by_environment(target, transaction=transaction)
            deleted_agent_ids = [
                str(agent["agent_id"]) for agent in agents if agent.get("deleted") is True
            ]
            sessions = await self._sessions_repo.list_agent_runtime_references(
                deleted_agent_ids, transaction=transaction,
            )
            agents_with_session_runtime = {
                str(session["agent_id"]) for session in sessions
                if _session_has_runtime_reference(session)
            }
            agents = [
                agent for agent in agents
                if agent.get("deleted") is not True
                or str(agent.get("sandbox_id") or "").strip()
                or read_undestroyed(agent)
                # A claimed manifest may survive Agent deletion while its
                # Session publishes ownership; pools may still be retiring.
                or agent.get("_prepared_slot")
                or str(agent.get("_client_pool_name") or "").strip()
                or agent.get("_retiring_client_pools")
                or str(agent["agent_id"]) in agents_with_session_runtime
            ]
            assistants = await self._assistant_repo.list_assistants_by_environment(target, transaction=transaction)
            deleted_assistant_ids = [
                str(assistant["assistant_id"]) for assistant in assistants if assistant.get("deleted") is True
            ]
            assistant_sessions = await self._sessions_repo.list_assistant_runtime_references(
                deleted_assistant_ids, transaction=transaction,
            )
            workspaces = await self._assistant_workspace_repo.list_runtime_references(
                deleted_assistant_ids, transaction=transaction,
            )
            assistants_with_runtime = {
                str(session["assistant_id"]) for session in assistant_sessions
                if _session_has_runtime_reference(session)
            }
            assistants_with_runtime.update(
                str(workspace["assistant_id"]) for workspace in workspaces
                if str(workspace.get("current_sandbox_id") or "").strip()
                or (workspace.get("state") == "MATERIALIZING" and workspace.get("provisioning_session_id"))
            )
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
            if holders:
                shown = ", ".join(
                    f"{holder['target_type']} '{holder['target_name']}'" for holder in holders[:5]
                )
                suffix = f", and {len(holders) - 5} more" if len(holders) > 5 else ""
                raise APIError(
                    code="ENVIRONMENT_IN_USE",
                    message=(
                        f"Environment is still used by {shown}{suffix}. "
                        "Move or delete these Agents and Assistants, and finish any pending "
                        "sandbox cleanup, before deleting the Environment."
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
    def _normalize_skills(raw: Any) -> list[str]:
        if isinstance(raw, list):
            return [str(s).strip() for s in raw if str(s).strip()]
        if isinstance(raw, str) and raw.strip():
            return [raw.strip()]
        return []

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

    @staticmethod
    def _normalize_tags(raw: Any) -> list[str]:
        if isinstance(raw, list):
            return [str(tag) for tag in raw if str(tag).strip()]
        if isinstance(raw, str) and raw.strip():
            return [raw.strip()]
        return []
