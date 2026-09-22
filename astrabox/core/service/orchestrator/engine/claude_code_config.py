"""Claude Code launch configuration owned by the Claude adapter.

The platform resolves engine-neutral deployment, model, workspace and secret
facts. This module is the only host-side place that turns those facts into
``ClaudeAgentOptions`` fields and ``ANTHROPIC_*`` environment variables.
"""

from __future__ import annotations

import json
from urllib.parse import quote

import posixpath
from dataclasses import dataclass
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.core.model import AgentView
from astrabox.core.service.orchestrator.engine.claude_code_options import (
    CLAUDE_ENGINE_OPTIONS_SCHEMA,
    CLAUDE_PLATFORM_OPTION_KEYS,
    CLAUDE_RUNNER_CONTROLLED_KEYS,
)
from astrabox.core.service.orchestrator.runtime.config_resolver import is_local_mode
from astrabox.core.service.orchestrator.runtime.conversation_identity import (
    normalize_runtime_identity,
)
from astrabox.core.service.orchestrator.runtime.mcp_servers import (
    prepared_slot_mcp_deployment_id,
    runtime_mcp_servers_for_binding,
    validate_direct_mcp_servers,
)
from astrabox.core.service.orchestrator.runtime.plugin_repos import (
    build_plugin_repo_plugin_options,
    get_template_plugin_repos,
    merge_plugin_options,
)
from astrabox.core.service.orchestrator.schema_validation import (
    validate_declared_config_bag,
)
from astrabox.seams.model import (
    ModelEndpoint,
    ModelRequestContext,
    ResolvedModelAccess,
    model_endpoint_for_name,
)

logger = get_logger(__name__)

CLAUDE_API_KEY_ENV = "ANTHROPIC_API_KEY"
CLAUDE_AUTH_TOKEN_ENV = "ANTHROPIC_AUTH_TOKEN"


@dataclass
class ModelConfig:
    """Claude executor model endpoint and credential environment contract."""

    api_key: str = ""
    base_url: str = ""
    model_name: str = ""
    credential_header: str = CLAUDE_AUTH_TOKEN_ENV
    endpoint_provider: str = ""


def build_claude_model_config_kwargs(
    access: ResolvedModelAccess,
) -> dict[str, Any]:
    """Map engine-neutral model access onto the Claude executor contract."""

    return {
        "api_key": str(access.credential or ""),
        "base_url": normalize_claude_model_base_url(access.base_url or ""),
        "model_name": str(access.model_name or "").strip() or None,
        "credential_header": (
            CLAUDE_API_KEY_ENV
            if access.credential_kind == "api_key"
            else CLAUDE_AUTH_TOKEN_ENV
        ),
        "endpoint_provider": access.endpoint_provider,
    }


def normalize_claude_model_base_url(raw_value: str) -> str:
    """Normalize an Anthropic Messages endpoint to the Claude SDK API root."""

    value = str(raw_value or "").strip().rstrip("/")
    if not value:
        return ""
    lowered = value.lower()
    for suffix in ("/v1/messages", "/messages"):
        if lowered.endswith(suffix):
            normalized = value[: -len(suffix)].rstrip("/")
            if normalized:
                logger.warning(
                    "normalize Claude model base_url from endpoint to api root: "
                    "%s -> %s",
                    value,
                    normalized,
                )
                return normalized
    return value


