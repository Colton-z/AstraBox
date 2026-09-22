"""Remote/engine anchor observation for the turn worker's bridge command.

Each function takes ``(worker, state, ctx)`` — ``worker`` is the
:class:`TurnWorker`, ``state`` is the per-turn :class:`_BridgeRunState`, and
``ctx`` bundles the ``_run_bridge_command`` locals / sibling closures the
bodies need (``session_id``, ``correlation_id``, the loaded ``session``
dict); see
:mod:`bridge_journal` for the same convention.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    coerce_int as _coerce_int,
    normalize_current_turn_engine_anchor as _normalize_current_turn_engine_anchor,
    normalize_current_turn_remote_anchor as _normalize_current_turn_remote_anchor,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn._helpers import (
    _build_current_turn_engine_anchor,
    _build_current_turn_remote_anchor,
)

if TYPE_CHECKING:
    from astrabox.core.service.orchestrator.session_kernel.workers.turn.state import (
        _BridgeRunState,
    )

logger = get_logger(__name__)


async def _observe_engine_anchor(worker: Any, state: _BridgeRunState, ctx: Any, raw_anchor: Any) -> dict[str, Any] | None:
    session_id = ctx.session_id
    if not state.effective_turn_id:
        return state.current_turn_engine_anchor
    observed_anchor = _build_current_turn_engine_anchor(raw_anchor)
    if not isinstance(observed_anchor, dict):
        return state.current_turn_engine_anchor
    current_anchor = _normalize_current_turn_engine_anchor(
        state.current_turn_engine_anchor
    )
    if (
        isinstance(current_anchor, dict)
        and (
            current_anchor.get("engine_kind") != observed_anchor.get("engine_kind")
            or current_anchor.get("engine_turn_id") != observed_anchor.get("engine_turn_id")
        )
    ):
        raise RuntimeError(
            "observed conflicting engine turn anchor for the active turn"
        )

    updated_anchor = dict(current_anchor or observed_anchor)
    sequence_number = _coerce_int(observed_anchor.get("engine_sequence_number"))
    current_sequence = _coerce_int(updated_anchor.get("engine_sequence_number"))
    if sequence_number is not None and (
        current_sequence is None or sequence_number > current_sequence
    ):
        updated_anchor["engine_sequence_number"] = int(sequence_number)
    if observed_anchor.get("engine_session_key"):
        updated_anchor["engine_session_key"] = observed_anchor["engine_session_key"]

    persisted = await worker._session_snapshots_repo.force_update_fields(
        session_id,
        {"current_turn_engine_anchor": dict(updated_anchor)},
        extra_filter={"current_turn_id": state.effective_turn_id},
    )
    if persisted:
        state.persisted_current_turn_engine_anchor = dict(updated_anchor)
    state.current_turn_engine_anchor = dict(updated_anchor)
    return state.current_turn_engine_anchor
