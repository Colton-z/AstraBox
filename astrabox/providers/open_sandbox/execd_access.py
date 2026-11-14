"""The per-sandbox credential that gates a Docker sandbox's execd.

On OpenSandbox's Docker runtime a sandbox publishes three ports on the host:
execd (44772), the image's :8080 gateway and the egress sidecar's API (18080).
Every other in-box port is reached through execd's ``/proxy/<port>`` route
(``services/docker/docker_service.py`` and ``services/docker/networking.py`` in
``opensandbox-server`` 1.2.1). Without a credential, anything that reaches the
published execd port — another container on the bridge, a process on the host
— runs commands as root, reads and writes files, and opens every in-box
service in that sandbox.

execd has its own credential: started with ``EXECD_ACCESS_TOKEN`` in its
environment, it answers 401 to every request that does not carry the same value
in ``X-EXECD-ACCESS-TOKEN``, except ``/ping`` and ``/ready``. The names are the
vendor's (``components/execd/pkg/flag/parser.go`` and
``components/execd/pkg/web/model/header.go``). AstraBox's execd build adds
``containers/execd/access-token.patch``: ``/internal/init``, which replaces that
credential, then requires it too, and ``/proxy/<port>`` does not forward it to
any in-box service but execd itself.

**Custody.** The token is derived, not stored: an HMAC of the deployment's
secret root (:func:`derive_platform_key` under :func:`platform_secret_root`, as
the :8080 key in :mod:`.aio_auth` is) over a random subject written to the
sandbox's metadata under :data:`SANDBOX_EXECD_ACCESS_METADATA_KEY` at create.
The subject is not a secret; whoever reads metadata through the lifecycle API
still cannot compute the token without the deployment secret. Every replica,
a restarted server, a pooled box claimed later and a shared box's every
conversation compute the same value from the box's own metadata. Adoption
rewrites the box's owner and assignment keys but never this one, and pause
keeps both the metadata and execd's environment.

**Where the token is.** In the create environment, which only execd and the
vendor's ``bootstrap.sh`` (PID 1, root) keep: :func:`entrypoint_without_token`
removes it before the image's entrypoint runs, execd removes it from every
command, terminal and isolated session it starts, and a process of another
account cannot read a root process's ``/proc/<pid>/environ``. A workload that
runs as root in the sandbox can read it there.

A sandbox whose metadata carries no subject was created without a token, and
AstraBox sends it none.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from typing import Any

import httpx

from astrabox.common.utils.errors import APIError

#: execd's own environment variable for its API credential (vendor name).
EXECD_ACCESS_TOKEN_ENV = "EXECD_ACCESS_TOKEN"

#: The request header execd authenticates (vendor name).
EXECD_ACCESS_TOKEN_HEADER = "X-EXECD-ACCESS-TOKEN"

#: The sandbox metadata key holding the token's derivation subject.
SANDBOX_EXECD_ACCESS_METADATA_KEY = "astrabox.execd-access"

#: In-box ports whose Docker endpoint is their own published port rather than
#: execd's ``/proxy/<port>`` (``_resolve_host_mapped_endpoint`` in
#: ``services/docker/networking.py``): only the image's :8080 gateway. The egress
#: API is published too, but its endpoint is execd's proxy like every other port.
ENDPOINTS_OUTSIDE_EXECD = frozenset({8080})

_EXECD_ACCESS_DOMAIN = "astrabox-execd-access"


def execd_access_required(settings: Any) -> bool:
    """Whether a sandbox this deployment creates gets an execd credential.

    Only the bundled lifecycle server on Docker: there AstraBox selects the
    execd image, so :func:`require_execd_access_enforced` can hold every box
    to it. ``ASTRABOX_SANDBOX_UNAUTHENTICATED_EXECD`` turns it off there and
    is refused anywhere else, where it would change nothing.
    """

    from astrabox.providers.open_sandbox.sandbox import OpenSandboxSandboxProvider

    unauthenticated = bool(getattr(settings, "sandbox_unauthenticated_execd", False))
    if OpenSandboxSandboxProvider._bundled_docker():
        return not unauthenticated
    if unauthenticated:
        raise APIError(
            code="SANDBOX_CONFIG_INVALID",
            message=(
                "ASTRABOX_SANDBOX_UNAUTHENTICATED_EXECD=true applies only to the "
                "bundled lifecycle server on the Docker runtime; this deployment "
                "creates every sandbox without an execd credential already"
            ),
            status_code=500,
        )
    return False


def new_execd_access_subject() -> str:
    """A fresh derivation subject for one sandbox, valid as a metadata value."""

    return secrets.token_hex(16)


def execd_access_token(subject: str) -> str:
    """The execd credential of the sandbox whose metadata names ``subject``."""

    from astrabox.core.service.orchestrator.platform_secret import (
        derive_platform_key,
        platform_secret_root,
    )

    value = str(subject or "").strip()
    if not value:
        raise ValueError("an execd access token needs a non-empty subject")
    return derive_platform_key(
        platform_secret_root(), domain=_EXECD_ACCESS_DOMAIN, subject=value
    ).hex()


def execd_access_headers(metadata: Mapping[str, Any] | None) -> dict[str, str]:
    """The header a request to this sandbox's execd carries; empty when it has no token."""

    subject = str(dict(metadata or {}).get(SANDBOX_EXECD_ACCESS_METADATA_KEY) or "").strip()
    if not subject:
        return {}
    return {EXECD_ACCESS_TOKEN_HEADER: execd_access_token(subject)}


