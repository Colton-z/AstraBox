"""DeepSeek Harness vocabulary → the platform's contracts.

Two translations, both one-way at the seam and both reading the vendor's own
words: session-log events become AI SDK stream frames, and the interactions
the harness raises become structural interaction contracts. Sources of truth:
the types in ``@deepseek-ai/dsh-api-session-controller`` and ``dsh-llm``.

The harness's ``session/follow`` Remote stream carries raw session-log events.
This module translates the events; the link retains their addressed Session.

Vocabulary boundaries this translator enforces:

* Only ``turn/end`` settles a turn. Cursorless Assistant stream frames carry
  live output; ``assistant/message`` and ``assistant/attempt`` commit its exact
  compact stream. A live settlement waits for its named end without replaying
  chunks; a history-only settlement expands its compact stream.
* ``user/message`` echoes input already held by the platform and is dropped.
* Unknown event types become private diagnostics rather than disappearing or
  crossing the browser protocol. The client closes that typed boundary after
  this translator returns.
"""

from __future__ import annotations


import json
from collections.abc import Iterator
from typing import Any

from astrabox.core.service.orchestrator.engine.emissions import public_ui_frame

from astrabox.core.service.orchestrator.engine.file_changes import (
    ContentDiff, ExcerptDiff, FileChange, file_changes_frame,
)

from astrabox.core.service.orchestrator.engine.interaction_contract import (
    validated_question_answer_rows,
)


class DeepSeekHarnessProtocolError(RuntimeError):
    """The pinned harness emitted an event this translator cannot honor."""


#: Session-log bookkeeping AstraBox stores elsewhere, or must not write
#: twice. An event that is not listed here becomes a private diagnostic, so an
#: entry belongs here only when AstraBox already holds the same fact elsewhere.
#:
#: Creating a session emits the three permission events, and selecting a
#: preset emits a command log around them. Neither is conversation: the
#: console renders the permission mode as a control, and the only commands
#: AstraBox runs are its own.
_DROPPED_EVENT_TYPES = frozenset(
    {
        # AstraBox stores the prompt, the queue and the committed reply
        "agent/inbox/spliced",
        "request/context",
        "request/header",
        "session/title",
        "session/title-llm-request",
        "turn/start",
        "user/message",
        # permission state, rendered as a control rather than as transcript
        "agent-preset/selected",
        # the model the platform selected for this conversation: the session
        # row already holds it and the console renders it as a control, and
        # the adapter re-asserts it on every publish
        "model/selection",
        "approval/policy",
        "permission/preset",
        "permissionPresets/preset",
        "sandbox/mode",
        # the harness's log of a command the platform itself ran
        "command/done",
        "command/run",
        # the approval's own log; the platform renders the interaction and its
        # answer, so a card beside them says the same thing twice
        "approval/asked",
        "approval/decided",
    }
)

#: turn/end reason kinds, from the vendor's ``TurnEndReasonMap``. ``aborted``
#: is a cancellation request landing; ``interrupted`` is a persistence backend
#: closing a crash-orphaned turn — nobody asked for that outcome, so it maps
#: to ``error`` rather than ``cancelled``.
_FINISH_REASONS = {
    "completed": "stop",
    "aborted": "cancelled",
    "blocked": "error",
    "error": "error",
    "max-tokens": "stop",
    "interrupted": "error",
}


def raw_event_frame(subtype: str, raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "data-raw-event",
        "data": {
            "event_type": "deepseek_harness.sdk",
            "subtype": subtype or "unknown",
            "raw": dict(raw),
        },
    }


def _stream_integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 2**53 - 1:
        raise DeepSeekHarnessProtocolError(f"{label} is not a non-negative safe integer")
    return value


