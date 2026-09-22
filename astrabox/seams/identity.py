"""HTTP identity resolver seam (``astrabox.web.identity``).

One resolver authenticates every browser, API, and public MCP HTTP request.
The identity middleware binds its result before routing, so protocol facades
and resource handlers share the same deployment identity and authorization
policy.

This module imports only the standard library + typing; ``UserContext`` is
referenced only under ``TYPE_CHECKING`` so the contract stays import-light.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Mapping, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing-only import, no runtime dependency
    from astrabox.common.utils.user_context import UserContext

__all__ = ["WebIdentityResolver"]


@runtime_checkable
class WebIdentityResolver(Protocol):
    """Resolve one HTTP or WebSocket caller from request headers.

    The implementation is loaded by name from ``astrabox.web.identity``. An
    ASGI middleware runs it once per request and binds the returned
    :class:`UserContext` for every downstream handler.

    Resolution contract:

    * a :class:`UserContext` — the authenticated user (bound for the request);
    * ``None`` — "no identity asserted"; the request proceeds anonymous and the
      reader falls through to the deployment's default local identity;
    * raise an ``APIError`` with status ``401`` — reject the request. A strict
      deployment's resolver raises here for a missing or invalid credential.

    Browser entry points that need to initiate authentication call
    :meth:`login_url`. An OIDC resolver returns its same-origin login route;
    resolvers whose ingress owns login return ``None``.
    """

    async def resolve(self, headers: Mapping[str, str]) -> "UserContext | None":
        """Return the :class:`UserContext` for a web request's *headers*, or
        ``None`` when no identity is asserted (see the class contract)."""
        ...

    def login_url(self, next_url: str = "/") -> str | None:
        """Return a browser login URL for *next_url*, or ``None`` when the
        deployment's ingress owns login."""
        ...
