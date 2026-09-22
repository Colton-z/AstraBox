"""Frame translation checked against content from historical vendor recordings.

The fixtures under ``tests/data/deepseek_harness/`` are byte-exact
``notifications.expected.jsonl`` streams from the harness repository (see the
README there). The shared fixture adapter moves their chunks into the current
durable settlement envelope without changing recorded content. Expected text,
tool results and step counts still come from the original recording; these
tests do not claim to capture the current live transport.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from deepseek_harness_fixtures import current_settlement_frames

from astrabox.core.service.orchestrator.engine.deepseek_harness_events import (
    DeepSeekHarnessProtocolError,
    DeepSeekHarnessTurnTranslator,
)

_DATA_DIR = Path(__file__).parent / "data" / "deepseek_harness"
_SESSION_ID = "dsh-golden-session"


def _load_session_events(case: str) -> list[dict[str, Any]]:
    raw = (_DATA_DIR / f"{case}.notifications.jsonl").read_text()
    raw = raw.replace("{{sessionId}}", _SESSION_ID)
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        notification = json.loads(line)
        if notification.get("method") != "session.event":
            continue
        params = notification["params"]
        if params["sessionId"] != _SESSION_ID:
            continue
        events.append(params["event"])
    return events


def _translate_all(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    translator = DeepSeekHarnessTurnTranslator(session_id=_SESSION_ID)
    frames: list[dict[str, Any]] = []
    current = current_settlement_frames(
        [{"payload": {"sessionId": _SESSION_ID, "event": event}} for event in events],
        session_id=_SESSION_ID,
    )
    for frame in current:
        frames.extend(translator.translate(frame["payload"]["event"]))
        if translator.terminal_seen:
            break
    return frames


def _vendor_block_texts(events: list[dict[str, Any]], block_type: str) -> list[str]:
    """The vendor's own committed block texts, from block-end chunks."""
    texts: list[str] = []
    for event in events:
        if event.get("type") != "assistant/chunk":
            continue
        chunk = event["data"]["chunk"]
        if chunk.get("type") == "block-end" and chunk["block"].get("type") == block_type:
            texts.append(chunk["block"]["text"])
    return texts


class TestTextTurnGoldenStream:
    def test_deltas_reassemble_the_vendors_committed_blocks(self) -> None:
        events = _load_session_events("text-turn")
        frames = _translate_all(events)

        by_id: dict[str, list[str]] = {}
        kinds: dict[str, str] = {}
        for frame in frames:
            if frame["type"] in {"text-delta", "reasoning-delta"}:
                by_id.setdefault(frame["id"], []).append(frame["delta"])
                kinds[frame["id"]] = frame["type"].removesuffix("-delta")

        reasoning = ["".join(parts) for fid, parts in by_id.items() if kinds[fid] == "reasoning"]
        text = ["".join(parts) for fid, parts in by_id.items() if kinds[fid] == "text"]
        assert reasoning == _vendor_block_texts(events, "reasoning")
        assert text == _vendor_block_texts(events, "text")
        assert text == ["SDK snapshot OK"]

    def test_every_stream_block_opens_and_closes_under_one_id(self) -> None:
        frames = _translate_all(_load_session_events("text-turn"))
        opened = [f["id"] for f in frames if f["type"] in {"text-start", "reasoning-start"}]
        closed = [f["id"] for f in frames if f["type"] in {"text-end", "reasoning-end"}]
        assert opened == closed
        assert len(set(opened)) == len(opened)

    def test_exactly_one_terminal_and_it_is_last(self) -> None:
        events = _load_session_events("text-turn")
        frames = _translate_all(events)
        results = [f for f in frames if f["type"] == "result"]
        assert len(results) == 1
        assert frames[-1] is results[0]
        assert results[0]["finishReason"] == "stop"

    def test_usage_rides_the_result_verbatim(self) -> None:
        events = _load_session_events("text-turn")
        vendor_usage = next(
            e["data"]["chunk"]["usage"]
            for e in events
            if e.get("type") == "assistant/chunk"
            and e["data"]["chunk"].get("type") == "usage"
        )
        result = _translate_all(events)[-1]
        assert result["usage"] == vendor_usage


