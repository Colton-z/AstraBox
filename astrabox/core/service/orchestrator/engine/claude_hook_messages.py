"""Claude's user-facing startup hook output, separate from model replies."""

from __future__ import annotations

import json
from typing import Any

from astrabox.core.service.orchestrator.engine.emissions import SessionMessageFact


def session_start_message(message: dict[str, Any]) -> SessionMessageFact | None:
    """Project the SDK hook response using its native event identity."""
    if (
        message.get("__sdk_type") != "HookEventMessage"
        or message.get("subtype") != "hook_response"
        or message.get("hook_event_name") != "SessionStart"
    ):
        return None
    data = message.get("data")
    output = data.get("output") if isinstance(data, dict) else None
    if not isinstance(output, str) or not output.strip():
        return None
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        # Successful hooks may return plain stdout without a display message.
        return None
    if not isinstance(payload, dict) or "systemMessage" not in payload:
        return None
    content = payload["systemMessage"]
    event_id = message.get("uuid")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("SessionStart systemMessage must be nonempty text")
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("SessionStart systemMessage requires its native event uuid")
    return SessionMessageFact(f"sdk-hook-system-message:{event_id}", content)
