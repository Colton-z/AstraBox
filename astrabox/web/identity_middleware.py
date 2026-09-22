"""Per-request web identity binding — the wire between the identity seam and
every ``get_current_user_context`` reader.

A pure-ASGI middleware (NOT ``BaseHTTPMiddleware``, whose task hop can drop
ContextVars set in ``dispatch``): it runs the configured
:class:`~astrabox.seams.identity.WebIdentityResolver` once per HTTP/WS request
and, when it returns a :class:`UserContext`, binds it via
``set_current_user_context`` for the duration of the request — so the
handler call sites that read ``get_current_user_context`` observe the
authenticated identity with no change to any of them.

The built-in resolver asserts the deployment's one local administrator. An
authenticated resolver instead asserts the verified external identity and maps
its configured administrator group onto AstraBox's fixed internal role.
"""

from __future__ import annotations

import os
from typing import Any

from starlette.routing import get_route_path

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import (
    API_ADMIN_SCOPE,
    API_READ_SCOPE,
    API_WRITE_SCOPE,
    PLATFORM_ADMIN_ROLE,
    reset_current_user_context,
    set_current_user_context,
)

logger = get_logger(__name__)


#: Path prefixes a strict SSO resolver's rejection is downgraded on — machine /
#: capability surfaces that carry their OWN auth and must never be broken by
#: front-door identity. Each line states why it is self-authorizing:
_DEFAULT_EXEMPT_PREFIXES: tuple[str, ...] = (
    # Public share links: the capability is the signed link itself (no login).
    "/api/v1/share/",
    # Deployment triggers authenticate via their own credential (HMAC /
    # scheduler secret / channel provider auth); only trigger endpoints may
    # live under this prefix — admin CRUD stays under /api/v1/admin/**.
    "/api/v1/deployments/",
    # Sandbox->host callbacks are authorized by a per-sandbox capability token.
    "/api/v1/sandbox-callback/",
    # Sandbox->backend transcript writes carry a per-session capability token in
    # the path (/api/v1/sbxcap/{token}/...); the route verifies it. The
    # un-scoped /api/v1/transcript/ path is deliberately NOT exempt — it is
    # fail-closed unless the capability requirement is opted out.
    "/api/v1/sbxcap/",
    # Sandbox->platform MCP calls are authorized by the deployment id in the
    # path, which the route resolves against a stored binding before answering.
    # It is unguessable either way it is minted — a session uuid for an Agent, a
    # sha256 over scope/owner/user for an Assistant — so the id is the
    # capability, as it is for sbxcap above. This is the only sandbox-originated
    # MCP surface: every other server is dialled by the sandbox's own client and
    # never reaches this app.
    "/api/v1/platform-mcp/",
    # The built-in OIDC login flow IS the door: login/callback carry their own
    # protection (signed state+PKCE flow cookie, ID-token verification) and a
    # strict resolver 401 here would lock the user out of signing in.
    "/api/v1/auth/",
)


def _exempt_prefixes() -> tuple[str, ...]:
    """Auth-exempt path prefixes; ``ASTRABOX_AUTH_EXEMPT_PREFIXES`` REPLACES them.

    When the (comma-separated) env override is set it supplants the default
    prefix list wholesale. The exact rules in :func:`_is_exempt_path` that do
    not depend on this list (``/healthz`` and public static paths) always hold.
    """
    override = str(os.getenv("ASTRABOX_AUTH_EXEMPT_PREFIXES", "") or "").strip()
    if override:
        return tuple(part.strip() for part in override.split(",") if part.strip())
    return _DEFAULT_EXEMPT_PREFIXES


def _is_exempt_path(path: str) -> bool:
    """Whether a strict resolver's rejection is downgraded to "proceed anonymous".

    Two invariants hold even when the prefix list is overridden:

    * exact ``/healthz`` — the liveness probe is unauthenticated by design;
    * any path NOT under ``/api/`` — the SPA's static assets are public; every
      privileged surface lives behind ``/api/``.
    Plus any configured/default machine-capability prefix (see
    :data:`_DEFAULT_EXEMPT_PREFIXES`).
    """
    if path == "/healthz":
        return True
    if not path.startswith("/api/"):
        return True
    return any(path.startswith(prefix) for prefix in _exempt_prefixes())


def _is_admin_path(path: str) -> bool:
    """Admin console surface. The single ``/api/v1/admin`` prefix (deliberately no
    trailing slash) covers both ``/api/v1/admin/*`` and ``/api/v1/admin-api/*``."""
    return path.startswith("/api/v1/admin")


def _required_api_scope(scope: dict[str, Any], path: str) -> str | None:
    """Return the OAuth scope a machine identity needs for this route."""

    if not path.startswith("/api/") or _is_exempt_path(path):
        return None
    if _is_admin_path(path):
        return API_ADMIN_SCOPE
    if scope.get("type") == "websocket":
        return API_WRITE_SCOPE
    method = str(scope.get("method") or "GET").upper()
    if method == "OPTIONS":
        return None
    if method in {"GET", "HEAD"}:
        return API_READ_SCOPE
    return API_WRITE_SCOPE