def apply_claude_engine_options(
    options_kwargs: dict[str, Any],
    template: Any,
) -> None:
    """Overlay native SDK JSON without enumerating supplier-owned fields."""

    raw = getattr(template, "engine_options", None)
    if raw is None:
        return
    if not isinstance(raw, dict):
        raise APIError(
            code="CLAUDE_OPTIONS_INVALID",
            message="stored engine_options must be an object",
            status_code=500,
        )
    try:
        validate_declared_config_bag(
            raw,
            CLAUDE_ENGINE_OPTIONS_SCHEMA,
            bag_label="engine_options",
            owner_label="engine 'claude_code'",
        )
    except APIError as exc:
        raise APIError(
            code="CLAUDE_OPTIONS_INVALID",
            message=(
                "stored engine_options violate the Claude adapter contract: "
                f"{exc.message}"
            ),
            status_code=500,
        ) from exc

    native = raw.get("sdk_options", {})
    if not isinstance(native, dict):
        raise APIError(
            code="CLAUDE_OPTIONS_INVALID",
            message="engine_options.sdk_options must be a JSON object",
            status_code=400,
        )
    controlled = set(native) & (
        CLAUDE_PLATFORM_OPTION_KEYS | CLAUDE_RUNNER_CONTROLLED_KEYS
    )
    if controlled:
        raise APIError(
            code="CLAUDE_OPTIONS_INVALID",
            message=f"sdk_options contains platform-managed fields: {sorted(controlled)}",
            status_code=400,
        )
    options_kwargs.update(native)
    if "settings" in native:
        if not isinstance(native["settings"], dict):
            raise APIError(
                code="CLAUDE_OPTIONS_INVALID",
                message="sdk_options.settings must be a JSON object, not a file path",
                status_code=400,
            )
        options_kwargs["settings"] = json.dumps(native["settings"], allow_nan=False)


def _reject_native_overrides(
    configured: Any, managed: set[str], *, node: str
) -> None:
    """Refuse JSON values that platform wiring would otherwise overwrite."""
    if configured is None:
        return
    if not isinstance(configured, dict):
        raise APIError(
            code="CLAUDE_OPTIONS_INVALID",
            message=f"sdk_options.{node} must be a JSON object",
            status_code=400,
        )
    conflicts = set(configured) & managed
    if conflicts:
        raise APIError(
            code="CLAUDE_OPTIONS_INVALID",
            message=f"sdk_options.{node} contains platform-managed keys: {sorted(conflicts)}",
            status_code=400,
        )



#: How often the CLI flushes each signal, in milliseconds. The vendor's defaults
#: are 60s for metrics and 5s for traces and logs, chosen for a workstation.
#: These boxes are disposable — the vendor's own warning is that "if your process
#: is killed before the CLI shuts down, anything still in the batch buffer is
#: lost" — so a minute of buffered metrics is a minute of telemetry that dies
#: with the sandbox. Same reasoning as the transcript mirror's eager flush: the
#: window that matters is how long data sits inside a box that can vanish.
_OTEL_FLUSH_INTERVAL_MS = "1000"


