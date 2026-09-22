"""Shared SDK raw-event helpers for stream and transcript projection paths."""

from typing import Any

from astrabox.core.service.orchestrator.assistant_text import is_full_assistant_snapshot
from astrabox.core.service.orchestrator.slash_commands import (
    merge_slash_command_details,
)


def is_result_payload(raw: dict[str, Any]) -> bool:
    payload_type = str(raw.get("type") or "").lower()
    payload_subtype = str(raw.get("subtype") or "").lower()
    return payload_type == "result" or payload_subtype == "result"


def extract_partial_text(raw: dict[str, Any]) -> str:
    if is_full_assistant_snapshot(raw, raw.get("type")):
        return ""

    direct_text = raw.get("text")
    if isinstance(direct_text, str):
        return direct_text

    chunk = raw.get("chunk")
    if isinstance(chunk, dict):
        chunk_text = chunk.get("text")
        if isinstance(chunk_text, str):
            return chunk_text

    content = raw.get("content")
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                chunks.append(block["text"])
        if chunks:
            return "".join(chunks)

    event = raw.get("event")
    if isinstance(event, dict):
        delta = event.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("text"), str):
            return delta["text"]
        if isinstance(event.get("text"), str):
            return event["text"]

    return ""


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


def serialize_message(message: Any) -> dict[str, Any]:
    """Serialize an SDK message object to a plain dict."""
    if message is None:
        return {}

    if isinstance(message, dict):
        return message

    from dataclasses import asdict, is_dataclass
    if is_dataclass(message):
        return asdict(message)

    if hasattr(message, "model_dump"):
        return dict(message.model_dump())

    payload: dict[str, Any] = {}
    for key in dir(message):
        if key.startswith("_"):
            continue
        try:
            value = getattr(message, key)
        except Exception:
            continue
        if callable(value):
            continue
        payload[key] = value
    return payload
