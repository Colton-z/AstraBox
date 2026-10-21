"""AstraBox FastAPI application factory.

:func:`create_app` builds the app, mounts the HTTP turn surface, and wires
startup/shutdown:

* a ``FastAPI`` instance built by :func:`create_app`;
* a ``/healthz`` liveness route (used by the server image HEALTHCHECK);
* the user, session, turn, and admin resource routers under
  :mod:`astrabox.api.routes`; the turn routes carry the AI-SDK
  **data-stream-protocol over SSE** frames the frontend consumes;
* supporting ``register_*_routes(app)`` route modules for administration,
  sandbox callbacks and inventory, transcripts, credentials, deployments,
  sharing, extensions, Agents, and Assistants;
* a ``lifespan`` that on **startup** registers the built-in providers, brings up
  the persistence schema, and seeds the default RuntimeProfile and Agent, and on
  **shutdown** awaits ``run_lifecycle_shutdown()``.

``create_app()`` is import-time constructible; the persistence backend is resolved
lazily by the DAL ingress, so route activation is never gated on a Mongo URI.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response

from astrabox import __version__
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.time_utils import utcnow_iso

logger = get_logger(__name__)

#: The seeded default environment name.
_DEFAULT_RUNTIME_PROFILE_NAME = "claude-code"


def _seed_default_model() -> str:
    """Return the deployment's default model, which the built-in Agents are seeded with.

    :meth:`RuntimeConfigResolver.default_model_name` defines it, and schema
    migration 6 gives Agents stored without a model the same value.
    ``astrabox/config/app.yml`` supplies the bundled model unless deployment
    configuration replaces it. Empty when the deployment configures none.
    """

    from astrabox.common.utils.settings import load_astrabox_settings
    from astrabox.core.service.orchestrator.runtime.config_resolver import (
        RuntimeConfigResolver,
    )

    return RuntimeConfigResolver(load_astrabox_settings()).default_model_name() or ""

_FINANCIAL_SERVICES_REPOSITORY = "https://github.com/anthropics/financial-services.git"
_FINANCIAL_SERVICES_REVISION = "286f9068951335bcabd99525d5197752866ee05f"
_FINANCIAL_ANALYSIS_PLUGIN_PATH = "plugins/vertical-plugins/financial-analysis"
_EQUITY_RESEARCH_PLUGIN_PATH = "plugins/vertical-plugins/equity-research"
_KEYVEX_MCP_URL = "https://mcp.keyvex.com"

#: System prompt for the seeded ``Investment Research`` example Agent.
_INVESTMENT_RESEARCH_SYSTEM_PROMPT = (
    "You are an investment research analyst working in a sandboxed Linux workspace.\n\n"
    "Use the installed financial-analysis and equity-research Plugins for their modeling and "
    "research workflows, Skills, slash commands, and data connectors. Use the KeyVex MCP tools "
    "as the public-data source when a subscribed connector is not authorized; never guess a "
    "current filing or disclosure from model memory. State the source and reporting date for "
    "every material fact.\n\n"
    "Separate reported facts, your calculations, and your interpretation. Save useful tables, "
    "scripts, and reports in the workspace so the user can inspect and download them. Explain "
    "data gaps and never present the output as personalized investment advice."
)


async def _warn_about_runtime_profiles_on_disabled_backends() -> None:
    """Name each enabled RuntimeProfile whose sandbox backend this deployment does not enable.

    Startup proceeds: an operator may stop running a backend while keeping the
    records that name it. A Session that resolves such a RuntimeProfile fails
    with ``SANDBOX_BACKEND_DISABLED`` when it needs a sandbox; this names the
    RuntimeProfiles before anyone meets that.
    """
    from astrabox.persistence.repository.runtime_profile_repository import RuntimeProfileRepository
    from astrabox.seams.sandbox import enabled_sandbox_names

    enabled = enabled_sandbox_names()
    for environment in await RuntimeProfileRepository().list_all():
        if environment.get("enabled") is False:
            continue
        backend = str(environment.get("sandbox_backend") or "").strip().lower()
        if backend and backend not in enabled:
            logger.warning(
                "RuntimeProfile %r selects sandbox backend %r, which this deployment "
                "does not enable (enabled: %s); its Sessions fail with "
                "SANDBOX_BACKEND_DISABLED until the backend is added to "
                "ASTRABOX_SANDBOX_BACKENDS or the RuntimeProfile selects another",
                environment.get("name"),
                backend,
                enabled,
            )


async def _seed_default_agent_if_empty() -> None:
    """Seed the built-in ``claude-code`` environment + a usable default Agent when
    the ``agents`` store is empty.

    A fresh store has no agents, so the console would open on an empty list with
    nothing to converse with. This seeds one Agent plus the ``claude-code``
    RuntimeProfile it selects, so the first-run path works without separate setup.
    Runs only when
    the agents collection is empty, so an operator's later edits are never
    overwritten.

    Credentials are not seeded: the environment carries no ``provider_access`` and
    no ``endpoint_provider`` here, so model connectivity falls back to the
    deployment's selected model endpoint provider. Organization, backend, and
    image selectors are also absent, so their resolution boundaries use the
    current request or deployment defaults until an operator writes a pin.

    Each Agent stores the deployment's default model (:func:`_seed_default_model`),
    because every Agent names its own; a later change to the deployment's model
    does not change these Agents. Raises before writing anything when the
    deployment configures no default model.
    """
    import uuid

    from astrabox.core.service.orchestrator.agent_schema import (
        default_prepared_capacity,
    )
    from astrabox.core.service.orchestrator.runtime_profile_schema import (
        normalize_runtime_profile_payload,
    )
    from astrabox.persistence.repository.agent_repository import AgentRepository
    from astrabox.persistence.repository.runtime_profile_repository import RuntimeProfileRepository
    from astrabox.seams.sandbox import default_sandbox_backend, sandbox_for_name

    agents_repo = AgentRepository()
    if await agents_repo.has_agents():
        logger.debug("agent seed skipped: agents already present")
        return

    # The default backend provider is the single source of truth for its
    # deploy-time defaults (seed image).
    backend_name = default_sandbox_backend()
    runtime_defaults = sandbox_for_name(backend_name).runtime_defaults()
    default_runtime_image = str(runtime_defaults.runtime_image or "").strip()
    if not default_runtime_image:
        # Seeding is a bring-up convenience; a backend without runtime defaults
        # simply means the operator authors the environment + agent explicitly.
        logger.info(
            "agent seed skipped: backend %r declares no runtime defaults", backend_name
        )
        return
    deployment_model = _seed_default_model()
    if not deployment_model:
        raise RuntimeError(
            "cannot seed the built-in Agents: every Agent names a model, and this "
            "deployment configures no default model to give them; set "
            "ANTHROPIC_MODEL (or ASTRABOX_MODEL_NAME)"
        )

    runtime_profiles_repo = RuntimeProfileRepository()
    # The seed writes repositories directly instead of using
    # AgentConfigService.upsert_runtime_profile_config, so it supplies the Agent
    # records' timestamps explicitly.
    seeded_at = utcnow_iso()
    # The environment is the single source of runtime config (image, backend,
    # install policy) + model connectivity. Seed it first so the agent resolves
    # cleanly. No endpoint_provider / provider_access — deployment defaults apply.
    # Normalized like an administrator's write, which settles its idle action.
    await runtime_profiles_repo.put(
        _DEFAULT_RUNTIME_PROFILE_NAME,
        normalize_runtime_profile_payload({
            "enabled": True,
            "display_name": "Claude Code",
            "description": "Default environment: the built-in sandbox backend.",
            "engine_kind": "claude_code",
            # No backend selector and no image. An environment's image field is
            # a pin: a value there wins over the deployment's configured image
            # for every session this environment serves, permanently and
            # silently. Seeding it with the deployment default would pin these
            # sessions to whatever image was current the day the store was
            # created, so upgrading the deployment would stop changing them with
            # nothing to say so. Left empty, each create resolves the image at
            # create time and the deployment's configuration stays the live
            # answer; an operator who types an image gets a pin.
            # Secure default: no generic Internet access. Each Agent's explicit
            # remote MCP endpoints are admitted, while Plugin-internal endpoints
            # remain administrator-owned additions because the platform does not
            # parse or rewrite engine-native Plugin configuration.
            "networking": {
                "type": "limited",
                "allowed_hosts": [],
                "allow_mcp_servers": True,
            },
        }),
        updated_by="system",
    )

    # The seed writes the repository directly, so it applies the same
    # create-time default as AgentConfigService.create_agent_config.
    seeded_capacity = default_prepared_capacity(backend_name)
    investment_engine_options = {"sdk_options": {"strict_mcp_config": True}}
    from astrabox.core.service.orchestrator.engine.claude_code_options import (
        CLAUDE_ENGINE_OPTIONS_SCHEMA,
    )
    from astrabox.core.service.orchestrator.schema_validation import (
        validate_declared_config_bag,
    )

    validate_declared_config_bag(
        investment_engine_options,
        CLAUDE_ENGINE_OPTIONS_SCHEMA,
        bag_label="engine_options",
        owner_label="engine 'claude_code'",
    )

    def _agent_doc(
        name: str,
        description: str,
        system: str | None,
        *,
        capabilities: dict[str, object] | None = None,
    ) -> dict[str, object]:
        # visibility public so any user of a fresh instance can see/use it; the
        # system principal is the creator (seed-time, no request user).
        # Organization stays absent, so catalog resolution uses the request's
        # current organization; an explicitly authored one still wins. The
        # model is written like any Agent's: the write path requires one and
        # the runtime refuses an Agent without one.
        return {
            "agent_id": str(uuid.uuid4()),
            "enabled": True,
            "created_by": "system",
            "visibility": "public",
            "name": name,
            "created_at": seeded_at,
            "updated_at": seeded_at,
            "description": description,
            "model": deployment_model,
            "system": system,
            "runtime_profile_name": _DEFAULT_RUNTIME_PROFILE_NAME,
            "display_meta": {"display_name": name},
            "prepared_capacity": seeded_capacity,
            **(capabilities or {}),
        }

    await agents_repo.create_agent(
        _agent_doc(
            "Claude Code",
            "Default agent: Claude Code on the built-in sandbox backend.",
            None,
        )
    )
    # A complete example Agent: one reviewed upstream Plugin provides Skills and
    # slash commands, while a public MCP endpoint makes a real tool call possible
    # without asking a new user for a commercial data subscription first.
    await agents_repo.create_agent(
        _agent_doc(
            "Investment Research",
            "Research public companies and disclosures with an official Anthropic "
            "Plugin and a live public-data MCP server.",
            _INVESTMENT_RESEARCH_SYSTEM_PROMPT,
            capabilities={
                # The Plugin stays engine-native, including any MCP definitions
                # it contains. The explicit KeyVex server is an additional Agent
                # capability, not a replacement or a restriction on the Plugin.
                "engine_options": investment_engine_options,
                "packages": [
                    {
                        "kind": "claude-plugin",
                        "url": _FINANCIAL_SERVICES_REPOSITORY,
                        "protocol": "https",
                        "sha": _FINANCIAL_SERVICES_REVISION,
                        "paths": [
                            _FINANCIAL_ANALYSIS_PLUGIN_PATH,
                            _EQUITY_RESEARCH_PLUGIN_PATH,
                        ],
                    }
                ],
                # A vendor MCP entry, and only that: a transport and an
                # endpoint. What decides where a call is answered is
                # ``platform_server`` -- naming an AstraBox capability -- so an
                # entry that names a URL is dialled by the sandbox because it
                # named no capability, not because a field says so. A `backend`
                # key here was read by nothing; it survived as a word that looked
                # like the decision it was not.
                "mcp_servers": {
                    "keyvex": {
                        "type": "http",
                        "url": _KEYVEX_MCP_URL,
                    }
                },
            },
        )
    )
    logger.info(
        "seeded default environment %r (deployment backend=%s, image=%s) "
        "+ 2 agents (model=%r)",
        _DEFAULT_RUNTIME_PROFILE_NAME,
        backend_name,
        default_runtime_image,
        deployment_model,
    )


def _e2e_faults_armed() -> bool:
    """Whether the deployment explicitly enabled test-only fault support."""
    import os

    return str(os.getenv("ASTRABOX_E2E_FAULTS") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _install_e2e_fault_hooks_if_armed() -> None:
    """Arm the E2E fault-injection hooks — ONLY when ``ASTRABOX_E2E_FAULTS`` is set.

    This is the single place :mod:`astrabox.testing` enters a running app: with
    the flag unset (every production boot) the package is never imported and the
    fault seam (:mod:`astrabox.common.fault_injection`) has no hooks installed,
    so the hot-path probes are structurally inert.
    """
    if not _e2e_faults_armed():
        return
    from astrabox.testing.e2e_faults import install_e2e_fault_hooks

    install_e2e_fault_hooks()


def _register_e2e_fault_routes_if_armed(app: FastAPI) -> None:
    """Mount test-only mutation routes only on explicitly armed deployments."""
    if not _e2e_faults_armed():
        return
    from astrabox.testing.e2e_faults import register_e2e_fault_routes

    register_e2e_fault_routes(app)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan (startup → yield → shutdown).

    **Startup**:

    1. ``session_signing_secret()`` — materialize the deployment-local key used
       by browser sessions and the sandbox's scoped inference credential.
    2. ``register_builtin_providers()`` — import the built-in provider modules
       for their register_* side effects. Also triggered at import of
       ``runtime_manager``, but this call is the guaranteed bring-up site
       regardless of import order.
    3. ``load_entry_point_providers()`` — enumerate the ``astrabox.providers.*``
       entry-point groups so installed third-party provider distributions
       activate without any in-tree import naming them.
    4. ``set_default_sandbox_backend(...)`` and ``set_enabled_sandbox_backends(...)``
       — publish the deployment-configured default backend and enabled backend
       set (settings) to the seam; the only place backend names enter the
       dispatch path. The scheduler provider is selected and checked against
       the database in the same composition step.
    5. ``get_persistence().initialize_schema()`` — initializes the selected
       provider's schema. The built-in PostgreSQL provider upgrades to this
       build's Alembic head and serializes replicas with an advisory lock.
       ``sandbox_installation_id()`` then reads the installation id this
       database holds (creating it on a new database), which every sandbox
       create stamps and the ownerless reap matches, so a database that cannot
       answer fails startup instead of a first create.
       ``warn_about_catalog_selections_without_a_catalog()`` then names each
       record that still holds an extension-catalog selection while the
       deployment runs no catalog.
       ``_warn_about_runtime_profiles_on_disabled_backends()`` then names each
       enabled RuntimeProfile that selects a sandbox backend outside the enabled
       set.
       ``configure_sandbox_quotas()`` then resolves and logs each enabled
       backend's sandbox quota, refusing one on a backend that cannot list
       its sandboxes, and ``reconcile_at_startup()`` folds each backend's
       inventory into its quota ledger.
    6. ``_seed_default_agent_if_empty()`` — seed the built-in ``claude-code``
       environment + a default Agent so the first message has something to run.
    7. ``_install_e2e_fault_hooks_if_armed()`` — E2E-only; a no-op unless
       ``ASTRABOX_E2E_FAULTS`` is set.
    8. ``start_deployment_scheduler()`` — start the selected scheduler
       provider and reconcile configured scheduled Deployments before any
       background repository readers can hold a PostgreSQL snapshot.
    9. ``run_lifecycle_startup()`` — start the platform services (session
       readers and their sweep, the expiration watcher, channel recovery) so
       a restarted idle instance converges without traffic; logged-not-fatal
       on failure (the lazy first-request bootstrap remains the fallback).

    Steps 1–6 and 9 are fail-loud: key materialization, provider registration,
    schema bring-up, schema migration, and schedule reconciliation must not
    leave the app serving a capability that cannot execute.

    **Shutdown** awaits ``run_lifecycle_shutdown()`` — the sync cleanup body plus
    the draining it schedules, which only this coroutine can wait for (the loop
    is torn down the moment the lifespan returns). A teardown hook must never
    raise out of the lifespan, so an import/cleanup failure on the way down is
    logged, not fatal.
    """
    # --- Startup composition, persistence, and default resources --------------
    from astrabox.persistence.provider import get_persistence
    from astrabox.seams.sandbox import enabled_sandbox_providers, sandbox_installation_id
    from astrabox.core.service.orchestrator.runtime.sandbox_quota import (
        configure_sandbox_quotas,
        reconcile_at_startup,
    )
    from astrabox.bootstrap import bootstrap
    from astrabox.core.service.orchestrator.agent_extension_service import (
        warn_about_catalog_selections_without_a_catalog,
    )
    from astrabox.core.service.orchestrator.transcript_capability import (
        validate_signing_key_config,
    )
    from astrabox.identity.session_signing import session_signing_secret

    # Fail closed before anything else: a multi-tenant deploy with capability
    # enforcement on but no configured signing key would run the transcript
    # tenant fence on a public trust root, so its tokens would be forgeable.
    # Refuse to boot rather than serve that.
    validate_signing_key_config()
    session_signing_secret()
    bootstrap()
    await get_persistence().initialize_schema()
    from astrabox.persistence.repository.environment_repository import EnvironmentRepository

    initial_local_user_id = None
    resolver = app.state.web_identity_resolver
    if getattr(resolver, "is_no_auth_default", False):
        local_user = await resolver.resolve({})
        if local_user is not None:
            initial_local_user_id = local_user.user_id
    await EnvironmentRepository().ensure_current(initial_local_user_id=initial_local_user_id)
    await sandbox_installation_id()
    await warn_about_catalog_selections_without_a_catalog()
    await _warn_about_runtime_profiles_on_disabled_backends()
    await configure_sandbox_quotas(dict(enabled_sandbox_providers()))
    await reconcile_at_startup(dict(enabled_sandbox_providers()))
    await _seed_default_agent_if_empty()
    _install_e2e_fault_hooks_if_armed()

    # Start the selected scheduler provider on this running loop before any
    # background repository reader: the built-in DBOS provider migrates its
    # PostgreSQL schema with concurrent indexes, which must not wait on a
    # loop-owned reader whose transaction cannot close during its synchronous
    # launch. Then reconcile the stored schedules into the provider before
    # accepting traffic.
    from astrabox.core.service.orchestrator.deployment_scheduling import (
        start_deployment_scheduler,
        stop_deployment_scheduler,
    )

    await start_deployment_scheduler()

    # Platform services at boot, not on the first API request: a restarted
    # idle instance must resume session readers and reap expired sandboxes
    # without waiting for traffic (run_lifecycle_startup). Failure is logged loud but
    # does not abort boot — the lazy first-request bootstrap remains the
    # fallback, so an API-only deployment (no Docker daemon) still boots.
    try:
        from astrabox.core.service.orchestrator.service_registry import (
            run_lifecycle_startup,
        )

        await run_lifecycle_startup()
    except Exception:
        logger.error(
            "recovery control plane failed to start at boot; the first API "
            "request will retry the lazy bootstrap",
            exc_info=True,
        )

    # Extension lifespan hooks (astrabox.lifespan_hooks): enter after the
    # whole core startup sequence, exit in reverse before core teardown.
    # Startup is fail-loud like core steps 1-6; teardown is tolerated-logged.
    from astrabox.api.extensions import extension_lifespan

    try:
        async with extension_lifespan(app):
            yield
    finally:
        # --- shutdown: drain window, then graceful service teardown ---------
        # Flip /readyz to not-ready first (the LB stops routing new traffic
        # here), then optionally hold for in-flight turns.
        import os

        from astrabox.api.readiness import begin_drain

        begin_drain()
        try:
            drain_seconds = float(
                os.getenv("ASTRABOX_SHUTDOWN_DRAIN_SECONDS", "0") or "0"
            )
        except ValueError:
            drain_seconds = 0.0
        if drain_seconds > 0:
            import asyncio as _asyncio

            logger.info(
                "shutdown drain: holding %.1fs for in-flight turns", drain_seconds
            )
            try:
                await _asyncio.sleep(drain_seconds)
            except Exception:  # noqa: BLE001 — a cancelled drain sleep is fine
                pass

        try:
            # Stop the scheduler while Session services are still live. Its
            # bounded drain cannot call into an already-quiesced platform.
            await stop_deployment_scheduler()
        except Exception:  # noqa: BLE001 — shutdown must continue
            logger.warning(
                "Deployment scheduler shutdown failed (non-fatal)", exc_info=True
            )

        try:
            from astrabox.core.service.orchestrator.service_registry import (
                run_lifecycle_shutdown,
            )

            await run_lifecycle_shutdown(reason="lifespan_shutdown")
        except Exception:  # noqa: BLE001 — shutdown must not escape lifespan
            logger.warning(
                "service shutdown cleanup failed (non-fatal)", exc_info=True
            )

        try:
            await get_persistence().dispose()
        except Exception:  # noqa: BLE001 - shutdown must continue
            logger.warning("persistence shutdown failed (non-fatal)", exc_info=True)