def _claude_tracing_env(spec: Any, *, session_id: str | None = None) -> dict[str, str]:
    """Translate one platform tracing spec into the vendor's own switches.

    The platform carries OpenTelemetry's words (endpoint, headers, environment);
    what turns them on is Claude Code's, and it is not one switch but several:
    telemetry is off until ``CLAUDE_CODE_ENABLE_TELEMETRY``, each signal has its
    own exporter variable, and traces additionally need the beta flag. Naming
    all of them here — rather than assuming the endpoint is enough — is the
    difference between a configuration that works and one that stores cleanly
    and emits nothing.

    ``CLAUDE_CODE_OTEL_DIAG_STDERR`` is set whenever tracing is: the CLI drops
    export failures silently by default, which makes "the collector rejected
    this" indistinguishable from "tracing was never on". An operator who
    switched it on has asked to know.
    """

    env: dict[str, str] = {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        # Traces are beta and gated separately from the master switch.
        "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_ENDPOINT": spec.endpoint,
        "OTEL_METRIC_EXPORT_INTERVAL": _OTEL_FLUSH_INTERVAL_MS,
        "OTEL_LOGS_EXPORT_INTERVAL": _OTEL_FLUSH_INTERVAL_MS,
        "OTEL_TRACES_EXPORT_INTERVAL": _OTEL_FLUSH_INTERVAL_MS,
        "CLAUDE_CODE_OTEL_DIAG_STDERR": "1",
    }
    # One exporter switch per signal the Environment asked for, and `none` for
    # the rest. Never `console`: that exporter writes to stdout, which is the
    # channel the runner speaks to the CLI over.
    for signal, var in (
        ("traces", "OTEL_TRACES_EXPORTER"),
        ("metrics", "OTEL_METRICS_EXPORTER"),
        ("logs", "OTEL_LOGS_EXPORTER"),
    ):
        env[var] = "otlp" if signal in spec.signals else "none"

    headers = dict(spec.headers)
    if spec.auth_token:
        # Verbatim, with no scheme invented for it. The scheme belongs to the
        # collector, not the platform: Langfuse authenticates with
        # ``Basic base64(public:secret)``, other backends with ``Bearer``, and
        # some with no ``Authorization`` header at all (Honeycomb reads
        # ``x-honeycomb-team``, which is what the `headers` map is for).
        # Prefixing ``Bearer `` produced ``Bearer Basic <token>`` and Langfuse
        # answered "Invalid public key" — a credential error that says nothing
        # about the word this code added in front of it.
        headers.setdefault("Authorization", spec.auth_token)
    if headers:
        env["OTEL_EXPORTER_OTLP_HEADERS"] = ",".join(
            f"{name}={value}" for name, value in headers.items()
        )
    # Resource attributes are one `k=v,k=v` string, so a value carrying a comma,
    # an equals sign or a space would split the list rather than fail — the
    # vendor's reference says to percent-encode them, and `environment` is
    # operator-supplied text.
    attributes: list[tuple[str, str]] = []
    if spec.environment:
        attributes.append(("deployment.environment", spec.environment))
    if session_id:
        # Namespaced, and deliberately NOT `session.id`: the CLI already puts its
        # own session on every span, which is what a backend groups by. This is
        # the other identity — the AstraBox conversation the turn belongs to —
        # and a trace with no way back to it is a trace nobody can act on.
        attributes.append(("astrabox.conversation.id", session_id))
    if attributes:
        env["OTEL_RESOURCE_ATTRIBUTES"] = ",".join(
            f"{name}={quote(value, safe='')}" for name, value in attributes
        )
    if spec.log_user_prompt:
        # Opt-in, and only this one: the vendor has three other content
        # switches (tool details, tool bodies, raw API bodies) and an
        # environment that asked to see prompts did not ask for those.
        env["OTEL_LOG_USER_PROMPTS"] = "1"
    return env

