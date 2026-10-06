"""Protocol rejection and control-event translation.

Native text, reasoning, tools, step boundaries, completion and usage are checked
against the running supplier and public history by
``tests/e2e-ui/specs/dsh-native-tool-output.exclusive.spec.ts``.
"""

from __future__ import annotations

import pytest

from astrabox.core.service.orchestrator.engine.deepseek_harness_events import (
    DeepSeekHarnessProtocolError,
    DeepSeekHarnessTurnTranslator,
)

_SESSION_ID = "dsh-golden-session"


class TestProtocolBoundaries:
    def test_unknown_event_types_are_preserved_as_raw_data(self) -> None:
        translator = DeepSeekHarnessTurnTranslator(session_id=_SESSION_ID)
        frames = list(
            translator.translate({"type": "novel/telemetry", "seq": 1, "data": {"x": 1}})
        )
        assert frames == [
            {
                "type": "data-raw-event",
                "data": {
                    "event_type": "deepseek_harness.sdk",
                    "subtype": "novel/telemetry",
                    "raw": {"type": "novel/telemetry", "seq": 1, "data": {"x": 1}},
                },
            }
        ]

    def test_the_selected_model_is_a_control_not_transcript(self) -> None:
        """The platform re-asserts it on every publish and already holds it.

        Left unlisted it becomes a private diagnostic frame, and because the
        selection is appended before the turn that reads the downlink, the
        held-back window releases it into that turn's transcript.
        """

        translator = DeepSeekHarnessTurnTranslator(session_id=_SESSION_ID)
        frames = list(
            translator.translate(
                {
                    "type": "model/selection",
                    "seq": 3,
                    "data": {"provider": "deepseek-official", "model": "gpt-5.6-luna"},
                }
            )
        )
        assert frames == []

    def test_unknown_turn_end_reason_fails_loud(self) -> None:
        translator = DeepSeekHarnessTurnTranslator(session_id=_SESSION_ID)
        with pytest.raises(DeepSeekHarnessProtocolError, match="unknown reason kind"):
            list(
                translator.translate(
                    {"type": "turn/end", "seq": 9, "data": {"turn": 1, "reason": {"kind": "novel"}}}
                )
            )

    def test_delta_without_open_block_fails_loud(self) -> None:
        translator = DeepSeekHarnessTurnTranslator(session_id=_SESSION_ID)
        with pytest.raises(DeepSeekHarnessProtocolError, match="no matching open block"):
            list(
                translator.translate(
                    {
                        "type": "assistant/message",
                        "seq": 2,
                        "data": {"turn": 1, "step": 1, "stream": [
                            {"type": "chunk", "time": 2, "chunk": {"type": "text-delta", "index": 0, "text": "x"}},
                        ]},
                    }
                )
            )

    def test_aborted_turn_settles_cancelled_and_carries_the_vendor_reason(self) -> None:
        translator = DeepSeekHarnessTurnTranslator(session_id=_SESSION_ID)
        frames = list(
            translator.translate(
                {
                    "type": "turn/end",
                    "seq": 3,
                    "data": {"turn": 1, "reason": {"kind": "aborted", "reason": {"kind": "user"}}},
                }
            )
        )
        assert frames == [
            {
                "type": "result",
                "finishReason": "cancelled",
                "__engine_terminal_reason": "aborted",
                "vendorReason": {"kind": "aborted", "reason": {"kind": "user"}},
            }
        ]

    def test_llm_failure_maps_to_error_with_the_vendor_message(self) -> None:
        translator = DeepSeekHarnessTurnTranslator(session_id=_SESSION_ID)
        frames = list(
            translator.translate(
                {
                    "type": "turn/end",
                    "seq": 3,
                    "data": {
                        "turn": 1,
                        "reason": {"kind": "error", "error": {"message": "rate limited", "code": "429"}},
                    },
                }
            )
        )
        assert frames[0]["finishReason"] == "error"
        assert frames[0]["error"]["message"] == "rate limited"
