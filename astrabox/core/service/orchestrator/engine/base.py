"""Engine adapter base types.

``EngineAdapter`` declares one engine: the box it needs, how it is prepared
without a session, and how its runtime is activated in a prepared box. An
activated runtime is a :class:`~astrabox.core.service.orchestrator.engine.runtime.RuntimeClient`;
the runtime contract (``engine/runtime.py``) is how the platform reads and
drives it.

Adapters are looked up by engine_kind via the registry (engine.registry).
Transport, native payload schemas and vendor semantics stay inside the adapter.
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from astrabox.common.utils.errors import APIError

from astrabox.core.service.orchestrator.engine.emissions import (
    ChildResourceFact,
    EngineTurnEmission,
    PrivateDiagnostic,
    SessionMessageFact,
    TurnTerminal,
)

if TYPE_CHECKING:
    from astrabox.core.service.orchestrator.engine.runtime import RuntimeClient
    from astrabox.core.service.orchestrator.engine.capabilities import (
        EngineRuntimeCapabilities,
    )

EngineKind = str



class EngineOutputAlreadyObserved(RuntimeError):
    """A live host already owns the engine output connection."""


class EngineStreamDetached(RuntimeError):
    """The connection to the engine ended without the engine ending its response.

    A statement about the transport, never about the response: the link died,
    the process is shutting down, the proxy dropped, while the engine in the
    box may still be answering. The session reader reconnects and resumes from
    its cursor; it never ends a response because of this, since the box may
    still finish valid work. Only the sandbox being gone ends open responses.
    """


class RuntimeStateSaveRequired(APIError):
    """Native state is not durable; the exact allocation must remain recoverable."""

    def __init__(self, sandbox_id: str, runtime_identity: dict[str, Any]) -> None:
        super().__init__(
            code="RUNTIME_STATE_SAVE_REQUIRED", status_code=503,
            message="Native runtime state could not be saved; the sandbox is retained for recovery",
            data={"sandbox_id": sandbox_id, "phase": "native_state_save"},
        )
        self.sandbox_id = sandbox_id
        self.runtime_identity = dict(runtime_identity)


@dataclass(frozen=True)
class EngineCapabilityManifest:
    """Engine-declared capabilities for a session.

    Used by the platform to render capability chips in the UI and to gate
    operations (for example permission modes and child-run control).
    """

    engine_kind: EngineKind
    tools: list[str] = field(default_factory=list)
    #: Platform turn-content block types this connected engine consumes. Text
    #: is mandatory; an adapter adds another type only
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
    # as stopping a response are part of the runtime contract, not feature flags.
    supports_interaction: bool = False
    supports_child_run_control: bool = False
    supports_server_info: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


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
    #: The native profile can belong to a workspace or to one Session's account.
    runtime_state_scope: Literal["workspace", "session"] = "workspace"


@dataclass(frozen=True)
class EngineStartupContext:
    """A platform-prepared runtime handed to one engine adapter.

    The platform has already selected or claimed the box, mounted and prepared
    its durable workspace, installed protected credentials, recorded cleanup
    ownership, and restored any platform-managed transcript before constructing
    this value.  The adapter can now do one thing: activate its vendor runtime
    inside the supplied box and return a runtime whose client serves the runtime contract.

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
    #: RuntimeProfile delivered through the engine's native activation contract.
    session_env: dict[str, str] | None = field(default=None, repr=False)
    #: Host-side refresh a client runs before handing its runtime an input
    #: (protected MCP credentials rotated since the runtime started).
    prepare_engine_input: Any = None
    prepared_manifest: dict[str, Any] | None = None
    service_credential: str | None = field(default=None, repr=False)
    runner_uri: str | None = None
    user_id: str | None = None
    permission_mode: str | None = None
    deployment_settings: Any = None
    capability_scope: Any = None
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


def validate_engine_client_type(client_type: type[Any]) -> None:
    """Reject an adapter whose declared client does not serve the runtime contract.

    Registration runs this before the engine appears in a product catalog.
    """
    from astrabox.core.service.orchestrator.engine.runtime import validate_runtime_client_type

    validate_runtime_client_type(client_type)


@runtime_checkable
class EngineStoredChildRuns(Protocol):
    """Read children recorded only in the native SessionStore, without journal writes.

    The adapter resolves its own task, run and transcript identities from the
    main transcript and sub-transcripts. It returns lifecycle facts for children
    missing from the stream; streamed lifecycle remains authoritative.
    """

    def stored_child_run_facts(
        self, *, main_entries: list[dict[str, Any]], raw_scopes: list[dict[str, Any]]
    ) -> list[ChildResourceFact]: ...


@runtime_checkable
class EngineStoredChildTranscript(Protocol):
    """Optional authoritative child history read from the Session's SessionStore mirror.

    ``raw_scopes`` are the runtime's sub-transcripts for the Session, each
    ``{"project_key", "session_id", "subpath", "entries"}`` with the entries as
    the runtime wrote them. The adapter resolves its own child identity and
    returns the child's complete message facts, or none when the mirror holds
    no transcript of that child. ``native_state`` is the adapter-owned metadata
    retained with its child lifecycle. This read does not require a running sandbox
    or change child lifecycle.
    """

    def stored_child_transcript_facts(
        self,
        *,
        engine_ref: str,
        closed: bool,
        native_state: dict[str, Any],
        raw_scopes: list[dict[str, Any]],
    ) -> list[ChildResourceFact]: ...


