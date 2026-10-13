"""One deployment integration for every preparation point of an Agent and Session.

The platform owns ordering, placement, fencing, file delivery and failure
handling. The provider owns external identities, declarations, credential
policy and user prerequisites. It never starts a consent flow, registers an
Agent per Session or dispatches work. A deployment installs at most one.
"""
from __future__ import annotations

import inspect
import copy
import dataclasses
import hashlib
import re
from abc import ABC
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlsplit

from astrabox.seams.sandbox import SandboxHandle

TURN_PREPARATION_FAILED = "TURN_PREPARATION_FAILED"
TURN_PREPARATION_USER_ACTION_REQUIRED = "TURN_PREPARATION_USER_ACTION_REQUIRED"


@dataclass(frozen=True)
class PreparedPackage:
    source: Mapping[str, Any]
    checkout_path: str


@dataclass(frozen=True)
class AgentPreparationOperation:
    """Durable identity of one callback, including recovery after host failure."""
    agent_id: str
    operation_id: str


@dataclass(frozen=True)
class AgentPreparationContext:
    operation: AgentPreparationOperation
    creator_user_id: str
    config_version: int
    preparation_id: str
    runtime_generation: str
    config: Mapping[str, Any] = field(repr=False)
    sandbox: SandboxHandle = field(repr=False)
    packages: tuple[PreparedPackage, ...]
    skills_path: str | None


@dataclass(frozen=True)
class RuntimePreparationPlan:
    """Whether engine initialization needs a bound Session's materials.

    A Session-bound engine is not initialized in unclaimed inventory. The
    platform still prepares its image, workspace identity and Agent resources.
    This declaration contains no credentials and participates in generation.
    """
    session_bound: bool = False


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
    """Persisted owner and resolved placement before the engine initializes.

    Runs on cold start, claim of waiting inventory, reattachment and sandbox
    replacement, each before ``EngineAdapter.activate_runtime``.
    """
    session_id: str
    principal_id: str
    agent_id: str | None
    config_version: int
    runtime_generation: str
    sandbox_id: str
    runtime_identity: Mapping[str, Any]
    config: Mapping[str, Any] = field(repr=False)
    sandbox: SandboxHandle = field(repr=False)

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
    """Identity and Agent binding resolved by the host, never by request JSON."""
    session_id: str
    principal_id: str
    agent_id: str | None


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

    Keep the Agent's external identity stable. Read the actual prepared files,
    merge declarations, and skip unchanged external writes in this adapter.
    Config versions and pool members are not external identity keys. The host
    serializes operations per Agent across replicas and generations.
    """
    name: str
    #: Seconds a turn preparation stays valid. When set, the host re-runs
    #: ``prepare_turn`` for the same attempt before it lapses while the engine
    #: write is live; a failed refresh quarantines the attempt.
    turn_validity_seconds: float | None = None

    def plan(self, *, config: Mapping[str, Any]) -> RuntimePreparationPlan:
        """Declare preparation requirements without I/O or personal identity."""
        return RuntimePreparationPlan()

    async def synchronize(self, *, context: AgentPreparationContext) -> None:
        """Return only after all external writes have conclusively completed.

        Use operation.operation_id for durable idempotency/recovery. Exceptions,
        cancellation and host loss leave the operation unresolved; no later
        operation may proceed until recover positively fences this operation.
        Never schedule external writes that can outlive a successful return.
        The context contains trusted configuration and must not be logged.
        """

    async def recover(self, *, operation: AgentPreparationOperation) -> bool:
        """True proves this exact operation can never write again.

        Quiesce/fence even a still-running invocation or one not yet received
        by the remote service. A missing result alone is insufficient. Recovery
        must be idempotent and safe concurrently, and must not affect newer
        operations. False keeps preparation blocked and visible as failed.
        The default requires the adapter to resolve the uncertainty first.
        """
        return False

    async def prepare_session(self, *, context: SessionPreparationContext) -> RuntimeMaterials:
        """Resolve existing Session materials before the engine initializes.

        Return files and environment; the platform writes files with workload
        ownership and mode 0600. The environment stays fixed for the life of
        the runtime this startup activates.
        """
        return RuntimeMaterials()

    async def prepare_turn(self, *, context: TurnPreparationContext) -> RuntimeMaterials:
        """Prepare one imminent engine write; idempotent per dispatch attempt.

        Runs under the host's durable per-Session guard and deadline before the
        input reaches the engine. Return refreshed files only (the startup
        environment is fixed); provider-owned state may also be renewed here.
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
    "synchronize", "recover", "prepare_session", "prepare_turn",
    "read_user_actions", "perform_user_action",
)


def register_runtime_preparation_provider(provider: RuntimePreparationProvider) -> None:
    global _PROVIDER
    validity = getattr(provider, "turn_validity_seconds", None)
    if (
        not isinstance(provider, RuntimePreparationProvider)
        or not isinstance(getattr(provider, "name", None), str)
        or not provider.name.strip()
        or any(not inspect.iscoroutinefunction(getattr(provider, method, None))
               for method in _ASYNC_METHODS)
    ):
        raise TypeError("runtime-preparation provider requires a name and async preparation methods")
    if validity is not None and (
        isinstance(validity, bool) or not isinstance(validity, (int, float)) or validity <= 0
    ):
        raise TypeError("turn_validity_seconds must be a positive number or None")
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


def runtime_preparation_plan(config: Mapping[str, Any] | Any) -> RuntimePreparationPlan:
    """Read the declaration used by all preparation and startup paths."""
    provider = runtime_preparation_provider()
    if provider is None:
        return RuntimePreparationPlan()
    values = dataclasses.asdict(config) if dataclasses.is_dataclass(config) and not isinstance(config, type) else (
        dict(config) if isinstance(config, Mapping) else vars(config).copy()
    )
    result = provider.plan(config=copy.deepcopy(values))
    if not isinstance(result, RuntimePreparationPlan) or type(result.session_bound) is not bool:
        raise TypeError("runtime preparation plan must declare a boolean session_bound")
    return result
