"""Sandbox seam — the ``SandboxProvider`` contract, its data-plane transport
types, and its registry.

One provider exposes the atomic capabilities of its sandbox platform: creation
(:meth:`create_sandbox`), backend-wide decisions that sit outside the data plane
(SDK sandbox-class selection,
create/connect credential wiring, network-policy shape, backend secret material,
optional surfaces such as a web shell), the by-id lifecycle operations (connect /
kill / renew / read expiry / probe liveness / resolve an in-box port endpoint when
only the sandbox **id** is known — the multi-machine / post-restart case where the
runtime object is not in this process's memory), *and* the in-box transport itself:
the provider builds its
own :class:`SandboxDataPlane` (HTTP requests to an in-box port) from whatever it has
— a live sandbox object or a bare persisted endpoint string — so the platform's
business logic never sees a scheme, host, presign token, or transport class. The live
agent stream is not part of this HTTP data plane: the engine adapter owns its
vendor protocol after the platform has selected and prepared the placement.

Lifecycle dispatch is keyed by the sandbox's persisted backend name (read off the
session row), never guessed from the id string. Capability flags carry the defaults
a provider keeps unless its behaviour differs.

Destruction is not on that by-id list by accident. A backend's ``kill`` is the
delete REQUEST; the platform never calls it directly. It goes through
:meth:`SandboxProvider.confirm_destroyed`, the single place a
:class:`~astrabox.seams.sandbox_disposal.SandboxDestruction` verdict is minted,
so "on what evidence was this box declared gone" has one answer for every
backend — and :meth:`SandboxProvider.claim_of` answers the other half, "whose
box is it", from the ownership metadata the create wrote. Both verdicts refuse
to be read as booleans; see :mod:`astrabox.seams.sandbox_disposal` for why the
optimistic collapse had to be made impossible rather than discouraged.

Alongside the by-id lifecycle sits a read-only CONTROL-PLANE INVENTORY face
(:meth:`SandboxProvider.list_sandboxes` / :meth:`~SandboxProvider.describe_sandbox`
/ :meth:`~SandboxProvider.read_diagnostics`): what a backend is running, seen as a
resource rather than as the side effect of a session. It is optional and every
default refuses loud with a 501 naming the backend — a backend that cannot answer
says so, and never returns an empty page that would read as "nothing is running".

To add a backend: implement :class:`SandboxProvider`, call
:func:`register_sandbox` at import, and advertise the implementation under the
``astrabox.providers.sandbox`` entry-point group so it is discovered once
installed. Lookups fail loud on an unknown name. The **only** fallback is the
process-wide default backend, which is deployment configuration: the composition
root calls :func:`set_default_sandbox_backend` once at bootstrap (from settings);
no backend name is hardcoded anywhere in the dispatch path.

Installed is not enabled. Registration says a provider is installed in this
process; the composition root then publishes the deployment's enabled set
(:func:`set_enabled_sandbox_backends`), and every lookup and aggregation reads
only that set: name resolution, the backend and permission enums an Environment
form offers, capability questions, reverse lookup of a live handle, background
inventory, and shutdown. A record that names an installed backend this
deployment has not enabled fails with :class:`SandboxBackendDisabled` instead of
being served by another backend. A process that never ran the composition root
(a script, a unit test) has no enabled set, and every installed provider answers.

This module imports only the standard library plus import-light sibling seams and
``astrabox.common.utils.errors`` (the transport-neutral error type) — never the
orchestrator core, so a provider distribution can depend on the seam without
dragging the host in.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
import contextlib
import inspect
import json as _json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import TYPE_CHECKING
from typing import Any, Literal, Mapping, Protocol, cast, runtime_checkable

from astrabox.common.utils.errors import APIError
from astrabox.seams.egress_credentials import SandboxEgressCredentialPlan
from astrabox.seams.sandbox_data import (
    ProcessExit,
    ProcessFrame,
    ProcessOutput,
    ProcessReplay,
    SandboxCommandOutcomeUnknown,
    SandboxCommandRefused,
    SandboxCommandResult,
    SandboxCommands,
    SandboxDirectorySpec,
    SandboxEndpoint,
    SandboxEntry,
    SandboxEntryKind,
    SandboxFiles,
    SandboxPathNotFound,
    SandboxPathRejected,
    SandboxProcessAlreadyAttached,
    SandboxProcessAttachment,
    SandboxProcessDetached,
    SandboxProcessReplayGap,
    SandboxProcesses,
)
from astrabox.seams.sandbox_disposal import (
    SandboxClaim,
    SandboxDestruction,
    claim_from_metadata,
)

if TYPE_CHECKING:
    from astrabox.seams.storage import WorkspaceRoute

#: Entry-point group an external package registers a provider under.
ENTRY_POINT_GROUP = "astrabox.providers.sandbox"

#: Probe status values; a conforming provider never returns a fourth.
SANDBOX_LIFECYCLE_PROBE_OK = "OK"
SANDBOX_LIFECYCLE_PROBE_NOT_FOUND = "NOT_FOUND"
SANDBOX_LIFECYCLE_PROBE_FAILED = "PROBE_FAILED"

SANDBOX_NETWORK_UNRESTRICTED = "unrestricted"
SANDBOX_NETWORK_LIMITED = "limited"
SANDBOX_NETWORK_MODES = (
    SANDBOX_NETWORK_UNRESTRICTED,
    SANDBOX_NETWORK_LIMITED,
)


@dataclass(frozen=True, slots=True)
class SandboxNetworkPolicy:
    """Provider-neutral outbound reachability for one sandbox.

    This policy answers only whether a destination is reachable. Credential
    storage and request-time injection travel through ``vault_write`` on
    :class:`SandboxCreateSpec` and are deliberately absent here: a provider
    that advertises both capabilities must preserve that independence or
    refuse an unsupported combination before allocation. It may not use one
    input to widen, narrow, enable, or disable the other.

    ``unrestricted`` carries no hosts because every destination is reachable.
    ``limited`` denies destinations not named by ``allowed_hosts``. Targets are
    opaque provider-neutral host patterns at this seam; each provider maps
    them to its own policy vocabulary or refuses the policy before creating a
    sandbox.
    """

    mode: Literal["unrestricted", "limited"]
    allowed_hosts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in SANDBOX_NETWORK_MODES:
            raise ValueError(
                f"sandbox network mode must be one of {SANDBOX_NETWORK_MODES}, "
                f"got {self.mode!r}"
            )
        if self.mode == SANDBOX_NETWORK_UNRESTRICTED and self.allowed_hosts:
            raise ValueError("unrestricted sandbox networking cannot carry allowed_hosts")


#: How long a confirmed destruction may wait for an ACCEPTED delete to settle.
#: Kubernetes deletes a workload asynchronously — the record stays visible
#: until pod teardown completes — so the second observation re-asks within
#: this budget instead of reporting a terminating box as undead. Below the
#: platform's 30s sandbox-control deadline on purpose: the caller's timeout
#: stays the harder wall.
DESTRUCTION_SETTLE_BUDGET_S = 20.0
DESTRUCTION_SETTLE_INTERVAL_S = 1.0


@dataclass(frozen=True, slots=True)
class SandboxBrowserEndpoint:
    """A sandbox service address that can be handed to a browser.

    ``headers`` is retained so a provider can report that its endpoint needs
    request headers. The browser-facing service rejects such a result instead
    of returning a link an address-bar navigation cannot use. ``expires_at`` is
    present only for a time-limited endpoint minted by the sandbox platform.
    """

    endpoint: str
    headers: Mapping[str, str] = field(default_factory=dict)
    expires_at: datetime | None = None
    signed: bool = False


@runtime_checkable
class SandboxHandle(Protocol):
    """The contract core holds on a created/connected sandbox.

    This is the shape behind the handle returned by
    :meth:`SandboxProvider.create_sandbox` / :meth:`SandboxProvider.connect`.
    Core relies on:

    * **identity** — ``sandbox_id`` (``extract_sandbox_id`` probes
      ``code_interpreter_id`` / ``sandbox_id`` / ``id`` in that order; expose at
      least one, ``sandbox_id`` preferred);
    * **endpoint resolution** — ``await get_endpoint(port)`` returning the
      :class:`SandboxEndpoint` at which the host reaches that in-box port, with
      every header a request to it needs; raise when the port is not published
      rather than returning a wrong address. ``await get_signed_endpoint(port,
      expires_at=...)`` returns one whose URL itself carries a credential valid
      until ``expires_at``, for a client that cannot send headers; a backend
      that cannot sign raises the 501 ``SANDBOX_SIGNED_ENDPOINT_UNSUPPORTED``;
    * **files** — the box's filesystem as :class:`SandboxFiles`;
    * **commands** — command execution in the box as :class:`SandboxCommands`;
      and
    * **processes** — long-lived processes in the box as
      :class:`SandboxProcesses` (all four types in
      :mod:`astrabox.seams.sandbox_data`). A handle whose provider does not
      declare :attr:`SandboxProvider.supports_process_sessions` still has the
      property and returns :class:`UnsupportedSandboxProcesses`, whose every
      operation raises the 501 ``SANDBOX_PROCESS_SESSIONS_UNSUPPORTED``.

    Two further methods are OPTIONAL and attempted best-effort by the
    terminate path (``await handle.kill()`` then ``await handle.close()``,
    failures tolerated): a backend whose handle lacks them is destroyed
    through the by-id
    :meth:`SandboxProvider.kill` fallback instead, so absence is never an
    error. Declare them on your handle when the SDK supports direct teardown;
    they are deliberately NOT part of this protocol so a minimal handle stays
    minimal.

    Handles must be durable value objects: they survive the provider call that
    created them and may be rebuilt by :meth:`SandboxProvider.connect` on
    another process (the multi-machine / post-restart case).
    """

    @property
    def sandbox_id(self) -> str: ...

    @property
    def files(self) -> SandboxFiles: ...

    @property
    def commands(self) -> SandboxCommands: ...

    @property
    def processes(self) -> SandboxProcesses: ...

    async def get_endpoint(self, port: int = ...) -> SandboxEndpoint: ...

    async def get_signed_endpoint(self, port: int, *, expires_at: datetime) -> SandboxEndpoint: ...


@dataclass(frozen=True, slots=True)
class SandboxClientPoolMember:
    """Provider-issued identity for one not-yet-claimed pool member.

    Every create gets its own identity, so any number of members can be
    created concurrently. The platform receives it only so the complete
    :class:`SandboxCreateSpec` carries it; it remains unrelated to any eventual
    user Session.
    """

    pool_name: str
    session_id: str
    assignment_id: str


SandboxClientPoolCreator = Callable[
    [SandboxClientPoolMember], Awaitable[SandboxHandle]
]
SandboxClientPoolPreparer = Callable[[SandboxHandle], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class SandboxClientPoolSpec:
    """Provider-neutral operating envelope for one client-side sandbox pool.

    The platform chooses the pool's identity, desired ready capacity and lease
    budgets.  The provider supplies the maintained pooling mechanism and calls
    the platform-provided creator/preparer for each member; neither side takes
    over the other's product vocabulary.
    """

    pool_name: str
    creation_image: str
    max_idle: int
    idle_timeout_seconds: int
    preparation_timeout_seconds: int

    def __post_init__(self) -> None:
        name = str(self.pool_name or "").strip()
        if not name:
            raise ValueError("sandbox client pool requires pool_name")
        image = str(self.creation_image or "").strip()
        if not image:
            raise ValueError("sandbox client pool requires creation_image")
        if int(self.max_idle) <= 0:
            raise ValueError("sandbox client pool max_idle must be positive")
        if int(self.idle_timeout_seconds) <= 0:
            raise ValueError(
                "sandbox client pool idle_timeout_seconds must be positive"
            )
        if int(self.preparation_timeout_seconds) <= 0:
            raise ValueError(
                "sandbox client pool preparation_timeout_seconds must be positive"
            )
        object.__setattr__(self, "pool_name", name)
        object.__setattr__(self, "creation_image", image)


@dataclass(frozen=True, slots=True)
class SandboxClientPoolReadiness:
    """Whether this deployment gives the backend's client pool what it needs.

    ``detail`` names the missing piece when ``available`` is False, in the
    backend's own terms (a coordination store, a credential), so a refusal
    can say what to configure.
    """

    available: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class SandboxClientPoolStatus:
    """The supplier pool's observable state, without supplier SDK types."""

    pool_name: str
    lifecycle_state: str | None
    ready: bool
    idle_count: int
    max_idle: int
    failure_count: int | None = None
    backoff_active: bool | None = None
    in_flight_operations: int | None = None
    last_error: bool | None = None
    idle_sandbox_ids: tuple[str, ...] = ()


class SandboxLifecycleProbeResult:
    """Result of a by-id liveness probe (``probe_status`` is one of the three
    ``SANDBOX_LIFECYCLE_PROBE_*`` constants)."""

    __slots__ = ("probe_status", "sandbox_state", "error_text")

    def __init__(
        self,
        *,
        probe_status: str,
        sandbox_state: str | None = None,
        error_text: str | None = None,
    ) -> None:
        self.probe_status = probe_status
        self.sandbox_state = sandbox_state
        self.error_text = error_text


