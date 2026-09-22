"""Engine adapter base types.

``EngineAdapter`` is the engine-side abstraction that composes:
- platform-owned sandbox lifecycle and workspace primitives
- engine-owned process startup (runner launch for Claude, TUI Gateway launch for Hermes)
- ordered input dispatch (deliver / begin_delivery / stream / stop)
- typed emission contract (native events → public/control/private facts)

Adapters are looked up by engine_kind via the registry (engine.registry).
Public UI frames retain the AI SDK's open payload model. Control categories are
closed types, while transport, native payload schemas and vendor semantics stay
inside the adapter.
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from astrabox.core.service.orchestrator.engine.emissions import (
    ChildResourceFact,
    EngineTurnEmission,
    PrivateDiagnostic,
    SessionMessageFact,
    TurnTerminal,
)
if TYPE_CHECKING:
    from astrabox.core.service.orchestrator.engine.capabilities import (
        EngineRuntimeCapabilities,
    )

EngineKind = str

#: Durable, engine-owned messages projected from a resident engine stream.
#: The payload keeps the vendor message intact under ``message`` and names the
#: adapter under ``engine_kind``; platform consumers hand the message back to
#: that adapter instead of interpreting vendor fields themselves.
ENGINE_MESSAGE_EVENT_TYPE = "engine.message"


class EngineStreamDetached(RuntimeError):
    """The turn's event stream ended without the engine's own terminal.

    A statement about the transport, never about the turn: the link died, the
    process is shutting down, the proxy dropped — while the engine in the box
    may well still be running the turn to completion. The consumer that sees
    this must not author a turn terminal from it; settling belongs to the
    recovery lane, which judges from durable evidence (the transcript mirror)
    and sandbox lifecycle. Treating a detached link as terminal would let a
    shutting-down worker persist failure while the box continues valid work.
    """


@dataclass(frozen=True)
class EngineCapabilityManifest:
    """Engine-declared capabilities for a session.

    Used by the platform to render capability chips in the UI and to gate
    operations (for example permission modes and child-run control).
    """

    engine_kind: EngineKind
    tools: list[str] = field(default_factory=list)
    #: Platform turn-content block types this connected engine consumes. Text
    #: is the mandatory EngineClient journey; an adapter adds another type only
    #: when its vendor input protocol carries that block without loss.
    input_content_types: list[str] = field(default_factory=lambda: ["text"])
    #: The permission modes this engine understands, in its own vocabulary.
    #:
    #: Declared here rather than fixed by the platform because it is the
    #: engine's to define: the claude_code engine takes it from the agent SDK
    #: it wraps, and another engine has no reason to share those names. A
    #: platform-wide whitelist would force every later engine to speak the
    #: first one's vocabulary.
    #:
    #: Empty means "this engine has no permission-mode concept"; the platform
    #: then has nothing to validate against and must not invent one.
    permission_modes: list[str] = field(default_factory=list)
    # Optional engine-protocol vocabulary.  Platform lifecycle guarantees such
    # as stopping a turn are part of EngineClient below, not feature flags.
    supports_interaction: bool = False
    supports_child_run_control: bool = False
    supports_server_info: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EngineTurnReceipt:
    """Receipt for a started turn — opaque handle plus platform FIFO boundary.

    The platform stores `engine_turn_id` for later cancel/approval dispatch.
    A newly created native conversation may not reveal `engine_session_key`
    until its first terminal frame; the adapter must publish it then.
    `started_at_monotonic_ns` is used by the turn-service for stall detection.
    ``input_id`` identifies the durable FIFO item. ``input_consumed`` is true
    only when durable recovery or a parked interaction proves that the engine
    already crossed that boundary; it is platform state, not a vendor payload.
    """

    engine_turn_id: str
    engine_session_key: str | None
    started_at_monotonic_ns: int
    input_id: str | None = None
    input_consumed: bool = False


@dataclass(frozen=True)
class EngineSettledProjection:
    """Engine-neutral result of reading a vendor's durable transcript."""

    blocks: list[dict[str, Any]] = field(default_factory=list)
    assistant_text: str = ""
    completed: bool = False
    has_result: bool = False
    interrupted: bool = False


@dataclass(frozen=True)
class EngineOutputCheckpoint:
    """Durable output handed back to the adapter on a live reconnect.

    ``after_sequence`` is the adapter-authored raw-output cursor saved on the
    turn anchor. ``committed_frames`` are the turn's committed AI SDK frames,
    authored by the same adapter. Core does not infer an engine's open text,
    reasoning, tool, or child-agent state from them; the adapter that owns
    that output grammar resumes its own translator.
    """

    after_sequence: int | None = None
    committed_frames: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class EngineConversationBinding:
    """The durable identity an engine runtime must adopt before input.

    ``engine_session_key`` is opaque vendor state. The adapter provisions or
    attaches its runtime with that key, then proves the resulting client is
    bound to the same conversation. AstraBox never rebuilds vendor context
    from UI messages: those are an output projection, not an engine checkpoint.
    """

    platform_session_id: str
    engine_session_key: str | None = None


