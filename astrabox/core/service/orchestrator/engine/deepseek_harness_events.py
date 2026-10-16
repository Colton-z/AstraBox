"""DeepSeek Harness vocabulary → AI SDK parts and interaction contracts.

Pure translations of single vendor records, read in the vendor's own words.
Sources of truth: the types of ``@deepseek-ai/dsh-session`` (``SessionEventMap``),
``@deepseek-ai/dsh-llm`` (``StreamChunk``, ``AssistantStreamRecord``),
``@deepseek-ai/dsh-api-session-controller`` (follow and assistant-stream
frames), ``@deepseek-ai/dsh-user-approval`` and ``@deepseek-ai/dsh-user-questions``
(the two answerable requests). Positions, response boundaries and cursors are
the runtime client's (``deepseek_harness_runtime_client``).

A model attempt's text reaches the platform twice: as process-local
assistant-stream chunks while it streams, and inside its durable
``assistant/message`` or ``assistant/attempt`` settlement as the same chunk
sequence in compact form. :class:`AttemptBlocks` names each block by the
attempt's place in its step, so both sources translate to identical parts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from astrabox.core.service.orchestrator.engine.emissions import public_ui_frame
from astrabox.core.service.orchestrator.engine.file_changes import (
    ContentDiff,
    ExcerptDiff,
    FileChange,
    file_changes_frame,
)
from astrabox.core.service.orchestrator.engine.interaction_contract import (
    validated_question_answer_rows,
)


class DeepSeekHarnessProtocolError(RuntimeError):
    """The pinned harness emitted a record this adapter cannot honor."""


def tool_result_message(data: dict[str, Any]) -> dict[str, Any]:
    """Read the supplier's V4 ToolResultMessage shared by live and child history."""
    message = data.get("message")
    if not isinstance(message, dict) or message.get("role") != "tool":
        raise DeepSeekHarnessProtocolError("tool/result carries no tool-role message")
    if not isinstance(message.get("toolCallId"), str):
        raise DeepSeekHarnessProtocolError("tool/result message has no toolCallId")
    if not isinstance(message.get("content"), list):
        raise DeepSeekHarnessProtocolError("tool/result message has no content blocks")
    if "isError" in message and not isinstance(message["isError"], bool):
        raise DeepSeekHarnessProtocolError("tool/result message has invalid isError")
    return message


def raw_event_frame(subtype: str, raw: dict[str, Any]) -> dict[str, Any]:
    """A vendor record kept as operator evidence, never shown to a browser."""
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


def assistant_stream_chunks(stream: Any) -> list[dict[str, Any]]:
    """Expand the supplier's compact ``AssistantStreamRecord`` list into its chunks.

    Runs of deltas expand member by member without joining, so the result has
    exactly one entry per chunk the attempt streamed live.
    """
    if not isinstance(stream, list):
        raise DeepSeekHarnessProtocolError("assistant settlement lacks its compact stream")
    chunks: list[dict[str, Any]] = []
    for record in stream:
        if not isinstance(record, dict):
            raise DeepSeekHarnessProtocolError("assistant compact record is not an object")
        kind = record.get("type")
        if kind == "chunk":
            chunk = record.get("chunk")
            if not isinstance(chunk, dict):
                raise DeepSeekHarnessProtocolError("assistant compact chunk is not an object")
            chunks.append(chunk)
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
                chunks.append({
                    "type": delta_kind, "index": index, "id": record["id"],
                    "argumentsDelta": member,
                    **({"name": record["name"]} if "name" in record else {}),
                })
            else:
                chunks.append({"type": delta_kind, "index": index, "text": member})
    return chunks


class AttemptBlocks:
    """One model attempt's chunks as AI SDK text and reasoning parts.

    ``key`` is the attempt's place in its session: ``(turn, step, ordinal)``
    where ``ordinal`` counts the attempts of that step. Block ids derive from
    it and the vendor's block index, never from the process-local attempt id,
    which a durable settlement does not carry.
    """

    def __init__(self, session_id: str, key: tuple[int, int, int]) -> None:
        self._session_id = session_id
        self.key = key
        #: content-block index → the open part ({"kind", "id"}).
        self._open: dict[int, dict[str, str]] = {}
        self.chunks = 0

    def _block_id(self, kind: str, index: int) -> str:
        turn, step, ordinal = self.key
        return f"dsh-{kind}:{self._session_id}:{turn}:{step}:{ordinal}:{index}"

    def chunk(self, chunk: dict[str, Any]) -> list[dict[str, Any]]:
        """Translate the attempt's next chunk; tool-call blocks settle through ``tool/call``."""
        self.chunks += 1
        kind = str(chunk.get("type") or "").strip()
        index = chunk.get("index")
        if kind == "block-start":
            part = {"text": "text", "reasoning": "reasoning"}.get(str(chunk.get("blockType") or ""))
            if part is None:
                return []
            if not isinstance(index, int) or isinstance(index, bool):
                raise DeepSeekHarnessProtocolError("block-start has no integer index")
            block_id = self._block_id(part, index)
            self._open[index] = {"kind": part, "id": block_id}
            return [{"type": f"{part}-start", "id": block_id}]
        if kind in {"text-delta", "reasoning-delta"}:
            expected = "text" if kind == "text-delta" else "reasoning"
            block = self._open.get(index) if isinstance(index, int) else None
            if block is None or block["kind"] != expected:
                raise DeepSeekHarnessProtocolError(f"{kind} at index {index!r} has no matching open block")
            return [{"type": f"{expected}-delta", "id": block["id"], "delta": str(chunk.get("text") or "")}]
        if kind == "block-end":
            block = self._open.pop(index, None) if isinstance(index, int) and not isinstance(index, bool) else None
            return [] if block is None else [{"type": f"{block['kind']}-end", "id": block["id"]}]
        if kind in {"usage", "finish", "tool-call-delta"}:
            # The step's usage travels on its durable settlement, a step's
            # finish is not the turn's end, and a tool call's complete
            # arguments arrive on the durable tool/call event.
            return []
        raise DeepSeekHarnessProtocolError(f"unknown assistant stream chunk type {kind!r}")

    def close(self) -> list[dict[str, Any]]:
        """End every block the attempt left open."""
        parts = [{"type": f"{block['kind']}-end", "id": block["id"]} for block in self._open.values()]
        self._open.clear()
        return parts