def _register_feature_routes(app: FastAPI) -> None:
    """Mount the supporting ``register_*_routes(app)`` route modules.

    These modules (admin-api / platform-mcp / session-file /
    sandbox-callback / sandbox-inventory / transcript / vault / webhook / share
    / assistant, plus agent behind the agent feature flag) keep the clean
    ``register_*_routes(app)`` shape — each adds its routes onto the passed app and
    is idempotent per app id. They are imported here (call-time), not at module
    import, so ``create_app`` stays import-time clean. The block is fail-loud: a
    registration error is raised, not swallowed.
    """
    from astrabox.common.utils.settings import load_astrabox_settings
    from astrabox.api.routes.admin_api import register_admin_api_routes
    from astrabox.api.routes.platform_mcp import (
        register_platform_mcp_routes,
    )
    from astrabox.api.routes.session_browser import register_session_browser_routes
    from astrabox.api.routes.session_files import (
        register_session_file_routes,
    )
    from astrabox.api.routes.sandbox_callback import (
        register_sandbox_callback_routes,
    )
    from astrabox.api.routes.sandboxes import register_sandbox_routes
    from astrabox.api.routes.transcript import register_transcript_routes
    from astrabox.api.routes.runtime_state import register_runtime_state_routes
    from astrabox.api.routes.vaults import register_vault_routes
    from astrabox.api.routes.mcp_servers import register_mcp_server_routes
    from astrabox.api.routes.extensions import register_extension_routes
    from astrabox.api.routes.assistant_defaults import register_assistant_defaults_routes
    from astrabox.api.routes.deployments import register_deployment_routes
    from astrabox.api.routes.connectors import register_connector_routes
    from astrabox.api.routes.share import register_share_routes
    from astrabox.api.routes.artifacts import register_artifact_routes

    # The built-in OIDC login flow exists only when that identity mode is
    # selected: under local / trusted_header / jwt these routes would be a door
    # to nowhere (no IdP to redirect to), and an inert endpoint is the kind of
    # dead surface this codebase refuses to ship.
    if str(os.getenv("ASTRABOX_WEB_IDENTITY", "") or "").strip().lower() == "oidc":
        from astrabox.api.routes.auth import register_auth_routes

        register_auth_routes(app)

    register_admin_api_routes(app)
    register_platform_mcp_routes(app)
    register_session_file_routes(app)
    register_session_browser_routes(app)
    register_sandbox_callback_routes(app)
    register_sandbox_routes(app)
    _register_e2e_fault_routes_if_armed(app)
    register_transcript_routes(app)
    register_runtime_state_routes(app)
    register_vault_routes(app)
    register_mcp_server_routes(app)
    register_extension_routes(app)
    register_assistant_defaults_routes(app)
    register_deployment_routes(app)
    register_connector_routes(app)
    register_share_routes(app)
    register_artifact_routes(app)

    feat = load_astrabox_settings()
    if feat.agent_enabled:
        from astrabox.api.routes.agents import register_agent_routes
        from astrabox.api.routes.agent_catalog import register_agent_catalog_routes
        from astrabox.api.routes.agent_mcp import (
            register_agent_mcp_routes,
        )
        from astrabox.api.routes.mcp_tokens import register_mcp_token_routes
        from astrabox.api.routes.agent_a2a import create_agent_a2a_routes
        from astrabox.core.service.orchestrator.service_registry import get_agent_a2a_service

        register_agent_routes(app)
        register_agent_catalog_routes(app)
        register_agent_mcp_routes(app)
        app.router.routes.extend(create_agent_a2a_routes(get_agent_a2a_service()))
        # Behind the same gate as the facade the keys authenticate against:
        # issuing a credential for an endpoint this deployment does not serve
        # would be a key that opens nothing.
        register_mcp_token_routes(app)

    # User-bound assistant routes are always available and have no feature gate.
    from astrabox.api.routes.assistant import register_assistant_routes

    register_assistant_routes(app)


