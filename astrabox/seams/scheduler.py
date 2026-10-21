"""Scheduler seam — the ``SchedulerProvider`` contract and its registry.

A scheduled Deployment has two halves with different owners.

The platform owns the product: the Deployment record (its name, prompt, cron
expression, IANA timezone and enabled flag) is the only desired state, the
routes authorize who may change it or run it, a Run's Session and its input are
admitted with the Run's id as their idempotency key, and the public Run status
is projected from the provider's dispatch state and the Session's response to
the Run's input.

A scheduler provider owns execution: it fires each due time, gives every
invocation one durable id, calls the platform back at least once per invocation
until it succeeds or fails permanently, recovers invocations a process lost, and
keeps the ledger of Runs that the platform lists and replays. The platform
never re-implements any of that (no polling loop, lease, retry queue or Run
state machine of its own).

What a provider must guarantee:

1. **Due times.** Each due time is computed from the five-field cron expression
   in the schedule's IANA timezone. A time that passes while no process of the
   deployment is running, or while the schedule is disabled, is skipped: it is
   neither backfilled nor merged into a later invocation. Every due time is an
   independent invocation; one still running does not hold back the next.
2. **Invocation identity.** One due time of one Deployment, identified by its
   UTC instant, produces exactly one :class:`RunRecord` and one ``run_id``
   across every replica. The provider's store enforces this with a uniqueness
   constraint, not with a read before the write.
3. **Dispatch.** For each invocation the provider calls
   :meth:`RunHandler.start_session`, records the returned Session id durably,
   then calls :meth:`RunHandler.admit_input` and records the input. Either
   call may happen more than once, always with the same :class:`RunInvocation`;
   the handler is idempotent on ``run_id``. A failure other than
   :class:`RunPermanentError` is retried with bounded backoff; a permanent
   error, or retries running out, records the Run as ``FAILED``.
4. **Recovery.** An invocation interrupted by the loss of a process continues
   under the same ``run_id`` with the Session id already recorded.
   :attr:`SchedulerProperties.recovery_scope` says which processes take it over.
5. **Enable and disable.** After :meth:`SchedulerProvider.apply` returns with a
   disabled definition, no new scheduled invocation starts later than
   :attr:`SchedulerProperties.pause_visibility_seconds`; invocations already
   started continue. The handler re-checks the Deployment before it starts a
   scheduled Run's Session, which closes that window.
6. **Run now and replay.** :meth:`SchedulerProvider.enqueue` returns after the
   new Run is stored and does not move the calendar.
7. **Listing.** :meth:`SchedulerProvider.list_runs` returns a Deployment's Runs
   of every trigger, newest first.
8. **Failures.** A storage or engine failure raises
   :class:`SchedulerUnavailable`; the provider's own vocabulary and error text
   never reach an API response.

Together with the platform's idempotent admission (a Session and an input
keyed by ``run_id``), at-least-once dispatch gives each invocation at most one
admitted input, and exactly one once it succeeds.

To add a provider: implement :class:`SchedulerProvider`, call
:func:`register_scheduler` at import, and advertise the class under the
``astrabox.providers.scheduler`` entry-point group. The deployment selects one
with ``ASTRABOX_SCHEDULER_PROVIDER``; ``none`` disables scheduled Deployments.
:mod:`astrabox.testing.scheduler_conformance` is the reusable contract suite.

This module imports only the standard library.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

#: Entry-point group an external package registers a scheduler provider under.
ENTRY_POINT_GROUP = "astrabox.providers.scheduler"

#: The ``ASTRABOX_SCHEDULER_PROVIDER`` value that disables scheduled Deployments.
SCHEDULER_NONE = "none"

RunTrigger = Literal["schedule", "manual", "replay"]

#: Where an invocation is in the provider's dispatch. ``DISPATCHED`` means the
#: Session and its input were admitted; what the agent then does with the input
#: is the Session's state, not the provider's. ``UNKNOWN`` is a state the
#: provider's engine reported and the provider cannot name.
RunDispatchState = Literal[
    "QUEUED", "DISPATCHING", "DISPATCHED", "FAILED", "CANCELLED", "UNKNOWN"
]
RUN_DISPATCH_STATES: tuple[str, ...] = (
    "QUEUED",
    "DISPATCHING",
    "DISPATCHED",
    "FAILED",
    "CANCELLED",
    "UNKNOWN",
)


@dataclass(frozen=True, slots=True)
class ScheduleDefinition:
    """One Deployment's schedule as the provider projects it.

    ``cron`` and ``timezone`` arrive validated: exactly five fields and an IANA
    name. ``payload`` is the platform's own JSON-serializable invocation input
    (the target and the prompt); the provider stores it unchanged and hands it
    back in every :class:`RunInvocation` the schedule fires.
    """

    deployment_id: str
    environment_name: str
    cron: str
    timezone: str
    enabled: bool
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RunInvocation:
    """One invocation of a Deployment, as recorded when it was created.

    ``scheduled_for`` is the due time (timezone-aware) for ``trigger="schedule"``
    and ``None`` otherwise. ``payload`` is captured when the invocation is
    created and never changes afterwards, so editing the Deployment does not
    rewrite a Run that already exists.
    """

    run_id: str
    deployment_id: str
    environment_name: str
    trigger: RunTrigger
    payload: Mapping[str, Any]
    scheduled_for: datetime | None = None
    replayed_from_run_id: str | None = None


@dataclass(frozen=True, slots=True)
class RunInputBinding:
    """The input a Run's invocation admitted into its Session."""

    input_id: str


