import os
import socket
from typing import Any, Literal

from pydantic import (
    AliasChoices,
    AliasPath,
    Field,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from astrabox.config.config import APP_YAML_PATH, AppYamlSource
from astrabox.config.config import config as _CONFIG
from astrabox.config.release_images import release_image

DEFAULT_REMOTE_AGENT_MAX_BUFFER_SIZE_BYTES = 32 * 1024 * 1024


def get_config_diagnostics() -> dict[str, Any]:
    return {
        "settings_module_file": __file__,
        "config_type": f"{type(_CONFIG).__module__}.{type(_CONFIG).__qualname__}",
        "config_env": getattr(_CONFIG, "env", None),
    }


def _safe_get(key: str, default: Any) -> Any:
    get_or_default = getattr(_CONFIG, "get_or_default", None)
    if callable(get_or_default):
        return get_or_default(key, default)

    get_value = getattr(_CONFIG, "get")
    try:
        value = get_value(key)
    except KeyError:
        return default
    return default if value is None else value


def _to_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "on"}:
            return True
        if text in {"false", "0", "no", "off"}:
            return False
    return bool(value)


def _running_in_container() -> bool:
    """True when this process is running inside a Docker/OCI container.

    ``/.dockerenv`` is Docker's marker file; ``/run/.containerenv`` is Podman's.
    """
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")


