"""Claude Code's vendor declaration, preparation, and activation machinery.

The platform owns placement, workspace, credentials, lifecycle, recovery, and
endpoint resolution. This module maps the resulting prepared context onto the
Claude SDK and runner wire. It may prepare an input-free Claude child, connect
a new Session, or use Claude's attach/configure handshake after host recovery;
none of those operations can allocate or adopt a sandbox.

This module must not import from
``astrabox.core.service.orchestrator.runtime_manager`` at module load time:
that module calls ``astrabox.providers.register_builtin_providers()`` at its
own module scope, which transitively imports ``engine.claude_code`` (which
imports this module) to register the claude_code adapter — a module-level
import back into ``runtime_manager`` from here would be a load-time cycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import json
from collections.abc import Awaitable, Callable
from enum import Enum
from pathlib import Path
from typing import Any, cast, get_args

from claude_agent_sdk import ClaudeAgentOptions, PermissionMode, SessionStore

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.seams.egress_credentials import (
    EGRESS_HELD_PLACEHOLDER,
)
from astrabox.core.model import AgentView
from astrabox.core.service.orchestrator.engine.base import (
    EngineConversationBinding,
    EngineEventSink,
    EnginePreparationContext,
    EngineStartupContext,
    ResidentOutputSink,
    initialize_engine_client,
)
from astrabox.core.service.orchestrator.engine.provisioning import (
    EngineSandboxRequest,
    ModelCredentialRequest,
)
from astrabox.core.service.orchestrator.engine.claude_code_options import (
    CLAUDE_WIRE_OPTION_KEYS,
)
from astrabox.core.service.orchestrator.engine.input_delivery import (
    consumption_carrier,
)
from astrabox.core.service.orchestrator.engine.claude_code_config import (
    ModelConfig,
    build_claude_model_config_kwargs,
    build_claude_options_kwargs,
)
from astrabox.core.service.orchestrator.runtime.models import SessionRuntime
from astrabox.core.service.orchestrator.runtime.diagnostics import (
    build_runtime_start_error_message,
    collect_runtime_start_diagnostics,
)
from astrabox.core.service.orchestrator.runtime.mcp_servers import (
    is_platform_mcp_server,
    template_mcp_servers,
)
from astrabox.core.service.orchestrator.runtime.plugin_repos import plugin_repo_egress_hosts
from astrabox.core.service.orchestrator.runtime.sandbox_client import (
    extract_sandbox_id,
    get_underlying_sandbox,
)
from astrabox.providers.sandbox_image import (
    AIO_IMAGE_ENTRYPOINT,
    IN_BOX_SIDECAR_PORT,
)
from astrabox.seams.sandbox import sandbox_for_sandbox
from astrabox.persistence.transcript_session_store import TranscriptSessionStore

logger = get_logger(__name__)


def normalize_claude_permission_mode(value: str | None) -> str:
    """Resolve Claude's vendor-owned mode at the Claude adapter boundary."""

    mode = str(value or "").strip() or "default"
    if mode not in get_args(PermissionMode):
        raise APIError(
            code="CLAUDE_OPTIONS_INVALID",
            message=f"unknown Claude permission mode {mode!r}",
            status_code=400,
        )
    return mode


# ── claude-only launch/config builders ──────────────────────────────────────
#
# The platform supplies neutral facts; these helpers map them to Claude's
# executor and SDK contracts.


# Default for a ``ModelConfig`` that omits ``credential_header``: the
# credential keeps riding as a Bearer auth token, so callers that do not set
# it see no behavior change.
_DEFAULT_CREDENTIAL_HEADER = "ANTHROPIC_AUTH_TOKEN"
_VALID_CREDENTIAL_HEADERS = frozenset({"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"})


def _credential_header(model_config: Any) -> str:
    """The sandbox env var ``model_config.api_key`` rides under.

    ``ANTHROPIC_API_KEY`` (x-api-key) for a plain Anthropic API key,
    ``ANTHROPIC_AUTH_TOKEN`` (Bearer) for everything else — see
    ``build_claude_model_config_kwargs``, which maps the neutral credential
    kind alongside ``api_key`` so the two never disagree. Falls back to
    ``ANTHROPIC_AUTH_TOKEN`` for a missing or unrecognized value.
    """
    header = str(getattr(model_config, "credential_header", "") or "").strip()
    return header if header in _VALID_CREDENTIAL_HEADERS else _DEFAULT_CREDENTIAL_HEADER


