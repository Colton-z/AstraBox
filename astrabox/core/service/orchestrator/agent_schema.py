"""Editable Agent configuration schema.

This module is the single authoritative source for the *editable* shape of an
Agent (``docs/domain-model.md``). The integer ``version`` is optimistic
concurrency state, not a selectable Agent configuration version.
It drives the schema-driven form editor on the frontend and the lightweight
payload validation on the write path (``AgentConfigService.upsert_agent_config``).

The stored Agent carries ``model`` as a first-class required field, ``system``
as its prompt, ``mcp_servers`` as a name-keyed server map, and
``exposure_mode`` / ``idle_hibernate_seconds`` are runtime settings.
Model-provider *credentials* do not live here — they are an Environment
property (``environment.provider_access``), shared across the agents on an
environment.

Design notes:
- Field metadata is a plain ``list[dict]`` constant (not pydantic) to match the
  ``@dataclass`` convention used across the codebase and to stay trivially
  JSON-serializable for the GET endpoint.
- Validation here is intentionally thin: types / required / enum only. Deep
  normalization (plugin_repos, default_repo, mcp) stays in its existing owners.
  The payload is never rewritten and defaults are never filled in. The
  top-level wire shape and fixed nested objects are closed: undeclared input
  fails loud instead of entering the Agent document's open storage shape.
- ``model`` validates as a required free-text string. Self-hosted BYO-key has
  no authoritative model registry (a gateway can serve any id), so there is no
  code-derived model enum; the console offers a curated combobox with a
  free-entry escape hatch (a frontend convenience over free-text validation).
"""

from __future__ import annotations

from typing import Any

from astrabox.core.service.orchestrator.schema_validation import (
    invalid_request,
    validate_payload,
)

AGENT_SCHEMA_VERSION = 1

# Field group ids, ordered to mirror the Claude Console layout.
GROUP_IDENTITY = "identity"
GROUP_RUNTIME = "runtime"
GROUP_MODEL = "model"
GROUP_TOOLS = "tools"
GROUP_WORKSPACE = "workspace"

# Choose the runtime before model options: the Environment determines which
# engine-specific fields the form exposes. Group `id` is the stable contract;
# the label + subtitle are owned by the frontend i18n catalogue, keyed by this id
# (misc:agent_form.groups.<id>.{label,description}). The backend schema never
# carries human-facing copy — that is a frontend i18n concern, not the API's.
AGENT_GROUPS: list[dict[str, str]] = [
    {"id": GROUP_IDENTITY},
    {"id": GROUP_RUNTIME},
    {"id": GROUP_MODEL},
    {"id": GROUP_TOOLS},
    {"id": GROUP_WORKSPACE},
]

# Enum candidate sets. These are fixed, code-derived values. There is
# deliberately no model-name enum — model is free text (see module docstring).
_REPO_PROTOCOLS = ["ssh", "https"]
_EXPOSURE_MODES = ["chat_only", "mcp_only", "both"]

# Sub-field schemas for the structured nested blocks. Free-form blocks
# (engine_options, mcp_servers) intentionally have no item_schema
# — they are edited via key-value / structured editors on the form. These carry
# shape for clients and validation, not console copy: the form renders an object
# block through its JSON escape hatch unless the block is listed in
# `INLINE_ITEM_SCHEMA_KEYS` (frontend/src/manage/agentEditConfig.ts), and only
# then does a sub-field become a control needing a label
# (misc:agent_form.fields.<parent>.<sub>.label, which
# tests/agent_form_copy_test.py then requires).
_DEFAULT_REPO_ITEM_SCHEMA: list[dict[str, Any]] = [
    {"key": "url", "type": "string"},
    {"key": "protocol", "type": "enum", "enum": _REPO_PROTOCOLS, "advanced": True},
    {"key": "deploy_key_secret_name", "type": "string", "advanced": True},
    {"key": "branch", "type": "string", "advanced": True},
    {"key": "depth", "type": "integer", "advanced": True},
]

_PLUGIN_REPO_ITEM_SCHEMA: list[dict[str, Any]] = [
    {"key": "url", "type": "string", "required": True},
    {"key": "protocol", "type": "enum", "enum": _REPO_PROTOCOLS, "advanced": True},
    {"key": "deploy_key_secret_name", "type": "string", "advanced": True},
    {"key": "branch", "type": "string", "advanced": True},
    {"key": "depth", "type": "integer", "advanced": True},
    {"key": "sha", "type": "string", "advanced": True},
    {"key": "plugin_paths", "type": "string_list", "advanced": True},
]

