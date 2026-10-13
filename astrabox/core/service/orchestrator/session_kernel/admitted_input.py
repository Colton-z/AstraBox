"""Prove that an admitted input still belongs to a live pre-delivery worker."""

from __future__ import annotations

from typing import Any

from astrabox.common.utils.time_utils import parse_iso, utcnow


async def admitted_input_pending(
    journal: Any,
    session_id: str,
    snapshot: dict[str, Any] | None,
    *,
    command_id: str | None = None,
    turn_id: str | None = None,
) -> bool:
    from astrabox.core.service.orchestrator.session_kernel.service_mixins._helpers import (
        _RUNTIME_SUBJECT_REBUILD_READY_BUDGET_SECONDS,
    )
    from astrabox.core.service.orchestrator.session_kernel.workers.reconcile_worker import (
        HEARTBEAT_STALE_S,
    )

    row = snapshot or {}
    command_id = command_id or str(row.get("current_turn_worker_command_id") or "")
    turn_id = turn_id or str(row.get("current_turn_id") or "")
    if (
        not command_id or not turn_id
        or row.get("current_turn_worker_command_id") != command_id
        or row.get("current_turn_id") != turn_id
        or row.get("conversation_state") != "PROCESSING"
        or row.get("delivery_state") != "PENDING"
        or row.get("current_turn_remote_anchor") is not None
        or row.get("current_turn_engine_anchor") is not None
        or row.get("terminal_state") in {"RUNNING", "INTERRUPTING"}
        or row.get("active_interaction_id")
    ):
        return False
    command = await journal.get_command_event(session_id, command_id=command_id)
    payload = (command or {}).get("payload") or {}
    if (
        (command or {}).get("turn_id") != turn_id
        or payload.get("command_type") != "StartTurn"
        or payload.get("admission_only") is not True
        or not payload.get("input_id")
    ):
        return False
    try:
        now = utcnow()
        heartbeat_age = (now - parse_iso(str(row.get("worker_heartbeat_at") or ""))).total_seconds()
        command_age = (now - parse_iso(str((command or {}).get("occurred_at") or ""))).total_seconds()
    except (TypeError, ValueError):
        return False
    if not (0 <= heartbeat_age < HEARTBEAT_STALE_S and 0 <= command_age < _RUNTIME_SUBJECT_REBUILD_READY_BUDGET_SECONDS):
        return False
    evidence = await journal.list_events(
        session_id, turn_id=turn_id,
        event_types=("input.delivered", "input.consumed", "dispatch.confirmed", "turn.failed", "turn.completed"),
        limit=1,
    )
    return not evidence
