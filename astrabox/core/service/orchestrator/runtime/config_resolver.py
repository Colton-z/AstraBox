"""Engine-neutral deployment, model, secret and sandbox config resolution."""

import os
import time
from collections.abc import Iterable
from typing import Any
from urllib.parse import parse_qs, urlparse

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.environment_networking import (
    parse_environment_networking,
)
from astrabox.seams.sandbox import (
    SANDBOX_NETWORK_LIMITED,
    SandboxNetworkPolicy,
)
from astrabox.seams.tracing import runtime_tracing_spec
from astrabox.common.utils.secrets import SecretProvider
from astrabox.common.utils.settings import _safe_get
from astrabox.core.model import AgentView
from astrabox.seams.model import (
    ModelCredentialKind,
    ModelEndpoint,
    ResolvedModelAccess,
    model_endpoint_for_name,
)
logger = get_logger(__name__)


def is_truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def is_local_mode() -> bool:
    return is_truthy(os.getenv("ASTRABOX_LOCAL_MODE"))


def allow_plaintext_model_api_key() -> bool:
    return is_local_mode() or is_truthy(
        os.getenv("ASTRABOX_ALLOW_PLAINTEXT_MODEL_API_KEY")
    )


def allow_plaintext_sandbox_api_key() -> bool:
    return is_local_mode() or is_truthy(
        os.getenv("ASTRABOX_ALLOW_PLAINTEXT_SANDBOX_API_KEY")
    )


def resolve_runtime_template_name(template: AgentView) -> str:
    """The container image a session for this environment runs.

    A value on the record is a pin: an environment that names an image runs that
    image, and upgrading the deployment does not move it. That is what naming one
    means, and it is how an environment for a different engine binds to its own
    image rather than the deployment's default one.

    No value means "follow the selected engine adapter", so an unpinned
    environment resolves that adapter's deployment default at create time. The
    distinction is reference versus copy: resolving here keeps deployment
    configuration live, while writing the same string onto the record once
    would freeze it at whatever the image happened to be that day.
    """
    runtime_template_name = str(template.runtime_template_name or "").strip()
    if runtime_template_name:
        return runtime_template_name
    engine_kind = str(template.engine_kind or "").strip()
    if not engine_kind:
        raise APIError(
            code="ENGINE_RUNTIME_IMAGE_REQUIRED",
            message=(
                "an unpinned Environment cannot resolve a runtime image without "
                "an engine_kind"
            ),
            status_code=409,
        )
    from astrabox.core.service.orchestrator.engine.registry import (
        get_engine_adapter,
    )

    default_image = str(
        get_engine_adapter(engine_kind).capabilities.default_runtime_image or ""
    ).strip()
    if not default_image:
        raise APIError(
            code="ENGINE_RUNTIME_IMAGE_REQUIRED",
            message=(
                f"engine {engine_kind!r} has no default runtime image; set the "
                "Environment's runtime_template_name"
            ),
            status_code=409,
        )
    return default_image


def platform_callback_egress_targets() -> list[str]:
    """The host(s) a box MUST be able to reach to talk back to this platform.

    A box calls home for platform-owned MCP capabilities and for lifecycle and
    transcript callbacks. The transcript mirror is the authority for the
    conversation, so losing it is not a degraded feature, it is data loss. All
    of those routes use ``ASTRABOX_MCP_PROXY_BASE_URL``; external MCP servers do
    not, because the engine reaches them directly.

    They are added to every limited policy automatically because an operator
    cannot reasonably be expected to know them: the address is derived per
    deployment, it is not a host anyone typed, and leaving it out does not fail
    loudly. It fails slowly: a policy that allows only the model endpoint leaves
    the in-box mirror to time out 3x30s inside the first turn, so a one-word
    answer from a fast model takes 93s and reads as a slow model. Adding the
    hosts here keeps that incomplete policy from ever becoming effective.

    Only the host is named, never a port or a path: the policy face is FQDN/host
    based, and the box needs the whole callback surface on that host or none of it.
    """
    from urllib.parse import urlsplit

    from astrabox.common.utils.settings import load_astrabox_settings

    base = str(load_astrabox_settings().mcp_proxy_base_url or "").strip()
    if not base:
        return []
    host = urlsplit(base if "//" in base else f"//{base}").hostname or ""
    return [host] if host else []


def required_network_host(url: str, *, label: str) -> str:
    """Return the host for a platform-known outbound URL or fail loud."""

    parsed = urlparse(str(url or "").strip())
    host = str(parsed.hostname or "").strip().lower()
    if parsed.scheme not in {"http", "https"} or not host:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=f"{label} must be an http(s) URL with a host; got {url!r}",
            status_code=500,
        )
    return host