@runtime_checkable
class EngineEventSink(Protocol):
    """Narrow platform sink for durable facts emitted by an engine.

    Engines classify their own native messages, but they never open a platform
    repository. The platform implementation owns persistence, idempotency and
    input-consumption bookkeeping behind these two operations.
    """

    async def confirm_input_consumed(
        self,
        *,
        input_id: str,
        content: str,
        consumer_carrier: str | None,
    ) -> None: ...

    async def persist_event(
        self,
        *,
        engine_kind: str,
        causation_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ResidentResponseHandle:
    """The address of one engine-owned response the platform is publishing.

    ``response_id`` is the vendor's own identity for output it has already
    produced — never a platform permission to run. Which native identity an
    adapter chooses is its own contract (Claude uses the SDK envelope uuid of
    the response's first root activity). ``owns_slot`` says whether the
    platform's conversation projection
    reads this response as its active turn; it is false when a platform turn
    holds the slot, in which case the output is still journaled durably under
    its own address but is not the overlay a reader sees.
    """

    response_id: str
    owns_slot: bool


@dataclass(frozen=True)
class ResidentOutputCheckpoint:
    """What the platform already holds of an engine-owned response.

    Handed to the adapter when its resident observer starts. ``committed_frames``
    are the response's journaled frame rows, authored by this same adapter,
    in journal order; the adapter rebuilds its own translator lanes from them
    the way :class:`EngineOutputCheckpoint` hands a live reconnect its
    committed frames. Native output at or before ``after_sequence`` is
    replay: translated for state, never published again. Output before
    ``boundary_sequence`` predates this response and never enters its
    translator. ``external_turn_active`` says the platform still owns a turn
    on this conversation; the observer stays out of the stream until that
    turn's consumer has finished.
    """

    open_response_id: str | None = None
    boundary_sequence: int | None = None
    after_sequence: int | None = None
    committed_frames: tuple[dict[str, Any], ...] = ()
    external_turn_active: bool = False


@runtime_checkable
class ResidentOutputSink(Protocol):
    """Platform publication for output an engine produces on its own.

    The adapter recognises native response boundaries and translates the
    stream; every durable and read-side effect happens here, through the
    same frame journal, message projection, active overlay and Session
    follower that platform turns use. Nothing in this sink schedules
    execution: it records what the engine already did.
    """

    async def restore_resident_output(
        self,
        *,
        engine_kind: str,
    ) -> ResidentOutputCheckpoint: ...

    async def open_resident_response(
        self,
        *,
        engine_kind: str,
        response_id: str,
        engine_session_key: str | None,
        causation_id: str,
        native_message: dict[str, Any],
        runner_sequence: int,
    ) -> ResidentResponseHandle | None: ...

    async def publish_resident_output(
        self,
        handle: ResidentResponseHandle,
        emissions: list[EngineTurnEmission],
        *,
        engine_sequence_number: int,
    ) -> None: ...

    async def heartbeat_resident_response(
        self,
        handle: ResidentResponseHandle,
    ) -> bool: ...

    async def open_resident_interaction(
        self,
        handle: ResidentResponseHandle,
        *,
        interaction_id: str,
        contract: dict[str, Any],
        engine_session_key: str | None,
        engine_sequence_number: int,
    ) -> bool:
        """Park the engine-owned response on a platform interaction.

        The same durable record, snapshot state and stream frames a platform
        turn's gate produces, so the same answer command resolves it and its
        continuation consumer takes the response over. Returns whether the
        platform now waits on it.
        """
        ...

    async def close_resident_response(
        self,
        handle: ResidentResponseHandle,
        *,
        terminal: TurnTerminal,
        engine_sequence_number: int,
    ) -> None: ...


@dataclass(frozen=True)
class EngineStartupMaterialRequest:
    """Platform material an engine needs after placement exists.

    This is a declaration, not a retrieval hook.  An adapter may name the
    platform capabilities and logical secrets its vendor setup consumes; the
    platform remains the only component that mints tokens or opens the secret
    store.
    """

    transcript_store: bool = False
    runtime_state_store: bool = False
    sandbox_death_notice: bool = False
    secret_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class EngineStartupContext:
    """A platform-prepared runtime handed to one engine adapter.

    The platform has already selected or claimed the box, mounted and prepared
    its durable workspace, installed protected credentials, recorded cleanup
    ownership, and restored any platform-managed transcript before constructing
    this value.  The adapter can now do one thing: activate its vendor runtime
    inside the supplied box and return an ``EngineClient``-backed runtime.

    ``prepared_manifest`` is opaque engine evidence from an input-free prepared
    unit.  Empty means this was an ordinary create.  Allocation ids, provider
    objects, pool configuration, and cleanup controls are deliberately absent:
    none is an engine capability.
    """

    session_id: str
    template: Any
    workspace_plan: Any
    sandbox: Any
    sandbox_id: str
    cwd: str
    runtime_identity: dict[str, Any] | None
    model_access: Any
    model_credential: str
    resume_session_key: str | None
    runtime_env: dict[str, str] | None = None
    prepare_engine_input: Any = None
    prepared_manifest: dict[str, Any] | None = None
    service_credential: str | None = field(default=None, repr=False)
    runner_uri: str | None = None
    user_id: str | None = None
    permission_mode: str | None = None
    deployment_settings: Any = None
    capability_scope: Any = None
    event_sink: EngineEventSink | None = None
    #: Publication for output the engine produces with no platform input in
    #: flight. Absent only for adapters that never observe a resident stream.
    resident_output_sink: ResidentOutputSink | None = None
    #: Platform-minted, capability-scoped targets consumed by an engine's
    #: vendor transport.  The adapter renders them but cannot mint or widen
    #: them.
    transcript_store: dict[str, Any] | None = None
    runtime_state_store: dict[str, Any] | None = None
    sandbox_death_notice: dict[str, Any] | None = None
    #: Logical secret name to resolved value.  Only names declared through
    #: ``startup_material_request`` are present; adapters never reach into the
    #: platform secret store themselves.
    platform_secrets: dict[str, str] = field(default_factory=dict)
    #: Platform-owned routing identity for MCP servers hosted by AstraBox.
    #: The adapter may render it into its vendor configuration, but it never
    #: creates, updates, or deletes the corresponding platform binding.
    platform_mcp_deployment_id: str | None = None
    #: Set only when the platform is rebuilding a process-local client around
    #: an existing Session. Engines use it solely to choose their vendor's
    #: reconnect handshake; sandbox adoption and workspace/credential refresh
    #: have already happened.
    attach_mode: Literal["full", "lightweight"] | None = None


@dataclass(frozen=True)
class EnginePreparationContext:
    """An input-free, platform-owned placement an engine may prepare.

    The platform has already allocated the box or isolated seat, mounted and
    initialized its workspace, installed protected credentials, started any
    platform runner, and retained cleanup ownership. The engine may only
    establish and prove vendor state that can safely exist before a Session.
    """

    template: Any
    slot_id: str
    activation_token: str
    placement: str
    sandbox: Any
    sandbox_id: str
    cwd: str
    runtime_identity: dict[str, Any]
    model_access: Any
    model_credential: str
    runtime_env: dict[str, str]
    runner_uri: str | None
    preparation_fingerprint: str
    deployment_settings: Any = None
    workspace_id: str | None = None
    gateway_substitution: bool = False
    service_credential: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class EngineInputCommand:
    """One durable platform input offered to an active engine turn.

    The command deliberately carries no vendor envelope.  The adapter owns
    the conversion from this platform input into Claude SDK input, JSON-RPC,
    stdin, or any other engine vocabulary.
    """

    command_id: str
    session_id: str
    sequence: int
    input_id: str
    content: str
    client_message_id: str | None = None
    #: Validated platform content blocks, when the input carries more than
    #: prose. The selected adapter declares which types it consumes and owns
    #: conversion into vendor input. ``content`` stays the text projection.
    content_blocks: list[dict[str, Any]] | None = None


@runtime_checkable
class EngineClient(Protocol):
    """Minimum per-sandbox engine client.

    One EngineClient instance is bound to one sandbox handle; the platform
    holds it on SessionRuntime for the lifetime of that runtime. Ordered input,
    conversation continuation, streaming and stop are the minimum Agent
    journey, not optional features. Approval, permission modes, background
    child-run control and server metadata are separate optional protocols below.

    All methods are async and must be coroutine-safe but not re-entrancy safe
    at the turn level — the turn-service serializes turns per session.
    """

    @property
    def is_live(self) -> bool:
        """False once this client cannot carry a turn.

        A client over a resident connection reports a dead link or process
        here, and the runtime manager evicts the runtime instead of handing
        that client another turn. Short-lived clients may report only their
        own lifecycle state.
        """
        ...

    @property
    def engine_session_key(self) -> str | None:
        """The exact native conversation this client currently owns.

        A resumed client must expose the requested key as soon as binding
        completes. A new conversation may expose ``None`` until the engine's
        first in-band message identifies it, but every settled turn must leave
        a non-empty key so another process can resume the same conversation.
        """
        ...

    async def bind_conversation(
        self,
        binding: EngineConversationBinding,
    ) -> None:
        """Prove this runtime adopted the durable conversation identity.

        Called once for each process-local ``SessionRuntime`` before that
        runtime dispatches or resumes a turn. Silently starting a blank
        conversation when ``engine_session_key`` is present is a contract
        violation.
        """
        ...

    async def deliver(self, command: EngineInputCommand) -> None:
        """Idempotently accept one durable input into the conversation FIFO.

        Returning acknowledges adapter/control-layer ownership, not model
        consumption. The stream must later emit ``data-input-consumed`` for
        the exact input before any response frames for it.
        """
        ...

    async def begin_delivery(
        self,
        command: EngineInputCommand,
        *,
        consumption_confirmed: bool = False,
    ) -> EngineTurnReceipt:
        """Attach the turn consumer to an accepted FIFO input.

        ``consumption_confirmed`` is durable recovery evidence: the adapter
        must resume the already-consumed input instead of submitting it again.
        """
        ...

    def iter_turn_events(self, receipt: EngineTurnReceipt) -> AsyncIterator[EngineTurnEmission]:
        """Stream typed adapter emissions for the given turn.

        The adapter classifies every native event before it crosses the seam.
        Public UI payloads stay open to additive engine fields; input,
        interaction, child-resource, terminal and private-diagnostic facts use
        closed envelope types so orchestration never infers them from vendor
        vocabulary. The generator must end with :class:`TurnTerminal`, park at
        :class:`InteractionRequested`, or raise :class:`EngineStreamDetached`.
        """
        ...

    async def cancel_turn(self, receipt: EngineTurnReceipt) -> bool:
        """Stop this turn through the adapter's best available mechanism.

        A vendor-native cancel RPC is not required.  The adapter may signal or
        terminate its owned process, but AstraBox's Stop operation is always
        available and the stream must settle as cancelled.
        """
        ...

    async def interrupt_active_turn(self) -> bool:
        """Stop the active turn when the caller has no in-process receipt."""
        ...

    async def get_capabilities(self) -> EngineCapabilityManifest:
        """Return the engine's capability manifest for this session."""
        ...

    async def close(self) -> None:
        """Release client-held resources (HTTP session, websocket, etc.)."""
        ...


def _keyword_gaps(name: str, contract_member: Any, candidate: Any) -> list[str]:
    """Keyword-only parameters the contract passes and an implementation drops.

    Presence and kind are not enough. The platform calls these methods by
    keyword, so a method that exists, is async, and simply lacks one of the
    contract's keyword-only parameters passes every other check here and then
    raises ``TypeError: got an unexpected keyword argument`` on the first real
    turn — after a box has been built and a person has typed something. An
    implementation that takes ``**kwargs`` is accepting them by definition and
    is left alone.
    """

    if not callable(contract_member) or not callable(candidate):
        return []
    try:
        expected = inspect.signature(contract_member).parameters
        offered = inspect.signature(candidate).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins, C slots
        return []
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in offered.values()):
        return []
    missing = [
        key
        for key, parameter in expected.items()
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY and key not in offered
    ]
    return [f"{name}: does not accept {key}" for key in missing]