def _headers_from_scope(scope: dict[str, Any]) -> dict[str, str]:
    """Lower-cased header mapping from an ASGI scope (last value wins).

    Reads ``scope['headers']`` only — never touches the receive channel, so the
    request body stays intact for the downstream app.
    """
    headers: dict[str, str] = {}
    for raw_key, raw_value in scope.get("headers") or []:
        try:
            key = bytes(raw_key).decode("latin-1").lower()
            value = bytes(raw_value).decode("latin-1")
        except Exception:
            continue
        headers[key] = value
    return headers


class WebIdentityMiddleware:
    """ASGI middleware that binds the resolved web identity per request."""

    def __init__(
        self,
        app: Any,
        resolver: Any,
        capability_bearer_paths: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self._app = app
        self._resolver = resolver
        #: Deployment-issued capability credentials the front door must not
        #: judge: for the named route path, a bearer carrying one of these
        #: prefixes skips the resolver and reaches the route UNSTAMPED — the
        #: route's own verifier accepts or refuses it (fail-closed; see the
        #: Agent MCP facade's `_caller`). Scoped to (path, prefix) pairs so
        #: the same bearer on any other path still meets the front door.
        self._capability_bearer_paths = dict(capability_bearer_paths or {})

    def _is_capability_bearer(self, path: str, headers: dict[str, str]) -> bool:
        prefixes = self._capability_bearer_paths.get(path)
        if not prefixes:
            return False
        presented = str(headers.get("authorization") or "").strip()
        if not presented.lower().startswith("bearer "):
            return False
        secret = presented[7:].strip()
        return secret.startswith(prefixes)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return

        headers = _headers_from_scope(scope)
        # Classify by the ROUTE path (root_path stripped), exactly as the
        # router sees it. Under a root_path deployment (ASGI sub-mount,
        # `uvicorn --root-path`, a prefixing reverse proxy) the raw
        # scope["path"] carries the mount prefix, every `/api/` prefix check
        # missed, and the whole API surface silently ran anonymous — the
        # failure mode of raw-path classification is auth-off, not an error.
        path = get_route_path(scope) or "/"
        if self._is_capability_bearer(path, headers):
            await self._app(scope, receive, send)
            return
        try:
            user = await self._resolver.resolve(headers)
        except APIError as exc:
            # A strict resolver rejected (missing/invalid credential). Exempt
            # machine/capability paths must NOT be broken by front-door SSO — they
            # carry their own auth, so they proceed unauthenticated. Every other
            # path is rejected with the mapped HTTP error.
            if not _is_exempt_path(path):
                await self._reject(scope, receive, send, exc)
                return
            user = None

        required_scope = _required_api_scope(scope, path)
        if (
            user is not None
            and user.api_scopes is not None
            and required_scope is not None
            and required_scope not in user.api_scopes
        ):
            await self._reject(
                scope,
                receive,
                send,
                APIError(
                    code="API_TOKEN_SCOPE_INSUFFICIENT",
                    message=f"API token requires scope {required_scope}",
                    status_code=403,
                    data={"required_scope": required_scope},
                ),
            )
            return

        # Admin-surface gate. With the no-auth DEFAULT resolver (single-user,
        # host-is-yours) the console stays open. The moment a real resolver is
        # configured, the admin surface hard-gates: anonymous never passes
        # (a strict resolver that returns None instead of raising must not
        # silently reopen the console), and an asserted identity needs the
        # admin role.
        if (
            user is None
            and _is_admin_path(path)
            and not getattr(self._resolver, "is_no_auth_default", False)
        ):
            await self._reject(
                scope,
                receive,
                send,
                APIError(
                    code="ADMIN_IDENTITY_REQUIRED",
                    message="admin surface requires an authenticated identity",
                    status_code=401,
                ),
            )
            return
        if (
            user is not None
            and _is_admin_path(path)
            and PLATFORM_ADMIN_ROLE not in user.roles
        ):
            await self._reject(
                scope,
                receive,
                send,
                APIError(
                    code="ADMIN_ROLE_REQUIRED",
                    message="admin role required",
                    status_code=403,
                ),
            )
            return

        token = set_current_user_context(user) if user is not None else None
        try:
            await self._app(scope, receive, send)
        finally:
            if token is not None:
                reset_current_user_context(token)

    @staticmethod
    async def _reject(scope: dict[str, Any], receive: Any, send: Any, exc: APIError) -> None:
        """Map any rejecting ``APIError`` to the response, honouring its status.

        Serves both the auth reject (401) and the admin-role gate (403): the HTTP
        status comes from ``exc.status_code`` (401 only as a last-resort default),
        and a websocket is closed with policy-violation 1008 in either case.
        """
        if scope.get("type") == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        import json

        from astrabox.common.utils.api_response import error_response

        # The standard platform envelope, exactly as route errors emit it —
        # including the exception's safe ``data`` payload (secret-free by the
        # APIError contract). A cookie/SSO resolver raising with
        # ``data.login_url`` needs that payload on the wire so the frontend
        # can drive a login redirect instead of a dead error screen.
        body = json.dumps(error_response(exc)).encode("utf-8")
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
        ]
        if int(getattr(exc, "status_code", 401) or 401) == 401 and (
            get_route_path(scope) or "/"
        ) == "/api/v1/mcp":
            headers.append(
                (b"www-authenticate", b'Bearer realm="astrabox-mcp"')
            )
        await send(
            {
                "type": "http.response.start",
                "status": int(getattr(exc, "status_code", 401) or 401),
                "headers": headers,
            }
        )
        await send({"type": "http.response.body", "body": body})