def _anthropic_container_env(model_config: Any) -> dict[str, str]:
    """``ANTHROPIC_*`` sandbox env for a CLI-direct agent sandbox, credential
    excluded.

    Sets the model-endpoint env block in the engine's box declaration. The
    platform composes that declaration into whichever create it chooses; the
    in-box agent CLI then talks to the model endpoint. Only non-empty values are
    emitted.

    The credential MUST NOT be one of them. It travels on exactly one channel
    — ``_model_runtime_creds`` → ``build_claude_options``'s ``options.env``
    overlay — for two reasons that reinforce each other:

    * That channel is the only one every box has. A prewarmed box is created
      before any session claims it, so it never receives a per-session
      container env at all; options.env is what its CLI is launched with.
      Delivering the credential here as well would make cold boxes and pooled
      boxes take different paths to the same secret, and only one of them would
      be exercised by the common case.
    * A credential must never travel without the endpoint it is scoped to, and
      that rule is enforced in ``build_claude_options`` (a gateway Bearer token
      with no ``base_url`` is withheld). A second, independent injection point
      would be a second place that has to implement the same rule correctly:
      emitting the credential and the endpoint under two separate conditions
      sends the token to whatever endpoint the CLI defaults to whenever a
      config carries a credential and no base URL.

    What stays is deliberately non-secret: the endpoint, the model name, and
    ``IS_SANDBOX``. They are what makes a box self-describing in a diagnostic
    report (see ``_DIAGNOSTIC_ENV_ALLOWLIST`` in the open_sandbox backend,
    which prints exactly these), and none of them is a credential.

    ``_credential_header`` still resolves which var the credential rides under —
    on the options.env channel, via ``_model_runtime_creds``.
    """
    base_url = str(getattr(model_config, "base_url", "") or "").strip()
    model_name = str(getattr(model_config, "model_name", "") or "").strip()
    env: dict[str, str] = {"IS_SANDBOX": "1"}
    if base_url:
        env["ANTHROPIC_BASE_URL"] = base_url
    if model_name:
        env["ANTHROPIC_MODEL"] = model_name
        env["ANTHROPIC_SMALL_FAST_MODEL"] = model_name
    return env


def sandbox_request(
    *, template: AgentView, model_access: Any
) -> EngineSandboxRequest:
    """Declare Claude's box requirements without choosing or creating a box."""

    model_config = ModelConfig(**build_claude_model_config_kwargs(model_access))
    return EngineSandboxRequest(
        entrypoint=AIO_IMAGE_ENTRYPOINT,
        credential=ModelCredentialRequest(
            access=model_access,
            request_paths=("v1/*",),
            missing_code="AGENT_RUNTIME_ERROR",
            missing_message="model api key/base_url not configured",
            missing_status=500,
            header=_credential_header(model_config),
            request_methods=("GET", "POST"),
        ),
        env=_anthropic_container_env(model_config),
        required_network_hosts=tuple(plugin_repo_egress_hosts(template)),
        wait_for_inbox_service_port=IN_BOX_SIDECAR_PORT,
    )


def _model_runtime_creds(model_config: Any, *, vault: bool = False) -> dict[str, str]:
    """Per-conversation model identity injected via options.env.

    Delivers the model name and auth token onto the per-session `claude` launch
    per-conversation rather than sandbox-wide, so the sandbox itself is never
    stamped with a model identity. build_claude_options only sets a var when its
    value is non-empty, so an engine whose auth lives elsewhere simply carries
    whatever its model_config already had. Reuses the model_config resolved at the
    call site (no secret re-fetch).

    This is the only channel that carries the credential. The sandbox-create
    env (``_anthropic_container_env``) deliberately carries none, so there is
    one place where "which header does it ride under" and "does it travel with
    its endpoint" are decided, and no second place that has to agree.

    Carries ``credential_header`` alongside ``api_key`` so
    ``build_claude_options`` sets the env var name the credential's own source
    calls for (``ANTHROPIC_API_KEY`` for a vendor x-api-key,
    ``ANTHROPIC_AUTH_TOKEN`` for a gateway Bearer token — see
    ``build_claude_model_config_kwargs``).

    Carries ``base_url`` because a credential must arrive with the endpoint it
    is scoped to, and it is load-bearing on this channel in particular: a
    pooled box is created before any session claims it, so it has no
    per-session container env at all — options.env is the only thing that
    reaches its CLI. A credential delivered without its endpoint is a
    credential sent to whatever endpoint the CLI defaults to, so the endpoint
    travels with it, on the same channel, always.
    """
    # With the vault in use the CLI is launched with a placeholder: the real
    # value never enters the box, and the sidecar attaches it on the way out.
    api_key = str(getattr(model_config, "api_key", "") or "").strip()
    return {
        "model_name": str(getattr(model_config, "model_name", "") or "").strip(),
        "api_key": EGRESS_HELD_PLACEHOLDER if (vault and api_key) else api_key,
        "base_url": str(getattr(model_config, "base_url", "") or "").strip(),
        "credential_header": _credential_header(model_config),
        # Forwarded host-side so build_claude_options can ask the selected
        # provider for endpoint-specific request headers.
        "endpoint_provider": str(getattr(model_config, "endpoint_provider", "") or "").strip(),
    }