def resolve_network_policy(
    template: AgentView,
    *,
    required_hosts: Iterable[str] = (),
    mcp_hosts: Iterable[str] = (),
) -> SandboxNetworkPolicy:
    """Resolve Environment networking into the provider-neutral sandbox seam.

    ``required_hosts`` are runtime destinations AstraBox can derive without
    inspecting a credential: the model endpoint, Plugin Git origins, and other
    platform-owned connections. ``mcp_hosts`` are the Agent's explicit remote
    MCP endpoints and are admitted only when the limited Environment opted into
    them. Vault binding hosts never enter either list: the provider receives the
    separate credential plan and admits only the destinations that assignment
    authorizes.
    """

    try:
        networking = parse_environment_networking(template.networking)
    except ValueError as exc:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=f"Environment networking is invalid: {exc}",
            status_code=500,
        ) from exc

    if networking.mode != SANDBOX_NETWORK_LIMITED:
        return SandboxNetworkPolicy(mode=networking.mode)

    explicit_mcp_hosts = [
        str(host).strip().lower()
        for host in mcp_hosts
        if str(host or "").strip()
    ]
    if explicit_mcp_hosts and not networking.allow_mcp_servers:
        raise APIError(
            code="AGENT_MCP_NETWORK_ACCESS_DISABLED",
            message=(
                "this Agent declares remote MCP endpoints, but its limited "
                "Environment has networking.allow_mcp_servers=false; enable "
                "that setting or remove the remote MCP servers. Hosts: "
                + ", ".join(sorted(set(explicit_mcp_hosts)))
            ),
            status_code=409,
        )

    candidates: list[str] = [*networking.allowed_hosts]
    from astrabox.core.service.orchestrator.runtime.conversation_identity import (
        skill_repo_egress_hosts,
    )

    candidates.extend(skill_repo_egress_hosts(template.skills))
    candidates.extend(platform_callback_egress_targets())
    candidates.extend(
        str(host).strip()
        for host in required_hosts
        if str(host or "").strip()
    )
    candidates.extend(explicit_mcp_hosts)

    tracing = runtime_tracing_spec(template)
    if tracing and tracing.active and tracing.endpoint_host:
        candidates.append(tracing.endpoint_host)

    allowed: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        host = str(candidate or "").strip().lower()
        if not host or host in seen:
            continue
        seen.add(host)
        allowed.append(host)
    return SandboxNetworkPolicy(
        mode=SANDBOX_NETWORK_LIMITED,
        allowed_hosts=tuple(allowed),
    )


