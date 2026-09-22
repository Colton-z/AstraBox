from __future__ import annotations

from typing import Any, NamedTuple


class ErrorSpec(NamedTuple):
    code: str
    status_code: int = 500
    category: str = "internal"
    retryable: bool = False
    owner: str = "platform"
    user_message: str | None = None


_ERROR_SPECS: dict[str, ErrorSpec] = {}

# Who has to act. A row picks one of these and nothing else, because a value
# that appears once is worth nothing to whoever routes on it, and a set that is
# not enforced grows a new member every time somebody writes a row.
#
# `unknown` is deliberately absent: it is what `error_spec` answers when it has
# no row, and a row that claims it would be asserting the same ignorance the
# fallback admits.
ERROR_OWNERS = frozenset(
    {
        "client",  # the caller's request or credential
        "session",  # the conversation's own state
        "mongo",  # the record store
        "runtime",  # the sandbox and what runs inside it
        "template",  # a value the Agent supplied
        "platform",  # AstraBox itself, including this deployment's configuration
    }
)

# The owner of a code no row claims. Not `platform`: a code can reach the
# envelope from outside — forwarded from a model gateway or a sandbox provider,
# or received from the in-box sidecar — and naming AstraBox the owner of one of
# those is a claim nothing here can support. An admission routes no worse than a
# wrong answer and reads honestly.
UNKNOWN_OWNER = "unknown"


def register_error(
    code: str,
    *,
    status_code: int = 500,
    category: str = "internal",
    retryable: bool = False,
    owner: str = "platform",
    user_message: str | None = None,
) -> ErrorSpec:
    normalized = str(code or "").strip().upper()
    if not normalized:
        raise ValueError("error code is required")
    resolved_owner = str(owner or "platform").strip() or "platform"
    if resolved_owner not in ERROR_OWNERS:
        raise ValueError(
            f"{normalized}: owner={resolved_owner!r} is not one of "
            f"{sorted(ERROR_OWNERS)}"
        )
    spec = ErrorSpec(
        code=normalized,
        status_code=int(status_code),
        category=str(category or "internal").strip() or "internal",
        retryable=bool(retryable),
        owner=resolved_owner,
        user_message=str(user_message).strip() if user_message is not None else None,
    )
    _ERROR_SPECS[normalized] = spec
    return spec


def error_spec(code: str, *, status_code: int | None = None) -> ErrorSpec:
    normalized = str(code or "").strip().upper() or "UNKNOWN_ERROR"
    spec = _ERROR_SPECS.get(normalized)
    if spec is not None:
        return spec
    return ErrorSpec(
        code=normalized,
        status_code=int(status_code or 500),
        category="unregistered",
        retryable=False,
        owner=UNKNOWN_OWNER,
    )


def _clean_mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    cleaned = {str(key): item for key, item in value.items() if item is not None}
    return cleaned or None


class APIError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int,
        data: Any = None,
        *,
        category: str | None = None,
        retryable: bool | None = None,
        user_message: str | None = None,
        debug_message: str | None = None,
        evidence: dict[str, Any] | None = None,
        cause_code: str | None = None,
    ) -> None:
        super().__init__(message)
        spec = error_spec(code, status_code=status_code)
        self.code = str(code or spec.code).strip().upper()
        self.message = str(message or "")
        self.status_code = int(status_code or spec.status_code)
        self.data = data
        self.category = str(category or spec.category or "internal").strip() or "internal"
        self.retryable = bool(spec.retryable if retryable is None else retryable)
        self.owner = spec.owner
        self.user_message = str(user_message or spec.user_message or self.message).strip()
        self.debug_message = str(debug_message or "").strip() or None
        self.evidence = _clean_mapping(evidence)
        self.cause_code = str(cause_code or "").strip().upper() or None

    def to_error_envelope(self) -> dict[str, Any]:
        envelope: dict[str, Any] = {
            "code": self.code,
            "status_code": self.status_code,
            "category": self.category,
            "retryable": self.retryable,
            "owner": self.owner,
            "user_message": self.user_message,
        }
        if self.debug_message:
            envelope["debug_message"] = self.debug_message
        if self.evidence:
            envelope["evidence"] = self.evidence
        if self.cause_code:
            envelope["cause_code"] = self.cause_code
        return envelope

    def to_response_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.user_message,
            "data": self.data,
            "error": self.to_error_envelope(),
        }

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.code}: {self.message}"