@dataclass(slots=True)
class SandboxHttpResponse:
    """Neutral HTTP response shape returned by every backend's ``request``.

    ``status_code`` is coerced to ``int`` and ``text`` normalised to ``""`` when falsy.
    """

    status_code: int
    text: str = field(default="")

    def __post_init__(self) -> None:
        self.status_code = int(self.status_code)
        self.text = self.text or ""

    def json(self) -> Any:
        return _json.loads(self.text or "")


class SandboxDataPlane(ABC):
    """A backend-specific handle for talking to one sandbox's in-box services.

    The async-context-manager methods are concrete no-ops (each request manages its own
    client/connection); ``request`` is the contract. The live agent stream is an exec
    concern the engine client owns, not part of this HTTP data plane.
    """

    async def __aenter__(self) -> "SandboxDataPlane":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    @abstractmethod
    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> SandboxHttpResponse:
        """Issue an in-box HTTP request to the sandbox port this plane is bound to."""


@dataclass(frozen=True)
class SandboxCreateSpec:
    """Backend-neutral inputs for provisioning one sandbox (no agent start).

    Carries only concepts every backend understands; a provider maps them onto its
    own create primitive (a container run, a cloud ``sandbox_class(...).create()``,
    …). ``env`` is the boot environment baked into the box at create;
    ``publish_ports`` are in-box service ports the host must be able to reach —
    a declaration of reachability, not of mechanism, so a provider whose
    platform resolves an address for ANY in-box port on demand (see
    :meth:`SandboxProvider.resolve_endpoint`) has nothing to do at create time
    and ignores the field;
    ``wait_for_inbox_service_port`` optionally blocks the create until the in-box
    service on that port accepts requests (the image's readiness contract), so the
    caller receives a handle that is ready to serve, not merely started.

    ``entrypoint`` is the image-specific process that must start the sandbox.
    ``None`` asks the provider to use its normal agent-image entrypoint; runtime
    adapters set it when their image has a different boot contract. An empty
    tuple is not meaningful and providers must not silently turn it into an
    unrelated keepalive command.

    ``assignment_id`` is the immutable identity of this one durable create
    command. A provider that advertises correlated create carries it through
    allocation and can recover the exact resource after the caller dies before
    publishing its sandbox id. It is not the Session id: a Session may replace
    its sandbox, and each replacement command receives another assignment.

    ``execution_target`` declares the physical owner before any database binding
    can be published. Providers persist :func:`execution_target_metadata` in
    create metadata and preserve it on adoption. Inventory maintenance uses this
    positive ownership fact even while creation is still in progress.

    ``resource_limits`` and ``resource_requests`` are the platform's explicit
    workload envelope. Both are required and non-empty: letting either fall
    through would hand scheduling policy to a provider default and, on
    OpenSandbox, omitting requests silently makes them equal to the limits.

    ``network_policy`` is the Environment's validated outbound reachability.
    A caller must check
    :attr:`SandboxProvider.supports_create_network_policy` before supplying
    one; silently dropping a requested security control is not an allowed
    provider behavior. ``credential_proxy_enabled`` independently asks the
    provider to prepare protected credential injection without putting a
    credential in the box. A prepared box can therefore establish that
    capability before a workload exists; the real credential is written only
    when the workload enters the box. ``vault_write`` is AstraBox's
    provider-neutral protected-credential plan, never a provider SDK object and
    never part of ``env``. It is excluded from repr because it contains secret
    values. Supplying it implies protected injection is enabled. It does not
    change the Environment's ``network_policy`` document or depend on a network
    mode, but a provider must admit each exact binding destination in its
    effective containment policy; otherwise the platform-managed credential
    would be installed behind a network rule that makes it unusable. Providers
    advertising both capabilities must preserve that distinction or refuse an
    unsupported combination before allocation.

    ``permission_level`` is the Environment's requested sandbox grant. Like a
    network policy, it is a security contract rather than a hint: a provider
    maps it to its create primitive or refuses it before returning a box.

    ``metadata`` carries opaque platform facts that must stay attached to this
    physical box across a client-pool handoff. Providers transport these values
    without interpreting them and preserve them when runtime ownership is
    adopted. A provider that cannot do so must refuse a non-empty mapping.

    ``death_callback_url`` is the push half of the sandbox-death convergence
    capability: a per-sandbox platform URL (see
    ``sandbox_lifecycle.build_sandbox_callback_url``) the provider should hit —
    best-effort, POST, body ``{"status": "terminated"}`` — when the sandbox
    reaches a terminal state, by whatever mechanism the backend has (a control
    plane webhook, a pre-stop hook baked into the box, an event watcher). A
    provider with no such mechanism ignores the field; the platform's probe
    reconciler (the pull half) converges those sessions within a watcher tick.
    """

    session_id: str
    assignment_id: str
    execution_target: str
    resource_limits: dict[str, str]
    resource_requests: dict[str, str]
    image: str | None = None
    entrypoint: tuple[str, ...] | None = None
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    metadata: Mapping[str, str] = field(default_factory=dict)
    #: ``(box_path, subpath)`` pairs naming what must outlive this box.
    #: The platform plans WHERE a subject's durable files live
    #: (`plan_subject_storage_mounts`); by the time a create carries them, this
    #: backend's workspace route has rewritten them into what its runtime
    #: mounts, with ``workspace_volume``. A provider that cannot mount anything
    #: must refuse a non-empty value rather than create a box whose files
    #: vanish with it.
    workspace_mounts: tuple[tuple[str, str], ...] = ()
    workspace_volume: str | None = None
    """Volume the backend's workspace route provisioned; the create only mounts it."""
    workspace_volume_create_if_missing: bool = True
    publish_ports: tuple[int, ...] = ()
    wait_for_inbox_service_port: int | None = None
    #: Whether the create must prove the box's command channel before returning.
    #: Separate from ``wait_for_inbox_service_port`` because they answer
    #: different questions — one is "the image's own service is serving", the
    #: other is "the platform can run a command in here at all" — and a box can
    #: have either without the other. Deriving one from the other gave every
    #: caller that waited on a port a command-channel proof it never asked for,
    #: and every caller that did not, none.
    requires_command_channel: bool = False
    death_callback_url: str | None = None
    network_policy: SandboxNetworkPolicy | None = None
    permission_level: str = "default"
    credential_proxy_enabled: bool = False
    vault_write: SandboxEgressCredentialPlan | None = field(
        default=None,
        repr=False,
    )

    def __post_init__(self) -> None:
        for field_name, resources in (
            ("resource_limits", self.resource_limits),
            ("resource_requests", self.resource_requests),
        ):
            if not resources:
                raise ValueError(f"sandbox create requires non-empty {field_name}")
            if any(
                not str(resource).strip() or not str(quantity).strip()
                for resource, quantity in resources.items()
            ):
                raise ValueError(
                    f"sandbox create {field_name} must contain non-empty names and quantities"
                )
        if any(
            not str(key).strip() or not str(value).strip()
            for key, value in self.metadata.items()
        ):
            raise ValueError("sandbox create metadata must contain non-empty keys and values")


SandboxAllocationScope = Literal["sandbox", "isolated_sessions"]


@dataclass(frozen=True, slots=True)
class SandboxAllocation:
    """The sandbox resource one runtime startup owns until publication.

    A startup either owns a whole sandbox or owns exact isolated sessions in a
    longer-lived sandbox.  The distinction is lifecycle authority, not engine
    semantics: every engine uses the same value and the platform can release it
    after a worker restart without knowing which vendor loop ran inside.

    ``sandbox_backend`` is persisted with the address because a sandbox id does
    not encode its provider.  ``isolated_session_ids`` is present only for the
    shared-box shape; destroying that physical sandbox would terminate sibling
    conversations, so an incomplete record is refused at construction time.
    """

    sandbox_id: str
    sandbox_backend: str
    scope: SandboxAllocationScope
    isolated_session_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        sandbox_id = str(self.sandbox_id or "").strip()
        sandbox_backend = str(self.sandbox_backend or "").strip().lower()
        isolated_session_ids = tuple(
            dict.fromkeys(
                str(item or "").strip()
                for item in self.isolated_session_ids
                if str(item or "").strip()
            )
        )
        if not sandbox_id:
            raise ValueError("sandbox allocation requires sandbox_id")
        if not sandbox_backend:
            raise ValueError("sandbox allocation requires sandbox_backend")
        if self.scope not in {"sandbox", "isolated_sessions"}:
            raise ValueError(f"unknown sandbox allocation scope {self.scope!r}")
        if self.scope == "sandbox" and isolated_session_ids:
            raise ValueError("whole-sandbox allocation cannot name isolated sessions")
        if self.scope == "isolated_sessions" and not isolated_session_ids:
            raise ValueError("isolated-session allocation requires session ids")
        object.__setattr__(self, "sandbox_id", sandbox_id)
        object.__setattr__(self, "sandbox_backend", sandbox_backend)
        object.__setattr__(self, "isolated_session_ids", isolated_session_ids)

    def as_record(self) -> dict[str, Any]:
        return {
            "sandbox_id": self.sandbox_id,
            "sandbox_backend": self.sandbox_backend,
            "scope": self.scope,
            "isolated_session_ids": list(self.isolated_session_ids),
        }

    @classmethod
    def from_record(cls, value: Any) -> "SandboxAllocation":
        if not isinstance(value, dict):
            raise ValueError("sandbox allocation record must be an object")
        raw_ids = value.get("isolated_session_ids")
        if raw_ids is None:
            isolated_session_ids: tuple[str, ...] = ()
        elif isinstance(raw_ids, list):
            isolated_session_ids = tuple(str(item or "") for item in raw_ids)
        else:
            raise ValueError("isolated_session_ids must be a list")
        scope = str(value.get("scope") or "")
        if scope not in {"sandbox", "isolated_sessions"}:
            raise ValueError(f"unknown sandbox allocation scope {scope!r}")
        return cls(
            sandbox_id=str(value.get("sandbox_id") or ""),
            sandbox_backend=str(value.get("sandbox_backend") or ""),
            scope=cast(SandboxAllocationScope, scope),
            isolated_session_ids=isolated_session_ids,
        )


@dataclass(frozen=True)
class SandboxRuntimeDefaults:
    """A provider's deploy-time defaults, read at seed/bootstrap time.

    ``runtime_image`` is the default sandbox image a fresh install should seed its
    first environment with; ``agent_command`` is the in-box agent CLI it runs. Both
    may be ``None`` when a backend has no opinion (the caller then requires explicit
    configuration). This keeps a provider's seed defaults in one place rather than
    duplicated by hand across the composition root.
    """

    runtime_image: str | None = None
    agent_command: str | None = None


#: Metadata key a provider writes so a live sandbox can be traced back
#: to its AstraBox runtime owner: a Session for a dedicated box, or the Agent
#: runtime for an Agent-shared box. The inventory face reads exactly this key:
#: a sandbox whose metadata does not carry it reports ``session_id = None``
#: rather than a guess derived from the id string or the create order.
SANDBOX_SESSION_ID_METADATA_KEY = "astrabox.session-id"

#: The second metadata pair every box this deployment creates carries. Together
#: with :data:`SANDBOX_SESSION_ID_METADATA_KEY` it is the ownership FACT the
#: disposal judgement reads off a live box (see
#: :func:`astrabox.seams.sandbox_disposal.claim_from_metadata`): the session key
#: alone cannot tell a box this deployment created from one another deployment
#: created for a session id that happens to collide.
SANDBOX_MANAGED_BY_METADATA_KEY = "astrabox.managed-by"
SANDBOX_MANAGED_BY_METADATA_VALUE = "astrabox"

#: Immutable identity of one attempt to obtain a physical sandbox. Unlike the
#: Session id, this value changes when a Session replaces its box. Providers
#: that support correlated create carry it through allocation and can find the
#: resulting resource by it after the creating worker dies before publication.
SANDBOX_ASSIGNMENT_ID_METADATA_KEY = "astrabox.assignment-id"

#: The AstraBox installation that created a box: its value is the id the
#: installation keeps in its own database (:func:`sandbox_installation_id`).
#: Installations that share one control plane — a Docker daemon, or a
#: Kubernetes namespace — each see the other's boxes in the backend inventory,
#: and the pair above carries the same value for both. A path that acts on a box
#: it found by listing the backend, rather than by a row in its own database,
#: acts only on boxes carrying this installation's value.
SANDBOX_INSTALLATION_METADATA_KEY = "astrabox.installation"

#: Physical execution ownership, present before a create can publish its database binding.
SANDBOX_EXECUTION_TARGET_METADATA_KEY = "astrabox.execution-target"


def execution_target_metadata(execution_target: str) -> dict[str, str]:
    """The full target identity as an opaque, label-safe ownership token.

    A backend carries this mapping unchanged from creation through adoption.
    Inventory sweeps use the same token before touching unbound resources.
    The readable target name remains on the platform's persistent records.
    """
    if not execution_target or execution_target != execution_target.strip():
        raise ValueError("sandbox execution target must be non-empty and unpadded")
    token = base64.b32encode(hashlib.sha256(execution_target.encode()).digest())
    return {SANDBOX_EXECUTION_TARGET_METADATA_KEY: token.decode().rstrip("=").lower()}

#: The diagnostic scopes the inventory face accepts. Each names one report a
#: backend may be able to produce about one sandbox; nothing here promises a
#: backend can produce any of them (see :meth:`SandboxProvider.read_diagnostics`).
SANDBOX_DIAGNOSTIC_SCOPES = ("summary", "inspect", "events", "logs")


