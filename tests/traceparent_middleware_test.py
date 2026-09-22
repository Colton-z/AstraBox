"""The ``traceparent`` response header — the behavior collapsed out of the
former ``_AgentPlatformTraceRoute`` route class into
:class:`astrabox.web.traceparent_middleware.TraceparentMiddleware`.

Nothing pinned this invariant before, so a middleware that stopped emitting the
header would have failed silently. This file is the pin. It drives a small app wired EXACTLY like ``create_app``
does — the real middleware plus the real app-level exception handlers from
``api/routes/_shared`` — and asserts the invariant across every response shape:

* a plain 200, an SSE/``StreamingResponse`` stream, an ``APIError`` envelope, a
  422 validation envelope, and the two error paths that reach the outermost
  ``Exception`` handler (a mapped storage 503 and an unexpected 500) all carry a
  well-formed ``traceparent`` response header;
* an inbound ``traceparent`` propagates its trace-id (same id, fresh span);
  absent/garbage inbound mints a fresh one;
* the error envelope's own ``trace_id`` field is the SAME id the header carries
  (the middleware stash and the handler agree);
* attaching the SAME FastAPI request-span instrumentation ``init_tracing`` uses
  (``FastAPIInstrumentor``) does not crash and does not double the header — the
  traceparent middleware still owns exactly one ``traceparent``. (The instrumentation is
  attached with an isolated in-memory span exporter so the invariant is pinned
  without OTLP network export, which is fail-soft and tested separately.)

The storage-503 / unexpected-500 cases are served by ``handle_unknown_error``,
which Starlette dispatches at ``ServerErrorMiddleware`` (outside every user
middleware) and which RE-RAISES after responding — so those clients run with
``raise_server_exceptions=False`` (a live server sends the response and logs the
re-raise; the TestClient would otherwise surface it as an exception).
"""

from __future__ import annotations

import re

import pytest
from fastapi import Body, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.testclient import TestClient

from astrabox.api.routes._shared import (
    handle_api_error,
    handle_request_validation_error,
    handle_unknown_error,
)
from astrabox.common.utils.errors import APIError
from astrabox.web.traceparent_middleware import TraceparentMiddleware

_TRACEPARENT_RE = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-01$")


def _trace_id_field(traceparent: str | None) -> str:
    assert traceparent is not None, "response carried no traceparent header"
    parts = traceparent.split("-")
    assert len(parts) == 4, f"malformed traceparent: {traceparent!r}"
    return parts[1]


class _Payload(BaseModel):
    n: int


def _build_app() -> FastAPI:
    """A small app wired like ``create_app``: the traceparent middleware plus the
    real app-level exception handlers, over a handful of representative routes."""
    app = FastAPI()
    app.add_middleware(TraceparentMiddleware)
    app.add_exception_handler(APIError, handle_api_error)
    app.add_exception_handler(RequestValidationError, handle_request_validation_error)
    app.add_exception_handler(Exception, handle_unknown_error)

    @app.get("/ok")
    async def ok() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/sse")
    async def sse() -> StreamingResponse:
        async def gen():
            for i in range(3):
                yield f"data: {i}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/apierror")
    async def apierror() -> dict[str, str]:
        raise APIError(code="TEAPOT", message="no coffee", status_code=418)

    @app.get("/boom-storage")
    async def boom_storage() -> dict[str, str]:
        # Lazy: the unit lane has no pymongo; only the (importorskip-gated)
        # storage test drives this route.
        from pymongo.errors import AutoReconnect

        raise AutoReconnect("mongo unreachable")

    @app.get("/boom-unexpected")
    async def boom_unexpected() -> dict[str, str]:
        raise ValueError("kaboom")

    @app.post("/validate")
    async def validate(payload: _Payload = Body(...)) -> dict[str, int]:
        return {"n": payload.n}

    return app


@pytest.fixture()
def client() -> TestClient:
    # raise_server_exceptions=False: the Exception handler runs at
    # ServerErrorMiddleware, which re-raises after responding.
    return TestClient(_build_app(), raise_server_exceptions=False)


# ── (a) a normal 200 route ───────────────────────────────────────────────────


def test_plain_200_carries_well_formed_traceparent(client: TestClient) -> None:
    r = client.get("/ok")
    assert r.status_code == 200
    assert _TRACEPARENT_RE.match(r.headers.get("traceparent") or "")


def test_inbound_traceparent_trace_id_is_propagated(client: TestClient) -> None:
    inbound_tid = "a" * 32
    inbound = f"00-{inbound_tid}-{'b' * 16}-01"
    r = client.get("/ok", headers={"traceparent": inbound})
    out = r.headers.get("traceparent")
    assert _TRACEPARENT_RE.match(out or "")
    # Same trace-id field carried across the hop; the span is freshly minted.
    assert _trace_id_field(out) == inbound_tid
    assert out != inbound  # fresh span id


