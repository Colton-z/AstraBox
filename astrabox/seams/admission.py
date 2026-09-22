"""Admission seam — the injection point for enterprise quota / rate policy.

The community base ships NO quota, rate limit, or concurrency cap: a single
tenant can cold-create sandboxes and dispatch turns without bound. An
enterprise deployment needs per-user / global limits, but the POLICY (what the
limits are, how they are counted, where the counters live) is deployment
business logic, not the base's. This seam is the official place to plug it, so
a downstream never has to fork the turn/session hot path.

An :class:`AdmissionController` is consulted at two points:

* **turn** — before a StartTurn is accepted (a per-user concurrent-turn or
  rate cap);
* **sandbox** — before a user-facing session (and its sandbox) is created (a
  per-user or global sandbox cap).

The default :class:`AllowAllAdmissionController` admits every request. Register
an enforcing controller via
:func:`register_admission_controller` from a bootstrap/lifespan hook, or
advertise it at the ``astrabox.providers.admission`` entry-point group.

FAIL-OPEN: admission is a POLICY layer, not a security gate (identity +
ownership are the security gates). A controller that RAISES is logged and
treated as "admit" — a bug in the quota policy must not take the platform's
turns down. A controller that wants to deny must return
``AdmissionDecision(allowed=False, ...)``, not raise.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping

from astrabox.common.logger.logger_factory import get_logger

logger = get_logger(__name__)

ENTRY_POINT_GROUP = "astrabox.providers.admission"

ADMISSION_KIND_TURN = "turn"
ADMISSION_KIND_SANDBOX = "sandbox"


@dataclass(frozen=True)
class AdmissionRequest:
    """What the controller decides on."""

    kind: str  # ADMISSION_KIND_TURN | ADMISSION_KIND_SANDBOX
    user_id: str
    session_id: str | None = None
    agent_id: str | None = None
    template_name: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AdmissionDecision:
    """Allow or deny, with an operator-facing reason and optional backoff."""

    allowed: bool
    reason: str = ""
    #: When set, surfaced as a Retry-After hint on the 429 the caller raises.
    retry_after_seconds: float | None = None

    @classmethod
    def allow(cls) -> "AdmissionDecision":
        return cls(allowed=True)

    @classmethod
    def deny(cls, reason: str, *, retry_after_seconds: float | None = None) -> "AdmissionDecision":
        return cls(allowed=False, reason=reason, retry_after_seconds=retry_after_seconds)


class AdmissionController(ABC):
    """Deployment-supplied quota / rate / concurrency policy (see module docstring)."""

    @abstractmethod
    async def check(self, request: AdmissionRequest) -> AdmissionDecision:
        """Return an :class:`AdmissionDecision`. Deny by RETURNING, never raise."""


class AllowAllAdmissionController(AdmissionController):
    """The default: admit everything (no quota in the community base)."""

    async def check(self, request: AdmissionRequest) -> AdmissionDecision:
        return AdmissionDecision.allow()


_controller: AdmissionController = AllowAllAdmissionController()


def register_admission_controller(controller: AdmissionController) -> None:
    """Install the deployment's admission controller (last registration wins)."""
    global _controller
    if not isinstance(controller, AdmissionController):
        raise RuntimeError(
            "admission controller must subclass AdmissionController; got "
            f"{type(controller).__name__}"
        )
    _controller = controller
    logger.info("admission controller registered: %s", type(controller).__name__)


def get_admission_controller() -> AdmissionController:
    return _controller


async def check_admission(request: AdmissionRequest) -> AdmissionDecision:
    """Consult the registered controller, fail-open on error (see module docstring)."""
    try:
        return await _controller.check(request)
    except Exception:
        logger.warning(
            "admission controller raised for kind=%s user=%s — failing OPEN (admit)",
            request.kind, request.user_id, exc_info=True,
        )
        return AdmissionDecision.allow()


async def enforce_admission(request: AdmissionRequest) -> None:
    """Consult admission and raise a 429 ``APIError`` on denial.

    The single call site helper: core paths call this and only proceed if it
    returns. A denial carries the controller's reason (and Retry-After hint).
    """
    decision = await check_admission(request)
    if decision.allowed:
        from astrabox.observability.metrics import (
            METRIC_TURNS_ACCEPTED,
            increment,
        )

        if request.kind == "turn":
            increment(METRIC_TURNS_ACCEPTED)
        return
    from astrabox.observability.metrics import METRIC_TURNS_DENIED, increment

    increment(METRIC_TURNS_DENIED, labels={"kind": request.kind})
    from astrabox.common.utils.errors import APIError

    data: dict[str, Any] = {"admission_kind": request.kind}
    if decision.retry_after_seconds is not None:
        data["retry_after_seconds"] = decision.retry_after_seconds
    raise APIError(
        code="ADMISSION_DENIED",
        message=decision.reason or "admission denied by deployment policy",
        status_code=429,
        data=data,
    )