def validate_engine_client_type(client_type: type[Any]) -> None:
    """Reject an adapter whose declared client cannot complete the Agent journey.

    Registration performs this structural gate before the engine appears in a
    product catalog. Runtime construction repeats the instance check because a
    faulty adapter can still return a different object. Behavioral FIFO/resume
    proof belongs in that engine's conformance and live E2E tests.
    """

    if not isinstance(client_type, type):
        raise TypeError("engine_client_type must be a class")
    protocol_members = {
        name
        for name in getattr(EngineClient, "__protocol_attrs__", dir(EngineClient))
        if not name.startswith("_")
    }
    problems: list[str] = []
    for name in sorted(protocol_members):
        contract_member = inspect.getattr_static(EngineClient, name, None)
        candidate = inspect.getattr_static(client_type, name, None)
        if candidate is None:
            problems.append(f"{name}: missing")
        elif isinstance(contract_member, property):
            if not isinstance(candidate, property):
                problems.append(f"{name}: must be a property")
        elif inspect.iscoroutinefunction(contract_member):
            if not inspect.iscoroutinefunction(candidate):
                problems.append(f"{name}: must be async def")
        elif not callable(candidate) or inspect.iscoroutinefunction(candidate):
            problems.append(f"{name}: must return an async iterator")
        problems.extend(_keyword_gaps(name, contract_member, candidate))
    problems.extend(_optional_protocol_gaps(client_type))
    if problems:
        raise TypeError(
            f"{client_type.__name__} does not implement the mandatory EngineClient "
            "surface: " + "; ".join(problems)
        )