class TestBashToolGoldenStream:
    def test_tool_call_becomes_input_frames_with_parsed_arguments(self) -> None:
        events = _load_session_events("bash-tool")
        frames = _translate_all(events)
        starts = [f for f in frames if f["type"] == "tool-input-start"]
        available = [f for f in frames if f["type"] == "tool-input-available"]
        assert len(starts) == len(available) == 1
        assert available[0]["toolName"] == "bash"
        assert available[0]["input"]["command"] == "echo dsh-sdk-proof-7391"
        assert available[0]["toolCallId"] == starts[0]["toolCallId"]
        # See pi_translation_test: the flag is what makes a `dynamic-tool` part.
        assert starts[0]["dynamic"] is True and available[0]["dynamic"] is True

    def test_tool_result_answers_the_same_call_id(self) -> None:
        frames = _translate_all(_load_session_events("bash-tool"))
        call_id = next(f["toolCallId"] for f in frames if f["type"] == "tool-input-start")
        outputs = [f for f in frames if f["type"] == "tool-output-available"]
        assert len(outputs) == 1
        assert outputs[0]["toolCallId"] == call_id
        assert outputs[0]["output"]["isError"] is False
        assert "dsh-sdk-proof-7391" in json.dumps(outputs[0]["output"]["content"])

    def test_step_finish_does_not_end_the_turn(self) -> None:
        """The tool step ends with finish.reason.kind="tool-calls"; the turn
        continues into a second step. Only turn/end may produce the result
        frame — a step terminal leaking through would strand the real answer
        exactly like the segment-end-as-turn-end defect class."""
        events = _load_session_events("bash-tool")
        step_finishes = [
            e
            for e in events
            if e.get("type") == "assistant/chunk"
            and e["data"]["chunk"].get("type") == "finish"
        ]
        assert len(step_finishes) >= 2, "fixture must contain a multi-step turn"
        frames = _translate_all(events)
        results = [f for f in frames if f["type"] == "result"]
        assert len(results) == 1
        assert frames[-1] is results[0]


class TestEngineStepBoundaries:
    """The harness names its own message boundaries; the turn carries them.

    A platform turn is ONE UI message and the engine answers it in several of
    its own steps. The vendor emits ``step/start``/``step/end`` natively, so
    the count, the nesting and the block containment are all checkable against
    the vendor stream rather than against a hand-written expectation.
    """

    @pytest.mark.parametrize("case", ["text-turn", "bash-tool"])
    def test_every_vendor_step_becomes_one_ui_step(self, case: str) -> None:
        events = _load_session_events(case)
        frames = _translate_all(events)
        vendor_starts = sum(1 for e in events if e.get("type") == "step/start")
        vendor_ends = sum(1 for e in events if e.get("type") == "step/end")
        assert vendor_starts and vendor_starts == vendor_ends
        assert sum(1 for f in frames if f["type"] == "start-step") == vendor_starts
        assert sum(1 for f in frames if f["type"] == "finish-step") == vendor_ends

    def test_a_tool_turn_carries_more_than_one_step(self) -> None:
        # The whole point of the boundary: think → call the tool → answer is
        # two engine messages, and a reader that sees one flat run has lost it.
        frames = _translate_all(_load_session_events("bash-tool"))
        assert sum(1 for f in frames if f["type"] == "start-step") >= 2

    @pytest.mark.parametrize("case", ["text-turn", "bash-tool"])
    def test_steps_nest_and_never_split_a_block(self, case: str) -> None:
        frames = _translate_all(_load_session_events(case))
        depth = 0
        open_blocks: set[str] = set()
        for frame in frames:
            kind = frame["type"]
            if kind == "start-step":
                assert depth == 0, "a step opened inside another step"
                depth = 1
            elif kind == "finish-step":
                assert depth == 1, "a step closed without opening"
                assert not open_blocks, "a step boundary split an open block"
                depth = 0
            elif kind.endswith("-start") and "id" in frame:
                open_blocks.add(frame["id"])
            elif kind.endswith("-end") and "id" in frame:
                open_blocks.discard(frame["id"])
        assert depth == 0, "a step was left open at the end of the turn"


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