def make_api_error(
    code: str,
    message: str,
    *,
    status_code: int | None = None,
    data: Any = None,
    category: str | None = None,
    retryable: bool | None = None,
    user_message: str | None = None,
    debug_message: str | None = None,
    evidence: dict[str, Any] | None = None,
    cause_code: str | None = None,
) -> APIError:
    spec = error_spec(code, status_code=status_code)
    return APIError(
        code=spec.code,
        message=message,
        status_code=int(status_code or spec.status_code),
        data=data,
        category=category,
        retryable=retryable,
        user_message=user_message,
        debug_message=debug_message,
        evidence=evidence,
        cause_code=cause_code,
    )


for _code, _status, _category, _retryable, _owner in (
    ("INVALID_REQUEST", 400, "request", False, "client"),
    ("CHANNEL_PROVIDER_UNKNOWN", 400, "request", False, "client"),
    ("CHANNEL_CONFIG_INVALID", 400, "request", False, "client"),
    ("CHANNEL_CREDENTIALS_INVALID", 400, "request", False, "client"),
    ("CHANNEL_UNAUTHORIZED", 401, "auth", False, "client"),
    ("CHANNEL_BAD_PAYLOAD", 400, "request", False, "client"),
    ("CHANNEL_CALLBACK_NOT_FOUND", 404, "request", False, "client"),
    ("CHANNEL_CALLBACK_PATH_REQUIRED", 404, "request", False, "client"),
    ("NOT_A_CHANNEL", 409, "state", False, "client"),
    ("DEPLOYMENT_UNAUTHORIZED", 401, "auth", False, "client"),
    ("DEPLOYMENT_MISCONFIGURED", 409, "state", False, "platform"),
    ("DEPLOYMENT_NO_OWNER", 409, "state", False, "platform"),
    # An Environment naming a permission level the platform does not define.
    # `template` because the value is stored on the Environment, which is what
    # a session's runtime is rendered from — the same owner as the other
    # unsupported-value refusals read off a stored definition. Not retryable:
    # the same Environment produces the same refusal until someone edits it.
    ("UNSUPPORTED_SANDBOX_PERMISSION_LEVEL", 409, "state", False, "template"),
    (
        "SANDBOX_ISOLATION_UNSUPPORTED",
        409,
        "sandbox.isolation",
        False,
        "runtime",
    ),
    (
        "CHANNEL_CREDENTIALS_UNAVAILABLE",
        503,
        "channel.credentials",
        False,
        "platform",
    ),
    (
        "CHANNEL_GATEWAY_UNAVAILABLE",
        503,
        "channel.gateway",
        True,
        "platform",
    ),
    ("INVALID_SCHEDULE", 400, "request", False, "client"),
    ("SCHEDULE_UNAVAILABLE", 409, "state", False, "platform"),
    ("SCHEDULE_CAPACITY_EXCEEDED", 409, "state", False, "client"),
    (
        "SCHEDULE_EXECUTION_UNAVAILABLE",
        503,
        "persistence",
        True,
        "platform",
    ),
    ("UNAUTHORIZED", 401, "auth", False, "client"),
    # Raised where a browser surface needs a sign-in that has not happened, so
    # the console can redirect instead of rendering a refusal. Same shape as the
    # row above: the caller supplies a credential, nothing on the platform is
    # broken, and sending the identical request again changes nothing.
    ("AUTH_REQUIRED", 401, "auth", False, "client"),
    # Not retryable, by this table's meaning of the word: re-sending the same
    # request cannot succeed, because the credential in it is the thing that
    # expired. The client must obtain a different token first, which the code
    # itself is what tells it to do.
    ("TOKEN_EXPIRED", 401, "auth", False, "client"),
    ("FORBIDDEN", 403, "auth", False, "client"),
    # The last row of the auth family that is not the caller's to fix. The token
    # is good and the provider answered, so a 4xx would send the caller to renew
    # a credential that is already valid; 502 says an upstream answer was not
    # usable, and unlike the 503 next to it the answer will not become usable by
    # waiting — someone edits the provider's scope or claim mapping, which is
    # this deployment's configuration and so `platform`.
    ("IDENTITY_PROVIDER_MISCONFIGURED", 502, "auth", False, "platform"),
    # An MCP client key that is valid and too narrow. 403 rather than 401
    # because the credential is not the problem — a 401 sends the holder to
    # replace something that works. `client` because issuing a wider key is
    # theirs to do, and not retryable because the same key carries the same
    # scope however many times it is sent.
    ("MCP_TOKEN_SCOPE_INSUFFICIENT", 403, "auth", False, "client"),
    ("API_TOKEN_SCOPE_INSUFFICIENT", 403, "auth", False, "client"),
    ("NOT_FOUND", 404, "request", False, "client"),
    # A token id that is not the caller's answers the same as one that never
    # existed. 403 would confirm it exists and belongs to somebody.
    ("MCP_TOKEN_NOT_FOUND", 404, "request", False, "client"),
    ("SESSION_NOT_FOUND", 404, "request", False, "session"),
    ("SESSION_BUSY", 409, "state", True, "session"),
    # Same class of 409 as the row above — the resource's current state refuses
    # the operation — so it carries the same category rather than an auth one:
    # the caller is permitted, the Vault is simply still held. Not retryable,
    # because the caller has to unbind those holders first, and it owns that
    # action, which is what `client` records.
    ("VAULT_IN_USE", 409, "state", False, "client"),
    # Another sandbox holds the workspace's write claim. Same class of 409 as
    # Not retryable, unlike its 409 neighbour: re-sending the same append_id
    # with the same entries is the idempotent path and never reaches this code,
    # so a caller that sees it has sent two different batches under one id and
    # must change the request, not repeat it.
    ("APPEND_ID_CONFLICT", 409, "request", False, "client"),
    ("RUNTIME_STATE_CONFLICT", 409, "state", False, "runtime"),
    # Conversation create is the other externally supplied idempotency token.
    # Invalid syntax and a token whose durable identity is retired
    # both require the caller to mint a different key; replaying either request
    # unchanged cannot succeed. A binding conflict is likewise a request
    # identity collision, not a transient runtime failure.
    ("INVALID_IDEMPOTENCY_KEY", 400, "request", False, "client"),
    ("IDEMPOTENCY_KEY_CONFLICT", 409, "request", False, "client"),
    ("IDEMPOTENCY_KEY_RETIRED", 409, "state", False, "client"),
    ("REQUEST_CANCELLED", 499, "request", False, "client"),
    ("E2E_TRANSCRIPT_APPEND_FAULT", 503, "persistence", True, "platform"),
    ("PERSISTENCE_UNAVAILABLE", 503, "persistence", True, "mongo"),
    ("PERSISTENCE_OPERATION_FAILED", 500, "persistence", False, "mongo"),
    ("PERSISTENCE_ERROR", 500, "persistence", False, "mongo"),
    # Optional engine protocols are requested by the caller. An engine that
    # does not declare one is healthy; the requested operation is unavailable.
    ("ENGINE_CAPABILITY_UNAVAILABLE", 409, "request", False, "client"),
    # A malformed manifest is an adapter integration defect, not a turn or
    # sandbox failure, and retrying the same deployed code cannot repair it.
    ("ENGINE_CAPABILITY_CONTRACT_VIOLATION", 502, "runtime.engine", False, "platform"),
    # Child-run projection defects come from adapter facts or their durable
    # read model. The caller cannot repair them by changing the request.
    ("CHILD_RUN_PROJECTION_INVALID", 409, "runtime.engine", False, "platform"),
    ("CHILD_RUN_NOT_FOUND", 404, "request", False, "session"),
    ("CHILD_RUN_ALREADY_TERMINAL", 409, "state", False, "session"),
    ("CHILD_RUN_CONTROL_UNAVAILABLE", 409, "state", False, "session"),
    # An engine with a permission-mode vocabulary but no product default needs
    # the caller to choose one explicitly.
    ("ENGINE_PERMISSION_MODE_REQUIRED", 400, "request", False, "client"),
    # An unpinned Environment follows its adapter only when that adapter has a
    # real image default. Replaying cannot invent one; the Environment owner
    # must select an image (or install an adapter that declares its default).
    (
        "ENGINE_RUNTIME_IMAGE_REQUIRED",
        409,
        "configuration.environment",
        False,
        "template",
    ),
    # The session currently has no engine client on which a control operation
    # can run. Reattachment can make the identical operation valid later.
    ("ENGINE_RUNTIME_UNAVAILABLE", 409, "state", True, "session"),
    # A runtime published before binding its durable engine conversation is a
    # platform ordering defect; the republished runtime repeats it on retry.
    ("ENGINE_CONVERSATION_NOT_BOUND", 500, "runtime.preparation", False, "platform"),
    ("ENGINE_ATTACH_MODE_INVALID", 500, "runtime.startup", False, "platform"),
    # An enabled Assistant Environment without an engine identity is malformed
    # deployment configuration. Repeating the request cannot repair its record.
    ("ASSISTANT_ENVIRONMENT_INVALID", 500, "configuration.environment", False, "template"),
    ("ASSISTANT_RUNTIME_MANAGER_UNAVAILABLE", 500, "state", False, "platform"),
    ("ASSISTANT_LIFECYCLE_UNAVAILABLE", 500, "state", False, "platform"),
    # A workspace disappearing while its Assistant still exists violates the
    # durable-owner invariant. A confirmed-dead pointer that remains after the
    # exact-id convergence fence is a transient state conflict worth retrying.
    ("ASSISTANT_WORKSPACE_NOT_FOUND", 500, "state", False, "platform"),
    ("ASSISTANT_WORKSPACE_CONVERGENCE_FAILED", 503, "state", True, "platform"),
    # Hibernation is a storage transaction followed by a fenced sandbox
    # release. Malformed transition input is a platform invariant.
    ("ASSISTANT_WORKSPACE_HIBERNATE_CONFLICT", 409, "state", True, "platform"),
    (
        "ASSISTANT_WORKSPACE_INVALID_HIBERNATE_MARK",
        500,
        "state",
        False,
        "platform",
    ),
    # Conversation startup and terminal routing may forward this code from a
    # typed binding resolution rather than constructing it at the raise site.
    ("ASSISTANT_WORKSPACE_NOT_READY", 409, "state", True, "runtime"),
    # RuntimeSubject is the common Session-startup control plane. Invalid or
    # changing authority is a platform invariant; a timed-out owner startup is
    # a retryable runtime condition.
    (
        "ASSISTANT_WORKSPACE_MATERIALIZER_IDENTITY_MISMATCH",
        500,
        "state",
        False,
        "platform",
    ),
    ("RUNTIME_SUBJECT_INVALID", 500, "state", False, "platform"),
    ("RUNTIME_SUBJECT_UNAVAILABLE", 500, "state", False, "platform"),
    ("RUNTIME_SUBJECT_CHANGED", 409, "state", False, "platform"),
    ("RUNTIME_RECOVERY_SUPERSEDED", 409, "runtime.startup", False, "platform"),
    (
        "RUNTIME_SUBJECT_MATERIALIZATION_LOST",
        409,
        "state",
        False,
        "platform",
    ),
    ("RUNTIME_SUBJECT_STARTUP_TIMEOUT", 504, "runtime.startup", True, "runtime"),
    # Durable create recovery is mandatory for every configured sandbox
    # backend. Repeating the Session against the same provider cannot add that
    # capability; the deployment must select or repair its provider adapter.
    (
        "SANDBOX_CORRELATED_CREATE_UNSUPPORTED",
        501,
        "runtime.startup",
        False,
        "platform",
    ),
    # Assignment identity is platform-authored startup state. A missing value
    # is an AstraBox invariant failure; contradictory or duplicate provider
    # resources belong to the sandbox runtime that reports them.
    ("SANDBOX_ASSIGNMENT_INVALID", 500, "runtime.startup", False, "platform"),
    ("SANDBOX_ASSIGNMENT_CONFLICT", 409, "runtime.startup", False, "runtime"),
    ("SANDBOX_ASSIGNMENT_AMBIGUOUS", 409, "runtime.startup", False, "runtime"),
    (
        "SANDBOX_CLIENT_POOL_UNSUPPORTED",
        501,
        "runtime.preparation",
        False,
        "platform",
    ),
    (
        "SANDBOX_CLIENT_POOL_UNAVAILABLE",
        503,
        "runtime.preparation",
        True,
        "runtime",
    ),
    ("SANDBOX_CLEANUP_UNCONFIRMED", 502, "runtime.sandbox", True, "runtime"),
    ("AGENT_RUNTIME_GENERATION_CONFLICT", 409, "state", True, "platform"),
    # The platform wrote this durable record and cannot safely guess which
    # sandbox resource it names when the stored shape is invalid.
    ("STARTUP_ALLOCATION_INVALID", 500, "runtime.startup", False, "platform"),
    # Runtime identity is written by AstraBox and is required to resolve the
    # workspace safely. A missing or contradictory value is a platform-state
    # invariant failure, not a request the caller can correct or retry.
    ("SESSION_RUNTIME_IDENTITY_INVALID", 500, "state", False, "platform"),
    # A provider resolved a server outside the destination scope it declared
    # for its own credential. Repeating a Session cannot change that provider
    # contract; its template or adapter must be corrected.
    (
        "AGENT_EXTENSION_PROVIDER_INVALID",
        500,
        "runtime.extension",
        False,
        "template",
    ),
    ("AGENT_RUNTIME_ERROR", 502, "runtime", True, "runtime"),
    (
        "SANDBOX_EGRESS_MUTATION_UNSUPPORTED",
        501,
        "runtime.egress",
        False,
        "runtime",
    ),
    ("SIDECAR_ATTACH_IDENTITY_INVALID", 409, "runtime.attach", True, "runtime"),
    ("SIDECAR_ATTACH_IDENTITY_MISMATCH", 409, "runtime.attach", True, "runtime"),
    ("TURN_PREPARATION_FAILED", 502, "runtime.preparation", True, "runtime"),
    # Starting, reattaching, or adopting the Assistant profile's resident
    # Hermes gateway failed. The same start is valid once the sandbox answers.
    ("HERMES_GATEWAY_START_FAILED", 502, "runtime.preparation", True, "runtime"),
    ("CONVERSATION_BOOTSTRAP_FAILED", 502, "runtime.bootstrap", True, "runtime"),
    ("CONVERSATION_BOOTSTRAP_API_UNAVAILABLE", 502, "runtime.bootstrap", True, "runtime"),
    ("DEFAULT_REPO_CLONE_FAILED", 502, "runtime.default_repo", False, "runtime"),
    ("DEFAULT_REPO_INVALID", 500, "runtime.default_repo", False, "template"),
    ("DEFAULT_REPO_INVALID_KEY", 500, "runtime.default_repo", False, "template"),
    ("DEFAULT_REPO_MISSING_KEY", 500, "runtime.default_repo", False, "template"),
    ("DEFAULT_REPO_UNSUPPORTED_PROTOCOL", 500, "runtime.default_repo", False, "template"),
    # A malformed plugin-repository value belongs to the Agent definition, not
    # the conversation that first tries to use it. Re-sending the same request
    # cannot change that definition, so these failures are not retryable.
    ("PLUGIN_REPO_INVALID", 500, "runtime.plugin_repo", False, "template"),
    ("PLUGIN_REPO_UNSUPPORTED_PROTOCOL", 500, "runtime.plugin_repo", False, "template"),
    # This code is the in-box read of a plugin's MCP declaration: an unreadable
    # cache marker, an absent command runner, a scan that failed or answered
    # with something other than documents. The row beneath it is the separate
    # question of whether the deployment configured a proxy for the bridge to
    # reach at all, which the operator answers and the sandbox cannot. Splitting
    # them is what lets an `owner` be true of every site that raises a code.
    # The Environment administrator chose whether limited networking admits
    # Agent-declared MCP destinations. Repeating the same Agent/Environment
    # pair cannot change that policy.
    (
        "AGENT_MCP_NETWORK_ACCESS_DISABLED",
        409,
        "configuration.environment",
        False,
        "template",
    ),
    # A deployment configured a storage medium that cannot hold an exclusive
    # write claim for a workspace that needs one. Nobody's request is wrong, so
    # no 4xx: an operator changes the provider or the medium. Raised where the
    # product action creates the thing that will need arbitration, not at the
    # first write — by then a user has spent a conversation on it.
    ("NAS_MOUNT_FAILED", 500, "runtime.storage", True, "runtime"),
    ("UNEXPECTED_SERVER_ERROR", 500, "internal", False, "platform"),
):
    register_error(
        _code,
        status_code=_status,
        category=_category,
        retryable=_retryable,
        owner=_owner,
    )


