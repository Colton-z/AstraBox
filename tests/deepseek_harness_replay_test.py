"""DSH native replay fills missing output without repeating committed parts."""
from __future__ import annotations

from copy import deepcopy

import pytest

from astrabox.core.service.orchestrator.engine.base import EngineStreamDetached
from astrabox.core.service.orchestrator.engine.deepseek_harness_client import DeepSeekHarnessEngineClient
from astrabox.core.service.orchestrator.engine.deepseek_harness_events import DeepSeekHarnessProtocolError, DeepSeekHarnessTurnTranslator
from astrabox.core.service.orchestrator.engine.deepseek_harness_replay import DshOutputReplay

_SESSION = "native:opaque-session"
_LIVE_ID = f"dsh-text:{_SESSION}:2:1:attempt:opaque:0"
_HISTORY_ID = f"dsh-text:{_SESSION}:2:1:event-17:0"


def _committed(*frames):
    return tuple({"scope": "turn", "payload": {**frame, "__engine_public_ui": True}} for frame in frames)


@pytest.mark.parametrize("pieces", [("pre", "fix suffix"), ("prefix suffix",), ("prefix", " suffix")])
def test_replay_trims_a_coalesced_prefix_and_keeps_the_original_block_id(pieces) -> None:
    committed = _committed(
        {"type": "text-start", "id": _LIVE_ID},
        {"type": "text-delta", "id": _LIVE_ID, "delta": "prefix"},
    )
    original = deepcopy(committed)
    replay = DshOutputReplay(_SESSION, committed)
    assert replay.accept({"type": "text-start", "id": _HISTORY_ID}) == []
    output = []
    for text in pieces:
        output += replay.accept({"type": "text-delta", "id": _HISTORY_ID, "delta": text})
    assert "".join(frame["delta"] for frame in output) == " suffix"
    assert all(frame["id"] == _LIVE_ID for frame in output)
    assert replay.accept({"type": "text-end", "id": _HISTORY_ID}) == [{"type": "text-end", "id": _LIVE_ID}]
    assert committed == original


def test_replay_spans_multiple_committed_delta_rows_without_repeating_tool_cards() -> None:
    tool = {"type": "tool-input-available", "toolCallId": "call-1", "toolName": "bash", "input": {"command": "pwd"}, "dynamic": True}
    replay = DshOutputReplay(_SESSION, _committed(
        {"type": "text-start", "id": _LIVE_ID},
        {"type": "text-delta", "id": _LIVE_ID, "delta": "pre"},
        {"type": "text-delta", "id": _LIVE_ID, "delta": "fix"},
        {"type": "text-end", "id": _LIVE_ID}, tool,
    ))
    frames = [
        {"type": "text-start", "id": _HISTORY_ID},
        {"type": "text-delta", "id": _HISTORY_ID, "delta": "prefix"},
        {"type": "text-end", "id": _HISTORY_ID}, tool,
    ]
    assert [item for frame in frames for item in replay.accept(frame)] == []
    result = {"type": "tool-output-available", "toolCallId": "call-1", "output": "/workspace"}
    assert replay.accept(result) == [result]


@pytest.mark.parametrize("different", [
    {"type": "text-start", "id": f"dsh-text:{_SESSION}:3:1:event-17:0"},
    {"type": "reasoning-start", "id": _HISTORY_ID},
    {"type": "result", "finishReason": "stop"},
])
def test_replay_refuses_a_different_turn_kind_or_truncated_prefix(different) -> None:
    replay = DshOutputReplay(_SESSION, _committed({"type": "text-start", "id": _LIVE_ID}))
    with pytest.raises(EngineStreamDetached, match="cannot reconcile"):
        replay.accept(different)


def test_divergent_content_and_tool_results_are_not_silently_discarded() -> None:
    replay = DshOutputReplay(_SESSION, _committed(
        {"type": "text-start", "id": _LIVE_ID},
        {"type": "text-delta", "id": _LIVE_ID, "delta": "private prefix"},
    ))
    replay.accept({"type": "text-start", "id": _HISTORY_ID})
    with pytest.raises(EngineStreamDetached, match="differs") as error:
        replay.accept({"type": "text-delta", "id": _HISTORY_ID, "delta": "changed private text"})
    assert "private" not in str(error.value)
    replay = DshOutputReplay(_SESSION, _committed({"type": "tool-output-available", "toolCallId": "call", "output": "old"}))
    with pytest.raises(EngineStreamDetached, match="cannot reconcile"):
        replay.accept({"type": "tool-output-available", "toolCallId": "call", "output": "changed"})


def test_replay_refuses_a_different_native_turn_before_publishing_any_output() -> None:
    translator = DeepSeekHarnessTurnTranslator(session_id=_SESSION, expected_turn=2)
    with pytest.raises(DeepSeekHarnessProtocolError, match="different turn"):
        list(translator.translate({"type": "turn/start", "seq": 12, "data": {"turn": 3}}))


@pytest.mark.parametrize("source", ["history", "baseline"])
def test_native_translation_resumes_partial_text_from_history_or_live_baseline(source) -> None:
    committed = _committed(
        {"type": "start-step"},
        {"type": "text-start", "id": _LIVE_ID},
        {"type": "text-delta", "id": _LIVE_ID, "delta": "prefix"},
    )
    translator = DeepSeekHarnessTurnTranslator(session_id=_SESSION, committed_frames=committed)
    client = DeepSeekHarnessEngineClient(session_id="platform", link=None, native_session_id=_SESSION)
    step = {"type": "session/event", "payload": {"sessionId": _SESSION,
        "event": {"type": "step/start", "seq": 12, "data": {"turn": 2, "step": 1}}}}
    assert client._translate_output_frame(translator, step) == []
    chunks = [
        {"type": "block-start", "index": 0, "blockType": "text"},
        {"type": "text-delta", "index": 0, "text": "prefix suffix"},
        {"type": "block-end", "index": 0},
    ]
    records = [{"type": "chunk", "time": index, "chunk": chunk} for index, chunk in enumerate(chunks)]
    if source == "history":
        frame = {"type": "session/event", "payload": {"sessionId": _SESSION, "event": {
            "type": "assistant/message", "seq": 17, "surfaceOp": "append",
            "data": {"turn": 2, "step": 1, "stream": records, "message": {}},
        }}}
    else:
        frame = {"type": "session/assistant-stream-snapshot", "payload": {"sessionId": _SESSION, "cursor": 12,
            "baseline": {"revision": 4, "activeAttempt": {"attemptId": "attempt:opaque", "startedAfterSeq": 12,
                "turn": 2, "step": 1, "nextIndex": 3, "stream": records}}}}
    output = client._translate_output_frame(translator, frame)
    assert [part["type"] for part in output] == ["text-delta", "text-end"]
    assert output[0]["delta"] == " suffix"
    assert all(part["id"] == _LIVE_ID for part in output)
    assert all(part["__engine_output_cursor"]["sessionId"] == _SESSION for part in output)
