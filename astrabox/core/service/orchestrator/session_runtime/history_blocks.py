"""A conversation's history with each ended response's work folded behind a header.

The console groups a response's UI message parts the way
``frontend/src/session/components/MessageParts.tsx`` (``groupAssistantProcess``
and ``MessageBubble``) does: consecutive work, meaning reasoning and tool calls
that returned their output, forms a process group, and once the response has
ended, everything up to its last tool call that the reader does not have to act
on folds behind one header, provided the response either completed and said
something after its last tool call, or stopped short. A response that ended
without such a fold still shows each process group behind a header of its own.

A history page gets the same fold from the server, so the work does not travel
with the page: each fold becomes one ``data-process-block`` part in place of
the parts it stands for, and :func:`load_block_parts` reads those parts when a
reader opens the header. Both reads are pinned to the output position the page
was read at (its cursor), and only a response whose parts had all been stored by
that position folds, so a header opens to exactly the work the page folded.

How a response ended is the runtime's recorded outcome: ``completed``, or
``failed`` / ``cancelled`` for one that stopped short.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from astrabox.core.service.orchestrator.session_runtime.ui_message import history_messages
from astrabox.persistence.repository.session_read_repository import SessionReadRepository
from astrabox.persistence.repository.session_runtime_repository import SessionRuntimeRepository

PROCESS_BLOCK_PART = "data-process-block"

#: Data parts the console shows on their own: an outcome, a decision, a fold or
#: a platform tool's card. They never join a process group.
_STANDALONE_PARTS = frozenset(
    {"data-result", "data-raw-event", "data-api-retry", "data-interaction", "data-tool-card", "data-input-consumed", PROCESS_BLOCK_PART}
)
#: Standalone parts a fold leaves in view wherever they sit.
_ALERT_PARTS = frozenset({"data-result", "data-raw-event", "data-api-retry", "data-interaction", "data-input-consumed"})
_FAILED_TOOL_STATES = frozenset({"output-error", "output-denied"})
#: A call that never returned: the one a stop landed on, or one waiting on a person.
_UNFINISHED_TOOL_STATES = frozenset(
    {"input-streaming", "input-available", "approval-requested", "approval-responded"}
)

_CURSOR_VERSION = 1


class HistoryCursorError(ValueError):
    """A cursor this module did not issue, or one of the wrong kind for the read."""


@dataclass(frozen=True)
class Fold:
    """One ended response as a history page shows it.

    ``parts`` are the message's parts with each fold replaced by its header.
    ``details`` maps each header's block id to the parts it stands for, in
    order. ``summary_text`` is the folded work a label describes, present when
    one header stands for all of the response's work.
    """

    parts: list[dict[str, Any]]
    details: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    summary_text: str | None = None
    response_completed: bool = False


@dataclass(frozen=True)
class _Group:
    indexes: list[int]
    process: bool


def _type(part: Mapping[str, Any]) -> str:
    return str(part.get("type") or "")


def _is_tool(part: Mapping[str, Any]) -> bool:
    kind = _type(part)
    return kind == "dynamic-tool" or kind.startswith("tool-")


def _tool_state(part: Mapping[str, Any]) -> str:
    return str(part.get("state") or "")


def _tool_failed(part: Mapping[str, Any]) -> bool:
    state = _tool_state(part)
    if state in _FAILED_TOOL_STATES:
        return True
    approval = part.get("approval")
    return state == "approval-responded" and isinstance(approval, dict) and approval.get("approved") is False


def _is_process(part: Mapping[str, Any]) -> bool:
    return _type(part) == "reasoning" or (_is_tool(part) and _tool_state(part) == "output-available")


def _is_carried(part: Mapping[str, Any]) -> bool:
    """A step boundary or a data part the console carries beside the work, such
    as a Write call's file changes: it neither starts nor ends a group and is
    never folded, so the Diff panel still finds it on a folded response."""
    kind = _type(part)
    return kind == "step-start" or (kind.startswith("data-") and kind not in _STANDALONE_PARTS)


def _is_blank_text(part: Mapping[str, Any]) -> bool:
    return _type(part) == "text" and not str(part.get("text") or "").strip()


def _groups(parts: Sequence[Mapping[str, Any]]) -> list[_Group]:
    groups: list[_Group] = []
    for index, part in enumerate(parts):
        if _is_carried(part) or _is_blank_text(part):
            continue
        process = _is_process(part)
        if process and groups and groups[-1].process:
            groups[-1].indexes.append(index)
        else:
            groups.append(_Group([index], process))
    return groups


def fold_parts(
    parts: Sequence[Mapping[str, Any]],
    *,
    outcome: str | None,
    message_id: str,
    session_id: str,
    cursor: str,
) -> Fold:
    """Fold one ended response's parts; ``outcome`` is the runtime's recorded ending.

    ``cursor`` is the position the page was read at, stamped on each header so
    that opening it reads the parts this fold stood for.
    """
    completed = outcome == "completed"
    stopped = outcome in ("failed", "cancelled")
    groups = _groups(parts)
    tool_groups = [index for index, group in enumerate(groups) if any(_is_tool(parts[i]) for i in group.indexes)]
    last_tool = tool_groups[-1] if tool_groups else -1
    conclusion = any(_type(parts[group.indexes[0]]) == "text" for group in groups[last_tool + 1 :])

    def alert(group: _Group) -> bool:
        for i in group.indexes:
            part = parts[i]
            if _type(part) in _ALERT_PARTS:
                return True
            if _is_tool(part) and (
                _tool_state(part) in _UNFINISHED_TOOL_STATES
                or (not (completed and conclusion) and _tool_failed(part))
            ):
                return True
        return False

    waiting = any(_is_tool(part) and _tool_state(part) == "approval-requested" for part in parts)
    folded = [group for group in groups[: last_tool + 1] if not alert(group)]
    headers: list[tuple[list[int], bool]] = []
    has_input_boundary = any(_type(part) == "data-input-consumed" for part in parts)
    if last_tool >= 0 and not waiting and folded and not has_input_boundary and ((completed and conclusion) or stopped):
        headers = [(sorted(i for group in folded for i in group.indexes), True)]
    elif completed or stopped:
        headers = [(group.indexes, False) for group in groups if group.process]
    if not headers:
        return Fold(parts=[deepcopy(dict(part)) for part in parts])

    details: dict[str, list[dict[str, Any]]] = {}
    header_at: dict[int, dict[str, Any]] = {}
    for indexes, summarize in headers:
        block_id = f"{message_id}:p{indexes[0]}"
        details[block_id] = [deepcopy(dict(parts[i])) for i in indexes]
        header_at[indexes[0]] = {
            "type": PROCESS_BLOCK_PART,
            "id": block_id,
            "data": {
                "block_id": block_id,
                "cursor": cursor,
                "session_id": session_id,
                "message_id": message_id,
                "tool_count": sum(1 for i in indexes if _is_tool(parts[i])),
                "summarize": summarize,
                "summary": None,
            },
        }
    hidden = {i for indexes, _summarize in headers for i in indexes}
    shown: list[dict[str, Any]] = []
    for index, part in enumerate(parts):
        if index in header_at:
            shown.append(header_at[index])
        if index not in hidden:
            shown.append(deepcopy(dict(part)))
    summary_text = None
    if headers[0][1]:
        # Reasoning folds with the work but stays out of the label's input: it
        # is the engine's private thinking, not an operation the reader saw.
        summary_text = json.dumps(
            [
                part
                for part in details[f"{message_id}:p{headers[0][0][0]}"]
                if _type(part) == "text" or _is_tool(part)
            ],
            ensure_ascii=False,
        )
    return Fold(parts=shown, details=details, summary_text=summary_text, response_completed=completed)


# --------------------------------------------------------------------------
# Cursors
# --------------------------------------------------------------------------


def encode_cursor(through_seq: int, before: tuple[str, int, int] | None = None) -> str:
    """A history position: the output ``seq`` the read is pinned to and, for a page, the oldest entry shown."""
    payload: dict[str, Any] = {"v": _CURSOR_VERSION, "through_seq": int(through_seq)}
    if before is not None:
        payload["before"] = list(before)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str) -> tuple[int, tuple[str, int, int] | None]:
    """``(through_seq, before)`` from a cursor :func:`encode_cursor` issued."""
    text = str(value or "").strip()
    if not text:
        raise HistoryCursorError("history cursor is empty")
    try:
        payload = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise HistoryCursorError("history cursor is not one this server issued") from exc
    if not isinstance(payload, dict) or payload.get("v") != _CURSOR_VERSION:
        raise HistoryCursorError("history cursor is not one this server issued")
    through_seq = payload.get("through_seq")
    if isinstance(through_seq, bool) or not isinstance(through_seq, int) or through_seq < 0:
        raise HistoryCursorError("history cursor names no output position")
    before = payload.get("before")
    if before is None:
        return through_seq, None
    try:
        at, kind, seq = before
        return through_seq, (str(at), int(kind), int(seq))
    except (TypeError, ValueError) as exc:
        raise HistoryCursorError("history cursor names no history entry") from exc


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class HistoryPage:
    """One page of history with every ended response folded.

    ``labelled`` maps the id of each response whose single header a label
    describes to that header's data, for the label to be filled in.
    """

    messages: list[dict[str, Any]]
    has_more: bool
    through_seq: int
    next_cursor: str | None
    labelled: dict[str, dict[str, Any]]


async def load_history_page(
    facts: SessionRuntimeRepository,
    reads: SessionReadRepository,
    session_id: str,
    *,
    cursor: str | None,
    limit: int,
) -> HistoryPage:
    """One page of history, newest first in pages and oldest first within one.

    Without ``cursor`` the page starts at the newest entry and is pinned to the
    output stored so far; ``next_cursor`` keeps every older page on that same
    position.
    """
    if cursor is None:
        through_seq, _reader_cursor = await facts.resume_point(session_id)
        before = None
    else:
        through_seq, before = decode_cursor(cursor)
        if before is None:
            raise HistoryCursorError("a history page needs a page cursor, not a header's")
    page = await facts.history_page(session_id, before=before, limit=limit)
    response_ids = [str(row["response_id"]) for row in page["responses"]]
    outputs = await facts.output_for_responses(session_id, response_ids, through_seq=through_seq)
    interactions = await facts.response_interactions(session_id, response_ids)
    last_seqs = await reads.last_output_seqs(session_id, response_ids)
    ended = {
        str(row["response_id"])
        for row in page["responses"]
        if row["status"] == "ended" and last_seqs.get(str(row["response_id"]), 0) <= through_seq
    }
    checkpoint = encode_cursor(through_seq)
    messages: list[dict[str, Any]] = []
    labelled: dict[str, dict[str, Any]] = {}
    for message in history_messages(page, outputs, interactions):
        response_id = str(message["metadata"].get("response_id") or "")
        if message["role"] == "assistant" and response_id in ended:
            fold = fold_parts(
                message["parts"],
                outcome=message["metadata"].get("outcome"),
                message_id=str(message["id"]),
                session_id=session_id,
                cursor=checkpoint,
            )
            message = {**message, "parts": fold.parts}
            if fold.summary_text is not None:
                labelled[str(message["id"])] = next(
                    part["data"] for part in fold.parts if _type(part) == PROCESS_BLOCK_PART
                )
        messages.append(message)
    return HistoryPage(
        messages=messages,
        has_more=bool(page["has_more"]),
        through_seq=through_seq,
        next_cursor=encode_cursor(through_seq, page["before"]) if page["has_more"] and page["before"] else None,
        labelled=labelled,
    )


@dataclass(frozen=True)
class LoadedResponse:
    """One response as history shows it, folded when it had ended by the position read.

    ``last_seq`` is the ``seq`` of the response's last stored part, 0 for none.
    """

    row: dict[str, Any]
    message: dict[str, Any]
    fold: Fold | None
    last_seq: int


async def load_response(
    facts: SessionRuntimeRepository,
    reads: SessionReadRepository,
    session_id: str,
    response_id: str,
    *,
    through_seq: int | None,
) -> LoadedResponse | None:
    """One response through ``through_seq`` (all of it for ``None``); ``None`` when there is no such response."""
    row = await facts.get_response(session_id, response_id)
    if row is None:
        return None
    outputs = await facts.output_for_responses(session_id, [response_id], through_seq=through_seq)
    interactions = await facts.response_interactions(session_id, [response_id])
    last_seq = (await reads.last_output_seqs(session_id, [response_id])).get(response_id, 0)
    single = {"entries": [{"kind": "response", "seq": int(row["seq"])}], "inputs": [], "responses": [row]}
    message = history_messages(single, outputs, interactions)[0]
    fold = None
    if row["status"] == "ended" and (through_seq is None or last_seq <= through_seq):
        fold = fold_parts(
            message["parts"],
            outcome=row.get("outcome"),
            message_id=str(message["id"]),
            session_id=session_id,
            cursor=encode_cursor(through_seq if through_seq is not None else last_seq),
        )
    return LoadedResponse(row=row, message=message, fold=fold, last_seq=last_seq)


async def load_block_parts(
    facts: SessionRuntimeRepository,
    reads: SessionReadRepository,
    session_id: str,
    block_id: str,
    *,
    cursor: str,
) -> tuple[LoadedResponse, list[dict[str, Any]]] | None:
    """The parts one header stood for on the page that issued ``cursor``; ``None`` when that page had no such header."""
    through_seq, before = decode_cursor(cursor)
    if before is not None:
        raise HistoryCursorError("a header opens with the cursor it carries, not a page cursor")
    response_id, separator, _index = str(block_id).rpartition(":p")
    if not separator or not response_id:
        return None
    loaded = await load_response(facts, reads, session_id, response_id, through_seq=through_seq)
    if loaded is None or loaded.fold is None:
        return None
    parts = loaded.fold.details.get(block_id)
    if parts is None:
        return None
    return loaded, parts
