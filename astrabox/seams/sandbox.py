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

This module imports only the standard library plus import-light sibling seams and
``astrabox.common.utils.errors`` (the transport-neutral error type) — never the
orchestrator core, so a provider distribution can depend on the seam without
dragging the host in.
"""

from __future__ import annotations

import asyncio
import time
import contextlib
import inspect
import json as _json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Literal, Mapping, Protocol, cast, runtime_checkable

from astrabox.common.utils.errors import APIError
from astrabox.seams.egress_credentials import SandboxEgressCredentialPlan
from astrabox.seams.sandbox_disposal import (
    SandboxClaim,
    SandboxDestruction,
    claim_from_metadata,
)

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


@runtime_checkable
class SandboxEndpointRef(Protocol):
    """What :meth:`SandboxHandle.get_endpoint` returns: an object carrying the
    resolved endpoint URL/host under ``.endpoint``. Matches both the cloud-SDK
    endpoint objects and the in-tree ``_Endpoint`` value type."""

    endpoint: str


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
    """The duck contract core holds on a created/connected sandbox.

    This is the implicit shape behind the ``Any`` returned by
    :meth:`SandboxProvider.create_sandbox` / :meth:`SandboxProvider.connect`.
    Core code relies on exactly two things:

    * **identity** — ``sandbox_id`` (``extract_sandbox_id`` probes
      ``code_interpreter_id`` / ``sandbox_id`` / ``id`` in that order; expose at
      least one, ``sandbox_id`` preferred), and
    * **endpoint resolution** — ``await get_endpoint(port)`` returning an
      object with ``.endpoint`` (see :class:`SandboxEndpointRef`); raise when
      the port is not published rather than returning a wrong address.

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

    async def get_endpoint(self, port: int = ...) -> SandboxEndpointRef: ...


@runtime_checkable
class SandboxEndpointFilesystem(Protocol):
    """Optional file service at a conversation's provider-owned endpoint.

    The provider resolves routing and supplies its native filesystem API.
    Callers select the existing service port and its access headers; they do
    not translate isolated paths into the box's physical filesystem.
    """

    async def get_filesystem(
        self, port: int, *, headers: Mapping[str, str] | None = None
    ) -> Any: ...

    async def search_file_paths(
        self, path: str, pattern: str, *, port: int,
        headers: Mapping[str, str] | None = None,
    ) -> list[str]:
        """Find matching paths in that endpoint's filesystem without SDK types."""
        ...


@dataclass(frozen=True, slots=True)
class SandboxClientPoolMember:
    """Provider-issued identity for one not-yet-claimed pool member.

    The pool implementation owns this technical identity because it is what
    lets that implementation recover a create that finished before the member
    reached its distributed idle inventory.  The platform receives it only so
    the complete :class:`SandboxCreateSpec` carries the same durable identity;
    it remains unrelated to any eventual user Session.

    The current contract deliberately exposes one member ordinal.  Providers
    must refuse capacities they cannot give distinct durable identities rather
    than reusing one assignment across concurrent creates.
    """

    pool_name: str
    member_index: int
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
    resource_limits: dict[str, str]
    resource_requests: dict[str, str]
    image: str | None = None
    entrypoint: tuple[str, ...] | None = None
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    metadata: Mapping[str, str] = field(default_factory=dict)
    #: ``(box_path, storage_subpath)`` pairs naming what must outlive this box.
    #: Backend-neutral on purpose: the platform plans WHERE a subject's durable
    #: files live (`plan_subject_storage_mounts`), and the provider decides what
    #: medium answers for them. A provider that cannot mount anything must
    #: refuse a non-empty value rather than create a box whose files vanish with
    #: it — which is the failure this field exists to end: the seam kept
    #: `uses_create_oss_mounts = True`, meaning "the create path mounts this",
    #: while the create path had nothing to mount with.
    workspace_mounts: tuple[tuple[str, str], ...] = ()
    workspace_volume: str | None = None
    """Storage-provider provisioned volume; the sandbox provider only mounts it."""
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


TURN_PREPARATION_FAILED = "TURN_PREPARATION_FAILED"
TURN_PREPARATION_USER_ACTION_REQUIRED = "TURN_PREPARATION_USER_ACTION_REQUIRED"
TURN_PREPARATION_CONTRACT_VERSION = 1


