"""An interrupt-denied tool_result must classify DENIED, not ERROR.

The interrupt-deny path defeats its own structured DENIED stamp:
the funnel-poll interrupt returns PermissionResultDeny without marking the
interaction ANSWERED (no answer_persisted -> no structured stamp), and the deny
comment the CLI records into the transcript is exactly
"User interrupted the session." — which, without the content marker, classifies
output-error on every content-judged plane (wire ingest, canonical projector,
mirror replay) and then clobbers the projection's structured DENIED in
_merge_tool_result_block.
"""

from __future__ import annotations

from astrabox.core.service.orchestrator.tool_result_semantics import (
    TOOL_RESULT_STATE_DENIED,
    TOOL_RESULT_STATE_ERROR,
    normalize_tool_result_block,
    tool_result_state_from_content,
)


def test_interrupt_content_classifies_denied() -> None:
    assert (
        tool_result_state_from_content(
            content="User interrupted the session.", is_error=True
        )
        == TOOL_RESULT_STATE_DENIED
    )


def test_normalize_interrupt_denied_result_adds_explicit_state() -> None:
    block = normalize_tool_result_block(
        {
            "type": "tool_result",
            "tool_use_id": "toolu_interrupted",
            "content": "User interrupted the session.",
            "is_error": True,
        }
    )
    assert block["tool_result_state"] == TOOL_RESULT_STATE_DENIED
    assert block["is_error"] is True


def test_unrelated_error_still_classifies_error() -> None:
    # Negative control in-suite: the marker must not widen into a blanket
    # error->denied rewrite.
    assert (
        tool_result_state_from_content(content="Command failed: exit 1", is_error=True)
        == TOOL_RESULT_STATE_ERROR
    )
