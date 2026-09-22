"""Characterization pins for the quiesce/disconnect invariants.

Target: ``RemoteAgentRuntimeManager`` in
``astrabox/core/service/orchestrator/runtime_manager.py`` — the shutdown
quiesce chain (``quiesce`` → ``_disconnect_runtimes_for_quiesce`` →
``_close_runtime_turn_client``).

The tests cover this shutdown contract:

* ``quiesce`` is idempotent (first reason wins), snapshots+clears
  ``_runtimes``, cancels in-flight turns, and closes each runtime's
  engine client; repeating the close on the same runtime is safe.

All fast, deterministic, no docker/network.
"""

from __future__ import annotations

import asyncio
import unittest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.runtime_manager import (
    RemoteAgentRuntimeManager,
    SessionRuntime,
)


def _claude_runtime(*, session_id: str = "s-1", **kwargs) -> SessionRuntime:
    return SessionRuntime(
        session_id=session_id,
        agent=None,
        engine_kind="claude_code",
        **kwargs,
    )


class _RecordingEngineClient:
    def __init__(self, log: list[str]) -> None:
        self._log = log

    async def close(self) -> None:
        self._log.append("engine_client.close")


class QuiesceIdempotencyTests(unittest.TestCase):
    def test_quiesce_is_idempotent_first_reason_wins(self) -> None:
        # A second quiesce with a different reason is a no-op: the first reason
        # is retained (shutdown is a one-shot latch).
        mgr = RemoteAgentRuntimeManager()
        mgr.quiesce(reason="first")
        self.assertEqual(mgr._quiesced_reason, "first")

        mgr.quiesce(reason="second")
        self.assertEqual(mgr._quiesced_reason, "first")

        # ...and once quiesced, the entry gate rejects new work loudly.
        with self.assertRaises(APIError) as ctx:
            mgr._raise_if_quiesced()
        self.assertEqual(ctx.exception.code, "AGENT_RUNTIME_RELEASING")
        self.assertEqual(ctx.exception.status_code, 503)


# ── quiesce/disconnect ordering + double-disconnect ──────────────────────────


class QuiesceDisconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_quiesce_snapshots_clears_and_cancels_inflight_turn(self) -> None:
        # quiesce() sets the reason, snapshots + CLEARS _runtimes (and the
        # interaction probe cache), and cancels every in-flight turn — all
        # synchronously before the background disconnect chain runs.
        mgr = RemoteAgentRuntimeManager()
        turn_started = asyncio.Event()

        async def _turn() -> None:
            turn_started.set()
            await asyncio.Event().wait()  # a live, never-completing turn

        turn_task = asyncio.create_task(_turn())
        await turn_started.wait()

        runtime = _claude_runtime(session_id="s-1")
        runtime.current_task = turn_task
        mgr._runtimes["s-1"] = runtime

        mgr.quiesce(reason="deploy")

        self.assertEqual(mgr._quiesced_reason, "deploy")
        self.assertEqual(dict(mgr._runtimes), {})

        # The in-flight turn is cancelled by quiesce.
        with self.assertRaises(asyncio.CancelledError):
            await turn_task
        self.assertTrue(turn_task.cancelled())
        # Drain the scheduled background disconnect task.
        await asyncio.sleep(0.02)

    async def test_close_runtime_turn_client_closes_engine_client(self) -> None:
        # _close_runtime_turn_client closes the runtime's engine client — the
        # only turn client a runtime carries.
        log: list[str] = []
        runtime = _claude_runtime(session_id="s-1")
        runtime.engine_client = _RecordingEngineClient(log)

        await RemoteAgentRuntimeManager._close_runtime_turn_client(runtime)

        self.assertEqual(log, ["engine_client.close"])

    async def test_double_disconnect_is_safe(self) -> None:
        # Closing the SAME runtime twice is safe and closes the engine client
        # both times, raising nothing.
        log: list[str] = []
        runtime = _claude_runtime(session_id="s-1")
        runtime.engine_client = _RecordingEngineClient(log)

        await RemoteAgentRuntimeManager._close_runtime_turn_client(runtime)
        log.clear()
        await RemoteAgentRuntimeManager._close_runtime_turn_client(runtime)
        self.assertEqual(log, ["engine_client.close"])


if __name__ == "__main__":
    unittest.main()