def _assistant_stream_chunks(stream: Any) -> Iterator[dict[str, Any]]:
    """Expand the supplier's AssistantStreamRecord union without joining deltas."""
    if not isinstance(stream, list):
        raise DeepSeekHarnessProtocolError("assistant settlement lacks its compact stream")
    for record in stream:
        if not isinstance(record, dict):
            raise DeepSeekHarnessProtocolError("assistant compact record is not an object")
        kind = record.get("type")
        if kind == "chunk":
            chunk = record.get("chunk")
            if not isinstance(chunk, dict):
                raise DeepSeekHarnessProtocolError("assistant compact chunk is not an object")
            yield chunk
            continue
        delta_kind = {
            "text-chunks": "text-delta",
            "reasoning-chunks": "reasoning-delta",
            "tool-call-chunks": "tool-call-delta",
        }.get(kind)
        if delta_kind is None:
            raise DeepSeekHarnessProtocolError(f"unknown assistant compact record {kind!r}")
        index = _stream_integer(record.get("index"), "compact block index")
        members = record.get("args" if kind == "tool-call-chunks" else "texts")
        gaps = record.get("dt")
        if (
            not isinstance(members, list) or not members
            or any(not isinstance(member, str) for member in members)
            or not isinstance(gaps, list) or len(gaps) != len(members) - 1
        ):
            raise DeepSeekHarnessProtocolError("assistant compact run has invalid members or gaps")
        for member in members:
            if kind == "tool-call-chunks":
                if not isinstance(record.get("id"), str):
                    raise DeepSeekHarnessProtocolError("assistant tool-call run lacks id")
                yield {
                    "type": delta_kind, "index": index, "id": record["id"],
                    "argumentsDelta": member,
                    **({"name": record["name"]} if "name" in record else {}),
                }
            else:
                yield {"type": delta_kind, "index": index, "text": member}