class TurnPreparationUserActionRequired(Exception):
    """The deliberately-narrow escape hatch from the swallow-everything rule.

    Preparation cannot proceed until the USER acts (e.g. the principal has
    not yet authorized the agent against the credential system) and the
    provider knows the small, safe payload the user needs — a message and
    optionally a URL to visit. The host validates every field before any of
    it becomes user-visible (bounded lengths, no control characters, http(s)
    URLs only) and maps the failure onto a distinct, non-retryable,
    user-visible outcome instead of the generic infrastructure fault. Any
    OTHER exception a provider raises stays swallowed — arbitrary provider
    error text can carry secrets.
    """

    def __init__(
        self,
        *,
        message: str,
        url: str | None = None,
        status: str = "user_action_required",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.url = url
        self.status = status


@dataclass(frozen=True, slots=True)
class SandboxTurnContext:
    """Provider-neutral input for one imminent engine write.

    The context is intentionally small and immutable.  ``principal_id`` is the
    opaque canonical owner copied from the persisted session row; it is not a
    request actor, display name, email address, or full session mapping.
    ``dispatch_attempt_id`` identifies this exact write attempt, including
    reconnect retries.
    """

    sandbox_backend: str
    sandbox_id: str
    session_id: str
    principal_id: str = field(repr=False)
    turn_id: str
    engine_kind: str
    dispatch_attempt_id: str
    sandbox_handle: SandboxHandle | None = field(default=None, repr=False, compare=False)


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
    #: The shared assistant workspace root (``/home/conversations``) is already
    #: present in the box (baked into the image / bound at create), so the runtime
    #: must NOT attempt a network-storage (NAS) mount for it. Default False (cloud
    #: backends mount the shared root at runtime).
    assistant_workspace_root_preprovisioned: bool = False
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
    #: Opt in to the security-sensitive hook invoked immediately before every
    #: real engine write.  Until a distributed sandbox-scoped lease/token seam
    #: exists, registration permits this only for profile-exclusive sandboxes;
    #: shared-sandbox identity mutation cannot be made safe by a process lock.
    supports_turn_preparation: bool = False
    #: Version of the opt-in turn-preparation contract. Providers enabling the
    #: capability must declare the exact host version so new hosts fail loud on
    #: incompatible semantics while SEAMS_API_VERSION remains additive-stable.
    turn_preparation_contract_version: int | None = None
    #: Every sandbox this backend CREATES ends on its own, without anything
    #: from AstraBox: the create carries a lease the control plane honours, and
    #: a box whose lease lapses is terminated by the backend. Default False.
    #:
    #: This is a PROMISE, not a method, and it is here because one part of the
    #: system trades on it: a create whose answer never arrives leaves a box
    #: nobody holds an id for, and the only thing that bounds that leak is the
    #: box expiring by itself. A backend that does not declare this flag must
    #: not be asked to make creates whose id can go missing — the caller has no
    #: name to retry with and no bound on the leak. Declared here rather than
    #: assumed in a provider's own docstring so a caller can CHECK it, and so a
    #: backend for which it is false is refused instead of silently trusted.
    created_sandboxes_self_expire: bool = False
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
    #: How long one successful preparation stays valid (seconds), for
    #: providers whose prepared state expires (a minted credential's TTL).
    #: When set, the host re-invokes the (idempotent, guard-fenced)
    #: ``prepare_turn`` before expiry while the same dispatch attempt's
    #: engine write is still live; a failed re-preparation quarantines the
    #: guard exactly as a failed initial preparation does. ``None`` (the
    #: default) means the prepared state never expires — no refresh runs.
    turn_preparation_validity_seconds: float | None = None

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

    async def prepare_turn(self, *, context: SandboxTurnContext) -> None:
        """Prepare provider-owned state for one imminent engine write.

        Providers opt in with ``supports_turn_preparation = True`` and must
        implement an idempotent hook for ``context.dispatch_attempt_id``.  The
        default remains a no-op so existing providers are additive-compatible.
        """
        _ = context

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
                endpoint_str = getattr(endpoint, "endpoint", None)
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

    # --- control-plane inventory (the read-only ops face) ----------------------
    # Three optional reads that let an operator see a backend's sandboxes as a
    # RESOURCE rather than only as the side effect of a session. Every default
    # below refuses loud with a 501 that NAMES the backend: a backend whose
    # control plane cannot enumerate or explain its sandboxes must say so, never
    # return an empty page that reads as "there are no sandboxes".
    async def list_sandboxes(self, *, page: int = 1, page_size: int = 50) -> SandboxPage:
        """One page of the sandboxes this backend's control plane knows about.

        Paged because a backend's inventory is unbounded; the caller advances
        while :attr:`SandboxPage.has_next_page` holds. Includes sandboxes this
        deployment did not create — the question is "what is this backend
        running", not "what was asked for".
        """
        _ = (page, page_size)
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

_BACKENDS: dict[str, SandboxProvider] = {}

#: Deployment-configured default backend name. Set once at bootstrap from
#: settings; never hardcoded to a concrete provider here.
_DEFAULT_BACKEND: str = ""


def register_sandbox(provider: SandboxProvider) -> None:
    """Register a provider under its ``name``. Last registration wins."""
    name = str(getattr(provider, "name", "") or "").strip().lower()
    if not name:
        raise RuntimeError("sandbox provider must have a non-empty name")
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
    self_expiring = getattr(provider, "created_sandboxes_self_expire", False)
    if not isinstance(self_expiring, bool):
        raise RuntimeError(
            f"sandbox provider {name!r} capability created_sandboxes_self_expire must be bool"
        )
    isolates_sessions = getattr(provider, "supports_isolated_sessions", False)
    if not isinstance(isolates_sessions, bool):
        raise RuntimeError(
            f"sandbox provider {name!r} capability supports_isolated_sessions must be bool"
        )
    if isolates_sessions:
        # All three or none. A backend that reports the capability but cannot
        # open a session fails at the moment a conversation needs a box, which
        # is the worst possible time to discover a half-declared capability.
        for op in (
            "read_isolation_capability",
            "prepare_isolated_workspace",
            "open_isolated_session",
            "run_in_isolated_session",
            "close_isolated_session",
        ):
            if getattr(type(provider), op, None) is getattr(SandboxProvider, op):
                raise RuntimeError(
                    f"sandbox provider {name!r} declares supports_isolated_sessions "
                    f"but does not override {op}"
                )
    recovers_isolated_sessions = getattr(
        provider, "supports_isolated_session_recovery", False
    )
    if not isinstance(recovers_isolated_sessions, bool):
        raise RuntimeError(
            f"sandbox provider {name!r} capability "
            "supports_isolated_session_recovery must be bool"
        )
    if recovers_isolated_sessions and not isolates_sessions:
        raise RuntimeError(
            f"sandbox provider {name!r} cannot recover isolated sessions "
            "without supporting them"
        )
    if recovers_isolated_sessions and (
        getattr(type(provider), "find_isolated_sessions_by_workspace", None)
        is getattr(SandboxProvider, "find_isolated_sessions_by_workspace")
    ):
        raise RuntimeError(
            f"sandbox provider {name!r} declares isolated-session recovery "
            "but does not override find_isolated_sessions_by_workspace"
        )
    prepares_turns = getattr(provider, "supports_turn_preparation", False)
    if not isinstance(prepares_turns, bool):
        raise RuntimeError(
            f"sandbox provider {name!r} capability supports_turn_preparation must be bool"
        )
    if prepares_turns:
        contract_version = getattr(provider, "turn_preparation_contract_version", None)
        if (
            type(contract_version) is not int
            or contract_version != TURN_PREPARATION_CONTRACT_VERSION
        ):
            raise RuntimeError(
                f"sandbox provider {name!r} turn preparation contract version "
                f"must equal {TURN_PREPARATION_CONTRACT_VERSION}"
            )
        if not profile_exclusive:
            raise RuntimeError(
                f"sandbox provider {name!r} cannot enable turn preparation for "
                "a shared sandbox; set sandbox_is_profile_exclusive=True or "
                "provide a future distributed fencing capability"
            )
        owns_sandbox = getattr(provider, "owns_sandbox", None)
        inherited_owns_sandbox = getattr(type(provider), "owns_sandbox", None)
        if inherited_owns_sandbox is SandboxProvider.owns_sandbox or not callable(owns_sandbox):
            raise RuntimeError(
                f"sandbox provider {name!r} enables turn preparation but does "
                "not implement live-handle ownership validation"
            )
        prepare_turn = getattr(provider, "prepare_turn", None)
        inherited = getattr(type(provider), "prepare_turn", None)
        if inherited is SandboxProvider.prepare_turn or not callable(prepare_turn):
            raise RuntimeError(
                f"sandbox provider {name!r} enables turn preparation but does "
                "not override prepare_turn"
            )
        if not inspect.iscoroutinefunction(prepare_turn):
            raise RuntimeError(f"sandbox provider {name!r} prepare_turn must be async")
        parameters = list(inspect.signature(prepare_turn).parameters.values())
        if [parameter.name for parameter in parameters] != ["context"] or parameters[
            0
        ].kind is not inspect.Parameter.KEYWORD_ONLY:
            raise RuntimeError(
                f"sandbox provider {name!r} prepare_turn must have signature (*, context)"
            )
    validity = getattr(provider, "turn_preparation_validity_seconds", None)
    if validity is not None:
        if not prepares_turns:
            raise RuntimeError(
                f"sandbox provider {name!r} declares "
                "turn_preparation_validity_seconds without enabling "
                "supports_turn_preparation"
            )
        if not isinstance(validity, (int, float)) or isinstance(validity, bool) or validity <= 0:
            raise RuntimeError(
                f"sandbox provider {name!r} turn_preparation_validity_seconds "
                "must be a positive number of seconds"
            )
    _BACKENDS[name] = provider


def set_default_sandbox_backend(name: str | None) -> None:
    """Configure the process-wide default backend (deployment configuration).

    Called by the composition root at bootstrap with the settings value. An empty
    name clears the default (dispatch then requires an explicit name, except when
    exactly one provider is registered — the unambiguous case)."""
    global _DEFAULT_BACKEND
    _DEFAULT_BACKEND = str(name or "").strip().lower()


def default_sandbox_backend() -> str:
    """The effective default backend name, or ``""`` when there is none.

    The configured default wins; with none configured and exactly one provider
    registered, that sole provider is the unambiguous default."""
    if _DEFAULT_BACKEND:
        return _DEFAULT_BACKEND
    if len(_BACKENDS) == 1:
        return next(iter(_BACKENDS))
    return ""


def registered_sandbox_names() -> list[str]:
    """The registered backend names, sorted (for schemas/diagnostics)."""
    return sorted(_BACKENDS)


def registered_sandbox_permission_levels() -> list[str]:
    """Permission levels at least one registered backend can attest."""

    supported = {
        level
        for provider in _BACKENDS.values()
        for level in getattr(
            provider,
            "supported_permission_levels",
            (SANDBOX_PERMISSION_LEVEL_DEFAULT,),
        )
    }
    return [level for level in SANDBOX_PERMISSION_LEVELS if level in supported]


async def shutdown_sandbox_providers_for_current_loop() -> None:
    """Release every registered provider's resources for this event loop."""
    errors: list[Exception] = []
    for name, provider in list(_BACKENDS.items()):
        try:
            await provider.shutdown_current_loop_resources()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # one provider must not strand the rest
            exc.add_note(f"while shutting down sandbox provider {name!r}")
            errors.append(exc)
    if errors:
        raise ExceptionGroup("sandbox provider shutdown failed", errors)


def any_backend_supports_egress_credential_injection() -> bool:
    """True iff any registered sandbox backend can substitute secrets at its
    egress boundary (``supports_egress_credential_injection``).

    Gate for features that only work with such a backend — most importantly the
    vault ``environment_variable`` credential type, whose structural-secrecy
    guarantee requires egress substitution. The answer is derived from every
    registered backend; the built-in OpenSandbox backend advertises support.
    Callers use it to fail loud at CONFIGURE time rather than exposing a feature
    that can only fail later at session attach.
    """
    return any(
        bool(getattr(p, "supports_egress_credential_injection", False)) for p in _BACKENDS.values()
    )


def sandbox_if_registered(backend: str | None) -> SandboxProvider | None:
    """The provider registered under ``backend``, or ``None`` (non-raising lookup
    for callers that must not fail closed on an unregistered name — e.g. the
    entry-point loader asking whether a plugin already claimed this backend)."""
    return _BACKENDS.get(str(backend or "").strip().lower())


def sandbox_for_name(backend: str | None, *, default: str | None = None) -> SandboxProvider:
    """Resolve a provider by name, or raise listing the registered names.

    An empty ``backend`` falls back to ``default`` and then to the configured
    process default (:func:`default_sandbox_backend`)."""
    name = str(backend or default or "").strip().lower() or default_sandbox_backend()
    if not name:
        raise RuntimeError(
            "sandbox provider name missing and no default backend configured "
            f"(registered: {sorted(_BACKENDS)})"
        )
    provider = _BACKENDS.get(name)
    if provider is None:
        raise RuntimeError(
            f"no SandboxProvider registered for backend={name!r} (registered: {sorted(_BACKENDS)})"
        )
    return provider


def sandbox_for_sandbox(sandbox: Any) -> SandboxProvider | None:
    """Reverse-map a live sandbox object to its owning provider, or None."""
    for provider in _BACKENDS.values():
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
    "SandboxTurnContext",
    "SandboxProvider",
    "TURN_PREPARATION_CONTRACT_VERSION",
    "TURN_PREPARATION_FAILED",
    "TURN_PREPARATION_USER_ACTION_REQUIRED",
    "TurnPreparationUserActionRequired",
    "register_sandbox",
    "set_default_sandbox_backend",
    "default_sandbox_backend",
    "registered_sandbox_names",
    "registered_sandbox_permission_levels",
    "shutdown_sandbox_providers_for_current_loop",
    "sandbox_for_name",
    "sandbox_for_sandbox",
    "sandbox_name_for_template",
    "sandbox_for_template",
]
