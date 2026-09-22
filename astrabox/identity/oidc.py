"""Provider-neutral OIDC discovery, validation, and claim mapping.

This module deliberately has no dependency on AstraBox's API, persistence, or
service layers, so multiple authentication adapters can share one OIDC
implementation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

import httpx

_DEFAULT_SCOPES = "openid profile email"
_DEFAULT_GROUPS_CLAIM = "groups"
_DEFAULT_ADMIN_GROUP = "astrabox-admin"


class OidcAccessTokenRejected(Exception):
    """The identity provider rejected a presented access token."""


class OidcProviderUnavailable(Exception):
    """The identity provider could not currently validate a token."""


class OidcProviderMisconfigured(Exception):
    """The identity provider answered, and its answer is not an identity.

    A sibling of the two above rather than a subclass, because it asks for a
    third action: the token is good and the provider is reachable, so neither
    presenting a new token nor waiting changes the outcome. Someone edits the
    provider's configuration.
    """


@dataclass(frozen=True)
class IdentityPrincipal:
    """Identity fields shared across product-specific authorization adapters."""

    user_id: str
    email: str | None = None
    display_name: str | None = None
    org_id: str | None = None
    roles: tuple[str, ...] = ()
    api_scopes: tuple[str, ...] | None = None


def _env(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or "").strip()


def _exclusive_secret(
    name: str,
    file_name: str,
    value: str,
    path: str,
) -> str:
    """Read one secret from an environment value or a file, never both."""

    if value and path:
        raise RuntimeError(f"set only one of {name} and {file_name}")
    if not path:
        return value
    try:
        secret = open(path, encoding="utf-8").read().strip()
    except OSError as exc:
        raise RuntimeError(f"cannot read {file_name}: {path}") from exc
    if not secret:
        raise RuntimeError(f"{file_name} is empty: {path}")
    return secret


@dataclass(frozen=True)
class OidcProviderConfig:
    """OIDC provider settings shared by login and bearer-token validation."""

    issuer: str
    client_id: str
    client_secret: str
    internal_issuer: str
    scopes: str
    groups_claim: str
    admin_group: str
    redirect_url_override: str
    api_client_id: str
    api_client_secret: str

    @staticmethod
    def load() -> "OidcProviderConfig":
        issuer = _env("ASTRABOX_OIDC_ISSUER").rstrip("/")
        client_id = _env("ASTRABOX_OIDC_CLIENT_ID")
        client_secret = _exclusive_secret(
            "ASTRABOX_OIDC_CLIENT_SECRET",
            "ASTRABOX_OIDC_CLIENT_SECRET_FILE",
            _env("ASTRABOX_OIDC_CLIENT_SECRET"),
            _env("ASTRABOX_OIDC_CLIENT_SECRET_FILE"),
        )
        api_client_id = _env("ASTRABOX_OIDC_API_CLIENT_ID")
        api_client_secret = _exclusive_secret(
            "ASTRABOX_OIDC_API_CLIENT_SECRET",
            "ASTRABOX_OIDC_API_CLIENT_SECRET_FILE",
            _env("ASTRABOX_OIDC_API_CLIENT_SECRET"),
            _env("ASTRABOX_OIDC_API_CLIENT_SECRET_FILE"),
        )
        if not issuer or not client_id:
            raise RuntimeError(
                "OIDC identity requires ASTRABOX_OIDC_ISSUER and "
                "ASTRABOX_OIDC_CLIENT_ID (and normally ASTRABOX_OIDC_CLIENT_SECRET)"
            )
        if bool(api_client_id) != bool(api_client_secret):
            raise RuntimeError(
                "OIDC API access requires both ASTRABOX_OIDC_API_CLIENT_ID and "
                "one of ASTRABOX_OIDC_API_CLIENT_SECRET or "
                "ASTRABOX_OIDC_API_CLIENT_SECRET_FILE"
            )
        return OidcProviderConfig(
            issuer=issuer,
            client_id=client_id,
            client_secret=client_secret,
            internal_issuer=_env("ASTRABOX_OIDC_INTERNAL_ISSUER").rstrip("/")
            or issuer,
            scopes=_env("ASTRABOX_OIDC_SCOPES") or _DEFAULT_SCOPES,
            groups_claim=_env("ASTRABOX_OIDC_GROUPS_CLAIM")
            or _DEFAULT_GROUPS_CLAIM,
            admin_group=_env("ASTRABOX_OIDC_ADMIN_GROUP") or _DEFAULT_ADMIN_GROUP,
            redirect_url_override=_env("ASTRABOX_OIDC_REDIRECT_URL"),
            api_client_id=api_client_id,
            api_client_secret=api_client_secret,
        )

    def to_internal(self, url: str) -> str:
        """Rewrite a discovered public endpoint onto the server-side issuer."""

        if self.internal_issuer == self.issuer:
            return url
        if url.startswith(self.issuer):
            return self.internal_issuer + url[len(self.issuer) :]
        return url


_discovery_cache: dict[str, dict[str, Any]] = {}


async def discover(config: OidcProviderConfig) -> dict[str, Any]:
    """Fetch and cache the provider's OpenID configuration."""

    cached = _discovery_cache.get(config.internal_issuer)
    if cached is not None:
        return cached
    url = config.internal_issuer + "/.well-known/openid-configuration"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            document = response.json()
    except (httpx.HTTPError, TypeError, ValueError) as exc:
        raise OidcProviderUnavailable("OIDC discovery failed") from exc
    if not isinstance(document, dict):
        raise OidcProviderUnavailable("OIDC discovery returned an invalid document")
    for required in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not str(document.get(required) or "").strip():
            raise OidcProviderUnavailable(
                f"OIDC discovery document is missing {required}"
            )
    _discovery_cache[config.internal_issuer] = document
    return document


