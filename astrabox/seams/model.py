"""Model endpoint seam and provider registry.

One provider resolves a requested model configuration to the wire endpoint the
runtime uses. It also owns endpoint-specific deployment validation and optional
HTTP request headers. The orchestrator carries those values without knowing the
gateway's configuration names or protocol vocabulary.

Providers register through the ``astrabox.providers.model`` entry-point group
or :func:`register_model_endpoint`. A provider may explicitly declare itself as
the deployment default. Name resolution fails on an unknown or ambiguous
selection.

This module imports only the standard library.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
import ipaddress
from typing import Any, Literal
from urllib.parse import urlsplit

#: Entry-point group an external package registers a provider under.
ENTRY_POINT_GROUP = "astrabox.providers.model"


ModelCredentialKind = Literal["api_key", "bearer"]


@dataclass(frozen=True)
class ModelEndpoint:
    """The effective wire endpoint for the agent runtime's model traffic.

    ``None`` fields mean "no opinion — keep what the requested configuration
    resolved" (the ``direct`` provider returns all-``None``); non-``None``
    fields OVERRIDE the requested values.
    """

    base_url: str | None = None
    api_key: str | None = None
    model_name: str | None = None
    credential_kind: ModelCredentialKind | None = None


@dataclass(frozen=True)
class ResolvedModelAccess:
    """Engine-neutral model access resolved for one Environment.

    ``configuration`` retains non-secret, provider- and engine-authored fields
    the selected adapter may understand. The remaining fields are the common
    transport facts every adapter needs. ``credential_kind`` describes HTTP
    authentication without naming any engine's environment variable; mapping
    it onto a CLI flag, profile key or environment variable belongs to the
    adapter.
    """

    configuration: Mapping[str, Any]
    base_url: str | None
    model_name: str | None
    credential: str | None
    credential_kind: ModelCredentialKind
    endpoint_provider: str


@dataclass(frozen=True)
class ModelRequestContext:
    """Identity and conversation metadata available for one model request."""

    conversation_id: str | None = None
    user_id: str | None = None


class ModelEndpointConfigurationError(RuntimeError):
    """The selected provider cannot honor the deployment configuration."""


class ModelEndpointProvider(ABC):
    """One model gateway's integration: requested config → effective endpoint."""

    #: Registry key. Required, non-empty; lowercased when registered.
    name: str
    #: Exactly one built-in or deployment provider may declare the default.
    is_default: bool = False

    @abstractmethod
    def resolve(self, *, requested: ModelEndpoint, settings: Any = None) -> ModelEndpoint:
        """Return the effective endpoint for a requested model configuration.

        ``requested`` carries what the template/environment/env resolution asked
        for (model name, and direct credentials when configured). A gateway
        provider typically keeps ``requested.model_name`` (its routing key) and
        overrides ``base_url``/``api_key`` with its own; it must fail loud when
        its own configuration is missing rather than degrade to passthrough.
        """

    def list_models(self, *, provider_access: Any = None, settings: Any = None) -> list[str]:
        """The model ids the console can offer for an agent on this gateway.

        ``provider_access`` is the environment's provider-owned access object.
        A provider with no authoritative registry returns ``[]`` and the
        console uses free-text entry. This is a best-effort config-time
        convenience: network or authorization failure returns ``[]``.
        """
        _ = (provider_access, settings)
        return []

    def request_headers(
        self,
        *,
        endpoint: ModelEndpoint,
        context: ModelRequestContext,
    ) -> Mapping[str, str]:
        """Return provider-owned HTTP headers for one runtime conversation."""

        _ = (endpoint, context)
        return {}

    def session_credential(self, *, context: ModelRequestContext) -> str | None:
        """The credential one Session's own identity spends at this gateway.

        A gateway that can tell one conversation from another issues each
        Session its own credential; the platform hands that value to the
        Session's sandbox instead of the deployment-wide one, and the gateway
        attributes what it sees. ``None`` means this provider has no
        per-Session identity, and the caller keeps the shared credential —
        the difference is attribution, never authorization.

        Derived rather than looked up: a replica that did not mint the
        credential still has to name it when it composes egress state.
        """

        _ = context
        return None

    async def ensure_session_credential(
        self, *, context: ModelRequestContext
    ) -> str | None:
        """Make :meth:`session_credential` usable, returning it.

        Called once on the path that binds a Session to its runtime, before
        any model request can be made under it. A provider that cannot honour
        the request raises; returning ``None`` means only that this provider
        has no per-Session identity to establish.
        """

        _ = context
        return None

    async def release_session_credential(
        self, *, context: ModelRequestContext
    ) -> bool:
        """Garbage-collect a finished Session's credential; True when removed.

        Best effort by contract: a leaked per-Session credential is scoped to
        the spend it could already make, so cleanup is never a correctness
        gate and must not fail a caller.
        """

        _ = context
        return False

    def validate_configuration(self, *, settings: Any = None) -> None:
        """Reject deployment settings this provider cannot honor."""

        if not bool(getattr(settings, "model_gateway_require_https", False)):
            return
        requested = ModelEndpoint(
            base_url=str(getattr(settings, "model_base_url", "") or "").strip()
            or None,
            model_name=str(getattr(settings, "model_name", "") or "").strip()
            or None,
        )
        resolved = self.resolve(requested=requested, settings=settings)
        base_url = str(resolved.base_url or requested.base_url or "").strip()
        parsed = urlsplit(base_url)
        host = str(parsed.hostname or "").strip().lower().rstrip(".")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ModelEndpointConfigurationError(
                f"model endpoint has an invalid port: {base_url!r}"
            ) from exc
        try:
            ipaddress.ip_address(host)
        except ValueError:
            host_is_ip = False
        else:
            host_is_ip = True
        if (
            parsed.scheme.lower() != "https"
            or not host
            or host_is_ip
            or "." not in host
            or (port is not None and port != 443)
        ):
            raise ModelEndpointConfigurationError(
                "the configured model endpoint must be an HTTPS FQDN on port "
                f"443; got {base_url!r}"
            )