def _build_claude_options(
    settings: Any,
    template: Any,
    *,
    cwd: str | None = None,
    session_id: str | None = None,
    resume: str | None = None,
    permission_mode: str | None = None,
    runtime_identity: dict[str, Any] | None = None,
    capability_scope: str = "conversation",
    model_runtime_creds: dict[str, Any] | None = None,
    runtime_env: dict[str, str] | None = None,
    prepared_slot_id: str | None = None,
) -> Any:
    return ClaudeAgentOptions(
        **build_claude_options_kwargs(
            settings,
            template,
            cwd=cwd,
            session_id=session_id,
            resume=resume,
            permission_mode=normalize_claude_permission_mode(permission_mode),
            runtime_identity=runtime_identity,
            capability_scope=capability_scope,
            model_runtime_creds=model_runtime_creds,
            runtime_env=runtime_env,
            prepared_slot_id=prepared_slot_id,
        )
    )


def _disable_plugin_mcp_autostart(options: Any, server_ids: list[str]) -> Any:
    """Keep Plugin capabilities while refusing its implicit MCP connections.

    Claude Code starts a Plugin's MCP servers automatically. AstraBox
    owns the Agent's selected MCP map separately, so starting those bundled
    servers as well creates a second, unaudited connection path. Carry the
    vendor's own ``deniedMcpServers`` setting across the runner wire; do
    not rewrite the Plugin checkout or invent another MCP vocabulary.
    """

    identities = sorted(
        {str(server_id).strip() for server_id in server_ids if str(server_id).strip()}
    )
    if not identities:
        return options

    raw_settings = str(getattr(options, "settings", None) or "").strip()
    settings: dict[str, Any] = {}
    if raw_settings:
        try:
            parsed = json.loads(raw_settings)
        except json.JSONDecodeError as exc:
            raise APIError(
                code="CLAUDE_OPTIONS_INVALID",
                message=(
                    "AstraBox cannot combine Plugin MCP selection with a Claude "
                    "settings file path; supply the settings as a JSON object"
                ),
                status_code=500,
            ) from exc
        if not isinstance(parsed, dict):
            raise APIError(
                code="CLAUDE_OPTIONS_INVALID",
                message="Claude settings must be a JSON object",
                status_code=500,
            )
        settings = dict(parsed)

    existing = settings.get("deniedMcpServers", [])
    if not isinstance(existing, list) or any(not isinstance(item, dict) for item in existing):
        raise APIError(
            code="CLAUDE_OPTIONS_INVALID",
            message="Claude deniedMcpServers must be a list of restriction objects",
            status_code=500,
        )
    denied = list(existing)
    existing_names = {
        str(item.get("serverName") or "").strip()
        for item in existing
        if str(item.get("serverName") or "").strip()
    }
    denied.extend(
        {"serverName": server_id}
        for server_id in identities
        if server_id not in existing_names
    )
    settings["deniedMcpServers"] = denied
    return dataclasses.replace(
        options,
        settings=json.dumps(settings, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )


async def activate_runtime(context: EngineStartupContext) -> SessionRuntime:
    """Activate Claude in a box whose lifecycle the platform already settled."""

    prepared = dict(context.prepared_manifest or {})
    is_prepared = context.prepared_manifest is not None
    engine_client = None
    try:
        template = context.template
        model_config = ModelConfig(
            **build_claude_model_config_kwargs(context.model_access)
        )
        model_runtime_creds = _model_runtime_creds(model_config, vault=False)
        # This value is deliberately the platform's delivery result: a real value
        # without Vault, the general placeholder with Vault, or a slot-specific
        # placeholder after prepared-unit claim. The engine never chooses which.
        model_runtime_creds["api_key"] = context.model_credential
        options = _build_claude_options(
            context.deployment_settings,
            template,
            cwd=context.cwd,
            session_id=context.session_id,
            resume=context.resume_session_key,
            permission_mode=context.permission_mode,
            runtime_identity=context.runtime_identity,
            capability_scope=context.capability_scope,
            model_runtime_creds=model_runtime_creds,
            runtime_env=dict(context.runtime_env or {}),
        )
        runner_uri = str(context.runner_uri or "").strip()
        if not runner_uri:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message="the platform supplied Claude no ready runner endpoint",
                status_code=500,
            )
        carrier = consumption_carrier(
            context.sandbox_id,
            str((context.runtime_identity or {}).get("isolated_session_id") or ""),
        )
        if context.attach_mode is not None:
            runner_options = _disable_plugin_mcp_autostart(
                options,
                list(
                    (context.runtime_identity or {}).get(
                        "plugin_mcp_server_ids"
                    )
                    or []
                ),
            )
            engine_client, how = await _attach_runner_engine_client(
                runner_uri,
                session_id=context.session_id,
                workspace_dir=str(options.cwd or ""),
                sdk_options=_runner_configure_options(runner_options),
                transcript_store=context.transcript_store,
                sandbox_death_notice=context.sandbox_death_notice,
                consumer_carrier=carrier,
                event_sink=context.event_sink,
                resident_output_sink=context.resident_output_sink,
            )
            logger.info(
                "runtime reattached (%s): session=%s sandbox=%s resume=%s",
                how,
                context.session_id,
                context.sandbox_id,
                bool(context.resume_session_key),
            )
        elif is_prepared:
            engine_client = await _activate_runner_engine_client(
                runner_uri,
                slot_id=str(prepared.get("slot_id") or ""),
                activation_token=str(prepared.get("activation_token") or ""),
                session_id=context.session_id,
                workspace_dir=str(options.cwd or ""),
                permission_mode=context.permission_mode,
                resume_session_key=context.resume_session_key,
                activation_mcp_servers=[
                    str(name).strip()
                    for name in (prepared.get("activation_mcp_servers") or [])
                    if str(name).strip()
                ],
                transcript_store=context.transcript_store,
                sandbox_death_notice=context.sandbox_death_notice,
                consumer_carrier=carrier,
                event_sink=context.event_sink,
                resident_output_sink=context.resident_output_sink,
            )
        else:
            runner_options = _disable_plugin_mcp_autostart(
                options,
                list(
                    (context.runtime_identity or {}).get(
                        "plugin_mcp_server_ids"
                    )
                    or []
                ),
            )
            engine_client = await _connect_runner_engine_client(
                runner_uri,
                session_id=context.session_id,
                workspace_dir=str(options.cwd or ""),
                sdk_options=_runner_configure_options(runner_options),
                transcript_store=context.transcript_store,
                sandbox_death_notice=context.sandbox_death_notice,
                consumer_carrier=carrier,
                event_sink=context.event_sink,
                resident_output_sink=context.resident_output_sink,
            )
        engine_manifest = await initialize_engine_client(
            engine_client,
            expected_engine_kind="claude_code",
            conversation_binding=EngineConversationBinding(
                platform_session_id=context.session_id,
                engine_session_key=context.resume_session_key,
            ),
        )
    except APIError:
        if engine_client is not None:
            with contextlib.suppress(BaseException):
                await engine_client.close()
        raise
    except BaseException as exc:
        failure: BaseException = exc
        if (
            context.attach_mode is None
            and context.prepared_manifest is None
            and not isinstance(exc, asyncio.CancelledError)
        ):
            diagnostic = await collect_runtime_start_diagnostics(
                context.sandbox,
                session_id=context.session_id,
                exc=exc,
                sandbox_provider=sandbox_for_sandbox(context.sandbox),
                get_underlying_sandbox_fn=get_underlying_sandbox,
                extract_sandbox_id_fn=extract_sandbox_id,
                runtime_identity=context.runtime_identity,
            )
            failure = APIError(
                code="AGENT_RUNTIME_ERROR",
                message=build_runtime_start_error_message(exc, diagnostic),
                status_code=502,
            )
        if engine_client is not None:
            with contextlib.suppress(BaseException):
                await engine_client.close()
        if failure is not exc:
            raise failure from exc
        raise
    # The link is resident from here on and the runner keeps receiving after
    # every Result. Output the engine produces on its own — a queued task
    # notification's answer — has a publisher only once this observer reads
    # the link between turns; a turn consumer takes the read position over
    # for its own input and hands it back at its terminal.
    engine_client.start_resident_observation()
    return SessionRuntime(
        session_id=context.session_id,
        agent=None,
        engine_kind="claude_code",
        engine_client=engine_client,
        engine_manifest=engine_manifest,
        conversation_bound=True,
        sandbox=context.sandbox,
        sandbox_id=context.sandbox_id,
        isolated_session_id=(
            str(
                (context.runtime_identity or {}).get("isolated_session_id")
                or ""
            )
            or None
        ),
        engine_session_key=engine_client.engine_session_key,
        terminal_cwd=context.cwd,
        permission_mode=normalize_claude_permission_mode(context.permission_mode),
        runtime_identity=context.runtime_identity,
        prepare_engine_input=context.prepare_engine_input,
    )