register_error(
    "AGENT_ENVIRONMENT_DISABLED",
    status_code=409,
    category="configuration",
    owner="platform",
)
register_error(
    "SANDBOX_CAPACITY_UNAVAILABLE",
    status_code=503,
    category="runtime.sandbox",
    retryable=True,
    owner="platform",
    user_message=(
        "This sandbox has no room to place another conversation; retry, or "
        "give the Agent a box with more capacity."
    ),
)
register_error(
    "AGENT_PREWARM_DISCARD_UNCONFIRMED",
    status_code=502,
    category="runtime.prewarm",
    retryable=True,
    owner="runtime",
    user_message=(
        "Prepared runtime cleanup could not be confirmed; retry after the "
        "sandbox control plane is reachable."
    ),
)
register_error(
    "AGENT_PREWARM_SLOT_CONFLICT",
    status_code=409,
    category="runtime.prewarm",
    retryable=True,
    owner="platform",
    user_message=(
        "Another refill published this Agent's prepared slot first; the loser "
        "was discarded and a retry finds the published slot."
    ),
)


# These four rows deliberately do not form one cleanup family. Reading the
# durable cleanup anchor is a record-store failure; stopping an engine or PTY is
# a runtime failure. A sandbox destroy that returned no answer can be retried as
# sent, while a refused destroy needs its ownership/backend evidence repaired
# before the same DELETE is useful.
register_error(
    "SESSION_CLEANUP_STATE_READ_FAILED",
    status_code=503,
    category="persistence",
    retryable=True,
    owner="mongo",
    user_message="Session cleanup state is temporarily unavailable; retry the operation.",
)
register_error(
    "SESSION_PROCESS_CLEANUP_FAILED",
    status_code=502,
    category="runtime.cleanup",
    retryable=True,
    owner="runtime",
    user_message=(
        "Session processes could not be stopped; retry after checking the sandbox runtime."
    ),
)
register_error(
    "SESSION_SANDBOX_DESTRUCTION_UNCONFIRMED",
    status_code=502,
    category="runtime.cleanup",
    retryable=True,
    owner="runtime",
    user_message=(
        "Sandbox destruction could not be confirmed; retry session deletion."
    ),
)
register_error(
    "SESSION_SANDBOX_DESTRUCTION_REFUSED",
    status_code=502,
    category="runtime.cleanup",
    retryable=False,
    owner="runtime",
    user_message=(
        "Sandbox destruction was refused; resolve sandbox ownership or backend "
        "metadata before retrying session deletion."
    ),
)