def _coerce_groups(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return text.replace(",", " ").split() if text else []


def roles_from_claims(
    claims: Mapping[str, Any], config: OidcProviderConfig
) -> list[str]:
    """Map the configured IdP group to AstraBox's platform-admin role."""

    for group in _coerce_groups(claims.get(config.groups_claim)):
        if config.admin_group in (group, group.split("/")[-1]):
            # This provider-neutral file is also copied as a standalone gateway
            # adapter whose isolated environment deliberately has no AstraBox
            # package. ``admin`` is therefore part of that adapter wire contract;
            # platform consumers use their shared constant for the same fixed
            # vocabulary.
            return ["admin"]
    return []


def principal_from_claims(
    claims: Mapping[str, Any], config: OidcProviderConfig
) -> IdentityPrincipal:
    """Map standard OIDC claims plus Casdoor's display name to a principal."""

    user_id = str(claims.get("sub") or "").strip()
    if not user_id:
        raise OidcProviderMisconfigured(
            "OIDC identity has no subject: configure the provider to return a "
            "stable 'sub' claim (UserInfo tokens normally require 'openid' scope)"
        )
    display_name = (
        str(claims.get("displayName") or "").strip()
        or str(claims.get("name") or "").strip()
        or str(claims.get("preferred_username") or "").strip()
        or str(claims.get("username") or "").strip()
        or None
    )
    return IdentityPrincipal(
        user_id=user_id,
        email=str(claims.get("email") or "").strip() or None,
        display_name=display_name,
        roles=tuple(roles_from_claims(claims, config)),
    )


async def principal_from_access_token(
    config: OidcProviderConfig,
    access_token: str,
) -> IdentityPrincipal:
    """Validate a bearer with UserInfo or, when configured, introspection."""

    token = str(access_token or "").strip()
    if not token:
        raise OidcAccessTokenRejected("OIDC access token is missing")
    document = await discover(config)
    if config.api_client_id:
        return await _principal_from_introspection(config, document, token)
    userinfo_url = str(document.get("userinfo_endpoint") or "").strip()
    if not userinfo_url:
        raise OidcProviderUnavailable(
            "OIDC discovery document has no userinfo_endpoint"
        )
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                config.to_internal(userinfo_url),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
            )
    except httpx.HTTPError as exc:
        raise OidcProviderUnavailable("OIDC UserInfo request failed") from exc
    if response.status_code in {400, 401, 403}:
        raise OidcAccessTokenRejected("OIDC access token was rejected")
    if not response.is_success:
        raise OidcProviderUnavailable(
            f"OIDC UserInfo returned status {response.status_code}"
        )
    try:
        claims = response.json()
    except ValueError as exc:
        raise OidcProviderUnavailable("OIDC UserInfo returned invalid JSON") from exc
    if not isinstance(claims, dict):
        raise OidcProviderUnavailable("OIDC UserInfo returned an invalid identity")
    return principal_from_claims(claims, config)


async def _principal_from_introspection(
    config: OidcProviderConfig,
    document: Mapping[str, Any],
    token: str,
) -> IdentityPrincipal:
    """Validate a machine token with RFC 7662 and preserve its granted scopes."""

    introspection_url = str(document.get("introspection_endpoint") or "").strip()
    if not introspection_url:
        raise OidcProviderMisconfigured(
            "OIDC API access requires an introspection_endpoint in provider discovery"
        )
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                config.to_internal(introspection_url),
                auth=(config.api_client_id, config.api_client_secret),
                data={"token": token, "token_type_hint": "access_token"},
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise OidcProviderUnavailable("OIDC token introspection failed") from exc
    if response.status_code in {401, 403}:
        raise OidcProviderMisconfigured(
            "OIDC token introspection rejected the configured API client"
        )
    if not response.is_success:
        raise OidcProviderUnavailable(
            f"OIDC token introspection returned status {response.status_code}"
        )
    try:
        claims = response.json()
    except ValueError as exc:
        raise OidcProviderUnavailable(
            "OIDC token introspection returned invalid JSON"
        ) from exc
    if not isinstance(claims, dict) or not isinstance(claims.get("active"), bool):
        raise OidcProviderMisconfigured(
            "OIDC token introspection returned no boolean active field"
        )
    if not claims["active"]:
        raise OidcAccessTokenRejected("OIDC access token is inactive")

    scope_value = claims.get("scope", "")
    if not isinstance(scope_value, str):
        raise OidcProviderMisconfigured(
            "OIDC token introspection returned a non-string scope field"
        )
    principal_claims = claims
    if not str(claims.get("sub") or "").strip():
        client_id = str(claims.get("client_id") or "").strip()
        if not client_id:
            raise OidcProviderMisconfigured(
                "OIDC token introspection returned neither sub nor client_id"
            )
        principal_claims = dict(claims)
        principal_claims["sub"] = f"oauth-client:{client_id}"
        if not str(principal_claims.get("username") or "").strip():
            principal_claims["username"] = client_id
    principal = principal_from_claims(principal_claims, config)
    return IdentityPrincipal(
        user_id=principal.user_id,
        email=principal.email,
        display_name=principal.display_name,
        org_id=principal.org_id,
        roles=principal.roles,
        api_scopes=tuple(dict.fromkeys(scope_value.split())),
    )


__all__ = [
    "IdentityPrincipal",
    "OidcAccessTokenRejected",
    "OidcProviderConfig",
    "OidcProviderMisconfigured",
    "OidcProviderUnavailable",
    "discover",
    "principal_from_access_token",
    "principal_from_claims",
    "roles_from_claims",
]
