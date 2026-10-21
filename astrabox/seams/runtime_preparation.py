"""One deployment integration for every preparation point of an Agent, Assistant and Session.

The platform owns ordering, placement, fencing, file delivery and failure
handling. The provider owns external identities, declarations, credential
policy and user prerequisites. It never starts a consent flow, registers an
Agent per Session or dispatches work. A deployment installs at most one.
"""
from __future__ import annotations

import inspect
import asyncio
import copy
import dataclasses
import hashlib
import re
from abc import ABC
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Protocol, TYPE_CHECKING
from urllib.parse import urlsplit

from astrabox.seams.sandbox import SandboxHandle

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

TURN_PREPARATION_FAILED = "TURN_PREPARATION_FAILED"
TURN_PREPARATION_USER_ACTION_REQUIRED = "TURN_PREPARATION_USER_ACTION_REQUIRED"


@dataclass(frozen=True)
class PreparedPackage:
    source: Mapping[str, Any]
    checkout_path: str


@dataclass(frozen=True)
class PreparationSubject:
    """What prepared resources and an external identity belong to.

    An Agent is shared by everyone it is published to; an Assistant belongs to
    one owner. The id is stable for the subject's life and is the key for an
    external identity; revisions are not.
    """
    kind: Literal["agent", "assistant"]
    id: str

    def __post_init__(self) -> None:
        if self.kind not in ("agent", "assistant") or not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("a preparation subject is an agent or assistant with an id")


@dataclass(frozen=True)
class ResourcePreparationOperation:
    """Durable identity of one callback, including recovery after host failure."""
    subject: PreparationSubject
    operation_id: str


class PreparationCommitGuard(Protocol):
    """Host-issued currentness checks held through the caller's local commit.

    Validate before locking provider rows, on the selected persistence engine's
    active transaction. One subject per transaction; no savepoints or remote
    calls while held. A refusal must abort the provider write. The guard neither
    commits nor makes remote effects atomic with the database.
    """

    @property
    def subject(self) -> PreparationSubject: ...

    @property
    def owner_user_id(self) -> str: ...

    @property
    def revision(self) -> str: ...

    @property
    def operation(self) -> ResourcePreparationOperation | None: ...

    async def validate(self, connection: AsyncConnection) -> None:
        """Lock and validate authoritative subject state on this transaction."""
        ...


@dataclass(frozen=True)
class ResourcePreparationContext:
    """A subject's resources, already placed in a box no engine has started in.

    ``revision`` names the configuration the resources were prepared from: an
    Agent's version, or an Assistant's configuration revision. ``packages``
    and ``skills_path`` are the actual prepared paths in ``sandbox``. The host
    supplies ``commit_guard`` for local state writes tied to this operation.
    """
    operation: ResourcePreparationOperation
    owner_user_id: str
    revision: str
    preparation_id: str
    runtime_generation: str
    config: Mapping[str, Any] = field(repr=False)
    sandbox: SandboxHandle = field(repr=False)
    packages: tuple[PreparedPackage, ...]
    skills_path: str | None
    commit_guard: PreparationCommitGuard = field(repr=False)


@dataclass(frozen=True)
class RuntimePreparationPlan:
    """Whether engine initialization needs a bound Session's materials.

    A Session-bound engine is not initialized in unclaimed inventory. The
    platform still prepares its image, workspace identity and Agent resources.
    ``model_credential_delivery="provider"`` delegates model protection and
    claim binding to this integration. The engine receives only a unique inert
    placeholder; MCP and environment credential protection stay independent.
    This declaration contains no credentials and participates in generation.
    """
    session_bound: bool = False
    model_credential_delivery: Literal["platform", "provider"] = "platform"

    def __post_init__(self) -> None:
        if type(self.session_bound) is not bool or self.model_credential_delivery not in ("platform", "provider"):
            raise TypeError("runtime preparation requires session_bound and a model credential delivery owner")


@dataclass(frozen=True)
class RuntimeFile:
    name: str
    content: bytes = field(repr=False)


@dataclass(frozen=True)
class RuntimeMaterials:
    files: tuple[RuntimeFile, ...] = field(default=(), repr=False)
    environment: Mapping[str, str] = field(default_factory=dict, repr=False)


