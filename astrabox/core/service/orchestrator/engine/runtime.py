"""The contract between the platform and one agent runtime.

The runtime owns its conversation: whether it is working or waiting, which
inputs it has taken, where each response starts and ends, which approvals or
questions it is waiting on, and its own transcript. An engine adapter reads
those facts in the vendor's own vocabulary and reports them through this
contract; the platform records and shows them and decides none of them.

A runtime client offers five things:

* :meth:`RuntimeClient.submit` hands the runtime one input. It is idempotent
  by ``input_id``: submitting an input the runtime already holds is a no-op.
* :meth:`RuntimeClient.events` is the runtime's event stream from a cursor.
  Every event carries the cursor to resume after it, and the events after a
  cursor are the same events each time they are read. The platform reads it
  with one reader per session for as long as the runtime lives, and reports
  what it has recorded with :meth:`RuntimeClient.committed`.
* :meth:`RuntimeClient.state` is the runtime's own account of itself now:
  status, open responses, pending interactions and queued inputs.
* :meth:`RuntimeClient.answer` and :meth:`RuntimeClient.interrupt` pass a
  person's decision to the runtime.

The events are a small closed set of envelopes the platform routes on; each
carries the adapter's translation into browser parts and keeps the vendor's
own words (outcome reasons, interaction contracts) unchanged. See
``docs/maintainers/sdk-semantics-audit.md`` for why the platform never
re-encodes engine semantics.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.base import (
    EngineCapabilityManifest,
    validate_manifest_vocabulary,
)
from astrabox.core.service.orchestrator.engine.emissions import public_ui_frame

RuntimeStatus = Literal["idle", "working", "waiting"]
"""What the runtime reports about itself.

``working`` means it is producing a response (including background work it
started itself); ``waiting`` means a response is paused on a person (an
approval or a question); ``idle`` means neither.
"""

ResponseOrigin = Literal["input", "engine"]
"""``input``: the response answers one or more submitted inputs.
``engine``: the runtime started it on its own (a sub-agent finished, a
scheduled job ran, a queued follow-up drained)."""

ResponseOutcome = Literal["completed", "failed", "cancelled"]
"""The adapter's declared reading of the vendor's own ending; the vendor's
words travel beside it in :attr:`ResponseEnded.native_reason`."""


@dataclass(frozen=True, slots=True)
class RuntimeCursor:
    """Where to resume a runtime's event stream.

    ``generation`` names the runtime incarnation the position belongs to (a
    runner process, a backend epoch, a vendor session id). A cursor from
    another generation cannot be resumed; :meth:`RuntimeClient.events` raises
    :class:`RuntimeCursorExpired` for it. ``position`` is the adapter's own
    resume state, stored by the platform as JSON and never interpreted.
    """

    generation: str
    position: dict[str, Any]

    def as_json(self) -> dict[str, Any]:
        return {"generation": self.generation, "position": deepcopy(self.position)}

    @classmethod
    def from_json(cls, value: Mapping[str, Any] | None) -> RuntimeCursor | None:
        if not value:
            return None
        generation = str(value.get("generation") or "").strip()
        position = value.get("position")
        if not generation or not isinstance(position, dict):
            raise ValueError("a runtime cursor needs a generation and a position object")
        return cls(generation=generation, position=deepcopy(position))


class RuntimeCursorExpired(RuntimeError):
    """The cursor names a runtime generation that does not exist, or a
    position the runtime does not retain. The reader resumes from
    :meth:`RuntimeClient.state` instead."""


RUNTIME_PROTOCOL_INCOMPATIBLE = "runtime_protocol_incompatible"


class RuntimeProtocolError(APIError):
    """The runtime's protocol cannot be read; its execution outcome is unknown.

    Retain the last readable cursor and native execution facts. Reconnecting
    cannot repair this contract; an operator must correct the runtime first.
    """

    def __init__(self, message: str) -> None:
        super().__init__(code="RUNTIME_PROTOCOL_INCOMPATIBLE", message=message,
                         status_code=502, retryable=False)


class RuntimeInputRefused(RuntimeError):
    """The runtime declined an input; ``code`` and ``message`` are the user-facing reason."""

    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class RuntimeControlRefused(RuntimeError):
    """A native control failed without ending the runtime's event transport."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class InteractionNotOpen(RuntimeError):
    """The runtime holds no open interaction with this id (answered, withdrawn
    or lost with a restarted runtime process)."""


@dataclass(frozen=True, slots=True)
class RuntimeInput:
    """One input for the runtime.

    ``input_id`` is the platform's stable id for the input; the adapter uses it
    (or a value derived from it) as the vendor's message id wherever the vendor
    deduplicates, and must make a repeated submit of the same ``input_id`` a
    no-op where the vendor does not.
    """

    input_id: str
    content: str
    content_blocks: tuple[dict[str, Any], ...] = ()
    permission_mode: str | None = None


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    """Base of every event; ``cursor`` resumes the stream after this event."""

    cursor: RuntimeCursor | None


