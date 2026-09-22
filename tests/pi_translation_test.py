"""Frame translation proven against pi's own recorded wire output.

The fixtures under ``tests/data/pi/`` are verbatim ``pi --mode rpc`` stdout
from the pinned release (see the README there). Every assertion about
ordering, block identity and terminal placement is checked against what pi
actually emits rather than against a hand-written imitation of it — the first
recording was made against a different pi version and disagreed with the
vendor's own documentation on two counts, which is the failure this file
exists to prevent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from astrabox.core.service.orchestrator.engine.pi_events import (
    PiProtocolError,
    PiTurnTranslator,
)

_DATA_DIR = Path(__file__).parent / "data" / "pi"
_SESSION_ID = "pi-golden-session"


def _load(case: str) -> list[dict[str, Any]]:
    """The session events of one recording, without the command responses."""

    records = [
        json.loads(line)
        for line in (_DATA_DIR / f"{case}.jsonl").read_text().splitlines()
        if line.strip()
    ]
    return [record for record in records if record.get("type") != "response"]


def _translate(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], PiTurnTranslator]:
    translator = PiTurnTranslator(session_id=_SESSION_ID)
    frames: list[dict[str, Any]] = []
    for event in events:
        frames.extend(translator.translate(event))
        if translator.terminal_seen:
            break
    return frames, translator


def _types(frames: list[dict[str, Any]]) -> list[str]:
    return [str(frame.get("type")) for frame in frames]


def test_a_streamed_text_reply_translates_to_one_step_and_one_terminal() -> None:
    frames, translator = _translate(_load("text-reply"))

    assert _types(frames) == [
        "start-step",
        "text-start",
        "text-delta",
        "text-delta",
        "text-end",
        "finish-step",
        "data-raw-event",  # agent_end, which is not a terminal
        "result",
    ]
    assert "".join(
        str(frame["delta"]) for frame in frames if frame["type"] == "text-delta"
    ) == "Hello there"
    assert frames[-1]["finishReason"] == "stop"
    assert frames[-1]["__engine_terminal_reason"] == "stop"
    assert translator.outcome == "completed"


def test_the_committed_reply_is_not_written_a_second_time() -> None:
    """``message_end`` carries the whole assistant message it already streamed.

    Emitting it as content is how a reply lands in the transcript twice.
    """

    frames, _ = _translate(_load("text-reply"))

    text = "".join(
        str(frame.get("delta") or "") for frame in frames if "delta" in frame
    )
    assert text == "Hello there"
    assert not any(
        "Hello there" == frame.get("text") for frame in frames
    ), "the committed message body was re-emitted"


def test_the_prompt_echo_is_not_translated_as_content() -> None:
    """Pi replays the user message it accepted; the platform already stores it."""

    events = _load("text-reply")
    assert any(
        event.get("type") == "message_start"
        and (event.get("message") or {}).get("role") == "user"
        for event in events
    ), "fixture no longer contains the user echo this test is about"

    frames, _ = _translate(events)

    assert not any(frame["type"].startswith("text-") for frame in frames[:1])
    assert _types(frames)[0] == "start-step"


def test_agent_end_does_not_settle_the_turn() -> None:
    """Only ``agent_settled`` ends a turn.

    ``agent_end`` can be followed by a retry, a compaction retry, or a queued
    continuation, so settling on it truncates the answer.
    """

    events = _load("text-reply")
    without_settled = [event for event in events if event.get("type") != "agent_settled"]

    frames, translator = _translate(without_settled)

    assert translator.terminal_seen is False
    assert "result" not in _types(frames)
    assert translator.outcome is None


def test_a_tool_call_turn_carries_two_steps_and_settles_once() -> None:
    frames, translator = _translate(_load("tool-call"))

    assert _types(frames).count("start-step") == 2, "each pi turn is one step"
    assert _types(frames).count("finish-step") == 2
    assert _types(frames).count("result") == 1, "one platform turn, one terminal"
    assert frames[-1]["type"] == "result"
    assert frames[-1]["finishReason"] == "stop"
    assert translator.outcome == "completed"


def test_the_tool_call_and_its_result_are_linked_by_the_engine_id() -> None:
    frames, _ = _translate(_load("tool-call"))

    starts = [f for f in frames if f["type"] == "tool-input-start"]
    available = [f for f in frames if f["type"] == "tool-input-available"]
    outputs = [f for f in frames if f["type"] == "tool-output-available"]

    assert [f["toolCallId"] for f in starts] == ["call_1"]
    assert [f["toolName"] for f in starts] == ["bash"]
    # The AI SDK builds a `dynamic-tool` part only from a frame that says so;
    # without the flag the console gets a typed `tool-bash` part that none of
    # its tool-part readers accept.
    assert all(f["dynamic"] is True for f in starts + available)
    assert available[0]["input"] == {"command": "echo hi"}
    assert [f["toolCallId"] for f in outputs] == ["call_1"]
    assert outputs[0]["output"]["isError"] is False
    assert outputs[0]["output"]["content"] == {"content": [{"type": "text", "text": "hi\n"}]}


def test_two_assistant_messages_in_one_turn_do_not_share_a_block_id() -> None:
    """Pi indexes content per message, so both start at ``contentIndex`` 0.

    Deriving a block id from the index alone would make the second message's
    text stream into the first one's block.
    """

    frames, _ = _translate(_load("tool-call"))

    text_ids = {
        str(frame["id"]) for frame in frames if frame["type"] in {"text-start", "text-end"}
    }
    assert len(text_ids) == 1, "this recording streams text in its second message only"

    # The tool call streamed at contentIndex 0 of the FIRST message; the text
    # streamed at contentIndex 0 of the SECOND. A shared id would collide.
    assert all(":2:" in block_id for block_id in text_ids), (
        f"text block ids do not carry the message ordinal: {text_ids}"
    )


def test_a_tool_result_message_is_not_translated_as_content() -> None:
    """The tool result arrives through tool_execution_end, not as a message."""

    events = _load("tool-call")
    assert any(
        (event.get("message") or {}).get("role") == "toolResult" for event in events
    ), "fixture no longer contains the toolResult message this test is about"

    frames, _ = _translate(events)

    assert _types(frames).count("tool-output-available") == 1


def test_an_event_after_the_terminal_is_refused() -> None:
    events = _load("text-reply")
    _, translator = _translate(events)

    with pytest.raises(PiProtocolError, match="after agent_settled"):
        list(translator.translate({"type": "turn_start"}))


def test_a_tool_result_without_the_error_flag_is_refused() -> None:
    """Pi always sets ``isError``; inferring it renders a failure as success."""

    translator = PiTurnTranslator(session_id=_SESSION_ID)

    with pytest.raises(PiProtocolError, match="carries no isError"):
        list(
            translator.translate(
                {
                    "type": "tool_execution_end",
                    "toolCallId": "call_1",
                    "toolName": "bash",
                    "result": {"content": []},
                }
            )
        )


def test_settling_without_a_stop_reason_is_refused() -> None:
    """A terminal has to state why it ended, and pi always gives one."""

    translator = PiTurnTranslator(session_id=_SESSION_ID)

    with pytest.raises(PiProtocolError, match="without a stop reason"):
        list(translator.translate({"type": "agent_settled"}))


def test_an_aborted_reply_settles_as_cancelled() -> None:
    translator = PiTurnTranslator(session_id=_SESSION_ID)
    list(translator.translate({"type": "message_start", "message": {"role": "assistant"}}))
    list(
        translator.translate(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "stopReason": "aborted",
                    "content": [],
                },
            }
        )
    )

    frames = list(translator.translate({"type": "agent_settled"}))

    assert frames[0]["finishReason"] == "cancelled"
    assert translator.outcome == "cancelled"


def test_a_failed_reply_settles_as_an_error_carrying_its_message() -> None:
    translator = PiTurnTranslator(session_id=_SESSION_ID)
    list(translator.translate({"type": "message_start", "message": {"role": "assistant"}}))
    list(
        translator.translate(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "stopReason": "error",
                    "errorMessage": "upstream refused the request",
                    "content": [],
                },
            }
        )
    )

    frames = list(translator.translate({"type": "agent_settled"}))

    assert frames[0]["finishReason"] == "error"
    assert frames[0]["error"]["message"] == "upstream refused the request"
    assert translator.outcome == "failed"


def test_an_unclosed_block_is_closed_when_its_message_ends() -> None:
    """An aborted stream ends without its ``text_end``.

    Leaving the block open holds the console's message open forever.
    """

    translator = PiTurnTranslator(session_id=_SESSION_ID)
    list(translator.translate({"type": "message_start", "message": {"role": "assistant"}}))
    list(
        translator.translate(
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_start", "contentIndex": 0},
            }
        )
    )

    frames = list(
        translator.translate(
            {
                "type": "message_end",
                "message": {"role": "assistant", "stopReason": "aborted", "content": []},
            }
        )
    )

    assert _types(frames) == ["text-end"]


def test_an_unknown_event_is_kept_as_a_diagnostic() -> None:
    """A field a later pi release adds stays visible instead of vanishing."""

    translator = PiTurnTranslator(session_id=_SESSION_ID)

    frames = list(translator.translate({"type": "something_pi_added_later", "n": 1}))

    assert frames[0]["type"] == "data-raw-event"
    assert frames[0]["data"]["subtype"] == "something_pi_added_later"
    assert frames[0]["data"]["raw"]["n"] == 1


def test_retry_events_are_recorded_because_they_explain_a_late_terminal() -> None:
    translator = PiTurnTranslator(session_id=_SESSION_ID)

    frames = list(
        translator.translate(
            {
                "type": "auto_retry_start",
                "attempt": 1,
                "maxAttempts": 3,
                "delayMs": 500,
                "errorMessage": "overloaded",
            }
        )
    )

    assert frames[0]["type"] == "data-raw-event"
    assert frames[0]["data"]["subtype"] == "auto_retry_start"


def test_a_turn_recorded_on_the_testbed_translates_to_a_settled_reply() -> None:
    """The same translator, against a turn no fake produced.

    ``testbed-real-model.jsonl`` was recorded inside the pi sandbox image on
    the AWS testbed: a real model answered through the deployment's gateway,
    and pi wrote these records to its stdout. It is here because the local
    recordings script the model's replies, and a stream shaped by a fake
    endpoint cannot prove that the terminal this adapter waits for arrives on
    the real path.
    """

    frames, translator = _translate(_load("testbed-real-model"))

    assert _types(frames) == [
        "start-step",
        "text-start",
        "text-delta",
        "text-delta",
        "text-delta",
        "text-delta",
        "text-end",
        "finish-step",
        "data-raw-event",  # agent_end, still not a terminal
        "result",
    ]
    text = "".join(
        str(frame["delta"]) for frame in frames if frame["type"] == "text-delta"
    )
    assert text == "PI_BOX_OK"
    assert frames[-1]["finishReason"] == "stop"
    assert "usage" in frames[-1], "the real model reports usage; the terminal carries it"
    assert translator.outcome == "completed"
