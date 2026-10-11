"""Refuse requests that reach AstraBox at a path prefix it is not configured for.

AstraBox routes every request by its path below ``ASTRABOX_PUBLIC_PATH_PREFIX``
(:mod:`astrabox.deploy.public_path`). A gateway delivers that path in one of two
ways: a reverse proxy removes the prefix before forwarding, or an ASGI host
mounts the application at the prefix, which Starlette records as the request's
``root_path``. Two deployment mistakes break that contract:

* An ASGI host mounted AstraBox at a path other than the configured prefix. The
  request's ``root_path`` differs from the setting, so every URL AstraBox
  composes would point at the wrong place: ``PUBLIC_PATH_PREFIX_MISMATCH``.
* A reverse proxy forwarded the prefix instead of removing it. An API request, a
  probe or a request for a console file arrives with the prefix still in front
  of it: ``PUBLIC_PATH_PREFIX_NOT_REMOVED``.

Without this middleware either mistake surfaces as a console that never
finishes loading, or as links that lead nowhere, with nothing naming the
setting. It answers both with the platform error envelope instead, and logs the
refusal, so API clients and probes see the cause as well as browsers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from starlette.routing import get_route_path

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.deploy.public_path import PUBLIC_PATH_PREFIX_ENV, public_path_prefix

logger = get_logger(__name__)

# AstraBox paths a client requests directly, besides the API and the console's
# files. One of these behind the prefix can only be a forwarded prefix.
_DIRECT_PATHS = frozenset(
    {"/healthz", "/readyz", "/metrics", "/openapi.json", "/docs", "/redoc"}
)


class PublicPathMiddleware:
    """ASGI middleware enforcing how requests arrive below the public path prefix.

    *console_dist* is the built console directory the application serves, or
    ``None`` for an API-only server. A path below the prefix names a console
    file only when that file exists there.
    """

    def __init__(self, app: Any, *, console_dist: Path | None) -> None:
        self._app = app
        self._console_dist = console_dist

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return
        refusal = self._refusal(scope)
        if refusal is None:
            await self._app(scope, receive, send)
            return
        logger.warning("public path refusal: %s", refusal.message)
        await _reject(scope, send, refusal)

    def _refusal(self, scope: dict[str, Any]) -> APIError | None:
        prefix = public_path_prefix()
        mounted_at = str(scope.get("root_path") or "")
        if mounted_at not in ("", prefix):
            return APIError(
                code="PUBLIC_PATH_PREFIX_MISMATCH",
                message=(
                    f"AstraBox is mounted at {mounted_at}, but {PUBLIC_PATH_PREFIX_ENV} "
                    f"is {prefix or 'empty'}; set {PUBLIC_PATH_PREFIX_ENV}={mounted_at} "
                    "so the URLs AstraBox composes match where it is mounted"
                ),
                status_code=409,
                data={"root_path": mounted_at, "public_path_prefix": prefix},
            )
        if not prefix:
            return None
        route_path = get_route_path(scope) or "/"
        if not route_path.startswith(prefix + "/"):
            return None
        remainder = route_path[len(prefix) :]
        if not self._addressed_below_prefix(remainder):
            return None
        return APIError(
            code="PUBLIC_PATH_PREFIX_NOT_REMOVED",
            message=(
                f"the request for {route_path} still carries the public path prefix "
                f"{prefix}; the reverse proxy must remove {prefix} before forwarding, "
                f"because AstraBox serves {remainder} below it"
            ),
            status_code=404,
            data={"path": route_path, "public_path_prefix": prefix},
        )

    def _addressed_below_prefix(self, remainder: str) -> bool:
        """Whether *remainder* is an AstraBox API path, probe or console file.

        Only such a path proves the prefix was forwarded. A console route below
        a prefix that shares its first segment, such as ``/agents`` under the
        prefix ``/agents``, names none of these, so it passes.
        """

        if remainder.startswith("/api/") or remainder in _DIRECT_PATHS:
            return True
        if self._console_dist is None:
            return False
        candidate = (self._console_dist / remainder.lstrip("/")).resolve()
        return candidate.is_file() and candidate.is_relative_to(self._console_dist)


async def _reject(scope: dict[str, Any], send: Any, exc: APIError) -> None:
    """Answer with the platform error envelope, or close a websocket."""

    if scope.get("type") == "websocket":
        await send({"type": "websocket.close", "code": 1011})
        return
    from astrabox.common.utils.api_response import error_response

    body = json.dumps(error_response(exc)).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": exc.status_code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


__all__ = ["PublicPathMiddleware"]
