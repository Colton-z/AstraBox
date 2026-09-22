"""Shared agent-stream error vocabulary (a dependency leaf).

``IncompleteStreamError`` is needed on both sides of the truth-owner boundary:
the engine turn driver raises it and the session worker classifies it.  Vendor
transport exceptions are translated by their adapter before reaching here.

Also home to ``turn_service``'s other module-level, stateless turn-boundary
helpers: the dispatch-error payload/classification functions, the sandbox-gone
detector, the pending-interaction dict-shape helpers, and the
``RuntimeEnsureResult`` value type (plus its status constants) that the
"Runtime ensure" collaborator returns.

Imports: stdlib + astrabox.common.utils.errors (itself a leaf: stdlib-only).
Never import engine SDKs or orchestrator modules from here.
"""

from __future__ import annotations

import os
from typing import Any

from astrabox.common.utils.errors import APIError

__all__ = [
    "AGENT_CHAT_WAKE_POLL_INTERVAL_SECONDS",
    "AGENT_CHAT_WAKE_TIMEOUT_SECONDS",
    "IncompleteStreamError",
    "RUNTIME_ENSURE_ATTACHED",
    "RUNTIME_ENSURE_ATTACH_FAILED",
    "RUNTIME_ENSURE_BINDING_MISSING",
    "RuntimeEnsureResult",
    "dispatch_cancelled_before_claim",
    "extract_dispatch_error_payload",
    "get_machine_id",
    "is_active_sandbox_dispatch_error",
    "is_incomplete_stream_error",
    "is_recoverable_turn_attach_error",
    "is_sandbox_gone_error",
    "normalize_pending_interaction",
    "pending_interaction_matches_turn",
]


class IncompleteStreamError(RuntimeError):
    """Raised when the SDK stream ends before emitting a final ResultMessage."""


def is_incomplete_stream_error(exc: BaseException) -> bool:
    if isinstance(exc, IncompleteStreamError):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(is_incomplete_stream_error(sub) for sub in exc.exceptions)
    cause = exc.__cause__
    if cause is not None and cause is not exc:
        return is_incomplete_stream_error(cause)
    return False


def extract_dispatch_error_payload(exc: BaseException) -> dict[str, Any] | None:
    payload = getattr(exc, "payload", None)
    if isinstance(payload, dict):
        return dict(payload)
    if isinstance(exc, APIError):
        data = getattr(exc, "data", None)
        if isinstance(data, dict):
            return dict(data)
    return None


def is_active_sandbox_dispatch_error(exc: BaseException) -> bool:
    payload = extract_dispatch_error_payload(exc)
    if not isinstance(payload, dict):
        return False
    return (
        str(payload.get("status") or "").strip() == "failed"
        and str(payload.get("error") or "").strip() == "sandbox turn is still active"
    )


def is_recoverable_turn_attach_error(exc: BaseException) -> bool:
    if is_active_sandbox_dispatch_error(exc):
        return False
    if isinstance(exc, BaseExceptionGroup):
        return any(is_recoverable_turn_attach_error(sub) for sub in exc.exceptions)

    payload = extract_dispatch_error_payload(exc)
    code = str(
        getattr(exc, "code", "") or (payload or {}).get("code") or ""
    ).strip()
    if code in {
        "SIDECAR_ATTACH_IDENTITY_INVALID",
        "SIDECAR_ATTACH_IDENTITY_MISMATCH",
        "SIDECAR_GENERATION_UNAVAILABLE",
        "SIDECAR_GENERATION_REVISION_MISMATCH",
        "SIDECAR_GENERATION_DRAINING",
    }:
        return True

    message = str(getattr(exc, "message", None) or exc).lower()
    recoverable_tokens = (
        "turn transport attach requires dispatch attach_proof",
        "turn transport attach proof does not show a ready sidecar query",
        "turn transport attach proof sidecar revision mismatch",
        "turn transport attach proof sidecar generation mismatch",
        "requested sidecar generation is unavailable",
        "requested sidecar generation revision mismatch",
        "requested sidecar generation is draining",
        "sidecar_generation_draining",
        "cached sidecar runtime lacks concrete generation owner",
    )
    return any(token in message for token in recoverable_tokens)


