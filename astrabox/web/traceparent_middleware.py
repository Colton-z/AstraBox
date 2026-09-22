"""Per-request W3C ``traceparent`` binding + response header — the collapse of
the former ``_AgentPlatformTraceRoute`` ``APIRoute`` subclass into one middleware.

A pure-ASGI middleware (mirrors :class:`~astrabox.web.identity_middleware.WebIdentityMiddleware`
and :class:`~astrabox.web.trusted_host_middleware.TrustedHostMiddleware` — NOT
``BaseHTTPMiddleware``, whose buffering breaks streaming and whose task hop drops
request state). It does two things that would otherwise have to be duplicated
inline in every per-resource route class:

* **On the way in** — resolve the request's trace context
  (:func:`~astrabox.api.routes.http_common.resolve_request_trace_context`:
  an inbound ``traceparent`` trace-id is recovered, else a fresh one is minted)
  and stash it on ``request.state`` (``astrabox_trace_id`` / ``astrabox_rpc_id``),
  so every downstream consumer reads the SAME id — the route handlers'
  ``extract_trace_headers`` (outbound MCP hops) and the app-level exception
  handlers (log lines + error-envelope ``trace_id``).
* **On the way out** — set a ``traceparent`` response header (``setdefault``, so
  an inner handler that already set one wins) on EVERY response by wrapping the
  ASGI ``send``: the header rides the ``http.response.start`` event, so it lands
  on plain JSON, error envelopes AND streaming/SSE responses alike (a
  ``StreamingResponse`` emits exactly one ``http.response.start`` before its body
  chunks — see ``tests/traceparent_middleware_test.py``).

One response path this middleware structurally cannot reach: the app's catch-all
``Exception`` handler (``handle_unknown_error``) runs at Starlette's
``ServerErrorMiddleware``, which always wraps OUTSIDE every user middleware — so
its 500/503 responses (unexpected errors + mapped storage failures) never pass
through this ``send`` wrapper. That one handler sets its own ``traceparent``
header (reading the id THIS middleware stashed); see the note there.
"""

from __future__ import annotations

from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.requests import Request

from astrabox.api.routes.http_common import (
    _w3c_traceparent,
    resolve_request_trace_context,
)


class TraceparentMiddleware:
    """ASGI middleware: bind the request trace context + emit ``traceparent``."""

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        # Resolve + stash BEFORE the app runs, so the id survives even when the
        # inner app raises (the exception handlers read this same stash) and so
        # every reader within the request agrees on one trace id.
        request = Request(scope)
        trace_id, rpc_id = resolve_request_trace_context(request)
        request.state.astrabox_trace_id = trace_id
        request.state.astrabox_rpc_id = rpc_id

        traceparent = _w3c_traceparent(trace_id, rpc_id)

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).setdefault("traceparent", traceparent)
            await send(message)

        await self._app(scope, receive, send_wrapper)