@dataclass(frozen=True, slots=True)
class SandboxDescriptor:
    """One sandbox as its backend's control plane describes it.

    Read-only inventory, for an operator looking at what a backend is actually
    running. Every field is copied from the control plane verbatim — nothing is
    inferred, defaulted or filled in when the backend reports a value the
    operator may find surprising; the panel shows what the backend said.

    ``session_id`` is the provider's wire value for the AstraBox runtime owner,
    read from the ``astrabox.session-id`` create metadata
    (:data:`SANDBOX_SESSION_ID_METADATA_KEY`) and ``None`` when the box carries
    no such key — a sandbox this deployment did not create, or one created by a
    path that predates the metadata. It is the metadata ownership claim, not a
    join against the Session or Agent stores.
    """

    sandbox_id: str
    state: str
    created_at: datetime | None = None
    expires_at: datetime | None = None
    image: str | None = None
    entrypoint: tuple[str, ...] = ()
    metadata: Mapping[str, str] = field(default_factory=dict)
    session_id: str | None = None
    endpoint: str | None = None
    """Where this box actually answers, as the backend resolves it.

    ``None`` when the backend cannot say — it does not pool, the box is not
    running, or resolving would cost a connect this read is not willing to
    make. It is never inferred: an address that is wrong is worse than absent,
    because everything that would use one (an operator opening a shell, a probe
    reaching into the box) fails somewhere else entirely.

    It exists because nothing else publishes it. On the kubernetes runtime a
    sandbox is a Pod, the Pod carries only pool labels, and the control plane's
    record has no address — so the platform is the ONLY party that knows which
    box belongs to which session, and it was keeping that to itself.
    """


@dataclass(frozen=True, slots=True)
class SandboxPage:
    """One page of :class:`SandboxDescriptor` plus the backend's own paging counters.

    The counters are the control plane's, not a re-derivation: a caller pages by
    asking for the next ``page`` while ``has_next_page`` holds, and never has to
    assume ``total_items`` fits in one response.
    """

    items: tuple[SandboxDescriptor, ...]
    page: int
    page_size: int
    total_items: int
    total_pages: int
    has_next_page: bool


@dataclass(frozen=True, slots=True)
class SandboxSecurityPosture:
    """What one sandbox reports about its OWN containment, asked of the box.

    Not read from the control plane's inventory, which reports none of this: a
    ``SandboxInfo`` carries id, state, image, entrypoint, metadata and little
    else. Inferring the answer from what AstraBox configured would defeat the
    purpose — an operator asking "is this box actually filtered" is asking
    precisely whether the configuration took effect, and an answer derived from
    that configuration cannot tell them.

    So every field here is the BOX's answer or absent. ``available`` is False
    when the box has no egress sidecar to ask, which is itself the finding: no
    sidecar means no egress policy and no vault, whatever the deployment
    intended.

    ``credential_names`` are names only. Upstream's vault never returns a stored
    value — it is write-only by construction — and this face would not carry one
    if it did.
    """

    sandbox_id: str
    available: bool
    default_action: str | None = None
    egress_rules: tuple[tuple[str, str], ...] = ()
    credential_names: tuple[str, ...] = ()
    binding_names: tuple[str, ...] = ()
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ConversationIdentity:
    """Who a conversation is, and where it lives inside whatever runs it.

    Deliberately says NOTHING about whether the conversation gets a box of its
    own or a place inside one its agent already has. That is a packing decision
    and it belongs to the backend: a box that can host isolated execution
    sessions can carry several conversations, one that cannot carries one, and
    a conversation cannot tell the difference — each has its own workspace, its
    own POSIX owner, and no reach into a sibling's.

    Modelling the distinction here would have been modelling something the
    product cannot observe, and every name for it would have been wrong in one
    of the two directions.

    ``agent_id`` is whose box a conversation may share. ``workspace_dir`` is
    the stable path visible inside the conversation, while
    ``workspace_source_dir`` is its conversation-owned backing path in the
    shared box. A backend that packs conversations must preserve that mapping.
    """

    agent_id: str
    home_dir: str
    workspace_dir: str
    workspace_source_dir: str
    linux_user: str = ""
    config_dir: str = ""
    cache_dir: str = ""
    temp_dir: str = ""
    #: Populated only when restoring a shared placement after a host restart.
    #: On first placement, the sandbox backend allocates these values.
    uid: int | None = None
    gid: int | None = None
    #: A shared placement gives the user terminal its own isolated shell. It
    #: shares this conversation's uid and files, but not the Agent runner's PID
    #: namespace. Persisting the id lets a restarted host close or reuse the
    #: exact terminal shell instead of falling back to the box-level root PTY.
    terminal_isolated_session_id: str | None = None


#: How many conversations one box carries. An ENVIRONMENT property, decided by
#: whoever chose the substrate — the same kind of fact as ``idle_action`` (how a
#: box dies).
#:
#: It lives on this seam and not in a runtime profile because it is not the
#: engine's to answer. Isolated sessions are the SANDBOX's capability; whether an
#: operator wants them used is a statement about boxes, not about the agent CLI
#: running inside one. Encoded as a profile id it became engine × tenancy — two
#: engine documents already declared the identical tenancy fact, and every new
#: engine would have doubled the set. What the engine DOES answer is what one
#: conversation looks like at a given tenancy: its home, its config dir, who
#: allocates its uid. That stays in the profile, where the vendor's vocabulary
#: belongs.
SANDBOX_TENANCY_CONVERSATION = "conversation"
SANDBOX_TENANCY_AGENT = "agent"
SANDBOX_TENANCIES = (SANDBOX_TENANCY_CONVERSATION, SANDBOX_TENANCY_AGENT)


#: What the substrate is asked to GRANT one of this Environment's boxes. An
#: Environment property, like ``sandbox_tenancy`` and for the same reason: it is
#: a statement about boxes, not about the agent CLI running inside one.
#:
#: The counterpart of :class:`SandboxIsolationCapability`, which is what a box
#: REPORTS. Both exist because neither answers the other's question — an
#: operator asking "did this take effect" cannot be answered from what was
#: configured, and a deployment choosing a posture cannot read it off a box that
#: does not exist yet.
#:
#: ``default``
#:     The substrate's own minimum. Enough to run an agent and its tools, and
#:     the level a deployment serving mutually untrusted users stays on: the BOX
#:     is the isolation boundary, and one conversation per box needs nothing
#:     inside it.
#: ``advanced``
#:     Adds what creating namespaces inside the box requires — on Kubernetes,
#:     ``CAP_SYS_ADMIN`` together with an unconfined AppArmor profile; measured,
#:     either alone still fails. Two things need it. Agent tenancy, whose
#:     conversations are isolated sessions carved inside one box. And an engine
#:     that runs its OWN sandbox in-box: Codex bundles bubblewrap and enforces
#:     its ``workspace-write`` mode with it, and without this level that
#:     enforcement silently degrades into asking a person instead.
#: ``privileged``
#:     The container-as-VM case — docker-in-docker and the like. Named here
#:     because the tier exists and refusing an unknown value is better than
#:     accepting one; a deployment that wants it says so deliberately.
#:
#: How a provider delivers and proves a level is its own contract. A provider
#: must refuse a level it cannot attest rather than creating a box with less
#: than was asked for. OpenSandbox can both request and probe ``advanced``; its
#: lifecycle API currently exposes no proof for ``privileged``.
SANDBOX_PERMISSION_LEVEL_DEFAULT = "default"
SANDBOX_PERMISSION_LEVEL_ADVANCED = "advanced"
SANDBOX_PERMISSION_LEVEL_PRIVILEGED = "privileged"
SANDBOX_PERMISSION_LEVELS = (
    SANDBOX_PERMISSION_LEVEL_DEFAULT,
    SANDBOX_PERMISSION_LEVEL_ADVANCED,
    SANDBOX_PERMISSION_LEVEL_PRIVILEGED,
)


@dataclass(frozen=True, slots=True)
class SandboxIsolationCapability:
    """Whether ONE box can host isolated execution sessions inside itself.

    Asked of the box, like :class:`SandboxSecurityPosture` and for the same
    reason: whether isolation works depends on what the container was GRANTED
    (namespace creation needs privileges a hardened default withholds), so a
    report derived from what AstraBox configured cannot answer it. The same
    image reports ``available=False`` under a default pod template and
    ``True`` once the template grants both the capability and an unconfined
    profile — one of the two alone is not enough.

    ``available=False`` is an answer, not an error: it is what a box in the
    per-conversation privilege level correctly reports, and ``detail`` carries
    the reason the box gave.

    Every field is the box's own vocabulary, carried across unchanged, because
    a backend that renamed them would be inventing a second vocabulary for
    something the box already names.
    """

    sandbox_id: str
    available: bool
    isolator: str | None = None
    version: str | None = None
    setpriv_available: bool = False
    userns_available: bool = False
    commit_supported: bool = False
    diff_supported: bool = False
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class SandboxIsolatedSession:
    """One isolated execution session living inside a sandbox.

    ``session_id`` is the box's, and it is the WHOLE handle: a host that
    restarts rebuilds its grip on a live session from this string alone. So it
    belongs on durable state, next to ``sandbox_id``, and never only in memory.

    ``uid``/``gid`` are the POSIX owner the session's processes actually run
    as — enforced by the box, allocated by the platform. Two sessions in one
    box under different owners cannot read each other's files.
    """

    sandbox_id: str
    session_id: str
    uid: int | None = None
    gid: int | None = None
    workspace_dir: str | None = None
    workspace_source_dir: str | None = None


#: What one data-plane assessment found (:meth:`SandboxProvider.assess_data_plane`).
SANDBOX_DATA_PLANE_ALIVE = "ALIVE"
SANDBOX_DATA_PLANE_DEAD = "DEAD"
SANDBOX_DATA_PLANE_UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class SandboxDataPlaneAssessment:
    """Whether a box's data plane is there, as its provider can tell.

    ``DEAD`` is evidence the box is gone even though its control-plane record
    may still answer; only it licenses replacing the box. ``UNKNOWN`` is no
    evidence either way, and ``detail`` says why.
    """

    #: One of :data:`SANDBOX_DATA_PLANE_ALIVE`, :data:`SANDBOX_DATA_PLANE_DEAD`
    #: and :data:`SANDBOX_DATA_PLANE_UNKNOWN`.
    state: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class SandboxMemoryHeadroom:
    """A box's own memory limit and current use, in bytes, as the box reports them.

    Asked of the box rather than kept by the platform: the limit is whatever the
    box's template set, and the kernel already holds it.
    """

    limit_bytes: int
    current_bytes: int


class SandboxUnreachable(APIError, ConnectionError):
    """The box could not be reached; nothing is proved about whether it exists.

    A failed connection is not evidence that a box is gone. Only its control
    plane, or the provider's data-plane assessment, may say that. A caller
    treats the box as unusable for this operation and leaves its records alone.
    It is a ``ConnectionError`` too, so a data-plane caller can treat it as the
    transport failure it is without knowing the platform's error type.
    """

    def __init__(self, sandbox_id: str, detail: str) -> None:
        super().__init__(
            code="SANDBOX_UNREACHABLE",
            message=f"sandbox {sandbox_id!r} could not be reached: {detail}",
            status_code=503,
        )
        self.sandbox_id = sandbox_id


class SandboxProbeFailed(APIError):
    """The box answered a probe with something its provider cannot read.

    Raised rather than read as "no answer": a probe that quietly degraded would
    turn a reading defect into a capacity decision nobody sees.
    """

    def __init__(self, sandbox_id: str, *, probe: str, detail: str) -> None:
        super().__init__(
            code="SANDBOX_PROBE_FAILED",
            message=(
                f"sandbox {sandbox_id!r} answered the {probe} probe with "
                f"something that cannot be read: {detail}"
            ),
            status_code=502,
        )
        self.sandbox_id = sandbox_id
        self.probe = probe


class UnsupportedSandboxProcesses:
    """The ``processes`` of a handle whose backend has no process sessions.

    Every operation raises the 501 ``SANDBOX_PROCESS_SESSIONS_UNSUPPORTED``
    naming the backend, so a feature that reaches it fails at once rather than
    with an error about a missing attribute. A provider that does not declare
    :attr:`SandboxProvider.supports_process_sessions` returns one from its
    handle's ``processes``.
    """

    def __init__(self, backend: str) -> None:
        self._backend = str(backend)

    def _refusal(self) -> APIError:
        return APIError(
            code="SANDBOX_PROCESS_SESSIONS_UNSUPPORTED",
            message=f"sandbox backend {self._backend!r} has no process sessions",
            status_code=501,
        )

    async def start(self, command: str, *, cwd: str | None) -> str:
        raise self._refusal()

    async def exists(self, process_id: str) -> bool:
        raise self._refusal()

    async def output_offset(self, process_id: str) -> int:
        raise self._refusal()

    async def attach(
        self, process_id: str, *, since: int, takeover: bool
    ) -> SandboxProcessAttachment:
        raise self._refusal()

    async def signal(self, process_id: str, name: str) -> None:
        raise self._refusal()

    async def close(self, process_id: str) -> None:
        raise self._refusal()