_PROVIDERS: dict[str, ModelEndpointProvider] = {}
_DEFAULT_PROVIDER: str | None = None


def register_model_endpoint(provider: ModelEndpointProvider) -> None:
    """Register a provider under its name and publish its default declaration."""

    global _DEFAULT_PROVIDER
    name = str(getattr(provider, "name", "") or "").strip().lower()
    if not name:
        raise RuntimeError("model endpoint provider must have a non-empty name")
    _PROVIDERS[name] = provider
    if bool(getattr(provider, "is_default", False)):
        if _DEFAULT_PROVIDER not in {None, name}:
            raise RuntimeError(
                "multiple default model endpoint providers registered: "
                f"{_DEFAULT_PROVIDER!r} and {name!r}"
            )
        _DEFAULT_PROVIDER = name


def default_model_endpoint_name() -> str:
    """Return the declared default, or the sole provider, and fail on ambiguity."""

    if _DEFAULT_PROVIDER:
        return _DEFAULT_PROVIDER
    if len(_PROVIDERS) == 1:
        return next(iter(_PROVIDERS))
    raise RuntimeError(
        "model endpoint provider name is required: "
        f"{len(_PROVIDERS)} registered ({sorted(_PROVIDERS)})"
    )


def model_endpoint_for_name(name: str | None) -> ModelEndpointProvider:
    """Resolve a provider by name, using only an explicitly declared default."""

    wanted = str(name or "").strip().lower() or default_model_endpoint_name()
    provider = _PROVIDERS.get(wanted)
    if provider is None:
        raise RuntimeError(
            f"no ModelEndpointProvider registered for name={wanted!r} "
            f"(registered: {sorted(_PROVIDERS)})"
        )
    return provider


def registered_model_endpoint_names() -> list[str]:
    """The registered provider names, sorted (for schemas/diagnostics)."""
    return sorted(_PROVIDERS)


__all__ = [
    "ENTRY_POINT_GROUP",
    "ModelEndpoint",
    "ModelEndpointConfigurationError",
    "ModelEndpointProvider",
    "ModelRequestContext",
    "ModelCredentialKind",
    "ResolvedModelAccess",
    "default_model_endpoint_name",
    "register_model_endpoint",
    "model_endpoint_for_name",
    "registered_model_endpoint_names",
]