def build_claude_options_kwargs(
    settings: Any,
    template: AgentView,
    *,
    cwd: str | None = None,
    session_id: str | None = None,
    resume: str | None = None,
    permission_mode: str,
    runtime_identity: dict[str, Any] | None = None,
    capability_scope: str = "conversation",
    model_runtime_creds: dict[str, str] | None = None,
    runtime_env: dict[str, str] | None = None,
    prepared_slot_id: str | None = None,
) -> dict[str, Any]:
    """Compose one Claude SDK option bag from platform-resolved facts.

    ``session_id`` and ``prepared_slot_id`` are mutually exclusive identities.
    A Session id activates every Session-bound branch: the platform MCP URLs,
    the transcript capability token, model correlation headers and the tracing
    conversation attribute. A prepared slot has no Session yet, so it names
    only what a slot legitimately owns — private plugin paths, the debug file,
    and a slot-addressed MCP deployment id the platform binds at claim — and
    every Session-bound branch stays off until activation supplies the real
    identity over the wire.
    """

    slot_scope = str(prepared_slot_id or "").strip()
    if slot_scope and str(session_id or "").strip():
        raise APIError(
            code="AGENT_PREWARM_CONFIG_INVALID",
            message=(
                "a Claude option bag is built for a Session or for a prepared "
                "slot, never both"
            ),
            status_code=500,
        )
    effective_permission_mode = str(permission_mode or "").strip()
    if not effective_permission_mode:
        raise APIError(
            code="CLAUDE_OPTIONS_INVALID",
            message="claude permission_mode must be resolved by the engine",
            status_code=500,
        )
    configured_buffer_size = settings.remote_agent_max_buffer_size
    if (
        isinstance(configured_buffer_size, bool)
        or not isinstance(configured_buffer_size, int)
        or configured_buffer_size <= 0
    ):
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message="astrabox.remote_agent.max_buffer_size must be a positive integer",
            status_code=500,
        )
    options_kwargs: dict[str, Any] = {
        "tools": {"type": "preset", "preset": "claude_code"},
        "setting_sources": ["project", "user"],
        "permission_mode": effective_permission_mode,
        "include_partial_messages": settings.remote_agent_include_partial_messages,
        "max_buffer_size": configured_buffer_size,
    }
    if cwd:
        options_kwargs["cwd"] = cwd
    if resume:
        options_kwargs["resume"] = resume

    apply_claude_engine_options(options_kwargs, template)
    system_instructions = str(getattr(template, "system", None) or "").strip()
    if system_instructions:
        options_kwargs["system_prompt"] = {
            "type": "preset",
            "preset": "claude_code",
            "append": system_instructions,
        }
    max_buffer_size = options_kwargs.get("max_buffer_size")
    if (
        isinstance(max_buffer_size, bool)
        or not isinstance(max_buffer_size, int)
        or max_buffer_size <= 0
    ):
        raise APIError(
            code="CLAUDE_OPTIONS_INVALID",
            message="template.engine_options.sdk_options.max_buffer_size must be a positive integer",
            status_code=500,
        )
    identity = normalize_runtime_identity(runtime_identity)
    if runtime_identity is not None and identity is None:
        raise APIError(
            code="CONVERSATION_IDENTITY_REQUIRED",
            message="runtime_identity is invalid; refusing to build root Claude options",
            status_code=409,
        )

    include_template_capabilities = (
        str(capability_scope or "conversation").strip() != "agent_runtime"
    )
    if not include_template_capabilities:
        options_kwargs.pop("plugins", None)
        options_kwargs.pop("mcp_servers", None)
    plugin_repos = (
        get_template_plugin_repos(template) if include_template_capabilities else []
    )
    generated_plugins: list[dict[str, str]] = []
    if plugin_repos:
        capability_plan = (identity or {}).get("capability_plan")
        cached_plugins = (
            capability_plan.get("claude_plugin_options")
            if isinstance(capability_plan, dict)
            else None
        )
        if isinstance(cached_plugins, list) and cached_plugins:
            generated_plugins = [
                {"type": "local", "path": str(item.get("path") or "").strip()}
                for item in cached_plugins
                if isinstance(item, dict) and str(item.get("path") or "").strip()
            ]
        else:
            plugin_scope = str(session_id or "").strip() or slot_scope
            if not plugin_scope:
                raise APIError(
                    code="PLUGIN_REPO_INVALID",
                    message=(
                        "a session or prepared-slot id is required to resolve "
                        "template.plugin_repos"
                    ),
                    status_code=500,
                )
            plugin_base_dir = (
                f"{identity['config_dir'].rstrip('/')}/plugins" if identity else None
            )
            generated_plugins = build_plugin_repo_plugin_options(
                plugin_scope,
                plugin_repos,
                base_dir=plugin_base_dir,
            )
    if generated_plugins or options_kwargs.get("plugins") is not None:
        options_kwargs["plugins"] = merge_plugin_options(
            options_kwargs.get("plugins"),
            generated_plugins,
            template_name=str(getattr(template, "name", "") or "?"),
        )

    raw_extra_args = options_kwargs.get("extra_args")
    _reject_native_overrides(
        raw_extra_args,
        {"replay-user-messages"} | ({"debug-file"} if identity else set()),
        node="extra_args",
    )
    extra_args = dict(raw_extra_args) if isinstance(raw_extra_args, dict) else {}
    extra_args.setdefault("allow-dangerously-skip-permissions", None)
    extra_args["replay-user-messages"] = None
    if identity:
        extra_args.pop("debug-file", None)
    debug_scope = str(session_id or "").strip() or slot_scope
    if debug_scope and is_local_mode():
        debug_root = str((identity or {}).get("config_dir") or "/root/.claude").rstrip(
            "/"
        )
        debug_file = f"{debug_root}/debug/astrabox-{debug_scope}.txt"
        if identity:
            extra_args["debug-file"] = debug_file
        else:
            extra_args.setdefault("debug-file", debug_file)
    if extra_args:
        options_kwargs["extra_args"] = extra_args

    if include_template_capabilities and template.mcp_servers:
        proxy_base = settings.mcp_proxy_base_url.rstrip("/")
        mcp_deployment_id = str(session_id or "").strip() or (
            prepared_slot_mcp_deployment_id(slot_scope) if slot_scope else ""
        )
        if proxy_base and mcp_deployment_id:
            rewritten = runtime_mcp_servers_for_binding(
                template.mcp_servers,
                proxy_base_url=proxy_base,
                deployment_id=mcp_deployment_id,
            )
            options_kwargs["mcp_servers"] = rewritten
            logger.info(
                "MCP servers resolved for runtime: scope=%s servers=%s",
                str(session_id or "").strip() or f"slot:{slot_scope}",
                sorted(rewritten.keys()),
            )
        else:
            options_kwargs["mcp_servers"] = validate_direct_mcp_servers(
                template.mcp_servers
            )

    if identity:
        options_kwargs["cwd"] = identity["workspace_dir"]
        env = options_kwargs.get("env")
        _reject_native_overrides(
            env,
            {"HOME", "PWD", "USER", "LOGNAME", "PATH", "NPM_CONFIG_PREFIX", "CLAUDE_CONFIG_DIR"},
            node="env",
        )
        merged_env = dict(env) if isinstance(env, dict) else {}
        default_path = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        home_local = f"{identity['home_dir'].rstrip('/')}/.local"
        identity_bin = f"{home_local}/bin"
        merged_env.update(
            {
                "HOME": identity["home_dir"],
                "PWD": identity["workspace_dir"],
                "USER": identity["linux_user"],
                "LOGNAME": identity["linux_user"],
                "PATH": f"{identity_bin}:{default_path}",
                "NPM_CONFIG_PREFIX": home_local,
                "CLAUDE_CONFIG_DIR": identity["config_dir"],
            }
        )
        options_kwargs["env"] = merged_env
        _validate_identity_claude_options(options_kwargs, identity)

    if model_runtime_creds:
        env = options_kwargs.get("env")
        merged_env = dict(env) if isinstance(env, dict) else {}
        model_name = str(model_runtime_creds.get("model_name") or "").strip()
        api_key = str(model_runtime_creds.get("api_key") or "").strip()
        base_url = str(model_runtime_creds.get("base_url") or "").strip()
        credential_header = (
            str(model_runtime_creds.get("credential_header") or "").strip()
            or CLAUDE_AUTH_TOKEN_ENV
        )
        _reject_native_overrides(
            env,
            ({"ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL"} if model_name else set())
            | ({"ANTHROPIC_BASE_URL"} if base_url else set())
            | ({credential_header} if api_key else set()),
            node="env",
        )
        if model_name:
            merged_env["ANTHROPIC_MODEL"] = model_name
            merged_env["ANTHROPIC_SMALL_FAST_MODEL"] = model_name
        if base_url:
            merged_env["ANTHROPIC_BASE_URL"] = base_url
        if api_key and (base_url or credential_header == CLAUDE_API_KEY_ENV):
            merged_env[credential_header] = api_key
        elif api_key:
            logger.error(
                "withholding the model credential: %s is a gateway Bearer token "
                "and this model config resolved no base_url, so there is no "
                "gateway to send it to. Set the environment's model endpoint, "
                "or configure a vendor key under %s.",
                credential_header,
                CLAUDE_API_KEY_ENV,
            )
        endpoint_provider = str(
            model_runtime_creds.get("endpoint_provider") or ""
        ).strip()
        if endpoint_provider and session_id and base_url:
            provider = model_endpoint_for_name(endpoint_provider)
            request_headers = provider.request_headers(
                endpoint=ModelEndpoint(
                    base_url=base_url,
                    model_name=model_name or None,
                ),
                context=ModelRequestContext(
                    conversation_id=session_id,
                    user_id=str((identity or {}).get("user_id") or "").strip()
                    or None,
                ),
            )
            rendered_headers = ", ".join(
                f"{str(name).strip()}: {str(value).strip()}"
                for name, value in request_headers.items()
                if str(name).strip() and str(value).strip()
            )
            existing_headers = str(
                merged_env.get("ANTHROPIC_CUSTOM_HEADERS") or ""
            ).strip()
            if rendered_headers:
                merged_env["ANTHROPIC_CUSTOM_HEADERS"] = (
                    f"{existing_headers}, {rendered_headers}"
                    if existing_headers
                    else rendered_headers
                )
        options_kwargs["env"] = merged_env

    # Tracing rides options.env for the same reason the model credential does:
    # it is a per-session fact, and a prewarmed box never receives a per-session
    # container env at all. Delivering it any other way would give cold and
    # pooled boxes two different paths to the same setting.
    from astrabox.seams.tracing import runtime_tracing_spec

    tracing = runtime_tracing_spec(template)
    if tracing is not None and tracing.active:
        env = options_kwargs.get("env")
        merged = dict(env) if isinstance(env, dict) else {}
        tracing_env = _claude_tracing_env(tracing, session_id=session_id)
        _reject_native_overrides(env, set(tracing_env), node="env")
        merged.update(tracing_env)
        options_kwargs["env"] = merged

    if runtime_env:
        env = options_kwargs.get("env")
        merged_env = dict(env) if isinstance(env, dict) else {}
        invalid_names = sorted(
            repr(name) for name in runtime_env if not str(name or "").strip()
        )
        if invalid_names:
            raise APIError(
                code="AGENT_RUNTIME_ENV_INVALID",
                message=(
                    "managed runtime environment variable names must be non-empty: "
                    + ", ".join(invalid_names)
                ),
                status_code=400,
            )
        conflicts = sorted(set(merged_env) & set(runtime_env))
        if conflicts:
            raise APIError(
                code="VAULT_CREDENTIAL_CONFLICT",
                message=(
                    "managed credential environment variables conflict with the "
                    "Agent runtime configuration: "
                    f"{', '.join(conflicts)}. Rename the bound credential's "
                    "environment variable."
                ),
                status_code=409,
            )
        merged_env.update(
            {str(name): str(value) for name, value in runtime_env.items()}
        )
        options_kwargs["env"] = merged_env

    return options_kwargs


