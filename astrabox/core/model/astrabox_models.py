from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class SessionState(str, Enum):
    CREATING = "CREATING"
    READY = "READY"
    BACKGROUND_RUNNING = "BACKGROUND_RUNNING"
    BUSY = "BUSY"
    INTERRUPTING = "INTERRUPTING"
    TERMINATED = "TERMINATED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    DELETED = "DELETED"


VALID_TRANSITIONS: dict[str, set[str]] = {
    SessionState.CREATING.value: {
        SessionState.READY.value, SessionState.TERMINATED.value,
    },
    SessionState.READY.value: {
        SessionState.BACKGROUND_RUNNING.value,
        SessionState.BUSY.value,
        SessionState.TERMINATED.value,
        SessionState.DELETED.value, SessionState.RECOVERY_REQUIRED.value,
    },
    SessionState.BACKGROUND_RUNNING.value: {
        SessionState.READY.value,
        SessionState.BUSY.value,
        SessionState.TERMINATED.value,
        SessionState.DELETED.value, SessionState.RECOVERY_REQUIRED.value,
    },
    SessionState.BUSY.value: {
        SessionState.READY.value, SessionState.INTERRUPTING.value,
        SessionState.RECOVERY_REQUIRED.value, SessionState.TERMINATED.value,
    },
    SessionState.INTERRUPTING.value: {
        SessionState.READY.value, SessionState.RECOVERY_REQUIRED.value,
    },
    SessionState.RECOVERY_REQUIRED.value: {
        SessionState.READY.value, SessionState.CREATING.value,
        SessionState.TERMINATED.value, SessionState.DELETED.value,
    },
    SessionState.TERMINATED.value: {
        SessionState.CREATING.value, SessionState.DELETED.value,
    },
    SessionState.DELETED.value: set(),
}


def validate_transition(from_state: str, to_state: str) -> bool:
    """Return True if the transition is valid per the state diagram."""
    allowed = VALID_TRANSITIONS.get(from_state)
    if allowed is None:
        return True
    return to_state in allowed


class ErrorCode(str, Enum):
    UNAUTHORIZED = "UNAUTHORIZED"
    TEMPLATE_NOT_ALLOWED = "TEMPLATE_NOT_ALLOWED"
    SESSION_BUSY = "SESSION_BUSY"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    AGENT_RUNTIME_ERROR = "AGENT_RUNTIME_ERROR"
    INVALID_REQUEST = "INVALID_REQUEST"


