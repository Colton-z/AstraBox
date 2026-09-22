"""Static engine capabilities declared by each registered adapter.

An engine is selected by identity and composed by capability.  Core code may
use ``engine_kind`` to find the adapter, but never to infer behavior.  Adding a
third-party adapter therefore requires no edit to a core allowlist: the adapter
declares which session products it can drive and the runtime shape it needs.

The platform contract is mandatory; only its implementation shape is declared.
Conversation continuity, FIFO input, streaming, stop, and runtime reattachment
are structural contracts, never feature flags. A dead engine cannot continue
its unfinished token stream, so AstraBox settles that turn explicitly and the
next runtime resumes the same native conversation. Optional vendor vocabulary
such as permission modes and interactive tools lives in
``EngineCapabilityManifest``; this module describes the stable adapter shape
needed before a runtime exists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping, cast

from astrabox.seams.sandbox import SANDBOX_TENANCIES


# Product defaults are choices AstraBox owns.  They are not compatibility
# allowlists: any registered adapter can declare support for any session kind.
_SESSION_KIND_DEFAULT_ENGINE: dict[str, str] = {
    "agent_chat": "claude_code",
    "assistant_chat": "assistant",
}
SESSION_KINDS = frozenset(_SESSION_KIND_DEFAULT_ENGINE)
SessionKind = Literal["agent_chat", "assistant_chat"]

#: Platform configuration inputs whose materialization depends on the selected
#: engine. Their values remain opaque to this declaration; the set only says
#: whether the adapter has a real consumer for the platform field.
ENGINE_CONFIGURATION_INPUTS = frozenset({
    "mcp_servers",
    "skills",
    "plugin_repos",
    # Another ENVIRONMENT field rather than an Agent one. The question the set
    # answers is the same either way — does the selected adapter have a real
    # consumer — and the answer is engine-specific in the usual way: Claude
    # Code reads `OTEL_*` from its process environment, Codex takes an `[otel]`
    # table over its app-server protocol, and an engine that emits no telemetry
    # at all declares nothing and has the field refused.
    "tracing",
})

_ENGINE_OPTIONS_FIELD_KEYS = frozenset({
    "key",
    "type",
    "required",
    "help",
    "label",
    "advanced",
    "placeholder",
    "protected_keys",
})

_ENGINE_OPTIONS_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class EngineSessionLogDeclaration:
    """Where an engine keeps a conversation it never hands over.

    Engines differ in how their transcript reaches the platform, and the
    difference is not what they persist but who moves it. The Claude CLI emits
    its transcript as protocol frames, so the platform is handed the lines.
    Codex writes rollouts and says nothing; the DeepSeek Harness writes one log
    per conversation and says nothing. For the second kind the box is the only
    place the conversation exists, and an engine that declares this is asking
    the platform to move the bytes: out while the box lives, back before a
    replacement box is asked to rejoin.

    Declaring it is the whole of an adapter's part. It is deliberately two
    values and no format: the platform relays bytes and never parses a line,
    because a session log's grammar is the vendor's, versioned by the vendor
    and migrated by the vendor on read.
    """

    #: Directory in the box under which the engine writes its logs, as a
    #: template over the conversation's home (``{home}/.codex/sessions``).
    #: The platform supplies the image account's home for conversation tenancy
    #: and each conversation's private home for shared tenancy. An absolute
    #: template is valid only for a log root independent of those homes.
    root_template: str
    #: Prefix that keeps this engine's scopes apart from every other engine's
    #: inside one platform session's transcript store. The rest of a scope is
    #: the log's path relative to the rendered root, which is what lets a
    #: restore be transcription rather than reconstruction.
    namespace: str
    #: Filenames under the rendered root that are session logs.
    glob: str = "*.jsonl"

    def rendered_root(self, *, home: str) -> str:
        """The concrete log directory for one conversation's home."""

        cleaned = str(home or "").strip().rstrip("/")
        if "{home}" in self.root_template and not cleaned:
            raise ValueError(
                "session_log.root_template needs the conversation home and "
                f"none was supplied: {self.root_template!r}"
            )
        rendered = self.root_template.format(home=cleaned)
        if not rendered.startswith("/"):
            raise ValueError(
                f"session_log root rendered to a relative path: {rendered!r}"
            )
        return rendered


