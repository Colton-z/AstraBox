"""LiteLLM custom-auth adapter for AstraBox's shared OIDC identity.

The adapter recognises three credential forms of AstraBox's own: the embedded
gateway's private service credential, an AstraBox-signed browser capability, and
an access token from the same OIDC provider AstraBox uses.

It is NOT the gateway's only authenticator. ``custom_auth_settings.mode: auto``
in the proxy config makes LiteLLM fall back to its own key authentication when
this adapter declines, so LiteLLM virtual keys, their budgets and their route
scoping all keep working. That fallback is driven by the exception type: LiteLLM
re-raises ``ProxyException`` and falls back on anything else. So a credential
that is not AstraBox's raises ``_NotOurCredential`` and LiteLLM decides, while an
AstraBox credential that fails its own checks raises ``ProxyException`` and the
request ends here — a bad capability must not get a second hearing as a key.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sys
from pathlib import Path
from typing import Any

import jwt
from fastapi import Request
from litellm.proxy._types import LitellmUserRoles, ProxyException, UserAPIKeyAuth

# LiteLLM loads this file directly from the config path with
# ``spec_from_file_location``. That loader does not add the config directory to
# ``sys.path``, while the deliberately small adapter distribution keeps its
# provider-neutral helpers beside this module. Make that local module boundary
# importable without exposing the platform package to LiteLLM's venv.
_ADAPTER_DIR = str(Path(__file__).resolve().parent)
if _ADAPTER_DIR not in sys.path:
    sys.path.insert(0, _ADAPTER_DIR)

from astrabox_identity_session import read_session_signing_secret  # noqa: E402
from astrabox_litellm_auth import (
    CAPABILITY_PREFIX,
    ADMIN_UI_PURPOSE,
    AGENT_CONSOLE_PURPOSE,
    looks_like_access_token,
    verify_litellm_capability,
)  # noqa: E402
from astrabox_oidc import (
    OidcAccessTokenRejected,
    OidcProviderConfig,
    OidcProviderUnavailable,
    principal_from_access_token,
)  # noqa: E402

_PUBLIC_NO_CREDENTIAL_PATHS = {
    "/health/liveliness",
    "/health/readiness",
    "/.well-known/litellm-ui-config",
    "/litellm/.well-known/litellm-ui-config",
}

_identity_mode = str(os.getenv("ASTRABOX_WEB_IDENTITY") or "local").strip().lower()
_oidc_config = OidcProviderConfig.load() if _identity_mode == "oidc" else None
_capability_secret = read_session_signing_secret()


def _credential(value: Any) -> str:
    token = str(value or "").strip()
    if token.lower().startswith("bearer "):
        return token[7:].strip()
    return token


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class _NotOurCredential(Exception):
    """This credential is not AstraBox's; LiteLLM's own key auth should decide.

    Deliberately not a ``ProxyException``: under ``custom_auth_settings.mode:
    auto`` LiteLLM re-raises ``ProxyException`` and falls back on every other
    exception type. Raising this is how the adapter declines without deciding.
    """


def _auth_error(message: str, status_code: int = 401) -> ProxyException:
    return ProxyException(
        message=message,
        type="auth_error",
        param="api_key",
        code=status_code,
    )


def _result(
    *,
    token: str,
    user_id: str,
    email: str | None,
    org_id: str | None,
    user_role: LitellmUserRoles,
    allowed_routes: list[str] | None = None,
) -> UserAPIKeyAuth:
    # `token`, unlike `api_key`, is already an internal identifier. Store only a
    # digest so an opaque OIDC token or prefixed browser capability cannot appear
    # in LiteLLM request logs or spend records.
    return UserAPIKeyAuth(
        token=_token_hash(token),
        key_alias="astrabox-shared-identity",
        user_id=user_id,
        user_email=email,
        org_id=org_id,
        user_role=user_role,
        allowed_routes=allowed_routes,
    )


async def user_api_key_auth(request: Request, api_key: str) -> UserAPIKeyAuth:
    """Map AstraBox identity into LiteLLM's documented custom-auth object."""

    credential = _credential(api_key)
    if not credential and request.url.path in _PUBLIC_NO_CREDENTIAL_PATHS:
        return UserAPIKeyAuth(user_role=LitellmUserRoles.INTERNAL_USER_VIEW_ONLY)
    if not credential:
        raise _NotOurCredential("no credential")

    master_key = str(os.getenv("LITELLM_MASTER_KEY") or "").strip()
    if master_key and hmac.compare_digest(credential, master_key):
        return _result(
            token=credential,
            user_id="astrabox-service",
            email=None,
            org_id=None,
            user_role=LitellmUserRoles.PROXY_ADMIN,
        )

    if credential.startswith(CAPABILITY_PREFIX):
        try:
            claims = verify_litellm_capability(
                credential,
                secret=_capability_secret,
                purposes=(ADMIN_UI_PURPOSE, AGENT_CONSOLE_PURPOSE),
            )
        except jwt.PyJWTError as exc:
            raise _auth_error("AstraBox browser session is invalid or expired") from exc

        purpose = str(claims.get("use") or "")
        routes_value = claims.get("allowed_routes")
        allowed_routes = (
            [str(route) for route in routes_value if str(route).strip()]
            if isinstance(routes_value, list)
            else None
        )
        if purpose == ADMIN_UI_PURPOSE and "admin" not in {
            str(role) for role in claims.get("roles") or []
        }:
            raise _auth_error("AstraBox administrator role required", 403)
        if purpose == AGENT_CONSOLE_PURPOSE and not allowed_routes:
            raise _auth_error("Agent extension routes are missing", 403)
        return _result(
            token=credential,
            user_id=str(claims["sub"]),
            email=str(claims.get("email") or "").strip() or None,
            org_id=str(claims.get("org_id") or "").strip() or None,
            # LiteLLM's management handlers require proxy_admin. The signed
            # extension capability is additionally fenced by allowed_routes.
            user_role=LitellmUserRoles.PROXY_ADMIN,
            allowed_routes=allowed_routes,
        )

    # Everything left is an access token or it belongs to LiteLLM. Shape decides,
    # not exclusion: a LiteLLM virtual key reaching the OIDC provider would be
    # rejected there, and rejecting it here as "invalid access token" ends the
    # request before LiteLLM ever sees a key it issued.
    if _oidc_config is None or not looks_like_access_token(credential):
        raise _NotOurCredential("not an AstraBox credential")
    try:
        principal = await principal_from_access_token(_oidc_config, credential)
    except OidcAccessTokenRejected as exc:
        raise _auth_error("OIDC access token is invalid or expired") from exc
    except OidcProviderUnavailable as exc:
        raise _auth_error("OIDC provider is unavailable", 503) from exc

    role = (
        LitellmUserRoles.PROXY_ADMIN
        if "admin" in principal.roles
        else LitellmUserRoles.INTERNAL_USER_VIEW_ONLY
    )
    return _result(
        token=credential,
        user_id=principal.user_id,
        email=principal.email,
        org_id=principal.org_id,
        user_role=role,
    )


__all__ = [
    "ADMIN_UI_PURPOSE",
    "AGENT_CONSOLE_PURPOSE",
    "user_api_key_auth",
]
