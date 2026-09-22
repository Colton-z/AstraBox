"""Cross-cutting error-mapping machinery shared by the resource routers.

Holds the lazy platform-service accessor (``_svc``), the request-identity
accessor (``_resolve_user``), the JSON error-response helpers, the SSE
first-event timeout helper, and the app-level exception-handler coroutines that
:func:`astrabox.api.app.create_app` registers via ``app.add_exception_handler(...)``.

Trace binding + the ``traceparent`` response header ride one ASGI middleware
(:class:`astrabox.web.traceparent_middleware.TraceparentMiddleware`), which
stashes the trace id on ``request.state`` on the way in and sets the header on
the way out for every response — plain JSON, error envelopes and SSE streams.
The one response path that middleware cannot wrap is the catch-all ``Exception``
handler below (``handle_unknown_error`` runs at Starlette's outermost
``ServerErrorMiddleware``), so that handler sets its own header.

This module is a leaf with respect to the rest of :mod:`astrabox.api.routes`:
it imports only from :mod:`astrabox.api.routes.http_common` (never from a
sibling route module), and every other module in this package may import
from here — that one-directional rule is what keeps the package's internal
dependency graph acyclic.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.api_response import error_response
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import (
    UserContext,
    get_current_user_context,
)
from astrabox.core.service.orchestrator.service_registry import get_platform_service
from astrabox.api.routes.http_common import (
    _w3c_traceparent,
    map_storage_api_error,
    map_unexpected_api_error,
    resolve_request_trace_context,
)


logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Lazy platform-service accessor                                                #
# --------------------------------------------------------------------------- #
# ``get_platform_service()`` constructs the whole service graph, so it must not
# run at import of this module, or ``create_app()`` (which imports this module to
# mount the routers) would not be import-time constructible (the clean-boot
# invariant). Instead every handler resolves the service through this accessor.
# ``get_platform_service()`` is itself a memoized singleton, so this is "construct
# once, on first request", not "construct per request".
def _svc() -> Any:
    return get_platform_service()


# --------------------------------------------------------------------------- #
# Error-response helpers                                                        #
# --------------------------------------------------------------------------- #
def _json_api_error_response(exc: APIError) -> JSONResponse:
    return JSONResponse(
        content=error_response(exc),
        status_code=exc.status_code,
    )


def _json_mapped_storage_error_response(exc: Exception) -> JSONResponse | None:
    mapped = map_storage_api_error(exc)
    if mapped is None:
        return None
    return JSONResponse(
        content=error_response(mapped),
        status_code=mapped.status_code,
    )


def _json_unexpected_error_response(
    exc: Exception,
    *,
    path: str,
    trace_id: str,
    rpc_id: str,
) -> JSONResponse:
    mapped = map_unexpected_api_error(
        exc,
        path=path,
        trace_id=trace_id,
        rpc_id=rpc_id,
    )
    return JSONResponse(
        content=error_response(mapped),
        status_code=mapped.status_code,
    )


def _stream_first_event_timeout_seconds() -> float:
    raw = str(os.getenv("ASTRABOX_STREAM_START_TIMEOUT_SECONDS", "")).strip()
    if not raw:
        return 5.0
    try:
        value = float(raw)
    except ValueError:
        return 5.0
    if value <= 0:
        return 5.0
    return value


# --------------------------------------------------------------------------- #
# Application-level exception handlers (registered on the app by create_app)    #
# --------------------------------------------------------------------------- #
# There is no module-level app in this module, so these are plain coroutines
# that :func:`astrabox.api.app.create_app` registers via
# ``app.add_exception_handler(...)``.
async def handle_api_error(request: Request, exc: APIError):
    trace_id, rpc_id = resolve_request_trace_context(request)
    logger.warning(
        "api error: code=%s path=%s trace_id=%s rpc_id=%s",
        exc.code,
        request.url.path,
        trace_id,
        rpc_id,
    )
    return _json_api_error_response(exc)


async def handle_request_validation_error(request: Request, exc: RequestValidationError):
    trace_id, rpc_id = resolve_request_trace_context(request)
    logger.warning(
        "request validation error: path=%s trace_id=%s rpc_id=%s err=%s",
        request.url.path,
        trace_id,
        rpc_id,
        exc,
    )
    # No explicit traceparent header here: RequestValidationError is dispatched
    # by Starlette's ExceptionMiddleware, which sits inside every user
    # middleware, so TraceparentMiddleware's send wrapper stamps this response
    # on the way out (verified in tests/traceparent_middleware_test.py).
    return JSONResponse(
        content=error_response("INVALID_REQUEST", str(exc)),
        status_code=422,
    )


async def handle_unknown_error(request: Request, exc: Exception):
    trace_id, rpc_id = resolve_request_trace_context(request)
    logger.exception(
        "unexpected error: path=%s trace_id=%s rpc_id=%s err=%s",
        request.url.path,
        trace_id,
        rpc_id,
        exc,
    )
    response = _json_mapped_storage_error_response(exc)
    if response is None:
        response = _json_unexpected_error_response(
            exc,
            path=request.url.path,
            trace_id=trace_id,
            rpc_id=rpc_id,
        )
    # This handler is the one traceparent path TraceparentMiddleware cannot own:
    # it is registered for ``Exception`` (→ Starlette's ServerErrorMiddleware,
    # the outermost wrapper above every user middleware), so its 500/503
    # responses — unexpected errors and mapped storage failures — never pass
    # through the middleware's send wrapper. Stamp the header here, using the id
    # the middleware stashed on request.state, so these responses stay traceable
    # exactly like the rest.
    response.headers.setdefault("traceparent", _w3c_traceparent(trace_id, rpc_id))
    return response


async def _resolve_user(request: Request) -> UserContext:
    """Resolve the request's user through the identity seam.

    The web identity middleware (:mod:`astrabox.web.identity_middleware`) has
    already run the configured ``astrabox.web.identity`` resolver for this
    request and bound any resolved identity; this reads it back. The default
    resolver asserts no identity, so every request is the single local user;
    register an alternate resolver at the ``astrabox.web.identity`` entry-point
    to add authentication with no change to this call site.
    """
    return await get_current_user_context(request)