def _validate_identity_claude_options(
    options_kwargs: dict[str, Any], identity: dict[str, Any]
) -> None:
    home_dir = str(identity.get("home_dir") or "").rstrip("/")
    workspace_dir = str(identity.get("workspace_dir") or "").rstrip("/")
    config_dir = str(identity.get("config_dir") or "").rstrip("/")
    plugin_root = f"{config_dir}/plugins"

    plugins = options_kwargs.get("plugins")
    if plugins is not None:
        if not isinstance(plugins, list):
            raise APIError(
                code="CLAUDE_OPTIONS_INVALID",
                message="identity-scoped engine_options.sdk_options.plugins must be a list",
                status_code=500,
            )
        for index, plugin in enumerate(plugins):
            if not isinstance(plugin, dict):
                raise APIError(
                    code="CLAUDE_OPTIONS_INVALID",
                    message=(
                        "identity-scoped engine_options.sdk_options.plugins"
                        f"[{index}] must be an object"
                    ),
                    status_code=500,
                )
            path = str(plugin.get("path") or "").strip()
            if not _is_path_under(path, plugin_root):
                raise APIError(
                    code="IDENTITY_BOUNDARY_INCOMPLETE",
                    message=(
                        "identity-scoped plugin path is outside config plugins dir: "
                        f"{path}"
                    ),
                    status_code=500,
                )

    add_dirs = options_kwargs.get("add_dirs")
    if add_dirs:
        values = add_dirs if isinstance(add_dirs, list) else [add_dirs]
        for index, raw_path in enumerate(values):
            path = str(raw_path or "").strip()
            if not path.startswith("/") or not _is_path_under(path, home_dir):
                raise APIError(
                    code="IDENTITY_BOUNDARY_INCOMPLETE",
                    message=(
                        f"identity-scoped add_dirs[{index}] must stay inside the "
                        f"conversation home: {path}"
                    ),
                    status_code=500,
                )

    extra_args = options_kwargs.get("extra_args")
    if isinstance(extra_args, dict):
        debug_file = str(extra_args.get("debug-file") or "").strip()
        if debug_file and not _is_path_under(debug_file, f"{config_dir}/debug"):
            raise APIError(
                code="IDENTITY_BOUNDARY_INCOMPLETE",
                message=(
                    "identity-scoped debug-file must stay inside config debug dir: "
                    f"{debug_file}"
                ),
                status_code=500,
            )
        for key, value in extra_args.items():
            if _contains_shared_root_path(value):
                raise APIError(
                    code="IDENTITY_BOUNDARY_INCOMPLETE",
                    message=(
                        f"identity-scoped extra_args.{key} contains a shared "
                        "root/global path"
                    ),
                    status_code=500,
                )

    env = options_kwargs.get("env")
    if not isinstance(env, dict):
        raise APIError(
            code="IDENTITY_BOUNDARY_INCOMPLETE",
            message="identity-scoped Claude options must include environment",
            status_code=500,
        )
    expected_env = {
        "HOME": home_dir,
        "PWD": workspace_dir,
        "USER": str(identity.get("linux_user") or ""),
        "LOGNAME": str(identity.get("linux_user") or ""),
        "CLAUDE_CONFIG_DIR": config_dir,
    }
    for key, expected in expected_env.items():
        actual = str(env.get(key) or "").rstrip("/")
        if actual != expected:
            raise APIError(
                code="IDENTITY_BOUNDARY_INCOMPLETE",
                message=f"identity-scoped env.{key}={actual!r}, expected {expected!r}",
                status_code=500,
            )
    expected_bin = f"{home_dir}/.local/bin"
    path = str(env.get("PATH") or "")
    if not (path == expected_bin or path.startswith(f"{expected_bin}:")):
        raise APIError(
            code="IDENTITY_BOUNDARY_INCOMPLETE",
            message="identity-scoped PATH must begin with the conversation local bin",
            status_code=500,
        )


def _is_path_under(path: str, root: str) -> bool:
    value = _safe_norm_path(path)
    base = _safe_norm_path(root)
    return bool(value and base and (value == base or value.startswith(f"{base}/")))


def _safe_norm_path(path: Any) -> str:
    raw = str(path or "").strip()
    if not raw.startswith("/") or any(part == ".." for part in raw.split("/")):
        return ""
    return posixpath.normpath(raw).rstrip("/")


def _contains_shared_root_path(value: Any) -> bool:
    if isinstance(value, str):
        return (
            value == "/root"
            or "/root/" in value
            or value.startswith("/usr/local/bin")
        )
    if isinstance(value, dict):
        return any(_contains_shared_root_path(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_contains_shared_root_path(item) for item in value)
    return False