@dataclass(frozen=True, slots=True)
class RunRecord:
    """One Run in the provider's ledger."""

    invocation: RunInvocation
    state: RunDispatchState
    created_at: datetime | None
    session_id: str | None = None
    input_id: str | None = None
    #: Whether the invocation ended in failure. The provider's error text is
    #: an operator diagnostic and is deliberately not carried.
    failed: bool = False


@dataclass(frozen=True, slots=True)
class SchedulerProperties:
    """How a provider's engine behaves where engines legitimately differ.

    ``recovery_scope`` is ``"cluster"`` when any replica takes over an
    invocation a lost process left behind, and ``"executor"`` when only a
    process restarted with the same executor identity does. ``max_active_schedules``
    is the engine's ceiling on enabled schedules (``None`` for none);
    :meth:`SchedulerProvider.apply` enforces it. ``pause_visibility_seconds``
    bounds how long a disabled schedule may still fire.
    """

    recovery_scope: Literal["cluster", "executor"]
    max_active_schedules: int | None
    pause_visibility_seconds: float


class RunHandler(Protocol):
    """The platform's half of an invocation, called by the provider."""

    async def start_session(self, invocation: RunInvocation) -> str:
        """Create (or find) the invocation's Session and return its id."""
        ...

    async def admit_input(
        self, invocation: RunInvocation, *, session_id: str
    ) -> RunInputBinding:
        """Admit (or find) the invocation's input in ``session_id``."""
        ...


class RunPermanentError(Exception):
    """The invocation cannot succeed by being retried; record it as failed."""


class InvalidSchedule(ValueError):
    """The provider's engine rejected the cron expression or timezone."""


class ScheduleCapacityExceeded(RuntimeError):
    """Enabling the schedule would exceed the engine's active-schedule ceiling."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"at most {limit} active schedules are supported")
        self.limit = limit


class SchedulerUnavailable(RuntimeError):
    """The provider's store or engine failed; the operation may be retried."""


class SchedulerNotStarted(RuntimeError):
    """The provider was asked to act before :meth:`SchedulerProvider.start`."""


class SchedulerConfigurationError(RuntimeError):
    """The provider cannot run with this deployment's configuration."""


class SchedulerProvider(ABC):
    """One durable execution engine for scheduled Deployments."""

    #: Registry key; ``ASTRABOX_SCHEDULER_PROVIDER`` names it.
    name: str = ""
    properties: SchedulerProperties

    @abstractmethod
    def configure(self, *, database_url: str, environment_name: str) -> None:
        """Validate and bind this deployment's configuration; called at startup.

        ``database_url`` is the platform's PostgreSQL URL, which a provider may
        use for its own durable state. Raise
        :class:`SchedulerConfigurationError` naming what is missing when the
        provider cannot run here, so the process refuses to start instead of
        failing the first schedule a user creates.
        ``environment_name`` is this process's durable execution namespace;
        workers may dispatch only invocations belonging to that target.
        """

    @abstractmethod
    async def start(self, handler: RunHandler | None) -> None:
        """Open control operations and, with a handler, start this target's worker.

        ``handler=None`` must not dispatch, recover or fire any invocation.
        Control operations still manage the explicitly named target's records.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop dispatching within a bounded drain."""

    @abstractmethod
    async def reconcile(self, definitions: Sequence[ScheduleDefinition]) -> None:
        """Make the provider's schedules exactly ``definitions``.

        Called at startup with every non-deleted scheduled Deployment. A
        schedule the provider holds for a Deployment that is not listed is
        removed. Raises :class:`ScheduleCapacityExceeded` when the enabled
        definitions exceed the engine's ceiling.
        """

    @abstractmethod
    async def apply(self, definition: ScheduleDefinition) -> None:
        """Create or replace one schedule, enabled or disabled as defined.

        On failure the provider restores the schedule it held before the call
        and raises :class:`InvalidSchedule`, :class:`ScheduleCapacityExceeded`
        or :class:`SchedulerUnavailable`.
        """

    @abstractmethod
    async def delete(self, deployment_id: str, *, environment_name: str) -> None:
        """Remove a Deployment's schedule; a missing schedule is not an error."""

    @abstractmethod
    async def enqueue(
        self,
        *,
        deployment_id: str,
        environment_name: str,
        trigger: Literal["manual", "replay"],
        payload: Mapping[str, Any],
        replayed_from_run_id: str | None = None,
    ) -> RunRecord:
        """Store and start a Run that no due time produced; return its record."""

    @abstractmethod
    async def get_run(self, run_id: str, *, environment_name: str) -> RunRecord | None:
        """The Run with ``run_id``, or ``None`` when this provider holds none."""

    @abstractmethod
    async def list_runs(
        self, deployment_id: str, *, environment_name: str, limit: int,
    ) -> Sequence[RunRecord]:
        """A Deployment's Runs, newest first, at most ``limit`` (1 to 200)."""


