"""The engine's account of why a run ended is carried, projected, never re-minted.

The translator keeps vendor result fields under their own names. The typed
terminal carries the native reason separately from the platform outcome, and a
missing reason stays ``None`` rather than being synthesized by orchestration.
"""

from __future__ import annotations

import unittest

from astrabox.core.service.orchestrator.engine.frame_translator import (
    ClaudeStreamCursor,
    translate_claude_sdk_message,
)
from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    build_turn_terminal_snapshot_updates,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn.bridge_terminal import (
    _engine_terminal_reason,
)


def _result_message(**overrides: object) -> dict[str, object]:
    return {
        "__sdk_type": "ResultMessage",
        "subtype": "success",
        "session_id": "sdk-session-1",
        **overrides,
    }


class TranslatorCarriesTheResultVerbatimTests(unittest.TestCase):
    def _translate(self, message: dict[str, object]) -> dict[str, object]:
        frames = list(translate_claude_sdk_message(message, envelope_seq=1))
        (result,) = [f for f in frames if f.get("type") == "result"]
        return result

    def test_the_vendor_fields_survive_under_their_own_names(self) -> None:
        result = self._translate(
            _result_message(
                terminal_reason="aborted_tools",
                num_turns=3,
                total_cost_usd=0.42,
                api_error_status=529,
                permission_denials=[{"tool_name": "Bash"}],
            )
        )
        self.assertEqual(result["terminal_reason"], "aborted_tools")
        self.assertEqual(result["num_turns"], 3)
        self.assertEqual(result["total_cost_usd"], 0.42)
        self.assertEqual(result["api_error_status"], 529)
        self.assertEqual(result["permission_denials"], [{"tool_name": "Bash"}])

    def test_an_older_cli_without_the_fields_adds_no_keys(self) -> None:
        result = self._translate(_result_message())
        for key in ("terminal_reason", "num_turns", "total_cost_usd"):
            self.assertNotIn(key, result)


class TheEngineVerdictDecidesTheFinishReasonTests(unittest.TestCase):
    """``is_error`` outranks ``subtype``, because they disagree.

    Measured against claude 2.1.233 pointed at a gateway answering 401: the CLI
    retries ten times over ~175 seconds and then ends the run with
    ``subtype="success"`` AND ``is_error=True``,
    ``terminal_reason="api_error"``, ``api_error_status=401``, and the
    gateway's own sentence in ``result``. Deciding on the subtype filed a turn
    killed by a rejected credential as a normal stop, and the sentence never
    became the turn's failure.
    """

    GATEWAY_REFUSAL = (
        "Failed to authenticate. API Error: 401 Authentication Error: "
        "no LiteLLM virtual key supplied"
    )

    def _refused_run(self, **overrides: object) -> dict[str, object]:
        return _result_message(
            is_error=True,
            terminal_reason="api_error",
            api_error_status=401,
            result=self.GATEWAY_REFUSAL,
            **overrides,
        )

    def _result_frame(self, message: dict[str, object]) -> dict[str, object]:
        frames = list(translate_claude_sdk_message(message, envelope_seq=1))
        (result,) = [f for f in frames if f.get("type") == "result"]
        return result

    def test_a_success_subtype_does_not_outvote_the_engines_own_is_error(self) -> None:
        self.assertEqual(self._result_frame(self._refused_run())["finishReason"], "error")

    def test_the_failure_carries_the_gateways_own_sentence(self) -> None:
        error = self._result_frame(self._refused_run())["error"]
        assert isinstance(error, dict)
        self.assertEqual(error["message"], self.GATEWAY_REFUSAL)

    def test_the_code_is_the_reason_the_engine_named_not_its_success_subtype(self) -> None:
        error = self._result_frame(self._refused_run())["error"]
        assert isinstance(error, dict)
        self.assertEqual(error["code"], "api_error")

    def test_a_subtype_that_names_the_failure_still_wins_over_terminal_reason(self) -> None:
        error = self._result_frame(
            self._refused_run(subtype="error_during_execution")
        )["error"]
        assert isinstance(error, dict)
        self.assertEqual(error["code"], "error_during_execution")

    def test_the_refusal_is_not_also_replayed_as_the_assistants_answer(self) -> None:
        # The result-text fallback exists so a run whose only answer lives in
        # `result` still shows one. A refusal is not an answer, and it already
        # rides the failure — projecting it here would print it twice.
        cursor = ClaudeStreamCursor()
        frames = list(
            translate_claude_sdk_message(self._refused_run(), envelope_seq=1, cursor=cursor)
        )
        self.assertEqual([f for f in frames if f.get("type") == "text-delta"], [])

    def test_a_run_that_really_succeeded_still_stops_and_still_answers(self) -> None:
        cursor = ClaudeStreamCursor()
        message = _result_message(is_error=False, result="the answer is 42")
        frames = list(translate_claude_sdk_message(message, envelope_seq=1, cursor=cursor))
        (result,) = [f for f in frames if f.get("type") == "result"]
        self.assertEqual(result["finishReason"], "stop")
        self.assertNotIn("error", result)
        self.assertEqual(
            [f["delta"] for f in frames if f.get("type") == "text-delta"],
            ["the answer is 42"],
        )


class SettleReadsTheReasonOffTheResultTests(unittest.TestCase):
    class _State:
        def __init__(self, last_terminal_reason: str | None) -> None:
            self.last_terminal_reason = last_terminal_reason

    def test_the_reason_comes_from_the_typed_terminal(self) -> None:
        state = self._State("max_turns")
        self.assertEqual(_engine_terminal_reason(state), "max_turns")

    def test_no_native_reason_stays_none(self) -> None:
        self.assertIsNone(_engine_terminal_reason(self._State(None)))


class TerminalUpdatesKeepTheVocabulariesApartTests(unittest.TestCase):
    def test_engine_reason_and_our_phase_are_different_fields(self) -> None:
        updates = build_turn_terminal_snapshot_updates(
            turn_id="t-1",
            status="COMPLETED",
            error_text=None,
            command_id="c-1",
            terminal_reason="aborted_streaming",
        )
        self.assertEqual(updates["last_turn_terminal_reason"], "aborted_streaming")
        self.assertIsNone(updates["last_turn_failure_phase"])

    def test_our_lifecycle_phase_never_masquerades_as_an_engine_reason(self) -> None:
        updates = build_turn_terminal_snapshot_updates(
            turn_id="t-1",
            status="FAILED",
            error_text="sandbox reclaimed",
            command_id=None,
            failure_phase="sandbox_reclaimed",
        )
        self.assertEqual(updates["last_turn_failure_phase"], "sandbox_reclaimed")
        self.assertIsNone(updates["last_turn_terminal_reason"])


if __name__ == "__main__":
    unittest.main()
