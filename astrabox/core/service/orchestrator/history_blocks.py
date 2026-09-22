"""Folded display blocks derived from settled transcript records.

A history block is a *display* projection of one durable ``MessageRecord``: the
tool calls and thinking a settled response worked through are folded into a
single ``process_block`` header, and the blocks that were folded are handed
back separately when a reader opens that header. Folding changes only what a
page shows. The ``session_events`` log this reads stays untouched, the same
record always projects to the same blocks, and a reader who never opens a
header still receives the whole response through
:func:`project_record`'s ``details``.

Which records fold is decided from the platform's own settled facts — the
``result`` meter a finished turn carries, a ``turn_failure`` block, and each
``tool_result``'s ``tool_result_state``. No engine vocabulary is interpreted
here, and every function in this module is pure: it reads records and returns
new ones, with no I/O.

Two terminal states drive every decision below:

* **normal end** — no ``turn_failure`` block, a ``result`` block is present,
  and that result is not itself an error.
* **interrupted** — a ``turn_failure`` block is present (a user stop is one
  case of it), or the ``result`` block is an error.

A record in neither state is not collapsible and is returned unchanged; an
unfinished turn has no durable assistant record at all, so every record that
reaches this module is settled.
"""

from __future__ import annotations

import base64
import binascii
import json
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from astrabox.core.service.orchestrator.tool_result_semantics import (
    TOOL_RESULT_STATE_AVAILABLE,
)


#: Blocks carried verbatim into the text a process summary is written from.
#: Thinking is folded into the header but stays out of that text: it is the
#: engine's private reasoning, not an operation the reader watched happen.
_SUMMARY_BLOCK_TYPES = frozenset({"text", "tool_use", "tool_result"})

_BLOCK_CURSOR_VERSION = 1


@dataclass(frozen=True)
class HistoryBlock:
    """One record as a page shows it, plus the blocks its headers stand for.

    ``record`` is a copy of the source record carrying ``history_block_id`` and,
    where a group was folded, one ``process_block`` in place of that group's
    blocks. ``details`` maps each header's ``block_id`` to the blocks it
    replaced, in source order, so the detail read needs no second projection
    rule.
    """

    history_block_id: str
    record: dict[str, Any]
    details: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


@dataclass(frozen=True)
class ProcessSummaryInput:
    """Everything a summary model is given about one folded process."""

    process_text: str
    turn_completed: bool
    tool_count: int


@dataclass(frozen=True)
class _Atom:
    """One indivisible display unit: a tool call with its result, or one block.

    ``block_indexes`` point back into the record's own block list, so folding
    can rebuild the surviving blocks in source order instead of reassembling
    them from the atoms.
    """

    block_indexes: tuple[int, ...]
    is_tool: bool
    is_text: bool
    is_process: bool


@dataclass(frozen=True)
class _Group:
    """Consecutive atoms that one ``process_block`` header stands for."""

    atoms: tuple[_Atom, ...]
    summarize: bool


def _block_type(block: Any) -> str:
    if not isinstance(block, dict):
        return ""
    return str(block.get("type") or "").strip()


def _record_blocks(record: dict[str, Any]) -> list[dict[str, Any]]:
    value = record.get("blocks")
    if not isinstance(value, list):
        return []
    return [block for block in value if isinstance(block, dict)]


def _terminal_state(blocks: list[dict[str, Any]]) -> tuple[bool, bool]:
    """Return ``(normal_end, interrupted)`` for one settled record."""

    has_failure = any(_block_type(block) == "turn_failure" for block in blocks)
    result_block = next(
        (block for block in reversed(blocks) if _block_type(block) == "result"),
        None,
    )
    result_failed = result_block is not None and result_block.get("is_error") is True
    interrupted = has_failure or result_failed
    normal_end = not has_failure and result_block is not None and not result_failed
    return normal_end, interrupted


def _pair_tool_results(blocks: list[dict[str, Any]]) -> dict[int, int]:
    """Map each ``tool_use`` block index to its ``tool_result`` block index.

    Pairing is by ``tool_use_id`` rather than adjacency, and is resolved over
    the whole record before the walk below, so a result that precedes its call
    still binds to it instead of becoming a second atom.
    """

    tool_use_index: dict[str, int] = {}
    for index, block in enumerate(blocks):
        if _block_type(block) != "tool_use":
            continue
        tool_use_id = str(block.get("id") or "").strip()
        if tool_use_id and tool_use_id not in tool_use_index:
            tool_use_index[tool_use_id] = index
    paired: dict[int, int] = {}
    for index, block in enumerate(blocks):
        if _block_type(block) != "tool_result":
            continue
        owner = tool_use_index.get(str(block.get("tool_use_id") or "").strip())
        if owner is None or owner in paired:
            continue
        paired[owner] = index
    return paired


