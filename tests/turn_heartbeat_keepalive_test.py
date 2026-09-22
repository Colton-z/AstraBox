"""The turn heartbeat is also the sandbox keepalive.

The same tick that keeps a turn "processing" renews the sandbox's backend
lease, so a long-running turn (hours) never has its box reclaimed mid-work by
the runtime TTL. When the heartbeat stops — turn settled, bridge fenced — the
renewal stops with it, and an abandoned sandbox expires naturally after one
lease. The session kernel's bridge heartbeat loop owns both renewals.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from astrabox.core.service.orchestrator.session_kernel.workers.turn import (
    bridge_loop,
)


async def test_each_heartbeat_tick_renews_the_sandbox_lease() -> None:
    renewed: list[str] = []

    class _Manager:
        async def maybe_renew_lease_on_activity(self, session_id: str) -> None:
            renewed.append(session_id)

    worker = SimpleNamespace(
        _worker_heartbeat_interval_s=0.001,
        _runtime_manager=_Manager(),
    )
    state = SimpleNamespace(effective_turn_id="turn-1")
    ctx = SimpleNamespace(session_id="sess-1")

    beats = iter([True, True, False])  # two live ticks, then the turn settles

    async def _fake_write_heartbeat(
        _worker: object, _state: object, _ctx: object, *, raise_on_fence: bool
    ) -> bool:
        return next(beats)

    with (
        patch.object(bridge_loop, "_bridge_done_before_terminal", lambda *a: False),
        patch.object(bridge_loop, "_write_heartbeat", _fake_write_heartbeat),
    ):
        await bridge_loop._turn_heartbeat_loop(worker, state, ctx)

    # Renewed on every tick that kept the turn alive — and NOT on the tick
    # that ended it: renewal is strictly scoped to active processing.
    assert renewed == ["sess-1", "sess-1"]


async def test_a_renew_failure_never_breaks_the_heartbeat() -> None:
    calls: list[str] = []

    class _FailingManager:
        async def maybe_renew_lease_on_activity(self, session_id: str) -> None:
            calls.append(session_id)
            raise RuntimeError("backend renew endpoint down")

    worker = SimpleNamespace(
        _worker_heartbeat_interval_s=0.001,
        _runtime_manager=_FailingManager(),
    )
    state = SimpleNamespace(effective_turn_id="turn-1")
    ctx = SimpleNamespace(session_id="sess-1")

    beats = iter([True, False])

    async def _fake_write_heartbeat(
        _worker: object, _state: object, _ctx: object, *, raise_on_fence: bool
    ) -> bool:
        return next(beats)

    with (
        patch.object(bridge_loop, "_bridge_done_before_terminal", lambda *a: False),
        patch.object(bridge_loop, "_write_heartbeat", _fake_write_heartbeat),
    ):
        # Must run to the settle tick despite the renew raising every time.
        await bridge_loop._turn_heartbeat_loop(worker, state, ctx)

    assert calls == ["sess-1"]


def test_a_bridge_parked_in_a_fault_hold_still_owns_its_turn() -> None:
    """The engine stream can end while the bridge is parked inside a fault
    barrier; the turn is still owned, so the heartbeat must keep renewing —
    otherwise the reconcile worker recovers a turn nobody abandoned."""

    from types import SimpleNamespace

    from astrabox.core.service.orchestrator.session_kernel.workers.turn import bridge_loop
    from astrabox.core.service.orchestrator.session_kernel.workers.turn.state import _BridgeRunState

    state = _BridgeRunState()
    state.bridge_stream_task = SimpleNamespace(done=lambda: True)  # type: ignore[assignment]
    state.turn_settled = False
    assert bridge_loop._bridge_done_before_terminal(None, state, None) is True
    state.frame_hold_active = True
    assert bridge_loop._bridge_done_before_terminal(None, state, None) is False