def runtime_material_path(
    runtime_identity: Mapping[str, Any], *, session_id: str, sandbox_id: str, name: str,
) -> str:
    """Place one material in this workload's private home, never its workspace."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name):
        raise ValueError("runtime material requires one safe file name")
    home = str(runtime_identity.get("home_dir") or "").rstrip("/")
    workspace = str(runtime_identity.get("workspace_dir") or "").rstrip("/")
    if workspace and (home == workspace or home.startswith(workspace + "/")):
        raise ValueError("runtime materials cannot be placed in the workspace")
    if not home.startswith("/"):
        raise ValueError("runtime materials require a private workload home")
    scope = hashlib.sha256(f"{session_id}:{sandbox_id}".encode()).hexdigest()[:24]
    return f"{home}/.astrabox-runtime-{scope}-{name}"


@dataclass(frozen=True)
class SessionPreparationContext:
    """Persisted owner and resolved placement before engine activation.

    Runs on cold start, claim of waiting inventory, reattachment and sandbox
    replacement, each before ``EngineAdapter.activate_runtime``. An initialized
    engine accepts files and external binding updates. RuntimeProfile requires an
    engine-native activation mechanism; spawn-only consumers remain Session-bound.
    ``model_credential_placeholder`` is the inert model value the provider must
    bind to this persisted owner before returning, when it owns model delivery.
    """
    session_id: str
    principal_id: str
    subject: PreparationSubject
    revision: str
    runtime_generation: str
    sandbox_id: str
    runtime_identity: Mapping[str, Any]
    config: Mapping[str, Any] = field(repr=False)
    sandbox: SandboxHandle = field(repr=False)
    engine_initialized: bool = False
    model_credential_placeholder: str | None = None

    def file_path(self, name: str) -> str:
        return runtime_material_path(
            self.runtime_identity, session_id=self.session_id,
            sandbox_id=self.sandbox_id, name=name,
        )


@dataclass(frozen=True)
class TurnPreparationContext:
    """One imminent engine write, validated against the persisted Session.

    ``principal_id`` is the opaque owner from the Session row, not a request
    actor. ``dispatch_attempt_id`` identifies this exact write attempt,
    including reconnect retries; the hook must be idempotent for it.
    """
    sandbox_backend: str
    sandbox_id: str
    session_id: str
    principal_id: str = field(repr=False)
    turn_id: str
    engine_kind: str
    dispatch_attempt_id: str
    runtime_identity: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    sandbox_handle: SandboxHandle | None = field(default=None, repr=False, compare=False)

    def file_path(self, name: str) -> str:
        """The same path the Session's startup material used for ``name``."""
        return runtime_material_path(
            self.runtime_identity, session_id=self.session_id,
            sandbox_id=self.sandbox_id, name=name,
        )


class PreparationUserActionRequired(Exception):
    """The deliberately narrow escape hatch from the swallow-everything rule.

    Preparation cannot proceed until the USER acts, and the provider knows the
    small, safe payload the user needs: a message and optionally a URL. The
    host validates every field before any of it becomes user-visible and maps
    it onto a distinct, non-retryable outcome. Any OTHER exception a provider
    raises stays swallowed; arbitrary provider error text can carry secrets.
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


def validated_user_action(value: object) -> dict[str, str | None] | None:
    """Allow only the public, bounded message/status/http(s) URL payload."""
    if not isinstance(value, Mapping):
        return None
    message = value.get("message")
    status = value.get("status", "user_action_required")
    url = value.get("url")
    if not isinstance(message, str) or not message.strip() or len(message) > 500:
        return None
    if any(ord(c) < 32 for c in message):
        return None
    if not isinstance(status, str) or not status or len(status) > 64:
        return None
    if any(ord(c) < 33 for c in status):
        return None
    if url is not None:
        if not isinstance(url, str) or len(url) > 2000 or any(ord(c) < 33 for c in url):
            return None
        try:
            parsed = urlsplit(url)
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
                return None
        except ValueError:
            return None
    return {"status": status, "message": message.strip(), "url": url}


@dataclass(frozen=True)
class SessionUserActionContext:
    """Identity and subject binding resolved by the host, never by request JSON."""
    session_id: str
    principal_id: str
    subject: PreparationSubject


@dataclass(frozen=True)
class SessionUserActionCommand:
    """An explicit operation on the provider's current pending requirement."""
    id: str
    label: str


@dataclass(frozen=True)
class SessionUserActionState:
    ready: bool
    user_action: Mapping[str, Any] | None = None
    actions: tuple[SessionUserActionCommand, ...] = ()

    def public(self) -> dict[str, Any]:
        if type(self.ready) is not bool:
            raise ValueError("ready must be a boolean")
        action = validated_user_action(self.user_action)
        if (self.user_action is not None and action is None) or (not self.ready and action is None):
            raise ValueError("blocked states require a valid user action")
        if self.ready and (self.user_action is not None or self.actions):
            raise ValueError("ready states cannot contain pending operations")
        commands = []
        seen: set[str] = set()
        for command in self.actions:
            if not isinstance(command, SessionUserActionCommand):
                raise ValueError("invalid user-action command")
            if (not isinstance(command.id, str) or not command.id or len(command.id) > 128
                    or any(ord(c) < 33 for c in command.id) or command.id in seen):
                raise ValueError("invalid or duplicate user-action command id")
            if (not isinstance(command.label, str) or not command.label.strip()
                    or len(command.label) > 100 or any(ord(c) < 32 for c in command.label)):
                raise ValueError("invalid user-action command label")
            seen.add(command.id)
            commands.append({"id": command.id, "label": command.label})
        return {"ready": self.ready, "user_action": action, "actions": commands}