def _claude_option_wire_value(value: Any) -> Any:
    """Convert one SDK option into the runner's JSON value language."""

    if isinstance(value, Enum):
        return _claude_option_wire_value(value.value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _claude_option_wire_value(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, dict):
        return {
            str(key): _claude_option_wire_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_claude_option_wire_value(item) for item in value]
    raise TypeError(f"unsupported value type {type(value).__name__}")


def _runner_configure_options(options: Any) -> dict[str, Any]:
    """Serialize the built ``ClaudeAgentOptions`` into ``configure.options``.

    The Claude adapter owns the fields it composes or exposes. Host-only fields
    (hooks, callbacks, stores, transports) never leave the host. The runner
    independently validates these names against the SDK installed in its image,
    and a value the wire cannot carry is a loud error rather than a dropped
    option.
    """

    sdk_fields = frozenset(field.name for field in dataclasses.fields(ClaudeAgentOptions))
    missing = CLAUDE_WIRE_OPTION_KEYS - sdk_fields
    if missing:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=(
                "the Claude adapter declares options absent from the installed SDK: "
                f"{sorted(missing)!r}"
            ),
            status_code=500,
        )

    out: dict[str, Any] = {}
    for name in sorted(CLAUDE_WIRE_OPTION_KEYS):
        value = getattr(options, name, None)
        if value in (None, [], {}, ()):
            continue
        try:
            out[name] = _claude_option_wire_value(value)
        except (TypeError, ValueError) as exc:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=(
                    f"claude option {name!r} is not wire-serializable for the "
                    f"runner: {exc}"
                ),
                status_code=500,
            ) from exc
    # The one option the runner itself consumes rather than the SDK: how long a
    # conversation keeps holding an answer slot for a host that is not
    # connected. It rides in `prepare.options` because that is where the runner
    # reads it (`RUNNER_OPTION_KEYS`); sent anywhere else it is accepted and
    # ignored. Being here also puts it in the spawn fingerprint, so a prepared
    # slot built under one budget is not handed to a deployment with another.
    out["interaction_wait_s"] = float(
        load_astrabox_settings().runner_interaction_wait_seconds
    )
    try:
        json.loads(json.dumps(out, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=f"claude options are not wire-serializable for the runner: {exc}",
            status_code=500,
        ) from exc
    return out


def _template_platform_mcp_servers(template: Any) -> list[str]:
    """Declared MCP servers the platform itself hosts behind the proxy."""

    return sorted(
        str(name)
        for name, config in template_mcp_servers(
            getattr(template, "mcp_servers", None)
        ).items()
        if isinstance(config, dict) and is_platform_mcp_server(config)
    )


def _activation_mcp_server_names(template: Any) -> list[str]:
    """Remote MCP declarations whose handshake needs the claiming user's identity.

    These are the servers activation must reconnect and prove ``connected``
    before input is released. The platform's MCP assignment vocabulary has no
    per-user-identity marker — every declared server is anonymous or carries a
    shared secret the gateway injects — so nothing defers to activation and
    every server connects at prepare, leaving the slot in exactly the steady
    state an ordinary session start produces. When
    user-bound connections enter the assignment vocabulary they belong here;
    requiring every remote server instead was measured to impose a stronger
    guarantee than ordinary sessions have and to fail on gateway-managed
    servers whose credential injection is independent of the Session.
    """

    _ = template
    return []


async def prepare_runtime(context: EnginePreparationContext) -> dict[str, Any]:
    """Initialize Claude in an unclaimed provider slot without model input.

    Every value fixed here is slot identity, never Session identity: the
    option bag is built with ``prepared_slot_id`` so the Session-bound
    branches (transcript capability token, model correlation headers, the
    tracing conversation attribute, Session MCP URLs) stay off, and activation
    supplies the platform Session over the wire. The spawn fingerprint over
    the serialized wire options is the slot's compatibility receipt: a claim
    whose configuration would serialize differently must discard the slot.
    """

    template = context.template
    runtime_identity = context.runtime_identity
    target_slot = str(context.slot_id or "").strip()
    target_agent = str(getattr(template, "agent_id", "") or "").strip()
    fingerprint = str(context.preparation_fingerprint or "").strip()
    if not target_slot or not target_agent or not fingerprint:
        raise APIError(
            code="AGENT_PREWARM_CONFIG_INVALID",
            message="prepared Claude slot requires slot, Agent, and fingerprint",
            status_code=500,
        )
    model_config = ModelConfig(
        **build_claude_model_config_kwargs(context.model_access)
    )
    model_runtime_creds = _model_runtime_creds(model_config, vault=False)
    model_runtime_creds["api_key"] = context.model_credential

    if str(model_runtime_creds.get("endpoint_provider") or "").strip():
        # The provider's per-conversation correlation headers cannot be fixed
        # into an unclaimed child; the option builder omits them without a
        # Session id. Until the gateway binds slot→Session at claim, model
        # calls from a prepared slot are not grouped per conversation there.
        logger.info(
            "prepared slot %s omits per-conversation model headers "
            "(endpoint_provider=%s); gateway-side correlation binds at claim",
            target_slot,
            model_runtime_creds.get("endpoint_provider"),
        )

    options = _build_claude_options(
        context.deployment_settings,
        template,
        cwd=str(runtime_identity.get("workspace_dir") or "").strip(),
        prepared_slot_id=target_slot,
        resume=None,
        permission_mode="bypassPermissions",
        runtime_identity=runtime_identity,
        capability_scope="conversation",
        model_runtime_creds=model_runtime_creds,
        runtime_env=context.runtime_env,
    )
    system_prompt = getattr(options, "system_prompt", None)
    if system_prompt is None:
        system_prompt = {"type": "preset", "preset": "claude_code"}
    if not isinstance(system_prompt, dict):
        raise APIError(
            code="AGENT_PREWARM_UNSUPPORTED",
            message=(
                "prepared Claude slots require the vendor's preset system "
                "prompt so dynamic Session sections can move to first input"
            ),
            status_code=409,
        )
    options.system_prompt = {
        **system_prompt,
        "exclude_dynamic_sections": True,
    }

    wire_options = _runner_configure_options(options)
    spawn_fingerprint = hashlib.sha256(
        json.dumps(
            wire_options,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    from astrabox.core.service.orchestrator.engine.runner_link import RunnerLink

    link = RunnerLink(str(context.runner_uri or ""))
    await link.__aenter__()
    try:
        await link.prepare(
            target_slot,
            activation_token=context.activation_token,
            options=wire_options,
        )
    finally:
        await link.close()
    return {
        "engine_kind": "claude_code",
        "spawn_fingerprint": spawn_fingerprint,
        "activation_mcp_servers": _activation_mcp_server_names(template),
    }


#: The SDK messages that can begin a root assistant response and therefore be
#: the runner-declared boundary of an engine-owned one.
_ENGINE_BOUNDARY_MESSAGE_TYPES = frozenset({"StreamEvent", "AssistantMessage"})


def _runner_event_persister(
    session_id: str,
    *,
    event_sink: EngineEventSink,
    consumer_carrier: str | None = None,
) -> Callable[[dict[str, Any]], Awaitable[None]]:
    """Persist resident SDK messages and FIFO consumption before cursor advance.

    The runner link is session-resident and keeps receiving after a turn's
    Result. Its persistence callback commits the external-input consumption
    boundary, task notices and hook envelopes, including before the first turn.
    Interpreting their user-facing meaning remains the Claude adapter's job
    when a read projection asks for it through the engine seam.
    ``consumer_carrier`` signs those consumption receipts with the runtime
    generation this link is bound to, so a receipt orphaned by that
    generation's death reopens its FIFO row for redelivery.
    """

    normalized_session_id = str(session_id or "").strip()
    if not normalized_session_id:
        raise ValueError("session_id is required for runner event persistence")

    async def persist(frame: dict[str, Any]) -> None:
        from astrabox.core.service.orchestrator.engine.frame_translator import (
            CLAUDE_SDK_TASK_MESSAGE_TYPES,
        )

        if str(frame.get("session_id") or "").strip() != normalized_session_id:
            raise ValueError("runner durable event belongs to another session")
        sequence = frame.get("seq")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 1
        ):
            raise ValueError("runner durable event requires a positive seq")
        message_type = str(frame.get("message_type") or "").strip()
        engine_boundary = frame.get("engine_boundary") is True
        if engine_boundary:
            # The runner's declaration that a root assistant response began
            # with nothing platform-owned in flight: the first root
            # `message_start`, or the complete AssistantMessage without
            # partial events. It is the durable boundary the resident
            # observer opens the engine-owned response at, and it is
            # committed before the runner may compact it.
            if message_type not in _ENGINE_BOUNDARY_MESSAGE_TYPES:
                raise ValueError(
                    "runner marked an unsupported SDK message as an engine-owned "
                    f"response boundary: {message_type!r}"
                )
        elif (
            message_type not in CLAUDE_SDK_TASK_MESSAGE_TYPES
            and message_type not in {"UserMessage", "HookEventMessage"}
        ):
            raise ValueError(
                f"runner marked an unsupported SDK message durable: {message_type!r}"
            )
        message = frame.get("message")
        if not isinstance(message, dict):
            raise ValueError("runner durable event has no SDK message object")
        if str(message.get("__sdk_type") or "").strip() != message_type:
            raise ValueError("runner durable event SDK type disagrees with its envelope")
        if engine_boundary and message.get("parent_tool_use_id") is not None:
            raise ValueError("runner engine-owned response boundary is not a root message")

        if message_type == "UserMessage":
            input_id = str(message.get("uuid") or "").strip()
            content = message.get("content")
            if (
                message.get("parent_tool_use_id") is not None
                or not input_id
                or not isinstance(content, str)
            ):
                raise ValueError(
                    "runner durable UserMessage is not an SDK FIFO root boundary"
                )
            await event_sink.confirm_input_consumed(
                input_id=input_id,
                content=content,
                consumer_carrier=consumer_carrier,
            )
            return

        event_uuid = str(message.get("uuid") or "").strip()
        if not event_uuid:
            canonical_message = json.dumps(
                message,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            event_uuid = hashlib.sha256(canonical_message.encode("utf-8")).hexdigest()
        persisted = await event_sink.persist_event(
            engine_kind="claude_code",
            causation_id=f"claude_code:{message_type}:{event_uuid}",
            payload={
                "runner_sequence": sequence,
                "message": dict(message),
                **({"engine_boundary": True} if engine_boundary else {}),
            },
        )
        persisted_payload = persisted.get("payload")
        if (
            not isinstance(persisted_payload, dict)
            or persisted_payload.get("engine_kind") != "claude_code"
            or persisted_payload.get("message") != message
        ):
            raise RuntimeError(
                "runner SDK event identity collided with different durable content"
            )

    return persist


async def _connect_runner_engine_client(
    runner_uri: str,
    *,
    session_id: str,
    workspace_dir: str,
    sdk_options: dict[str, Any],
    transcript_store: dict[str, Any] | None,
    sandbox_death_notice: dict[str, Any] | None,
    consumer_carrier: str | None = None,
    event_sink: EngineEventSink | None = None,
    resident_output_sink: ResidentOutputSink | None = None,
) -> Any:
    """Connect the host to a just-launched in-box runner and hand back its
    engine client. Module-level seam (patched by the hosted-flow
    characterization suite the way every other flow seam is).

    ``configure.store`` is required: a box without durable transcript flush is
    a misconfiguration, refused loudly, not a degraded mode.

    The transcript and death-notice targets are opaque platform-minted
    capabilities. Claude sends them over its vendor runner protocol but never
    derives, widens, or signs either one.
    """
    # Deferred import: the engine package's adapter registration imports this
    # module at load; importing the client chain at module level closes a
    # cycle through engine.base.
    from astrabox.core.service.orchestrator.engine.claude_code_client import (
        ClaudeCodeEngineClient,
    )
    from astrabox.core.service.orchestrator.engine.runner_link import RunnerLink

    store_base_url = str((transcript_store or {}).get("base_url") or "").strip()
    if not store_base_url:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=(
                "the platform supplied Claude no transcript store — a box "
                "without durable transcript flush is a misconfiguration, not "
                "a degraded mode"
            ),
            status_code=500,
        )
    death_notice_url = str(
        (sandbox_death_notice or {}).get("url") or ""
    ).strip()
    if not death_notice_url:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message="the platform supplied Claude no sandbox death notice",
            status_code=500,
        )
    if event_sink is None:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message="the platform supplied Claude no durable engine event sink",
            status_code=500,
        )
    link = RunnerLink(
        runner_uri,
        persistent_event_handler=_runner_event_persister(
            session_id,
            event_sink=event_sink,
            consumer_carrier=consumer_carrier,
        ),
    )
    await link.__aenter__()
    try:
        await link.configure(
            session_id,
            options=sdk_options,
            store={"base_url": store_base_url},
            death_notice={"url": death_notice_url},
        )
    except BaseException:
        with contextlib.suppress(Exception):
            await link.close()
        raise
    return ClaudeCodeEngineClient(
        link,
        session_id=session_id,
        workspace_dir=workspace_dir,
        transcript_store=cast(SessionStore, TranscriptSessionStore(platform_session_id=session_id)),
        resume_session_key=str(sdk_options.get("resume") or "").strip() or None,
        resident_output_sink=resident_output_sink,
    )