@dataclass(frozen=True, slots=True)
class InputAdmitted(RuntimeEvent):
    """The runtime durably holds this input: after a restart of the runtime
    process it would still answer it (or has answered it). Until this event the
    platform keeps custody of the input and resubmits it to a replacement
    runtime."""

    input_id: str


@dataclass(frozen=True, slots=True)
class InputMessageObserved(RuntimeEvent):
    """An exact input observed in the native conversation, in native event order.

    Queue admission is not a conversation position. The adapter emits this
    from the native user message or an exact native command-consumption
    record, never from the fact that a queued submit succeeded. The platform
    records the message with the same cursor and ordered output log as native
    reply content, retaining its first position without changing timestamps.
    """

    input_id: str


@dataclass(frozen=True, slots=True)
class InputRefused(RuntimeEvent):
    """The runtime declined this input before starting a response."""

    input_id: str
    failure: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ResponseStarted(RuntimeEvent):
    """The runtime began a response.

    ``response_id`` is stable for the life of the response, including across a
    reader reattaching (derive it from the vendor's own ids). ``input_ids`` are
    the submitted inputs this response answers, empty for ``origin="engine"``.
    """

    response_id: str
    origin: ResponseOrigin
    input_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResponseOutput(RuntimeEvent):
    """One browser part (an AI SDK UI message chunk) of a response.

    ``frame`` is the adapter's translation and passes to the browser as is.
    ``response_id`` is ``None`` only for session-level output that belongs to
    no response. On a message's ``start``, ``channel_delivery=False`` keeps
    informational output in the Session without broadcasting it to channels.
    """

    response_id: str | None
    frame: dict[str, Any]
    channel_delivery: bool = True

    def __post_init__(self) -> None:
        frame = public_ui_frame(self.frame)
        if not self.channel_delivery:
            if frame["type"] != "start":
                raise ValueError("channel delivery is declared on a message start")
            frame["messageMetadata"] = {**frame.get("messageMetadata", {}), "channel_delivery": False}
        object.__setattr__(self, "frame", frame)


@dataclass(frozen=True, slots=True)
class InteractionOpened(RuntimeEvent):
    """The response is paused on a person.

    ``contract`` is the platform presentation the adapter builds from the
    vendor's request (see ``docs/domain-model.md``); ``interaction_id`` is the
    id :meth:`RuntimeClient.answer` accepts.
    """

    response_id: str
    interaction_id: str
    contract: dict[str, Any]


@dataclass(frozen=True, slots=True)
class InteractionClosed(RuntimeEvent):
    """The interaction is closed: the runtime does not wait on it.

    ``reason`` is ``answered``, ``withdrawn`` (the runtime cancelled it, e.g. on
    interrupt) or ``expired`` (the runtime gave up waiting).
    """

    interaction_id: str
    reason: Literal["answered", "withdrawn", "expired"]


@dataclass(frozen=True, slots=True)
class ResponseEnded(RuntimeEvent):
    """The runtime ended a response.

    ``outcome`` is the adapter's declared reading of the vendor's ending;
    ``native_reason`` is the vendor's own word for it, unchanged. ``usage`` and
    ``error`` are what the vendor reported.
    """

    response_id: str
    outcome: ResponseOutcome
    native_reason: str | None = None
    usage: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class StatusChanged(RuntimeEvent):
    """The runtime's own status changed."""

    status: RuntimeStatus


@dataclass(frozen=True, slots=True)
class RuntimeDiagnostic(RuntimeEvent):
    """Raw vendor evidence for operators: written to the server log, never sent to a browser."""

    event_type: str
    raw: Any


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OpenResponse:
    """A response the runtime has not ended."""

    response_id: str
    origin: ResponseOrigin
    input_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PendingInteraction:
    response_id: str
    interaction_id: str
    contract: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RuntimeState:
    """The runtime's own account of itself now.

    ``admitted_input_ids`` are the inputs the runtime durably holds, so a reader
    that missed :class:`InputAdmitted` events can still close the platform's
    custody of them. ``cursor`` is where :meth:`RuntimeClient.events` continues
    so that nothing after this snapshot is missed. ``closed_interactions``
    carries closure receipts the runtime retains; a saved platform answer is
    not evidence that the runtime accepted it.
    """

    status: RuntimeStatus
    open_responses: tuple[OpenResponse, ...] = ()
    pending_interactions: tuple[PendingInteraction, ...] = ()
    queued_input_ids: tuple[str, ...] = ()
    admitted_input_ids: tuple[str, ...] = ()
    cursor: RuntimeCursor | None = None
    #: Native closure receipts retained by the runtime for reader recovery.
    closed_interactions: tuple[InteractionClosed, ...] = ()
    refused_inputs: tuple[InputRefused, ...] = ()


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