# The authoritative editable field list. Order = render order on the form.
# Field `key` is the stable contract; the form's label/help are owned by frontend
# i18n (misc:agent_form.fields.<key>.{label,help}) — the schema carries shape,
# never copy. `path` lands the value in the stored document (display_meta.*).
#
# Not in this list (deliberately):
# - agent_id / version: system-managed identity + optimistic-concurrency state,
#   never client-editable form fields.
# - created_by: server-stamped ownership; admins / visibility /
#   allowed_user_ids: the dedicated Agent access endpoint. None is accepted by
#   the general authoring request.
# - model_config / provider access: model credentials are an Environment
#   property (environment.provider_access), not an agent field.
AGENT_FIELD_SCHEMA: list[dict[str, Any]] = [
    # ── identity ──────────────────────────────────────────────────────────
    {"key": "name", "type": "string", "group": GROUP_IDENTITY, "required": True},
    {
        "key": "display_name",
        "type": "string",
        "group": GROUP_IDENTITY,
        "path": "display_meta.display_name",
    },
    {"key": "description", "type": "text", "group": GROUP_IDENTITY},
    {
        "key": "icon",
        "type": "string",
        "advanced": True,
        "group": GROUP_IDENTITY,
        "path": "display_meta.icon",
    },
    {
        "key": "tags",
        "type": "string_list",
        "advanced": True,
        "group": GROUP_IDENTITY,
        "path": "display_meta.tags",
    },
    {"key": "use_cases", "type": "string_list", "advanced": True, "group": GROUP_IDENTITY},
    # ── model ─────────────────────────────────────────────────────────────
    # model is first-class + required. Free text (no authoritative registry);
    # the console renders a combobox with a free-entry escape hatch.
    {"key": "model", "type": "string", "group": GROUP_MODEL, "required": True},
    {"key": "system", "type": "text", "group": GROUP_MODEL},
    {
        "key": "engine_options",
        "type": "object",
        "advanced": True,
        "group": GROUP_MODEL,
        "complex": True,
    },
    # ── tools ─────────────────────────────────────────────────────────────
    {"key": "skills", "type": "string_list", "group": GROUP_TOOLS},
    # mcp_servers is a name-keyed map of server definitions; the console edits
    # it as a list and serializes to and from the map.
    {
        "key": "mcp_servers",
        "type": "object",
        "advanced": True,
        "group": GROUP_TOOLS,
        "complex": True,
    },
    # Which workspace surfaces a person needs to inspect THIS Agent's work.
    # AstraBox runs Agent programs of any purpose, and a diff view serves only
    # the ones that edit files; a research or support Agent showing a
    # permanently empty Diff tab is a promise the product does not keep. They
    # sit on the Agent rather than the Environment because the question is what
    # the Agent is for, not what substrate it runs on — one Environment serves
    # Agents of different purposes.
    #
    # Hiding the terminal is not an access control and must never be read as
    # one: the sandbox stays reachable through the API, and the Agent can run
    # commands regardless. `sandbox_permission_level` on the Environment owns
    # that question.
    {"key": "terminal_panel", "type": "boolean", "group": GROUP_WORKSPACE},
    {"key": "diff_panel", "type": "boolean", "group": GROUP_WORKSPACE},
    {
        "key": "default_repo",
        "type": "object",
        "advanced": True,
        "group": GROUP_WORKSPACE,
        "complex": True,
        "item_schema": _DEFAULT_REPO_ITEM_SCHEMA,
    },
    {
        "key": "plugin_repos",
        "type": "object_list",
        "advanced": True,
        "group": GROUP_TOOLS,
        "complex": True,
        "item_schema": _PLUGIN_REPO_ITEM_SCHEMA,
    },
    # ── runtime ───────────────────────────────────────────────────────────
    # The environment supplies sandbox/image/install + provider access; the
    # agent only names it. exposure_mode / idle_hibernate_seconds are the
    # agent's own runtime settings.
    {"key": "environment_name", "type": "env_ref", "group": GROUP_RUNTIME, "required": True},
    {
        "key": "exposure_mode",
        "type": "enum",
        "enum": _EXPOSURE_MODES,
        "advanced": True,
        "group": GROUP_RUNTIME,
    },
    {"key": "idle_hibernate_seconds", "type": "integer", "advanced": True, "group": GROUP_RUNTIME},
    {"key": "prewarm_enabled", "type": "boolean", "advanced": True, "group": GROUP_RUNTIME},
    {"key": "enabled", "type": "boolean", "group": GROUP_RUNTIME},
]

# The schema uses logical keys for labels while a path can place a field under
# a wire-level object (the three display fields live under display_meta).
# This is the closed set accepted by the general Agent authoring endpoints.
AGENT_EDITABLE_REQUEST_FIELDS = frozenset(
    str(field.get("path") or field["key"]).split(".", 1)[0] for field in AGENT_FIELD_SCHEMA
)
_AGENT_UPDATE_REQUEST_FIELDS = AGENT_EDITABLE_REQUEST_FIELDS | {"version"}

