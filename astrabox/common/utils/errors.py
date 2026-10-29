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
        "store",  # the record store
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
    ("SPEECH_INPUT_NOT_CONFIGURED", 503, "configuration", False, "platform"),
    ("SPEECH_INPUT_INVALID_AUDIO", 415, "request", False, "client"),
    ("SPEECH_INPUT_TOO_LARGE", 413, "request", False, "client"),
    ("SPEECH_INPUT_FAILED", 502, "model", True, "platform"),
    ("SPEECH_INPUT_TIMEOUT", 504, "model", True, "platform"),
    ("CHANNEL_PROVIDER_UNKNOWN", 400, "request", False, "client"),
    ("CHANNEL_CONFIG_INVALID", 400, "request", False, "client"),
    ("CHANNEL_CREDENTIALS_INVALID", 400, "request", False, "client"),
    ("CHANNEL_UNAUTHORIZED", 401, "auth", False, "client"),
    ("CHANNEL_BAD_PAYLOAD", 400, "request", False, "client"),
    ("CHANNEL_CALLBACK_NOT_FOUND", 404, "request", False, "client"),
    ("CHANNEL_CALLBACK_PATH_REQUIRED", 404, "request", False, "client"),
    ("CHANNEL_REFERENCE_OWNER_MISMATCH", 409, "state", False, "client"),
    ("CHANNEL_SESSION_OWNER_MISMATCH", 409, "state", False, "client"),
    # A trigger that runs as linked accounts received a message whose
    # provider named no verified sender; the provider integration fixes it.
    ("CHANNEL_PARTICIPANT_REQUIRED", 409, "state", False, "platform"),
    # The sender has no linked account on the trigger; an administrator
    # links it on the trigger's page.
    ("CHANNEL_PARTICIPANT_NOT_LINKED", 403, "auth", False, "client"),
    # The sender's linked account may not use the Agent; the Agent's
    # administrators widen its access or the link is changed.
    ("CHANNEL_ACCOUNT_NOT_AUTHORIZED", 403, "auth", False, "client"),
    ("NOT_A_CHANNEL", 409, "state", False, "client"),
    ("CONNECTOR_NOT_FOUND", 404, "request", False, "client"),
    # The connector is not registered with the hub or its tools are not
    # published; an administrator sets it up under Connectors.
    ("CONNECTOR_NOT_AVAILABLE", 409, "configuration", False, "platform"),
    # The caller asked for an app they have not connected to be offered as
    # connected; they connect it first.
    ("CONNECTOR_NOT_CONNECTED", 409, "request", False, "client"),
    # The deployment runs no server for a connector the vendor serves none of
    # (Google's apps); the operator deploys it and sets its URL variable.
    ("CONNECTOR_SERVER_NOT_DEPLOYED", 409, "configuration", False, "platform"),
    # No connector hub, or a partly configured one (raised 500).
    ("CONNECTOR_HUB_NOT_CONFIGURED", 409, "configuration", False, "platform"),
    # The hub was unreachable (503) or refused a call (502); the message
    # carries its answer.
    ("CONNECTOR_HUB_ERROR", 502, "runtime", True, "platform"),
    # The hub's public address is plain HTTP at a non-loopback host, which no
    # vendor accepts as a redirect URI; serving it over HTTPS resolves it.
    ("CONNECTOR_REQUIRES_HTTPS", 409, "configuration", False, "platform"),
    # A vendor's MCP server or authorization server failed discovery; the
    # raise site gives the status (400 for a bad response, 502 for an
    # unreachable or failing server).
    ("CONNECTOR_AUTHORIZATION_FAILED", 502, "auth", True, "platform"),
    # An ASGI host mounted AstraBox at a path other than
    # ASTRABOX_PUBLIC_PATH_PREFIX; the operator sets the variable to the mount
    # path (astrabox/web/public_path_middleware.py).
    ("PUBLIC_PATH_PREFIX_MISMATCH", 409, "configuration", False, "platform"),
    # A reverse proxy forwarded the public path prefix instead of removing it;
    # the operator changes the proxy. Raised 404: AstraBox serves nothing at
    # the prefixed path.
    ("PUBLIC_PATH_PREFIX_NOT_REMOVED", 404, "configuration", False, "platform"),
    # A page AstraBox proxies from another application that cannot be served
    # below the public path prefix, such as LiteLLM's management console; the
    # operator opens that application at its own address.
    ("PUBLIC_PATH_PREFIX_UNSUPPORTED", 409, "configuration", False, "platform"),
    ("DEPLOYMENT_UNAUTHORIZED", 401, "auth", False, "client"),
    ("DEPLOYMENT_MISCONFIGURED", 409, "state", False, "platform"),
    ("DEPLOYMENT_NO_OWNER", 409, "state", False, "platform"),
    ("ENVIRONMENT_MISSING", 500, "state", False, "platform"),
    ("ENVIRONMENT_MISMATCH", 409, "state", False, "client"),
    ("ENVIRONMENT_NOT_FOUND", 404, "request", False, "client"),
    ("ENVIRONMENT_ACCESS_DENIED", 403, "authorization", False, "client"),
    ("ENVIRONMENT_ENTRY_POLICY_UNAVAILABLE", 503, "authorization", True, "platform"),
    ("ENVIRONMENT_VERSION_CONFLICT", 409, "state", True, "client"),
    ("AGENT_PREWARM_DISABLED", 409, "configuration", False, "platform"),
    # A RuntimeProfile naming a permission level the platform does not define.
    # `template` because the value is stored on the RuntimeProfile, which is what
    # a session's runtime is rendered from — the same owner as the other
    # unsupported-value refusals read off a stored definition. Not retryable:
    # the same RuntimeProfile produces the same refusal until someone edits it.
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
    # An identity without the administrator role on the admin surface. The role
    # comes from the identity provider's groups (or, for the configured API
    # client, from its astrabox:admin scope), so the remedy is an administrator
    # granting it there; the same credential is refused every time.
    ("ADMIN_ROLE_REQUIRED", 403, "auth", False, "client"),
    # The last row of the auth family that is not the caller's to fix. The token
    # is good and the provider answered, so a 4xx would send the caller to renew
    # a credential that is already valid; 502 says an upstream answer was not
    # usable, and unlike the 503 next to it the answer will not become usable by
    # waiting — someone edits the provider's scope or claim mapping, which is
    # this deployment's configuration and so `platform`.
    ("IDENTITY_PROVIDER_MISCONFIGURED", 502, "auth", False, "platform"),
    # The provider vouched for the account, and the account is not one this
    # deployment accepts: a Casdoor user of another organization, such as
    # Casdoor's own `built-in` administrators, or a client token from another
    # application. 403 rather than 401, because a fresh token for the same
    # account is refused the same way; `client` because the remedy is to sign
    # in with an account of the configured organization.
    ("IDENTITY_ORGANIZATION_REJECTED", 403, "auth", False, "client"),
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
    # The session's terminal keeps only its latest command, so an older
    # command id answers this once a newer command has started.
    ("TERMINAL_COMMAND_NOT_FOUND", 404, "request", False, "session"),
    ("SESSION_BUSY", 409, "state", True, "session"),
    ("SESSION_NOT_READY", 409, "state", True, "session"),
    ("INTERACTION_NOT_FOUND", 404, "request", False, "session"),
    # Answered, withdrawn or expired: answering again cannot succeed.
    ("INTERACTION_NOT_OPEN", 409, "state", False, "session"),
    # A message whose turn ended before the message reached the Agent program:
    # its delivery could not attach a runtime, or the turn was settled before
    # delivery. The turn is over, so the same message sent again under its
    # client_message_id answers this again; a new message starts a new turn.
    # Recorded on an input the platform still held when its conversation ended.
    ("CONVERSATION_ENDED", 409, "state", False, "session"),
    # A message whose replacement runtime failed to start; sending it again
    # starts another attempt.
    ("RUNTIME_START_FAILED", 502, "runtime", False, "runtime"),
    # Same class of 409 as the row above — the resource's current state refuses
    # the operation — so it carries the same category rather than an auth one:
    # the caller is permitted, the Vault is simply still held. Not retryable,
    # because the caller has to unbind those holders first, and it owns that
    # action, which is what `client` records.
    ("VAULT_IN_USE", 409, "state", False, "client"),
    # The request names a Credential Vault that does not exist, or that was
    # deleted while the binding naming it was saved.
    ("VAULT_NOT_FOUND", 404, "request", False, "client"),
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
    ("E2E_TRANSCRIPT_APPEND_FAULT", 503, "persistence", True, "platform"),
    ("PERSISTENCE_UNAVAILABLE", 503, "persistence", True, "store"),
    ("PERSISTENCE_ERROR", 500, "persistence", False, "store"),
    ("ARTIFACT_SIGN_IN_REQUIRED", 401, "request", False, "client"),
    ("ARTIFACT_SHARE_AUDIENCE_RESTRICTED", 409, "request", False, "client"),
    ("ARTIFACT_AGENT_RUN_LIMITED", 429, "request", False, "client"),
    ("ARTIFACT_AGENT_RUN_NOT_FOUND", 404, "request", False, "client"),
    ("ARTIFACT_AGENT_UNAVAILABLE", 503, "runtime", False, "runtime"),
    ("ARTIFACT_AGENT_DENIED", 403, "request", False, "client"),
    ("ARTIFACT_SAMPLE_DENIED", 403, "request", False, "client"),
    ("ARTIFACT_ROOM_DENIED", 403, "request", False, "client"),
    ("ARTIFACT_DATA_NOT_FOUND", 404, "request", False, "client"),
    ("MODEL_ENDPOINT_INVALID", 500, "configuration.model", False, "platform"),
    ("MCP_BINDING_NOT_FOUND", 404, "request", False, "client"),
    ("MCP_PROXY_NOT_CONFIGURED", 503, "configuration.runtime_profile", False, "platform"),
    ("AGENT_COORDINATION_UNAVAILABLE", 503, "configuration.runtime_profile", False, "platform"),
    ("ARTIFACT_NOT_FOUND", 404, "request", False, "client"),
    ("ARTIFACT_VERSION_NOT_FOUND", 404, "request", False, "client"),
    ("ARTIFACT_VERSION_CONFLICT", 409, "request", False, "client"),
    ("ARTIFACT_CONTENT_TOO_LARGE", 413, "request", False, "client"),
    ("ARTIFACT_VERSION_LIMIT", 409, "request", False, "client"),
    ("ARTIFACT_KIND_INVALID", 400, "request", False, "client"),
    ("ARTIFACT_CAPABILITY_UNKNOWN", 400, "request", False, "client"),
    ("ARTIFACT_MANIFEST_INVALID", 400, "request", False, "client"),
    ("ARTIFACT_EDIT_NOT_APPLICABLE", 400, "request", False, "client"),
    ("ARTIFACT_CAPABILITY_NOT_DECLARED", 403, "request", False, "client"),
    ("ARTIFACT_NOT_WRITER", 403, "request", False, "client"),
    ("ARTIFACT_DATA_DENIED", 403, "request", False, "client"),
    ("ARTIFACT_DATA_COUNT_LIMIT", 413, "request", False, "client"),
    ("ARTIFACT_DATA_LIMIT", 413, "request", False, "client"),
    ("ARTIFACT_SHARE_NOT_FOUND", 404, "request", False, "client"),
    ("ARTIFACT_SAMPLE_UNAVAILABLE", 503, "runtime", False, "runtime"),
    ("ARTIFACT_SAMPLE_TOO_LARGE", 413, "request", False, "client"),
    ("ARTIFACT_SAMPLE_TOKEN_LIMIT", 400, "request", False, "client"),
    ("ARTIFACT_SAMPLE_RATE_LIMITED", 429, "request", False, "client"),
    ("ARTIFACT_LIVE_RATE_LIMITED", 429, "request", False, "client"),
    ("ARTIFACT_ROOM_LIMIT", 413, "request", False, "client"),
    ("ARTIFACT_TITLE_LIMIT", 413, "request", False, "client"),
    ("ARTIFACT_STORE_UNAVAILABLE", 503, "persistence", False, "platform"),
    ("RUNTIME_PROFILE_NOT_FOUND", 404, "request", False, "client"),
    ("AGENT_RUNTIME_PROFILE_REQUIRED", 500, "configuration.runtime_profile", False, "platform"),
    ("AGENT_RUNTIME_PROFILE_MISSING", 500, "configuration.runtime_profile", False, "platform"),
    ("ASSISTANT_RUNTIME_PROFILE_MISSING", 403, "configuration.runtime_profile", False, "template"),
    # No RuntimeProfile for Assistants yet: an administrator sets one in the
    # Assistant defaults, so the user's retry cannot change the answer.
    ("ASSISTANT_DEFAULTS_UNAVAILABLE", 409, "configuration.runtime_profile", False, "template"),
    ("RUNTIME_PROFILE_IN_USE", 409, "configuration.runtime_profile", False, "template"),
    # The row names a stored credential that is absent from the secret store.
    ("RUNTIME_PROFILE_SECRET_MISSING", 500, "configuration.runtime_profile", False, "platform"),
    ("RUNTIME_PROFILE_SECRET_STORE_NOT_TRANSACTIONAL", 500, "configuration.runtime_profile", False, "platform"),
    # Optional engine protocols are requested by the caller. An engine that
    # does not declare one is healthy; the requested operation is unavailable.
    ("ENGINE_CAPABILITY_UNAVAILABLE", 409, "request", False, "client"),
    # An input whose delivery frame exceeds the runner link's message limit;
    # sending it again fails the same way.
    ("RUNTIME_INPUT_TOO_LARGE", 413, "request", False, "client"),
    # A malformed manifest is an adapter integration defect, not a turn or
    # sandbox failure, and retrying the same deployed code cannot repair it.
    ("ENGINE_CAPABILITY_CONTRACT_VIOLATION", 502, "runtime.engine", False, "platform"),
    # Child-run projection defects come from adapter facts or their durable
    # read model. The caller cannot repair them by changing the request.
    ("CHILD_RUN_PROJECTION_INVALID", 409, "runtime.engine", False, "platform"),
    ("CHILD_RUN_NOT_FOUND", 404, "request", False, "session"),
    ("CHILD_RUN_ALREADY_TERMINAL", 409, "state", False, "session"),
    ("CHILD_RUN_CONTROL_UNAVAILABLE", 409, "state", False, "session"),
    # A running response whose runtime has no connected reader; retrying once
    # the reader reconnects can succeed.
    ("INTERRUPT_UNAVAILABLE", 409, "state", True, "session"),
    ("PENDING_INPUTS_NOT_CANCELLED", 409, "state", False, "session"),
    # The conversation's Agent was deleted; its history stays readable.
    ("AGENT_DELETED", 409, "state", False, "session"),
    ("SESSION_READ_ONLY", 409, "state", False, "session"),
    # An engine with a permission-mode vocabulary but no product default needs
    # the caller to choose one explicitly.
    ("ENGINE_PERMISSION_MODE_REQUIRED", 400, "request", False, "client"),
    # An unpinned RuntimeProfile follows its adapter only when that adapter has a
    # real image default. Replaying cannot invent one; the RuntimeProfile owner
    # must select an image (or install an adapter that declares its default).
    (
        "ENGINE_RUNTIME_IMAGE_REQUIRED",
        409,
        "configuration.runtime_profile",
        False,
        "template",
    ),
    # The session currently has no engine client on which a control operation
    # can run. Reattachment can make the identical operation valid later.
    ("ENGINE_RUNTIME_UNAVAILABLE", 409, "state", True, "session"),
    # A runtime published before binding its durable engine conversation is a
    # platform ordering defect; the republished runtime repeats it on retry.
    ("ENGINE_ATTACH_MODE_INVALID", 500, "runtime.startup", False, "platform"),
    # The model provider mapped an engine's native protocol outside the gateway
    # base URL this deployment configured. Only that configuration can fix it.
    ("MODEL_ENDPOINT_INVALID", 500, "configuration.model", False, "platform"),
    # An enabled Assistant RuntimeProfile without an engine identity is malformed
    # deployment configuration. Repeating the request cannot repair its record.
    ("ASSISTANT_RUNTIME_PROFILE_INVALID", 500, "configuration.runtime_profile", False, "template"),
    ("ASSISTANT_RUNTIME_MANAGER_UNAVAILABLE", 500, "state", False, "platform"),
    ("ASSISTANT_LIFECYCLE_UNAVAILABLE", 500, "state", False, "platform"),
    # A workspace disappearing while its Assistant still exists violates the
    # durable-owner invariant. A confirmed-dead pointer that remains after the
    # exact-id convergence fence is a transient state conflict worth retrying.
    ("ASSISTANT_WORKSPACE_NOT_FOUND", 500, "state", False, "platform"),
    ("ASSISTANT_WORKSPACE_CONVERGENCE_FAILED", 503, "state", True, "platform"),
    # The running workspace did not answer its program's job store; asking
    # again after the box settles can succeed.
    ("ASSISTANT_SCHEDULED_JOBS_UNAVAILABLE", 503, "state", True, "platform"),
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
    ("SESSION_RUNTIME_LOCK_LOST", 502, "state", False, "platform"),
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
    # A record names an installed sandbox backend the deployment did not enable
    # (ASTRABOX_SANDBOX_BACKENDS). Retrying cannot help; the operator enables
    # the backend or retires the records that name it.
    ("SANDBOX_BACKEND_DISABLED", 409, "state", False, "platform"),
    (
        "SANDBOX_CLIENT_POOL_UNSUPPORTED",
        501,
        "runtime.preparation",
        False,
        "platform",
    ),
    # A feature reached the long-lived processes of a backend that has none
    # (SandboxProvider.supports_process_sessions). Retrying cannot add them;
    # the deployment selects a backend that has them for that feature.
    (
        "SANDBOX_PROCESS_SESSIONS_UNSUPPORTED",
        501,
        "runtime.sandbox",
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
    # The sandbox a conversation is bound to does not exist any more: the
    # control plane does not know it, it is not running, its network wiring is
    # stale, or it does not answer. The runtime owns it. Not retryable, because
    # the same request sent again does not succeed on any path that raises it.
    # A message refused with it (409) was already accepted, and sending it
    # again under the same client_message_id replays that command instead of
    # starting a turn. A file operation or engine control that meets the dead
    # box (404) keeps meeting it. A new message is what moves the conversation
    # to a new sandbox: its admission replaces the box.
    ("SANDBOX_GONE", 404, "runtime.sandbox", False, "runtime"),
    ("BROWSER_TENANCY_UNSUPPORTED", 409, "runtime.sandbox", False, "runtime"),
    ("BROWSER_UNAVAILABLE", 502, "runtime.sandbox", False, "runtime"),
    ("FILE_PREVIEW_UNAVAILABLE", 502, "runtime.sandbox", False, "runtime"),
    # A transport failure to the box: it proves nothing about whether the box
    # exists, and the same request may succeed once the box answers again.
    ("SANDBOX_UNREACHABLE", 503, "runtime.sandbox", True, "runtime"),
    # The box answered a probe in a form its provider cannot read; repeating
    # the request reads the same answer.
    ("SANDBOX_PROBE_FAILED", 502, "runtime.sandbox", False, "runtime"),
    # The selected backend cannot create a box with the deployment's resource
    # envelope (ASTRABOX_SANDBOX_*_LIMIT / *_REQUEST), and refused before
    # allocating. Retrying cannot help; the operator selects a shape it offers.
    ("SANDBOX_RESOURCES_UNSUPPORTED", 409, "configuration", False, "platform"),
    # A box would take its backend past a sandbox quota ceiling
    # (ASTRABOX_SANDBOX_QUOTA_*). The same request succeeds once boxes are
    # released, so it is retryable; the message names the ceiling, its use and
    # its limit.
    ("SANDBOX_CAPACITY_EXHAUSTED", 429, "runtime.sandbox", True, "platform"),
    # An Agent category id names no category, or a new name is one another
    # category already has (names are compared without regard to case).
    ("AGENT_CATEGORY_NOT_FOUND", 404, "request", False, "client"),
    ("AGENT_CATEGORY_NAME_TAKEN", 409, "request", False, "client"),
    # A conversation start was re-executed after a platform restart on a
    # sandbox backend that cannot look a box up by its assignment, so whether
    # the earlier execution made a box is unknown. The same command is never
    # retried; starting the conversation again is a new command.
    ("SANDBOX_CREATE_INTERRUPTED", 409, "runtime.startup", False, "platform"),
    # Port 8080 is the sandbox's control API, not an application preview.
    ("EXPOSE_PORT_RESERVED", 400, "request", False, "client"),
    ("AGENT_RUNTIME_GENERATION_CONFLICT", 409, "state", True, "platform"),
    ("ASSISTANT_REVISION_CONFLICT", 409, "state", True, "platform"),
    # Another write changed the record between this request's read and its
    # compare-and-set, so nothing was written. The same request computes its
    # change again from the record as it now is.
    ("ASSISTANT_VERSION_CONFLICT", 409, "state", True, "client"),
    ("ASSISTANT_DEFAULTS_VERSION_CONFLICT", 409, "state", True, "client"),
    ("RESOURCE_SYNC_BUSY", 409, "state", True, "platform"),
    ("RESOURCE_SYNC_UNRESOLVED", 409, "state", False, "platform"),
    ("RESOURCE_SYNC_FAILED", 502, "runtime", False, "platform"),
    # Replaying the same guard cannot make its captured subject state current.
    # The caller must resolve the conflict before issuing another guarded write.
    ("PREPARATION_SUBJECT_CONFLICT", 409, "state", False, "platform"),
    ("RUNTIME_PREPARATION_FAILED", 502, "runtime", False, "platform"),
    ("CHANNEL_RESPONSE_JUDGE_UNAVAILABLE", 409, "configuration", False, "platform"),
    ("HISTORY_IMPORT_AFTER_CONVERSATION", 409, "state", False, "session"),
    ("HISTORY_IMPORT_CONFLICT", 409, "request", False, "client"),
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
    # The deployment runs no extension catalog (ASTRABOX_EXTENSION_PROVIDER=none):
    # a catalog read, a non-empty catalog selection, or a record still holding
    # one when it starts work. Retrying cannot help; someone removes the
    # selection or the operator selects a provider.
    ("EXTENSION_CATALOG_DISABLED", 409, "configuration", False, "platform"),
    # The selected extension provider declares no management console, so there
    # is none to open; the console offers the entry only when one is declared.
    ("EXTENSION_CONSOLE_UNSUPPORTED", 409, "configuration", False, "platform"),
    ("AGENT_RUNTIME_ERROR", 502, "runtime", True, "runtime"),
    ("RUNTIME_PROTOCOL_INCOMPATIBLE", 502, "runtime", False, "platform"),
    (
        "SANDBOX_EGRESS_MUTATION_UNSUPPORTED",
        501,
        "runtime.egress",
        False,
        "runtime",
    ),
    ("SANDBOX_ISOLATED_SESSION_NOT_FOUND", 409, "runtime.attach", False, "runtime"),
    ("SESSION_USER_ACTION_FAILED", 502, "runtime.preparation", False, "platform"),
    ("TURN_PREPARATION_FAILED", 502, "runtime.preparation", True, "runtime"),
    # Starting, reattaching, or adopting the Assistant profile's resident
    # Hermes gateway failed. The same start is valid once the sandbox answers.
    ("HERMES_GATEWAY_START_FAILED", 502, "runtime.preparation", True, "runtime"),
    ("HERMES_BACKEND_NOT_READY", 503, "runtime.preparation", True, "runtime"),
    ("RUNTIME_STATE_SAVE_REQUIRED", 503, "runtime.preparation", True, "runtime"),
    # Hermes declined one message at prompt.submit: its active-session limit,
    # session storage it could not write, or a voice-mode stop phrase. The
    # identical message goes through once Hermes takes messages again.
    ("HERMES_INPUT_REFUSED", 409, "runtime.engine", True, "runtime"),
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
    ("PACKAGE_INVALID", 500, "runtime.package", False, "template"),
    ("PACKAGE_UNSUPPORTED_PROTOCOL", 500, "runtime.package", False, "template"),
    # This code is the in-box read of a plugin's MCP declaration: an unreadable
    # cache marker, an absent command runner, a scan that failed or answered
    # with something other than documents. The row beneath it is the separate
    # question of whether the deployment configured a proxy for the bridge to
    # reach at all, which the operator answers and the sandbox cannot. Splitting
    # them is what lets an `owner` be true of every site that raises a code.
    # The RuntimeProfile administrator chose whether limited networking admits
    # Agent-declared MCP destinations. Repeating the same Agent/RuntimeProfile
    # pair cannot change that policy.
    (
        "AGENT_MCP_NETWORK_ACCESS_DISABLED",
        409,
        "configuration.runtime_profile",
        False,
        "template",
    ),
    # An Agent's or Assistant's own Skill, Plugin or MCP host would open the
    # deployment's own network (private, loopback, link-local, unresolvable) to
    # a limited RuntimeProfile that does not name it. Raised when the definition
    # is saved and again when a sandbox policy is built from it. Only a changed
    # host or an administrator's RuntimeProfile edit changes the answer.
    (
        "AGENT_EGRESS_HOST_REFUSED",
        403,
        "configuration.runtime_profile",
        False,
        "template",
    ),
    # An author's own MCP definition sets `provider` or
    # `credential_target_url`, which choose the credential the egress sidecar
    # attaches; only a catalog assignment writes them.
    ("AGENT_MCP_FIELD_RESERVED", 403, "configuration.agent", False, "template"),
    # A repository names a deploy-key secret the administrator did not list in
    # ASTRABOX_DEPLOY_KEY_SECRET_NAMES. The author picks a listed key, or an
    # administrator lists this one.
    ("AGENT_DEPLOY_KEY_NOT_ALLOWED", 403, "configuration.agent", False, "template"),
    # A stored Agent names no model. Every write requires one and schema
    # migration 6 gives one to rows stored without it, so only a row written
    # past both reaches this. Resolving it anyway would run the deployment's
    # default model in its place; whoever manages the Agent sets its model.
    ("AGENT_MODEL_REQUIRED", 409, "configuration.agent", False, "template"),
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
    "AGENT_DISABLED",
    status_code=409,
    category="configuration",
    owner="platform",
)
register_error(
    "AGENT_RUNTIME_PROFILE_DISABLED",
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
    "PREPARED_CAPACITY_LIMIT_EXCEEDED",
    status_code=409,
    category="configuration.agent",
    owner="platform",
    user_message=(
        "This deployment's prepared capacity is already committed to other "
        "Agents; lower this Agent's prepared_capacity or raise "
        "ASTRABOX_PREPARED_CAPACITY_LIMIT."
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


# These rows deliberately do not form one cleanup family. Stopping an engine or
# PTY is a runtime failure. A sandbox destroy that returned no answer can be retried as
# sent, while a refused destroy needs its ownership/backend evidence repaired
# before the same DELETE is useful.
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