def create_app() -> FastAPI:
    """Construct and return the AstraBox FastAPI application.

    This is the single ingress used by both the CLI (``astrabox serve``) and any
    external ASGI runner (``uvicorn astrabox.api.app:create_app --factory``). It is
    import-safe and side-effect free beyond building the app object and mounting
    routers (it does not touch the database or the Docker daemon — that happens in
    the ``lifespan`` startup).
    """
    # One config source for both read paths (typed settings + os.getenv): fill
    # process env from .env before anything reads configuration. Real env vars
    # always win; a missing .env is a normal no-op.
    from astrabox.config.settings import load_env_file_into_process_env

    load_env_file_into_process_env()

    # These links are configuration, not best-effort decoration. Validate them
    # before the server starts so a malformed or contradictory URL cannot turn
    # into a silently missing administrator capability later.
    from astrabox.deploy.admin_integrations import configured_management_links

    configured_management_links()

    # Every browser-facing URL the server composes carries the public path
    # prefix, so an invalid value stops the server here instead of producing
    # addresses that lead nowhere.
    from astrabox.core.service.connectors.contextforge import (
        require_public_url_at_hub_path,
    )
    from astrabox.deploy.public_path import public_path_prefix

    prefix = public_path_prefix()
    require_public_url_at_hub_path()
    console_dist = _resolve_frontend_dist()

    # Access-log hygiene: the sandbox transcript capability token rides in the
    # URL path, and uvicorn's access log would otherwise print it verbatim.
    # Idempotent, covers every launch path that builds the app via this factory.
    from astrabox.api.access_log_privacy import install_access_log_token_mask

    install_access_log_token_mask()

    app = FastAPI(
        title="AstraBox",
        version=__version__,
        summary=(
            "Self-hosted agent runtime that runs installed Agent programs in isolated "
            "sandboxes and streams responses over the AI SDK Data Stream Protocol (SSE)."
        ),
        lifespan=_lifespan,
        # The reference pages are registered below the public path prefix by
        # _register_api_docs, and the document names the prefix as its server
        # so "Try it out" calls the API where clients reach it.
        docs_url=None,
        redoc_url=None,
        servers=[{"url": prefix}] if prefix else None,
    )
    _register_api_docs(app, prefix)

    # Extension middlewares are added first, which puts them innermost under
    # Starlette's LIFO add_middleware: every extension runs behind the
    # trusted-host gate and behind identity binding (see
    # astrabox/api/extensions.py for the contract).
    from astrabox.api.extensions import (
        include_extension_routers,
        install_extension_middlewares,
    )

    install_extension_middlewares(app)

    # W3C traceparent binding wraps resource routers and Starlette's
    # ExceptionMiddleware but remains inside the identity and trusted-host
    # gates. Gate rejections therefore carry no traceparent. The middleware
    # stores the trace id on request.state and stamps plain JSON, error, and SSE
    # responses. See api/routes/_shared.handle_unknown_error.
    from astrabox.web.traceparent_middleware import TraceparentMiddleware

    app.add_middleware(TraceparentMiddleware)

    # Web identity: run the configured resolver per request and bind the result
    # so every handler's get_current_user_context sees it. The no-auth default
    # resolver returns None (identity unasserted → local user), so this is a
    # no-op by default; registering a resolver at the astrabox.web.identity group
    # is what turns on authentication. Resolved once at construction (fail-loud on
    # an unknown configured name).
    from astrabox.providers.identity import load_web_identity_resolver
    from astrabox.web.identity_middleware import WebIdentityMiddleware

    from astrabox.core.service.orchestrator.mcp_client_token_service import (
        SECRET_PREFIX as _MCP_CLIENT_KEY_PREFIX,
    )

    web_identity_resolver = load_web_identity_resolver()
    app.state.web_identity_resolver = web_identity_resolver
    app.add_middleware(
        WebIdentityMiddleware,
        resolver=web_identity_resolver,
        # The Agent MCP facade verifies deployment-issued client keys itself;
        # the front door cannot judge that credential kind and must not eat
        # it. Scoped to the one path that consumes them.
        capability_bearer_paths={"/api/v1/mcp": (_MCP_CLIENT_KEY_PREFIX,)},
    )

    # Requests must arrive with the public path prefix removed: by the reverse
    # proxy, or by an ASGI host whose mount path equals the setting. Inside the
    # Host gate, so an off-allowlist request learns nothing about the prefix,
    # and outside identity, so a misrouted request is refused before any
    # resolver work.
    from astrabox.web.public_path_middleware import PublicPathMiddleware

    app.add_middleware(PublicPathMiddleware, console_dist=console_dist)

    # DNS-rebinding guard for the browser-facing surface: reject any request whose
    # Host header is not on the allowlist (ASTRABOX_ALLOWED_HOSTS; default
    # loopback). Added after the identity middleware so it wraps outermost and
    # runs first — a spoofed-Host request is refused before any resolver work. The
    # Platform MCP, transcript capability, and health paths are exempt
    # (machine-to-machine; see the module).
    from astrabox.web.trusted_host_middleware import TrustedHostMiddleware

    app.add_middleware(TrustedHostMiddleware)

    # Opt-in distributed tracing (BYO OpenTelemetry/Langfuse). Off by default:
    # with OTEL_EXPORTER_OTLP_ENDPOINT unset this is a zero-overhead no-op that
    # imports nothing and leaves boot unchanged. When set it attaches the OTLP
    # exporter + FastAPI request-span instrumentation last, so the request span
    # wraps the identity/trusted-host middlewares. instrument_app adds middleware
    # only — it does not touch the route table or the OpenAPI schema. Fail-soft:
    # any setup error is logged, never raised (see the module).
    from astrabox.observability.tracing import init_tracing

    init_tracing(app)

    @app.get("/healthz", tags=["meta"], summary="Liveness probe")
    async def healthz() -> dict[str, str]:
        """Liveness endpoint.

        Returns a static ``ok`` payload. Intentionally dependency-free (it does
        not touch the database, Docker daemon, or any provider) so it answers even
        before the runtime is wired — it reports that the HTTP server is up, which
        is exactly what the container HEALTHCHECK and load balancers need.

        Liveness stays ``ok`` through a drain (see ``/readyz``): a draining
        replica must not be killed by a liveness probe before it finishes its
        in-flight turns.
        """
        return {"status": "ok", "service": "astrabox", "version": __version__}

    @app.get("/metrics", tags=["meta"], summary="Prometheus metrics", include_in_schema=False)
    async def metrics(request: Request) -> Response:
        """Prometheus text-exposition of the in-process metric registry.

        A minimal seam (``astrabox/observability/``), not a metrics platform —
        a handful of core counters. Enabled by ``ASTRABOX_METRICS_ENABLED``
        (default true); 404 when disabled.

        Unauthenticated by default (counter names + integers; the usual probe /
        scraper posture, same as ``/healthz``) — but a deployment whose metrics
        endpoint is reachable beyond its scrape network can set
        ``ASTRABOX_METRICS_TOKEN``; the endpoint then requires
        ``Authorization: Bearer <token>`` (Prometheus ``authorization.credentials``)
        and answers 401 otherwise.
        """
        import hmac as _hmac
        import os

        from astrabox.observability.metrics import render_prometheus

        enabled = str(os.getenv("ASTRABOX_METRICS_ENABLED", "true") or "true").strip().lower() not in (
            "0", "false", "no", "off",
        )
        if not enabled:
            return Response(status_code=404)
        required_token = str(os.getenv("ASTRABOX_METRICS_TOKEN", "") or "").strip()
        if required_token:
            # The Bearer scheme is required, not just the raw secret: the documented
            # contract is Prometheus ``authorization.credentials``, and scheme
            # laxness is how header-forwarding middleboxes get surprised.
            header = str(request.headers.get("authorization") or "")
            scheme, _, credentials = header.partition(" ")
            provided = credentials.strip() if scheme.lower() == "bearer" else ""
            if not provided or not _hmac.compare_digest(provided, required_token):
                return Response(status_code=401)
        return Response(
            content=render_prometheus(), media_type="text/plain; version=0.0.4"
        )

    @app.get("/readyz", tags=["meta"], summary="Readiness probe")
    async def readyz(response: Response) -> dict[str, str]:
        """Readiness endpoint for rolling deploys.

        200 when this replica should receive new traffic; 503 during shutdown
        or after its core notification listener ends unexpectedly. In-flight
        turns are not killed by this probe. See ``astrabox/api/readiness.py``.
        """
        from astrabox.api.readiness import readiness_status

        ready, reason = readiness_status()
        response.status_code = 200 if ready else 503
        return {"status": "ready" if ready else "not_ready", "reason": reason}

    # ── HTTP turn surface ────────────────────────────────────────────────────
    # The template / user / session-CRUD / turn-streaming / admin resource
    # routers — each its own
    # ``APIRouter``, all under :mod:`astrabox.api.routes`. Trace binding + the
    # ``traceparent`` response header ride the app-level TraceparentMiddleware
    # (added above); the app-level exception handlers registered below map every
    # route's errors (APIError / validation / storage / unexpected) the same way.
    from fastapi.exceptions import RequestValidationError

    from astrabox.common.utils.errors import APIError
    from astrabox.api.routes.user import router as user_router
    from astrabox.api.routes.environments import router as environments_router
    from astrabox.api.routes.sessions import router as sessions_router
    from astrabox.api.routes.turns import router as turns_router
    from astrabox.api.routes.speech_input import router as speech_input_router
    from astrabox.api.routes.admin import router as admin_router
    from astrabox.api.routes._shared import (
        handle_api_error,
        handle_request_validation_error,
        handle_unknown_error,
    )

    app.include_router(user_router)
    app.include_router(environments_router)
    from astrabox.api.routes.session_user_actions import router as session_user_actions_router

    app.include_router(sessions_router)
    app.include_router(session_user_actions_router)
    app.include_router(turns_router)
    app.include_router(speech_input_router)
    app.include_router(admin_router)

    app.add_exception_handler(APIError, handle_api_error)
    app.add_exception_handler(
        RequestValidationError, handle_request_validation_error
    )
    app.add_exception_handler(Exception, handle_unknown_error)

    # ── Supporting route modules ─────────────────────────────────────────────
    _register_feature_routes(app)

    # ── Extension routers (after every core route, before the SPA catch-all) ──
    include_extension_routers(app)

    # ── Console SPA (served last so it only catches unmatched paths) ──────────
    if console_dist is not None:
        _mount_frontend(app, console_dist)

    return app