@runtime_checkable
class RuntimeClient(Protocol):
    """One live connection to one runtime conversation."""

    engine_kind: str

    @property
    def is_live(self) -> bool:
        """Whether the connection to the runtime is open."""
        ...

    @property
    def engine_session_key(self) -> str | None:
        """The vendor's conversation id, which a replacement runtime resumes.

        ``None`` until the vendor holds a conversation it can resume by that id.
        """
        ...

    async def submit(self, item: RuntimeInput) -> None:
        """Hand the runtime one input; return once the runtime has received it.

        Idempotent by ``item.input_id``. A refusal raises
        :class:`RuntimeInputRefused` or arrives as :class:`InputRefused` on
        the event stream when native preflight completes asynchronously.
        """
        ...

    def events(self, *, after: RuntimeCursor | None) -> AsyncIterator[RuntimeEvent]:
        """The runtime's events after ``after`` (from the start when ``None``),
        for as long as the connection lives.

        Raises :class:`RuntimeCursorExpired` when ``after`` cannot be resumed.
        """
        ...

    async def state(self) -> RuntimeState:
        """The runtime's own account of itself now."""
        ...

    async def answer(self, interaction_id: str, response: dict[str, Any]) -> None:
        """Pass a person's answer to an open interaction.

        Raises :class:`InteractionNotOpen` when the runtime holds no such open
        interaction and no receipt that it already accepted this answer.
        """
        ...

    async def interrupt(self, *, cancel_queued: bool = False) -> None:
        """Stop the response in progress; with ``cancel_queued`` also drop the
        inputs the runtime has queued behind it."""
        ...

    async def committed(self, cursor: RuntimeCursor) -> None:
        """The platform durably recorded every event through ``cursor``.

        A runtime that keeps history only until its reader has it may release
        what precedes ``cursor``.
        """
        ...

    async def capabilities(self) -> EngineCapabilityManifest:
        """What this runtime offers the platform: permission modes, input types, controls."""
        ...

    async def close(self) -> None:
        """Close the connection; the runtime itself keeps running."""
        ...


def validate_runtime_client_type(client_type: type[Any]) -> None:
    """Reject a client class that does not offer the whole runtime contract.

    Registration runs this before an engine appears in a product catalog, so a
    member the reader calls and the class lacks fails there rather than on a
    person's first input. ``events`` must be an async generator; the other
    methods are ``async def`` and ``is_live`` and ``engine_session_key`` are
    properties.
    """

    if not isinstance(client_type, type):
        raise TypeError("a runtime client type must be a class")
    problems: list[str] = []
    for name in sorted(getattr(RuntimeClient, "__protocol_attrs__", ())):
        contract = inspect.getattr_static(RuntimeClient, name, None)
        candidate = inspect.getattr_static(client_type, name, None)
        if candidate is None:
            problems.append(f"{name}: missing")
        elif isinstance(contract, property):
            if not isinstance(candidate, property):
                problems.append(f"{name}: must be a property")
        elif name == "events":
            if not inspect.isasyncgenfunction(candidate):
                problems.append(f"{name}: must be an async generator")
        elif inspect.iscoroutinefunction(contract) and not inspect.iscoroutinefunction(candidate):
            problems.append(f"{name}: must be async def")
    if problems:
        raise TypeError(f"{client_type.__name__} does not serve the runtime contract: " + "; ".join(problems))


async def initialize_runtime_client(
    client: RuntimeClient, *, engine_kind: str, resume_session_key: str | None
) -> EngineCapabilityManifest:
    """Check a newly connected client before a reader may use it.

    It must be live, have resumed the conversation it was asked to resume, and
    declare the permission modes its adapter registered.
    """
    from astrabox.core.service.orchestrator.engine.registry import get_engine_adapter

    if not client.is_live:
        raise RuntimeError(f"{engine_kind} runtime closed while it was being connected")
    if resume_session_key is not None and client.engine_session_key != resume_session_key:
        raise RuntimeError(
            f"{engine_kind} runtime did not resume the requested conversation: "
            f"expected={resume_session_key!r} actual={client.engine_session_key!r}"
        )
    manifest = await client.capabilities()
    if manifest.engine_kind != engine_kind:
        raise TypeError(f"capability manifest of {manifest.engine_kind!r} from a {engine_kind!r} runtime")
    validate_manifest_vocabulary(manifest)
    declared = get_engine_adapter(engine_kind).capabilities.permission_modes
    missing = [mode for mode in declared if mode not in manifest.permission_modes]
    if missing:
        raise TypeError(f"{engine_kind} runtime is missing required permission modes {missing!r}")
    return manifest