class DeepSeekHarnessTurnTranslator:
    """Reduce one turn's ``session.event`` stream to AI SDK frames.

    One instance per driven turn; ``terminal_seen`` flips when ``turn/end``
    produced the ``result`` frame and nothing may follow it.
    """

    def __init__(self, *, session_id: str) -> None:
        self._session_id = session_id
        #: content-block index → the open stream part ({"kind", "id"}).
        self._open_blocks: dict[int, dict[str, str]] = {}
        self._last_usage: dict[str, Any] | None = None
        self._turn: int | None = None
        self.terminal_seen = False
        self._file_tools: dict[str, tuple[str, str]] = {}
        self._assistant_revision: int | None = None
        self._assistant_attempt: dict[str, Any] | None = None
        self._attempt_identity = ""

    # ── stream-block identity ────────────────────────────────────────────
    def _block_id(self, kind: str, event: dict[str, Any], index: int) -> str:
        data = event.get("data") or {}
        turn = data.get("turn")
        step = data.get("step")
        return f"dsh-{kind}:{self._session_id}:{turn}:{step}:{self._attempt_identity}:{index}"

    def translate(self, event: dict[str, Any]) -> Iterator[dict[str, Any]]:
        if self.terminal_seen:
            raise DeepSeekHarnessProtocolError(
                "session event arrived after turn/end settled the turn"
            )
        event_type = str(event.get("type") or "").strip()
        data = event.get("data")
        if not isinstance(data, dict):
            raise DeepSeekHarnessProtocolError(
                f"session event {event_type!r} has no data object"
            )

        if event_type in {"assistant/message", "assistant/attempt"}:
            yield from self._translate_assistant_settlement(event, data)
            return
        if event_type == "tool/call":
            yield from self._translate_tool_call(data)
            return
        if event_type == "tool/result":
            yield from self._translate_tool_result(data)
            return
        if event_type == "step/start":
            # A platform turn is one UI message; the harness answers it in
            # several of its own steps (think, call a tool, read the result,
            # think again). It names those boundaries natively, so they are
            # carried rather than re-derived. Blocks are opened and closed by
            # the vendor's own block-start/block-end chunks, so a step boundary
            # never splits one.
            yield {"type": "start-step"}
            return
        if event_type == "step/end":
            yield {"type": "finish-step"}
            return
        if event_type == "turn/end":
            yield self._translate_turn_end(data)
            return
        if event_type in _DROPPED_EVENT_TYPES:
            return
        yield raw_event_frame(event_type, event)

    def restore_assistant_stream(self, baseline: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Accept the supplier's same-cut cursorless stream opening."""
        revision = _stream_integer(baseline.get("revision"), "baseline revision")
        self._assistant_revision = revision
        active = baseline.get("activeAttempt")
        self._assistant_attempt = None
        if active is None:
            return
        if not isinstance(active, dict):
            raise DeepSeekHarnessProtocolError("assistant baseline has invalid activeAttempt")
        self._begin_assistant_attempt(active)
        chunks = list(_assistant_stream_chunks(active.get("stream")))
        if len(chunks) != _stream_integer(active.get("nextIndex"), "baseline nextIndex"):
            raise DeepSeekHarnessProtocolError("assistant baseline stream does not match nextIndex")
        for chunk in chunks:
            yield from self._translate_attempt_chunk(chunk)

    def _begin_assistant_attempt(self, frame: dict[str, Any]) -> None:
        attempt_id = frame.get("attemptId")
        if not isinstance(attempt_id, str) or not attempt_id:
            raise DeepSeekHarnessProtocolError("assistant stream lacks attemptId")
        started_after = frame.get("startedAfterSeq")
        if not isinstance(started_after, int) or isinstance(started_after, bool) or started_after < -1:
            raise DeepSeekHarnessProtocolError("assistant stream lacks its durable opening cursor")
        self._assistant_attempt = {
            "attemptId": attempt_id,
            "turn": _stream_integer(frame.get("turn"), "assistant turn"),
            "step": _stream_integer(frame.get("step"), "assistant step"),
            "startedAfterSeq": started_after,
            "nextIndex": 0,
            "settlement": None,
        }
        self._attempt_identity = attempt_id

    def translate_assistant_stream(self, frame: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Carry native start/chunk/end without giving live chunks durable seqs."""
        revision = _stream_integer(frame.get("revision"), "assistant revision")
        if self._assistant_revision is not None and revision != self._assistant_revision + 1:
            raise DeepSeekHarnessProtocolError("assistant stream revision is not contiguous")
        self._assistant_revision = revision
        kind = frame.get("type")
        if kind not in {"start", "chunk", "end"}:
            raise DeepSeekHarnessProtocolError(f"unknown assistant stream frame {kind!r}")
        if kind == "start":
            if self._assistant_attempt is not None:
                raise DeepSeekHarnessProtocolError("assistant start overlaps an active attempt")
            self._begin_assistant_attempt(frame)
            return
        attempt = self._assistant_attempt
        # A reconnect can miss a start. The supplier's Client ignores those
        # transient frames and presents their complete durable settlement.
        if attempt is None or frame.get("attemptId") != attempt["attemptId"]:
            return
        if _stream_integer(frame.get("index"), "assistant index") != attempt["nextIndex"]:
            raise DeepSeekHarnessProtocolError("assistant stream chunk index is not contiguous")
        if kind == "chunk":
            chunk = frame.get("chunk")
            if not isinstance(chunk, dict):
                raise DeepSeekHarnessProtocolError("assistant stream chunk is not an object")
            yield from self._translate_attempt_chunk(chunk)
            return
        if kind != "end":
            raise DeepSeekHarnessProtocolError(f"unknown assistant stream frame {kind!r}")
        outcome = frame.get("outcome")
        if not isinstance(outcome, dict) or outcome.get("kind") not in {"committed", "abandoned"}:
            raise DeepSeekHarnessProtocolError("assistant stream end has no native outcome")
        settlement = attempt["settlement"]
        if outcome["kind"] == "committed":
            if not isinstance(settlement, dict) or (
                settlement.get("type"), settlement.get("seq")
            ) != (outcome.get("eventType"), outcome.get("seq")):
                raise DeepSeekHarnessProtocolError("assistant stream end does not match durable settlement")
            chunks = list(_assistant_stream_chunks(settlement["data"].get("stream")))
            if len(chunks) != attempt["nextIndex"]:
                raise DeepSeekHarnessProtocolError("assistant settlement disagrees with live chunk count")
            usage = settlement["data"].get("usage")
            if isinstance(usage, dict):
                self._last_usage = dict(usage)
        elif settlement is not None:
            raise DeepSeekHarnessProtocolError("abandoned assistant attempt has a durable settlement")
        yield from self._close_assistant_blocks()
        self._assistant_attempt = None

    def _translate_attempt_chunk(self, chunk: dict[str, Any]) -> Iterator[dict[str, Any]]:
        attempt = self._assistant_attempt
        if attempt is None:
            raise DeepSeekHarnessProtocolError("assistant chunk has no active attempt")
        yield from self._translate_chunk({"data": attempt}, chunk)
        attempt["nextIndex"] += 1

    def _translate_assistant_settlement(
        self, event: dict[str, Any], data: dict[str, Any]
    ) -> Iterator[dict[str, Any]]:
        chunks = list(_assistant_stream_chunks(data.get("stream")))
        attempt = self._assistant_attempt
        matching = attempt is not None and (
            data.get("turn"), data.get("step")
        ) == (attempt["turn"], attempt["step"]) and (
            _stream_integer(event.get("seq"), "assistant settlement seq") > attempt["startedAfterSeq"]
        ) and (event["type"] != "assistant/message" or event.get("surfaceOp") == "append")
        if matching and attempt is not None:
            if attempt["settlement"] is not None:
                raise DeepSeekHarnessProtocolError("assistant attempt has multiple durable settlements")
            # The supplier names the settlement only in the following end
            # frame. Do not advance the live index from a durable record.
            attempt["settlement"] = event
        else:
            self._attempt_identity = f"event-{event.get('seq')}"
            for chunk in chunks:
                yield from self._translate_chunk(event, chunk)
            yield from self._close_assistant_blocks()
        usage = data.get("usage")
        if isinstance(usage, dict):
            self._last_usage = dict(usage)

    def _close_assistant_blocks(self) -> Iterator[dict[str, Any]]:
        for block in self._open_blocks.values():
            yield {"type": f"{block['kind']}-end", "id": block["id"]}
        self._open_blocks.clear()

    def _translate_chunk(
        self, event: dict[str, Any], chunk: dict[str, Any]
    ) -> Iterator[dict[str, Any]]:
        chunk_type = str(chunk.get("type") or "").strip()

        if chunk_type == "block-start":
            index = chunk.get("index")
            block_type = str(chunk.get("blockType") or "").strip()
            kind = {"text": "text", "reasoning": "reasoning"}.get(block_type)
            if kind is None:
                # Tool-call blocks stream separately and settle through the
                # dedicated tool/call event, which carries the complete call.
                return
            if not isinstance(index, int) or isinstance(index, bool):
                raise DeepSeekHarnessProtocolError("block-start has no integer index")
            frame_id = self._block_id(kind, event, index)
            self._open_blocks[index] = {"kind": kind, "id": frame_id}
            yield {"type": f"{kind}-start", "id": frame_id}
            return

        if chunk_type in {"text-delta", "reasoning-delta"}:
            index = chunk.get("index")
            open_block = self._open_blocks.get(index) if isinstance(index, int) else None
            expected_kind = "text" if chunk_type == "text-delta" else "reasoning"
            if open_block is None or open_block["kind"] != expected_kind:
                raise DeepSeekHarnessProtocolError(
                    f"{chunk_type} at index {index!r} has no matching open block"
                )
            yield {
                "type": f"{expected_kind}-delta",
                "id": open_block["id"],
                "delta": str(chunk.get("text") or ""),
            }
            return

        if chunk_type == "block-end":
            index = chunk.get("index")
            open_block = None
            if isinstance(index, int) and not isinstance(index, bool):
                open_block = self._open_blocks.pop(index, None)
            if open_block is None:
                # Tool-call blocks were never opened here (see block-start).
                return
            yield {"type": f"{open_block['kind']}-end", "id": open_block["id"]}
            return

        if chunk_type == "usage":
            usage = chunk.get("usage")
            if isinstance(usage, dict):
                self._last_usage = dict(usage)
            return

        if chunk_type == "finish":
            # Step terminal, not turn terminal: reason.kind "tool-calls" means
            # the model paused to run tools and the turn continues.
            return

        if chunk_type == "tool-call-delta":
            # Argument streaming; the complete call arrives on tool/call.
            return

        raise DeepSeekHarnessProtocolError(
            f"unknown assistant stream chunk type {chunk_type!r}"
        )

    # ── tools ────────────────────────────────────────────────────────────
    def _translate_tool_call(self, data: dict[str, Any]) -> Iterator[dict[str, Any]]:
        call_id = str(data.get("callId") or "").strip()
        name = str(data.get("name") or "").strip()
        raw_arguments = data.get("arguments")
        if not call_id or not name or not isinstance(raw_arguments, str):
            raise DeepSeekHarnessProtocolError("tool/call lacks callId, name, or arguments")
        try:
            arguments = json.loads(raw_arguments)
        except ValueError as exc:
            raise DeepSeekHarnessProtocolError(
                f"tool/call {call_id!r} arguments are not valid JSON"
            ) from exc
        if not isinstance(arguments, dict):
            raise DeepSeekHarnessProtocolError(
                f"tool/call {call_id!r} arguments are not an object"
            )
        if name in {"write", "edit"}:
            self._file_tools[call_id] = (name, str(arguments["file_path"]))
        yield public_ui_frame({
            "type": "tool-input-start",
            "toolCallId": call_id,
            "toolName": name,
            "dynamic": True,
        })
        yield public_ui_frame({
            "type": "tool-input-available",
            "toolCallId": call_id,
            "toolName": name,
            "input": arguments,
            "dynamic": True,
        })

    def _translate_tool_result(self, data: dict[str, Any]) -> Iterator[dict[str, Any]]:
        message = data.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        blocks = [
            block
            for block in (content if isinstance(content, list) else [])
            if isinstance(block, dict) and block.get("type") == "tool-result"
        ]
        if not blocks:
            raise DeepSeekHarnessProtocolError("tool/result carries no tool-result block")
        for block in blocks:
            tool_call_id = str(block.get("toolCallId") or "").strip()
            if not tool_call_id:
                raise DeepSeekHarnessProtocolError("tool-result block has no toolCallId")
            yield {
                "type": "tool-output-available",
                "toolCallId": tool_call_id,
                "output": {
                    "content": block.get("content"),
                    "isError": bool(block.get("isError")),
                },
            }
            file_tool = self._file_tools.pop(tool_call_id, None)
            if not file_tool or block.get("isError"):
                continue
            name, path = file_tool
            meta = data.get("meta")
            hunks = meta.get("diffs") if isinstance(meta, dict) else None
            changes: list[FileChange] = []
            if isinstance(hunks, list) and hunks:
                grouped: dict[str, list[ContentDiff]] = {}
                for hunk in hunks:
                    grouped.setdefault(hunk["path"], []).append(ContentDiff(
                        before=hunk["oldText"] if hunk["oldText"] is not None else "",
                        after=hunk["newText"],
                    ))
                changes = [FileChange(path=p, diff=ExcerptDiff(excerpts=parts))
                           for p, parts in grouped.items()]
            else:
                # A success receipt identifies a write, not its previous bytes.
                changes = [FileChange(path=path, diff=None)]
            yield file_changes_frame(tool_call_id, name, changes)

    # ── turn terminal ────────────────────────────────────────────────────
    def _translate_turn_end(self, data: dict[str, Any]) -> dict[str, Any]:
        reason = data.get("reason")
        kind = str(reason.get("kind") or "").strip() if isinstance(reason, dict) else ""
        finish_reason = _FINISH_REASONS.get(kind)
        if finish_reason is None:
            raise DeepSeekHarnessProtocolError(
                f"turn/end carries unknown reason kind {kind!r}"
            )
        frame: dict[str, Any] = {
            "type": "result",
            "finishReason": finish_reason,
            "__engine_terminal_reason": kind,
        }
        if self._last_usage is not None:
            # Verbatim from the last usage chunk; a multi-step turn's earlier
            # step usages are not summed — summed counters would be platform
            # arithmetic presented as the vendor's report.
            frame["usage"] = dict(self._last_usage)
        if kind != "completed":
            frame["vendorReason"] = dict(reason) if isinstance(reason, dict) else {"kind": kind}
        if finish_reason == "error":
            error = reason.get("error") if isinstance(reason, dict) else None
            frame["error"] = {
                "code": "DEEPSEEK_HARNESS_TURN_FAILED",
                "message": (
                    str(error.get("message") or kind)
                    if isinstance(error, dict)
                    else kind
                ),
            }
        self.terminal_seen = True
        return frame


# ── interactions ─────────────────────────────────────────────────────────
#
# The two answerable frames the harness pushes on its downlink, expressed as
# the platform's structural presentations. What the interaction means stays
# the vendor's: the whole frame rides `raw_input` verbatim, and the encoders
# below read it back from there rather than from anything this platform
# rewrote.

#: The vendor's own name for the tool that asks the user a question. Carried
#: as the contract's tool_name so a transcript names what actually asked.
DSH_QUESTION_TOOL_NAME = "ask_user_question"


def build_approval_contract(payload: dict[str, Any]) -> dict[str, Any]:
    """``approval/request`` → a tool-approval contract.

    The harness asks before a controlled operation and, with no answerer
    available, fails the operation closed.
    """

    tool_name = str(payload.get("toolName") or "").strip()
    if not tool_name:
        raise DeepSeekHarnessProtocolError(
            "approval/request carries no toolName"
        )
    reason = str(payload.get("reason") or "").strip()
    contract: dict[str, Any] = {
        "presentation": "tool_approval",
        "tool_name": tool_name,
        "prompt": reason or f"The harness is asking to run {tool_name}.",
        "raw_input": dict(payload),
    }
    call_id = str(payload.get("callId") or "").strip()
    if call_id:
        # AstraBox removes this key before validating the contract, and uses
        # it to attach the approval to the tool call it gates.
        contract["tool_use_id"] = call_id
    return contract


def build_question_contract(payload: dict[str, Any]) -> dict[str, Any]:
    """``user-questions/request`` → a form contract.

    The vendor's question rows already carry exactly what a form needs — an
    id, a header, the question, its options and whether several may be
    chosen — so the rows are carried across rather than reshaped. ``detail``
    has no field of its own on this side and joins the question text, which
    keeps both strings visible verbatim instead of dropping one.
    """

    raw_questions = payload.get("questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        raise DeepSeekHarnessProtocolError(
            "user-questions/request carries no questions"
        )
    questions: list[dict[str, Any]] = []
    for raw in raw_questions:
        if not isinstance(raw, dict):
            raise DeepSeekHarnessProtocolError("question row is not an object")
        row_id = str(raw.get("id") or "").strip()
        text = str(raw.get("question") or "").strip()
        if not row_id or not text:
            raise DeepSeekHarnessProtocolError(
                "question row lacks an id or a question"
            )
        detail = str(raw.get("detail") or "").strip()
        row: dict[str, Any] = {
            "id": row_id,
            "question": f"{text}\n\n{detail}" if detail else text,
            "multi_select": bool(raw.get("multiSelect")),
            "allow_free_text": True,
            "allow_empty_text": False,
            "options": [
                {
                    "label": str(option.get("label") or ""),
                    "description": str(option.get("description") or ""),
                }
                for option in (raw.get("options") or [])
                if isinstance(option, dict)
            ],
        }
        header = str(raw.get("header") or "").strip()
        if header:
            row["header"] = header
        questions.append(row)
    lead = questions[0].get("header") or questions[0]["question"]
    return {
        "presentation": "form",
        "tool_name": DSH_QUESTION_TOOL_NAME,
        "prompt": str(lead),
        "questions": questions,
        "raw_input": dict(payload),
    }


def approval_response_value(
    pending: dict[str, Any], *, session_id: str, denied: bool
) -> str:
    """Return the supplier's scoped approval waterfall outcome."""
    _ = pending, session_id
    return "rejected" if denied else "allowed-once"


def question_response_value(
    pending: dict[str, Any],
    response: dict[str, Any],
    *,
    session_id: str,
) -> dict[str, Any]:
    """The client-response value that answers one question batch.

    The harness matches an answer against the exact request it resolves —
    same count, same order, same ids, deduplicated selections, at most one
    selection unless the row is multi-select, custom text and selections
    mutually exclusive on a single-select row, and every selected string an
    option label it declared. Building the answer to those rules here is what
    keeps a well-meant reply from coming back ``bad-response``.
    """

    rows = validated_question_answer_rows(pending, response)
    answers: list[dict[str, Any]] = []
    for row in rows:
        question = row["question"]
        answer = row["answer"]
        declared = {
            str(option.get("label") or "")
            for option in (question.get("options") or [])
            if isinstance(option, dict)
        }
        raw_labels = answer.get("option_labels")
        labels = (
            [str(value).strip() for value in raw_labels if str(value).strip()]
            if isinstance(raw_labels, list)
            else []
        )
        single = str(answer.get("option_label") or "").strip()
        if single and single not in labels:
            labels.append(single)
        # Deduplicated in declaration order, and anything the harness did not
        # declare as an option is free text by definition — sending it as a
        # selection is exactly what its validator refuses.
        selected: list[str] = []
        for label in labels:
            if label in declared and label not in selected:
                selected.append(label)
        custom = str(answer.get("free_text") or "").strip()
        if not bool(question.get("multi_select")):
            selected = selected[:1]
            if custom:
                selected = []
        entry: dict[str, Any] = {"id": str(question.get("id") or ""), "selected": selected}
        if custom:
            entry["custom"] = custom
        answers.append(entry)
    return {"answers": answers}
