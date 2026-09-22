"""Resolve AstraBox's OpenSandbox lifecycle and endpoint settings.

The lifecycle API and the browser-facing data plane can use different network
addresses and schemes. For example, AstraBox may call the lifecycle API over
HTTP on a private network while users open an HTTPS ingress gateway. This
module keeps those two settings separate and builds the SDK configuration used
for each operation.

Scheme discipline: the SDK's ``ConnectionConfig`` defaults to ``http``, but a
deployment may require ``https``. The lifecycle base URL therefore carries an
explicit scheme. Browser endpoints use ``ASTRABOX_SANDBOX_ENDPOINT_SCHEME``
when it is set and otherwise inherit the lifecycle scheme.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
from opensandbox.config import ConnectionConfig

from astrabox.common.utils.errors import APIError


def lifecycle_base_url(settings: Any, *, override: str | None = None) -> str:
    """The validated OpenSandbox lifecycle base URL (explicit scheme required)."""
    configured = (
        getattr(settings, "sandbox_openapi_base_url", "")
        if override is None
        else override
    )
    raw = str(configured or "").strip().rstrip("/")
    if not raw:
        if override is not None:
            raise APIError(
                code="SANDBOX_CONFIG_INVALID",
                message="the OpenSandbox lifecycle base URL override is empty",
                status_code=500,
            )
        raise APIError(
            code="SANDBOX_CONFIG_INVALID",
            message=(
                "open_sandbox requires ASTRABOX_SANDBOX_OPENAPI_BASE_URL "
                "(the OpenSandbox lifecycle API base URL); it is not configured"
            ),
            status_code=500,
        )
    if not raw.startswith(("http://", "https://")):
        source = (
            "the OpenSandbox lifecycle base URL override"
            if override is not None
            else "ASTRABOX_SANDBOX_OPENAPI_BASE_URL"
        )
        raise APIError(
            code="SANDBOX_CONFIG_INVALID",
            message=(
                f"{source} must carry an explicit "
                f"http:// or https:// scheme; got {raw!r}. The SDK would default "
                "to http while legacy helpers forced https — configure the "
                "scheme instead of letting either side guess"
            ),
            status_code=500,
        )
    return raw


def resolve_api_key(settings: Any) -> str | None:
    """The sandbox API key via the existing resolution chain, or ``None``.

    ``None`` is a legal configuration: a local, authless OpenSandbox server
    needs no key. The chain (plaintext env behind the local/debug gate, then the
    named secret) is the one the runtime already documents — no second path.
    """
    # Lazy import: config_resolver pulls the orchestrator's option-building
    # machinery, which a lifecycle-only lookup must not drag in at import time.
    from astrabox.core.service.orchestrator.runtime.config_resolver import (
        RuntimeConfigResolver,
    )

    return RuntimeConfigResolver(settings).resolve_sandbox_api_key()


def scrub_secret(text: str, *, secret: str | None) -> str:
    """Strip the API key from failure text before it can reach a log or repr.

    Defense in depth for the key-hygiene invariant: SDK error messages normally
    carry only operation + status, but a misbehaving server can echo request
    headers back in an error body, and that body flows into the SDK exception
    message verbatim. Package-shared because every module that talks to the
    lifecycle face (provider, executor, storage) has the same exception exits —
    the invariant covers the whole backend, not one file.
    """
    if secret:
        return text.replace(secret, "***")
    return text


def use_server_proxy(settings: Any) -> bool:
    """Whether AstraBox reaches sandbox services through the lifecycle server.

    This setting controls internal AstraBox-to-sandbox requests, such as the
    runner and Files API. Direct mode uses the host-mapped execd address. Proxy
    mode asks the OpenSandbox server to relay those requests and is useful when
    AstraBox runs in a container that cannot reach host-published ports.

    It does not control links returned to users. Browser endpoints are resolved
    separately and always use OpenSandbox's public execd or ingress address, so
    application traffic does not pass through the AstraBox API process.
    """
    return bool(getattr(settings, "sandbox_endpoint_via_server_proxy", False))


def sdk_connection_config(
    settings: Any,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    request_timeout_seconds: int | None = None,
    use_server_proxy_override: bool | None = None,
    lifecycle_base_url_override: str | None = None,
) -> ConnectionConfig:
    """One SDK ``ConnectionConfig`` for a single lifecycle operation.

    ``transport`` exists for tests only (an ``httpx.MockTransport`` injected
    through the signature rather than a monkeypatch). Production always passes
    ``None`` so the SDK creates and OWNS its transport (``_owns_transport=True``)
    and ``Sandbox.close()`` / ``SandboxManager.close()`` actually release the
    connection pool — a caller-shared transport would leak it.

    ``use_server_proxy`` normally rides on the connection config rather than on
    each call because the SDK threads it into every endpoint resolution it
    performs internally. ``use_server_proxy_override`` is reserved for the
    browser-endpoint resolver: a server process may need an internal relay while
    the browser must receive OpenSandbox's public execd/ingress route.
    """
    base_url = lifecycle_base_url(settings, override=lifecycle_base_url_override)
    api_key = resolve_api_key(settings)
    configured_timeout = int(
        getattr(settings, "sandbox_request_timeout_seconds", 15)
        if request_timeout_seconds is None
        else request_timeout_seconds
    )
    request_timeout = timedelta(seconds=configured_timeout)
    proxied = (
        use_server_proxy(settings)
        if use_server_proxy_override is None
        else bool(use_server_proxy_override)
    )
    common: dict[str, Any] = {
        "api_key": api_key,
        "domain": base_url,
        "request_timeout": request_timeout,
        "use_server_proxy": proxied,
    }
    if transport is not None:
        common["transport"] = transport
    return ConnectionConfig(**common)


def create_request_timeout_seconds(settings: Any) -> int:
    """The lifecycle POST budget for a sandbox create.

    OpenSandbox provisions the runtime and its egress sidecar inside the create
    request. The ordinary lifecycle timeout may be intentionally short, but it
    cannot cut off that synchronous POST before the configured ready budget: a
    timed-out client has no sandbox id while the server may still finish the
    create, leaving a live sandbox nobody can clean up.
    """

    request_seconds = int(getattr(settings, "sandbox_request_timeout_seconds", 15))
    ready_seconds = int(getattr(settings, "sandbox_ready_timeout_seconds", 120))
    return max(request_seconds, ready_seconds)


#: Header the OpenSandbox lifecycle API authenticates with.
API_KEY_HEADER = "OPEN-SANDBOX-API-KEY"

def lifecycle_headers(
    connection: ConnectionConfig,
    *,
    secret: str | None,
    accept: str,
) -> dict[str, str]:
    """Headers for a lifecycle request this backend issues WITHOUT the SDK.

    Two faces are reached over plain httpx rather than through the SDK, for the
    same reason: the SDK's generated client does not model them (the four
    plain-text diagnostic reports) or models them in a shape the operation
    cannot use (``Sandbox.create`` demands an image, which a pool-lent box must
    not carry). Both must still look exactly like every other request this
    deployment makes to that server, which is what this shares: the configured
    user agent, the deployment's own extra headers, and the API key when one is
    configured. ``accept`` is the caller's, because a plain-text report and a
    JSON resource want different ones.
    """
    headers = {
        "Accept": accept,
        "User-Agent": str(getattr(connection, "user_agent", "") or "astrabox"),
        **dict(getattr(connection, "headers", None) or {}),
    }
    if secret:
        headers[API_KEY_HEADER] = secret
    return headers


def endpoint_url(raw: str, *, settings: Any | None = None) -> str:
    """Normalize an SDK endpoint (``host:port`` or full URL) to a full URL.

    The scheme normally comes from the lifecycle base URL. A split deployment
    may expose its lifecycle API over an internal HTTP address while its ingress
    gateway uses HTTPS; ``ASTRABOX_SANDBOX_ENDPOINT_SCHEME`` states that separate
    data-plane decision.
    """
    value = str(raw or "").strip().rstrip("/")
    if not value:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message="open_sandbox resolved an empty sandbox endpoint",
            status_code=502,
        )
    if value.startswith(("http://", "https://")):
        return value
    if settings is None:
        from astrabox.common.utils.settings import load_astrabox_settings

        settings = load_astrabox_settings()
    scheme = str(getattr(settings, "sandbox_endpoint_scheme", "") or "").strip()
    if not scheme:
        scheme = lifecycle_base_url(settings).split("://", 1)[0]
    return f"{scheme}://{value}"


__all__ = [
    "API_KEY_HEADER",
    "endpoint_url",
    "lifecycle_headers",
    "lifecycle_base_url",
    "resolve_api_key",
    "scrub_secret",
    "sdk_connection_config",
    "use_server_proxy",
]