#: Stored keys a read projection must drop, on every path that returns an
#: Agent. Leading-underscore keys are dropped by the rule the readers apply
#: themselves; these carry no underscore and so have to be named.
#:
#: This set exists in one place because it was in two, and they had drifted:
#: ``extension_catalog`` was documented as an internal the Agent form "must
#: neither expose nor write" in one reader while the other returned it, and
#: ``conversation_uid_cursor`` — the shared box's UID allocation cursor — was
#: returned by both.
#:
#: This is about what a client may SEE. What it may WRITE is a separate and
#: deliberately stricter contract: the authoring payload is closed and rejects
#: server-owned keys rather than ignoring them, because silently dropping an
#: access-policy or ownership field would let a caller believe a
#: security-semantic change took effect (``agent_access_control_update_test``).
#: A read-modify-write client therefore strips what it does not own — the
#: fields here never reach it to strip.
AGENT_PRIVATE_STORED_FIELDS = frozenset(
    {
        "main_session_id",
        "credential_vault_ids",
        "credentials_updated_by",
        "credentials_updated_at",
        "plugin_mcp_bridge_allowlist",
        # Runtime snapshots maintained only by the Agent-extension API.
        "extension_catalog",
        # Allocation state owned by conversation_identity's CAS.
        "conversation_uid_cursor",
        # Durable storage identity owned by the platform's mount planner.
        "workspace_id",
        # Supplier client-pool cleanup address; orchestration state, not Agent
        # authoring configuration.
        "_client_pool_name",
        "_client_pool_backend",
        "_retiring_client_pools",
    }
)
_DISPLAY_META_REQUEST_FIELDS = frozenset(
    str(field["path"]).split(".", 1)[1]
    for field in AGENT_FIELD_SCHEMA
    if str(field.get("path") or "").startswith("display_meta.")
)
_DEFAULT_REPO_REQUEST_FIELDS = frozenset(str(field["key"]) for field in _DEFAULT_REPO_ITEM_SCHEMA)
_PLUGIN_REPO_REQUEST_FIELDS = frozenset(str(field["key"]) for field in _PLUGIN_REPO_ITEM_SCHEMA)


def get_agent_schema() -> dict[str, Any]:
    """Return the JSON-serializable schema consumed by the admin form."""
    return {
        "version": AGENT_SCHEMA_VERSION,
        "groups": [dict(g) for g in AGENT_GROUPS],
        "fields": [dict(f) for f in AGENT_FIELD_SCHEMA],
    }


# ── Validation ────────────────────────────────────────────────────────────


def _reject_unknown_fields(payload: dict[str, Any], *, allow_version: bool) -> None:
    allowed = _AGENT_UPDATE_REQUEST_FIELDS if allow_version else AGENT_EDITABLE_REQUEST_FIELDS
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise invalid_request(f"agent payload contains unsupported fields: {', '.join(unknown)}")

    if "version" in payload:
        version = payload["version"]
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise invalid_request("version must be a positive integer")

    display_meta = payload.get("display_meta")
    if display_meta is not None:
        if not isinstance(display_meta, dict):
            raise invalid_request("display_meta must be an object")
        _reject_unknown_nested("display_meta", display_meta, _DISPLAY_META_REQUEST_FIELDS)

    default_repo = payload.get("default_repo")
    if isinstance(default_repo, dict):
        _reject_unknown_nested("default_repo", default_repo, _DEFAULT_REPO_REQUEST_FIELDS)

    plugin_repos = payload.get("plugin_repos")
    if isinstance(plugin_repos, list):
        for index, repo in enumerate(plugin_repos):
            if isinstance(repo, dict):
                _reject_unknown_nested(
                    f"plugin_repos[{index}]",
                    repo,
                    _PLUGIN_REPO_REQUEST_FIELDS,
                )


def _reject_unknown_nested(label: str, value: dict[str, Any], allowed: frozenset[str]) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        qualified = ", ".join(f"{label}.{key}" for key in unknown)
        raise invalid_request(f"agent payload contains unsupported fields: {qualified}")


def validate_agent_payload(
    payload: dict[str, Any], *, allow_version: bool = False
) -> dict[str, Any]:
    """Validate and return one closed Agent authoring request."""

    _reject_unknown_fields(payload, allow_version=allow_version)
    validate_payload(payload, AGENT_FIELD_SCHEMA, payload_label="agent payload")
    return {key: payload[key] for key in AGENT_EDITABLE_REQUEST_FIELDS if key in payload}