def _detect_own_container_ip() -> str:
    """This container's own bridge-network IP, or "" if it can't be determined.

    A *connected* UDP socket transmits nothing, but forces the kernel to resolve
    the source IP it would egress from; on the default Docker bridge that is this
    container's own 172.17.x.x address — the same address a sibling sandbox
    container reaches this container at. The ``8.8.8.8`` literal is an arbitrary
    off-box target used only to select a non-loopback route; no packet is sent.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        ip = str(sock.getsockname()[0] or "").strip()
    except OSError:
        return ""
    finally:
        sock.close()
    if not ip or ip.startswith("127.") or ip == "0.0.0.0":
        return ""
    return ip


def _default_mcp_proxy_base_url() -> str:
    """Derive the sandbox->server callback base URL for the containerized quickstart.

    When ``ASTRABOX_MCP_PROXY_BASE_URL`` is unset the lifecycle worker still builds
    a sandbox-callback URL and fails loud on an empty base, 500ing session creation
    in the README ``docker run`` topology. There the server runs in a container
    that publishes its port to the host loopback only (``-p 127.0.0.1:8088:8000``)
    and spawns sandbox containers on the same default bridge. A loopback-bound
    host port is unreachable from a sandbox (host-gateway routes to the host,
    which listens only on 127.0.0.1), so the reachable address is the server
    container's own bridge IP plus its internal listen port (``ASTRABOX_PORT``,
    default 8000). This derivation runs only inside a container; on the host the
    operator sets the variable explicitly (``scripts/dev.sh`` and the e2e scripts
    do), so returning "" preserves the fail-loud behavior there.
    """
    if not _running_in_container():
        return ""
    ip = _detect_own_container_ip()
    if not ip:
        return ""
    port = str(os.environ.get("ASTRABOX_PORT") or "8000").strip() or "8000"
    return f"http://{ip}:{port}"


class AstraBoxRuntimeSettings(BaseSettings):
    """The wide operational config surface, resolved by ``pydantic-settings``.

    Every field is sourced natively, highest precedence first:

        ASTRABOX_* (or the aliased) environment var  >  ``app.yml``  >  field default

    ``env_ignore_empty`` drops a blank env var *before* the merge, so an empty
    ``ASTRABOX_FOO=""`` never masks a YAML/default value. Each field whose env
    name differs from its nested ``app.yml`` key declares both via
    ``AliasChoices(<ENV_NAME>, AliasPath(<yaml.path>))``; a field with only an
    ``AliasPath`` is YAML-only and declares no environment-variable alias.
    """

    model_config = SettingsConfigDict(
        env_prefix="ASTRABOX_",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
    )

    @classmethod
    def settings_customise_sources(  # noqa: D102 - pydantic hook
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # env (ASTRABOX_*/aliased) then app.yml. Precedence is resolved per field
        # by each AliasChoices' order (env name(s) before the AliasPath), and the
        # empty-env trap by env_ignore_empty — so env > YAML > default.
        return (init_settings, env_settings, AppYamlSource(settings_cls))

    # --- sandbox platform --------------------------------------------------
    sandbox_openapi_base_url: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ASTRABOX_SANDBOX_OPENAPI_BASE_URL",
            AliasPath("astrabox", "sandbox_platform", "openapi_base_url"),
        ),
    )
    sandbox_request_timeout_seconds: int = Field(
        default=15,
        validation_alias=AliasChoices(
            "ASTRABOX_SANDBOX_REQUEST_TIMEOUT_SECONDS",
            AliasPath("astrabox", "sandbox_platform", "request_timeout_seconds"),
        ),
    )
    sandbox_ready_timeout_seconds: int = Field(
        default=120,
        validation_alias=AliasChoices(
            "ASTRABOX_SANDBOX_READY_TIMEOUT_SECONDS",
            AliasPath("astrabox", "sandbox_platform", "ready_timeout_seconds"),
        ),
    )
    # How long a runner keeps holding an answer slot after its host disconnects.
    # Every activation passes the configured value so reconnect behavior follows
    # the deployment's budget.
    runner_interaction_wait_seconds: int = Field(
        default=120,
        validation_alias=AliasChoices(
            "ASTRABOX_RUNNER_INTERACTION_WAIT_SECONDS",
            AliasPath("astrabox", "runtime", "interaction_wait_seconds"),
        ),
    )
    # Shared state for OpenSandbox's official client-side pool. Redis contains
    # only supplier-owned idle-capacity state; AstraBox product ownership stays
    # in the configured primary database.
    agent_prewarm_redis_url: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ASTRABOX_AGENT_PREWARM_REDIS_URL",
            AliasPath("astrabox", "agent_prewarm", "redis_url"),
        ),
    )
    #: Ask the lifecycle server to hand back endpoints that point at the server
    #: itself and relay to the sandbox, instead of endpoints that point at a
    #: published host port. See
    #: ``providers/open_sandbox/_config.sdk_connection_config`` for the
    #: reachability argument; the one-container deployment sets this.
    sandbox_endpoint_via_server_proxy: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "ASTRABOX_SANDBOX_ENDPOINT_VIA_SERVER_PROXY",
            AliasPath("astrabox", "sandbox_platform", "endpoint_via_server_proxy"),
        ),
    )
    #: Protect Kubernetes ingress endpoints with OpenSandbox Secure Access.
    #: Docker does not implement this capability; its execd proxy remains the
    #: native browser route when this is false.
    sandbox_secure_access_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "ASTRABOX_SANDBOX_SECURE_ACCESS",
            AliasPath("astrabox", "sandbox_platform", "secure_access"),
        ),
    )
    sandbox_endpoint_url_ttl_seconds: int = Field(
        default=900,
        ge=60,
        le=86400,
        validation_alias=AliasChoices(
            "ASTRABOX_SANDBOX_ENDPOINT_URL_TTL_SECONDS",
            AliasPath("astrabox", "sandbox_platform", "endpoint_url_ttl_seconds"),
        ),
    )
    #: OpenSandbox endpoint responses contain a host/path but no scheme. Most
    #: deployments use the lifecycle API's scheme; split-control/data-plane
    #: deployments can state the ingress gateway's scheme explicitly.
    sandbox_endpoint_scheme: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ASTRABOX_SANDBOX_ENDPOINT_SCHEME",
            AliasPath("astrabox", "sandbox_platform", "endpoint_scheme"),
        ),
    )
    sandbox_api_key_secret_name: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ASTRABOX_SANDBOX_API_KEY_SECRET_NAME",
            AliasPath("astrabox", "sandbox_platform", "sandbox_api_key_secret_name"),
        ),
    )
    # Git host HTTPS access-token secret name, used for cloning plugin/default
    # repos over https on backends whose egress cannot reach git over SSH/22
    # (the ``requires_https_git`` capability). SSH-capable backends authenticate
    # with the per-repo deploy key instead.
    git_https_token_secret_name: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ASTRABOX_GIT_HTTPS_TOKEN_SECRET_NAME",
            AliasPath("astrabox", "git", "https_token_secret_name"),
        ),
    )

    # --- mongodb -----------------------------------------------------------
    environment_collection: str = Field(
        default="environment",
        validation_alias=AliasChoices(AliasPath("astrabox", "mongodb", "environment_collection")),
    )
    deployments_collection: str = Field(
        default="deployments",
        validation_alias=AliasChoices(AliasPath("astrabox", "mongodb", "deployments_collection")),
    )
    sessions_collection: str = Field(
        default="sessions",
        validation_alias=AliasChoices(AliasPath("astrabox", "mongodb", "sessions_collection")),
    )
    messages_collection: str = Field(
        default="messages",
        validation_alias=AliasChoices(AliasPath("astrabox", "mongodb", "messages_collection")),
    )

    # --- top-level astrabox ------------------------------------------------
    session_ttl_seconds: int = Field(
        default=86400, validation_alias=AliasChoices(AliasPath("astrabox", "session_ttl_seconds"))
    )
    # --- model -------------------------------------------------------------
    # base_url / model_name / api_key bridge the ANTHROPIC_* (and ASTRABOX_LLM_*)
    # names claude-agent-sdk reads natively, so one env set drives both layers;
    # AliasChoices order matches AstraBoxSettings.llm_* (env wins over app.yml).
    model_base_url: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ANTHROPIC_BASE_URL",
            "ASTRABOX_LLM_BASE_URL",
            AliasPath("astrabox", "model", "base_url"),
        ),
    )
    model_name: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ANTHROPIC_MODEL",
            "ASTRABOX_LLM_MODEL",
            AliasPath("astrabox", "model", "model_name"),
        ),
    )
    model_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_API_KEY",
            "ASTRABOX_LLM_AUTH_TOKEN",
            AliasPath("astrabox", "model", "api_key"),
        ),
    )
    model_api_key_secret_name: str = Field(
        default="",
        validation_alias=AliasChoices(AliasPath("astrabox", "model", "api_key_secret_name")),
    )
    # Which ModelEndpointProvider decides the wire endpoint the sandbox uses.
    # Empty selects the provider that explicitly declares itself the default.
    model_endpoint_provider: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ASTRABOX_MODEL_ENDPOINT_PROVIDER", AliasPath("astrabox", "model", "endpoint_provider")
        ),
    )
    # Which ExtensionProvider supplies the catalog and runtime bindings.
    # Empty selects the provider that explicitly declares itself the default.
    extension_provider: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ASTRABOX_EXTENSION_PROVIDER",
            AliasPath("astrabox", "extensions", "provider"),
        ),
    )
    # Team deployments can require the sandbox-facing model gateway to use a
    # normal HTTPS origin. This setting is independent of Credential Vault: the
    # operator may still turn Vault delivery off, but a gateway key or model
    # request must not cross the sandbox-facing hop as plaintext.
    model_gateway_require_https: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "ASTRABOX_MODEL_GATEWAY_REQUIRE_HTTPS",
            AliasPath("astrabox", "model", "gateway_require_https"),
        ),
    )

    # --- title model -------------------------------------------------------
    title_model_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "ASTRABOX_TITLE_MODEL_ENABLED", AliasPath("astrabox", "title_model", "enabled")
        ),
    )
    title_model_base_url: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ASTRABOX_TITLE_MODEL_BASE_URL", AliasPath("astrabox", "title_model", "base_url")
        ),
    )
    title_model_name: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ASTRABOX_TITLE_MODEL_NAME", AliasPath("astrabox", "title_model", "model_name")
        ),
    )
    title_model_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ASTRABOX_TITLE_MODEL_API_KEY", AliasPath("astrabox", "title_model", "api_key")
        ),
    )
    title_model_api_key_secret_name: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ASTRABOX_TITLE_MODEL_API_KEY_SECRET_NAME",
            AliasPath("astrabox", "title_model", "api_key_secret_name"),
        ),
    )
    title_model_max_tokens: int = Field(
        default=256,
        validation_alias=AliasChoices(
            "ASTRABOX_TITLE_MODEL_MAX_TOKENS", AliasPath("astrabox", "title_model", "max_tokens")
        ),
    )
    title_model_request_timeout_seconds: float = Field(
        default=60.0,
        gt=0,
        allow_inf_nan=False,
        validation_alias=AliasChoices(
            "ASTRABOX_TITLE_MODEL_REQUEST_TIMEOUT_SECONDS",
            AliasPath("astrabox", "title_model", "request_timeout_seconds"),
        ),
    )

    # --- remote agent ------------------------------------------------------
    remote_agent_include_partial_messages: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            AliasPath("astrabox", "remote_agent", "include_partial_messages")
        ),
    )
    remote_agent_max_buffer_size: int = Field(
        default=DEFAULT_REMOTE_AGENT_MAX_BUFFER_SIZE_BYTES,
        validation_alias=AliasChoices(
            AliasPath("astrabox", "remote_agent", "max_buffer_size")
        ),
    )

    # --- sandbox-facing platform callback base ----------------------------
    # This is the sandbox-facing base for platform MCP and callback routes.
    # Empty derives the server's own bridge address inside a container.
    mcp_proxy_base_url: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ASTRABOX_MCP_PROXY_BASE_URL", AliasPath("astrabox", "mcp_proxy", "base_url")
        ),
    )
    # --- NAS ---------------------------------------------------------------
    nas_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "ASTRABOX_NAS_ENABLED", AliasPath("astrabox", "nas", "enabled")
        ),
    )
    nas_endpoint: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ASTRABOX_NAS_ENDPOINT", AliasPath("astrabox", "nas", "endpoint")
        ),
    )
    nas_base_path: str = Field(
        default="/astrabox",
        validation_alias=AliasChoices(
            "ASTRABOX_NAS_BASE_PATH", AliasPath("astrabox", "nas", "base_path")
        ),
    )
    #: Optional volume for workspace files, partitioned by subject subPath.
    #: Empty keeps workspace files on the sandbox's temporary filesystem.
    sandbox_workspace_volume: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ASTRABOX_SANDBOX_WORKSPACE_VOLUME",
            AliasPath("astrabox", "sandbox", "workspace_volume"),
        ),
    )
    #: Used only when ``sandbox_workspace_volume`` names a backing volume.
    workspace_mounter_image: str = Field(
        default_factory=lambda: release_image("workspace-mounter"),
        validation_alias=AliasChoices(
            "ASTRABOX_WORKSPACE_MOUNTER_IMAGE", AliasPath("astrabox", "storage", "mounter_image")
        ),
    )
    workspace_storage_topology: Literal["local", "shared"] = Field(
        default="local",
        validation_alias=AliasChoices(
            "ASTRABOX_WORKSPACE_STORAGE_TOPOLOGY", AliasPath("astrabox", "storage", "topology")
        ),
    )
    workspace_mount_root: str = Field(
        default="/var/lib/astrabox/workspace-mounts",
        validation_alias=AliasChoices(
            "ASTRABOX_WORKSPACE_MOUNT_ROOT", AliasPath("astrabox", "storage", "mount_root")
        ),
    )

    # --- agent -------------------------------------------------------------
    agent_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "ASTRABOX_AGENT_ENABLED", AliasPath("astrabox", "agent", "enabled")
        ),
    )
    agents_collection: str = Field(
        default="agents",
        validation_alias=AliasChoices(AliasPath("astrabox", "mongodb", "agents_collection")),
    )
    assistant_catalog_collection: str = Field(
        default="assistant_catalog",
        validation_alias=AliasChoices(
            AliasPath("astrabox", "mongodb", "assistant_catalog_collection")
        ),
    )
    assistant_workspace_collection: str = Field(
        default="assistant_workspace",
        validation_alias=AliasChoices(
            AliasPath("astrabox", "mongodb", "assistant_workspace_collection")
        ),
    )
    # Default for an Agent that omits its per-Agent idle window. The expiration
    # watcher reads this only for that unset case; an Agent-authored value wins.
    agent_idle_hibernate_seconds: int = Field(
        default=1800,
        gt=0,
        validation_alias=AliasChoices(AliasPath("astrabox", "agent", "idle_hibernate_seconds")),
    )
    agent_sandbox_renew_ttl_seconds: int = Field(
        default=604200,
        validation_alias=AliasChoices(
            "ASTRABOX_AGENT_SANDBOX_RENEW_TTL_SECONDS",
            AliasPath("astrabox", "agent", "sandbox_renew_ttl_seconds"),
        ),
    )
    expiration_watcher_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(AliasPath("astrabox", "agent", "expiration_watcher_enabled")),
    )
    expiration_watcher_interval_seconds: int = Field(
        default=300,
        validation_alias=AliasChoices(
            "ASTRABOX_EXPIRATION_WATCHER_INTERVAL_SECONDS",
            AliasPath("astrabox", "agent", "expiration_watcher_interval_seconds"),
        ),
    )
    expiration_watcher_threshold_seconds: int = Field(
        default=3600,
        validation_alias=AliasChoices(
            "ASTRABOX_EXPIRATION_WATCHER_THRESHOLD_SECONDS",
            AliasPath("astrabox", "agent", "expiration_watcher_threshold_seconds"),
        ),
    )
    reprovision_cooldown_seconds: int = Field(
        default=600,
        validation_alias=AliasChoices(
            "ASTRABOX_REPROVISION_COOLDOWN_SECONDS",
            AliasPath("astrabox", "agent", "reprovision_cooldown_seconds"),
        ),
    )
    # Per-sandbox lease: every managed sandbox is created with this short TTL and
    # auto-terminates unless renewed. The session owner renews it lazily — only
    # when the remaining lease drops below the renew threshold and there has been
    # recent activity (a turn or a mirror write). An abandoned sandbox (session
    # ended, process crashed, cold restart) stops being renewed and dies ~one lease
    # after the last activity, so leaks are prevented by construction with no
    # reaper. renew sets expires_at to now+lease (absolute), so concurrent renews
    # never accumulate.
    sandbox_lease_seconds: int = Field(  # 4h — the conversation lease
        default=14400,
        validation_alias=AliasChoices(
            "ASTRABOX_SANDBOX_LEASE_SECONDS", AliasPath("astrabox", "sandbox_lease_seconds")
        ),
    )
    # What a lapsed lease does to the box. The lease above already answers "how
    # long does an idle sandbox live"; this answers the other half, and it is
    # deliberately deployment-wide. Reclaiming idle capacity is a platform
    # decision — a per-agent setting would let any user pin cluster resources by
    # asking never to be reclaimed.
    #
    #   terminate — the box dies with its workspace. This is the default;
    #               pausing requires snapshot support and cluster storage.
    #   pause     — the filesystem is committed and the compute freed; the next
    #               turn resumes that same sandbox with its files intact.
    sandbox_idle_action: str = Field(
        default="terminate",
        validation_alias=AliasChoices(
            "ASTRABOX_SANDBOX_IDLE_ACTION", AliasPath("astrabox", "sandbox_idle_action")
        ),
    )
    # How long a parked box is kept before the platform reclaims it, and the only
    # promise this deployment makes about a paused conversation's files.
    #
    # It is a lease, not a shelf: a paused sandbox still expires on its lease and
    # takes its snapshot with it, so the sweeper renews to this value in the same
    # breath as pausing — the renew must run before the pause, because renewing an
    # already-paused sandbox makes the control plane fail it. Reaching the end of
    # it destroys the box and orphans its snapshot image in the registry, which no
    # AstraBox code and no OpenSandbox route collects; a deployment that parks
    # boxes owns that reclamation.
    sandbox_parked_retention_seconds: int = Field(
        default=604800,  # 7 days
        validation_alias=AliasChoices(
            "ASTRABOX_SANDBOX_PARKED_RETENTION_SECONDS",
            AliasPath("astrabox", "sandbox_parked_retention_seconds"),
        ),
    )
    # Host-controlled resolver used by the egress sidecar. It is intentionally
    # not copied from an Environment's env: changing DNS changes where a
    # credential-bound hostname goes. The one-container image fills this with
    # its private CoreDNS address; source-dev scripts do the equivalent.
    sandbox_egress_dns_upstream: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ASTRABOX_SANDBOX_EGRESS_DNS_UPSTREAM",
            AliasPath("astrabox", "sandbox_egress_dns_upstream"),
        ),
    )
    # The model credential is held by the egress sidecar instead of being handed
    # to the sandbox. On by default; callers synthesize a deny-by-default policy
    # when the Environment has none. A requested path that the backend cannot
    # provide fails instead of silently sending the real credential into the box.
    sandbox_credential_vault_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "ASTRABOX_SANDBOX_CREDENTIAL_VAULT",
            AliasPath("astrabox", "sandbox_credential_vault_enabled"),
        ),
    )
    sandbox_lease_renew_threshold_seconds: int = Field(  # renew when <1h remains
        default=3600,
        validation_alias=AliasChoices(
            "ASTRABOX_SANDBOX_LEASE_RENEW_THRESHOLD_SECONDS",
            AliasPath("astrabox", "sandbox_lease_renew_threshold_seconds"),
        ),
    )

    # -- coercions native pydantic does not replicate ----------------------
    @field_validator("sandbox_endpoint_scheme", mode="after")
    @classmethod
    def _validate_sandbox_endpoint_scheme(cls, value: str) -> str:
        normalized = str(value or "").strip().lower().removesuffix("://")
        if normalized not in {"", "http", "https"}:
            raise ValueError("ASTRABOX_SANDBOX_ENDPOINT_SCHEME must be http or https")
        return normalized

    @model_validator(mode="after")
    def _resolve_derived(self) -> "AstraBoxRuntimeSettings":
        # Empty derives the server's own bridge address inside a container so
        # platform MCP and callback routes remain reachable from a sandbox;
        # inert on the host or when explicitly configured.
        if not self.mcp_proxy_base_url:
            self.mcp_proxy_base_url = _default_mcp_proxy_base_url()
        if self.sandbox_secure_access_enabled and self.sandbox_endpoint_via_server_proxy:
            raise ValueError(
                "ASTRABOX_SANDBOX_SECURE_ACCESS cannot be combined with "
                "ASTRABOX_SANDBOX_ENDPOINT_VIA_SERVER_PROXY: signed browser URLs "
                "must use the OpenSandbox ingress gateway"
            )
        return self


def load_astrabox_settings() -> AstraBoxRuntimeSettings:
    """Build the runtime settings from the environment + ``app.yml`` (uncached).

    Uncached by design: each call re-reads the live environment (the YAML parse
    is cached), so a test that mutates ``ASTRABOX_*`` between cases sees the new
    value on the next call.
    """
    return AstraBoxRuntimeSettings()


def is_astrabox_enabled() -> bool:
    env_override = os.getenv("ASTRABOX_ENABLED")
    if env_override is not None:
        return env_override.lower() == "true"
    return _to_bool(_safe_get("astrabox.enabled", True), default=True)
