from __future__ import annotations

from typing import Any


def build_terminal_session_mirror_updates(
    *,
    state: str,
    projection_result: dict[str, Any] | None,
    base_updates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build sessions mirror updates after terminal projection lands."""

    updates = dict(base_updates or {})
    if state != "READY":
        return updates
    updates.update(
        {
            "state": "READY",
            "runtime_unavailable": False,
            "last_error": None,
            "recovery_policy": None,
            "recovery_reason": None,
            "startup_progress": None,
        }
    )
    active_interaction_id = ""
    if isinstance(projection_result, dict):
        active_interaction_id = str(
            projection_result.get("active_interaction_id") or ""
        ).strip()
    if not active_interaction_id:
        updates["current_turn_id"] = None
        updates["pending_interaction"] = None
    return updates