def with_execd_access(connection_config: Any, access: Mapping[str, str]) -> Any:
    """``connection_config`` for one sandbox, carrying its execd credential.

    The SDK sends ``ConnectionConfig.headers`` on every request it makes for
    that sandbox — execd's commands and files, and also the lifecycle API and
    the egress sidecar's API, which are deployment components outside the
    sandbox.
    """

    if not access:
        return connection_config
    return connection_config.model_copy(
        update={"headers": {**dict(connection_config.headers or {}), **dict(access)}}
    )


def entrypoint_without_token(entrypoint: tuple[str, ...]) -> tuple[str, ...]:
    """The image entrypoint, started without execd's credential in its environment.

    OpenSandbox's ``bootstrap.sh`` starts execd and then the entrypoint with the
    whole create environment. It removes ``EXECD_ACCESS_TOKEN`` only when execd
    runs as the sandbox's init, which this deployment does not enable, so every
    process the image starts would inherit the token.
    """

    return ("env", "-u", EXECD_ACCESS_TOKEN_ENV, *entrypoint)


async def require_execd_access_enforced(
    execd_url: str,
    *,
    token: str,
    routing_headers: Mapping[str, str],
) -> None:
    """Prove that the execd at ``execd_url`` admits only ``token``, or raise.

    Three requests, none of which changes the sandbox: a file lookup without
    the token must be refused, the same lookup with it must succeed, and an
    empty ``/internal/init`` without it must be refused. execd authenticates
    before it validates, so a vendor build without ``access-token.patch``
    answers that last request with 400, and the sandbox's token could be
    replaced by any caller.
    """

    base = str(execd_url).rstrip("/")
    routing = dict(routing_headers)
    authorized = {**routing, EXECD_ACCESS_TOKEN_HEADER: token}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            anonymous = await client.get(
                f"{base}/files/info", params={"path": "/"}, headers=routing
            )
            admitted = await client.get(
                f"{base}/files/info", params={"path": "/"}, headers=authorized
            )
            init = await client.post(f"{base}/internal/init", json={}, headers=routing)
    except httpx.HTTPError as exc:
        raise APIError(
            code="SANDBOX_EXECD_ACCESS_UNVERIFIED",
            message=f"could not check the sandbox's execd credential: {exc}",
            status_code=502,
        ) from exc
    if anonymous.status_code != 401:
        raise APIError(
            code="SANDBOX_EXECD_ACCESS_UNVERIFIED",
            message=(
                "the sandbox's execd answered a request without its access token "
                f"with {anonymous.status_code}; the token in its create "
                "environment did not reach execd"
            ),
            status_code=502,
        )
    if admitted.status_code != 200:
        raise APIError(
            code="SANDBOX_EXECD_ACCESS_UNVERIFIED",
            message=(
                "the sandbox's execd refused its own access token with "
                f"{admitted.status_code}"
            ),
            status_code=502,
        )
    if init.status_code != 401:
        raise APIError(
            code="SANDBOX_EXECD_ACCESS_UNVERIFIED",
            message=(
                "the sandbox's execd accepts /internal/init without its access "
                f"token (answered {init.status_code}), so any caller could "
                "replace that token. Its execd image is not AstraBox's build "
                "with containers/execd/access-token.patch; set "
                "ASTRABOX_SANDBOX_SERVER_EXECD_IMAGE to that image"
            ),
            status_code=502,
        )


__all__ = [
    "ENDPOINTS_OUTSIDE_EXECD",
    "EXECD_ACCESS_TOKEN_ENV",
    "EXECD_ACCESS_TOKEN_HEADER",
    "SANDBOX_EXECD_ACCESS_METADATA_KEY",
    "entrypoint_without_token",
    "execd_access_headers",
    "execd_access_required",
    "execd_access_token",
    "new_execd_access_subject",
    "require_execd_access_enforced",
    "with_execd_access",
]
