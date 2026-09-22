"""The public result card carries reader-facing metrics under SDK field names."""

from __future__ import annotations

import unittest

from astrabox.core.service.orchestrator.engine.frame_translator import claude_result_data


def _settled_run(**overrides: object) -> dict[str, object]:
    """A ResultMessage as the runner serializes one, under SDK field names."""

    return {
        "__sdk_type": "ResultMessage",
        "subtype": "success",
        "session_id": "sdk-session-1",
        "is_error": False,
        "duration_ms": 4210,
        "duration_api_ms": 3980,
        "num_turns": 2,
        "total_cost_usd": 0.0137,
        "usage": {"input_tokens": 1204, "output_tokens": 88},
        "stop_reason": "end_turn",
        "result": "the answer is 42",
        **overrides,
    }


class TheConsoleFindsWhatItRendersTests(unittest.TestCase):
    def test_the_meter_fields_survive_under_the_vendors_names(self) -> None:
        data = claude_result_data(_settled_run())
        self.assertEqual(data["duration_ms"], 4210)
        self.assertEqual(data["num_turns"], 2)
        self.assertEqual(data["total_cost_usd"], 0.0137)
        self.assertEqual(data["usage"], {"input_tokens": 1204, "output_tokens": 88})

    def test_the_stop_reason_arrives_so_an_abnormal_stop_can_be_named(self) -> None:
        self.assertEqual(
            claude_result_data(_settled_run(stop_reason="max_tokens"))[
                "stop_reason"
            ],
            "max_tokens",
        )

    def test_private_terminal_and_control_fields_do_not_enter_the_result_card(self) -> None:
        data = claude_result_data(
            _settled_run(
                is_error=True,
                terminal_reason="aborted_streaming",
                result="private terminal sentence",
                deferred_tool_use={
                    "id": "native-control",
                    "name": "Ask",
                    "input": {},
                },
                errors=["private error"],
            )
        )

        for key in (
            "session_id",
            "subtype",
            "is_error",
            "terminal_reason",
            "result",
            "deferred_tool_use",
            "errors",
        ):
            self.assertNotIn(key, data)

    def test_no_field_is_re_spelled(self) -> None:
        respelled = [
            key for key in claude_result_data(_settled_run()) if key.lower() != key
        ]
        self.assertEqual(respelled, [])

    def test_a_run_that_reports_no_metrics_has_no_public_result_fields(self) -> None:
        data = claude_result_data(
            {"__sdk_type": "ResultMessage", "subtype": "success"}
        )
        self.assertEqual(data, {})


if __name__ == "__main__":
    unittest.main()