def _register_api_docs(app: FastAPI, prefix: str) -> None:
    """Serve the interactive API reference at ``/docs`` and ``/redoc``.

    These are FastAPI's own pages, built by the same functions with the same
    settings. FastAPI points them at ``root_path`` followed by
    ``/openapi.json``, and ``root_path`` is empty behind a reverse proxy that
    removed the public path prefix, so its pages would fetch the document from
    the root of the host. These point at the prefixed document instead, which
    is the same address under an ASGI mount, where the prefix is the root
    path.
    """
    from fastapi.openapi.docs import (
        get_redoc_html,
        get_swagger_ui_html,
        get_swagger_ui_oauth2_redirect_html,
    )
    from fastapi.responses import HTMLResponse

    openapi_url = prefix + str(app.openapi_url)
    oauth2_redirect_url = str(app.swagger_ui_oauth2_redirect_url)

    async def swagger_ui_html(request: Request) -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url=openapi_url,
            title=f"{app.title} - Swagger UI",
            oauth2_redirect_url=prefix + oauth2_redirect_url,
            init_oauth=app.swagger_ui_init_oauth,
            swagger_ui_parameters=app.swagger_ui_parameters,
        )

    async def swagger_ui_redirect(request: Request) -> HTMLResponse:
        return get_swagger_ui_oauth2_redirect_html()

    async def redoc_html(request: Request) -> HTMLResponse:
        return get_redoc_html(openapi_url=openapi_url, title=f"{app.title} - ReDoc")

    # Plain Starlette routes, registered as FastAPI registers its own pages.
    app.add_route("/docs", swagger_ui_html, include_in_schema=False)
    app.add_route(oauth2_redirect_url, swagger_ui_redirect, include_in_schema=False)
    app.add_route("/redoc", redoc_html, include_in_schema=False)