def _optional_protocol_gaps(client_type: type[Any]) -> list[str]:
    """Signature gaps in the OPTIONAL protocols a client chose to implement.

    An optional protocol is opt-in by presence: the platform checks
    ``isinstance(client, EngineInteractions)`` and then calls the method by
    keyword. Presence is therefore the whole opt-in, and a method that is
    present with a different signature has opted in to a contract it cannot
    honour. The mandatory sweep above does not reach these, which is how
    ``submit_interaction_response(self, interaction_id, response)`` passed
    registration, conformance and mypy, and raised
    ``TypeError: got an unexpected keyword argument 'pending'`` on the first
    real approval — after a box, a turn, and a person clicking Approve.

    A protocol the client does not implement at all is not its contract and is
    not checked; this only holds an implementation to the shape it claims.
    """

    problems: list[str] = []
    for protocol in OPTIONAL_ENGINE_CLIENT_PROTOCOLS:
        members = {
            name
            for name in getattr(protocol, "__protocol_attrs__", ())
            if not name.startswith("_")
        }
        if not members:
            continue
        candidates = {
            name: inspect.getattr_static(client_type, name, None) for name in members
        }
        if not any(candidate is not None for candidate in candidates.values()):
            continue
        for name in sorted(members):
            candidate = candidates[name]
            if candidate is None:
                problems.append(f"{protocol.__name__}.{name}: missing")
                continue
            contract_member = inspect.getattr_static(protocol, name, None)
            problems.extend(
                _keyword_gaps(f"{protocol.__name__}.{name}", contract_member, candidate)
            )
    return problems