async def _activate_runner_engine_client(
    runner_uri: str,
    *,
    slot_id: str,
    activation_token: str,
    session_id: str,
    workspace_dir: str,
    permission_mode: str | None,
    resume_session_key: str | None,
    activation_mcp_servers: list[str],
    transcript_store: dict[str, Any] | None,
    sandbox_death_notice: dict[str, Any] | None,
    consumer_carrier: str | None = None,
    event_sink: EngineEventSink | None = None,
    resident_output_sink: ResidentOutputSink | None = None,
) -> Any:
    """Claim a prepared slot's runner for one platform Session.

    The activation frame carries what preparation could not know: the Session
    id, its transcript store target, the box death notice, the Session's
    permission mode, and the user-bound MCP servers whose reconnect must prove
    ``connected`` before input is released. Store and notice are built the
    same way ``_connect_runner_engine_client`` builds them; the difference is
    that engine configuration was fixed at prepare and fingerprinted. Resume
    names the old native Session and reconnects its SDK process in this box.
    """

    from astrabox.core.service.orchestrator.engine.claude_code_client import (
        ClaudeCodeEngineClient,
    )
    from astrabox.core.service.orchestrator.engine.runner_link import RunnerLink

    store_base_url = str((transcript_store or {}).get("base_url") or "").strip()
    if not store_base_url:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=(
                "the platform supplied Claude no transcript store — a box "
                "without durable transcript flush is a misconfiguration, not "
                "a degraded mode"
            ),
            status_code=500,
        )
    death_notice_url = str(
        (sandbox_death_notice or {}).get("url") or ""
    ).strip()
    if not death_notice_url:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message="the platform supplied Claude no sandbox death notice",
            status_code=500,
        )
    if event_sink is None:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message="the platform supplied Claude no durable engine event sink",
            status_code=500,
        )
    link = RunnerLink(
        runner_uri,
        persistent_event_handler=_runner_event_persister(
            session_id,
            event_sink=event_sink,
            consumer_carrier=consumer_carrier,
        ),
    )
    await link.__aenter__()
    try:
        await link.activate(
            slot_id,
            session_id,
            activation_token=activation_token,
            permission_mode=normalize_claude_permission_mode(permission_mode),
            resume_session_key=resume_session_key,
            mcp_servers=activation_mcp_servers,
            store={"base_url": store_base_url},
            death_notice={"url": death_notice_url},
        )
    except BaseException:
        with contextlib.suppress(Exception):
            await link.close()
        raise
    return ClaudeCodeEngineClient(
        link,
        session_id=session_id,
        workspace_dir=workspace_dir,
        transcript_store=cast(SessionStore, TranscriptSessionStore(platform_session_id=session_id)),
        resume_session_key=resume_session_key,
        resident_output_sink=resident_output_sink,
    )


