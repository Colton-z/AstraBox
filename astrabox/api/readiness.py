"""Readiness + drain state for rolling deploys.

``/healthz`` is the liveness probe — is the process up — and stays 200 through
a drain so the container is not killed mid-drain. ``/readyz`` is the readiness
probe — should the load balancer route new traffic here — and flips to
not-ready the moment shutdown begins, so a rolling deploy stops sending new
requests to a replica that is draining its in-flight turns.

Two signals make a replica not-ready:

* an explicit drain (``begin_drain``) — set at the start of the lifespan
  shutdown, before in-flight work is cancelled, so the LB's next readiness
  probe removes this replica while existing turns finish;
* the platform having quiesced (``AgentPlatformService.quiesced_reason``) —
  a belt-and-braces reflection of a shutdown already underway.

The drain window itself (how long to keep serving in-flight turns after the
LB is told to stop) is ``ASTRABOX_SHUTDOWN_DRAIN_SECONDS`` (default 0 —
opt-in), applied in the lifespan shutdown. A Kubernetes deployment typically
also sets a ``preStop`` sleep + a ``terminationGracePeriodSeconds`` above the
longest turn; see docs/deploy.md.
"""

from __future__ import annotations

_draining = False


def begin_drain() -> None:
    """Mark this replica draining — ``/readyz`` reports not-ready henceforth."""
    global _draining
    _draining = True


def is_draining() -> bool:
    return _draining


def reset_drain_for_tests() -> None:  # pragma: no cover - test hygiene
    global _draining
    _draining = False


def readiness_status() -> tuple[bool, str]:
    """``(ready, reason)``. Not-ready while draining or once quiesced."""
    if _draining:
        return False, "draining"
    try:
        from astrabox.core.service.orchestrator.service_registry import (
            get_platform_service,
        )

        reason = get_platform_service().quiesced_reason
        if reason:
            return False, f"quiesced:{reason}"
    except Exception:
        # No platform yet (early boot) is not a reason to fail readiness — the
        # lifespan startup gates traffic; a construction error surfaces there.
        pass
    return True, "ready"
