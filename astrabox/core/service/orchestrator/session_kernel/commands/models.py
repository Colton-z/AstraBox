from __future__ import annotations

from typing import Any, ClassVar, Literal, Protocol

CommandChannel = Literal["conversation", "interaction", "lifecycle", "terminal"]


class CommandEnvelope:
    """Persisted command-journal envelope."""

    __slots__ = (
        "command_id",
        "session_id",
        "command_type",
        "channel",
        "payload",
        "turn_id",
        "causation_id",
        "correlation_id",
    )

    def __init__(
        self,
        *,
        command_id: str,
        session_id: str,
        command_type: str,
        channel: CommandChannel,
        payload: dict[str, Any] | None = None,
        turn_id: str | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        self.command_id = command_id
        self.session_id = session_id
        self.command_type = command_type
        self.channel = channel
        self.payload = dict(payload or {})
        self.turn_id = turn_id
        self.causation_id = causation_id
        self.correlation_id = correlation_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "session_id": self.session_id,
            "command_type": self.command_type,
            "channel": self.channel,
            "payload": dict(self.payload),
            "turn_id": self.turn_id,
            "causation_id": self.causation_id,
            "correlation_id": self.correlation_id,
        }


class KernelCommand(Protocol):
    """Protocol shared by all durable Session Kernel commands."""

    command_type: str
    session_id: str
    turn_id: str | None
    causation_id: str | None
    correlation_id: str | None

    def to_payload(self) -> dict[str, Any]:
        """Return the command-specific payload that is stored in the journal."""