_PROVIDERS: dict[str, SchedulerProvider] = {}

#: The provider name the composition root selected; ``None`` until it runs.
_CONFIGURED: str | None = None


def register_scheduler(provider: SchedulerProvider) -> None:
    """Install a provider under its ``name``. Last registration wins."""
    name = str(getattr(provider, "name", "") or "").strip().lower()
    if not name:
        raise RuntimeError("scheduler provider must have a non-empty name")
    if name == SCHEDULER_NONE:
        raise RuntimeError(f"{SCHEDULER_NONE!r} is reserved for disabling scheduling")
    properties = getattr(provider, "properties", None)
    if not isinstance(properties, SchedulerProperties):
        raise RuntimeError(
            f"scheduler provider {name!r} must declare SchedulerProperties"
        )
    if properties.recovery_scope not in ("cluster", "executor"):
        raise RuntimeError(
            f"scheduler provider {name!r} recovery_scope must be 'cluster' or 'executor'"
        )
    if properties.max_active_schedules is not None and properties.max_active_schedules <= 0:
        raise RuntimeError(
            f"scheduler provider {name!r} max_active_schedules must be positive or None"
        )
    if properties.pause_visibility_seconds < 0:
        raise RuntimeError(
            f"scheduler provider {name!r} pause_visibility_seconds must not be negative"
        )
    _PROVIDERS[name] = provider


def registered_scheduler_names() -> list[str]:
    """The installed scheduler provider names, sorted."""
    return sorted(_PROVIDERS)


def set_configured_scheduler(name: str) -> None:
    """Select the deployment's scheduler provider (the composition root calls this).

    ``none`` disables scheduled Deployments. Any other name must be installed.
    """
    global _CONFIGURED
    normalized = str(name or "").strip().lower()
    if not normalized:
        raise RuntimeError(
            f"no scheduler provider selected (installed: {registered_scheduler_names()}); "
            f"name one, or {SCHEDULER_NONE!r}"
        )
    if normalized != SCHEDULER_NONE and normalized not in _PROVIDERS:
        raise RuntimeError(
            f"no scheduler provider named {normalized!r} is installed "
            f"(installed: {registered_scheduler_names()})"
        )
    _CONFIGURED = normalized


def configured_scheduler_name() -> str | None:
    """The selected provider name, ``none``, or ``None`` before composition."""
    return _CONFIGURED


def configured_scheduler() -> SchedulerProvider | None:
    """The selected provider, or ``None`` when scheduling is disabled.

    Raises before the composition root has selected one: a process that never
    composed its providers has no scheduler, and answering "disabled" would
    misdescribe it.
    """
    if _CONFIGURED is None:
        raise SchedulerNotStarted(
            "no scheduler provider has been selected in this process; "
            "astrabox.bootstrap selects one"
        )
    if _CONFIGURED == SCHEDULER_NONE:
        return None
    return _PROVIDERS[_CONFIGURED]


__all__ = [
    "ENTRY_POINT_GROUP",
    "RUN_DISPATCH_STATES",
    "SCHEDULER_NONE",
    "InvalidSchedule",
    "RunDispatchState",
    "RunHandler",
    "RunInvocation",
    "RunPermanentError",
    "RunRecord",
    "RunTrigger",
    "RunInputBinding",
    "ScheduleCapacityExceeded",
    "ScheduleDefinition",
    "SchedulerConfigurationError",
    "SchedulerNotStarted",
    "SchedulerProperties",
    "SchedulerProvider",
    "SchedulerUnavailable",
    "configured_scheduler",
    "configured_scheduler_name",
    "register_scheduler",
    "registered_scheduler_names",
    "set_configured_scheduler",
]
