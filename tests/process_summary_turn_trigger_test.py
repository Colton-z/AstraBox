"""The turn worker's summary offer names every foldable response of a turn.

A turn with a native input queue holds several responses, each answering the
user record just before it. The offer runs after the terminal settles, through
the real :class:`SessionTitleService` over the real :class:`SessionMessageView`
projection of the turn's frames, with a fake model that records what it was
asked and the real repository on the SQLite backend for the claim rows.

The scene is the one that goes wrong when only the turn's LAST response is
considered: three consumed inputs, where the first two responses ran a tool and
then answered (foldable, so each needs a label) and the third is plain text
(nothing folded, no label). Reading the last response alone finds nothing to
name and skips the whole turn; the offer must instead label the first two, each
with its own message id and its own preceding input, and leave the third alone.
"""

from __future__ import annotations

from typing import Any

import pytest

import astrabox.core.service.orchestrator.engine.claude_code  # noqa: F401  (self-registers)
from astrabox.config.settings import get_settings
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.core.service.orchestrator.session_message_view import SessionMessageView
from astrabox.core.service.orchestrator.session_title_service import SessionTitleService
from astrabox.persistence.repository.process_summary_repository import (
    ProcessSummaryRepository,
)

SESSION_ID = "session-1"
TURN_ID = "turn-1"


@pytest.mark.asyncio
async def test_disabled_label_ai_never_calls_model_or_claims_and_keeps_saved_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_TITLE_MODEL_ENABLED", "false")
    assert load_astrabox_settings().title_model_enabled is False
    repo = ProcessSummaryRepository()
    token = await repo.claim(SESSION_ID, "saved", through_seq=2, turn_completed=True)
    assert token is not None
    assert await repo.finish(
        SESSION_ID, "saved", {"status": "completed", "summary": "Saved label"}, claim_token=token
    )
    model = _RecordingModel()
    service = SessionTitleService(
        sessions_repo=None, message_view=None, model=model, process_summary_repo=repo,
    )
    assert await service.generate_for_first_completed_turn(
        session_id=SESSION_ID, turn_id=TURN_ID,
    ) == {"status": "skipped", "reason": "disabled"}
    assert await service.generate_process_summary_for_turn(SESSION_ID, TURN_ID) == {"status": "disabled"}
    for retry in (False, True):
        result = await service.generate_process_summary(
            session_id=SESSION_ID, message_id="new", user_text="Read a file",
            process_text="Read a.txt", through_seq=3, turn_completed=True, retry_failed=retry,
        )
        assert result["status"] == "disabled"
        assert result["error"] is None
    assert model.calls == []
    assert await repo.read(SESSION_ID, ["new"]) == {}
    assert (await service.read_process_summaries(SESSION_ID, ["saved"]))["saved"]["summary"] == "Saved label"