@runtime_checkable
class EngineLiveTurnReconnect(Protocol):
    """Reconnect output from an unfinished turn whose engine is still alive.

    This protocol says only that the same resident engine process can resume
    delivery after the AstraBox host reconnects. It does not survive engine
    process or sandbox death. It is an adapter recovery implementation detail,
    not an Agent-admission capability: platform frame replay still comes from
    ``session_events``, and a turn that cannot be reattached settles explicitly
    before the next turn resumes the mandatory native conversation.
    """

    def iter_reconnected_turn_events(
        self,
        *,
        engine_turn_id: str,
        output_checkpoint: EngineOutputCheckpoint,
    ) -> AsyncIterator[EngineTurnEmission]: ...


@runtime_checkable
class EngineTranscriptRecovery(Protocol):
    """Adapter-owned projection for a vendor's authoritative transcript.

    Like live reconnect, this is one way to recover an unfinished turn, not a
    weaker or stronger class of Agent. Conversation resume remains mandatory
    through :class:`EngineClient` either way.
    """

    def slice_recovery_turn(
        self,
        raw_items: list[dict[str, Any]],
        *,
        prompt_text: str,
    ) -> list[dict[str, Any]]: ...

    def has_transcript_terminal_evidence(
        self,
        raw_items: list[Any],
    ) -> bool: ...

    def project_settled_transcript(
        self,
        raw_items: list[Any],
        *,
        done: bool = False,
    ) -> EngineSettledProjection: ...


@runtime_checkable
class EngineStoredChildTranscript(Protocol):
    """Optional authoritative child history read from the Session's database mirror.

    Scopes and journal messages are opaque vendor records. The adapter resolves
    its own child identity and returns complete message facts, not previews.
    This read does not require a running sandbox or change child lifecycle.
    """

    def stored_child_transcript_facts(
        self,
        *,
        engine_ref: str,
        closed: bool,
        raw_scopes: list[dict[str, Any]],
        raw_messages: list[dict[str, Any]],
    ) -> list[ChildResourceFact]: ...


@runtime_checkable
class EngineChildRunControl(Protocol):
    """Optional control of an engine-owned child run.

    ``control_id`` is the opaque value the same adapter published on the child
    lifecycle. Core never assumes it is a task, tool call, thread, or process.
    """

    async def stop_child_run(self, control_id: str) -> None: ...


@runtime_checkable
class EngineChildResourceReconciler(Protocol):
    """Optional authoritative refresh of an engine's durable child resources.

    The adapter reads its own catalog and transcript store, resolves native
    identities, and returns only typed private facts. Core persists those
    facts at Session scope; it never interprets vendor lifecycle vocabulary.
    """

    async def reconcile_child_resources(
        self,
    ) -> list[ChildResourceFact | PrivateDiagnostic]: ...


@runtime_checkable
class EnginePermissionModes(Protocol):
    """Optional engine-native permission-mode control."""

    async def set_permission_mode(self, mode: str) -> None: ...


@runtime_checkable
class EngineServerInfo(Protocol):
    """Optional initialize metadata such as slash commands and skills."""

    async def get_server_info(self) -> dict[str, Any] | None: ...


@runtime_checkable
class EngineInteractions(Protocol):
    """Optional resolution channel for engine-owned pending interactions.

    ``pending`` is the durable record the platform persisted for this
    interaction — it carries the adapter's own declared contract, including
    the exact native tool name and the verbatim ``raw_input`` the adapter
    needs to encode the answer. ``response`` is the browser answer, already
    validated against the record's structural presentation; judging its
    vendor meaning and producing the native SDK reply is this method's job.
    """

    async def submit_interaction_response(
        self,
        receipt: EngineTurnReceipt,
        *,
        pending: dict[str, Any],
        response: dict[str, Any],
    ) -> bool: ...


