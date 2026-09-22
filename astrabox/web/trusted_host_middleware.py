"""Trusted-host gate — a DNS-rebinding guard for the browser-facing surface.

The local deployment is deliberately no-auth on loopback (single user, the host
is yours). The one browser-borne risk that loopback binding does NOT close is
**DNS rebinding**: a hostile page the user visits rebinds its own domain to
``127.0.0.1`` and then drives this API from the victim's browser. Same-origin
policy does not stop it — to the browser the request IS same-origin; the only
tell is that the ``Host`` header still carries the attacker's domain.

This pure-ASGI middleware (mirrors :class:`~astrabox.web.identity_middleware.WebIdentityMiddleware`
— NOT ``BaseHTTPMiddleware``) rejects any HTTP/WS request whose ``Host`` is not on
a small allowlist (``ASTRABOX_ALLOWED_HOSTS``; default ``localhost`` / ``127.0.0.1``
/ ``[::1]``). Two surfaces are EXEMPT, mirroring the exempt-path pattern in
:mod:`astrabox.web.identity_middleware`:

* ``/api/v1/platform-mcp/*`` — the per-session sandbox containers call the server on
  this path with a bridge-IP / container-hostname ``Host``; that traffic is
  machine-to-machine, and DNS rebinding is a *browser* attack, so gating it would
  only break the sandbox's access to platform capabilities.
* ``/healthz`` — the liveness probe answers any ``Host`` by design.

Default posture is unchanged for a local user: the browser reaches the console as
``http://127.0.0.1:<port>`` / ``http://localhost:<port>``, both on the allowlist.
Exposing the stack behind a real hostname means adding it to
``ASTRABOX_ALLOWED_HOSTS`` (and, still, putting real authentication in front).
"""

from __future__ import annotations

import json
import os
from typing import Any

from starlette.routing import get_route_path

from astrabox.common.logger.logger_factory import get_logger

logger = get_logger(__name__)


#: Host values allowed on the browser-facing surface. ``testserver`` is the host
#: Starlette's in-process TestClient sends; keeping it in the default lets the
#: unit suite drive the app without a live socket. A real ``ASTRABOX_ALLOWED_HOSTS``
#: override REPLACES this list wholesale (production sets its real hostname(s), and
#: ``testserver`` naturally drops out).
_DEFAULT_ALLOWED_HOSTS: tuple[str, ...] = (
    "localhost",
    "127.0.0.1",
    "[::1]",
    "testserver",
)

#: Path prefixes exempt from the Host allowlist — machine-to-machine surfaces a
#: *browser* rebinding attack cannot reach. Each line states why it is exempt:
_EXEMPT_PREFIXES: tuple[str, ...] = (
    # Sandbox containers call the platform MCP route with a bridge-IP /
    # container-hostname Host; that is machine-to-machine, and rebinding is a
    # browser-only attack.
    "/api/v1/platform-mcp/",
    # The in-box runner's transcript flush (SpoolSessionStore → the
    # capability-token transcript routes) arrives the same way: bridge-IP /
    # Pod-IP Host, machine-to-machine, authenticated by the per-session HMAC
    # capability token embedded in the path — the Host check adds nothing a
    # forged token wouldn't already defeat, and blocks the flush on any
    # box-reachable address.
    "/api/v1/sbxcap/",
)


def _allowed_hosts() -> frozenset[str]:
    """Allowed Host values; ``ASTRABOX_ALLOWED_HOSTS`` (comma-separated) REPLACES
    the default list. Compared case-insensitively (hostnames are)."""
    override = str(os.getenv("ASTRABOX_ALLOWED_HOSTS", "") or "").strip()
    if override:
        return frozenset(
            part.strip().lower() for part in override.split(",") if part.strip()
        )
    return frozenset(host.lower() for host in _DEFAULT_ALLOWED_HOSTS)


def _is_exempt_path(path: str) -> bool:
    """Whether the Host allowlist is skipped for this path.

    Exact ``/healthz`` (the unauthenticated liveness probe) plus any
    machine-to-machine prefix in :data:`_EXEMPT_PREFIXES`.
    """
    if path == "/healthz":
        return True
    return any(path.startswith(prefix) for prefix in _EXEMPT_PREFIXES)


def _host_from_scope(scope: dict[str, Any]) -> str:
    """The request ``Host`` header, port-stripped and lower-cased (``""`` if absent).

    Handles the IPv6 literal form (``[::1]`` / ``[::1]:8088``) so the bracketed
    host survives port removal.
    """
    raw = ""
    for key, value in scope.get("headers") or []:
        try:
            if bytes(key).lower() == b"host":
                raw = bytes(value).decode("latin-1")
                break
        except Exception:
            continue
    host = raw.strip().lower()
    if not host:
        return ""
    if host.startswith("["):  # IPv6 literal: [::1] or [::1]:<port>
        end = host.find("]")
        return host[: end + 1] if end != -1 else host
    return host.split(":", 1)[0]


class TrustedHostMiddleware:
    """ASGI middleware that rejects requests carrying an off-allowlist ``Host``."""

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return

        # Route path, not raw scope["path"]: under a root_path deployment the
        # raw path carries the mount prefix and the machine exemptions
        # (sandbox->host bridges) would silently stop matching.
        path = get_route_path(scope) or "/"
        if _is_exempt_path(path):
            await self._app(scope, receive, send)
            return

        host = _host_from_scope(scope)
        if host in _allowed_hosts():
            await self._app(scope, receive, send)
            return

        logger.debug(
            "trusted-host reject: Host %r not in allowlist (path=%s)", host, path
        )
        await self._reject(scope, send, host)

    @staticmethod
    async def _reject(scope: dict[str, Any], send: Any, host: str) -> None:
        """Reject an off-allowlist Host: 400 for HTTP, policy-close 1008 for WS."""
        if scope.get("type") == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps(
            {
                "error": {
                    "code": "HOST_NOT_ALLOWED",
                    "message": f"Host {host!r} is not in the trusted-host allowlist",
                }
            }
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 400,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
