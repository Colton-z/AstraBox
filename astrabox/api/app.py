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
  the persistence schema, and seeds the default Environment and Agent, and on
  **shutdown** awaits ``run_lifecycle_shutdown()``.

``create_app()`` is import-time constructible; the persistence backend is resolved
lazily by the DAL ingress, so route activation is never gated on a Mongo URI.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from astrabox import __version__
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.time_utils import utcnow_iso

logger = get_logger(__name__)

#: The seeded default environment name.
_DEFAULT_ENVIRONMENT_NAME = "claude-code"


def _seed_default_model() -> str:
    """Return the deployment model reported when built-in Agents are seeded.

    The selected endpoint provider owns any provider-specific model override.
    ``astrabox/config/app.yml`` supplies the bundled model unless deployment
    configuration replaces it.
    """

    from astrabox.common.utils.settings import load_astrabox_settings
    from astrabox.seams.model import ModelEndpoint, model_endpoint_for_name

    settings = load_astrabox_settings()
    endpoint = model_endpoint_for_name(settings.model_endpoint_provider).resolve(
        requested=ModelEndpoint(
            model_name=str(settings.model_name or "").strip() or None,
        ),
        settings=settings,
    )
    return str(endpoint.model_name or "").strip()

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


async def _seed_default_agent_if_empty() -> None:
    """Seed the built-in ``claude-code`` environment + a usable default Agent when
    the ``agents`` store is empty.

    A fresh store has no agents, so the console would open on an empty list with
    nothing to converse with. This seeds one Agent plus the ``claude-code``
    Environment it selects, so the first-run path works without separate setup.
    Runs only when
    the agents collection is empty, so an operator's later edits are never
    overwritten.

    Credentials are not seeded: the environment carries no ``provider_access`` and
    no ``endpoint_provider`` here, so model connectivity falls back to the
    deployment's selected model endpoint provider. Organization, model, backend,
    and image selectors are also absent, so their resolution boundaries use the
    current request or deployment defaults until an operator writes a pin.
    """
    import uuid

    from astrabox.persistence.repository import AgentRepository, EnvironmentRepository
    from astrabox.seams.sandbox import default_sandbox_backend, sandbox_for_name

    agents_repo = AgentRepository()
    existing = await agents_repo.list_all_agents()
    if existing:
        logger.debug("agent seed skipped: %d agent(s) already present", len(existing))
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

    environments_repo = EnvironmentRepository()
    # The seed writes repositories directly instead of using
    # AgentConfigService.upsert_environment_config, so it supplies record
    # timestamps explicitly.
    seeded_at = utcnow_iso()
    # The environment is the single source of runtime config (image, backend,
    # install policy) + model connectivity. Seed it first so the agent resolves
    # cleanly. No endpoint_provider / provider_access — deployment defaults apply.
    await environments_repo.upsert_by_name(
        _DEFAULT_ENVIRONMENT_NAME,
        {
            "created_at": seeded_at,
            "updated_at": seeded_at,
            "updated_by": "system",
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
        },
    )

    deployment_model = _seed_default_model()
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
        # Organization and model stay absent. Catalog resolution uses the
        # request's current organization and runtime resolution uses the
        # deployment's current model; explicitly authored values still win.
        return {
            "agent_id": str(uuid.uuid4()),
            "enabled": True,
            "version": 1,
            "state": "ACTIVE",
            "user_id": "system",
            "created_by": "system",
            "visibility": "public",
            "name": name,
            "created_at": seeded_at,
            "updated_at": seeded_at,
            "description": description,
            "system": system,
            "environment_name": _DEFAULT_ENVIRONMENT_NAME,
            "display_meta": {"display_name": name},
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
                "plugin_repos": [
                    {
                        "url": _FINANCIAL_SERVICES_REPOSITORY,
                        "protocol": "https",
                        "sha": _FINANCIAL_SERVICES_REVISION,
                        "plugin_paths": [
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
        "+ 2 agents (deployment model=%r)",
        _DEFAULT_ENVIRONMENT_NAME,
        backend_name,
        default_runtime_image,
        deployment_model or "<unset>",
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
    4. ``set_default_sandbox_backend(...)`` — publish the deployment-configured
       default backend (settings) to the seam; the only place a default backend
       name enters the dispatch path.
    5. ``backend.create_all()`` — migration-free persistence schema bring-up for
       the active backend (``CREATE TABLE IF NOT EXISTS`` + WAL for SQLite; a no-op
       for Mongo). This is the canonical schema-bring-up seam.
    6. ``run_pending_migrations()`` — the versioned schema-migration runner
       (:mod:`astrabox.persistence.migrations`) that brings ``schema_meta`` up
       to the running code's latest known version right after the DDL
       bring-up above. It is a no-op when the store already carries the latest
       registered schema version.
    7. ``_seed_default_agent_if_empty()`` — seed the built-in ``claude-code``
       environment + a default Agent so the first turn has something to run.
    8. ``_install_e2e_fault_hooks_if_armed()`` — E2E-only; a no-op unless
       ``ASTRABOX_E2E_FAULTS`` is set.
    9. ``start_deployment_run_runtime()`` — launch the embedded durable
       schedule/Run engine and reconcile configured scheduled Deployments
       before any background repository readers can hold a PostgreSQL snapshot.
    10. ``run_lifecycle_startup()`` — start the recovery control plane (reconcile
       worker, expiration watcher) so a restarted idle instance
       converges without traffic; logged-not-fatal on failure (the lazy
       first-request bootstrap remains the fallback).

    Steps 1–7 and 10 are fail-loud: key materialization, provider registration,
    schema bring-up, schema migration, and schedule reconciliation must not
    leave the app serving a capability that cannot execute.

    **Shutdown** awaits ``run_lifecycle_shutdown()`` — the sync cleanup body plus
    the draining it schedules, which only this coroutine can wait for (the loop
    is torn down the moment the lifespan returns). A teardown hook must never
    raise out of the lifespan, so an import/cleanup failure on the way down is
    logged, not fatal.
    """
    # --- Startup composition, persistence, and default resources --------------
    from astrabox.persistence.repository import backend as _dal_backend
    from astrabox.persistence.migrations import run_pending_migrations
    from astrabox.bootstrap import bootstrap
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
    await _dal_backend.create_all()
    await run_pending_migrations()
    await _seed_default_agent_if_empty()
    _install_e2e_fault_hooks_if_armed()

    # DBOS is an embedded execution engine, not a separate service. Launch it
    # on this running loop before starting background repository readers. Its
    # PostgreSQL bootstrap uses concurrent indexes, which must not be made to
    # wait on a loop-owned reader whose transaction cannot close while this
    # synchronous launch is running. Then reconcile Deployment config into its
    # schedule projection before accepting traffic.
    from astrabox.core.service.orchestrator.deployment_run_runtime import (
        start_deployment_run_runtime,
        stop_deployment_run_runtime,
    )

    await start_deployment_run_runtime()

    # Recovery control plane at boot, not on the first API request: a restarted
    # idle instance must converge stuck sessions / reap expired sandboxes
    # without waiting for traffic (run_lifecycle_startup → the reconcile worker
    # and the expiration watcher). Failure is logged loud but
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
            # Stop DBOS while Session services are still live. Its bounded
            # workflow drain cannot call into an already-quiesced platform.
            await stop_deployment_run_runtime()
        except Exception:  # noqa: BLE001 — shutdown must continue
            logger.warning(
                "Deployment Run runtime shutdown failed (non-fatal)", exc_info=True
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
    from astrabox.api.routes.deployments import register_deployment_routes
    from astrabox.api.routes.share import register_share_routes

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
    register_sandbox_callback_routes(app)
    register_sandbox_routes(app)
    _register_e2e_fault_routes_if_armed(app)
    register_transcript_routes(app)
    register_runtime_state_routes(app)
    register_vault_routes(app)
    register_mcp_server_routes(app)
    register_extension_routes(app)
    register_deployment_routes(app)
    register_share_routes(app)

    feat = load_astrabox_settings()
    if feat.agent_enabled:
        from astrabox.api.routes.agents import register_agent_routes
        from astrabox.api.routes.agent_mcp import (
            register_agent_mcp_routes,
        )
        from astrabox.api.routes.mcp_tokens import register_mcp_token_routes

        register_agent_routes(app)
        register_agent_mcp_routes(app)
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
    )

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

        200 when this replica should receive new traffic; 503 once shutdown
        begins (drain / quiesce), so the load balancer stops routing here while
        in-flight turns finish. See ``astrabox/api/readiness.py``.
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
    from astrabox.api.routes.sessions import router as sessions_router
    from astrabox.api.routes.turns import router as turns_router
    from astrabox.api.routes.admin import router as admin_router
    from astrabox.api.routes._shared import (
        handle_api_error,
        handle_request_validation_error,
        handle_unknown_error,
    )

    app.include_router(user_router)
    app.include_router(sessions_router)
    app.include_router(turns_router)
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
    _mount_frontend(app)

    return app


def _mount_frontend(app: FastAPI) -> None:
    """Serve the built console SPA so ``:8000`` opens the UI, not raw JSON.

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

    A single catch-all GET, registered after every API router, serves a real
    file from the dist when one matches (hashed assets get an immutable cache
    header) and otherwise falls back to ``index.html`` so the main SPA's
    client-side routes (``/manage/*`` …) deep-link correctly. It never shadows
    the ``/api`` / ``/healthz`` surfaces.
    """
    import os
    from pathlib import Path

    from fastapi.responses import FileResponse, Response

    from astrabox.api.frontend_cache import (
        asset_cache_control_for_name,
        frontend_html_response,
    )

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
        return
    index_html = dist / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _serve_spa(full_path: str) -> Response:
        # Guard the API/health surfaces: this only fires for
        # otherwise-unmatched GETs (already matched above).
        if full_path.startswith("api/") or full_path == "healthz":
            return Response(status_code=404)
        if full_path:
            candidate = (dist / full_path).resolve()
            if candidate.is_file() and candidate.is_relative_to(dist):
                response = FileResponse(candidate)
                response.headers.setdefault(
                    "Cache-Control", asset_cache_control_for_name(candidate.name)
                )
                return response
        # SPA fallback: client-side routing resolves inside index.html.
        return frontend_html_response(index_html)

    logger.info("console SPA mounted from %s", dist)