class RuntimeConfigResolver:
    """Encapsulates all configuration resolution for the runtime manager."""

    def __init__(self, settings: Any) -> None:
        self._settings = settings

    def resolve_template_model_name(self, template: AgentView) -> str | None:
        # Single source: model_config.model_name.
        model_config_payload = self.resolve_model_config(template.model_config or {})
        return str(model_config_payload.get("model_name") or "").strip() or None

    def resolve_remote_cwd(self) -> str | None:
        """The base cwd the agent works in, absent a per-conversation workspace.

        The NAS branch below is the mount target of the runtime NFS mount, and is
        correct only on a backend that performs that mount. It cannot be reached
        on a backend that mounts at create time instead: ``astrabox.bootstrap``
        refuses to start with the NAS pair set on such a backend, precisely
        because this function would otherwise move the cwd onto a directory that
        nothing mounted.
        """
        value = str(os.getenv("ASTRABOX_REMOTE_CWD", "")).strip()
        if value:
            return value

        if self._settings.nas_enabled and self._settings.nas_endpoint:
            return "/root/workspace"

        # The bundled image declares /workspace as its workload directory. Keep
        # the agent there so the image-created account and every backend's file
        # face expose the same tree; /root is mode-700 and belongs to a different
        # identity. Overridable via ``ASTRABOX_REMOTE_CWD`` for another image
        # contract.
        return "/workspace"

    def resolve_sandbox_api_key(self) -> str | None:
        env_key = str(os.getenv("ASTRABOX_SANDBOX_API_KEY", "")).strip()
        if env_key and allow_plaintext_sandbox_api_key():
            logger.warning("using plaintext ASTRABOX_SANDBOX_API_KEY in local/debug mode")
            return env_key
        if env_key:
            logger.warning("ignore ASTRABOX_SANDBOX_API_KEY outside local/debug mode")
        return SecretProvider.get_secret(self._settings.sandbox_api_key_secret_name)

    def resolve_endpoint_provider(self, model_config: dict[str, Any]) -> str:
        """The effective model-endpoint provider name for a requested config.

        The environment's ``endpoint_provider`` (carried in the synthesized
        ``model_config`` dict) wins, falling back to the deployment-global
        ``model_endpoint_provider`` setting when the environment does not pin
        one. An empty selection resolves through the provider registry's
        declared default. The returned name is always the concrete provider
        that owns the wire endpoint and its request metadata.
        """
        selected = str(
            model_config.get("endpoint_provider")
            or getattr(self._settings, "model_endpoint_provider", "")
            or ""
        ).strip()
        return model_endpoint_for_name(selected).name

    def _model_endpoint_override(self, model_config: dict[str, Any]) -> ModelEndpoint:
        """The selected ModelEndpointProvider's overrides for a requested config.

        The provider is chosen per environment and returns the endpoint the
        sandbox must use.
        """
        provider_name = self.resolve_endpoint_provider(model_config)
        return model_endpoint_for_name(provider_name).resolve(
            requested=ModelEndpoint(
                base_url=str(model_config.get("base_url") or "").strip() or None,
                model_name=str(model_config.get("model_name") or "").strip() or None,
            ),
            settings=self._settings,
        )

    def resolve_model_access(
        self, model_config: dict[str, Any]
    ) -> ResolvedModelAccess:
        """Resolve one Environment's model access without engine wire names."""

        payload, endpoint_provider, endpoint = self._resolve_model_configuration(
            model_config
        )
        credential, credential_kind = self._resolve_model_credential(
            payload,
            endpoint=endpoint,
        )
        public_configuration = dict(payload)
        public_configuration.pop("api_key", None)
        return ResolvedModelAccess(
            configuration=public_configuration,
            base_url=str(payload.get("base_url") or "").strip().rstrip("/") or None,
            model_name=str(payload.get("model_name") or "").strip() or None,
            credential=credential,
            credential_kind=credential_kind,
            endpoint_provider=endpoint_provider,
        )

    def resolve_model_config(self, model_config: dict[str, Any]) -> dict[str, Any]:
        """Resolve the non-secret configuration through the endpoint provider."""

        payload, _provider_name, _endpoint = self._resolve_model_configuration(
            model_config
        )
        payload.pop("api_key", None)
        return payload

    def _resolve_model_configuration(
        self, model_config: dict[str, Any]
    ) -> tuple[dict[str, Any], str, ModelEndpoint]:
        payload = dict(model_config or {})
        if not payload.get("base_url"):
            env_base_url = str(os.getenv("ASTRABOX_MODEL_BASE_URL", "")).strip()
            if env_base_url:
                payload["base_url"] = env_base_url
            elif self._settings.model_base_url:
                payload["base_url"] = self._settings.model_base_url
        if not payload.get("model_name"):
            env_model_name = str(os.getenv("ASTRABOX_MODEL_NAME", "")).strip()
            if env_model_name:
                payload["model_name"] = env_model_name
            elif self._settings.model_name:
                payload["model_name"] = self._settings.model_name
        if not payload.get("api_key_secret_name"):
            env_secret_name = str(
                os.getenv("ASTRABOX_MODEL_API_KEY_SECRET_NAME", "")
            ).strip()
            if env_secret_name:
                payload["api_key_secret_name"] = env_secret_name
            elif self._settings.model_api_key_secret_name:
                payload["api_key_secret_name"] = (
                    self._settings.model_api_key_secret_name
                )

        endpoint_provider = self.resolve_endpoint_provider(payload)
        endpoint = self._model_endpoint_override(payload)
        if endpoint.base_url:
            payload["base_url"] = endpoint.base_url
        if endpoint.model_name:
            payload["model_name"] = endpoint.model_name
        return payload, endpoint_provider, endpoint

    def _resolve_model_credential(
        self,
        model_config: dict[str, Any],
        *,
        endpoint: ModelEndpoint,
    ) -> tuple[str | None, ModelCredentialKind]:
        """Resolve credential value and transport-neutral authentication kind."""

        endpoint_credential = str(endpoint.api_key or "").strip()
        if endpoint_credential:
            return endpoint_credential, endpoint.credential_kind or "bearer"

        secret_name = model_config.get("api_key_secret_name")
        if secret_name:
            secret_value = SecretProvider.get_secret(str(secret_name))
            if secret_value:
                return secret_value, "bearer"
            logger.warning(
                "model api key secret_name configured but unresolved: %s; "
                "fallback to next source",
                secret_name,
            )

        api_key = str(model_config.get("api_key") or "").strip()
        if api_key and allow_plaintext_model_api_key():
            logger.warning("using plaintext template model api key in local/debug mode")
            return api_key, "bearer"
        if api_key:
            logger.warning(
                "ignore plaintext template model api key outside local/debug mode"
            )

        env_secret_name = str(
            os.getenv("ASTRABOX_MODEL_API_KEY_SECRET_NAME", "")
        ).strip()
        if env_secret_name:
            return SecretProvider.get_secret(env_secret_name), "bearer"

        env_api_key = str(os.getenv("ASTRABOX_MODEL_API_KEY", "")).strip()
        if env_api_key:
            return env_api_key, "bearer"

        if self._settings.model_api_key:
            value = self._settings.model_api_key
            kind: ModelCredentialKind = "bearer"
            if value != os.getenv("ANTHROPIC_AUTH_TOKEN") and value == os.getenv(
                "ANTHROPIC_API_KEY"
            ):
                kind = "api_key"
            return value, kind

        if self._settings.model_api_key_secret_name:
            return (
                SecretProvider.get_secret(self._settings.model_api_key_secret_name),
                "bearer",
            )

        return None, "bearer"