@dataclass(frozen=True, slots=True)
class FileTool:
    """A ``write``/``edit`` call whose result reports a file change."""

    name: str
    path: str


def tool_call_parts(data: dict[str, Any]) -> tuple[list[dict[str, Any]], FileTool | None]:
    """``tool/call`` → the tool part's input, and the file it writes when it is a file tool.

    The harness logs the call exactly as the model produced it, and records
    what became of it in the matching ``tool/result``. A call whose identity
    or arguments did not survive (an empty ``callId`` or ``name`` merged from
    a relay's stream deltas, or arguments that are not a JSON object) is shown
    as a tool input error rather than ending the conversation's stream.
    """
    call_id = str(data.get("callId") or "")
    name = str(data.get("name") or "")
    raw_arguments = data.get("arguments")
    problem: str | None = None
    arguments: Any = None
    if not call_id.strip() or not name.strip():
        problem = "the harness recorded this tool call without a call id or tool name"
    elif not isinstance(raw_arguments, str):
        problem = "the harness recorded this tool call without its arguments"
    else:
        try:
            arguments = json.loads(raw_arguments)
        except ValueError:
            problem = "the tool call's arguments are not valid JSON"
        else:
            if not isinstance(arguments, dict):
                problem = "the tool call's arguments are not a JSON object"
    if problem is not None:
        return [public_ui_frame({
            "type": "tool-input-error",
            "toolCallId": call_id,
            "toolName": name,
            "input": raw_arguments if isinstance(raw_arguments, str) else None,
            "errorText": problem,
        })], None
    file_tool = None
    if name in {"write", "edit"} and str(arguments.get("file_path") or "").strip():
        file_tool = FileTool(name=name, path=str(arguments["file_path"]))
    return [
        public_ui_frame({"type": "tool-input-start", "toolCallId": call_id, "toolName": name}),
        public_ui_frame({
            "type": "tool-input-available", "toolCallId": call_id, "toolName": name, "input": arguments,
        }),
    ], file_tool


def tool_result_parts(data: dict[str, Any], file_tool: FileTool | None) -> list[dict[str, Any]]:
    """``tool/result`` → the tool part's output, then the file change it made."""
    message = tool_result_message(data)
    tool_call_id = message["toolCallId"]
    parts: list[dict[str, Any]] = [{
        "type": "tool-output-available",
        "toolCallId": tool_call_id,
        "output": {"content": message["content"], "isError": message.get("isError", False)},
    }]
    if file_tool is None or message.get("isError") or not tool_call_id:
        return parts
    meta = data.get("meta")
    hunks = meta.get("diffs") if isinstance(meta, dict) else None
    changes: list[FileChange]
    if isinstance(hunks, list) and hunks:
        grouped: dict[str, list[ContentDiff]] = {}
        for hunk in hunks:
            grouped.setdefault(hunk["path"], []).append(ContentDiff(
                before=hunk["oldText"] if hunk["oldText"] is not None else "",
                after=hunk["newText"],
            ))
        changes = [FileChange(path=path, diff=ExcerptDiff(excerpts=excerpts)) for path, excerpts in grouped.items()]
    else:
        # A success receipt identifies a write, not its previous bytes.
        changes = [FileChange(path=file_tool.path, diff=None)]
    parts.append(file_changes_frame(tool_call_id, file_tool.name, changes))
    return parts


# ── interactions ─────────────────────────────────────────────────────────
#
# The two answerable requests the harness pushes on its `$events` stream,
# expressed as the platform's structural presentations. What the interaction
# means stays the vendor's: the whole request rides `raw_input` verbatim, and
# the encoders below read it back from there rather than from anything this
# platform rewrote.

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
        # The interaction record carries this as tool_call_id, which attaches
        # the approval to the tool call it gates.
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


def approval_response_value(*, denied: bool) -> str:
    """The supplier's one-shot ``ApprovalOutcome`` for a person's decision."""
    return "rejected" if denied else "allowed-once"


def question_response_value(
    pending: dict[str, Any],
    response: dict[str, Any],
) -> dict[str, Any]:
    """The ``AskUserQuestionAnswer`` that answers one question batch.

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
