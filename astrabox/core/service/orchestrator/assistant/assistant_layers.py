"""The layers an Assistant inherits, and how they combine with its own fields.

What an Assistant can do is decided by whoever deploys AstraBox and by its
owner, not by the platform. Layers above the Assistant provide Skills, MCP
servers, packages, native options, Credential Vaults and instructions; the
Assistant's own fields stack on top. The deployment's layer is the only one
read; the rules below take any number, broadest first, so a narrower layer
(an organization's within a hosted deployment) is one more scope. Layers are
read whenever an Assistant's runtime view is resolved.

Combination, broadest layer first and the Assistant last:

- instructions are joined in that order, before the Assistant's own;
- Skills, packages and Credential Vaults accumulate;
- MCP servers accumulate, and a name claimed twice is refused, since a tool
  call names one server;
- native option blocks are replaced block by block by a narrower layer;
- the model and, for a new Assistant, the Environment come from the
  narrowest layer that names one.
"""

from __future__ import annotations

from typing import Any

from astrabox.core.service.orchestrator.engine.capabilities import engine_allowed_for_session_kind
from astrabox.persistence.repository.assistant_defaults_repository import (
    DEPLOYMENT_SCOPE,
    AssistantDefaultsRepository,
)

#: The fields one layer may carry, named as an Assistant's own are.
LAYER_FIELDS = (
    "environment_name",
    "model_name",
    "instructions",
    "skills",
    "mcp_servers",
    "packages",
    "engine_options",
    "credential_vault_ids",
)

#: What of a layer changes what the Assistants beneath it run: everything it
#: provides, and its catalog selection. Not ``environment_name``, which only
#: chooses the Environment of Assistants created later, and not the layer's
#: bookkeeping (``version``, ``updated_by``, ``updated_at``), so a save that
#: changes neither prepares no Assistant again.
LAYER_RUNTIME_FIELDS = (
    "model_name",
    "instructions",
    "skills",
    "mcp_servers",
    "packages",
    "engine_options",
    "credential_vault_ids",
    "extension_catalog",
    "mcp_assignments",
)


async def inherited_layers(
    *, repo: AssistantDefaultsRepository | None = None
) -> list[tuple[str, dict[str, Any]]]:
    """The layers above an Assistant, broadest first, labelled."""

    layer = await (repo or AssistantDefaultsRepository()).get_layer(DEPLOYMENT_SCOPE)
    return [("the deployment's Assistant defaults", layer)] if layer else []


def narrowest(layers: list[tuple[str, dict[str, Any]]], field: str) -> str | None:
    """The value of ``field`` in the narrowest layer that names one."""

    for _label, layer in reversed(layers):
        value = str(layer.get(field) or "").strip()
        if value:
            return value
    return None


def joined_instructions(layers: list[tuple[str, dict[str, Any]]], own: str | None) -> str | None:
    parts = [str(layer.get("instructions") or "").strip() for _label, layer in layers]
    parts.append(str(own or "").strip())
    text = "\n\n".join(part for part in parts if part)
    return text or None


def merged_engine_options(
    layers: list[tuple[str, dict[str, Any]]], own: dict[str, Any] | None
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for _label, layer in layers:
        options = layer.get("engine_options")
        if isinstance(options, dict):
            merged.update(options)
    merged.update(dict(own or {}))
    return merged


async def assistant_environment_names(environments: list[dict[str, Any]]) -> list[str]:
    """The enabled Environments whose installed engine runs Assistants."""

    names: list[str] = []
    for environment in environments:
        if not isinstance(environment, dict) or environment.get("enabled") is False:
            continue
        if not engine_allowed_for_session_kind(str(environment.get("engine_kind") or ""), "assistant_chat"):
            continue
        name = str(environment.get("name") or "").strip()
        if name:
            names.append(name)
    return sorted(names)


async def default_environment_name(
    layers: list[tuple[str, dict[str, Any]]], environments: list[dict[str, Any]]
) -> str | None:
    """The Environment a new Assistant uses.

    The narrowest layer's choice; with none, the one Environment that can run
    Assistants when the deployment has exactly one, which is what lets a
    single-Environment deployment work without anyone setting a default.
    """

    chosen = narrowest(layers, "environment_name")
    if chosen:
        return chosen
    eligible = await assistant_environment_names(environments)
    return eligible[0] if len(eligible) == 1 else None
