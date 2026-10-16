"""Shared SDK raw-event helpers for stream and transcript projection paths."""

from typing import Any

from astrabox.core.service.orchestrator.assistant_text import is_full_assistant_snapshot
from astrabox.core.service.orchestrator.slash_commands import (
    merge_slash_command_details,
)


def extract_agent_session_metadata(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    payload = raw
    if (
        str(payload.get("subtype") or "").strip().lower() != "init"
        and not _is_initialization_snapshot_payload(payload)
    ):
        nested = raw.get("data")
        if not isinstance(nested, dict):
            return {}
        if (
            str(nested.get("subtype") or "").strip().lower() != "init"
            and not _is_initialization_snapshot_payload(nested)
        ):
            return {}
        payload = nested
    if not isinstance(payload, dict):
        return {}

    updates: dict[str, Any] = {}
    model_name = str(payload.get("model") or "").strip()
    if model_name:
        updates["model_name"] = model_name
    if _has_command_metadata(payload):
        details = merge_slash_command_details(
            commands=payload.get("commands"),
            slash_commands=payload.get("slash_commands"),
            skills=payload.get("skills"),
        )
        updates["slash_commands"] = [item["name"] for item in details]
        updates["slash_command_details"] = details
    return updates


def _is_initialization_snapshot_payload(payload: dict[str, Any]) -> bool:
    if not _has_command_metadata(payload):
        return False
    return any(
        key in payload
        for key in (
            "agents",
            "models",
            "output_style",
            "available_output_styles",
            "account",
            "pid",
        )
    )


def _has_command_metadata(payload: dict[str, Any]) -> bool:
    return any(isinstance(payload.get(key), list) for key in ("commands", "slash_commands", "skills"))