class SandboxResourcesUnsupported(APIError):
    """The backend cannot create a box with the deployment's resource envelope.

    Raised by a provider's ``fit_resources`` before anything is allocated, in
    place of creating a box of another shape: a backend that offers fixed
    shapes refuses an envelope it has none for, and the deployment chooses one
    of its shapes with ``ASTRABOX_SANDBOX_*_LIMIT`` / ``*_REQUEST``.
    """

    def __init__(
        self,
        backend: str,
        limits: Mapping[str, str],
        requests: Mapping[str, str],
        *,
        detail: str,
    ) -> None:
        super().__init__(
            code="SANDBOX_RESOURCES_UNSUPPORTED",
            message=(
                f"sandbox backend {backend!r} cannot create a box with limits "
                f"{dict(limits)} and requests {dict(requests)}: {detail}"
            ),
            status_code=409,
        )
        self.backend = backend
        self.limits = dict(limits)
        self.requests = dict(requests)


@dataclass(frozen=True, slots=True)
class SandboxDiagnostics:
    """One PLAIN-TEXT diagnostic report about one sandbox.

    ``text`` is opaque human-readable output — a report a backend renders for an
    operator to read. It carries NO promised fields, NO schema and no stability
    guarantee across backends or backend versions, which is why this type has a
    ``text`` and not a parsed body: anything that parsed it would be building on
    a format nobody agreed to. Render it in a ``<pre>``; do not scrape it.

    ``truncated`` is True when the transport capped the report — the text is then
    a fragment, and the provider documents which end it kept.
    """

    sandbox_id: str
    scope: str
    content_type: str
    text: str
    truncated: bool = False