async def _attach_runner_engine_client(
    runner_uri: str,
    *,
    session_id: str,
    workspace_dir: str,
    sdk_options: dict[str, Any],
    transcript_store: dict[str, Any] | None,
    sandbox_death_notice: dict[str, Any] | None,
    consumer_carrier: str | None = None,
    event_sink: EngineEventSink | None = None,
    resident_output_sink: ResidentOutputSink | None = None,
) -> tuple[Any, str]:
    """Attach to the box's live runner, or configure a relaunched one.

    Two real states, one correct handling each: a runner that still holds
    this session accepts ``attach`` (frames dropped while detached are
    surfaced as a ``gap`` against the durable store); a fresh runner process
    (box restarted, runner died and was relaunched) refuses it, and the host
    configures it with ``resume`` — the SDK restores the conversation from
    its own session file. A runner holding a different session refuses both
    opens loudly; that box is mis-assigned and must not be adopted.
    Module-level seam, patched by the flow characterization suite. Returns
    ``(engine_client, "attached" | "configured")``.
    """
    from astrabox.core.service.orchestrator.engine.claude_code_client import (
        ClaudeCodeEngineClient,
    )
    from astrabox.core.service.orchestrator.engine.runner_link import RunnerLink

    if event_sink is None:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message="the platform supplied Claude no durable engine event sink",
            status_code=500,
        )
    link = RunnerLink(
        runner_uri,
        persistent_event_handler=_runner_event_persister(
            session_id,
            event_sink=event_sink,
            consumer_carrier=consumer_carrier,
        ),
    )
    await link.__aenter__()
    try:
        await link.attach(
            session_id,
            last_seen_seq=0,
            required_option_keys=sdk_options.keys(),
            permission_mode=str(sdk_options.get("permission_mode") or "default"),
        )
        return (
            ClaudeCodeEngineClient(
                link,
                session_id=session_id,
                workspace_dir=workspace_dir,
                transcript_store=cast(
                    SessionStore, TranscriptSessionStore(platform_session_id=session_id)
                ),
                resume_session_key=(
                    str(sdk_options.get("resume") or "").strip() or None
                ),
                resident_output_sink=resident_output_sink,
            ),
            "attached",
        )
    except Exception as attach_exc:
        with contextlib.suppress(Exception):
            await link.close()
        logger.info(
            "runner attach refused, configuring with resume: session=%s err=%s",
            session_id,
            attach_exc,
        )
    client = await _connect_runner_engine_client(
        runner_uri,
        session_id=session_id,
        workspace_dir=workspace_dir,
        sdk_options=sdk_options,
        transcript_store=transcript_store,
        sandbox_death_notice=sandbox_death_notice,
        consumer_carrier=consumer_carrier,
        event_sink=event_sink,
        resident_output_sink=resident_output_sink,
    )
    return client, "configured"


__all__ = ["activate_runtime", "prepare_runtime", "sandbox_request"]