@dataclass(frozen=True)
class EngineRuntimeProfileDeclaration:
    """Filesystem and account shape one conversation runs under, resolved.

    This is a COMPOSED artifact, not something an adapter writes. The platform
    owns every field that varies with sandbox tenancy — account naming, home
    placement, the account-assembly commands — because tenancy is a fact about
    boxes and the substrate mechanism behind it (OpenSandbox isolated
    sessions) is reachable only through the provider seam, which adapters
    cannot see. Adapters contribute only their own facts, via
    :class:`EngineWorkloadDeclaration`; ``composed_runtime_profile`` in
    ``runtime_profiles`` joins the two. Consumers keep reading this one type.
    """

    sandbox_tenancy: str
    username_template: str
    home_template: str
    workspace_template: str
    workspace_source_template: str = "{workspace}"
    file_root_template: str = "{workspace}"
    config_dir_name: str | None = None
    config_env_var: str | None = None
    cache_template: str = "{home}/.cache"
    temp_template: str = "{home}/tmp"
    required_commands: tuple[str, ...] = ()


#: Where the adapter runs each conversation's process.
#: ``per_conversation_account`` uses a separate account, workspace, and isolated
#: session for each conversation in shared tenancy. ``box_account`` uses the
#: image account for a sandbox-wide service. The platform rejects shared tenancy
#: for that placement because it cannot isolate conversations from each other.
CONVERSATION_PLACEMENT_PER_ACCOUNT = "per_conversation_account"
CONVERSATION_PLACEMENT_BOX_ACCOUNT = "box_account"
CONVERSATION_PLACEMENTS = (
    CONVERSATION_PLACEMENT_PER_ACCOUNT,
    CONVERSATION_PLACEMENT_BOX_ACCOUNT,
)


@dataclass(frozen=True)
class EngineWorkloadDeclaration:
    """The engine-owned facts of a conversation account, tenancy-free.

    These fields are invariant across sandbox tenancies. Account names, homes,
    and account-creation commands belong to the platform. The adapter declares
    its configuration directory and required commands once; the platform
    composes the full :class:`EngineRuntimeProfileDeclaration` per
    ``(sandbox_tenancy, session_kind)``.
    """

    #: Dot-directory basename the engine keeps its per-account configuration
    #: in (e.g. ``.claude``), or ``None`` for an engine without one. Never
    #: inherited between engines.
    config_dir_name: str | None = None
    #: Environment variable naming that directory to the engine, when the
    #: vendor reads one (e.g. ``CLAUDE_CONFIG_DIR``).
    config_env_var: str | None = None
    #: Commands the ENGINE's own path needs in the image. Account-assembly
    #: commands (``useradd``/``groupadd``) are the platform's and are appended
    #: by composition on the shared tenancy — an adapter repeating them is
    #: harmless but redundant.
    required_commands: tuple[str, ...] = ()


