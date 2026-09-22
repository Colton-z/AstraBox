"""Native AI SDK tool-approval frame mapping.

The stream protocol validates every frame against a strict schema, so these
tests pin both the mapping semantics (which interaction presentations
participate in the native approval lifecycle) and the exact wire shape of the
emitted frames (no extra keys, approval ids stable across request and
response). The pendings are built through Claude's codec so the presentation
each native tool declares is the one under test, not a hand-written label.
"""

from __future__ import annotations

import unittest
from typing import Any

from astrabox.core.service.orchestrator.engine.claude_interaction_codec import (
    build_claude_interaction_contract,
)
from astrabox.core.service.orchestrator.engine.interaction_contract import (
    build_pending_interaction_record,
    build_tool_approval_request_frame,
    build_tool_approval_response_frame,
    validate_interaction_contract,
)

# Wire contract: the AI SDK UI-message-stream chunk schemas are strict
# objects, so emitting any key outside these sets breaks client validation.
_ALLOWED_REQUEST_KEYS = {"type", "approvalId", "toolCallId", "isAutomatic", "signature"}
_ALLOWED_RESPONSE_KEYS = {
    "type",
    "approvalId",
    "approved",
    "reason",
    "providerExecuted",
    "providerMetadata",
}


def _pending(
    *,
    tool_name: str,
    input_payload: dict[str, Any],
    tool_call_id: str | None = "toolu_01",
    **overrides: Any,
) -> dict[str, Any]:
    contract = build_claude_interaction_contract(
        tool_name=tool_name,
        input_payload=input_payload,
    )
    validate_interaction_contract(contract)
    pending = build_pending_interaction_record(
        contract=contract,
        session_id="session-1",
        turn_id="turn-1",
        interaction_id="interaction-1",
        tool_call_id=tool_call_id,
    )
    pending.update(overrides)
    return pending


def _tool_approval_pending(**overrides: Any) -> dict[str, Any]:
    return _pending(
        tool_name="Bash",
        input_payload={"command": "ls"},
        **overrides,
    )


def _form_pending(**overrides: Any) -> dict[str, Any]:
    return _pending(
        tool_name="AskUserQuestion",
        input_payload={"questions": [{"id": "q1", "question": "Pick one"}]},
        **overrides,
    )


def _decision_pending(**overrides: Any) -> dict[str, Any]:
    return _pending(
        tool_name="ExitPlanMode",
        input_payload={"plan": "1. do the thing"},
        **overrides,
    )


class ToolApprovalRequestFrameTests(unittest.TestCase):
    def test_tool_approval_maps_to_request_frame(self) -> None:
        frame = build_tool_approval_request_frame(_tool_approval_pending())
        self.assertEqual(
            frame,
            {
                "type": "tool-approval-request",
                "approvalId": "interaction-1",
                "toolCallId": "toolu_01",
            },
        )

    def test_request_frame_shape_is_wire_safe(self) -> None:
        frame = build_tool_approval_request_frame(_tool_approval_pending())
        assert frame is not None
        self.assertTrue(set(frame).issubset(_ALLOWED_REQUEST_KEYS), set(frame))

    def test_missing_tool_call_id_stays_on_data_interaction(self) -> None:
        pending = _tool_approval_pending(tool_call_id=None)
        self.assertIsNone(build_tool_approval_request_frame(pending))

    def test_a_form_is_not_a_native_approval(self) -> None:
        pending = _form_pending()
        self.assertEqual(pending["presentation"], "form")
        self.assertIsNone(build_tool_approval_request_frame(pending))

    def test_a_decision_is_not_a_native_approval(self) -> None:
        pending = _decision_pending()
        self.assertEqual(pending["presentation"], "decision")
        self.assertIsNone(build_tool_approval_request_frame(pending))

    def test_approval_id_falls_back_to_tool_call_id(self) -> None:
        pending = _tool_approval_pending(interaction_id="")
        frame = build_tool_approval_request_frame(pending)
        assert frame is not None
        self.assertEqual(frame["approvalId"], "toolu_01")


class ToolApprovalResponseFrameTests(unittest.TestCase):
    def test_approve_maps_to_approved_response(self) -> None:
        frame = build_tool_approval_response_frame(
            _tool_approval_pending(),
            {"decision": "approve"},
        )
        self.assertEqual(
            frame,
            {
                "type": "tool-approval-response",
                "approvalId": "interaction-1",
                "approved": True,
            },
        )

    def test_reject_carries_comment_as_reason(self) -> None:
        frame = build_tool_approval_response_frame(
            _tool_approval_pending(),
            {"decision": "reject", "comment": "not on this host"},
        )
        self.assertEqual(
            frame,
            {
                "type": "tool-approval-response",
                "approvalId": "interaction-1",
                "approved": False,
                "reason": "not on this host",
            },
        )

    def test_response_frame_shape_is_wire_safe(self) -> None:
        frame = build_tool_approval_response_frame(
            _tool_approval_pending(),
            {"decision": "reject", "comment": "no"},
        )
        assert frame is not None
        self.assertTrue(set(frame).issubset(_ALLOWED_RESPONSE_KEYS), set(frame))

    def test_response_approval_id_matches_request(self) -> None:
        pending = _tool_approval_pending()
        request = build_tool_approval_request_frame(pending)
        response = build_tool_approval_response_frame(pending, {"decision": "approve"})
        assert request is not None and response is not None
        self.assertEqual(request["approvalId"], response["approvalId"])

    def test_non_native_presentations_return_none(self) -> None:
        self.assertIsNone(
            build_tool_approval_response_frame(_form_pending(), {"decline": True})
        )
        self.assertIsNone(
            build_tool_approval_response_frame(
                _decision_pending(), {"decision": "reject"}
            )
        )


if __name__ == "__main__":
    unittest.main()
