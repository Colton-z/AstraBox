"""Admin Environment configuration schema.

An Environment is an admin-authored runtime configuration referenced by Agents
and Assistants. This module is the single authoritative source for the editable
shape persisted in the environment collection. It drives the schema-based form
and the write-path validation in ``platform_service.upsert_environment_config``.

An environment carries the sandbox runtime fields plus ``provider_access``
(the model-provider credentials shared across the Agents that select it);
``AgentConfigService`` merges it into each Agent's ``AgentView``
(``docs/domain-model.md``).

Schema properties:
- Field metadata is a plain ``list[dict]`` constant, trivially JSON-serializable.
- Validation is intentionally thin: types / required / enum plus the
  product-owned parsers for structured fields. Normalization is a separate
  write-time step that canonicalizes those fields and settles defaults once.
- There is no backing dataclass or import-time drift assertion; the open shape
  is validated by ``validate_environment_payload`` alone.
"""

from __future__ import annotations

from typing import Any

from astrabox.bootstrap import SANDBOX_IDLE_ACTIONS
from astrabox.seams.sandbox import (
    SANDBOX_NETWORK_MODES,
    SANDBOX_NETWORK_LIMITED,
    SANDBOX_PERMISSION_LEVEL_DEFAULT,
    SANDBOX_PERMISSION_LEVELS,
    SANDBOX_TENANCIES,
    SANDBOX_TENANCY_AGENT,
    SANDBOX_TENANCY_CONVERSATION,
)
from astrabox.core.service.orchestrator.schema_validation import (
    invalid_request,
    validate_payload,
)

ENVIRONMENT_SCHEMA_VERSION = 1

# Field group ids.
GROUP_BASIC = "basic"
GROUP_RUNTIME = "runtime"

# Ordered by the create/edit narrative: what it is → how it runs. Group `id` is
# the stable contract; label + subtitle are owned by frontend i18n, keyed by id
# (manage:env_form.groups.<id>.{label,description}). The schema carries no copy.
ENV_GROUPS: list[dict[str, str]] = [
    {"id": GROUP_BASIC},
    {"id": GROUP_RUNTIME},
]

# Engine / backend / permission enums are derived from the live registries at read/validate
# time, never hardcoded: the form offers exactly what this deployment can
# actually run (built-ins + any plugin-registered adapters/providers), and a
# plugin registering more extends the enum with no edit here.
def _engine_kinds() -> list[str]:
    from astrabox.core.service.orchestrator.engine.registry import known_engine_kinds

    return known_engine_kinds()


def _sandbox_backends() -> list[str]:
    from astrabox.seams.sandbox import registered_sandbox_names

    return registered_sandbox_names()


def _sandbox_permission_levels() -> list[str]:
    from astrabox.seams.sandbox import registered_sandbox_permission_levels

    return registered_sandbox_permission_levels()


def _endpoint_providers() -> list[str]:
    from astrabox.seams.model import registered_model_endpoint_names

    return registered_model_endpoint_names()


_DYNAMIC_ENUMS = {
    "engine_kind": _engine_kinds,
    "sandbox_backend": _sandbox_backends,
    "sandbox_permission_level": _sandbox_permission_levels,
    "endpoint_provider": _endpoint_providers,
}


def _field_enum(field: dict[str, Any]) -> list[str] | None:
    source = _DYNAMIC_ENUMS.get(str(field.get("key")))
    if source is not None:
        return source()
    return field.get("enum")

# Model-provider access sub-fields. Credentials are an Environment property, not
# an agent field: an api_key is not part of a harness's identity, so agents that
# differ only in system prompt share one key and a rotation touches one place
# (in Managed Agents this is invisible/platform-hosted; self-hosted BYO-key makes
# it an Environment property — see ``docs/domain-model.md``). ``api_key`` is a
# plaintext key (masked in read views); ``api_key_secret_name`` references a
# SecretProvider name instead. ``base_url`` points at the model endpoint / gateway.
_PROVIDER_ACCESS_ITEM_SCHEMA: list[dict[str, Any]] = [
    {"key": "base_url", "type": "string"},
    {"key": "api_key", "type": "string", "advanced": True},
    {"key": "api_key_secret_name", "type": "string", "advanced": True},
]