class SandboxProvider(ABC):
    """One sandbox platform's atomic resource and transport capabilities.

    Capability flags carry the defaults a backend keeps unless its behaviour
    differs. Four methods are abstract: the create/connect pair
    :meth:`connection_config` and :meth:`secret_material`, and the lifecycle pair
    :meth:`connect` and :meth:`kill`. Backends that create runtime resources
    also implement :meth:`create_sandbox`; placement selection, pooling,
    workspace preparation, credentials, and engine startup remain platform
    responsibilities.
    """

    #: Registry key. Required, non-empty; lowercased when registered.
    name: str

    requires_sandbox_object_for_ws: bool = False
    uses_create_oss_mounts: bool = False
    connection_secret_uses_legacy_sandbox_api_key: bool = False
    conversation_bootstrap_transport: str = "sidecar_http"
    requires_https_git: bool = False
    endpoint_is_authoritative: bool = False
    #: A create carries one caller-supplied immutable assignment id, and a later
    #: replica can find the resulting resource by that id without scanning the
    #: provider's full inventory. A caller must refuse multi-replica create on a
    #: backend that leaves this false.
    supports_correlated_create: bool = False
    #: The backend substitutes vault ``environment_variable`` credentials into
    #: outbound requests at its egress boundary (the sandbox holds only an
    #: opaque placeholder). Default False — a backend without an egress
    #: credential proxy cannot honor env-var or MCP Vault credentials, and a
    #: Session that needs one fails before sandbox allocation.
    #: A True backend MUST implement the substitution semantics of
    #: :mod:`astrabox.seams.egress_credentials` (placeholder minting, host
    #: gating, fail-closed refusal) — see docs/egress-credential-injection.md.
    supports_egress_credential_injection: bool = False
    #: Each live sandbox binding is exclusively owned by one session/profile and
    #: one canonical principal (and has its own network namespace), so fixed
    #: in-box ports never collide and principal-scoped mutable state cannot cross
    #: sessions. A provider must not reassign the same live sandbox to another
    #: principal while an abandoned operation may still mutate it. Default False
    #: (a shared-host/backend must disambiguate and fence per profile).
    sandbox_is_profile_exclusive: bool = False
    #: This backend can run many isolated execution sessions inside ONE box,
    #: each with its own PID/mount/tmpfs namespace and its own POSIX owner.
    #:
    #: A capability of the BACKEND, not a promise about any particular box:
    #: whether a given sandbox can actually host one depends on the privilege
    #: level of the pool it came from, which is why the per-box answer is a
    #: separate READ (:meth:`read_isolation_capability`) and every caller must
    #: gate on it. Declaring this obliges a provider to implement all three
    #: operations; registration checks that, because a half-implemented
    #: capability fails at the moment a conversation needs it.
    supports_isolated_sessions: bool = False
    #: Whether the provider can recover active isolated sessions by the
    #: durable workspace source the platform assigned before creating them.
    #: This is what closes the crash window between the provider returning a
    #: session id and the platform publishing it.
    supports_isolated_session_recovery: bool = False
    #: Permission levels this backend can both deliver and prove. The write path
    #: reads this declaration before persisting an Environment, so a level that
    #: can only fail at sandbox start is not offered as working configuration.
    #: Registration validates the values against ``SANDBOX_PERMISSION_LEVELS``.
    supported_permission_levels: tuple[str, ...] = (SANDBOX_PERMISSION_LEVEL_DEFAULT,)
    #: Whether creates honour an Environment's provider-neutral
    #: ``network_policy`` by creating the box under it.
    #:
    #: A network policy is a SECURITY CONTROL, so the only two acceptable
    #: outcomes are "enforced" and "refused"; a backend that quietly ignored one
    #: would leave a deployment believing egress was filtered when it was not.
    #: Declaring it here is what lets the caller refuse on the backend's behalf,
    #: with the backend named, instead of every engine hard-coding which ones can.
    #: If the provider also advertises egress credential injection, it must
    #: preserve both contracts or refuse an unsupported combination before
    #: allocation. A provider-specific limitation is not another platform
    #: mode and must never silently widen either input.
    supports_create_network_policy: bool = False
    #: Whether this backend can freeze a sandbox's filesystem and bring it back
    #: under the same id (:meth:`pause` / :meth:`resume`).
    #:
    #: Declared for the same reason as the flag above: pausing is what makes an
    #: idle box's WORKSPACE survive, so a backend that silently ignored a pause
    #: would hand a deployment the one outcome it was configuring against — a
    #: conversation that comes back to an empty box. The caller refuses on the
    #: backend's behalf, naming it, rather than discovering this per session.
    supports_pause: bool = False
    #: Whether this backend exposes a maintained client-side pool that can
    #: coordinate ready sandboxes across platform replicas.  The platform
    #: supplies complete create and preparation callbacks; the provider owns
    #: scheduling, leases, replenishment and atomic acquisition.
    supports_client_pool: bool = False
    #: Whether the control plane can enumerate and describe this backend's
    #: sandboxes (:meth:`list_sandboxes` / :meth:`describe_sandbox`). The
    #: background sweeps that reclaim ownerless boxes and retire boxes with
    #: stale network wiring read only backends that declare it; a backend that
    #: cannot list leaves its abandoned boxes to its own expiry, and startup
    #: says so. Registration checks that both reads are implemented.
    supports_inventory_listing: bool = False
    #: Whether this backend's handles run long-lived processes that any
    #: replica can attach to by id, replay output from a byte offset, and take
    #: over (:class:`SandboxProcesses`). The standard implementation over the
    #: image's execd provides all of it. The terminal of a conversation with
    #: its own box, and an engine that speaks over a process's stdin and
    #: stdout in such a box, need it, so an Agent or Environment that would
    #: use either on a backend without it is refused when it is saved.
    supports_process_sessions: bool = False
    #: Environment variables only this provider reads. When the provider is
    #: installed but not enabled, setting one does nothing, so startup warns
    #: about each one that is set.
    owned_settings: tuple[str, ...] = ()

    def workspace_route(self) -> "WorkspaceRoute | None":
        """How this backend brings persistent workspace storage into its boxes.

        The storage provider says where workspace files live; the route says
        how this runtime mounts them. ``None`` (the default) means it cannot:
        a deployment that configures a workspace volume with this backend is
        refused at startup.
        """
        return None

    async def create_sandbox(self, spec: SandboxCreateSpec) -> SandboxHandle:
        """Provision one sandbox from a backend-neutral spec; return a durable
        by-id :class:`SandboxHandle` (no agent started).

        The handle must survive the provider objects that created it (the caller
        may re-:meth:`connect` by id later). Soft-abstract: a backend that cannot
        provision (a lifecycle-only integration) inherits this loud failure.
        """
        raise NotImplementedError(
            f"sandbox provider {self.name!r} does not implement create_sandbox"
        )

    def validate_environment_configuration(self, payload: Mapping[str, Any]) -> None:
        """Refuse provider-specific Environment combinations that cannot work.

        Core validates the AstraBox Environment shape first. A provider may
        then check only its own deployment topology and capabilities. The
        default accepts the configuration; provider vocabulary must never be
        promoted into the public Environment schema through this hook.
        """

        _ = payload

    async def apply_credential_vault(
        self,
        sandbox: SandboxHandle,
        *,
        vault_write: SandboxEgressCredentialPlan,
        managed_credential_names: tuple[str, ...] = (),
        managed_binding_names: tuple[str, ...] = (),
        create_if_missing: bool = True,
    ) -> None:
        """Create or refresh protected credentials on an existing sandbox.

        A cold create can carry the vault in :class:`SandboxCreateSpec`. An
        adopted or resumed sandbox already exists, so a backend that advertises
        :attr:`supports_egress_credential_injection` must also provide this
        explicit refresh path. The default is a loud refusal; treating the
        placeholder as usable without its sidecar value would defer the failure
        to an unrelated model request.
        """
        _ = (
            sandbox,
            vault_write,
            managed_credential_names,
            managed_binding_names,
            create_if_missing,
        )
        raise NotImplementedError(
            f"sandbox provider {self.name!r} cannot refresh an existing credential vault"
        )

    async def admit_runtime_egress(
        self,
        sandbox: SandboxHandle,
        *,
        network_policy: SandboxNetworkPolicy,
        vault_write: SandboxEgressCredentialPlan | None,
    ) -> None:
        """Admit what an existing sandbox's runtime now needs to reach.

        A box's outbound policy is set from what its runtime needed when the
        box was created, and a long-lived box outlives changes to that: an
        Assistant gains an MCP server, a Vault binding is assigned. Before an
        attach refreshes such a box, the destinations ``network_policy`` and
        the bindings of ``vault_write`` name must be reachable from it. Only
        reachability is added; nothing is removed, because other runtimes on
        the same box may still be using a destination this one dropped. A
        backend that advertises :attr:`supports_create_network_policy` must
        provide this; the default is a loud refusal.
        """
        _ = (sandbox, network_policy, vault_write)
        raise NotImplementedError(
            f"sandbox provider {self.name!r} cannot widen an existing sandbox's egress"
        )

    async def adopt_sandbox_identity(
        self,
        sandbox: SandboxHandle,
        *,
        session_id: str,
        assignment_id: str,
    ) -> None:
        """Rebind a prepared sandbox to its runtime owner and assignment.

        Providers own the metadata transport and any projection its wire
        requires. Core supplies only AstraBox's runtime-owner and assignment
        identities; it must not write one provider's metadata model directly.
        """

        _ = (sandbox, session_id, assignment_id)
        raise NotImplementedError(
            f"sandbox provider {self.name!r} cannot adopt a prepared sandbox"
        )

    async def ensure_client_pool(
        self,
        spec: SandboxClientPoolSpec,
        *,
        creator: SandboxClientPoolCreator,
        preparer: SandboxClientPoolPreparer,
    ) -> None:
        """Start or join one distributed pool of platform-prepared boxes.

        ``creator`` declares and creates the complete sandbox through this
        seam; ``preparer`` turns it into the business-ready unit the platform
        asked for.  A provider calls both while replenishing and publishes the
        member as idle only after the preparer returns successfully.
        """

        _ = (spec, creator, preparer)
        raise APIError(
            code="SANDBOX_CLIENT_POOL_UNSUPPORTED",
            message=f"sandbox backend {self.name!r} has no client-side pool",
            status_code=501,
        )

    async def acquire_client_pool(
        self,
        spec: SandboxClientPoolSpec,
    ) -> SandboxHandle | None:
        """Atomically remove and return one prepared member, or ``None``.

        ``None`` means the supplier's ready inventory is empty.  Control-plane
        or coordination failures raise; callers may choose a cold create for
        an empty pool, but must not hide a broken pool as an ordinary miss.
        """

        _ = spec
        raise APIError(
            code="SANDBOX_CLIENT_POOL_UNSUPPORTED",
            message=f"sandbox backend {self.name!r} has no client-side pool",
            status_code=501,
        )

    async def describe_client_pool(
        self,
        pool_name: str,
    ) -> SandboxClientPoolStatus:
        """Read one pool's shared inventory without starting or changing it."""

        _ = pool_name
        raise APIError(
            code="SANDBOX_CLIENT_POOL_UNSUPPORTED",
            message=f"sandbox backend {self.name!r} has no client-side pool",
            status_code=501,
        )

    async def retire_client_pool(self, pool_name: str) -> None:
        """Fence one pool namespace and destroy its unclaimed members."""

        _ = pool_name
        raise APIError(
            code="SANDBOX_CLIENT_POOL_UNSUPPORTED",
            message=f"sandbox backend {self.name!r} has no client-side pool",
            status_code=501,
        )

    def client_pool_readiness(self) -> SandboxClientPoolReadiness:
        """Whether this deployment configures what the client pool needs.

        Asked before an Agent is given prepared capacity, so a pool that cannot
        start is refused when it is configured instead of recorded as a failure
        after every attempt.
        """

        raise APIError(
            code="SANDBOX_CLIENT_POOL_UNSUPPORTED",
            message=f"sandbox backend {self.name!r} has no client-side pool",
            status_code=501,
        )

    def created_with_current_network_wiring(self, descriptor: SandboxDescriptor) -> bool:
        """Whether this resource still carries the deployment's sandbox-facing addresses.

        A provider that fixes deployment addresses into a resource when it is
        created (a DNS upstream, egress rules, a callback base) answers False
        when the running deployment's addresses differ: that resource cannot
        reach the model or the platform, and the platform retires it.
        Providers that fix no such address into a resource answer True.
        """

        _ = descriptor
        return True

    def owns_unclaimed_sandbox(self, descriptor: SandboxDescriptor) -> bool:
        """Whether provider-managed preparation still owns this resource.

        Platform orphan reclamation must leave these resources to the provider,
        including creation and acquisition handoffs outside published idle
        inventory. Providers without preparation own no such resources.
        """

        if not self.supports_client_pool:
            return False
        raise NotImplementedError(
            f"sandbox provider {self.name!r} must identify its unclaimed inventory"
        )

    def runtime_defaults(self) -> SandboxRuntimeDefaults:
        """Deploy-time defaults (seed image / agent command). Default: no opinion."""
        return SandboxRuntimeDefaults()

    def fit_resources(
        self, limits: Mapping[str, str], requests: Mapping[str, str]
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Return the resources this runtime creates the box with.

        The input is the deployment's envelope, and the result is what a
        :class:`SandboxCreateSpec` carries, so the spec states the box that is
        actually created. The default honours the envelope as given. A runtime
        that cannot honour it raises :class:`SandboxResourcesUnsupported`
        before anything is allocated rather than creating a box of another
        shape; the deployment then selects one it offers. The one narrowing a
        runtime may do is a hard host ceiling that would fail every create:
        Docker refuses a CPU limit above its host's CPU count, so the bundled
        Docker server lowers it to that count.
        """
        return dict(limits), dict(requests)

    def per_box_overhead(self) -> tuple[float, int]:
        """CPU cores and memory bytes each box costs beyond its own limits.

        A runtime that starts a companion beside every box under a ceiling of
        its own (the bundled Docker server's egress sidecar) reports that
        ceiling, and the sandbox quota charges it with the box. The default is
        nothing.
        """
        return (0.0, 0)

    def paused_box_releases_resources(self) -> bool:
        """Whether a paused box gives its CPU and memory back to the host.

        A runtime that pauses by saving the box and removing its process (a
        Kubernetes pause commits the box and releases its Pod) answers True,
        and the sandbox quota stops charging a paused box. The default, False,
        is a pause that only freezes the box in place, still holding its memory.
        """
        return False

    async def host_memory_bytes(self) -> int | None:
        """Memory of the single host this backend runs every box on.

        The sandbox quota derives its memory default from it when the operator
        sets none. ``None`` — the default — means the backend's boxes may run on
        hosts this deployment cannot see, so nothing is derived.
        """
        return None

    async def shutdown_current_loop_resources(self) -> None:
        """Release provider resources owned by the current event loop.

        Most providers are stateless and inherit this no-op. Providers that run
        loop-bound clients or background reconcilers override it. The host calls
        this while the owning loop is still alive during graceful shutdown;
        closing such resources after the loop stops is too late to release
        distributed leases or flush network clients.
        """

    # --- backend-wide create/connect decisions ---------------------------------
    def sandbox_class(self, default_sandbox_cls: Any) -> Any:
        """Select the SDK sandbox class. Default: return the argument unchanged."""
        return default_sandbox_cls

    def owns_sandbox(self, sandbox: Any) -> bool:
        """True if ``sandbox`` belongs to this backend. Default: False.

        Used by :func:`sandbox_for_sandbox` to map a live sandbox object back to
        its owning provider.
        """
        return False

    @abstractmethod
    def connection_config(
        self,
        *,
        connection_config_cls: Any,
        settings: Any,
        request_timeout_seconds: int | None = None,
        secret_material: str | None = None,
    ) -> Any | None:
        """Return the SDK connection config for create/connect, or None if unused."""

    @abstractmethod
    def secret_material(self, *, settings: Any) -> str:
        """Return backend secret material for platform-owned per-sandbox services."""

    async def webshell_url(self, *, sandbox_id: str, user_id: str) -> str:
        """Return a web-shell URL. Default: raise; a backend without a web shell
        signals "unsupported" rather than returning a value."""
        raise APIError(
            code="WEB_SHELL_UNSUPPORTED",
            message=f"webshell is not supported for sandbox={self.name!r}",
            status_code=501,
        )

    # --- in-box transport (data plane) -----------------------------------------
    @abstractmethod
    def build_dataplane(
        self,
        *,
        sandbox: Any = None,
        endpoint: str | None = None,
        port: int = 8000,
    ) -> "SandboxDataPlane":
        """Build this backend's in-box transport, or fail loud.

        Two reach modes, both supported:

        * a LIVE sandbox object (``sandbox=``) — in-process, just created/connected
          (carries whatever the ws transport needs);
        * a bare persisted ENDPOINT string (``endpoint=``) — a different
          machine/request that only has the session row's endpoint (the
          multi-replica reach).

        The returned plane addresses the in-box ``port``. There is no fallback: a
        backend that cannot build a plane for the given inputs raises.
        """

    async def dataplane_for_sandbox(self, sandbox: Any, port: int = 8000) -> "SandboxDataPlane":
        """Build a plane for a live sandbox object, resolving its endpoint when needed.

        Default: try :meth:`build_dataplane` from the object directly; if that backend
        needs the resolved endpoint, resolve ``await sandbox.get_endpoint(port)`` and
        rebuild from the endpoint string. The resolved endpoint is memoized on the
        sandbox object (keyed by port) so later builds for the same sandbox avoid
        re-paying the endpoint round-trip. Backends rarely override this.
        """
        try:
            return self.build_dataplane(sandbox=sandbox, port=int(port))
        except Exception:
            cache = getattr(sandbox, "_astrabox_endpoint_cache", None)
            endpoint_str = cache.get(int(port)) if isinstance(cache, dict) else None
            if not endpoint_str:
                endpoint = await sandbox.get_endpoint(int(port))
                endpoint_str = endpoint.url
                if endpoint_str:
                    if not isinstance(cache, dict):
                        cache = {}
                        with contextlib.suppress(Exception):
                            sandbox._astrabox_endpoint_cache = cache
                    cache[int(port)] = endpoint_str
            return self.build_dataplane(sandbox=sandbox, endpoint=endpoint_str, port=int(port))

    # --- by-id lifecycle -------------------------------------------------------
    @abstractmethod
    async def connect(self, sandbox_id: str) -> SandboxHandle:
        """Connect to an existing sandbox by id (no agent start); return the
        :class:`SandboxHandle`."""

    @abstractmethod
    async def kill(self, sandbox_id: str) -> bool:
        """Destroy the sandbox; True ONLY when this box is proven gone.

        The return value is EVIDENCE, and the bar is deliberately high: True
        means the backend established that this exact sandbox is gone.
        A delete the backend accepted but whose effect it did not confirm, a
        response that cannot distinguish "this box is gone" from "this request
        never reached it", a timeout — all of those are False or a raise, never
        True. An over-confident True here becomes a released pointer upstream,
        which is how a live box loses its last name.

        Callers in the platform do NOT call this directly: they call
        :meth:`confirm_destroyed`, which turns this answer into a verdict that
        cannot be collapsed back into a bare boolean.
        """

    async def pause(self, sandbox_id: str) -> bool:
        """Freeze the sandbox, keeping its filesystem; True when it is paused.

        The counterpart to :meth:`kill` for a box nobody is using: the cluster
        gets its CPU and memory back, and the workspace survives because the
        backend committed it. Processes and memory do NOT survive — this is a
        filesystem snapshot, not a suspend-to-disk, so anything the box was
        running is gone and the next turn starts fresh against the same files.

        As with ``kill``, the boolean is evidence rather than acknowledgement: a
        pause the backend accepted but did not confirm is False, because a caller
        that believes a box is paused will not renew its lease.
        """
        _ = sandbox_id
        raise APIError(
            code="SANDBOX_PAUSE_UNSUPPORTED",
            message=(
                f"sandbox backend {self.name!r} cannot pause a sandbox: "
                "it has no snapshot operation"
            ),
            status_code=501,
        )

    async def resume(self, sandbox_id: str) -> bool:
        """Bring a paused sandbox back under the SAME id; True when running.

        The id is the whole point: a resumed box keeps every pointer the platform
        already holds for it, so a session reattaches instead of being rebuilt.
        """
        _ = sandbox_id
        raise APIError(
            code="SANDBOX_PAUSE_UNSUPPORTED",
            message=(
                f"sandbox backend {self.name!r} cannot resume a sandbox: "
                "it has no snapshot operation"
            ),
            status_code=501,
        )

    async def claim_of(
        self, sandbox_id: str, *, expected_session_id: str | None = None
    ) -> SandboxClaim:
        """Whose sandbox is this? — the ownership half of the disposal judgement.

        A backend answers from the ownership FACT the box carries (the create
        metadata under :data:`SANDBOX_SESSION_ID_METADATA_KEY` /
        :data:`SANDBOX_MANAGED_BY_METADATA_KEY`), which
        :func:`~astrabox.seams.sandbox_disposal.claim_from_metadata` turns into
        a verdict.

        The default is ``UNKNOWN`` naming the backend, which is the honest
        answer for a backend whose control plane cannot describe a box: a
        provider that cannot say whose a sandbox is must SAY that, exactly as
        the inventory face refuses rather than returning an empty page. Since
        ``UNKNOWN`` licenses nothing, a backend that does not implement this
        simply never gets a claim-gated disposal — it does not get an
        optimistic one.
        """
        _ = expected_session_id
        return SandboxClaim.unknown(
            sandbox_id,
            detail=(
                f"sandbox backend {self.name!r} cannot say whose a sandbox is: "
                "it does not implement claim_of"
            ),
        )

    async def confirm_destroyed(self, sandbox_id: str) -> SandboxDestruction:
        """Destroy one sandbox and report a verdict that names its evidence.

        The single minting point for
        :class:`~astrabox.seams.sandbox_disposal.SandboxDestruction`: the whole
        platform reaches destruction through here, so the question "on what
        evidence was this box declared gone" has exactly one answer for
        every backend.

        TWO OBSERVATIONS, not one. :meth:`kill` returning True is the delete
        call's own account of itself; the confirmation comes from asking a
        SECOND time, through :meth:`probe`, whether the control plane still
        knows the box. Only ``NOT_FOUND`` confirms. ``OK`` is a box still there
        after a delete that claimed success — reported UNCONFIRMED, which keeps
        the name and retries, instead of releasing a pointer to a live box.
        ``PROBE_FAILED`` is a control plane that could not be asked, which is
        also UNCONFIRMED: an unreachable server is not an absent sandbox.

        A backend with no :meth:`probe` falls back to the single observation
        and says so in ``detail`` — the honest degraded form, not a hidden one.
        """
        target = str(sandbox_id or "").strip()
        if not target:
            return SandboxDestruction.nothing_named(
                detail=f"sandbox backend {self.name!r} was given no sandbox id to destroy"
            )
        accepted = False
        kill_detail = ""
        try:
            accepted = bool(await self.kill(target))
        except Exception as exc:  # noqa: BLE001 - every failure is one verdict
            kill_detail = f"the delete failed: {type(exc).__name__}: {exc}"
        else:
            kill_detail = (
                "the delete reported success" if accepted else "the delete did not report success"
            )
        # An accepted delete is not yet an absent sandbox: on Kubernetes the
        # workload delete is asynchronous — the control plane answers 204 and
        # the record stays visible (still `running`, then terminating) until
        # pod teardown completes — so the confirming observation must spend
        # that window rather than ask once and report the box undead. The
        # re-asks stop at the first NOT_FOUND; a backend whose delete is
        # synchronous confirms on the first probe with zero added latency.
        # Bounded here so a caller without its own timeout cannot poll
        # forever; callers with a shorter deadline still cut this off.
        started = time.monotonic()
        while True:
            try:
                probe = await self.probe(target)
            except NotImplementedError:
                if accepted:
                    return SandboxDestruction.confirmed_gone(
                        target,
                        detail=(
                            f"{kill_detail}; sandbox backend {self.name!r} has no "
                            "liveness probe, so this rests on the delete alone"
                        ),
                    )
                return SandboxDestruction.unconfirmed(
                    target,
                    detail=(
                        f"{kill_detail}, and sandbox backend {self.name!r} has no "
                        "liveness probe to confirm it with"
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - a probe that raised proves nothing
                return SandboxDestruction.unconfirmed(
                    target,
                    detail=(
                        f"{kill_detail}, and the confirming probe failed: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                )
            if probe.probe_status == SANDBOX_LIFECYCLE_PROBE_NOT_FOUND:
                return SandboxDestruction.confirmed_gone(
                    target,
                    detail=(
                        f"{kill_detail}, and the control plane no longer knows "
                        "this sandbox"
                    ),
                )
            waited = time.monotonic() - started
            if (
                accepted
                and probe.probe_status == SANDBOX_LIFECYCLE_PROBE_OK
                and waited < DESTRUCTION_SETTLE_BUDGET_S
            ):
                await asyncio.sleep(DESTRUCTION_SETTLE_INTERVAL_S)
                continue
            if probe.probe_status == SANDBOX_LIFECYCLE_PROBE_OK:
                return SandboxDestruction.unconfirmed(
                    target,
                    detail=(
                        f"{kill_detail}, but the control plane still reports this "
                        f"sandbox (state={probe.sandbox_state!r}) after "
                        f"{waited:.1f}s"
                    ),
                )
            return SandboxDestruction.unconfirmed(
                target,
                detail=(
                    f"{kill_detail}, and the confirming probe could not be answered: "
                    f"{probe.error_text or 'no detail'}"
                ),
            )

    async def renew(self, sandbox_id: str, ttl_seconds: int) -> datetime | None:
        """Extend the sandbox TTL; return the new expiry if the backend reports one.
        Default: no TTL concept — return None (call-sites tolerate None)."""
        return None

    async def expires_at(self, sandbox_id: str) -> datetime | None:
        """Supplier expiry; None means no scheduled termination, not a failed read."""
        raise NotImplementedError("sandbox expiry lookup is not implemented")

    async def probe(self, sandbox_id: str) -> SandboxLifecycleProbeResult:
        """Liveness/lifecycle probe (OK / NOT_FOUND / PROBE_FAILED).

        A backend that cannot probe must raise ``NotImplementedError`` and NOT
        the 501 ``APIError`` the optional operations above use. The distinction
        is load-bearing rather than stylistic: :meth:`confirm_destroyed` reads
        ``NotImplementedError`` as "this backend has no second observation" and
        degrades honestly, while any other exception is an attempted probe that
        FAILED and yields UNCONFIRMED. Raise the wrong one and every destruction
        this backend performs stays unconfirmed forever — sandbox pointers are
        never released and terminate never converges.
        """
        raise NotImplementedError

    async def resolve_endpoint(self, sandbox_id: str, port: int) -> str | None:
        """Resolve an in-box port endpoint by id (no execd attach)."""
        raise NotImplementedError

    async def resolve_browser_endpoint(
        self,
        sandbox_id: str,
        port: int,
        *,
        expires_at: datetime | None = None,
    ) -> SandboxBrowserEndpoint | None:
        """Resolve the provider-native URL a browser should open.

        Backends inherit unsigned endpoint support through ``resolve_endpoint``.
        A requested expiry is a security requirement, so a backend that cannot
        mint a signed URL refuses rather than returning an unsigned substitute.
        """
        if expires_at is not None:
            raise APIError(
                code="SANDBOX_SIGNED_ENDPOINT_UNSUPPORTED",
                message=(f"sandbox backend {self.name!r} cannot mint signed browser endpoints"),
                status_code=501,
            )
        resolved = await self.resolve_endpoint(sandbox_id, int(port))
        if not resolved:
            return None
        value = str(resolved).strip()
        if not value.startswith(("http://", "https://")):
            value = f"https://{value}"
        return SandboxBrowserEndpoint(endpoint=value)

    def endpoint_names_sandbox(self, endpoint: str) -> str | None:
        """The id of the sandbox that ``endpoint`` addresses, when the address says so.

        A persisted endpoint is checked against the sandbox it is meant to
        reach before it is used, because an address can outlive the box it
        named (see docs/maintainers/sandbox-address-reuse-risk.md). How an
        address encodes a box is this backend's knowledge. The default answers
        ``None``, meaning the address does not say, and the caller then
        resolves the endpoint again from the sandbox id instead of using it.
        """
        _ = endpoint
        return None

    # --- control-plane inventory (the read-only ops face) ----------------------
    # Three optional reads that let an operator see a backend's sandboxes as a
    # RESOURCE rather than only as the side effect of a session. Every default
    # below refuses loud with a 501 that NAMES the backend: a backend whose
    # control plane cannot enumerate or explain its sandboxes must say so, never
    # return an empty page that reads as "there are no sandboxes".
    async def list_sandboxes(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        metadata: Mapping[str, str] | None = None,
    ) -> SandboxPage:
        """One page of the sandboxes this backend's control plane knows about.

        Paged because a backend's inventory is unbounded; the caller advances
        while :attr:`SandboxPage.has_next_page` holds. Without ``metadata`` it
        includes sandboxes this deployment did not create — the question is
        "what is this backend running", not "what was asked for". ``metadata``
        narrows the page to sandboxes whose create metadata carries every given
        pair; paging then counts only those. The narrowing is the control
        plane's, so a caller that acts on the result checks the pairs again.
        """
        _ = (page, page_size, metadata)
        raise APIError(
            code="SANDBOX_LISTING_UNSUPPORTED",
            message=(
                f"sandbox backend {self.name!r} cannot enumerate its sandboxes: "
                "its control plane has no listing operation"
            ),
            status_code=501,
        )

    async def find_sandbox_by_assignment(
        self,
        assignment_id: str,
    ) -> SandboxDescriptor | None:
        """Find the one resource created for an immutable assignment.

        This is a correlated-create operation, not a generic metadata query:
        caller-selected ids, idempotency keys and searchable create metadata
        can all implement it. The default refuses because scanning an unbounded
        inventory would turn recovery into an operational hazard.
        """

        _ = assignment_id
        raise APIError(
            code="SANDBOX_CORRELATED_CREATE_UNSUPPORTED",
            message=(
                f"sandbox backend {self.name!r} cannot find a sandbox by its "
                "create assignment"
            ),
            status_code=501,
        )

    async def read_security_posture(self, sandbox_id: str) -> SandboxSecurityPosture:
        """What this box reports about its own containment. Optional.

        A backend that cannot ask a box this returns
        ``SandboxSecurityPosture(sandbox_id, available=False, detail=…)`` rather
        than raising: "this backend cannot tell you" is a legitimate answer to
        show an operator, and it is a different answer from "the box is not
        contained". Both beat an empty panel, which reads as the second.
        """
        return SandboxSecurityPosture(
            sandbox_id=sandbox_id,
            available=False,
            detail=f"{self.name!r} cannot report a sandbox's containment",
        )

    async def patch_egress_rules(
        self,
        sandbox_id: str,
        *,
        rules: tuple[tuple[str, str], ...],
    ) -> None:
        """Merge outbound-network rules into one live sandbox. Optional.

        The action/target pairs are the sandbox backend's own egress-policy
        vocabulary and are carried unchanged. This mutation exists for the
        gated E2E fault surface; ordinary runtime and admin paths never call it.
        A backend that cannot mutate a live policy refuses loudly.
        """
        _ = (sandbox_id, rules)
        raise APIError(
            code="SANDBOX_EGRESS_MUTATION_UNSUPPORTED",
            message=(f"sandbox backend {self.name!r} cannot patch live egress rules"),
            status_code=501,
        )

    async def delete_egress_rules(
        self,
        sandbox_id: str,
        *,
        targets: tuple[str, ...],
    ) -> None:
        """Remove exact outbound-network targets from one live sandbox.

        Delete is separate from patch because restoring a fault must reproduce
        an absent rule as absent. Replacing absence with an explicit ``allow``
        changes the policy and is not a reversible test fault.
        """
        _ = (sandbox_id, targets)
        raise APIError(
            code="SANDBOX_EGRESS_MUTATION_UNSUPPORTED",
            message=(f"sandbox backend {self.name!r} cannot delete live egress rules"),
            status_code=501,
        )

    async def read_isolation_capability(self, sandbox_id: str) -> SandboxIsolationCapability:
        """Whether this box can host isolated sessions. Optional, and a READ.

        Never raises for "no": a backend with no such concept, and a box whose
        privilege level withholds it, are both legitimately ``available=False``
        with the reason in ``detail``. Callers gate on this before opening a
        session, so an exception here would turn "not this box" into an outage.
        """
        return SandboxIsolationCapability(
            sandbox_id=sandbox_id,
            available=False,
            detail=f"{self.name!r} cannot host isolated sessions",
        )

    async def open_isolated_session(
        self,
        sandbox_id: str,
        *,
        workspace_dir: str,
        workspace_source_dir: str,
        uid: int | None = None,
        gid: int | None = None,
        share_net: bool = True,
        extra_writable: list[str] | None = None,
        extra_binds: list[tuple[str, str]] | None = None,
    ) -> SandboxIsolatedSession:
        """Open one isolated execution session inside a live box.

        Only for a provider declaring ``supports_isolated_sessions``, and only
        against a box whose :meth:`read_isolation_capability` said yes. This
        RAISES where the capability read returns a negative answer, because by
        the time a caller opens a session it has already decided this box can
        host one — failing quietly here would hand back a session that isolates
        nothing, which is worse than not having one.

        ``share_net`` shares the sandbox's network namespace, which is what
        lets the host reach a resident process inside the session on a port of
        the box. Turning it off isolates the network too and makes anything
        listening inside unreachable from outside the session.

        ``workspace_dir`` is the path processes see; ``workspace_source_dir``
        is the box-level directory mounted there. They may be equal for a box
        dedicated to one conversation.

        ``extra_writable`` is the provider-native list of additional paths the
        isolated session may write.  It is needed when an engine keeps mutable
        config and cache outside ``workspace_dir`` but still under the
        conversation's private home.

        ``extra_binds`` are ``(source, destination)`` pairs mounted read-write
        on top of what the session already has. The destination may be a path
        the session's own base image or backend already provides — a private
        ``/tmp`` over the backend's shared one, say — so the pair is how a
        caller narrows a shared path to this conversation.
        """
        _ = (
            sandbox_id,
            workspace_dir,
            workspace_source_dir,
            uid,
            gid,
            share_net,
            extra_writable,
            extra_binds,
        )
        raise APIError(
            code="SANDBOX_ISOLATION_UNSUPPORTED",
            message=(f"sandbox backend {self.name!r} cannot open an isolated session"),
            status_code=501,
        )

    async def find_isolated_sessions_by_workspace(
        self,
        sandbox_id: str,
        *,
        workspace_source_dir: str,
    ) -> tuple[SandboxIsolatedSession, ...]:
        """Find active sessions created over one durable workspace source.

        A platform create records the source path before asking the provider
        to open a session.  Providers implementing this read can therefore
        recover and close a session whose returned id was lost with a crashed
        worker, without exposing their inventory representation to core.
        """

        _ = (sandbox_id, workspace_source_dir)
        raise APIError(
            code="SANDBOX_ISOLATION_UNSUPPORTED",
            message=(
                f"sandbox backend {self.name!r} cannot recover isolated sessions "
                "by workspace"
            ),
            status_code=501,
        )

    async def prepare_isolated_workspace(
        self, sandbox_id: str, *, workspace_dir: str, uid: int, gid: int
    ) -> None:
        """Make ``workspace_dir`` a place ``uid`` can actually work in.

        Create it, give it to ``uid``, and close it to everyone else. Every part
        is load-bearing and none of it happens on its own: a box auto-creates a
        missing workspace as ROOT, so a session opened against it is refused on
        its first write; and files land ``0644``, so without a private mode a
        sibling conversation in the same box can read this one's work.

        Runs as the box's own root, which is who should own a directory nobody
        has been handed yet. That is reaching into a box that is already
        running and already shared — a different thing from assembling a
        per-session box at claim time, which is what the image rule forbids.

        Fails loud. A workspace that could not be prepared is not a degraded
        conversation, it is one that will refuse its first write with an error
        pointing at the wrong layer.
        """
        _ = (sandbox_id, workspace_dir, uid, gid)
        raise APIError(
            code="SANDBOX_ISOLATION_UNSUPPORTED",
            message=(f"sandbox backend {self.name!r} cannot prepare an isolated workspace"),
            status_code=501,
        )

    async def run_in_isolated_session(
        self,
        sandbox_id: str,
        session_id: str,
        *,
        code: str,
        envs: Mapping[str, str] | None = None,
        timeout_s: float | None = 60.0,
    ) -> tuple[int, str, str]:
        """Run ``code`` inside one isolated session; return exit/stdout/stderr.

        Text-shaped, like the box-level command face and for the same reason:
        the box streams output as decoded line events, so interior newlines
        survive and a trailing one does not.

        Runs are SERIALIZED per session by the box — a second call waits for the
        first. A caller that wants concurrency wants a second session.

        Backgrounding is the caller's business, and it works: a process
        detached here outlives the run that started it, keeps listening, and
        dies when the session is closed. That is what lets a resident server
        live in a session at all, and it is the property
        ``close_isolated_session`` relies on for teardown.
        """
        _ = (sandbox_id, session_id, code, envs, timeout_s)
        raise APIError(
            code="SANDBOX_ISOLATION_UNSUPPORTED",
            message=(f"sandbox backend {self.name!r} cannot run inside an isolated session"),
            status_code=501,
        )

    async def stream_in_isolated_session(
        self,
        sandbox_id: str,
        session_id: str,
        *,
        code: str,
        envs: Mapping[str, str] | None = None,
        timeout_s: float | None = 300.0,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream one stateful shell run inside an isolated session.

        Events use the terminal-neutral ``stdout`` / ``stderr`` vocabulary and
        finish with one ``__done__`` event carrying ``exit_code``. Cancelling
        the consumer must close the provider stream. Providers do not promise
        that a cancelled run leaves its stateful shell reusable: OpenSandbox
        v1.1.0 cannot do that reliably. A caller that needs a deterministic
        interrupt must put disposable work in a separate isolated session and
        delete that session after cancellation.

        This is deliberately separate from the box-level PTY face. A provider
        must never implement it by silently falling back to a shell in the
        sandbox's main namespace, because that would cross conversation
        identity and filesystem boundaries in a shared box.
        """

        _ = (sandbox_id, session_id, code, envs, timeout_s)
        raise APIError(
            code="SANDBOX_ISOLATION_UNSUPPORTED",
            message=(
                f"sandbox backend {self.name!r} cannot stream inside an isolated session"
            ),
            status_code=501,
        )
        if False:  # pragma: no cover - keeps this an async-generator contract
            yield {}

    async def close_isolated_session(self, sandbox_id: str, session_id: str) -> None:
        """Destroy one isolated session and everything running inside it.

        This IS the teardown: the box kills the session's process group, so a
        resident process started inside dies with it and nothing else has to be
        stopped first. Idempotent — closing a session that is already gone is a
        success, because the caller's goal is that it not be running.
        """
        _ = (sandbox_id, session_id)
        raise APIError(
            code="SANDBOX_ISOLATION_UNSUPPORTED",
            message=(f"sandbox backend {self.name!r} cannot close an isolated session"),
            status_code=501,
        )

    async def assess_data_plane(self, sandbox_id: str) -> SandboxDataPlaneAssessment:
        """Whether the box's data plane is there while its control plane may say so.

        The false-Ready shape: a box killed out of band can leave a control-plane
        record that still answers as running while nothing behind its endpoint
        exists. A provider that can touch the data plane answers ``DEAD`` or
        ``ALIVE``; the default has no way to look and answers ``UNKNOWN``,
        which is no evidence.
        """
        _ = sandbox_id
        return SandboxDataPlaneAssessment(
            state=SANDBOX_DATA_PLANE_UNKNOWN,
            detail=f"sandbox backend {self.name!r} cannot assess a box's data plane",
        )

    async def count_active_isolated_sessions(self, sandbox_id: str) -> int:
        """How many isolated sessions the box itself holds active.

        The last word before a shared box is destroyed. Every platform record of
        occupancy is written at some point during a placement, so each has a
        window in which a conversation is real but not yet recorded; the box's
        own session table has none. Raises when there is no answer
        (:class:`SandboxUnreachable` for a box that cannot be reached), and a
        caller never reads a failure as an empty box.
        """
        _ = sandbox_id
        raise APIError(
            code="SANDBOX_ISOLATION_UNSUPPORTED",
            message=(
                f"sandbox backend {self.name!r} cannot count a box's isolated sessions"
            ),
            status_code=501,
        )

    async def read_memory_headroom(self, sandbox_id: str) -> SandboxMemoryHeadroom | None:
        """The box's memory limit and current use; ``None`` when it has no limit.

        A box without a memory limit is never packed with another conversation,
        so ``None`` is an answer, not a failure. Raises
        :class:`SandboxUnreachable` when the box cannot be reached and
        :class:`SandboxProbeFailed` when its answer cannot be read.

        The default reads the box's cgroup v2 files through its ``commands``.
        ``memory.max`` is the literal ``max`` when nothing set a limit, which is
        ``None``; any other answer that is not two byte counts is a reading
        defect and fails the placement that relied on it, rather than quietly
        turning packing off.
        """
        result = await self._probe_box(
            sandbox_id,
            probe="memory",
            command="cat /sys/fs/cgroup/memory.max /sys/fs/cgroup/memory.current",
        )
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if result.exit_code == 0 and len(lines) == 2 and lines[1].isdigit():
            if lines[0] == "max":
                return None
            if lines[0].isdigit():
                return SandboxMemoryHeadroom(limit_bytes=int(lines[0]), current_bytes=int(lines[1]))
        raise SandboxProbeFailed(
            sandbox_id,
            probe="memory",
            detail=(
                f"exit {result.exit_code}, cgroup v2 memory.max and memory.current "
                f"read as {result.output[:200]!r}"
            ),
        )

    async def read_listening_ports(self, sandbox_id: str) -> frozenset[int]:
        """The TCP ports something in the box is listening on.

        Raises :class:`SandboxUnreachable` when the box cannot be reached, so a
        placement into a box that cannot answer is refused instead of being made
        without its port-collision check.

        The default reads ``/proc/net/tcp`` and ``/proc/net/tcp6`` through the
        box's ``commands`` rather than a tool (``ss`` and ``netstat`` are not
        in every image): state ``0A`` is LISTEN and the local address is
        ``HEX_IP:HEX_PORT``. A box without IPv6 has no ``/proc/net/tcp6``,
        which makes ``cat`` exit non-zero after printing the IPv4 table, so the
        table's own header, not the exit status, decides whether the box
        answered.
        """
        result = await self._probe_box(
            sandbox_id, probe="listening ports", command="cat /proc/net/tcp /proc/net/tcp6"
        )
        if "local_address" not in result.stdout:
            raise SandboxProbeFailed(
                sandbox_id,
                probe="listening ports",
                detail=f"/proc/net/tcp read as {result.output[:200]!r}",
            )
        ports: set[int] = set()
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) < 4 or fields[3] != "0A":
                continue
            local = fields[1].rsplit(":", 1)
            if len(local) != 2:
                continue
            try:
                ports.add(int(local[1], 16))
            except ValueError:
                continue
        return frozenset(ports)

    async def _probe_box(self, sandbox_id: str, *, probe: str, command: str) -> SandboxCommandResult:
        """Run one read-only command in the box for a probe and return how it ended.

        A box gone for good keeps the ``SANDBOX_GONE`` its ``connect`` raised.
        Any other failure to connect or to reach the box proves nothing about
        whether it exists and is :class:`SandboxUnreachable`; a run the box
        refused, or gave no status for, is :class:`SandboxProbeFailed`.
        """
        try:
            handle = await self.connect(sandbox_id)
        except SandboxUnreachable:
            raise
        except APIError as exc:
            if exc.code == "SANDBOX_GONE":
                raise
            raise SandboxUnreachable(sandbox_id, exc.message) from exc
        except Exception as exc:
            raise SandboxUnreachable(sandbox_id, str(exc)) from exc
        try:
            return await handle.commands.run(command)
        except SandboxUnreachable:
            raise
        except Exception as exc:
            raise SandboxProbeFailed(sandbox_id, probe=probe, detail=str(exc)) from exc
        finally:
            close = getattr(handle, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    await close()

    async def describe_sandbox(self, sandbox_id: str) -> SandboxDescriptor:
        """The control plane's description of one sandbox by id."""
        _ = sandbox_id
        raise APIError(
            code="SANDBOX_DESCRIBE_UNSUPPORTED",
            message=(
                f"sandbox backend {self.name!r} cannot describe a sandbox: its "
                "control plane has no inventory operation"
            ),
            status_code=501,
        )

    async def read_diagnostics(self, sandbox_id: str, *, scope: str) -> SandboxDiagnostics:
        """One plain-text diagnostic report about one sandbox.

        ``scope`` is one of :data:`SANDBOX_DIAGNOSTIC_SCOPES`. Support is per
        backend AND, for a backend whose control plane fronts several runtimes,
        potentially per runtime — so an implementation that CAN ask must still
        relay a refusal from below as a refusal (a 501 stating which server said
        so), never as an empty or invented report.
        """
        _ = (sandbox_id, scope)
        raise APIError(
            code="SANDBOX_DIAGNOSTICS_UNSUPPORTED",
            message=(f"sandbox backend {self.name!r} produces no diagnostic reports"),
            status_code=501,
        )


@dataclass(frozen=True, slots=True)
class SandboxCapabilityRule:
    """What declaring one capability flag obliges a provider to implement.

    ``obliges`` are the :class:`SandboxProvider` methods the provider must
    override when the flag is True; ``requires`` are other flags that must be
    True with it.
    """

    flag: str
    obliges: tuple[str, ...]
    requires: tuple[str, ...] = ()


#: Every capability flag and its obligations, checked by :func:`register_sandbox`.
#: A flag added to :class:`SandboxProvider` gets a row here, and with it the
#: registration check; a flag without a row is checked by nothing.
SANDBOX_CAPABILITY_RULES: tuple[SandboxCapabilityRule, ...] = (
    SandboxCapabilityRule(
        "supports_isolated_sessions",
        (
            "read_isolation_capability",
            "prepare_isolated_workspace",
            "open_isolated_session",
            "run_in_isolated_session",
            "stream_in_isolated_session",
            "close_isolated_session",
            "count_active_isolated_sessions",
        ),
    ),
    SandboxCapabilityRule(
        "supports_isolated_session_recovery",
        ("find_isolated_sessions_by_workspace",),
        requires=("supports_isolated_sessions",),
    ),
    SandboxCapabilityRule("supports_pause", ("pause", "resume")),
    SandboxCapabilityRule(
        "supports_client_pool",
        (
            "ensure_client_pool",
            "acquire_client_pool",
            "describe_client_pool",
            "retire_client_pool",
            "owns_unclaimed_sandbox",
            "client_pool_readiness",
        ),
    ),
    SandboxCapabilityRule("supports_correlated_create", ("find_sandbox_by_assignment",)),
    SandboxCapabilityRule("supports_egress_credential_injection", ("apply_credential_vault",)),
    SandboxCapabilityRule("supports_create_network_policy", ("admit_runtime_egress",)),
    SandboxCapabilityRule("supports_inventory_listing", ("list_sandboxes", "describe_sandbox")),
    # The process face lives on the handle, so the provider has no method to
    # implement for it; the row makes registration check the flag's type.
    SandboxCapabilityRule("supports_process_sessions", ()),
)


def _overrides(provider: Any, method: str) -> bool:
    """Whether ``provider``'s class implements ``method`` itself.

    A class that does not subclass :class:`SandboxProvider` and lacks the
    method does not implement it either.
    """

    own = getattr(type(provider), method, None)
    return own is not None and own is not getattr(SandboxProvider, method, None)


def unsupported_sandbox_feature(
    provider: SandboxProvider, *, feature: str, flags: Sequence[str]
) -> str | None:
    """The refusal for ``feature`` on ``provider``, or ``None`` when it can run.

    ``flags`` are the capabilities the feature needs. A caller raises its own
    error with the sentence (a 400 on a write path, ``BootstrapConfigError`` at
    startup), so a configuration the backend cannot carry is refused where it
    is set instead of failing at the first conversation that needs it.
    """

    missing = [flag for flag in flags if getattr(provider, flag, False) is not True]
    if not missing:
        return None
    return (
        f"sandbox backend {provider.name!r} cannot provide {feature}: it does not "
        f"declare {', '.join(missing)}"
    )


class SandboxBackendDisabled(APIError):
    """A record names a sandbox backend this deployment installed but did not enable.

    The record is not served by another backend: a box created by one backend
    is not addressable through another, and quietly substituting the default
    would act on the wrong sandbox. The operator either enables the backend
    again or retires the records that still name it.
    """

    def __init__(self, backend: str, enabled: list[str]) -> None:
        super().__init__(
            code="SANDBOX_BACKEND_DISABLED",
            message=(
                f"sandbox backend {backend!r} is installed but not enabled in this "
                f"deployment (enabled: {enabled}); add it to "
                "ASTRABOX_SANDBOX_BACKENDS to serve records that name it"
            ),
            status_code=409,
        )
        self.backend = backend


_BACKENDS: dict[str, SandboxProvider] = {}

#: Deployment-configured default backend name. Set once at bootstrap from
#: settings; never hardcoded to a concrete provider here.
_DEFAULT_BACKEND: str = ""

#: The deployment's enabled backend names, published by the composition root.
#: ``None`` until then, which leaves every installed provider answering.
_ENABLED_BACKENDS: tuple[str, ...] | None = None

#: Reads this installation's id from wherever the composition root keeps it,
#: and the id once read. The id never changes for a database, so one read per
#: process is enough.
_INSTALLATION_LOADER: Callable[[], Awaitable[str]] | None = None
_INSTALLATION_ID: str = ""


def register_sandbox(provider: SandboxProvider) -> None:
    """Register a provider under its ``name``. Last registration wins.

    Every flag in :data:`SANDBOX_CAPABILITY_RULES` must be a bool, and a flag
    declared True must come with the methods and flags its rule names: a
    half-declared capability would otherwise fail at the moment a conversation
    needs it.
    """
    name = str(getattr(provider, "name", "") or "").strip().lower()
    if not name:
        raise RuntimeError("sandbox provider must have a non-empty name")
    for rule in SANDBOX_CAPABILITY_RULES:
        if not isinstance(getattr(provider, rule.flag, False), bool):
            raise RuntimeError(
                f"sandbox provider {name!r} capability {rule.flag} must be bool"
            )
    permission_levels = getattr(
        provider,
        "supported_permission_levels",
        (SANDBOX_PERMISSION_LEVEL_DEFAULT,),
    )
    if (
        not isinstance(permission_levels, tuple)
        or not permission_levels
        or any(type(level) is not str for level in permission_levels)
        or len(set(permission_levels)) != len(permission_levels)
        or any(level not in SANDBOX_PERMISSION_LEVELS for level in permission_levels)
    ):
        raise RuntimeError(
            f"sandbox provider {name!r} capability supported_permission_levels "
            f"must be a non-empty tuple drawn from {SANDBOX_PERMISSION_LEVELS!r}"
        )
    if SANDBOX_PERMISSION_LEVEL_PRIVILEGED in permission_levels:
        raise RuntimeError(
            f"sandbox provider {name!r} capability supported_permission_levels "
            "cannot include privileged until the provider seam exposes a live "
            "privileged-container attestation"
        )
    if (
        SANDBOX_PERMISSION_LEVEL_ADVANCED in permission_levels
        and getattr(type(provider), "read_isolation_capability", None)
        is getattr(SandboxProvider, "read_isolation_capability")
    ):
        raise RuntimeError(
            f"sandbox provider {name!r} declares the advanced permission level "
            "but does not override read_isolation_capability to attest it"
        )
    profile_exclusive = getattr(provider, "sandbox_is_profile_exclusive", False)
    if not isinstance(profile_exclusive, bool):
        raise RuntimeError(
            f"sandbox provider {name!r} capability sandbox_is_profile_exclusive must be bool"
        )
    for rule in SANDBOX_CAPABILITY_RULES:
        if not getattr(provider, rule.flag, False):
            continue
        for required in rule.requires:
            if getattr(provider, required, False) is not True:
                raise RuntimeError(
                    f"sandbox provider {name!r} declares {rule.flag}, which "
                    f"requires {required}"
                )
        for op in rule.obliges:
            if not _overrides(provider, op):
                raise RuntimeError(
                    f"sandbox provider {name!r} declares {rule.flag} but does not "
                    f"override {op}"
                )
    owned_settings = getattr(provider, "owned_settings", ())
    if not isinstance(owned_settings, tuple) or any(
        type(setting) is not str or not setting.strip() for setting in owned_settings
    ):
        raise RuntimeError(
            f"sandbox provider {name!r} owned_settings must be a tuple of variable names"
        )
    _BACKENDS[name] = provider


def set_default_sandbox_backend(name: str | None) -> None:
    """Configure the process-wide default backend (deployment configuration).

    Called by the composition root at bootstrap with the settings value. An empty
    name clears the default (dispatch then requires an explicit name, except when
    exactly one provider is registered — the unambiguous case)."""
    global _DEFAULT_BACKEND
    _DEFAULT_BACKEND = str(name or "").strip().lower()


def set_enabled_sandbox_backends(names: Sequence[str]) -> None:
    """Publish the backends this deployment enables (deployment configuration).

    Called by the composition root at bootstrap. Every name must already be
    installed: enabling a backend this process cannot construct would only fail
    later, at the first record that names it. Registering a provider afterwards
    installs it without enabling it, so neither an entry point loaded late nor a
    direct :func:`register_sandbox` call widens the set.
    """
    global _ENABLED_BACKENDS
    normalized = tuple(str(name or "").strip().lower() for name in names)
    if not normalized or any(not name for name in normalized):
        raise RuntimeError("the enabled sandbox backends must be a non-empty list of names")
    duplicates = sorted({name for name in normalized if normalized.count(name) > 1})
    if duplicates:
        raise RuntimeError(f"sandbox backends enabled more than once: {duplicates}")
    missing = [name for name in normalized if name not in _BACKENDS]
    if missing:
        raise RuntimeError(
            f"sandbox backends {missing} are enabled but not installed "
            f"(installed: {sorted(_BACKENDS)})"
        )
    _ENABLED_BACKENDS = normalized


def set_sandbox_installation_loader(loader: Callable[[], Awaitable[str]]) -> None:
    """Configure how :func:`sandbox_installation_id` reads the installation id.

    Called by the composition root, which knows where the id is kept. Setting
    a loader forgets an id read through a previous one.
    """
    global _INSTALLATION_LOADER, _INSTALLATION_ID
    _INSTALLATION_LOADER = loader
    _INSTALLATION_ID = ""


async def sandbox_installation_id() -> str:
    """The id :data:`SANDBOX_INSTALLATION_METADATA_KEY` carries for this installation.

    Read once through the configured loader, then remembered. Raises rather
    than returning a default: a box created or reaped under a guessed id would
    be claimed by, or taken from, another installation. The id must be a
    label-safe value, which the database-generated one is.
    """
    global _INSTALLATION_ID
    if _INSTALLATION_ID:
        return _INSTALLATION_ID
    if _INSTALLATION_LOADER is None:
        raise RuntimeError(
            "no sandbox installation id loader is configured; astrabox.bootstrap "
            "configures it before any sandbox is created or reaped"
        )
    value = str(await _INSTALLATION_LOADER() or "").strip()
    if not value:
        raise RuntimeError("the sandbox installation id loader returned an empty id")
    _INSTALLATION_ID = value
    return value


def default_sandbox_backend() -> str:
    """The effective default backend name, or ``""`` when there is none.

    The configured default wins; with none configured and exactly one provider
    registered, that sole provider is the unambiguous default."""
    if _DEFAULT_BACKEND:
        return _DEFAULT_BACKEND
    if len(_BACKENDS) == 1:
        return next(iter(_BACKENDS))
    return ""


def installed_sandbox_names() -> list[str]:
    """Every installed backend name, sorted. Only for diagnostics and errors:
    whatever a deployment may use comes from :func:`enabled_sandbox_names`."""
    return sorted(_BACKENDS)


def enabled_sandbox_names() -> list[str]:
    """The backends this deployment enables, sorted.

    Every installed backend when the composition root has not published a set
    (see :func:`set_enabled_sandbox_backends`).
    """
    if _ENABLED_BACKENDS is None:
        return sorted(_BACKENDS)
    return sorted(_ENABLED_BACKENDS)


def enabled_sandbox_providers() -> list[tuple[str, SandboxProvider]]:
    """``(name, provider)`` for every enabled backend that is installed, sorted."""
    return [(name, _BACKENDS[name]) for name in enabled_sandbox_names() if name in _BACKENDS]


def enabled_sandbox_permission_levels() -> list[str]:
    """Permission levels at least one enabled backend can attest."""

    supported = {
        level
        for _name, provider in enabled_sandbox_providers()
        for level in getattr(
            provider,
            "supported_permission_levels",
            (SANDBOX_PERMISSION_LEVEL_DEFAULT,),
        )
    }
    return [level for level in SANDBOX_PERMISSION_LEVELS if level in supported]


async def shutdown_sandbox_providers_for_current_loop() -> None:
    """Release every enabled provider's resources for this event loop.

    A provider that is installed but not enabled never served a request, so it
    holds nothing to release.
    """
    errors: list[Exception] = []
    for name, provider in enabled_sandbox_providers():
        try:
            await provider.shutdown_current_loop_resources()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # one provider must not strand the rest
            exc.add_note(f"while shutting down sandbox provider {name!r}")
            errors.append(exc)
    if errors:
        raise ExceptionGroup("sandbox provider shutdown failed", errors)


def any_enabled_backend_supports_egress_credential_injection() -> bool:
    """True iff an enabled sandbox backend can substitute secrets at its
    egress boundary (``supports_egress_credential_injection``).

    Gate for features that only work with such a backend — most importantly the
    vault ``environment_variable`` credential type, whose structural-secrecy
    guarantee requires egress substitution. A backend that is installed but not
    enabled cannot run the Session that would use the credential, so it does
    not count. Callers use it to fail loud at CONFIGURE time rather than
    exposing a feature that can only fail later at session attach.
    """
    return any(
        bool(getattr(provider, "supports_egress_credential_injection", False))
        for _name, provider in enabled_sandbox_providers()
    )


def sandbox_if_registered(backend: str | None) -> SandboxProvider | None:
    """The provider registered under ``backend``, or ``None`` (non-raising lookup
    for callers that must not fail closed on an unregistered name — e.g. the
    entry-point loader asking whether a plugin already claimed this backend)."""
    return _BACKENDS.get(str(backend or "").strip().lower())


def sandbox_for_name(backend: str | None, *, default: str | None = None) -> SandboxProvider:
    """Resolve an enabled provider by name, or raise.

    An empty ``backend`` falls back to ``default`` and then to the configured
    process default (:func:`default_sandbox_backend`). An unknown name raises
    ``RuntimeError`` listing the installed names; an installed name this
    deployment did not enable raises :class:`SandboxBackendDisabled`."""
    name = str(backend or default or "").strip().lower() or default_sandbox_backend()
    if not name:
        raise RuntimeError(
            "sandbox provider name missing and no default backend configured "
            f"(enabled: {enabled_sandbox_names()})"
        )
    provider = _BACKENDS.get(name)
    if provider is None:
        raise RuntimeError(
            f"no SandboxProvider registered for backend={name!r} "
            f"(installed: {sorted(_BACKENDS)})"
        )
    if _ENABLED_BACKENDS is not None and name not in _ENABLED_BACKENDS:
        raise SandboxBackendDisabled(name, enabled_sandbox_names())
    return provider


def sandbox_for_sandbox(sandbox: Any) -> SandboxProvider | None:
    """Reverse-map a live sandbox object to its owning enabled provider, or None."""
    for _name, provider in enabled_sandbox_providers():
        if provider.owns_sandbox(sandbox):
            return provider
    return None


def sandbox_name_for_template(template: Any) -> str:
    """Backend selected by the resolved Agent view or the process default.

    ``template`` is the parameter name for that resolved view.
    """
    raw = (
        template.get("sandbox_backend")
        if isinstance(template, dict)
        else getattr(template, "sandbox_backend", "")
    )
    return str(raw or "").strip().lower() or default_sandbox_backend()


def sandbox_for_template(template: Any) -> SandboxProvider:
    """Resolve the provider selected by the resolved Agent view."""
    return sandbox_for_name(sandbox_name_for_template(template))


__all__ = [
    "ENTRY_POINT_GROUP",
    "SANDBOX_LIFECYCLE_PROBE_OK",
    "SANDBOX_LIFECYCLE_PROBE_NOT_FOUND",
    "SANDBOX_LIFECYCLE_PROBE_FAILED",
    "SANDBOX_DIAGNOSTIC_SCOPES",
    "SANDBOX_MANAGED_BY_METADATA_KEY",
    "SANDBOX_MANAGED_BY_METADATA_VALUE",
    "SANDBOX_ASSIGNMENT_ID_METADATA_KEY",
    "SANDBOX_INSTALLATION_METADATA_KEY",
    "SANDBOX_EXECUTION_TARGET_METADATA_KEY",
    "execution_target_metadata",
    "SANDBOX_SESSION_ID_METADATA_KEY",
    "SandboxClaim",
    "SandboxDestruction",
    "claim_from_metadata",
    "SandboxLifecycleProbeResult",
    "SandboxHttpResponse",
    "SandboxDataPlane",
    "SandboxAllocation",
    "SandboxAllocationScope",
    "SandboxClientPoolCreator",
    "SandboxClientPoolMember",
    "SandboxClientPoolPreparer",
    "SandboxClientPoolSpec",
    "SandboxClientPoolStatus",
    "SandboxCreateSpec",
    "SandboxDescriptor",
    "SandboxDiagnostics",
    "ConversationIdentity",
    "SandboxIsolatedSession",
    "SandboxIsolationCapability",
    "SandboxSecurityPosture",
    "SandboxPage",
    "SandboxRuntimeDefaults",
    "SandboxProvider",
    "SANDBOX_CAPABILITY_RULES",
    "SandboxBackendDisabled",
    "SANDBOX_DATA_PLANE_ALIVE",
    "SANDBOX_DATA_PLANE_DEAD",
    "SANDBOX_DATA_PLANE_UNKNOWN",
    "SandboxCapabilityRule",
    "SandboxClientPoolReadiness",
    "SandboxDataPlaneAssessment",
    "SandboxCommandOutcomeUnknown",
    "SandboxCommandRefused",
    "SandboxCommandResult",
    "SandboxCommands",
    "SandboxDirectorySpec",
    "SandboxEndpoint",
    "SandboxEntry",
    "SandboxEntryKind",
    "SandboxFiles",
    "SandboxMemoryHeadroom",
    "SandboxPathNotFound",
    "SandboxPathRejected",
    "SandboxProbeFailed",
    "SandboxProcessAlreadyAttached",
    "SandboxProcessAttachment",
    "SandboxProcessDetached",
    "SandboxProcessReplayGap",
    "SandboxProcesses",
    "ProcessExit",
    "ProcessFrame",
    "ProcessOutput",
    "ProcessReplay",
    "SandboxResourcesUnsupported",
    "SandboxUnreachable",
    "UnsupportedSandboxProcesses",
    "register_sandbox",
    "set_default_sandbox_backend",
    "set_enabled_sandbox_backends",
    "default_sandbox_backend",
    "set_sandbox_installation_loader",
    "sandbox_installation_id",
    "installed_sandbox_names",
    "enabled_sandbox_names",
    "enabled_sandbox_providers",
    "enabled_sandbox_permission_levels",
    "any_enabled_backend_supports_egress_credential_injection",
    "shutdown_sandbox_providers_for_current_loop",
    "sandbox_for_name",
    "sandbox_for_sandbox",
    "unsupported_sandbox_feature",
    "sandbox_name_for_template",
    "sandbox_for_template",
]
