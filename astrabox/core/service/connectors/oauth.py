"""Where a vendor's MCP server sends its clients for authorization.

The connector hub runs the OAuth grant; what it needs from AstraBox is the
vendor's authorization server and endpoints, which the MCP authorization
specification (2026-07-28) has a client discover from the server's
protected-resource metadata (RFC 9728) and that server's metadata (RFC 8414).
The discovery URL order and metadata models come from the pinned ``mcp`` SDK
(``mcp/client/auth``).

Every URL this module requests is a public HTTPS address: a server's metadata
names the next URL, and a hostile server must not steer the platform at its
own network.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
)
from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.runtime_profile_networking import (
    non_public_address_reason,
)

_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


def connector_error(message: str, *, status_code: int = 502) -> APIError:
    return APIError(code="CONNECTOR_AUTHORIZATION_FAILED", message=message, status_code=status_code)


def require_redirect_uri(redirect_uri: str) -> None:
    """Refuse a redirect URI an authorization server must reject.

    The MCP authorization specification admits only HTTPS redirect URIs, or
    plain HTTP on a loopback host, and vendors enforce it at registration.
    The redirect URI is AstraBox's own address, so a deployment reached over
    plain HTTP at any other host cannot complete any connector's consent.
    """

    parts = urlsplit(redirect_uri)
    host = (parts.hostname or "").lower()
    if parts.scheme == "https":
        return
    if parts.scheme == "http":
        if host == "localhost":
            return
        try:
            if ipaddress.ip_address(host).is_loopback:
                return
        except ValueError:
            pass
    raise APIError(
        code="CONNECTOR_REQUIRES_HTTPS",
        message=(
            "connectors need AstraBox to be reached over HTTPS (or at localhost); "
            f"this request reached it at {parts.scheme}://{parts.netloc}"
        ),
        status_code=409,
    )


async def require_public_https(url: str) -> str:
    """Refuse a URL that is not HTTPS or that resolves to a non-public address."""

    parts = urlsplit(str(url or "").strip())
    if parts.scheme != "https" or not parts.hostname:
        raise connector_error(f"connector URL must be https: {url!r}", status_code=400)
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(parts.hostname, parts.port or 443, type=socket.SOCK_STREAM),
            timeout=10.0,
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise connector_error(f"could not resolve {parts.hostname!r}") from exc
    for info in infos:
        address = str(info[4][0]).split("%", 1)[0]
        reason = non_public_address_reason(address)
        if reason:
            raise connector_error(
                f"{parts.hostname!r} resolves to {address}, {reason}", status_code=400
            )
    return parts.geturl()


@dataclass(frozen=True)
class AuthorizationServer:
    """Where and how to obtain a token for one MCP server."""

    resource: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    #: What the MCP server says a client needs (RFC 9728 ``scopes_supported``).
    resource_scopes: tuple[str, ...]


async def _get_json(client: httpx.AsyncClient, url: str) -> dict[str, Any] | None:
    await require_public_https(url)
    response = await client.get(url, headers={"Accept": "application/json"})
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


async def discover(server_url: str) -> AuthorizationServer:
    """Find the authorization server for ``server_url`` the way the spec orders it."""

    server = await require_public_https(server_url)
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
        resource_metadata: ProtectedResourceMetadata | None = None
        for url in build_protected_resource_metadata_discovery_urls(None, server):
            payload = await _get_json(client, url)
            if payload is None:
                continue
            try:
                resource_metadata = ProtectedResourceMetadata.model_validate(payload)
            except ValueError:
                continue
            break
        auth_server_url = (
            str(resource_metadata.authorization_servers[0])
            if resource_metadata is not None and resource_metadata.authorization_servers
            else None
        )
        metadata: OAuthMetadata | None = None
        for url in build_oauth_authorization_server_metadata_discovery_urls(
            auth_server_url, server
        ):
            payload = await _get_json(client, url)
            if payload is None:
                continue
            try:
                metadata = OAuthMetadata.model_validate(payload)
            except ValueError:
                continue
            break
    if metadata is None:
        raise connector_error(f"{server!r} publishes no OAuth authorization server metadata")
    if auth_server_url and str(metadata.issuer).rstrip("/") != auth_server_url.rstrip("/"):
        # RFC 8414 §3.3: metadata whose issuer is not the server it was
        # fetched for must not be used.
        raise connector_error(
            f"authorization server metadata names issuer {str(metadata.issuer)!r}, "
            f"not {auth_server_url!r}"
        )
    resource = (
        str(resource_metadata.resource)
        if resource_metadata is not None and resource_metadata.resource
        else server
    )
    return AuthorizationServer(
        resource=resource,
        issuer=str(metadata.issuer),
        authorization_endpoint=str(metadata.authorization_endpoint),
        token_endpoint=str(metadata.token_endpoint),
        resource_scopes=tuple(
            (resource_metadata.scopes_supported or []) if resource_metadata is not None else []
        ),
    )