# Tracing sub-fields: where THIS environment's engines send their own spans.
# Not the same subject as the deployment's ``OTEL_EXPORTER_OTLP_ENDPOINT``,
# which traces the AstraBox process; this one is the engine inside the box. The
# credential is the same either/or as ``provider_access``: an inline value
# (masked in read views) or a SecretProvider name — and it is here rather than in an
# engine bag because ``engine_options`` deliberately has no secret type.
# Cross-field rules (an endpoint is required once anything else is set; the two
# credential forms are exclusive) live in ``parse_tracing_config``, the same
# call the runtime makes when it reads the document back.
_TRACING_ITEM_SCHEMA: list[dict[str, Any]] = [
    {"key": "enabled", "type": "boolean"},
    {"key": "endpoint", "type": "string"},
    {"key": "headers", "type": "key_value", "advanced": True},
    {"key": "auth_token", "type": "string", "advanced": True},
    {"key": "auth_token_secret_name", "type": "string", "advanced": True},
    {"key": "environment", "type": "string", "advanced": True},
    {"key": "signals", "type": "string_list", "advanced": True},
    {"key": "log_user_prompt", "type": "boolean", "advanced": True},
]

# Environment networking is a product contract, not an OpenSandbox policy
# document. The runtime resolves this shape into the provider-neutral sandbox
# seam, and each backend translates it at its own boundary. Vault bindings are
# intentionally absent: a provider composes their exact authorized destinations
# into its effective policy without duplicating them on the Environment record.
_NETWORKING_ITEM_SCHEMA: list[dict[str, Any]] = [
    {
        "key": "type",
        "type": "enum",
        "enum": list(SANDBOX_NETWORK_MODES),
        "required": True,
    },
    {"key": "allowed_hosts", "type": "string_list"},
    {"key": "allow_mcp_servers", "type": "boolean"},
]

# The authoritative editable field list. Order = render order on the form.
# Field `key` is the stable contract; label/help are owned by frontend i18n
# (manage:env_form.fields.<key>.{label,help}) — the schema carries shape, not copy.
ENV_FIELD_SCHEMA: list[dict[str, Any]] = [
    # ── basic ─────────────────────────────────────────────────────────────
    {"key": "name", "type": "string", "group": GROUP_BASIC, "required": True},
    {"key": "display_name", "type": "string", "group": GROUP_BASIC},
    {"key": "description", "type": "text", "group": GROUP_BASIC},
    {"key": "engine_kind", "type": "enum", "group": GROUP_BASIC, "required": True},
    {"key": "enabled", "type": "boolean", "group": GROUP_BASIC},
    # ── runtime ───────────────────────────────────────────────────────────
    {"key": "sandbox_backend", "type": "enum", "group": GROUP_RUNTIME},
    {"key": "runtime_template_name", "type": "string", "advanced": True, "group": GROUP_RUNTIME},
    # There is deliberately no per-environment CPU/memory field. The sandbox
    # backend's create path sends no resource request, so anything offered here
    # would be recorded and never applied — see docs/deploy.md "Sandbox resource
    # limits" for where the caps actually come from.
    {
        "key": "networking",
        "type": "object",
        "group": GROUP_RUNTIME,
        "complex": True,
        "item_schema": _NETWORKING_ITEM_SCHEMA,
        "default": {
            "type": SANDBOX_NETWORK_LIMITED,
            "allowed_hosts": [],
            "allow_mcp_servers": False,
        },
    },
    # What a quiet conversation's box becomes. This is an environment property:
    # whether a box can be paused is a
    # fact about the substrate this environment names (its backend, and a cluster
    # arranged for snapshots), not about the agent running in it.
    # Every environment carries its own idle action. There is no empty value
    # that defers to the installation, because the fate of a sandbox would then
    # depend on what ASTRABOX_SANDBOX_IDLE_ACTION happened to be when the sweep
    # ran, which can be months after the environment was written — and the
    # stored environment would not state what happens to its own sandboxes.
    # ``normalize_environment_payload`` settles the field once at write time
    # from that setting; nothing re-derives it on read. Whether the backend can
    # honor `pause` is the backend's answer, asked by the form before it offers
    # the choice — a setting that would never take effect must not be accepted.
    {"key": "idle_action", "type": "enum", "enum": list(SANDBOX_IDLE_ACTIONS),
     "advanced": True, "group": GROUP_RUNTIME},
    # How many conversations a box carries: a fact about the substrate this
    # environment names rather than about the agent running on it.
    #
    # `conversation` is the default and stays it on every substrate, because a
    # pool capable of carrying several is not an operator asking it to: mutually
    # untrusted tenants are the ordinary reason to keep paying for a box each.
    # So the intent is stated here and the substrate's capability is asked
    # afterwards. The write path requires an elevated permission level for
    # Agent tenancy; the running box is still probed because configuration is
    # not evidence that the grant took effect.
    {"key": "sandbox_tenancy", "type": "enum", "enum": list(SANDBOX_TENANCIES),
     "advanced": True, "group": GROUP_RUNTIME, "default": SANDBOX_TENANCY_CONVERSATION},
    # What a box of this environment is GRANTED, as opposed to how many
    # conversations it carries. `advanced` is what creating namespaces inside
    # the box needs, which is two different things: agent tenancy, whose
    # conversations ARE namespaces carved in one box, and an engine that runs
    # its own sandbox in-box — Codex enforces `workspace-write` with a bundled
    # bubblewrap and, without this, asks a person instead of enforcing.
    #
    # It stays `default` unless asked for. That level is the one a deployment
    # serving mutually untrusted users stays on, where the BOX is the boundary
    # and nothing inside it needs to carve another.
    {"key": "sandbox_permission_level", "type": "enum",
     "enum": list(SANDBOX_PERMISSION_LEVELS), "advanced": True,
     "group": GROUP_RUNTIME, "default": SANDBOX_PERMISSION_LEVEL_DEFAULT},
    # ── model connectivity ────────────────────────────────
    # endpoint_provider selects a registered model gateway. The schema response
    # fills its default from the provider registry, so this domain schema does
    # not name a concrete adapter.
    {"key": "endpoint_provider", "type": "enum", "group": GROUP_RUNTIME},
    # provider_access is the chosen provider's environment-level access object.
    {"key": "provider_access", "type": "object", "group": GROUP_RUNTIME, "complex": True,
     "item_schema": _PROVIDER_ACCESS_ITEM_SCHEMA},
    # ── engine tracing ────────────────────────────────────
    # An environment property for the same reasons provider access is: it names
    # an external service, it carries a credential, and it is the granularity at
    # which two sessions are decided to want the same treatment. Whether an
    # engine can emit spans at all is the engine's answer, asked before this is
    # accepted — a setting that would reach no code must not be stored.
    # Not marked advanced, unlike the network policy: an operator
    # who brings their own collector has to be able to find where it is named,
    # and this form's audience is the person who already decided the sandbox
    # backend and the model gateway sitting above it.
    {"key": "tracing", "type": "object", "group": GROUP_RUNTIME,
     "complex": True, "item_schema": _TRACING_ITEM_SCHEMA},
]