def process_atoms(blocks: list[dict[str, Any]]) -> list[_Atom]:
    """Split one assistant record's blocks into display atoms, in source order.

    A tool call and its result are one atom, and that atom is part of the
    process only while the result is ``output-available`` — a denied or errored
    result, and a call with no result at all, are things the reader has to be
    able to see. Blank text carries nothing to show and becomes no atom;
    everything else that is neither text, thinking nor a tool call is a trailer
    that never joins a process group.
    """

    paired = _pair_tool_results(blocks)
    folded_results = set(paired.values())
    atoms: list[_Atom] = []
    for index, block in enumerate(blocks):
        if index in folded_results:
            continue
        kind = _block_type(block)
        if kind == "tool_use":
            result_index = paired.get(index)
            state = (
                str(blocks[result_index].get("tool_result_state") or "").strip()
                if result_index is not None
                else ""
            )
            failed = state != TOOL_RESULT_STATE_AVAILABLE
            indexes = (index,) if result_index is None else (index, result_index)
            atoms.append(
                _Atom(
                    block_indexes=tuple(sorted(indexes)),
                    is_tool=True,
                    is_text=False,
                    is_process=not failed,
                )
            )
            continue
        if kind == "thinking":
            atoms.append(
                _Atom(
                    block_indexes=(index,),
                    is_tool=False,
                    is_text=False,
                    is_process=True,
                )
            )
            continue
        if kind == "text":
            if not str(block.get("text") or "").strip():
                continue
            atoms.append(
                _Atom(
                    block_indexes=(index,),
                    is_tool=False,
                    is_text=True,
                    is_process=False,
                )
            )
            continue
        atoms.append(
            _Atom(
                block_indexes=(index,),
                is_tool=False,
                is_text=False,
                is_process=False,
            )
        )
    return atoms


def _deferred_groups(blocks: list[dict[str, Any]]) -> list[_Group]:
    """Decide which atoms a header stands for, or return no groups at all.

    A response collapses only when it ran at least one tool AND it either was
    interrupted or ended normally with something to say afterwards — without a
    closing answer there would be nothing left on the page once the work was
    folded away.

    On a normal end every tool up to the end of the process folds in, errored
    and denied ones included: a tool that reported an error and was then worked
    around did not fail the turn, and the answer below already accounts for it.
    On an interruption those same tools stay outside and visible, because
    nothing came after them to explain what happened.
    """

    atoms = process_atoms(blocks)
    normal_end, interrupted = _terminal_state(blocks)
    last_tool = max((i for i, atom in enumerate(atoms) if atom.is_tool), default=-1)
    process_end = last_tool
    while (
        process_end >= 0
        and process_end + 1 < len(atoms)
        and atoms[process_end + 1].is_process
    ):
        process_end += 1
    conclusion = any(atom.is_text for atom in atoms[process_end + 1 :])
    collapse = last_tool >= 0 and (interrupted or (normal_end and conclusion))
    if collapse:
        folded = [
            atom
            for index, atom in enumerate(atoms)
            if index <= process_end
            and (
                atom.is_process
                or (normal_end and not interrupted and atom.is_tool)
                or atom.is_text
            )
        ]
        return [_Group(atoms=tuple(folded), summarize=True)] if folded else []
    if not (normal_end or interrupted):
        # Nothing here is settled enough to hide behind a header.
        return []
    groups: list[_Group] = []
    run: list[_Atom] = []
    for atom in atoms:
        if atom.is_process:
            run.append(atom)
            continue
        if run:
            groups.append(_Group(atoms=tuple(run), summarize=False))
            run = []
    if run:
        groups.append(_Group(atoms=tuple(run), summarize=False))
    return groups


def _group_block_indexes(group: _Group) -> list[int]:
    return sorted({index for atom in group.atoms for index in atom.block_indexes})