def is_sandbox_gone_error(exc: BaseException) -> bool:
    """Detect the definitive 'sandbox instance is gone' signal.

    The sandbox dataplane ingress returns HTTP 404 once the instance behind its
    endpoint is gone (out-of-band kill, lease expiry, reclaim); the endpoint
    metadata can still resolve for a window after the process dies. The runtime
    manager tags that 404 as a typed SANDBOX_GONE APIError. A gone sandbox must
    trigger a fresh-sandbox rebuild, never a reconnect to the same dead
    instance, so it is not folded into the existing-sandbox attach recovery
    path.

    A 404 is not the only producer. On the ``open_sandbox`` backend a box whose
    Pod was deleted out-of-band keeps a Ready control-plane record and a stale
    endpoint IP, so nothing ever answers 404 and the only evidence is that the
    address stopped accepting connections — which that provider also raises as
    SANDBOX_GONE. So this predicate does NOT separate "gone" from "alive but
    unreachable for the whole of one attach"; treating the two alike is the
    deliberate trade. Both readings converge the box's owners, and the cost of
    being wrong is one replacement box, while the conversation keeps its
    transcript. The other way round, the conversation stays wedged forever.
    """
    if isinstance(exc, BaseExceptionGroup):
        return any(is_sandbox_gone_error(sub) for sub in exc.exceptions)
    if str(getattr(exc, "code", "") or "").strip() == "SANDBOX_GONE":
        return True
    payload = extract_dispatch_error_payload(exc)
    if str((payload or {}).get("code") or "").strip() == "SANDBOX_GONE":
        return True
    cause = exc.__cause__
    if cause is not None and cause is not exc:
        return is_sandbox_gone_error(cause)
    return False


def dispatch_cancelled_before_claim(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    return (
        bool(payload.get("ok"))
        and bool(payload.get("found"))
        and str(payload.get("status") or "").strip() == "failed"
        and str(payload.get("error") or "").strip() == "dispatch cancelled"
    )


def get_machine_id() -> str:
    """Return a stable identifier for the current process/machine."""
    hostname = os.environ.get("HOSTNAME", "")
    if not hostname:
        import socket
        hostname = socket.gethostname()
    return f"{hostname}-{os.getpid()}"


class RuntimeEnsureResult:
    __slots__ = (
        "status",
        "runtime",
        "error_text",
        "session",
        "dispatch_id",
        "dispatch_payload",
        "sandbox_gone",
    )

    def __init__(
        self,
        *,
        status: str,
        runtime: Any | None = None,
        error_text: str | None = None,
        session: dict[str, Any] | None = None,
        dispatch_id: str | None = None,
        dispatch_payload: dict[str, Any] | None = None,
        sandbox_gone: bool = False,
    ) -> None:
        self.status = status
        self.runtime = runtime
        self.error_text = error_text
        self.session = session
        self.dispatch_id = dispatch_id
        self.dispatch_payload = dispatch_payload
        # True when ATTACH_FAILED because the bound sandbox is gone
        # (SANDBOX_GONE / 404). Drives the native same-send re-borrow in
        # iter_sandbox_events instead of a failed turn.
        self.sandbox_gone = sandbox_gone


RUNTIME_ENSURE_ATTACHED = "ATTACHED"
RUNTIME_ENSURE_BINDING_MISSING = "BINDING_MISSING"
RUNTIME_ENSURE_ATTACH_FAILED = "ATTACH_FAILED"
AGENT_CHAT_WAKE_TIMEOUT_SECONDS = 300.0
AGENT_CHAT_WAKE_POLL_INTERVAL_SECONDS = 2.0


def normalize_pending_interaction(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    interaction_id = str(value.get("interaction_id") or "").strip()
    tool_name = str(value.get("tool_name") or "").strip()
    if not interaction_id or not tool_name:
        return None
    return dict(value)


def pending_interaction_matches_turn(value: Any, turn_id: str) -> bool:
    pending = normalize_pending_interaction(value)
    if pending is None:
        return False
    return str(pending.get("turn_id") or "").strip() == str(turn_id or "").strip()
