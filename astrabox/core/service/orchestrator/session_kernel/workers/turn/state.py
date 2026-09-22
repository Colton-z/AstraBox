from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

@dataclass
class _BridgeRunState:
    """Per-invocation mutable state threaded through ``_run_bridge_command``.

    Holds exactly the ``_run_bridge_command`` locals that are shared across its
    nested closures — every local declared ``nonlocal`` in a closure, plus the
    handful that a closure reads after the outer body rebinds them.
    """

    # --- frame sequencing / lifecycle flags ---
    frame_seq: int | None = None
    live_frame_seq: int = 0
    started: bool = False
    dispatch_confirmed: bool = False
    dispatch_event_written: bool = False
    answer_projection_written: bool = False
    turn_settled: bool = False
    waiting_for_interaction: bool = False
    #: Quiet intervals survived back-to-back on a stream that is still open.
    #: Bounds how long the reader waits for a terminal that may never come.
    consecutive_quiet_intervals: int = 0
    #: The bridge is parked inside a fault barrier. The engine stream may end
    #: meanwhile; the turn is still owned, so its heartbeat keeps renewing.
    frame_hold_active: bool = False

    # --- turn identity / event cursor (runtime-initialised) ---
    effective_turn_id: str = ""
    last_event_seq: int = 0

    # --- terminal / result signals ---
    last_assistant_text: str = ""
    last_error_text: str = ""
    saw_error_event: bool = False
    saw_result_frame: bool = False
    saw_public_result_frame: bool = False
    saw_mirror_terminal_evidence: bool = False
    last_result_data: dict[str, Any] | None = None
    last_terminal_reason: str | None = None
    terminal_frame_proof: dict[str, Any] | None = None

    # --- rich-projection accumulators ---
    accumulated_thinking_parts: list[str] = field(default_factory=list)
    accumulated_tool_uses: dict[str, dict[str, Any]] = field(default_factory=dict)
    accumulated_tool_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    background_tasks_opened: dict[str, Any] | None = None
    max_observed_sandbox_seq: int | None = None
    parent_mirror_seq_applied: int | None = None

    # --- replay / resume machinery ---
    _resume_attempted: bool = False

    # --- remote / engine anchors (runtime-initialised) ---
    anchor_event_written: bool = False
    current_turn_remote_anchor: dict[str, Any] | None = None
    persisted_current_turn_remote_anchor: dict[str, Any] | None = None
    current_turn_engine_anchor: dict[str, Any] | None = None
    persisted_current_turn_engine_anchor: dict[str, Any] | None = None
    # --- heartbeat ---
    _last_heartbeat_mono: float = 0.0

    # --- async task handles / durable-writer ---
    bridge_stream_task: asyncio.Task | None = None
    durable_frame_writer_task: asyncio.Task | None = None
    durable_frame_writer_error: BaseException | None = None

    # --- pending interaction ---
    pending_interaction: dict[str, Any] | None = None
