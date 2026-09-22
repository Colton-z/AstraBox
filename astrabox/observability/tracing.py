"""Opt-in distributed tracing → OTLP (bring-your-own OpenTelemetry backend).

This module bootstraps an OpenTelemetry ``TracerProvider`` and attaches FastAPI
request-span instrumentation. It does not create custom turn, session, or LLM
spans.

Off by default — zero overhead, zero behavior change when unconfigured
---------------------------------------------------------------------
:func:`init_tracing` reads ``OTEL_EXPORTER_OTLP_ENDPOINT`` as its on/off guard.
With it unset (every default deployment) the function returns before importing or
constructing anything, so an unconfigured install creates no tracing provider or
instrumentation.

Turn it on by pointing ``OTEL_EXPORTER_OTLP_ENDPOINT`` at any OTLP/HTTP receiver.
For a BYO Langfuse instance that is ``<LANGFUSE_HOST>/api/public/otel`` plus
``OTEL_EXPORTER_OTLP_HEADERS`` carrying the ``Authorization=Basic <...>``
credential — the OTLP exporter reads both of those standard OpenTelemetry env
vars itself (the endpoint for the wire target, the headers for auth); this module
does not re-plumb them into the exporter.

Fail-soft
---------
Tracing is an observability side-channel, never load-bearing for a turn, so every
failure path here is caught and logged rather than raised: a missing ``otel``
extra, an exporter that cannot be built, an instrumentation error — none of them
may crash boot or change request behavior. (Export itself is async/lazy under the
``BatchSpanProcessor``, so an unreachable collector never blocks a request
either.)
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from astrabox.common.logger.logger_factory import get_logger

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = get_logger(__name__)


def init_tracing(app: FastAPI) -> None:
    """Wire OpenTelemetry OTLP tracing onto ``app`` — only when configured.

    No-op (returns immediately, imports nothing) unless
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set. When set, builds a
    ``TracerProvider`` (``service.name`` from ``OTEL_SERVICE_NAME``, default
    ``astrabox``) exporting over OTLP/HTTP via a ``BatchSpanProcessor`` and
    installs FastAPI request-span instrumentation. Any failure is logged and
    swallowed; it never raises.
    """
    endpoint = str(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or "").strip()
    if not endpoint:
        # Off by default: no provider, no imports, no overhead.
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning(
            "OTEL_EXPORTER_OTLP_ENDPOINT is set (%s) but the otel extra is not "
            "installed; distributed tracing stays off. Install it with: "
            "pip install 'astrabox[otel]'.",
            endpoint,
        )
        return

    try:
        service_name = str(os.getenv("OTEL_SERVICE_NAME") or "").strip() or "astrabox"
        resource = Resource.create({"service.name": service_name})
        provider = TracerProvider(resource=resource)
        # The exporter reads OTEL_EXPORTER_OTLP_ENDPOINT + OTEL_EXPORTER_OTLP_HEADERS
        # from the environment itself (endpoint → wire target, headers → auth).
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        FastAPIInstrumentor.instrument_app(app)
    except Exception:
        # Fail-soft: tracing must never crash boot.
        logger.warning(
            "OpenTelemetry tracing setup failed; continuing without tracing.",
            exc_info=True,
        )
        return

    # A missing/invalid Langfuse Basic-auth header is the most common BYO
    # misconfiguration, and the BatchSpanProcessor swallows the resulting export
    # 401 silently — so this logs at boot whether auth headers are configured
    # (never their value) to make that failure diagnosable.
    headers_configured = bool(str(os.getenv("OTEL_EXPORTER_OTLP_HEADERS") or "").strip())
    logger.info(
        "OpenTelemetry tracing enabled: exporting spans over OTLP to %s "
        "(service.name=%s, auth headers configured=%s)",
        endpoint,
        service_name,
        "yes" if headers_configured else "no",
    )