#: The opt-in-by-presence half of the client surface, held to its signatures by
#: `_optional_protocol_gaps`. A protocol added here starts being checked for
#: every client that implements any of its members.
OPTIONAL_ENGINE_CLIENT_PROTOCOLS: tuple[type[Any], ...] = (
    EngineLiveTurnReconnect,
    EngineTranscriptRecovery,
    EngineChildRunControl,
    EngineChildResourceReconciler,
    EnginePermissionModes,
    EngineServerInfo,
    EngineInteractions,
)


def validate_engine_client_manifest(
    client: EngineClient,
    manifest: EngineCapabilityManifest,
) -> None:
    """Reject a claimed capability whose callable contract is absent.

    An absent capability is valid.  The error boundary is the adapter lying
    about one: that would otherwise render a control which can only fail when
    the user invokes it.
    """

    permission_modes = manifest.permission_modes
    if not isinstance(permission_modes, list) or any(
        not isinstance(mode, str) or not mode.strip() for mode in permission_modes
    ):
        raise TypeError(
            f"engine_kind={manifest.engine_kind!r} permission_modes must be a list "
            "of non-empty names"
        )
    if len(set(permission_modes)) != len(permission_modes):
        raise TypeError(
            f"engine_kind={manifest.engine_kind!r} permission_modes must be unique"
        )

    input_content_types = manifest.input_content_types
    if not isinstance(input_content_types, list) or any(
        not isinstance(block_type, str) or not block_type.strip()
        for block_type in input_content_types
    ):
        raise TypeError(
            f"engine_kind={manifest.engine_kind!r} input_content_types must be a "
            "list of non-empty names"
        )
    if len(set(input_content_types)) != len(input_content_types):
        raise TypeError(
            f"engine_kind={manifest.engine_kind!r} input_content_types must be unique"
        )
    from astrabox.core.service.orchestrator.engine.input_content import (
        TURN_INPUT_CONTENT_TYPES,
    )

    unknown_content_types = sorted(
        set(input_content_types) - TURN_INPUT_CONTENT_TYPES
    )
    if unknown_content_types:
        raise TypeError(
            f"engine_kind={manifest.engine_kind!r} declares unknown input content "
            f"types: {unknown_content_types!r}"
        )
    if "text" not in input_content_types:
        raise TypeError(
            f"engine_kind={manifest.engine_kind!r} must accept text input"
        )

    claims: tuple[tuple[bool, type[Any], str], ...] = (
        (manifest.supports_interaction, EngineInteractions, "interaction"),
        (
            manifest.supports_child_run_control,
            EngineChildRunControl,
            "child-run control",
        ),
        (manifest.supports_server_info, EngineServerInfo, "server info"),
        (bool(manifest.permission_modes), EnginePermissionModes, "permission modes"),
    )
    missing = [
        label for claimed, protocol, label in claims if claimed and not isinstance(client, protocol)
    ]
    if missing:
        raise TypeError(
            f"engine_kind={manifest.engine_kind!r} claims unsupported client "
            f"capabilities: {', '.join(missing)}"
        )


