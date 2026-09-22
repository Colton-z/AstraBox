"""A session without ``sandbox_id`` takes one named recovery outcome.

No later reconcile tick can attach a runtime when the durable row names no
sandbox. An adapter-owned transcript can still settle the unfinished turn;
resident output reconnect cannot. Neither path weakens the separate mandatory
conversation-resume contract for the next turn.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from astrabox.core.service.orchestrator.session_kernel.service_mixins import (
    durable_recovery_assistant,
)

_SID = "sess-no-sandbox"
_TURN = "turn-1"


class _TranscriptAdapter:
    def slice_recovery_turn(self, raw_items, *, prompt_text):
        return raw_items

    def has_transcript_terminal_evidence(self, raw_items) -> bool:
        return bool(raw_items)

    def project_settled_transcript(self, raw_items, *, done=False):
        return SimpleNamespace(completed=done)


def _host() -> Any:
    class _Host(durable_recovery_assistant.DurableEngineRecoveryMixin):
        pass

    host = _Host.__new__(_Host)
    runtime_manager = SimpleNamespace(
        get_runtime=lambda *a, **k: None,
        get_sandbox_lifecycle_probe=AsyncMock(
            return_value=SimpleNamespace(
                probe_status="PROBE_FAILED",
                sandbox_state=None,
            )
        ),
        _is_terminal_sandbox_lifecycle_probe=Mock(return_value=False),
    )
    host._runtime_manager = runtime_manager
    # The lightweight attach exists and answers None — the row has no sandbox.
    turn_service = AsyncMock()
    turn_service._ensure_runtime_lightweight_for_session = AsyncMock(return_value=None)
    host._turn_service = turn_service
    host._settle_engine_turn_transcript_pending = AsyncMock(
        return_value={"conversation_state": "IDLE", "settled": "mirror"}
    )
    host._fail_engine_turn_unrecoverable = AsyncMock(
        return_value={"conversation_state": "IDLE", "settled": "unrecoverable"}
    )
    return host


def _snapshot(engine_kind: str) -> dict[str, Any]:
    return {
        "current_turn_id": _TURN,
        "current_turn_worker_command_id": "cmd-1",
        "current_turn_engine_anchor": {
            "engine_kind": engine_kind,
            "engine_turn_id": "engine-turn-1",
            "engine_sequence_number": 4,
        },
        "current_turn_remote_anchor": {"sandbox_turn_id": 3, "last_sandbox_seq": 41},
        "conversation_state": "STREAMING",
    }


class RecoveryWithoutSandboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_transcript_settles_without_a_sandbox(self) -> None:
        host = _host()
        with patch.object(
            durable_recovery_assistant,
            "get_engine_adapter",
            return_value=_TranscriptAdapter(),
        ):
            result = await host._recover_engine_via_anchor(
                session={
                    "session_id": _SID,
                    "session_kind": "agent_chat",
                    "sandbox_id": "",
                    "engine_kind": "custom_mirror_engine",
                },
                snapshot=_snapshot("custom_mirror_engine"),
            )
        self.assertIsNotNone(result, "returning None here is the livelock")
        self.assertEqual(result["settled"], "mirror")
        host._fail_engine_turn_unrecoverable.assert_not_awaited()
        kwargs = host._settle_engine_turn_transcript_pending.await_args.kwargs
        self.assertEqual(kwargs["turn_id"], _TURN)
        self.assertEqual(kwargs["engine_kind"], "custom_mirror_engine")
        self.assertEqual(
            kwargs["remote_anchor"],
            {"sandbox_turn_id": 3, "last_sandbox_seq": 41},
            "the mirror lane needs the anchor to find the turn's slice",
        )

    async def test_resident_output_reconnect_without_a_sandbox_is_unrecoverable(self) -> None:
        host = _host()
        with patch.object(
            durable_recovery_assistant,
            "get_engine_adapter",
            return_value=SimpleNamespace(),
        ):
            result = await host._recover_engine_via_anchor(
                session={
                    "session_id": _SID,
                    "session_kind": "agent_chat",
                    "sandbox_id": "",
                    "engine_kind": "custom_replay_engine",
                },
                snapshot=_snapshot("custom_replay_engine"),
            )
        self.assertIsNotNone(result, "returning None here is the livelock")
        self.assertEqual(result["settled"], "unrecoverable")
        host._settle_engine_turn_transcript_pending.assert_not_awaited()
        reason = host._fail_engine_turn_unrecoverable.await_args.kwargs["reason"]
        self.assertIn("sandbox_id", reason)

    async def test_a_present_sandbox_still_retries(self) -> None:
        # The narrow case only. A row that HAS a sandbox whose attach failed is
        # genuinely transient, and settling it would fail turns the next tick
        # would have recovered.
        host = _host()
        with patch.object(
            durable_recovery_assistant,
            "get_engine_adapter",
            return_value=SimpleNamespace(),
        ):
            result = await host._recover_engine_via_anchor(
                session={
                    "session_id": _SID,
                    "session_kind": "agent_chat",
                    "sandbox_id": "sbx-1",
                    "engine_kind": "custom_replay_engine",
                },
                snapshot=_snapshot("custom_replay_engine"),
            )
        self.assertIsNone(result)
        host._settle_engine_turn_transcript_pending.assert_not_awaited()
        host._fail_engine_turn_unrecoverable.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
