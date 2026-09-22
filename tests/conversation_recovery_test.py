from __future__ import annotations

import unittest
from typing import Any

from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    AI_SDK_FINISH_REASON_STOP,
    AI_SDK_FINISH_REASON_TOOL_CALLS,
    derive_turn_recovery_phase,
    find_recovery_finish_frame,
    is_terminal_data_result_frame,
    needs_turn_recovery,
    normalize_live_source_cursor,
)


class _PagedFramesRepo:
    def __init__(self, frames: list[dict[str, Any]]) -> None:
        self.frames = frames
        self.calls: list[dict[str, Any]] = []

    async def list_frames(self, session_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append({"session_id": session_id, **dict(kwargs)})
        command_id = kwargs.get("command_id")
        turn_id = kwargs.get("turn_id")
        after_seq = int(kwargs.get("after_seq") or -1)
        limit = int(kwargs.get("limit") or 500)
        result = [
            frame
            for frame in self.frames
            if frame.get("session_id") == session_id
            and frame.get("command_id") == command_id
            and frame.get("turn_id") == turn_id
            and int(frame.get("frame_seq") or 0) > after_seq
        ]
        return result[:limit]


class ConversationRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def test_transcript_recovery_requires_the_explicit_phase(self) -> None:
        incomplete = {
            "conversation_state": "IDLE",
            "last_turn_status": "FAILED",
            "last_turn_id": "turn-1",
            "current_turn_remote_anchor": {"sandbox_turn_id": 7},
        }

        self.assertIsNone(derive_turn_recovery_phase(incomplete))
        self.assertFalse(needs_turn_recovery(incomplete))

    def test_anchorless_transcript_pending_turn_needs_recovery(self) -> None:
        self.assertTrue(
            needs_turn_recovery(
                {
                    "conversation_state": "IDLE",
                    "last_turn_status": "FAILED",
                    "last_turn_id": "turn-1",
                    "turn_recovery_phase": "TRANSCRIPT_PENDING",
                    "current_turn_remote_anchor": None,
                }
            )
        )

    def test_sdk_response_result_is_not_platform_terminal_proof(self) -> None:
        self.assertFalse(
            is_terminal_data_result_frame(
                {
                    "type": "data-result",
                    "__sdk_response_boundary": True,
                    "data": {"subtype": "success"},
                }
            )
        )
        self.assertTrue(
            is_terminal_data_result_frame(
                {"type": "data-result", "data": {"subtype": "success"}}
            )
        )

    def test_normalize_live_source_cursor_preserves_zero_values(self) -> None:
        self.assertEqual(
            normalize_live_source_cursor(
                {"live_seq": 0, "sandbox_turn_id": 0, "sandbox_seq": 0}
            ),
            {"live_seq": 0, "sandbox_turn_id": 0, "sandbox_seq": 0},
        )
        self.assertEqual(
            normalize_live_source_cursor(
                {"liveSeq": 0, "sandboxTurnId": 0, "sandboxSeq": 0}
            ),
            {"live_seq": 0, "sandbox_turn_id": 0, "sandbox_seq": 0},
        )
        self.assertEqual(
            normalize_live_source_cursor({"live_seq": 1, "mirror_seq": 0}),
            {"live_seq": 1, "mirror_seq": 0},
        )
        self.assertEqual(
            normalize_live_source_cursor({"liveSeq": 1, "mirrorSeq": 1738}),
            {"live_seq": 1, "mirror_seq": 1738},
        )

    async def test_find_recovery_finish_frame_scans_past_first_page(self) -> None:
        frames = [
            {
                "session_id": "s",
                "turn_id": "t",
                "command_id": "c",
                "frame_seq": index,
                "payload": {"type": "text-delta", "delta": str(index)},
            }
            for index in range(511)
        ]
        frames.append(
            {
                "session_id": "s",
                "turn_id": "t",
                "command_id": "c",
                "frame_seq": 511,
                "payload": {
                    "type": "finish",
                    "finishReason": AI_SDK_FINISH_REASON_TOOL_CALLS,
                },
            }
        )
        frames.append(
            {
                "session_id": "s",
                "turn_id": "t",
                "command_id": "c",
                "frame_seq": 512,
                "payload": {"type": "finish", "finishReason": AI_SDK_FINISH_REASON_STOP},
            }
        )
        repo = _PagedFramesRepo(frames)

        proof = await find_recovery_finish_frame(
            repo,
            session_id="s",
            turn_id="t",
            command_id="c",
            page_size=500,
        )

        self.assertEqual(
            proof,
            {
                "turn_id": "t",
                "command_id": "c",
                "frame_seq": 512,
                "type": "finish",
                "finish_reason": AI_SDK_FINISH_REASON_STOP,
            },
        )
        self.assertEqual([call["after_seq"] for call in repo.calls], [-1, 499])


if __name__ == "__main__":
    unittest.main()
