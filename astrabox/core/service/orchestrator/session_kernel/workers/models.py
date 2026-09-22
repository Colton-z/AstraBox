from __future__ import annotations

from typing import Any, Literal, Protocol

WorkerChannel = Literal["conversation", "lifecycle", "terminal"]
WorkerStatus = Literal["idle", "running", "degraded", "terminated"]


class WorkerWakeup:
    """Scheduling hint that wakes a worker after a durable command append."""

    __slots__ = (
        "session_id",
        "channel",
        "command_id",
        "turn_id",
        "reason",
    )

    def __init__(
        self,
        *,
        session_id: str,
        channel: WorkerChannel,
        command_id: str,
        turn_id: str | None = None,
        reason: str = "command_appended",
    ) -> None:
        self.session_id = session_id
        self.channel = channel
        self.command_id = command_id
        self.turn_id = turn_id
        self.reason = reason


class WorkerOutcome:
    """Summary of a worker pass over one session channel."""

    __slots__ = (
        "session_id",
        "channel",
        "status",
        "processed_event_seq",
        "error_text",
        "metadata",
    )

    def __init__(
        self,
        *,
        session_id: str,
        channel: WorkerChannel,
        status: WorkerStatus,
        processed_event_seq: int | None = None,
        error_text: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.session_id = session_id
        self.channel = channel
        self.status = status
        self.processed_event_seq = processed_event_seq
        self.error_text = error_text
        self.metadata = dict(metadata or {})


class KernelWorker(Protocol):
    """Authoritative writer for one session channel."""

    worker_name: str
    channel: WorkerChannel

    async def run(self, wakeup: WorkerWakeup) -> WorkerOutcome:
        """Process a wakeup until the channel reaches a stable state."""
