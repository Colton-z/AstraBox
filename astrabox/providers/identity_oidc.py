"""Built-in OIDC login for the console (``astrabox.web.identity`` name: ``oidc``).

The third config-only auth mode, completing the ladder:

* ``local`` — no auth (loopback, single user, the default);
* ``trusted_header`` / ``jwt`` — an external gateway or IdP you already run;
* ``oidc`` (this module) — **AstraBox itself runs the login dance** against any
  standard OIDC provider, so a deployment gets a real login page without
  fronting a proxy. The bundled compose profile points it at Casdoor; the same
  three env vars point it at Keycloak/Zitadel/your IdP unchanged.

Split of labor, deliberately:

* The **IdP owns everything credential-shaped** — login UI, passwords, MFA,
  social/enterprise brokering. No password ever transits this codebase.
* AstraBox owns only the OAuth authorization-code dance (PKCE + ``state``) and
  a **signed session cookie** minted after the IdP's ID token verifies. The
  cookie is a compact HS256 JWT under a deployment-local key — the same
  key-management shape as the vault master key: env override
  (``ASTRABOX_AUTH_SESSION_SECRET``) or a key generated once into
  ``<state_dir>/auth-session.key`` (mode 0600).

Split-horizon deployments (compose: the browser reaches the IdP at a published
host, the server reaches it by service name) set
``ASTRABOX_OIDC_INTERNAL_ISSUER``: discovery and all **server-side** calls
(token endpoint, JWKS) are rewritten onto it, while browser-facing redirects
and ID-token ``iss`` validation keep the public issuer.

Shared invariants with :mod:`astrabox.providers.identity_sso`:

* fail loud at construction on missing config (a broken auth config breaks
  boot, not requests);
* a *present* credential that fails verification is always a hard 401; the
  strict flag (default ON) only governs the absent case;
* config is read once at ``__init__``; per-request code reads request data.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlencode

import jwt

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import (
    API_ADMIN_SCOPE,
    PLATFORM_ADMIN_ROLE,
    UserContext,
)
from astrabox.identity.oidc import (
    OidcAccessTokenRejected,
    OidcProviderMisconfigured,
    OidcProviderConfig,
    OidcProviderUnavailable,
    _discovery_cache,
    discover,
    principal_from_access_token,
    roles_from_claims,
)
from astrabox.identity.session_signing import session_signing_secret

logger = get_logger(__name__)

__all__ = [
    "OidcProviderConfig",
    "OidcSessionWebIdentityResolver",
    "SESSION_COOKIE",
    "FLOW_COOKIE",
    "mint_session_token",
    "verify_session_token",
    "mint_flow_token",
    "verify_flow_token",
]

SESSION_COOKIE = "astrabox_session"
FLOW_COOKIE = "astrabox_auth_flow"

_DEFAULT_SESSION_TTL_SECONDS = 7 * 24 * 3600
_FLOW_TTL_SECONDS = 600

_FALSEY = {"0", "false", "no", "off"}


def _env(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or "").strip()

# ── session / flow tokens ──────────────────────────────────────────────────


def _session_ttl_seconds() -> int:
    raw = _env("ASTRABOX_AUTH_SESSION_TTL_SECONDS")
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return _DEFAULT_SESSION_TTL_SECONDS


def mint_session_token(
    *,
    user_id: str,
    email: str | None,
    display_name: str | None,
    roles: list[str],
) -> str:
    now = int(time.time())
    claims = {
        "use": "session",
        "sub": user_id,
        "email": email or None,
        "name": display_name or None,
        "roles": list(roles or []),
        "iat": now,
        "exp": now + _session_ttl_seconds(),
    }
    return jwt.encode(claims, session_signing_secret(), algorithm="HS256")


def verify_session_token(token: str) -> dict[str, Any]:
    """Decode+verify a session token; raises ``jwt`` errors on any failure."""
    claims = jwt.decode(token, session_signing_secret(), algorithms=["HS256"])
    if claims.get("use") != "session":
        raise jwt.InvalidTokenError("not a session token")
    return claims


def mint_flow_token(*, state: str, code_verifier: str, next_url: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "use": "flow",
            "state": state,
            "cv": code_verifier,
            "next": next_url,
            "iat": now,
            "exp": now + _FLOW_TTL_SECONDS,
        },
        session_signing_secret(),
        algorithm="HS256",
    )


def verify_flow_token(token: str) -> dict[str, Any]:
    claims = jwt.decode(token, session_signing_secret(), algorithms=["HS256"])
    if claims.get("use") != "flow":
        raise jwt.InvalidTokenError("not a flow token")
    return claims


def make_pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for PKCE S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode("ascii").rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


# ── the resolver ───────────────────────────────────────────────────────────


class OidcSessionWebIdentityResolver:
    """Authenticate console requests by the session cookie the login flow set.

    Browser requests use the signed ``astrabox_session`` cookie. Machine clients
    use an access token issued by the configured OIDC provider. Without an API
    introspection client, bearer tokens use UserInfo. Once that client is
    configured, every bearer token uses provider introspection and carries its
    granted scopes into route authorization; browser cookies remain role-based.
    Absent credentials are rejected in strict mode, and a presented invalid
    credential is always a hard 401.
    """

    def __init__(self) -> None:
        # Fail loud at boot when the mode is selected but unconfigured, and
        # materialize the signing key early so a read-only state dir surfaces
        # now rather than at first login.
        self._config = OidcProviderConfig.load()
        session_signing_secret()
        self._strict = _env("ASTRABOX_OIDC_STRICT").lower() not in _FALSEY

    @property
    def config(self) -> OidcProviderConfig:
        return self._config

    @staticmethod
    def login_url(next_url: str = "/") -> str:
        """Return the local login entry point for a same-origin destination."""

        return "/api/v1/auth/login?" + urlencode({"next": next_url})

    @staticmethod
    def _cookie_token(headers: Mapping[str, str]) -> str:
        header = str(headers.get("cookie", "") or "")
        for part in header.split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE and value:
                return value.strip()
        return ""

    @staticmethod
    def _bearer_token(headers: Mapping[str, str]) -> str:
        auth_header = str(headers.get("authorization", "") or "").strip()
        if auth_header.lower().startswith("bearer "):
            return auth_header[7:].strip()
        return ""

    async def resolve(self, headers: Mapping[str, str]) -> UserContext | None:
        cookie_token = self._cookie_token(headers)
        bearer_token = self._bearer_token(headers)
        if not cookie_token and not bearer_token:
            if self._strict:
                raise APIError(
                    code="AUTH_REQUIRED",
                    message="sign-in required",
                    status_code=401,
                )
            return None
        # Prefer the HttpOnly browser session when both are present. A mounted
        # management adapter may send its own scoped bearer credential while the
        # browser also carries this cookie.
        if cookie_token:
            try:
                claims = verify_session_token(cookie_token)
            except jwt.PyJWTError as exc:
                raise APIError(
                    code="AUTH_REQUIRED",
                    message=f"session invalid or expired: {exc}",
                    status_code=401,
                ) from exc
            user_id = str(claims.get("sub") or "").strip()
            if not user_id:
                raise APIError(
                    code="AUTH_REQUIRED",
                    message="session carries no subject",
                    status_code=401,
                )
            return UserContext(
                user_id=user_id,
                display_name=str(claims.get("name") or "").strip() or None,
                email=str(claims.get("email") or "").strip() or None,
                org_id=str(claims.get("org_id") or "").strip() or None,
                roles=[str(r) for r in claims.get("roles") or []],
            )

        try:
            principal = await principal_from_access_token(
                self._config, bearer_token
            )
        except OidcAccessTokenRejected as exc:
            raise APIError(
                code="AUTH_REQUIRED",
                message="OIDC access token is invalid or expired",
                status_code=401,
            ) from exc
        except OidcProviderMisconfigured as exc:
            raise APIError(
                code="IDENTITY_PROVIDER_MISCONFIGURED",
                message=str(exc),
                status_code=502,
            ) from exc
        except OidcProviderUnavailable as exc:
            raise APIError(
                code="IDENTITY_PROVIDER_UNAVAILABLE",
                message="OIDC provider could not validate the access token",
                status_code=503,
            ) from exc
        roles = list(principal.roles)
        if (
            principal.api_scopes is not None
            and API_ADMIN_SCOPE in principal.api_scopes
            and PLATFORM_ADMIN_ROLE not in roles
        ):
            roles.append(PLATFORM_ADMIN_ROLE)
        return UserContext(
            user_id=principal.user_id,
            display_name=principal.display_name,
            email=principal.email,
            org_id=principal.org_id,
            roles=roles,
            api_scopes=(
                list(principal.api_scopes)
                if principal.api_scopes is not None
                else None
            ),
        )