def _resolve_frontend_dist() -> Path | None:
    """Return the built console SPA directory, or ``None`` for an API-only server.

    Resolution order for the built SPA:

    1. ``ASTRABOX_FRONTEND_DIST`` (set in the server image / by operators),
    2. the packaged copy at ``astrabox/_frontend_dist`` (release wheels ship
       the built console there — see ``make build-dist``),
    3. the in-repo ``frontend/dist`` (a source checkout after
       ``npm run build``).

    When none exists the app stays API-only and says so at WARNING level —
    a wheel install without the packaged SPA and a source checkout that has
    not built the console both land here, and a silent API-only boot reads as
    "the UI is broken" to a first-run user.
    """
    packaged_dist = Path(__file__).resolve().parents[1] / "_frontend_dist"
    repo_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    env_dist = os.environ.get("ASTRABOX_FRONTEND_DIST")
    candidates = (
        [Path(env_dist)] if env_dist else [packaged_dist, repo_dist]
    )
    dist = next(
        (c.resolve() for c in candidates if (c / "index.html").is_file()),
        None,
    )
    if dist is None:
        logger.warning(
            "console SPA not found (checked %s) — serving API only. "
            "pip install: this wheel was built without the packaged console; "
            "set ASTRABOX_FRONTEND_DIST to a built frontend/dist. "
            "source checkout: run `npm run build` in frontend/, or use "
            "`make dev` for the Vite console.",
            ", ".join(str(c) for c in candidates),
        )
    return dist


