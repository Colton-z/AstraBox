"""Shared HTTP utilities for route modules.

Kept in one place so routes can reuse them without importing a module that has
FastAPI app-creation side effects.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError

logger = get_logger(__name__)


def is_local_mode() -> bool:
    value = str(os.getenv("ASTRABOX_LOCAL_MODE", "")).strip().lower()
    return value in {"1", "true", "yes", "on"}


def _trace_id_from_traceparent(traceparent: str) -> str:
    """Extract the 32-hex trace-id from a W3C ``traceparent`` header, or ``""``.

    Format: ``version "-" trace-id "-" parent-id "-" flags`` (e.g.
    ``00-<32hex>-<16hex>-01``). Only the trace-id field is needed here.
    """
    parts = str(traceparent or "").strip().split("-")
    if len(parts) >= 2 and parts[1]:
        return parts[1].strip()
    return ""


def _w3c_traceparent(trace_id: str, span_id: str | None = None) -> str:
    """Render a well-formed W3C ``traceparent`` from a trace id (+ optional span).

    The trace id is normalized to 32 hex chars and the span (parent) id to 16;
    a random span id is minted when none is supplied. ``rpc_id`` values like
    ``"0"`` are not valid 16-hex span ids, so only the trace id carries across
    the hop.
    """
    trace_hex = "".join(c for c in str(trace_id or "").lower() if c in "0123456789abcdef")
    if len(trace_hex) < 32:
        trace_hex = (trace_hex + uuid.uuid4().hex)[:32]
    else:
        trace_hex = trace_hex[:32]
    span_hex = "".join(c for c in str(span_id or "").lower() if c in "0123456789abcdef")
    if len(span_hex) != 16:
        span_hex = uuid.uuid4().hex[:16]
    return f"00-{trace_hex}-{span_hex}-01"


def resolve_request_trace_context(request: Any) -> tuple[str, str]:
    state = getattr(request, "state", None)
    trace_id = str(getattr(state, "astrabox_trace_id", "") or "").strip()
    rpc_id = str(getattr(state, "astrabox_rpc_id", "") or "").strip()

    headers = getattr(request, "headers", None)
    if headers is not None:
        if not trace_id:
            # W3C traceparent inbound: recover the trace-id field.
            trace_id = _trace_id_from_traceparent(headers.get("traceparent") or "")

    if not trace_id:
        trace_id = uuid.uuid4().hex
    if not rpc_id:
        rpc_id = "0"
    return trace_id, rpc_id


def extract_trace_headers(request: Any) -> dict[str, str]:
    """Outbound trace headers to forward downstream — W3C ``traceparent``.

    Emits a single ``traceparent`` header carrying the resolved trace id. An
    inbound ``traceparent`` is passed through unchanged so the upstream sees a
    continuous trace.
    """
    headers = getattr(request, "headers", None)

    if headers is not None:
        inbound = str(headers.get("traceparent") or "").strip()
        if inbound:
            return {"traceparent": inbound}

    trace_id, rpc_id = resolve_request_trace_context(request)
    return {"traceparent": _w3c_traceparent(trace_id, rpc_id)}


def map_storage_api_error(exc: Exception) -> APIError | None:
    """Map known storage/backend exceptions to explicit API errors."""
    try:
        from pymongo.errors import (
            AutoReconnect,
            ConnectionFailure,
            NetworkTimeout,
            OperationFailure,
            PyMongoError,
            ServerSelectionTimeoutError,
        )

        data = {"detail": str(exc)} if is_local_mode() else None
        if isinstance(
            exc,
            (
                AutoReconnect,
                NetworkTimeout,
                ServerSelectionTimeoutError,
                ConnectionFailure,
            ),
        ):
            return APIError(
                code="PERSISTENCE_UNAVAILABLE",
                message="mongodb timeout/unavailable, please retry",
                status_code=503,
                data=data,
                evidence=data if isinstance(data, dict) else None,
            )
        if isinstance(exc, OperationFailure):
            return APIError(
                code="PERSISTENCE_OPERATION_FAILED",
                message="mongodb operation failed",
                status_code=500,
                data=data,
                evidence=data if isinstance(data, dict) else None,
            )
        if isinstance(exc, PyMongoError):
            return APIError(
                code="PERSISTENCE_ERROR",
                message="mongodb error",
                status_code=500,
                data=data,
                evidence=data if isinstance(data, dict) else None,
            )
    except Exception:
        return None

    return None


def _compact_error_detail(value: str, *, limit: int = 600) -> str:
    detail = " ".join(str(value or "").split())
    if len(detail) <= limit:
        return detail
    return detail[: limit - 3] + "..."


def map_unexpected_api_error(
    exc: Exception,
    *,
    path: str,
    trace_id: str,
    rpc_id: str,
) -> APIError:
    """Build an explicit, traceable response for otherwise unclassified server errors."""
    error_type = type(exc).__name__ or "Exception"
    detail = _compact_error_detail(str(exc))
    evidence = {
        "path": str(path or ""),
        "trace_id": str(trace_id or ""),
        "rpc_id": str(rpc_id or ""),
        "error_type": error_type,
    }
    if detail and is_local_mode():
        evidence["detail"] = detail

    message = f"unexpected {error_type} at {path}; trace_id={trace_id}"
    if detail and is_local_mode():
        message = f"{message}; detail={detail}"

    return APIError(
        code="UNEXPECTED_SERVER_ERROR",
        message=message,
        status_code=500,
        data=evidence,
        debug_message=detail or None,
        evidence=evidence,
    )