def test_absent_and_garbage_inbound_mint_a_fresh_traceparent(
    client: TestClient,
) -> None:
    fresh = _trace_id_field(client.get("/ok").headers.get("traceparent"))
    assert fresh and fresh != "a" * 32
    # A garbage inbound value is not trusted — a fresh trace-id is generated.
    garbage = client.get("/ok", headers={"traceparent": "not-a-traceparent"})
    assert _TRACEPARENT_RE.match(garbage.headers.get("traceparent") or "")


# ── (c) an SSE / streaming route ──────────────────────────────────────────────


def test_sse_stream_carries_traceparent_and_still_streams(client: TestClient) -> None:
    r = client.get("/sse")
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("text/event-stream")
    # The header rides http.response.start, ahead of the body chunks.
    assert _TRACEPARENT_RE.match(r.headers.get("traceparent") or "")
    assert r.text == "data: 0\n\ndata: 1\n\ndata: 2\n\n"


def test_sse_stream_propagates_inbound_trace_id(client: TestClient) -> None:
    inbound_tid = "c" * 32
    r = client.get("/sse", headers={"traceparent": f"00-{inbound_tid}-{'d' * 16}-01"})
    assert _trace_id_field(r.headers.get("traceparent")) == inbound_tid


# ── (b) an error-enveloped route: storage 503 via the app-level handler ───────


def test_storage_503_via_app_handler_carries_traceparent(client: TestClient) -> None:
    pytest.importorskip("pymongo")
    r = client.get("/boom-storage")
    assert r.status_code == 503
    body = r.json()
    # Error envelope shape is independent of the traceparent header.
    assert body["code"] == "PERSISTENCE_UNAVAILABLE"
    assert body["error"]["status_code"] == 503
    # …and it is still traced — the header handle_unknown_error stamps itself,
    # because the traceparent middleware structurally cannot wrap the
    # ServerErrorMiddleware layer.
    assert _TRACEPARENT_RE.match(r.headers.get("traceparent") or "")


def test_unexpected_500_envelope_trace_id_matches_header(client: TestClient) -> None:
    inbound_tid = "e" * 32
    r = client.get(
        "/boom-unexpected", headers={"traceparent": f"00-{inbound_tid}-{'f' * 16}-01"}
    )
    assert r.status_code == 500
    body = r.json()
    assert body["code"] == "UNEXPECTED_SERVER_ERROR"
    # The stashed id is the single source: envelope trace_id == header trace-id
    # == the propagated inbound trace-id.
    assert body["data"]["trace_id"] == inbound_tid
    assert _trace_id_field(r.headers.get("traceparent")) == inbound_tid


# ── APIError + validation envelopes (ExceptionMiddleware layer) ───────────────


def test_apierror_envelope_carries_traceparent(client: TestClient) -> None:
    r = client.get("/apierror")
    assert r.status_code == 418
    assert r.json()["code"] == "TEAPOT"
    assert _TRACEPARENT_RE.match(r.headers.get("traceparent") or "")


def test_validation_422_carries_traceparent(client: TestClient) -> None:
    # Proves the middleware owns the header for ExceptionMiddleware-dispatched
    # responses: handle_request_validation_error does not set it.
    r = client.post("/validate", json={"n": "not-an-int"})
    assert r.status_code == 422
    assert _TRACEPARENT_RE.match(r.headers.get("traceparent") or "")


# ── OTel instrumentation attached: no crash, no duplicate header ──────────────


def test_fastapi_instrumentation_does_not_crash_or_double_the_header() -> None:
    # The otel extra is not in CI's unit/mongo lanes (only .[dev] / .[mongo,dev]);
    # this coexistence pin runs wherever the extra is installed.
    pytest.importorskip("opentelemetry.instrumentation.fastapi")
    # init_tracing (opt-in) attaches FastAPIInstrumentor last → outermost, above
    # the traceparent middleware. Attach it here over an ISOLATED in-memory
    # provider (no global-state mutation, no OTLP network export) so this pins
    # only the coexistence invariant: instrumentation + the traceparent middleware
    # must not fight — one traceparent, the middleware's own, and no crash on any
    # response path.
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(InMemorySpanExporter()))
    app = _build_app()
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
    try:
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/ok")
        assert r.status_code == 200
        # Exactly one traceparent, and it is the traceparent middleware's own —
        # the instrumentation must not add a competing response header.
        assert len(r.headers.get_list("traceparent")) == 1
        assert _TRACEPARENT_RE.match(r.headers["traceparent"])
        # The error path through the ServerErrorMiddleware layer also survives
        # with instrumentation attached (the pymongo-free 500 route, so this pin
        # needs only the otel extra).
        e = client.get("/boom-unexpected")
        assert e.status_code == 500
        assert _TRACEPARENT_RE.match(e.headers.get("traceparent") or "")
    finally:
        FastAPIInstrumentor.uninstrument_app(app)
        provider.shutdown()