class RuntimePreparationProvider(ABC):
    """One deployment-owned integration; independent of the sandbox backend.

    Keep each subject's external identity stable. Read the actual prepared
    files, merge declarations, and skip unchanged external writes in this
    adapter. Revisions and boxes are not external identity keys. The host
    serializes operations per subject across replicas and generations.
    """
    name: str

    async def plan(self, *, config: Mapping[str, Any]) -> RuntimePreparationPlan:
        """Read persisted subject policy without personal identity or side effects."""
        return RuntimePreparationPlan()

    async def synchronize(self, *, context: ResourcePreparationContext) -> None:
        """Return only after all external writes have conclusively completed.

        Runs after the subject's Skills and packages are in the box and before
        any engine there starts: before an Agent's waiting inventory is
        published, and before an Assistant's workspace starts Hermes.

        Use operation.operation_id for durable idempotency/recovery. Exceptions,
        cancellation and host loss leave the operation unresolved; no later
        operation may proceed until recover positively fences this operation.
        Never schedule external writes that can outlive a successful return.
        The context contains trusted configuration and must not be logged.
        """

    async def recover(self, *, operation: ResourcePreparationOperation) -> bool:
        """True proves this exact operation can never write again.

        Quiesce/fence even a still-running invocation or one not yet received
        by the remote service. A missing result alone is insufficient. Recovery
        must be idempotent and safe concurrently, and must not affect newer
        operations. False keeps preparation blocked and visible as failed.
        The default requires the adapter to resolve the uncertainty first.
        """
        return False

    async def prepare_session(self, *, context: SessionPreparationContext) -> RuntimeMaterials:
        """Resolve existing Session materials before engine activation.

        Return files and environment; the platform writes files with workload
        ownership and mode 0600. The engine adapter applies environment through
        its declared startup or native activation mechanism.
        """
        return RuntimeMaterials()

    async def prepare_turn(self, *, context: TurnPreparationContext) -> RuntimeMaterials:
        """Prepare one input just before it reaches the engine; idempotent per input.

        Runs once per input, in the process whose reader holds the Session,
        under a deadline; ``context.dispatch_attempt_id`` is the input's id.
        Return refreshed files only (the startup environment is fixed);
        provider-owned state may also be renewed here. Raising refuses the
        input, which fails with the error code shown to the person.
        """
        return RuntimeMaterials()

    async def read_user_actions(self, *, context: SessionUserActionContext) -> SessionUserActionState:
        """Read prerequisites; never initiate consent, issue credentials or dispatch work."""
        return SessionUserActionState(ready=True)

    async def perform_user_action(
        self, *, context: SessionUserActionContext, action_id: str,
    ) -> SessionUserActionState:
        """Perform an operation that ``read_user_actions`` currently offers.

        Recheck the current user's pending operation. Repeated execution of
        the same current operation must be idempotent across replicas; stale
        operations must not authorize a different request. Keep all grants,
        operation identities and credentials in the provider's storage.
        """
        return await self.read_user_actions(context=context)


_PROVIDER: RuntimePreparationProvider | None = None
_ASYNC_METHODS = (
    "plan", "synchronize", "recover", "prepare_session", "prepare_turn",
    "read_user_actions", "perform_user_action",
)


def register_runtime_preparation_provider(provider: RuntimePreparationProvider) -> None:
    global _PROVIDER
    if (
        not isinstance(provider, RuntimePreparationProvider)
        or not isinstance(getattr(provider, "name", None), str)
        or not provider.name.strip()
        or any(not inspect.iscoroutinefunction(getattr(provider, method, None))
               for method in _ASYNC_METHODS)
    ):
        raise TypeError("runtime-preparation provider requires a name and async preparation methods")
    if _PROVIDER is not None and _PROVIDER is not provider:
        raise RuntimeError("a runtime-preparation provider is already registered")
    _PROVIDER = provider


def runtime_preparation_provider() -> RuntimePreparationProvider | None:
    return _PROVIDER


def prepares_turns(provider: RuntimePreparationProvider | None) -> bool:
    """Whether the provider implements turn preparation itself."""
    return provider is not None and (
        getattr(type(provider), "prepare_turn", None) is not RuntimePreparationProvider.prepare_turn
    )


async def resolve_runtime_preparation_plan(config: Any) -> RuntimePreparationPlan:
    """Resolve one immutable runtime view's declaration from persisted policy."""
    provider = runtime_preparation_provider()
    if provider is None:
        return RuntimePreparationPlan()
    values = dataclasses.asdict(config) if dataclasses.is_dataclass(config) and not isinstance(config, type) else (
        dict(config) if isinstance(config, Mapping) else vars(config).copy()
    )
    async with asyncio.timeout(30.0):
        result = await provider.plan(config=copy.deepcopy(values))
    if not isinstance(result, RuntimePreparationPlan):
        raise TypeError("runtime preparation plan requires RuntimePreparationPlan")
    return result


def runtime_preparation_plan(config: Mapping[str, Any] | Any) -> RuntimePreparationPlan:
    """Read the resolved declaration; never call the provider again during startup."""
    result = config.get("runtime_preparation") if isinstance(config, Mapping) else getattr(config, "runtime_preparation", None)
    if isinstance(result, RuntimePreparationPlan):
        return result
    if isinstance(result, Mapping):
        return RuntimePreparationPlan(**dict(result))
    if runtime_preparation_provider() is None:
        return RuntimePreparationPlan()
    raise TypeError("runtime preparation plan has not been resolved for this runtime view")
