"""Durable projections shared by live and reconnected engine emissions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    build_turn_waiting_snapshot_updates,
)


logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class OpenInteractionProjection:
    """Outcome of projecting one adapter-declared interaction."""

    event_seq: int
    snapshot: dict[str, Any] | None
    waiting: bool
    fenced: bool


async def project_engine_interaction_opened(
    *,
    session_events_repo: Any,
    session_snapshots_repo: Any,
    interaction_snapshots_repo: Any,
    session_id: str,
    turn_id: str,
    command_id: str,
    correlation_id: str,
    pending: dict[str, Any],
    tool_name: str,
    source_event_seq_applied: int,
    current_turn_remote_anchor: dict[str, Any] | None = None,
    current_turn_engine_anchor: dict[str, Any] | None = None,
) -> OpenInteractionProjection:
    """Commit one interaction through its event, semantic and Session views."""

    interaction_id = str(pending.get("interaction_id") or "").strip()
    if not interaction_id:
        raise RuntimeError("cannot open an interaction without interaction_id")
    if not turn_id:
        raise RuntimeError(
            "cannot open interaction without resolved turn_id: "
            f"session={session_id} interaction={interaction_id}"
        )

    known_interaction = await interaction_snapshots_repo.get_interaction(
        session_id,
        interaction_id,
    )

    async def repair_answered_interaction_projection(
        answered_interaction: dict[str, Any] | None,
        *,
        event_seq: int,
    ) -> None:
        resolved_tool_call_id = (
            str(pending.get("tool_call_id") or "").strip()
            or str((answered_interaction or {}).get("tool_call_id") or "").strip()
            or None
        )
        await interaction_snapshots_repo.project_answered_interaction(
            session_id=session_id,
            interaction_id=interaction_id,
            source_event_seq_applied=event_seq,
            updates={
                "turn_id": str((answered_interaction or {}).get("turn_id") or "").strip()
                or str(pending.get("turn_id") or "").strip()
                or None,
                "presentation": str(
                    (answered_interaction or {}).get("presentation")
                    or pending.get("presentation")
                    or ""
                ).strip()
                or None,
                "tool_name": str(
                    (answered_interaction or {}).get("tool_name")
                    or pending.get("tool_name")
                    or tool_name
                    or ""
                ).strip(),
                "tool_call_id": resolved_tool_call_id,
                "raw_input": dict(
                    pending.get("raw_input")
                    or (answered_interaction or {}).get("raw_input")
                    or {}
                ),
                "response": dict((answered_interaction or {}).get("response") or {}),
                "sandbox_turn_id": (
                    pending.get("sandbox_turn_id")
                    if isinstance(pending.get("sandbox_turn_id"), int)
                    else (answered_interaction or {}).get("sandbox_turn_id")
                ),
                "last_sandbox_seq": (
                    pending.get("last_sandbox_seq")
                    if isinstance(pending.get("last_sandbox_seq"), int)
                    else (answered_interaction or {}).get("last_sandbox_seq")
                ),
            },
        )

    known_state = str(
        (known_interaction or {}).get("interaction_state") or ""
    ).strip()
    if known_state == "ANSWERED":
        await repair_answered_interaction_projection(
            known_interaction,
            event_seq=source_event_seq_applied,
        )
        logger.info(
            "ignored reopened answered interaction session=%s turn=%s "
            "interaction=%s tool=%s",
            session_id,
            turn_id,
            interaction_id,
            tool_name,
        )
        return OpenInteractionProjection(
            event_seq=source_event_seq_applied,
            snapshot=None,
            waiting=False,
            fenced=False,
        )

    known_turn_id = str((known_interaction or {}).get("turn_id") or "").strip()
    if known_turn_id and known_turn_id != turn_id:
        raise RuntimeError(
            f"interaction {interaction_id} already OPEN on turn {known_turn_id}, "
            f"cannot reopen on turn {turn_id}"
        )

    event, created = await session_events_repo.try_claim_event(
        {
            "session_id": session_id,
            "channel": "conversation",
            "turn_id": turn_id,
            "event_type": "turn.awaiting_interaction",
            "causation_id": f"io:{session_id}:{turn_id}:{interaction_id}",
            "correlation_id": correlation_id,
            "payload": {**dict(pending), "command_id": command_id},
        }
    )
    event_seq = int(event.get("event_seq") or source_event_seq_applied)
    projected = await interaction_snapshots_repo.project_open_interaction(
        session_id=session_id,
        interaction_id=interaction_id,
        source_event_seq_applied=event_seq,
        updates={
            **dict(pending),
            "turn_id": turn_id,
            "interaction_state": "OPEN",
            "raw_input": dict(pending.get("raw_input") or {}),
            "active": True,
        },
    )
    if (
        isinstance(projected, dict)
        and (
            str(projected.get("interaction_state") or "").strip() == "ANSWERED"
            or not projected.get("active")
        )
    ):
        await repair_answered_interaction_projection(
            projected,
            event_seq=event_seq,
        )
        logger.info(
            "ignored reopened answered interaction after semantic projection "
            "session=%s turn=%s interaction=%s tool=%s",
            session_id,
            turn_id,
            interaction_id,
            tool_name,
        )
        return OpenInteractionProjection(
            event_seq=event_seq,
            snapshot=None,
            waiting=False,
            fenced=False,
        )

    snapshot = await session_snapshots_repo.apply_channel_update(
        session_id,
        channel="conversation",
        event_seq=event_seq,
        updates=build_turn_waiting_snapshot_updates(
            turn_id=turn_id,
            interaction_id=interaction_id,
            current_turn_remote_anchor=current_turn_remote_anchor,
            current_turn_engine_anchor=current_turn_engine_anchor,
        ),
        extra_filter={"current_turn_id": turn_id},
    )
    return OpenInteractionProjection(
        event_seq=event_seq,
        snapshot=dict(snapshot) if isinstance(snapshot, dict) else None,
        waiting=True,
        fenced=snapshot is None and created,
    )


async def record_engine_background_tasks_opened(
    *,
    session_events_repo: Any,
    session_id: str,
    turn_id: str,
    command_id: str,
    correlation_id: str,
    engine_kind: str,
    manifest: dict[str, Any],
) -> None:
    """Persist the adapter's opaque detached-child continuation manifest."""

    if not engine_kind:
        raise RuntimeError("background-task manifest has no engine kind")
    await session_events_repo.try_claim_event(
        {
            "session_id": session_id,
            "channel": "conversation",
            "turn_id": turn_id,
            "event_type": "turn.background_tasks_opened",
            "causation_id": f"{command_id}:background-continuation",
            "correlation_id": correlation_id,
            "payload": {
                "command_id": command_id,
                "source": f"{engine_kind}_background_task",
                **manifest,
            },
        }
    )
