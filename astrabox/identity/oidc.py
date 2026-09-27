"""Provider-neutral OIDC discovery, validation, and claim mapping.

This module deliberately has no dependency on AstraBox's API, persistence, or
service layers, so multiple authentication adapters can share one OIDC
implementation.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, replace
from typing import Any, Mapping

import httpx
import jwt

_DEFAULT_SCOPES = "openid profile email"
_DEFAULT_GROUPS_CLAIM = "groups"
_DEFAULT_ADMIN_GROUP = "astrabox-admin"
# The scope that makes a machine client an administrator. This file is copied
# into the model gateway, which has no AstraBox package, so the scope and the
# `admin` role are spelled here rather than imported (see roles_from_claims).
_API_SCOPE_PREFIX = "astrabox:"
_API_ADMIN_SCOPE = "astrabox:admin"

logger = logging.getLogger(__name__)


class OidcAccessTokenRejected(Exception):
    """The identity provider rejected a presented access token."""


class OidcProviderUnavailable(Exception):
    """The identity provider could not currently validate a token."""


class OidcIdentityRejected(Exception):
    """The provider vouched for an identity this deployment does not accept.

    The token is valid and the provider answered; the account belongs to
    another organization or client. Signing in with an account this deployment
    accepts is the remedy, which neither a new token for the same account nor
    waiting provides.
    """


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
    casdoor_organization: str

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
            casdoor_organization=_env("ASTRABOX_CASDOOR_ORGANIZATION"),
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


def require_casdoor_organization(
    claims: Mapping[str, Any], config: OidcProviderConfig
) -> None:
    """Refuse a Casdoor user from any organization but the configured one.

    Casdoor names a user's organization in the ``owner`` claim of the tokens it
    issues, and lets the users of its ``built-in`` organization, its global
    administrators, sign in to every application, this deployment's included.
    Without a configured organization (another provider) nothing is checked.
    """

    expected = config.casdoor_organization
    if not expected:
        return
    owner = str(claims.get("owner") or "").strip()
    if owner != expected:
        raise OidcIdentityRejected(
            f"the account belongs to Casdoor organization {owner or '(none)'!r}; "
            f"this deployment accepts only organization {expected!r} "
            "(ASTRABOX_CASDOOR_ORGANIZATION)"
        )


_jwks_clients: dict[str, jwt.PyJWKClient] = {}


async def _verified_casdoor_token_claims(
    config: OidcProviderConfig, document: Mapping[str, Any], token: str
) -> dict[str, Any]:
    """The claims of a Casdoor JWT access token, after checking its signature.

    UserInfo and introspection answers do not name the organization, and the
    access token does: Casdoor issues the bundled applications' access tokens
    as JWTs carrying the same user claims as the ID token.
    """

    jwks_uri = str(document.get("jwks_uri") or "").strip()
    if not jwks_uri:
        raise OidcProviderMisconfigured("OIDC discovery document has no jwks_uri")
    url = config.to_internal(jwks_uri)
    client = _jwks_clients.get(url)
    if client is None:
        client = _jwks_clients[url] = jwt.PyJWKClient(url)

    def _decode() -> dict[str, Any]:
        signing_key = client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            issuer=config.issuer,
            options={"verify_aud": False},
            leeway=30,
        )

    try:
        return await asyncio.to_thread(_decode)
    except jwt.PyJWKClientConnectionError as exc:
        raise OidcProviderUnavailable("Casdoor signing keys could not be fetched") from exc
    except jwt.PyJWTError as exc:
        raise OidcProviderMisconfigured(
            "ASTRABOX_CASDOOR_ORGANIZATION needs Casdoor access tokens in a JWT "
            f"format that carries the owner claim, and this one could not be read: {exc}"
        ) from exc


async def _require_casdoor_access_token(
    config: OidcProviderConfig,
    document: Mapping[str, Any],
    token: str,
) -> dict[str, Any] | None:
    """Refuse a bearer token from another organization or another client.

    A client-credentials token names no user: Casdoor issues it with ``type``
    ``application`` and the application's owner, which is ``admin`` for every
    application. Such a token is accepted only from the configured API client,
    the application the bundled seed registers in the configured organization.
    """

    if not config.casdoor_organization:
        return None
    claims = await _verified_casdoor_token_claims(config, document, token)
    if str(claims.get("type") or "") == "application":
        client_id = str(claims.get("azp") or "").strip()
        if not config.api_client_id or client_id != config.api_client_id:
            raise OidcIdentityRejected(
                f"the access token was issued to OAuth client {client_id or '(none)'!r}; "
                "this deployment accepts client tokens only from "
                f"{config.api_client_id or 'a configured API client'!r} "
                "(ASTRABOX_OIDC_API_CLIENT_ID)"
            )
        return claims
    require_casdoor_organization(claims, config)
    return claims


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
        principal = await _principal_from_introspection(config, document, token)
        return await _with_casdoor_checks(config, document, token, principal)
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
    principal = principal_from_claims(claims, config)
    return await _with_casdoor_checks(config, document, token, principal)


async def _with_casdoor_checks(
    config: OidcProviderConfig,
    document: Mapping[str, Any],
    token: str,
    principal: IdentityPrincipal,
) -> IdentityPrincipal:
    """Apply the Casdoor organization check, and read a user's groups from it.

    Casdoor's introspection answer carries no groups, and its access token
    carries the same groups as the ID token the browser signs in with, so a
    user's token takes its role from the access token, as a browser sign-in
    takes it from the ID token.
    """

    claims = await _require_casdoor_access_token(config, document, token)
    if claims is None or principal.api_scopes is not None:
        return principal
    return replace(principal, roles=tuple(roles_from_claims(claims, config)))


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
    scopes = tuple(dict.fromkeys(scope_value.split()))
    # Who the token was issued to decides what its scopes mean. A user can ask
    # the console's client for any scope, and a provider may grant it (Casdoor
    # does), so AstraBox API scopes count only on a token issued to the API
    # client, whose secret the deployment holds. A token issued to the console
    # client is the user's own, and carries the user's role from their groups
    # and nothing more, as a browser sign-in does.
    client_id = str(claims.get("client_id") or "").strip()
    if client_id and client_id == config.api_client_id:
        principal_claims = claims
        if not str(claims.get("sub") or "").strip():
            principal_claims = dict(claims)
            principal_claims["sub"] = f"oauth-client:{client_id}"
            if not str(principal_claims.get("username") or "").strip():
                principal_claims["username"] = client_id
        principal = principal_from_claims(principal_claims, config)
        roles = principal.roles
        if _API_ADMIN_SCOPE in scopes and "admin" not in roles:
            roles = (*roles, "admin")
        return replace(principal, roles=roles, api_scopes=scopes)
    if client_id and client_id == config.client_id:
        ignored = [scope for scope in scopes if scope.startswith(_API_SCOPE_PREFIX)]
        if ignored:
            logger.warning(
                "OIDC access token for %s: ignoring scopes %s; a token issued to the "
                "console client %r carries the user's role, and API scopes count only "
                "on tokens issued to the API client %r",
                str(claims.get("sub") or "(no subject)"),
                " ".join(ignored),
                config.client_id,
                config.api_client_id,
            )
        return principal_from_claims(claims, config)
    raise OidcIdentityRejected(
        f"the access token was issued to OAuth client {client_id or '(not named)'!r}; "
        f"this deployment accepts tokens issued to the console client {config.client_id!r} "
        f"(ASTRABOX_OIDC_CLIENT_ID) or the API client {config.api_client_id!r} "
        "(ASTRABOX_OIDC_API_CLIENT_ID)"
    )


__all__ = [
    "IdentityPrincipal",
    "OidcAccessTokenRejected",
    "OidcIdentityRejected",
    "OidcProviderConfig",
    "OidcProviderMisconfigured",
    "OidcProviderUnavailable",
    "discover",
    "principal_from_access_token",
    "principal_from_claims",
    "require_casdoor_organization",
    "roles_from_claims",
]
