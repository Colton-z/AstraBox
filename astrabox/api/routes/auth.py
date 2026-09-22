"""The built-in OIDC login flow (``/api/v1/auth/*``).

Registered only when ``ASTRABOX_WEB_IDENTITY=oidc`` — these routes are the
door itself, so the identity middleware exempts the prefix and each endpoint
carries its own protection: ``state`` + PKCE ride a short-lived signed flow
cookie, and the callback verifies the IdP's ID token (signature via JWKS,
audience, issuer) before minting the session cookie the
:class:`~astrabox.providers.identity_oidc.OidcSessionWebIdentityResolver`
authenticates every subsequent request with.

No password and no IdP access token is ever stored: the session cookie carries
only identity claims (sub/email/name/roles) under the deployment's own
signing key.
"""

from __future__ import annotations

import asyncio
import hmac
import secrets
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from jwt import PyJWKClient

from astrabox.common.logger.logger_factory import get_logger
from astrabox.providers.identity_oidc import (
    FLOW_COOKIE,
    SESSION_COOKIE,
    OidcProviderConfig,
    discover,
    make_pkce_pair,
    mint_flow_token,
    mint_session_token,
    roles_from_claims,
    verify_flow_token,
    verify_session_token,
)

logger = get_logger(__name__)

_registered_on: int | None = None


def _safe_next(raw: str | None) -> str:
    """Return ``raw`` only when it is a same-origin absolute path, else ``/``.

    Anything else — an absolute URL, or a protocol-relative ``//host`` path —
    would turn the post-login redirect into an open redirect.
    """
    value = str(raw or "").strip()
    if value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


def _redirect_uri(request: Request, config: OidcProviderConfig) -> str:
    if config.redirect_url_override:
        return config.redirect_url_override
    return str(request.base_url).rstrip("/") + "/api/v1/auth/callback"


def _cookie_secure(request: Request, config: OidcProviderConfig) -> bool:
    if config.redirect_url_override:
        return urlsplit(config.redirect_url_override).scheme == "https"
    return request.url.scheme == "https"


def _auth_error(status: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"success": False, "code": "AUTH_FLOW_FAILED", "message": message},
    )


def register_auth_routes(app: FastAPI) -> None:
    global _registered_on
    if _registered_on == id(app):
        return
    _registered_on = id(app)

    # Config is resolved once, at registration — same fail-loud posture as the
    # resolver: a deployment that selects oidc but misconfigures it breaks at
    # boot, not at first login.
    config = OidcProviderConfig.load()

    @app.get("/api/v1/auth/login")
    async def auth_login(request: Request) -> Response:
        document = await discover(config)
        state = secrets.token_urlsafe(24)
        code_verifier, code_challenge = make_pkce_pair()
        next_url = _safe_next(request.query_params.get("next"))
        params = {
            "response_type": "code",
            "client_id": config.client_id,
            "redirect_uri": _redirect_uri(request, config),
            "scope": config.scopes,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        url = str(document["authorization_endpoint"]) + "?" + urlencode(params)
        response = RedirectResponse(url, status_code=302)
        response.set_cookie(
            FLOW_COOKIE,
            mint_flow_token(state=state, code_verifier=code_verifier, next_url=next_url),
            max_age=600,
            httponly=True,
            samesite="lax",
            secure=_cookie_secure(request, config),
            path="/api/v1/auth/",
        )
        return response

    @app.get("/api/v1/auth/callback")
    async def auth_callback(request: Request) -> Response:
        error = str(request.query_params.get("error") or "").strip()
        if error:
            description = str(request.query_params.get("error_description") or "").strip()
            return _auth_error(401, f"identity provider rejected the login: {error} {description}".strip())

        code = str(request.query_params.get("code") or "").strip()
        state = str(request.query_params.get("state") or "").strip()
        flow_cookie = str(request.cookies.get(FLOW_COOKIE) or "").strip()
        if not code or not state or not flow_cookie:
            return _auth_error(400, "auth callback missing code/state/flow cookie — restart at /api/v1/auth/login")
        try:
            flow = verify_flow_token(flow_cookie)
        except jwt.PyJWTError as exc:
            return _auth_error(401, f"login flow expired or invalid ({exc}) — restart at /api/v1/auth/login")
        if not hmac.compare_digest(str(flow.get("state") or ""), state):
            return _auth_error(401, "state mismatch — restart at /api/v1/auth/login")

        document = await discover(config)
        token_endpoint = config.to_internal(str(document["token_endpoint"]))
        async with httpx.AsyncClient(timeout=15.0) as client:
            token_response = await client.post(
                token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": _redirect_uri(request, config),
                    "client_id": config.client_id,
                    "client_secret": config.client_secret,
                    "code_verifier": str(flow.get("cv") or ""),
                },
            )
        if token_response.status_code != 200:
            logger.warning(
                "oidc token exchange failed: status=%s body=%s",
                token_response.status_code,
                token_response.text[:400],
            )
            return _auth_error(502, f"token exchange failed with status {token_response.status_code}")
        id_token = str((token_response.json() or {}).get("id_token") or "").strip()
        if not id_token:
            return _auth_error(502, "identity provider returned no id_token")

        jwks_uri = config.to_internal(str(document["jwks_uri"]))

        def _verify() -> dict[str, Any]:
            signing_key = PyJWKClient(jwks_uri).get_signing_key_from_jwt(id_token)
            return jwt.decode(
                id_token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                audience=config.client_id,
                issuer=config.issuer,
                leeway=30,
            )

        try:
            claims = await asyncio.to_thread(_verify)
        except jwt.PyJWTError as exc:
            logger.warning("oidc id_token verification failed: %s", exc)
            return _auth_error(401, f"id_token verification failed: {exc}")

        user_id = str(claims.get("sub") or "").strip()
        if not user_id:
            return _auth_error(401, "id_token carries no subject")
        # displayName first: Casdoor carries the human name there and puts the
        # login name in `name`; standard IdPs use `name`/`preferred_username`.
        display_name = (
            str(claims.get("displayName") or "").strip()
            or str(claims.get("name") or "").strip()
            or str(claims.get("preferred_username") or "").strip()
            or None
        )
        session_token = mint_session_token(
            user_id=user_id,
            email=str(claims.get("email") or "").strip() or None,
            display_name=display_name,
            roles=roles_from_claims(claims, config),
        )
        response = RedirectResponse(_safe_next(str(flow.get("next") or "/")), status_code=302)
        response.set_cookie(
            SESSION_COOKIE,
            session_token,
            httponly=True,
            samesite="lax",
            secure=_cookie_secure(request, config),
            path="/",
        )
        response.delete_cookie(FLOW_COOKIE, path="/api/v1/auth/")
        return response

    @app.post("/api/v1/auth/logout")
    async def auth_logout(request: Request) -> Response:
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/api/v1/auth/session")
    async def auth_session(request: Request) -> JSONResponse:
        """Session probe for the console: which user, if any. Never 401s —
        the frontend uses it to decide whether to show the sign-in state."""
        token = str(request.cookies.get(SESSION_COOKIE) or "").strip()
        if not token:
            return JSONResponse({"authenticated": False})
        try:
            claims = verify_session_token(token)
        except jwt.PyJWTError:
            return JSONResponse({"authenticated": False})
        return JSONResponse(
            {
                "authenticated": True,
                "user": {
                    "user_id": str(claims.get("sub") or ""),
                    "display_name": claims.get("name"),
                    "email": claims.get("email"),
                    "roles": list(claims.get("roles") or []),
                },
            }
        )