@dataclass(frozen=True)
class EngineRuntimeCapabilities:
    """One adapter's stable runtime shape.

    An engine plugin composes into either existing AstraBox product by declaring
    ``agent_chat`` and/or ``assistant_chat``. Product kinds are not an engine
    extension seam: adding one requires its own ownership and workspace model.

    Each runtime profile names its own optional configuration directory.
    ``None`` means the engine has no such directory; it must not inherit
    Claude's ``.claude`` directory by accident.
    """

    engine_kind: str
    supported_session_kinds: frozenset[str]
    workload: EngineWorkloadDeclaration = EngineWorkloadDeclaration()
    conversation_placement: str = CONVERSATION_PLACEMENT_BOX_ACCOUNT
    #: Image used only when an Environment leaves ``runtime_template_name``
    #: unpinned. The adapter owns this default because the image contains that
    #: engine's process and control protocol. ``None`` requires every
    #: Environment for the adapter to name an image explicitly.
    default_runtime_image: str | None = None
    #: Permission modes required for bootstrap and advertised before a Session
    #: has connected. A verified runtime manifest may add vendor modes; it may
    #: not remove these because defaults and pre-runtime configuration rely on
    #: them.
    permission_modes: tuple[str, ...] = ()
    #: Default permission mode by supported AstraBox product.  The product
    #: chooses whether it wants a default; the adapter owns the vendor value
    #: that implements it.  Keeping both in this declaration means registering
    #: an engine never adds its vocabulary to platform core.
    permission_mode_defaults: tuple[tuple[str, str], ...] = ()
    #: Set when the engine's transcript is a file it keeps to itself, so the
    #: platform mirrors it out of the box and restores it into a replacement.
    #: Absent means the engine hands its transcript over some protocol of its
    #: own — which is a different mechanism, not a missing one.
    session_log: EngineSessionLogDeclaration | None = None
    #: Native JSON blocks accepted by the engine. Labels and help identify
    #: their vendor targets and merge rules. Only block names, object shape,
    #: and platform-owned keys are validated here; inner semantics are native.
    #: An empty declaration means the engine accepts no bag at all.
    engine_options_schema: tuple[dict[str, Any], ...] = ()
    #: Optional platform configuration the adapter actually consumes. Saving a
    #: value outside this set would create an inert knob: it could be read back
    #: from the Agent or Assistant even though no selected engine path uses it.
    configuration_inputs: frozenset[str] = frozenset()


def validate_runtime_profile_declaration(
    engine_kind: str,
    declaration: EngineRuntimeProfileDeclaration,
) -> None:
    """Validate the one typed platform shape at adapter registration."""

    if not isinstance(declaration, EngineRuntimeProfileDeclaration):
        raise TypeError("a composed runtime profile must be EngineRuntimeProfileDeclaration")
    tenancy = str(declaration.sandbox_tenancy or "").strip()
    if tenancy not in SANDBOX_TENANCIES:
        raise ValueError(
            f"engine_kind={engine_kind!r} declares unknown sandbox tenancy {tenancy!r}"
        )
    required_text = {
        "username_template",
        "home_template",
        "workspace_template",
        "workspace_source_template",
        "file_root_template",
        "cache_template",
        "temp_template",
    }
    missing = sorted(
        key for key in required_text if not str(getattr(declaration, key) or "").strip()
    )
    if missing:
        raise ValueError(
            f"engine_kind={engine_kind!r} runtime profile for {tenancy!r} "
            f"is missing fields: {missing}"
        )
    config_dir = declaration.config_dir_name
    if config_dir is not None and (
        not isinstance(config_dir, str)
        or not config_dir.startswith(".")
        or config_dir in {".", ".."}
        or "/" in config_dir
        or "\\" in config_dir
    ):
        raise ValueError(
            f"engine_kind={engine_kind!r} config_dir_name must be None "
            "or a dot-directory basename"
        )
    config_env_var = declaration.config_env_var
    if config_env_var is not None and (
        not isinstance(config_env_var, str)
        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", config_env_var) is None
    ):
        raise ValueError(
            f"engine_kind={engine_kind!r} config_env_var must be None or a shell "
            "environment variable name"
        )
    if config_env_var is not None and config_dir is None:
        raise ValueError(
            f"engine_kind={engine_kind!r} config_env_var requires config_dir_name"
        )
    commands = declaration.required_commands
    if not isinstance(commands, tuple) or any(
        not isinstance(command, str) or not command.strip() for command in commands
    ):
        raise ValueError(
            f"engine_kind={engine_kind!r} required_commands must be a tuple "
            "of non-empty command names"
        )
    if len(set(commands)) != len(commands):
        raise ValueError(
            f"engine_kind={engine_kind!r} required_commands must be unique"
        )