@runtime_checkable
class EngineStoredChildToolResults(Protocol):
    """Optional adapter read of missing child tool output from mirrored vendor records."""

    def enrich_stored_child_tool_results(
        self,
        *,
        engine_ref: str,
        raw_scopes: list[dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]: ...


def validate_manifest_vocabulary(manifest: EngineCapabilityManifest) -> None:
    """Reject malformed permission-mode and input-content declarations."""

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


def bound_engine_client_manifest(runtime: Any) -> EngineCapabilityManifest:
    """Return the construction-time manifest of a conversation-bound runtime."""

    client = getattr(runtime, "engine_client", None)
    manifest = getattr(runtime, "engine_manifest", None)
    if client is None or not isinstance(manifest, EngineCapabilityManifest):
        raise TypeError("live runtime has no validated engine capability manifest")
    runtime_kind = str(getattr(runtime, "engine_kind", "") or "").strip()
    if manifest.engine_kind != runtime_kind:
        raise TypeError(
            "live runtime capability identity mismatch: "
            f"runtime={runtime_kind!r} manifest={manifest.engine_kind!r}"
        )
    validate_manifest_vocabulary(manifest)
    return manifest


class EngineAdapter(ABC):
    """Engine adapter — declares and activates one engine in a prepared box.

    One adapter instance per engine_kind, registered into engine.registry.
    Adapters are stateless singletons; per-session state lives on the
    runtime client returned by the runtime provisioning flow. Sandbox ownership,
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
    def engine_client_type(self) -> type[RuntimeClient]:
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


    def startup_environment_scope(self) -> Literal["process", "session"] | None:
        """Where activation delivers the provider's Session environment."""
        return None

    def supports_initialized_environment(self) -> bool:
        """Whether native activation can acknowledge environment after initialization."""
        return False

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
        """Start the vendor runtime inside the platform-prepared context.

        A workspace-native state store keeps release custody until activation
        settles. On failure, stop its writers and confirm the save before
        raising; if that cannot complete, raise RuntimeStateSaveRequired so
        cleanup retains the allocation and its private runtime identity.
        """

        ...


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

    #: Whether this engine's durable child record is reachable only through
    #: the engine. A child-run read attaches the engine to refresh it only when
    #: this is True; otherwise the read refreshes through a runtime the server
    #: already holds and serves the journal as it is when there is none.
    child_record_lives_in_engine: bool = False

    def child_run_fact_reopens(self, data: dict[str, Any]) -> bool:
        """Whether a lifecycle fact that does not close a child revives a closed one.

        The child-run fold asks only when the child's latest applied fact
        closed it, and skips a fact this answers False for: the engine reads
        that fact as a report on the activation the close ended, journaled
        after it. The default answers True, so an engine that says nothing
        here has every such fact taken as the child living again.
        """
        return True

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

    def conversation_install_script(
        self,
        *,
        packages: list[dict[str, str]],
        engine_options: dict[str, Any],
        mcp_servers: dict[str, dict[str, Any]],
    ) -> str | None:
        """Bash the conversation bootstrap runs to install this engine's extensions.

        ``packages`` are the Agent's packages of the kinds this engine declares
        ``installed_by_engine``, each ``{"kind", "source"}`` with ``source`` the
        cached checkout directory; ``engine_options`` are the Agent's native
        options, for an engine whose own installer takes them; ``mcp_servers``
        are the author's servers (``author_mcp_servers``), for an engine whose
        MCP configuration is a file read when it starts. The script runs
        as the conversation's workload account with ``HOME`` set, before the
        engine starts, and must leave exactly what they declare installed,
        removing what it installed for an earlier configuration. ``None`` when
        there is nothing to run.
        """

        _ = (packages, engine_options, mcp_servers)
        return None

    def resident_restart_script(self) -> str | None:
        """Bash, run as root, that makes the image's resident service read an install.

        On a box whose engine is the image's own service, started at boot
        (the conversation tenancy), that service is already running when the
        bootstrap runs :meth:`conversation_install_script`. The bootstrap runs
        this right after an install there, with ``HOME`` the workload
        account's, and it must return only once the service answers again.
        ``None`` when the running service sees the install without a restart
        (Codex reads plugins per thread).
        """

        return None

    def install_egress_hosts(self, engine_options: dict[str, Any]) -> list[str]:
        """Hosts this engine's own installer reaches for the Agent's native options.

        The sources an author names in a native document (a DeepSeek Harness
        Bundle's npm version, git spec or tarball URL) are the author's network
        hosts, admitted within the same boundary as a Skill's or Plugin's Git
        origin. Empty when the engine installs nothing from its options.
        """

        _ = engine_options
        return []

    async def prepare_runtime(
        self, context: EnginePreparationContext
    ) -> dict[str, Any]:
        """Prepare vendor state in an already platform-owned placement."""

        raise NotImplementedError(
            f"engine_kind={self.engine_kind!r} does not support preparation"
        )


    async def quiesce_and_save_runtime_state(
        self, sandbox: Any, *, runtime_identity: dict[str, Any]
    ) -> None:
        """Stop native state writers and confirm durable storage before release."""
        raise NotImplementedError(
            f"engine_kind={self.engine_kind!r} cannot quiesce and save runtime state"
        )

    async def list_scheduled_jobs(
        self, sandbox: Any, *, runtime_identity: dict[str, Any]
    ) -> list[dict[str, Any]] | None:
        """The jobs the program scheduled for itself, read from its own store in a running box.

        ``None`` means the program keeps no schedule of its own. One that does
        answers its records in its own vocabulary, which the platform carries
        to the owner unchanged.
        """

        return None

    def process_disposal(self) -> Any | None:
        """Optional vendor process finalizer for a recorded native conversation."""

        return None