@pytest.fixture(autouse=True)
def _isolated_sqlite_state(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ASTRABOX_DB_BACKEND", "sqlite")
    monkeypatch.delenv("ASTRABOX_DB_URL", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _TurnEvents:
    """The per-turn reads ``SessionMessageView`` makes, over one turn's rows."""

    def __init__(self, events: list[dict[str, Any]], frames: list[dict[str, Any]]) -> None:
        self._events = events
        self._frames = frames

    async def list_events(self, session_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        assert session_id == SESSION_ID
        rows = list(self._events)
        event_types = kwargs.get("event_types")
        if isinstance(event_types, (set, frozenset)):
            rows = [row for row in rows if row["event_type"] in event_types]
        turn_id = kwargs.get("turn_id")
        if isinstance(turn_id, str):
            rows = [row for row in rows if row["turn_id"] == turn_id]
        after_seq = int(kwargs.get("after_seq") or 0)
        before_seq = kwargs.get("before_seq")
        rows = [row for row in rows if int(row["event_seq"]) > after_seq]
        if isinstance(before_seq, int):
            rows = [row for row in rows if int(row["event_seq"]) < before_seq]
        rows.sort(key=lambda row: int(row["event_seq"]), reverse=bool(kwargs.get("newest_first")))
        return rows[: int(kwargs.get("limit") or 500)]

    async def list_frames(self, session_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        assert session_id == SESSION_ID
        rows = list(self._frames)
        turn_id = kwargs.get("turn_id")
        turn_ids = kwargs.get("turn_ids")
        if isinstance(turn_id, str):
            rows = [row for row in rows if row["turn_id"] == turn_id]
        elif isinstance(turn_ids, (set, frozenset)):
            rows = [row for row in rows if row["turn_id"] in turn_ids]
        after_seq = int(kwargs.get("after_seq") or -1)
        before_seq = kwargs.get("before_seq")
        rows = [row for row in rows if int(row["frame_seq"]) > after_seq]
        if isinstance(before_seq, int):
            rows = [row for row in rows if int(row["frame_seq"]) < before_seq]
        rows.sort(key=lambda row: int(row["frame_seq"]))
        return rows[: int(kwargs.get("limit") or 500)]


def _frame(seq: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": SESSION_ID,
        "frame_seq": seq,
        "turn_id": TURN_ID,
        "created_at": f"2026-09-16T00:00:{seq:02d}Z",
        "payload": payload,
    }


def _consumed(seq: int, input_id: str, response_id: str, content: str) -> dict[str, Any]:
    return _frame(seq, {
        "type": "data-input-consumed",
        "data": {"inputId": input_id, "responseMessageId": response_id, "content": content},
    })


def _tool_response(start: int, tool_id: str, path: str, conclusion: str) -> list[dict[str, Any]]:
    # One response that read a file and then answered: a tool call with its
    # available result, the closing text, and the terminal meter — the shape
    # `history_blocks.process_summary_input` folds and names.
    return [
        _frame(start, {
            "type": "tool-input-available",
            "toolCallId": tool_id,
            "toolName": "Read",
            "input": {"file_path": path},
        }),
        _frame(start + 1, {"type": "tool-output-available", "toolCallId": tool_id, "output": f"contents of {path}"}),
        _frame(start + 2, {"type": "text-start", "id": f"{tool_id}-text"}),
        _frame(start + 3, {"type": "text-delta", "id": f"{tool_id}-text", "delta": conclusion}),
        _frame(start + 4, {"type": "data-result", "data": {"usage": {"input_tokens": 1, "output_tokens": 1}}}),
    ]


def _text_response(start: int, text: str) -> list[dict[str, Any]]:
    return [
        _frame(start, {"type": "text-start", "id": f"plain-{start}"}),
        _frame(start + 1, {"type": "text-delta", "id": f"plain-{start}", "delta": text}),
        _frame(start + 2, {"type": "data-result", "data": {"usage": {"input_tokens": 1, "output_tokens": 1}}}),
    ]


class _RecordingModel:
    """A title model that answers every label request and keeps what it saw."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def resolve_request_config(self) -> dict[str, str]:
        return {"model_name": "fake"}

    async def generate_process_summary(self, **kwargs: Any) -> str:
        self.calls.append(dict(kwargs))
        return f"label {len(self.calls)}"


@pytest.mark.asyncio
async def test_the_turn_offer_labels_every_foldable_response_with_its_own_input() -> None:
    frames = [
        _consumed(1, "input-a", "response-a", "first: read a.txt"),
        *_tool_response(2, "tool-a", "/workspace/a.txt", "a.txt holds alpha"),
        _consumed(7, "input-b", "response-b", "second: read b.txt"),
        *_tool_response(8, "tool-b", "/workspace/b.txt", "b.txt holds beta"),
        _consumed(13, "input-c", "response-c", "third: thanks"),
        *_text_response(14, "you are welcome"),
        _frame(17, {"type": "finish", "finishReason": "stop"}),
    ]
    events = [
        {
            "session_id": SESSION_ID,
            "event_seq": 18,
            "event_type": "turn.completed",
            "turn_id": TURN_ID,
            "occurred_at": "2026-09-16T00:00:18Z",
            "payload": {},
        },
    ]
    view = SessionMessageView(_TurnEvents(events, frames))

    # The projection the offer reads: three responses, in order, with their
    # own native ids. This is the precondition, stated so a red below cannot
    # be a broken fixture wearing the offer's name.
    projected = await view._messages_for_turn(SESSION_ID, turn_id=TURN_ID)
    assert [(row["role"], row["message_id"]) for row in projected] == [
        ("user", "input-a:user"),
        ("assistant", "response-a"),
        ("user", "input-b:user"),
        ("assistant", "response-b"),
        ("user", "input-c:user"),
        ("assistant", "response-c"),
    ]

    model = _RecordingModel()
    repo = ProcessSummaryRepository()
    service = SessionTitleService(
        sessions_repo=None,
        message_view=view,
        model=model,
        process_summary_repo=repo,
    )

    outcome = await service.generate_process_summary_for_turn(SESSION_ID, TURN_ID)

    # One label per foldable response, each written from that response's own
    # process and named against the input it answered — not the turn's first
    # input, and not the last response's.
    assert outcome["status"] == "completed"
    assert sorted(outcome["responses"]) == ["response-a", "response-b"]
    assert [call["user_text"] for call in model.calls] == ["first: read a.txt", "second: read b.txt"]
    assert "/workspace/a.txt" in model.calls[0]["process_text"]
    assert "a.txt holds alpha" not in model.calls[0]["process_text"]
    assert "/workspace/b.txt" in model.calls[1]["process_text"]
    assert "b.txt holds beta" not in model.calls[1]["process_text"]
    assert "you are welcome" not in "".join(call["process_text"] for call in model.calls)

    # The rows the browser reads back: both foldable responses labelled, the
    # plain-text response never claimed.
    rows = await repo.read(SESSION_ID, ["response-a", "response-b", "response-c"])
    assert rows["response-a"]["status"] == "completed"
    assert rows["response-a"]["summary"] == "label 1"
    assert rows["response-a"]["turn_completed"] is True
    assert rows["response-b"]["status"] == "completed"
    assert rows["response-b"]["summary"] == "label 2"
    assert "response-c" not in rows

    # A second offer for the same settled turn writes nothing new: every claim
    # is already held by the rows above.
    again = await service.generate_process_summary_for_turn(SESSION_ID, TURN_ID)
    assert again["status"] == "completed"
    assert len(model.calls) == 2
    assert again["responses"]["response-a"]["summary"] == "label 1"
    assert again["responses"]["response-b"]["summary"] == "label 2"