def validate_engine_options_schema(
    engine_kind: str,
    schema: tuple[dict[str, Any], ...],
) -> None:
    """Validate an engine's ``engine_options`` declaration at registration.

    The declaration is the write path's only authority for what an Agent's
    bag may contain, so a malformed declaration must die here — before any
    Agent can be validated against it — not surface as a confusing write
    rejection or an unrenderable form.
    """

    if not isinstance(schema, tuple):
        raise TypeError(
            f"engine_kind={engine_kind!r} engine_options_schema must be a tuple"
        )
    seen_keys: set[str] = set()
    for index, field in enumerate(schema):
        label = f"engine_kind={engine_kind!r} engine_options_schema[{index}]"
        if not isinstance(field, dict):
            raise TypeError(f"{label} must be a dict")
        unknown = sorted(set(field) - _ENGINE_OPTIONS_FIELD_KEYS)
        if unknown:
            raise ValueError(f"{label} has unknown declaration keys: {unknown}")
        key = field.get("key")
        if not isinstance(key, str) or _ENGINE_OPTIONS_KEY_RE.fullmatch(key) is None:
            raise ValueError(f"{label} key must be a snake_case identifier")
        if key in seen_keys:
            raise ValueError(f"{label} duplicates key {key!r}")
        seen_keys.add(key)
        field_type = field.get("type")
        if field_type != "object":
            raise ValueError(f"{label} must declare a native JSON object block")
        protected_keys = field.get("protected_keys", [])
        if (
            not isinstance(protected_keys, list)
            or any(not isinstance(item, str) or not item.strip() for item in protected_keys)
            or len(set(protected_keys)) != len(protected_keys)
            or (protected_keys and field_type != "object")
        ):
            raise ValueError(f"{label} protected_keys must be unique strings on an object block")
        if "required" in field and not isinstance(field.get("required"), bool):
            raise ValueError(f"{label} required must be a boolean")
        for text_key in ("help", "label", "placeholder"):
            if text_key in field and not isinstance(field.get(text_key), str):
                raise ValueError(f"{label} {text_key} must be a string")
        if "advanced" in field and not isinstance(field.get("advanced"), bool):
            raise ValueError(f"{label} advanced must be a boolean")