@dataclass
class AgentView:
    """The resolved runtime view of one Agent or Assistant.

    This is the in-memory object the runtime consumes, not the stored shape.
    ``AgentConfigService`` builds it by merging an
    ``agents`` document with its named ``environment``: the environment
    contributes the sandbox runtime fields and ``provider_access``, from which
    ``model_config`` is synthesized alongside ``model``. ``model_config`` is a
    resolution-boundary transport dict (``model_name`` / ``base_url`` /
    ``api_key`` / ``api_key_secret_name``) read by ``runtime.config_resolver``,
    ``engine.claude_code_runtime`` and ``engine.hermes``; it is never a
    stored or editable field. Its inputs are ``agent.model`` plus
    ``environment.provider_access`` (see ``docs/domain-model.md``).
    """

    # ── identity ─────────────────────────────────────────────────────────
    #: Stable Agent identity used by every runtime path. None only for an
    #: assistant-derived view (an assistant is not an agent — it resolves via
    #: its environment_name).
    agent_id: str | None = None
    name: str = ""
    description: str | None = None
    # ── model ────────────────────────────────────────────────────────────
    #: First-class model id (e.g. ``claude-opus-4-8``). The source of truth;
    #: ``model_config.model_name`` is derived from it at resolution.
    model: str | None = None
    #: Synthesized transport dict (model_name/base_url/api_key/…), populated by
    #: the resolver from ``model`` + the environment's ``provider_access``.
    model_config: dict[str, Any] | None = None
    # ── Agent program configuration ─────────────────────────────────────
    #: Platform-owned system instructions. Each adapter maps these to
    #: its own native prompt/configuration contract.
    system: str | None = None
    #: Remote MCP server definitions as a flat name→server-def map.
    mcp_servers: dict[str, Any] | None = None
    skills: list[str] = field(default_factory=list)
    #: Opaque options interpreted only by the selected engine adapter.
    engine_options: dict[str, Any] | None = None
    default_repo: dict[str, Any] | None = None
    plugin_repos: list[dict[str, Any]] = field(default_factory=list)
    #: Administrator-managed credential policy. This is carried only on the
    #: internal runtime view and is never exposed through Agent/Assistant APIs.
    credential_vault_ids: list[str] = field(default_factory=list)
    #: The compute and provider access used by this Agent.
    environment_name: str | None = None
    #: Registered engine adapter selected by the Environment. This is runtime
    #: identity, not an Agent-owned override.
    engine_kind: str | None = None
    #: Which registered model gateway the sandbox's traffic uses, resolved from
    #: the environment. Also mirrored into
    #: ``model_config["endpoint_provider"]`` for the resolution-boundary override.
    endpoint_provider: str | None = None
    # ── runtime settings ─────────────────────────────────────────────────
    exposure_mode: str | None = None
    idle_hibernate_seconds: int | None = None
    #: Keep one platform-ready runtime prepared for this Agent. Shared tenancy
    #: obtains base boxes through the sandbox provider's client-pool capability;
    #: conversation tenancy prepares a complete dedicated runtime directly.
    #: Which workspace surfaces a person needs to inspect this Agent's work.
    #: A diff view serves only Agents that edit files, and a terminal only
    #: those whose reader works in a shell; AstraBox runs Agent programs of any
    #: purpose, so both are declared rather than assumed. Hiding the terminal
    #: is not an access control — the sandbox stays reachable through the API.
    terminal_panel: bool = False
    diff_panel: bool = False
    prewarm_enabled: bool = False
    #: Auto-incrementing Agent version used for optimistic concurrency.
    version: int | None = None
    # ── environment-derived runtime fields ───────────────────────────────
    runtime_template_name: str | None = None
    #: How many conversations a box of this environment carries — ``conversation``
    #: (one each) or ``agent`` (one box per agent, conversations isolated inside
    #: it). This states the operator's intent, which is a separate question from
    #: what the substrate can do: a pool whose boxes can carry several still
    #: serves one each unless an environment asks otherwise. None ==
    #: ``conversation``.
    sandbox_tenancy: str | None = None
    #: What a box of this environment is GRANTED — ``default`` (the substrate's
    #: minimum), ``advanced`` (namespace creation inside the box, which agent
    #: tenancy needs and an engine running its own in-box sandbox needs too), or
    #: ``privileged``. A separate question from tenancy: one conversation per box
    #: still needs ``advanced`` when the engine enforces its permission mode
    #: inside. None == ``default``, so nothing is granted unasked.
    sandbox_permission_level: str | None = None
    #: Underlying sandbox backend selector; None == the default backend.
    sandbox_backend: str | None = None
    status: str | None = None
    scope: str | None = None
    image: str | None = None
    entrypoint: str | None = None
    display_name: str | None = None
    icon: str | None = None
    tags: list[str] = field(default_factory=list)
    use_cases: list[str] = field(default_factory=list)
    #: Environment-owned outbound reachability. The runtime parses this product
    #: shape into the provider-neutral sandbox seam; credential bindings are a
    #: separate input and never appear here.
    networking: dict[str, Any] | None = None
    #: What becomes of this environment's boxes when a conversation goes quiet:
    #: ``terminate`` (the box and its workspace go) or ``pause`` (the filesystem
    #: is committed and the next turn resumes the same box). None == the
    #: environment does not say, so the deployment default
    #: (``ASTRABOX_SANDBOX_IDLE_ACTION``) applies.
    idle_action: str | None = None
    #: The environment's ``tracing`` document, carried unvalidated because
    #: ``seams.tracing.parse_tracing_config`` is the one place that decides what
    #: a valid one is, at write time and again when the runtime reads it back.
    tracing: dict[str, object] | None = None
    #: Internal revision inputs for prepared-runtime replacement. They are
    #: resolved from stored configuration and never form an editable API field.
    environment_updated_at: str | None = None
    runtime_generation: str | None = None
    sandbox_generation: str | None = None
    #: A supplier pool lifetime is distinct from reusable box compatibility.
    client_pool_epoch: str | None = None
    #: Explicit administrator refresh, including unchanged Git branch names.
    prewarm_revision: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SessionSummary:
    session_id: str
    user_id: str
    template_name: str
    state: SessionState
    sandbox_id: str | None = None
    engine_session_key: str | None = None
    title: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    deleted: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["state"] = self.state.value
        return payload


@dataclass
class MessageRecord:
    session_id: str
    turn_id: str
    role: str
    content: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
