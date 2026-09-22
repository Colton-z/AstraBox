from __future__ import annotations

from typing import Any, Literal, Mapping, Protocol

ProjectionChannel = Literal["conversation", "lifecycle", "terminal"]


class ProjectionWatermark:
    """Last applied source event position for one projection channel."""

    __slots__ = (
        "session_id",
        "channel",
        "source_event_seq_applied",
        "source_channel_seq_applied",
        "updated_at",
    )

    def __init__(
        self,
        *,
        session_id: str,
        channel: ProjectionChannel,
        source_event_seq_applied: int,
        source_channel_seq_applied: int | None = None,
        updated_at: str | None = None,
    ) -> None:
        self.session_id = session_id
        self.channel = channel
        self.source_event_seq_applied = source_event_seq_applied
        self.source_channel_seq_applied = source_channel_seq_applied
        self.updated_at = updated_at


class SessionSnapshot:
    """Durable read model — the sole authoritative source for session state.

    ``sessions`` collection stores only metadata (sandbox_id, user_id, etc.).
    All runtime state lives here, partitioned by channel ownership.
    """

    __slots__ = (
        "session_id",
        # lifecycle channel (lifecycle_worker owns)
        "session_lifecycle_state",
        "runtime_connectivity_state",
        "permission_mode",
        "agent_binding",
        "last_error",
        "startup_progress",
        # conversation channel (turn_worker owns)
        "conversation_state",
        "current_turn_id",
        "current_turn_remote_anchor",
        "current_turn_engine_anchor",
        "turn_recovery_phase",
        "active_interaction_id",
        "last_turn_id",
        "last_turn_status",
        "last_turn_error",
        "last_turn_command_id",
        "last_turn_terminal_frame",
        # terminal channel (terminal_worker owns)
        "terminal_state",
        "terminal_exit_reason",
        "terminal_cwd",
        "active_terminal_command_id",
        "active_terminal_execution_id",
        "terminal_pty_session_id",
        # bookkeeping
        "watermarks",
        "updated_at",
    )

    def __init__(
        self,
        *,
        session_id: str,
        session_lifecycle_state: str = "CREATING",
        runtime_connectivity_state: str = "DISCONNECTED",
        permission_mode: str | None = None,
        agent_binding: dict[str, Any] | None = None,
        last_error: str | None = None,
        startup_progress: str | None = None,
        conversation_state: str = "IDLE",
        current_turn_id: str | None = None,
        current_turn_remote_anchor: dict[str, Any] | None = None,
        current_turn_engine_anchor: dict[str, Any] | None = None,
        turn_recovery_phase: str | None = None,
        active_interaction_id: str | None = None,
        last_turn_id: str | None = None,
        last_turn_status: str | None = None,
        last_turn_error: str | None = None,
        last_turn_command_id: str | None = None,
        last_turn_terminal_frame: dict[str, Any] | None = None,
        terminal_state: str = "IDLE",
        terminal_exit_reason: str | None = None,
        terminal_cwd: str | None = None,
        active_terminal_command_id: str | None = None,
        active_terminal_execution_id: str | None = None,
        terminal_pty_session_id: str | None = None,
        watermarks: dict[str, ProjectionWatermark] | None = None,
        updated_at: str | None = None,
    ) -> None:
        self.session_id = session_id
        self.session_lifecycle_state = session_lifecycle_state
        self.runtime_connectivity_state = runtime_connectivity_state
        self.permission_mode = permission_mode
        self.agent_binding = dict(agent_binding or {})
        self.last_error = last_error
        self.startup_progress = startup_progress
        self.conversation_state = conversation_state
        self.current_turn_id = current_turn_id
        self.current_turn_remote_anchor = (
            dict(current_turn_remote_anchor)
            if isinstance(current_turn_remote_anchor, dict)
            else None
        )
        self.current_turn_engine_anchor = (
            dict(current_turn_engine_anchor)
            if isinstance(current_turn_engine_anchor, dict)
            else None
        )
        self.turn_recovery_phase = turn_recovery_phase
        self.active_interaction_id = active_interaction_id
        self.last_turn_id = last_turn_id
        self.last_turn_status = last_turn_status
        self.last_turn_error = last_turn_error
        self.last_turn_command_id = last_turn_command_id
        self.last_turn_terminal_frame = (
            dict(last_turn_terminal_frame)
            if isinstance(last_turn_terminal_frame, dict)
            else None
        )
        self.terminal_state = terminal_state
        self.terminal_exit_reason = terminal_exit_reason
        self.terminal_cwd = terminal_cwd
        self.active_terminal_command_id = active_terminal_command_id
        self.active_terminal_execution_id = active_terminal_execution_id
        self.terminal_pty_session_id = terminal_pty_session_id
        self.watermarks = dict(watermarks or {})
        self.updated_at = updated_at


class TurnSnapshot:
    """Durable view of one logical turn."""

    __slots__ = (
        "session_id",
        "turn_id",
        "turn_state",
        "assistant_text",
        "pending_interaction_id",
        "last_event_seq",
        "created_at",
        "updated_at",
    )

    def __init__(
        self,
        *,
        session_id: str,
        turn_id: str,
        turn_state: str = "REQUESTED",
        assistant_text: str | None = None,
        pending_interaction_id: str | None = None,
        last_event_seq: int | None = None,
        created_at: str | None = None,
        updated_at: str | None = None,
    ) -> None:
        self.session_id = session_id
        self.turn_id = turn_id
        self.turn_state = turn_state
        self.assistant_text = assistant_text
        self.pending_interaction_id = pending_interaction_id
        self.last_event_seq = last_event_seq
        self.created_at = created_at
        self.updated_at = updated_at


class ArtifactRecord:
    """Generic durable artifact emitted by a worker."""

    __slots__ = (
        "session_id",
        "artifact_id",
        "artifact_type",
        "turn_id",
        "payload",
        "created_at",
    )

    def __init__(
        self,
        *,
        session_id: str,
        artifact_id: str,
        artifact_type: str,
        turn_id: str | None = None,
        payload: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> None:
        self.session_id = session_id
        self.artifact_id = artifact_id
        self.artifact_type = artifact_type
        self.turn_id = turn_id
        self.payload = dict(payload or {})
        self.created_at = created_at


class ProjectionWriter(Protocol):
    """Projection contract for synchronous read-model updates."""

    async def apply(self, event: Mapping[str, Any]) -> None:
        """Apply one canonical event to the read model."""

    async def rebuild(self, session_id: str) -> None:
        """Rebuild the projection for a session from the canonical journal."""