def validate_engine_capabilities(
    engine_kind: str,
    capabilities: EngineRuntimeCapabilities,
) -> None:
    """Validate claims that would otherwise misroute a runtime.

    Optional vendor protocols may be absent. The minimum Agent journey is not
    represented here and cannot be disabled; it is enforced by the adapter,
    transport, and client contracts. These checks cover malformed platform
    declarations: mismatched identity, no usable session product, or an unsafe
    config-directory basename.
    """

    normalized_kind = str(engine_kind or "").strip()
    if not normalized_kind:
        raise ValueError("engine_kind must be non-empty")
    if not isinstance(capabilities, EngineRuntimeCapabilities):
        raise TypeError(
            f"engine_kind={normalized_kind!r} capabilities must be "
            "EngineRuntimeCapabilities"
        )
    if capabilities.engine_kind != normalized_kind:
        raise ValueError(
            f"engine_kind={normalized_kind!r} does not match capabilities "
            f"engine_kind={capabilities.engine_kind!r}"
        )
    session_kinds = capabilities.supported_session_kinds
    if not isinstance(session_kinds, frozenset) or not session_kinds:
        raise ValueError(
            f"engine_kind={normalized_kind!r} must declare at least one "
            "supported_session_kind"
        )
    invalid_kinds = sorted(
        kind
        for kind in session_kinds
        if not isinstance(kind, str) or kind not in SESSION_KINDS
    )
    if invalid_kinds:
        raise ValueError(
            f"engine_kind={normalized_kind!r} has unsupported session kinds: "
            f"{invalid_kinds!r}"
        )
    workload = capabilities.workload
    if not isinstance(workload, EngineWorkloadDeclaration):
        raise TypeError(
            f"engine_kind={normalized_kind!r} workload must be EngineWorkloadDeclaration"
        )
    config_dir = workload.config_dir_name
    if config_dir is not None and (
        not isinstance(config_dir, str)
        or not config_dir.startswith(".")
        or "/" in config_dir
        or config_dir in {".", ".."}
    ):
        raise ValueError(
            f"engine_kind={normalized_kind!r} config_dir_name must be None or a "
            f"dot-directory basename, got {config_dir!r}"
        )
    if not isinstance(workload.required_commands, tuple) or any(
        not isinstance(command, str) or not command.strip()
        for command in workload.required_commands
    ):
        raise ValueError(
            f"engine_kind={normalized_kind!r} workload required_commands must be "
            "a tuple of non-empty strings"
        )
    placement = str(capabilities.conversation_placement or "").strip()
    if placement not in CONVERSATION_PLACEMENTS:
        raise ValueError(
            f"engine_kind={normalized_kind!r} conversation_placement must be one "
            f"of {CONVERSATION_PLACEMENTS}, got {placement!r}"
        )
    default_runtime_image = capabilities.default_runtime_image
    if default_runtime_image is not None and (
        not isinstance(default_runtime_image, str)
        or not default_runtime_image.strip()
        or default_runtime_image != default_runtime_image.strip()
    ):
        raise ValueError(
            f"engine_kind={normalized_kind!r} default_runtime_image must be "
            "None or a normalized non-empty image reference"
        )
    if (
        "assistant_chat" in session_kinds
        and placement != CONVERSATION_PLACEMENT_PER_ACCOUNT
    ):
        raise ValueError(
            f"engine_kind={normalized_kind!r} supports assistant_chat, whose "
            "product runs on the shared tenancy, but its integration places "
            "conversations on the box account"
        )
    permission_modes = capabilities.permission_modes
    if not isinstance(permission_modes, tuple) or any(
        not isinstance(mode, str) or not mode.strip() for mode in permission_modes
    ):
        raise ValueError(
            f"engine_kind={normalized_kind!r} permission_modes must be a tuple "
            "of non-empty engine-owned names"
        )
    if len(set(permission_modes)) != len(permission_modes):
        raise ValueError(
            f"engine_kind={normalized_kind!r} permission_modes must be unique"
        )
    permission_mode_defaults = capabilities.permission_mode_defaults
    if not isinstance(permission_mode_defaults, tuple) or any(
        not isinstance(item, tuple)
        or len(item) != 2
        or not all(isinstance(value, str) and value.strip() for value in item)
        for item in permission_mode_defaults
    ):
        raise ValueError(
            f"engine_kind={normalized_kind!r} permission_mode_defaults must be "
            "a tuple of (session_kind, mode) pairs"
        )
    default_kinds = [item[0] for item in permission_mode_defaults]
    if len(set(default_kinds)) != len(default_kinds):
        raise ValueError(
            f"engine_kind={normalized_kind!r} permission_mode_defaults must "
            "name each session kind at most once"
        )
    for default_session_kind, default_mode in permission_mode_defaults:
        if default_session_kind not in session_kinds:
            raise ValueError(
                f"engine_kind={normalized_kind!r} declares a permission-mode "
                f"default for unsupported session_kind={default_session_kind!r}"
            )
        if default_mode not in permission_modes:
            raise ValueError(
                f"engine_kind={normalized_kind!r} permission-mode default "
                f"{default_mode!r} is not in permission_modes"
            )
    configuration_inputs = capabilities.configuration_inputs
    if not isinstance(configuration_inputs, frozenset) or any(
        not isinstance(name, str) or not name.strip()
        for name in configuration_inputs
    ):
        raise ValueError(
            f"engine_kind={normalized_kind!r} configuration_inputs must be a "
            "frozenset of non-empty platform field names"
        )
    unknown_inputs = sorted(configuration_inputs - ENGINE_CONFIGURATION_INPUTS)
    if unknown_inputs:
        raise ValueError(
            f"engine_kind={normalized_kind!r} declares unknown configuration "
            f"inputs: {unknown_inputs!r}"
        )
    session_log = capabilities.session_log
    if session_log is not None:
        # Every field is load-bearing at a moment nobody is watching: the root
        # decides which files leave the box, the namespace keeps one engine's
        # scopes out of another's inside one session, and the glob decides what
        # counts as a log. A wrong one mirrors nothing and says nothing until a
        # replacement box comes up empty.
        raw_root = str(session_log.root_template or "")
        if not (raw_root.startswith("/") or raw_root.startswith("{home}/")):
            raise ValueError(
                f"engine_kind={normalized_kind!r} session_log.root_template "
                "must be absolute or rooted at the conversation home "
                f"('{{home}}/...'), not {raw_root!r}"
            )
        if not str(session_log.namespace or "").strip():
            raise ValueError(
                f"engine_kind={normalized_kind!r} session_log.namespace is "
                "required: it is what keeps this engine's scopes apart from "
                "another engine's in the same session"
            )
        if not str(session_log.glob or "").strip():
            raise ValueError(
                f"engine_kind={normalized_kind!r} session_log.glob is required"
            )
    validate_engine_options_schema(normalized_kind, capabilities.engine_options_schema)