def project_record(record: dict[str, Any]) -> HistoryBlock:
    """Project one durable record into its display form.

    A user record is returned unchanged apart from ``history_block_id``. An
    assistant record keeps every block a header does not stand for, in its
    original order, with each header emitted at the position of the first block
    it folded away.
    """

    message_id = str(record.get("message_id") or "")
    projected = dict(record)
    projected["history_block_id"] = message_id
    if str(record.get("role") or "").strip() != "assistant":
        return HistoryBlock(history_block_id=message_id, record=projected)

    blocks = _record_blocks(record)
    groups = _deferred_groups(blocks)
    if not groups:
        return HistoryBlock(history_block_id=message_id, record=projected)

    details: dict[str, list[dict[str, Any]]] = {}
    header_at: dict[int, dict[str, Any]] = {}
    folded: set[int] = set()
    for group in groups:
        indexes = _group_block_indexes(group)
        block_id = f"{message_id}:p{indexes[0]}"
        details[block_id] = [deepcopy(blocks[index]) for index in indexes]
        header_at[indexes[0]] = {
            "type": "process_block",
            "process_details": {
                "block_id": block_id,
                "cursor": "",
                "session_id": str(record.get("session_id") or ""),
                "message_id": message_id,
                "turn_id": str(record.get("turn_id") or ""),
                "tool_count": sum(1 for atom in group.atoms if atom.is_tool),
                "summarize": group.summarize,
                "summary": None,
            },
        }
        folded.update(indexes)

    shown: list[dict[str, Any]] = []
    for index, block in enumerate(blocks):
        header = header_at.get(index)
        if header is not None:
            shown.append(header)
        if index in folded:
            continue
        shown.append(deepcopy(block))
    projected["blocks"] = shown
    return HistoryBlock(
        history_block_id=message_id,
        record=projected,
        details=details,
    )


def project_history_blocks(
    records: list[dict[str, Any]],
    checkpoint_cursor: str,
) -> list[HistoryBlock]:
    """Project a page of records and stamp each header with the page's cursor.

    The cursor is what a detail read sends back, so the blocks it opens are the
    ones this page folded rather than whatever the transcript holds later.
    """

    projected: list[HistoryBlock] = []
    for record in records:
        block = project_record(record)
        for item in block.record.get("blocks") or []:
            details = item.get("process_details") if isinstance(item, dict) else None
            if isinstance(details, dict):
                details["cursor"] = checkpoint_cursor
        projected.append(block)
    return projected


def process_summary_input(
    assistant_record: dict[str, Any],
) -> ProcessSummaryInput | None:
    """Return what to summarize for one record, or ``None`` when nothing folds.

    This is the single rule for "can this response be summarized": the browser
    asking for a summary and the turn worker offering one both go through it,
    so neither can claim a summary for a record the page shows in full.
    """

    if str(assistant_record.get("role") or "").strip() != "assistant":
        return None
    blocks = _record_blocks(assistant_record)
    normal_end, interrupted = _terminal_state(blocks)
    groups = _deferred_groups(blocks)
    if len(groups) != 1 or not groups[0].summarize:
        return None
    group = groups[0]
    process_blocks = [
        blocks[index]
        for index in _group_block_indexes(group)
        if _block_type(blocks[index]) in _SUMMARY_BLOCK_TYPES
    ]
    return ProcessSummaryInput(
        process_text=json.dumps(process_blocks, ensure_ascii=False),
        turn_completed=normal_end and not interrupted,
        tool_count=sum(1 for atom in group.atoms if atom.is_tool),
    )


def preceding_user_text(
    records: list[dict[str, Any]],
    message_id: str,
) -> str:
    """Return the text of the nearest user record before ``message_id``.

    ``records`` is one turn's projected messages in order. A turn with a native
    input queue has several user records and several responses inside it, so
    the nearest preceding one — not the turn's first — is the request the
    response answered.
    """

    text = ""
    for record in records:
        if str(record.get("message_id") or "") == message_id:
            return text
        if str(record.get("role") or "").strip() == "user":
            text = str(record.get("content") or "")
    return ""


def encode_block_cursor(through_seq: int, before_block_id: str | None) -> str:
    """Encode a history-block page position as one opaque token."""

    payload: dict[str, Any] = {
        "v": _BLOCK_CURSOR_VERSION,
        "through_seq": int(through_seq),
    }
    if before_block_id:
        payload["before"] = str(before_block_id)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_block_cursor(value: str) -> tuple[int, str | None]:
    """Decode a cursor into ``(through_seq, before_block_id)``.

    Raises :class:`ValueError` for anything this module did not write. The
    caller turns that into a client error rather than serving a page against a
    checkpoint it cannot verify.
    """

    text = str(value or "")
    if not text:
        raise ValueError("block cursor is empty")
    padded = text + "=" * (-len(text) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"block cursor is not decodable: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("block cursor must encode an object")
    if payload.get("v") != _BLOCK_CURSOR_VERSION:
        raise ValueError("block cursor version is not supported")
    through_seq = payload.get("through_seq")
    if isinstance(through_seq, bool) or not isinstance(through_seq, int):
        raise ValueError("block cursor through_seq must be an integer")
    if through_seq < 0:
        raise ValueError("block cursor through_seq must not be negative")
    before = payload.get("before")
    if before is not None and not isinstance(before, str):
        raise ValueError("block cursor before must be a string")
    return through_seq, (before or None)
