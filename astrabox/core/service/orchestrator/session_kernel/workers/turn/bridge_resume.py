"""Bridge event producer for ``TurnWorker._run_bridge_command``.

``_bridge_event_producer`` pumps the engine event stream into the bridge
queue. Module functions take ``(worker, state, ctx)``, the same convention
as :mod:`bridge_frames`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.core.service.orchestrator.stream_errors import IncompleteStreamError
from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    coerce_int as _coerce_int,
    normalize_current_turn_remote_anchor as _normalize_current_turn_remote_anchor,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn import (
    bridge_journal,
    bridge_terminal,
)

if TYPE_CHECKING:
    from astrabox.core.service.orchestrator.session_kernel.workers.turn.state import (
        _BridgeRunState,
    )

logger = get_logger(__name__)

_MIRROR_SEQ_ENTRY_FIELD = "__astrabox_mirror_seq"


async def _bridge_event_producer(worker: Any, state: _BridgeRunState, ctx: Any, event_iter: Any) -> None:
    latency_trace = ctx.latency_trace
    bridge_event_queue = ctx.bridge_event_queue
    try:
        first_bridge_event = True
        async for event in event_iter:
            if first_bridge_event:
                first_bridge_event = False
                latency_trace.mark(
                    "turn_worker.first_bridge_event_queued",
                    event_type=str(event.get("type") or "").strip() or None,
                )
            await bridge_event_queue.put(("event", dict(event)))
            # asyncio.Queue.put() on an unbounded queue usually
            # completes without suspending.  Yield here so live
            # model deltas are not queued into large internal
            # bursts before the consumer can publish them.
            await asyncio.sleep(0)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await bridge_event_queue.put(("error", exc))
    finally:
        await bridge_event_queue.put(("done", None))