def unsupported_engine_configuration_inputs(
    engine_kind: str,
    values: Mapping[str, Any],
) -> tuple[str, ...]:
    """Configured platform fields the selected adapter does not consume."""

    supported = capabilities_for_engine_kind(engine_kind).configuration_inputs

    def configured(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (dict, list, tuple, set, frozenset)):
            return bool(value)
        # A malformed non-null value is still configured. Its shape validator
        # may give a more specific error, but it must never become an inert knob.
        return True

    return tuple(
        sorted(
            name
            for name, value in values.items()
            if configured(value) and name not in supported
        )
    )


def require_session_kind(session_kind: str | None) -> SessionKind:
    """Return one platform product kind or reject corrupt durable state."""

    kind = str(session_kind or "").strip()
    if kind not in SESSION_KINDS:
        raise ValueError(
            f"session_kind={kind!r} is not a supported AstraBox product; "
            f"expected one of {sorted(SESSION_KINDS)!r}"
        )
    return cast(SessionKind, kind)


def default_permission_mode_for_engine(
    engine_kind: str,
    session_kind: str,
) -> str | None:
    """Return AstraBox's product default, or None when no default is chosen."""

    normalized_engine = str(engine_kind or "").strip()
    normalized_session = require_session_kind(session_kind)
    capabilities = capabilities_for_engine_kind(normalized_engine)
    return dict(capabilities.permission_mode_defaults).get(normalized_session)


def default_engine_for_session_kind(session_kind: str | None) -> str:
    """Return AstraBox's product default for a session kind.

    Session creation supplies this field explicitly. A missing or unknown value
    is corrupt platform state, not a reason to route through Claude.
    """

    return _SESSION_KIND_DEFAULT_ENGINE[require_session_kind(session_kind)]


def engine_kinds_for_session_kind(session_kind: str | None) -> frozenset[str]:
    """Return registered adapters that declare support for ``session_kind``."""

    kind = str(session_kind or "").strip()
    if kind not in SESSION_KINDS:
        return frozenset()
    from astrabox.core.service.orchestrator.engine.registry import (
        registered_engine_adapters,
    )

    return frozenset(
        engine_kind
        for engine_kind, adapter in registered_engine_adapters().items()
        if kind in adapter.capabilities.supported_session_kinds
    )


def engine_allowed_for_session_kind(
    engine_kind: str | None,
    session_kind: str | None,
) -> bool:
    """Whether an installed adapter declares this session product."""

    engine = str(engine_kind or "").strip()
    if not engine:
        return False
    return engine in engine_kinds_for_session_kind(session_kind)


def require_engine_for_session_kind(
    engine_kind: str | None,
    session_kind: str | None,
) -> str:
    """Resolve a product default and validate the adapter's declaration."""

    engine = str(engine_kind or "").strip()
    if not engine:
        engine = default_engine_for_session_kind(session_kind)
    if not engine_allowed_for_session_kind(engine, session_kind):
        kind = str(session_kind or "").strip()
        raise ValueError(
            f"engine_kind={engine!r} is not installed with support for session_kind="
            f"{kind!r}; "
            f"available={sorted(engine_kinds_for_session_kind(session_kind))}"
        )
    return engine


def capabilities_for_engine_kind(engine_kind: str) -> EngineRuntimeCapabilities:
    """Return an installed adapter's declaration; never invent Claude defaults."""

    from astrabox.core.service.orchestrator.engine.registry import get_engine_adapter

    return get_engine_adapter(str(engine_kind or "").strip()).capabilities


def resolve_session_capabilities(
    session: dict | None,
    *,
    runtime: object | None = None,
) -> EngineRuntimeCapabilities:
    """Resolve the session's adapter and return its declared runtime shape."""

    from astrabox.core.service.orchestrator.engine_kind_utils import (
        resolve_session_engine_kind,
    )

    return capabilities_for_engine_kind(
        resolve_session_engine_kind(session, runtime=runtime)
    )