def get_environment_schema() -> dict[str, Any]:
    """Return the JSON-serializable schema consumed by the admin form.

    Dynamic enums (engine_kind / sandbox_backend / sandbox_permission_level /
    endpoint_provider) are resolved from the live registries per call, so the
    form always offers what this deployment runs.
    """
    fields: list[dict[str, Any]] = []
    for f in ENV_FIELD_SCHEMA:
        out = dict(f)
        if out.get("type") == "enum":
            out["enum"] = _field_enum(f)
        if out.get("key") == "endpoint_provider":
            from astrabox.seams.model import default_model_endpoint_name

            out["default"] = default_model_endpoint_name()
        fields.append(out)
    return {
        "version": ENVIRONMENT_SCHEMA_VERSION,
        "groups": [dict(g) for g in ENV_GROUPS],
        "fields": fields,
    }


# ── Normalization ─────────────────────────────────────────────────────────


def normalize_environment_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a storable Environment with concrete runtime defaults.

    Call this before :func:`validate_environment_payload`. When the form omits
    ``idle_action``, the installation's ``ASTRABOX_SANDBOX_IDLE_ACTION`` supplies
    it. Cleanup can then decide what happens to an idle sandbox from the stored
    Environment alone.
    """
    normalized = dict(payload)
    action = str(normalized.get("idle_action") or "").strip().lower()
    if not action:
        from astrabox.common.utils.settings import load_astrabox_settings

        action = str(load_astrabox_settings().sandbox_idle_action or "").strip().lower()
    normalized["idle_action"] = action
    from astrabox.core.service.orchestrator.environment_networking import (
        normalized_environment_networking,
    )

    try:
        normalized["networking"] = normalized_environment_networking(
            normalized.get("networking")
        )
    except ValueError as exc:
        raise invalid_request(f"environment payload: {exc}") from exc
    return normalized


# ── Validation ────────────────────────────────────────────────────────────


def validate_environment_payload(payload: dict[str, Any]) -> None:
    """Types / required / enum / item-schema validation for an environment payload.

    Delegates to the shared validator; ``_field_enum`` resolves the dynamic enums
    (engine_kind / sandbox_backend / sandbox_permission_level /
    endpoint_provider). Declared
    ``item_schema`` sub-fields such as ``provider_access`` are validated without
    mutating the payload.

    """
    validate_payload(
        payload, ENV_FIELD_SCHEMA, resolve_enum=_field_enum, payload_label="environment payload"
    )
    from astrabox.seams.tracing import parse_tracing_config

    parse_tracing_config(payload.get("tracing"))
    from astrabox.core.service.orchestrator.environment_networking import (
        parse_environment_networking,
    )

    try:
        parse_environment_networking(payload.get("networking"))
    except ValueError as exc:
        raise invalid_request(f"environment payload: {exc}") from exc
    _assert_tracing_reaches_the_engine(payload)
    _assert_idle_action_is_reachable(payload)
    _assert_permission_level_is_reachable(payload)
    _assert_tenancy_has_permission_level(payload)
    _assert_provider_configuration_is_reachable(payload)


def _assert_tracing_reaches_the_engine(payload: dict[str, Any]) -> None:
    """Refuse tracing on an engine that cannot emit it.

    An engine declares the platform configuration it consumes; anything else
    saved against it is an inert knob — readable back from the form, reaching no
    code. That failure is worse here than elsewhere, because the thing being
    configured is observability: a deployment would see no spans and have no way
    to tell "the collector is wrong" from "this engine never sends any".

    Only a switched-ON configuration is refused. A stored-but-disabled document
    is what an operator leaves behind when they turn tracing off, and refusing
    it would mean deleting the endpoint before an environment could change
    engines — the same credential loss the switch exists to prevent.
    """
    from astrabox.seams.tracing import parse_tracing_config

    spec = parse_tracing_config(payload.get("tracing"))
    if spec is None or not spec.active:
        return
    engine_kind = str(payload.get("engine_kind") or "").strip()
    if not engine_kind:
        # The thin validator already requires it; naming a second, vaguer
        # complaint about the same missing field helps nobody.
        return
    from astrabox.core.service.orchestrator.engine.capabilities import (
        unsupported_engine_configuration_inputs,
    )

    if not unsupported_engine_configuration_inputs(engine_kind, {"tracing": payload["tracing"]}):
        return
    raise invalid_request(
        f"environment payload: engine {engine_kind!r} does not emit tracing, so a "
        f"tracing endpoint configured here would never be read. Turn tracing off "
        f"for this environment, or select an engine that supports it."
    )


def _assert_idle_action_is_reachable(payload: dict[str, Any]) -> None:
    """Refuse ``idle_action: pause`` on a backend that cannot snapshot.

    The same reasoning as the startup gate on the deployment-wide setting, applied
    where an environment can now override it: recording ``pause`` against a
    backend with no snapshot would keep destroying the boxes, so nothing would look
    broken and the loss would surface later as users finding their files gone. A
    setting that cannot take effect is refused at the form instead.

    An empty backend field means the deployment's default, which the startup gate
    already vetted for the deployment-wide action — but not for this one, since an
    environment can select ``pause`` on a deployment whose default is
    ``terminate``. So the check resolves the same name the runtime would.
    """
    action = str(payload.get("idle_action") or "").strip().lower()
    if action != "pause":
        return
    from astrabox.seams.sandbox import sandbox_for_name

    provider = sandbox_for_name(payload.get("sandbox_backend"))
    if provider.supports_pause:
        return
    raise invalid_request(
        f"environment payload: idle_action 'pause' needs a sandbox backend that "
        f"can snapshot, and {provider.name!r} cannot — an idle box would go on "
        f"being destroyed with its workspace. Use 'terminate', or select a "
        f"backend that supports pausing."
    )


def _assert_tenancy_has_permission_level(payload: dict[str, Any]) -> None:
    """Refuse shared tenancy when its required isolation grant was not asked for."""

    tenancy = str(
        payload.get("sandbox_tenancy") or SANDBOX_TENANCY_CONVERSATION
    ).strip().lower()
    level = str(
        payload.get("sandbox_permission_level") or SANDBOX_PERMISSION_LEVEL_DEFAULT
    ).strip().lower()
    if tenancy != SANDBOX_TENANCY_AGENT or level != SANDBOX_PERMISSION_LEVEL_DEFAULT:
        return
    raise invalid_request(
        "environment payload: sandbox_tenancy 'agent' creates isolated areas "
        "inside one box and therefore requires sandbox_permission_level "
        "'advanced' or 'privileged'"
    )


def _assert_permission_level_is_reachable(payload: dict[str, Any]) -> None:
    """Refuse a grant the selected sandbox backend cannot attest."""

    from astrabox.seams.sandbox import sandbox_for_name

    provider = sandbox_for_name(payload.get("sandbox_backend"))
    level = str(
        payload.get("sandbox_permission_level") or SANDBOX_PERMISSION_LEVEL_DEFAULT
    ).strip().lower()
    supported = getattr(
        provider,
        "supported_permission_levels",
        (SANDBOX_PERMISSION_LEVEL_DEFAULT,),
    )
    if level in supported:
        return
    raise invalid_request(
        f"environment payload: sandbox backend {provider.name!r} cannot deliver "
        f"and attest sandbox_permission_level {level!r}; choose one of "
        f"{', '.join(repr(item) for item in supported)}"
    )


def _assert_provider_configuration_is_reachable(payload: dict[str, Any]) -> None:
    """Let the selected provider reject only its own unsupported topology."""

    from astrabox.seams.sandbox import sandbox_for_name

    sandbox_for_name(payload.get("sandbox_backend")).validate_environment_configuration(
        payload
    )