def _mount_frontend(app: FastAPI, dist: Path) -> None:
    """Serve the built console SPA in *dist* so ``:8000`` opens the UI, not raw JSON.

    A single catch-all GET, registered after every API router, serves a real
    file from the dist when one matches (hashed assets get an immutable cache
    header) and otherwise falls back to ``index.html`` so the main SPA's
    client-side routes (``/manage/*`` …) deep-link correctly. ``index.html``
    itself, at any route or by name, is rendered for the public path prefix
    (:func:`~astrabox.api.frontend_cache.console_shell_response`). It never
    shadows the ``/api`` / ``/healthz`` surfaces.
    """
    from fastapi.responses import FileResponse, Response

    from astrabox.api.frontend_cache import (
        asset_cache_control_for_name,
        console_shell_response,
    )
    from astrabox.deploy.public_path import public_path_prefix

    index_html = dist / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _serve_spa(full_path: str) -> Response:
        # Guard the API/health surfaces: this only fires for
        # otherwise-unmatched GETs (already matched above).
        if full_path.startswith("api/") or full_path == "healthz":
            return Response(status_code=404)
        if full_path:
            candidate = (dist / full_path).resolve()
            if (
                candidate != index_html
                and candidate.is_file()
                and candidate.is_relative_to(dist)
            ):
                response = FileResponse(candidate)
                response.headers.setdefault(
                    "Cache-Control", asset_cache_control_for_name(candidate.name)
                )
                return response
        # SPA fallback: client-side routing resolves inside index.html.
        return console_shell_response(index_html, public_path_prefix())

    logger.info("console SPA mounted from %s", dist)