class BaseCommand:
    """Common fields for commands that target a session."""

    command_type: ClassVar[str] = "BaseCommand"
    __slots__ = (
        "session_id",
        "turn_id",
        "causation_id",
        "correlation_id",
    )

    def __init__(
        self,
        *,
        session_id: str,
        turn_id: str | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        self.session_id = session_id
        self.turn_id = turn_id
        self.causation_id = causation_id
        self.correlation_id = correlation_id

    def to_payload(self) -> dict[str, Any]:
        return {}

    def to_envelope(self, command_id: str, *, channel: CommandChannel) -> CommandEnvelope:
        return CommandEnvelope(
            command_id=command_id,
            session_id=self.session_id,
            command_type=self.command_type,
            channel=channel,
            payload=self.to_payload(),
            turn_id=self.turn_id,
            causation_id=self.causation_id,
            correlation_id=self.correlation_id,
        )


class CreateSessionCommand(BaseCommand):
    """Create a session with its owner and resolved runtime configuration."""

    command_type: ClassVar[str] = "CreateSession"
    __slots__ = BaseCommand.__slots__ + (
        "template_name",
        "owner_user_id",
        "runtime_template_name",
        "session_kind",
        "workspace_ref",
    )

    def __init__(
        self,
        *,
        session_id: str,
        template_name: str = "",
        owner_user_id: str | None = None,
        runtime_template_name: str | None = None,
        session_kind: str = "agent_chat",
        workspace_ref: dict[str, Any] | None = None,
        turn_id: str | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        super().__init__(
            session_id=session_id,
            turn_id=turn_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
        )
        self.template_name = template_name
        self.owner_user_id = owner_user_id
        self.runtime_template_name = runtime_template_name
        self.session_kind = session_kind
        self.workspace_ref = dict(workspace_ref) if workspace_ref else None

    def to_payload(self) -> dict[str, Any]:
        return {
            "template_name": self.template_name,
            "owner_user_id": self.owner_user_id,
            "runtime_template_name": self.runtime_template_name,
            "session_kind": self.session_kind,
            "workspace_ref": dict(self.workspace_ref) if self.workspace_ref else None,
        }


class StartTurnCommand(BaseCommand):
    """Start a new conversation turn."""

    command_type: ClassVar[str] = "StartTurn"
    __slots__ = BaseCommand.__slots__ + (
        "content",
        "interaction_response",
        "permission_mode",
        "author_user_id",
    )

    def __init__(
        self,
        *,
        session_id: str,
        content: str = "",
        interaction_response: dict[str, Any] | None = None,
        permission_mode: str | None = None,
        author_user_id: str | None = None,
        turn_id: str | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        super().__init__(
            session_id=session_id,
            turn_id=turn_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
        )
        self.content = content
        self.interaction_response = dict(interaction_response) if interaction_response else None
        self.permission_mode = permission_mode
        self.author_user_id = author_user_id

    def to_payload(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "interaction_response": dict(self.interaction_response) if self.interaction_response else None,
            "permission_mode": self.permission_mode,
            "author_user_id": self.author_user_id,
        }


class AnswerInteractionCommand(BaseCommand):
    """Persist an answer to a pending interaction."""

    command_type: ClassVar[str] = "AnswerInteraction"
    __slots__ = BaseCommand.__slots__ + (
        "interaction_id",
        "answer",
        "raw_response",
        "answered_by",
    )

    def __init__(
        self,
        *,
        session_id: str,
        interaction_id: str = "",
        answer: str = "",
        raw_response: dict[str, Any] | None = None,
        answered_by: str | None = None,
        turn_id: str | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        super().__init__(
            session_id=session_id,
            turn_id=turn_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
        )
        self.interaction_id = interaction_id
        self.answer = answer
        self.raw_response = dict(raw_response) if raw_response else None
        self.answered_by = answered_by

    def to_payload(self) -> dict[str, Any]:
        return {
            "interaction_id": self.interaction_id,
            "answer": self.answer,
            "raw_response": dict(self.raw_response) if self.raw_response else None,
            "answered_by": self.answered_by,
        }


class InterruptTurnCommand(BaseCommand):
    """Request the authoritative runtime to interrupt the active turn."""

    command_type: ClassVar[str] = "InterruptTurn"
    __slots__ = BaseCommand.__slots__ + ("reason",)

    def __init__(
        self,
        *,
        session_id: str,
        reason: str | None = None,
        turn_id: str | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        super().__init__(
            session_id=session_id,
            turn_id=turn_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
        )
        self.reason = reason

    def to_payload(self) -> dict[str, Any]:
        return {"reason": self.reason}


class SetPermissionModeCommand(BaseCommand):
    """Switch the runtime permission mode for the session."""

    command_type: ClassVar[str] = "SetPermissionMode"
    __slots__ = BaseCommand.__slots__ + ("permission_mode", "updated_by")

    def __init__(
        self,
        *,
        session_id: str,
        permission_mode: str = "",
        updated_by: str | None = None,
        turn_id: str | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        super().__init__(
            session_id=session_id,
            turn_id=turn_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
        )
        self.permission_mode = permission_mode
        self.updated_by = updated_by

    def to_payload(self) -> dict[str, Any]:
        return {
            "permission_mode": self.permission_mode,
            "updated_by": self.updated_by,
        }


class RecoverSessionCommand(BaseCommand):
    """Request a session recovery pass after runtime loss or degradation."""

    command_type: ClassVar[str] = "RecoverSession"
    __slots__ = BaseCommand.__slots__ + ("recovery_reason", "last_known_command_id")

    def __init__(
        self,
        *,
        session_id: str,
        recovery_reason: str | None = None,
        last_known_command_id: str | None = None,
        turn_id: str | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        super().__init__(
            session_id=session_id,
            turn_id=turn_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
        )
        self.recovery_reason = recovery_reason
        self.last_known_command_id = last_known_command_id

    def to_payload(self) -> dict[str, Any]:
        return {
            "recovery_reason": self.recovery_reason,
            "last_known_command_id": self.last_known_command_id,
        }


class TerminateSessionCommand(BaseCommand):
    """Terminate the runtime and mark the session as ended."""

    command_type: ClassVar[str] = "TerminateSession"
    __slots__ = BaseCommand.__slots__ + ("termination_reason",)

    def __init__(
        self,
        *,
        session_id: str,
        termination_reason: str | None = None,
        turn_id: str | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        super().__init__(
            session_id=session_id,
            turn_id=turn_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
        )
        self.termination_reason = termination_reason

    def to_payload(self) -> dict[str, Any]:
        return {"termination_reason": self.termination_reason}


class DeleteSessionCommand(BaseCommand):
    """Soft-delete a session from the durable session registry."""

    command_type: ClassVar[str] = "DeleteSession"
    __slots__ = BaseCommand.__slots__ + ("deleted_by", "delete_reason")

    def __init__(
        self,
        *,
        session_id: str,
        deleted_by: str | None = None,
        delete_reason: str | None = None,
        turn_id: str | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        super().__init__(
            session_id=session_id,
            turn_id=turn_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
        )
        self.deleted_by = deleted_by
        self.delete_reason = delete_reason

    def to_payload(self) -> dict[str, Any]:
        return {
            "deleted_by": self.deleted_by,
            "delete_reason": self.delete_reason,
        }


class RunTerminalCommand(BaseCommand):
    """Execute a terminal command in the dedicated terminal channel."""

    command_type: ClassVar[str] = "RunTerminalCommand"
    __slots__ = BaseCommand.__slots__ + ("command", "cwd", "timeout_seconds")

    def __init__(
        self,
        *,
        session_id: str,
        command: str = "",
        cwd: str | None = None,
        timeout_seconds: int | None = None,
        turn_id: str | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        super().__init__(
            session_id=session_id,
            turn_id=turn_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
        )
        self.command = command
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds

    def to_payload(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "cwd": self.cwd,
            "timeout_seconds": self.timeout_seconds,
        }
