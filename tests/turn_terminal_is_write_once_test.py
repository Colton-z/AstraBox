"""The first durable terminal signal is final for a turn.

Multiple bridge-loop branches call ``_append_terminal_signal``. These tests
exercise conflicting late signals at that shared write boundary and require the
first signal to remain the terminal proof. Accepting a trailing ``finish`` after
an ``error`` would classify a failed turn as completed even though its frames
contain a turn failure and no assistant reply; accepting the reverse order would
replace a completed turn with a late failure. The guard is scoped per turn so a
later turn on the same worker can still settle.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from astrabox.core.service.orchestrator.session_kernel.workers.turn import (
    bridge_journal,
    bridge_terminal,
)

_TURN = "turn-1"
_COMMAND = "cmd-1"


def _state() -> Any:
    return SimpleNamespace(
        effective_turn_id=_TURN,
        terminal_frame_proof=None,
        dispatch_confirmed=True,
        live_frame_seq=0,
        turn_settled=False,
    )


def _ctx() -> Any:
    return SimpleNamespace(
        session_id="s-1",
        command_id=_COMMAND,
        publish_live_frame=AsyncMock(),
    )


class TerminalIsWriteOnceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.appended: list[dict[str, Any]] = []
        self._seq = 0

        async def _append_frame(worker: Any, state: Any, ctx: Any, payload: dict[str, Any], **kw: Any):
            self.appended.append(dict(payload))
            # Shaped like a real durable frame doc: the proof builder needs a
            # frame_seq, and a finish needs its reason, or the normalizer
            # rejects the proof and the write-once guard never sees it.
            self._seq += 1
            return {
                "payload": dict(payload),
                "turn_id": str(state.effective_turn_id or ""),
                "command_id": _COMMAND,
                "frame_seq": self._seq,
            }

        async def _has_type(*args: Any, **kw: Any) -> bool:
            return False

        self._orig_append = bridge_journal._append_frame
        self._orig_has = bridge_terminal._turn_has_durable_frame_type
        bridge_journal._append_frame = _append_frame  # type: ignore[assignment]
        bridge_terminal._turn_has_durable_frame_type = _has_type  # type: ignore[assignment]

    async def asyncTearDown(self) -> None:
        bridge_journal._append_frame = self._orig_append  # type: ignore[assignment]
        bridge_terminal._turn_has_durable_frame_type = self._orig_has  # type: ignore[assignment]

    async def _signal(self, state: Any, ctx: Any, kind: str) -> Any:
        return await bridge_terminal._append_terminal_signal(
            object(), state, ctx, kind, error_text="boom", publish=False
        )

    async def test_a_finish_cannot_overwrite_an_error(self) -> None:
        # The first terminal wins, so an engine stream's trailing READY must
        # not settle a failed turn as a success.
        state, ctx = _state(), _ctx()
        await self._signal(state, ctx, "error")
        second = await self._signal(state, ctx, "finish")

        self.assertIsNone(second, "the second terminal must be refused")
        types = [f.get("type") for f in self.appended if f.get("type") in ("error", "finish")]
        self.assertEqual(types, ["error"], "the durable terminal stays the first one")
        self.assertEqual(state.terminal_frame_proof.get("type"), "error")

    async def test_an_error_cannot_overwrite_a_finish_either(self) -> None:
        # Symmetric on purpose: the rule is first-wins, not error-wins. A late
        # error after a settled turn is exactly the "trailing bridge events
        # after the durable terminal" case the loop already refuses to let
        # pollute a finished turn.
        state, ctx = _state(), _ctx()
        await self._signal(state, ctx, "finish")
        second = await self._signal(state, ctx, "error")

        self.assertIsNone(second)
        types = [f.get("type") for f in self.appended if f.get("type") in ("error", "finish")]
        self.assertEqual(types, ["finish"])

    async def test_a_different_turn_still_gets_its_own_terminal(self) -> None:
        # The guard is per turn, not per worker: settling one turn must not
        # silence the next one on the same session.
        state, ctx = _state(), _ctx()
        await self._signal(state, ctx, "finish")
        state.effective_turn_id = "turn-2"
        state.terminal_frame_proof = {
            "type": "finish",
            "turn_id": _TURN,
            "command_id": _COMMAND,
            "frame_seq": 1,
            "finish_reason": "stop",
        }

        second = await self._signal(state, ctx, "finish")
        self.assertIsNotNone(second, "a new turn's terminal is not a duplicate")


if __name__ == "__main__":
    unittest.main()