async def initialize_engine_client(
    client: EngineClient,
    *,
    expected_engine_kind: str,
    conversation_binding: EngineConversationBinding,
) -> EngineCapabilityManifest:
    """Bind and validate a client before publishing its runtime.

    Conversation identity and capability discovery are one construction-time
    handshake. No input may reach a runtime before it proves that a durable
    native session key was resumed (or that this is a new conversation). The
    adapter's registered permission vocabulary is the bootstrap minimum: the
    connected client must still provide every required/default mode, while
    additive modes discovered from the live engine pass through. Once
    published, recovery and cleanup use the cached manifest even if the live
    transport later becomes unhealthy.
    """

    if not isinstance(conversation_binding, EngineConversationBinding):
        raise TypeError("conversation_binding must be EngineConversationBinding")
    platform_session_id = conversation_binding.platform_session_id
    if (
        not isinstance(platform_session_id, str)
        or not platform_session_id
        or platform_session_id != platform_session_id.strip()
    ):
        raise ValueError("conversation binding requires a normalized platform_session_id")
    engine_session_key = conversation_binding.engine_session_key
    if engine_session_key is not None and (
        not isinstance(engine_session_key, str)
        or not engine_session_key
        or engine_session_key != engine_session_key.strip()
    ):
        raise ValueError("engine_session_key must be None or a normalized non-empty string")
    if not isinstance(client, EngineClient):
        raise TypeError(
            "engine client does not implement the mandatory conversation and turn surface"
        )
    await client.bind_conversation(conversation_binding)
    if client.is_live is not True:
        raise RuntimeError("engine client became unavailable during conversation binding")
    actual_engine_session_key = client.engine_session_key
    if actual_engine_session_key is not None and (
        not isinstance(actual_engine_session_key, str)
        or not actual_engine_session_key
        or actual_engine_session_key != actual_engine_session_key.strip()
    ):
        raise ValueError(
            "engine client engine_session_key must be None or a normalized non-empty string"
        )
    if engine_session_key is not None and actual_engine_session_key != engine_session_key:
        raise RuntimeError(
            "engine client did not resume the requested native conversation: "
            f"expected={engine_session_key!r} actual={actual_engine_session_key!r}"
        )
    manifest = await client.get_capabilities()
    if not isinstance(manifest, EngineCapabilityManifest):
        raise TypeError(
            f"engine client returned an invalid capability manifest: {type(manifest).__name__}"
        )
    expected = str(expected_engine_kind or "").strip()
    if manifest.engine_kind != expected:
        raise TypeError(
            "engine capability manifest identity mismatch: "
            f"runtime={expected!r} manifest={manifest.engine_kind!r}"
        )
    from astrabox.core.service.orchestrator.engine.capabilities import (
        EngineRuntimeCapabilities,
    )
    from astrabox.core.service.orchestrator.engine.registry import (
        get_engine_adapter,
    )

    declared = get_engine_adapter(expected).capabilities
    if not isinstance(declared, EngineRuntimeCapabilities):
        raise TypeError(
            f"engine_kind={expected!r} has no validated runtime capability declaration"
        )
    validate_engine_client_manifest(client, manifest)
    runtime_permission_modes = tuple(manifest.permission_modes)
    missing_permission_modes = tuple(
        mode for mode in declared.permission_modes if mode not in runtime_permission_modes
    )
    if missing_permission_modes:
        raise TypeError(
            f"engine_kind={expected!r} runtime is missing required permission modes: "
            f"missing={missing_permission_modes!r} runtime={runtime_permission_modes!r}"
        )
    return manifest


def bound_engine_client_manifest(runtime: Any) -> EngineCapabilityManifest:
    """Return the construction-time manifest of a conversation-bound runtime."""

    client = getattr(runtime, "engine_client", None)
    manifest = getattr(runtime, "engine_manifest", None)
    if client is None or not isinstance(manifest, EngineCapabilityManifest):
        raise TypeError("live runtime has no validated engine capability manifest")
    if getattr(runtime, "conversation_bound", False) is not True:
        raise TypeError("live runtime has not bound its durable engine conversation")
    runtime_kind = str(getattr(runtime, "engine_kind", "") or "").strip()
    if manifest.engine_kind != runtime_kind:
        raise TypeError(
            "live runtime capability identity mismatch: "
            f"runtime={runtime_kind!r} manifest={manifest.engine_kind!r}"
        )
    validate_engine_client_manifest(client, manifest)
    return manifest


