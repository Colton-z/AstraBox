"""The exact engine profile selected for one live Agent E2E process.

The source matrix names vendor vocabulary and expected capabilities. Deployment
evidence adds the immutable image, Environment, model and Agent ids that were
actually configured. The live runner selects exactly one profile per process;
tests ask for semantic roles here and never learn another engine's vocabulary.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any


MATRIX_EVIDENCE_ENV = "ASTRABOX_E2E_AGENT_MATRIX_FILE"
ENGINE_KIND_ENV = "ASTRABOX_E2E_ENGINE_KIND"


def _required_text(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise RuntimeError(f"Agent E2E profile has no {label}")
    return text


@lru_cache(maxsize=1)
def current_profile() -> dict[str, Any]:
    """Load and validate the one deployment profile this process must drive."""

    raw_path = os.getenv(MATRIX_EVIDENCE_ENV, "").strip()
    path = Path(raw_path)
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise RuntimeError(
            f"{MATRIX_EVIDENCE_ENV} must name an absolute regular evidence file"
        )
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read Agent E2E matrix evidence {path}: {exc}") from exc
    if not isinstance(evidence, dict) or evidence.get("version") != 1:
        raise RuntimeError("Agent E2E matrix evidence must use version 1")
    engine_kind = _required_text(os.getenv(ENGINE_KIND_ENV), ENGINE_KIND_ENV)
    state = evidence.get("state")
    if state != "CONFIGURED":
        raise RuntimeError(f"Agent E2E matrix evidence is not CONFIGURED: state={state!r}")
    profiles = evidence.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        raise RuntimeError("Agent E2E matrix evidence has no profiles")
    matches = [
        item
        for item in profiles
        if isinstance(item, dict) and item.get("engine_kind") == engine_kind
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one Agent E2E profile for engine_kind={engine_kind!r}, "
            f"found {len(matches)}"
        )
    profile = dict(matches[0])
    required = {
        "agent_id",
        "agent_name",
        "child_completion",
        "contracts",
        "decisions",
        "engine_kind",
        "environment_name",
        "image",
        "instructions",
        "mirror_entry_types",
        "model",
        "modes",
        "presentation",
        "tools",
    }
    missing = sorted(required - set(profile))
    if missing:
        raise RuntimeError(f"Agent E2E profile is missing {missing}")
    for key in ("agent_id", "agent_name", "environment_name", "image", "model"):
        _required_text(profile.get(key), key)
    for key in (
        "child_completion",
        "contracts",
        "decisions",
        "instructions",
        "modes",
        "tools",
    ):
        if not isinstance(profile.get(key), dict):
            raise RuntimeError(f"Agent E2E profile {key} must be an object")
    entry_types = profile.get("mirror_entry_types")
    if not isinstance(entry_types, list) or not entry_types or any(
        not isinstance(item, str) or not item.strip() for item in entry_types
    ):
        raise RuntimeError("Agent E2E profile mirror_entry_types must name entries")
    return profile


def engine_kind() -> str:
    return str(current_profile()["engine_kind"])


def agent_name() -> str:
    return str(current_profile()["agent_name"])


def agent_id() -> str:
    return str(current_profile()["agent_id"])


def contract_supported(name: str) -> bool:
    value = current_profile()["contracts"].get(name)
    if not isinstance(value, bool):
        raise RuntimeError(f"Agent E2E profile has no boolean contract {name!r}")
    return value


def permission_mode(role: str) -> str | None:
    value = current_profile()["modes"].get(role)
    return _required_text(value, f"mode role {role!r}") if value is not None else None


def tool_names(role: str) -> tuple[str, ...]:
    value = current_profile()["tools"].get(role)
    if not isinstance(value, list) or not value or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise RuntimeError(f"Agent E2E profile has no tool role {role!r}")
    return tuple(item.strip() for item in value)


def tool_name(role: str) -> str:
    return tool_names(role)[0]


def decision(role: str) -> str:
    return _required_text(
        current_profile()["decisions"].get(role), f"decision role {role!r}"
    )


def approval_presentation() -> str:
    return _required_text(current_profile().get("presentation"), "approval presentation")


def mirror_entry_types() -> tuple[str, ...]:
    return tuple(str(item) for item in current_profile()["mirror_entry_types"])


def instruction(role: str) -> str:
    return _required_text(
        current_profile()["instructions"].get(role), f"instruction role {role!r}"
    )


def child_completion() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the selected engine's successful native status and reason words."""

    value = current_profile()["child_completion"]
    statuses = value.get("engine_statuses")
    reasons = value.get("engine_reasons")
    for name, items in (("engine_statuses", statuses), ("engine_reasons", reasons)):
        if not isinstance(items, list) or any(
            not isinstance(item, str) or not item.strip() for item in items
        ):
            raise RuntimeError(f"Agent E2E profile child_completion.{name} is invalid")
    if contract_supported("background_subagent") and not statuses:
        raise RuntimeError("background-subagent profile has no completion status")
    return tuple(statuses), tuple(reasons)
