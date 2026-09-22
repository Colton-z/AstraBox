"""Schema vocabulary for the ``session_snapshots`` collection.

One snapshot document per session, written concurrently by three independent
channels (lifecycle / conversation / terminal). Each channel owns a disjoint
set of fields and its own monotonic watermark; the ownership map below is the
single source of truth for that split. The DAL repository enforces it on every
write (``validate_channel_ownership``), and the session-kernel projections are
the writers that must stay within it.

This module is a pure leaf: no imports beyond the stdlib, so both the DAL
layer and the kernel can depend on it without an upward or circular
dependency. Importing kernel internals here for this vocabulary creates an
import-order-dependent cycle — see ``session_kernel/__init__.py``.
"""

from __future__ import annotations

SESSION_SNAPSHOT_FIELD_OWNERSHIP: dict[str, frozenset[str]] = {
    "lifecycle": frozenset(
        {
            "session_lifecycle_state",
            "runtime_connectivity_state",
            "permission_mode",
            "agent_binding",
            "last_error",
            "startup_progress",
            "degraded_channels.lifecycle",
        }
    ),
    "conversation": frozenset(
        {
            "conversation_state",
            "current_turn_id",
            "current_turn_worker_command_id",
            "current_turn_remote_anchor",
            "current_turn_engine_anchor",
            "turn_recovery_phase",
            "interrupt_requested",
            "interrupt_requested_at",
            "active_interaction_id",
            "delivery_state",
            "last_turn_id",
            "last_turn_status",
            "last_turn_failure_phase",
            "last_turn_terminal_reason",
            "last_turn_error",
            "last_turn_command_id",
            "last_turn_terminal_frame",
            "worker_heartbeat_at",
            "degraded_channels.conversation",
        }
    ),
    "terminal": frozenset(
        {
            "terminal_state",
            "terminal_exit_reason",
            "terminal_cwd",
            "active_terminal_command_id",
            "active_terminal_execution_id",
            "terminal_pty_session_id",
            "degraded_channels.terminal",
        }
    ),
}

SESSION_SNAPSHOT_WATERMARK_FIELDS: dict[str, str] = {
    "conversation": "conversation_event_seq_applied",
    "terminal": "terminal_event_seq_applied",
    "lifecycle": "lifecycle_event_seq_applied",
}


def owned_fields_for_channel(channel: str) -> frozenset[str]:
    fields = SESSION_SNAPSHOT_FIELD_OWNERSHIP.get(str(channel or "").strip())
    if not fields:
        raise ValueError(f"unsupported session snapshot channel: {channel}")
    return fields


def watermark_field_for_channel(channel: str) -> str:
    field = SESSION_SNAPSHOT_WATERMARK_FIELDS.get(str(channel or "").strip())
    if not field:
        raise ValueError(f"unsupported session snapshot channel: {channel}")
    return field


def validate_channel_ownership(channel: str, update_keys: set[str]) -> None:
    """Raise ValueError if any key in *update_keys* is not owned by *channel*.

    Watermark and bookkeeping fields (updated_at, created_at, session_id,
    _id) are always allowed.
    """
    allowed = owned_fields_for_channel(channel)
    _ALWAYS_ALLOWED = frozenset({
        "updated_at", "created_at", "session_id", "_id",
    })
    watermark = SESSION_SNAPSHOT_WATERMARK_FIELDS.get(channel)
    violations = update_keys - allowed - _ALWAYS_ALLOWED
    if watermark:
        violations -= {watermark}
    if violations:
        raise ValueError(
            f"channel '{channel}' attempted to write fields owned by another channel: "
            f"{sorted(violations)}"
        )