class EngineAdapter(ABC):
    """Engine adapter — declares and activates one engine in a prepared box.

    One adapter instance per engine_kind, registered into engine.registry.
    Adapters are stateless singletons; per-session state lives on the
    EngineClient returned by the runtime provisioning flow. Sandbox ownership,
    tracking, workspace policy, credentials and failed-start cleanup remain
    platform responsibilities.  The adapter owns only its box declaration and
    the vendor launch/configuration steps after the platform supplies a ready
    :class:`EngineStartupContext`.
    """

    @property
    @abstractmethod
    def engine_kind(self) -> EngineKind: ...

    @property
    @abstractmethod
    def engine_client_type(self) -> type[EngineClient]:
        """Concrete client class every runtime from this adapter publishes."""

        ...

    @property
    @abstractmethod
    def capabilities(self) -> "EngineRuntimeCapabilities":
        """This engine's explicit runtime shape and supported session products.

        There is deliberately no Claude-shaped base profile.  Inheriting a
        vendor's behavior is indistinguishable from falsely claiming that
        behavior, so every adapter must make the small declaration itself.
        """
        ...

    @abstractmethod
    def sandbox_request(
        self,
        *,
        template: Any,
        model_access: Any,
    ) -> Any:
        """Declare what this engine needs in a box, without creating one."""

        ...

    def startup_material_request(
        self,
        *,
        template: Any,
        model_access: Any,
        deployment_settings: Any,
    ) -> EngineStartupMaterialRequest:
        """Declare platform-owned startup material consumed by this engine."""

        _ = (template, model_access, deployment_settings)
        return EngineStartupMaterialRequest()

    @abstractmethod
    async def activate_runtime(
        self,
        context: EngineStartupContext,
    ) -> "Any":
        """Start only the vendor runtime inside the platform-prepared context."""

        ...

    def durable_session_message(
        self, message: dict[str, Any]
    ) -> SessionMessageFact | None:
        """Interpret a resident native event's user-facing, turn-free message."""
        return None

    def durable_child_resource_facts(
        self,
        raw_messages: list[dict[str, Any]],
    ) -> list[tuple[int, ChildResourceFact]]:
        """Recover child facts from this engine's durable native messages.

        Each tuple names the zero-based input message that authored the fact so
        core can preserve the journal's global ordering without inspecting the
        vendor payload. Engines whose durable messages carry no child-resource
        semantics return no facts.
        """
        return []

    def canonical_child_message_content(
        self, content: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Resolve vendor-equivalent encodings when reading retained child facts.

        The default preserves content exactly. Adapters with multiple native
        representations use the same normalization as their live producers.
        """
        return content

    def child_run_is_active(self, child_run: dict[str, Any]) -> bool:
        """Read native work activity from an already-folded child summary.

        An open resource can be idle but resumable. Only its engine can say
        whether the retained native lifecycle currently represents work,
        including queued work, that keeps the parent session active.
        """
        raise NotImplementedError(f"{self.engine_kind} does not declare child-run activity")

    #: Whether this adapter's ``prepare_runtime`` prepares a whole
    #: per-conversation box with NO in-box runner (``runner_uri=None``):
    #: preparation provisions the box and proves it can hold a conversation,
    #: rather than parking an engine process in a slot. What it proves differs
    #: by engine — a runtime resident in the image (Codex's app-server) is
    #: probed; an engine started per conversation (pi) has its box-boot
    #: prerequisites read back instead. Checked BEFORE any box is built for it.
    #: Engines that prepare inside an Agent-shared slot (a runner plus a
    #: parked engine child) leave this False even though they implement the
    #: method — the two placements need different machinery, and an Agent
    #: under conversation tenancy whose engine has only the slot form simply
    #: gets no prepared unit.
    prepares_conversation_box: bool = False

    def shared_conversation_service_launch(
        self, *, home: str, workspace: str, port: int
    ) -> str | None:
        """Shell line starting this engine's conversation service in a session.

        Runs inside the conversation's OpenSandbox isolated session — its own
        uid, home, and namespaces, with the box's network shared — and must
        background the service and return; the platform waits for ``port`` to
        listen and refuses loudly if it never does. The binary it starts must
        already be in the image (a capability lives in the image; using it is
        a trigger).

        ``None`` means this integration has no per-conversation service yet —
        the ``box_account`` placements — and a shared placement for it fails
        before anything is half-started.
        """

        _ = (home, workspace, port)
        return None
    async def prepare_runtime(
        self, context: EnginePreparationContext
    ) -> dict[str, Any]:
        """Prepare vendor state in an already platform-owned placement."""

        raise NotImplementedError(
            f"engine_kind={self.engine_kind!r} does not support preparation"
        )

    def detached_child_run_terminal(
        self,
        raw_event: dict[str, Any],
        *,
        transcript_refs: set[str],
        engine_refs: set[str],
        transcript_to_engine_ref: dict[str, str],
        activation_to_engine_ref: dict[str, str],
        observed_activations: dict[str, str],
        control_to_engine_ref: dict[str, str] | None = None,
    ) -> dict[str, str] | None:
        """Does this raw event end one detached child declared by the adapter?

        The engine and transcript references come from a manifest this same
        adapter produced. Activation references select the particular work
        declared by that manifest, not the child's lifetime identity.
        ``observed_activations`` is empty at each source's ordered scan and is
        maintained by the adapter for native events with omitted references.
        ``control_to_engine_ref`` carries the supplier's control aliases. Returns an adapter-
        authored terminal fact, or ``None`` when the event is unrelated.
        """
        raise NotImplementedError(
            f"engine_kind={self.engine_kind!r} emitted a detached-child manifest "
            "without a terminal matcher"
        )

    def detached_child_run_transcript_blocks(
        self,
        raw_scopes: list[dict[str, Any]],
        *,
        root_transcript_ref: str,
        engine_ref: str,
    ) -> list[dict[str, Any]]:
        """Project one detached child's durable transcript tree for the UI.

        ``raw_scopes`` carries opaque SessionStore subpaths and their raw
        entries. The platform deliberately does not interpret either. The
        adapter selects the tree rooted at its own transcript reference and
        returns private child-run facts anchored at ``engine_ref``. Engines
        without durable child transcripts return no blocks.
        """
        return []

    async def quiesce_and_save_runtime_state(
        self, sandbox: Any, *, runtime_identity: dict[str, Any]
    ) -> None:
        """Stop native state writers and confirm durable storage before release."""
        raise NotImplementedError(
            f"engine_kind={self.engine_kind!r} cannot quiesce and save runtime state"
        )

    def process_disposal(self) -> Any | None:
        """Optional vendor process finalizer for a durable turn anchor."""

        return None
