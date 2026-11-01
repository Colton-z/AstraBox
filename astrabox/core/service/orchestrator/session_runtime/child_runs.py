"""A session's child runs: the sub-agents its runtime reports.

An engine adapter reports a child run's lifecycle and messages as
``data-subagent`` parts whose data follows ``engine/child_runs.py``; the
session's reader stores them in the output log with every other part. This
module folds them, in log order, into one entry per child: its public id, its
parent, the engine's own status words, the operations it offers, and what it
said.

A child's messages are the runtime's own transcript of it when the engine
keeps one in the session's SessionStore mirror (``EngineStoredChildTranscript``):
that is the complete record, and the only one for a child whose messages the
runtime never streamed. Otherwise they are the message parts the runtime
streamed. An engine that mirrors tool output separately fills a closed child's
missing tool results from its transcripts (``EngineStoredChildToolResults``).

The engine's native references (``engineRef``, ``controlRef``) stay inside
this module; a child is addressed by the public id
:func:`~astrabox.core.service.orchestrator.engine.child_runs.public_child_run_id`
mints.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.core.service.orchestrator.engine.base import (
    EngineStoredChildRuns,
    EngineStoredChildToolResults,
    EngineStoredChildTranscript,
)
from astrabox.core.service.orchestrator.engine.child_runs import (
    ChildRunProjectionError,
    canonical_child_run_data,
    public_child_message_id,
    public_child_run_id,
)
from astrabox.core.service.orchestrator.engine.emissions import ChildResourceFact
from astrabox.core.service.orchestrator.engine.frame_scope import pop_engine_frame_scope
from astrabox.core.service.orchestrator.engine.registry import get_engine_adapter
from astrabox.core.service.orchestrator.message_blocks import merge_projected_message_blocks
from astrabox.persistence.repository.session_read_repository import SessionReadRepository
from astrabox.persistence.repository.transcript_child_run_repository import TranscriptChildRunRepository
from astrabox.persistence.repository.transcript_entry_repository import TranscriptEntryRepository

logger = get_logger(__name__)


def _new_entry(*, child_run_id: str, engine_kind: str, engine_ref: str, order: int) -> dict[str, Any]:
    return {
        "child_run_id": child_run_id,
        "engine_kind": engine_kind,
        "depth": 1,
        "closed": False,
        "operations": [],
        "tool_call_ids": [],
        "messages": [],
        "_engine_ref": engine_ref,
        "_message_indexes": {},
        "_first_seen": order,
    }


def _apply_parent(entry: dict[str, Any], parent_child_run_id: str) -> None:
    if not parent_child_run_id:
        return
    child_run_id = str(entry["child_run_id"])
    if parent_child_run_id == child_run_id:
        raise ChildRunProjectionError(f"child-run is its own parent child_run_id={child_run_id!r}")
    existing = str(entry.get("parent_child_run_id") or "")
    if existing and existing != parent_child_run_id:
        raise ChildRunProjectionError(
            f"child-run changed parent child_run_id={child_run_id!r} parents={existing!r},{parent_child_run_id!r}"
        )
    entry["parent_child_run_id"] = parent_child_run_id


def _apply_lifecycle(entry: dict[str, Any], data: dict[str, Any]) -> None:
    event = str(data["event"])
    if (
        entry.get("closed")
        and event != "closed"
        and not get_engine_adapter(str(entry["engine_kind"])).child_run_fact_reopens(data)
    ):
        # The engine reads this fact as a report on the activation the close
        # ended; applying it would reopen the child and replace its terminal
        # status, operations and usage.
        return
    _apply_parent(entry, str(data.get("parentChildRunId") or ""))
    tool_call_id = data.get("toolCallId")
    if isinstance(tool_call_id, str) and tool_call_id and tool_call_id not in entry["tool_call_ids"]:
        entry["tool_call_ids"].append(tool_call_id)
    control_ref = str(data.get("controlRef") or "").strip()
    if control_ref:
        existing_control = str(entry.get("_control_ref") or "")
        if existing_control and existing_control != control_ref:
            raise ChildRunProjectionError(
                f"child-run changed control reference child_run_id={entry['child_run_id']!r}"
            )
        entry["_control_ref"] = control_ref
    for source, target in (
        ("description", "description"),
        ("taskType", "task_type"),
        ("lastToolName", "last_tool_name"),
        ("summary", "summary"),
        ("model", "model"),
    ):
        value = data.get(source)
        if isinstance(value, str) and value.strip():
            entry[target] = value.strip()
    usage = data.get("usage")
    if isinstance(usage, dict):
        entry["usage"] = {**dict(entry.get("usage") or {}), **usage}
    if event == "opened":
        # A new activation has no terminal status until the engine supplies one.
        entry.pop("engine_status", None)
        entry.pop("engine_reason", None)
    if entry.get("closed") and event != "closed":
        # Whether a child may live again is the engine's answer, given above.
        logger.info("child-run %s reports %r after it closed; reopening it", entry["child_run_id"], event)
        entry["closed"] = False
    if "engineEvent" in data:
        entry["engine_event"] = str(data["engineEvent"])
    if "nativeState" in data:
        entry["_native_state"] = dict(data["nativeState"])
    for source, target in (("engineStatus", "engine_status"), ("engineReason", "engine_reason")):
        if source in data:
            entry[target] = str(data[source])
    entry["operations"] = list(data["operations"])
    if event == "closed":
        entry["closed"] = True


def _apply_message(entry: dict[str, Any], data: dict[str, Any], *, block_id: str, public_message_id: str) -> None:
    _apply_parent(entry, str(data.get("parentChildRunId") or ""))
    role = str(data.get("role") or "")
    content = data.get("content")
    if role not in ("assistant", "user") or not isinstance(content, list):
        raise ChildRunProjectionError(f"child-run message is malformed child_run_id={entry['child_run_id']!r}")
    message_ref = str(data.get("messageId") or "").strip()
    key = (
        f"message:{message_ref}"
        if message_ref
        else f"block:{block_id}:{json.dumps(data, sort_keys=True, separators=(',', ':'))}"
    )
    existing_index = entry["_message_indexes"].get(key)
    if existing_index is not None:
        existing = entry["messages"][existing_index]
        if existing["role"] != role:
            raise ChildRunProjectionError(
                f"child-run message changed role child_run_id={entry['child_run_id']!r} message_key={key!r}"
            )
        # One engine message can arrive in several fragments; they merge.
        existing["content"] = merge_projected_message_blocks(existing["content"], content)
        return
    entry["_message_indexes"][key] = len(entry["messages"])
    entry["messages"].append(
        {
            "role": role,
            "content": [dict(block) for block in content if isinstance(block, dict)],
            "message_id": public_message_id,
        }
    )


def _tree_order(entries: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    children: dict[str, list[dict[str, Any]]] = defaultdict(list)
    roots: list[dict[str, Any]] = []
    for entry in entries.values():
        parent = str(entry.get("parent_child_run_id") or "")
        if parent and parent in entries:
            children[parent].append(entry)
        else:
            roots.append(entry)

    def seen(entry: dict[str, Any]) -> int:
        return int(entry["_first_seen"])

    roots.sort(key=seen)
    for siblings in children.values():
        siblings.sort(key=seen)
    ordered: list[dict[str, Any]] = []
    visited: set[str] = set()

    def visit(entry: dict[str, Any], depth: int, ancestors: frozenset[str]) -> None:
        child_run_id = str(entry["child_run_id"])
        if child_run_id in ancestors:
            raise ChildRunProjectionError(f"child-run parent cycle includes child_run_id={child_run_id!r}")
        if child_run_id in visited:
            return
        visited.add(child_run_id)
        entry["depth"] = depth
        ordered.append(entry)
        for child in children.get(child_run_id, []):
            visit(child, depth + 1, ancestors | {child_run_id})

    for root in roots:
        visit(root, 1, frozenset())
    for entry in sorted(entries.values(), key=seen):
        if str(entry["child_run_id"]) not in visited:
            visit(entry, 1, frozenset())
    return ordered


def fold_child_runs(
    session_id: str, parts: Sequence[dict[str, Any]], *, with_messages: bool
) -> list[dict[str, Any]]:
    """Fold child-run parts (``{"id", "data"}``, in log order) into entries, parents before children.

    Entries keep their private keys (``_engine_ref``, ``_control_ref``,
    ``_native_state``); :func:`public_child_run` removes them. Without
    ``with_messages`` message parts count only for lineage, and may come
    without their content.
    """
    entries: dict[str, dict[str, Any]] = {}
    signatures: dict[str, str] = {}
    for order, part in enumerate(parts):
        block_id = str(part.get("id") or "").strip()
        raw = part.get("data")
        if not block_id or not isinstance(raw, dict):
            raise ChildRunProjectionError("child-run part lacks a stable id or data")
        if not with_messages and raw.get("kind") == "message":
            raw = {**raw, "content": []}
        engine_kind = str(raw.get("engineKind") or "").strip()
        data = canonical_child_run_data(raw, engine_kind=engine_kind)
        if data["kind"] == "message" and with_messages:
            data["content"] = get_engine_adapter(engine_kind).canonical_child_message_content(data["content"])
        signature = json.dumps(data, sort_keys=True, separators=(",", ":"))
        previous = signatures.get(block_id)
        if previous is not None:
            if previous != signature:
                raise ChildRunProjectionError(f"child-run block id was reused block_id={block_id!r}")
            continue
        signatures[block_id] = signature
        engine_ref = str(data["engineRef"])
        child_run_id = public_child_run_id(session_id=session_id, engine_kind=engine_kind, engine_ref=engine_ref)
        parent_ref = str(data.get("parentEngineRef") or "")
        if parent_ref:
            data["parentChildRunId"] = public_child_run_id(
                session_id=session_id, engine_kind=engine_kind, engine_ref=parent_ref
            )
        entry = entries.get(child_run_id)
        if entry is None:
            entry = _new_entry(child_run_id=child_run_id, engine_kind=engine_kind, engine_ref=engine_ref, order=order)
            entries[child_run_id] = entry
        elif entry["engine_kind"] != engine_kind or entry["_engine_ref"] != engine_ref:
            raise ChildRunProjectionError(f"public child-run id collision child_run_id={child_run_id!r}")
        if data["kind"] == "lifecycle":
            _apply_lifecycle(entry, data)
        elif with_messages:
            _apply_message(
                entry,
                data,
                block_id=block_id,
                public_message_id=public_child_message_id(
                    session_id=session_id,
                    engine_kind=engine_kind,
                    engine_ref=engine_ref,
                    message_ref=str(data.get("messageId") or "").strip() or block_id,
                ),
            )
        else:
            _apply_parent(entry, str(data.get("parentChildRunId") or ""))
    ordered = _tree_order(entries)
    for entry in ordered:
        if not str(entry.get("engine_event") or "").strip() and not entry.get("_native_state"):
            raise ChildRunProjectionError(
                f"child-run has messages but no lifecycle fact child_run_id={entry['child_run_id']!r}"
            )
    return ordered


def public_child_run(entry: dict[str, Any]) -> dict[str, Any]:
    """An entry as the console sees it, with the engine's own reading of whether it works."""
    public = {
        key: value
        for key, value in entry.items()
        if not key.startswith("_") and key != "messages"
    }
    public["active"] = get_engine_adapter(str(entry["engine_kind"])).child_run_is_active(
        {**public, **({"_native_state": entry["_native_state"]} if "_native_state" in entry else {})}
    )
    public.setdefault("engine_event", None)
    return public


async def list_child_runs(reads: SessionReadRepository, session_id: str) -> list[dict[str, Any]]:
    """Every child run of the session, parents before their children."""
    return [public_child_run(entry) for entry in await _child_run_entries(reads, session_id)]


async def _child_run_entries(reads: SessionReadRepository, session_id: str) -> list[dict[str, Any]]:
    """Combine streamed children with native children that never reached the stream."""
    parts = await reads.child_run_parts(session_id, with_content=False)
    entries = fold_child_runs(session_id, parts, with_messages=False)
    known = {entry["child_run_id"] for entry in entries}
    for engine_kind in dict.fromkeys(entry["engine_kind"] for entry in entries):
        adapter = get_engine_adapter(engine_kind)
        if not isinstance(adapter, EngineStoredChildRuns):
            continue
        scopes = await TranscriptChildRunRepository().scopes(
            session_id, engine_kind=engine_kind, projector=adapter.project_child_run_entries,
        )
        facts = adapter.stored_child_run_facts(
            main_entries=[entry for scope in scopes if not scope["subpath"] for entry in scope["entries"]],
            raw_scopes=[scope for scope in scopes if scope["subpath"]],
        )
        for fact in facts:
            frame = fact.as_frame()
            if pop_engine_frame_scope(frame) != "session" or frame.get("type") != "data-subagent":
                raise ChildRunProjectionError("stored child registry returned a non-child fact")
            data = canonical_child_run_data(frame["data"], engine_kind=engine_kind)
            if data["kind"] != "lifecycle":
                raise ChildRunProjectionError("stored child registry returned a non-lifecycle fact")
            child_id = public_child_run_id(session_id=session_id, engine_kind=engine_kind, engine_ref=data["engineRef"])
            if child_id not in known:
                parts.append({"id": frame["id"], "data": data})
    return fold_child_runs(session_id, parts, with_messages=False)


async def find_child_run(
    reads: SessionReadRepository, session_id: str, child_run_id: str
) -> dict[str, Any] | None:
    """One child's entry with its private references, or ``None``."""
    for entry in await _child_run_entries(reads, session_id):
        if entry["child_run_id"] == child_run_id:
            return entry
    return None


async def child_run_messages(
    reads: SessionReadRepository, session_id: str, child_run_id: str
) -> list[dict[str, Any]] | None:
    """One child's messages, oldest first; ``None`` when the session has no such child."""
    entry = await find_child_run(reads, session_id, child_run_id)
    if entry is None:
        return None
    engine_kind = str(entry["engine_kind"])
    engine_ref = str(entry["_engine_ref"])
    adapter = get_engine_adapter(engine_kind)
    if isinstance(adapter, EngineStoredChildTranscript):
        stored = adapter.stored_child_transcript_facts(
            engine_ref=engine_ref,
            closed=bool(entry["closed"]),
            native_state=dict(entry.get("_native_state") or {}),
            raw_scopes=await reads.child_transcript_scopes(session_id),
        )
        if stored:
            return _stored_messages(session_id, entry, stored)
    parts = await reads.child_run_parts(session_id, engine_ref=engine_ref, with_content=True)
    folded = next(
        (
            child
            for child in fold_child_runs(session_id, parts, with_messages=True)
            if child["child_run_id"] == child_run_id
        ),
        None,
    )
    messages = [dict(message) for message in (folded or {}).get("messages", [])]
    if entry["closed"] and isinstance(adapter, EngineStoredChildToolResults):
        messages = adapter.enrich_stored_child_tool_results(
            engine_ref=engine_ref,
            raw_scopes=await reads.child_transcript_scopes(session_id),
            messages=messages,
        )
    return messages


def _stored_messages(
    session_id: str, entry: dict[str, Any], facts: Sequence[ChildResourceFact]
) -> list[dict[str, Any]]:
    """The messages of one child from its engine's stored transcript facts."""
    engine_kind = str(entry["engine_kind"])
    engine_ref = str(entry["_engine_ref"])
    folded = _new_entry(
        child_run_id=str(entry["child_run_id"]), engine_kind=engine_kind, engine_ref=engine_ref, order=0
    )
    for fact in facts:
        if not isinstance(fact, ChildResourceFact):
            raise ChildRunProjectionError("stored child history returned a non-child fact")
        frame = fact.as_frame()
        if pop_engine_frame_scope(frame) != "session" or frame.get("type") != "data-subagent":
            raise ChildRunProjectionError("stored child history returned a frame that is not a child fact")
        data = canonical_child_run_data(frame["data"], engine_kind=engine_kind)
        if data["kind"] != "message" or data["engineRef"] != engine_ref:
            raise ChildRunProjectionError("stored child history changed the selected child")
        block_id = str(frame.get("id") or "")
        _apply_message(
            folded,
            data,
            block_id=block_id,
            public_message_id=public_child_message_id(
                session_id=session_id,
                engine_kind=engine_kind,
                engine_ref=engine_ref,
                message_ref=str(data.get("messageId") or "").strip() or block_id,
            ),
        )
    return [dict(message) for message in folded["messages"]]
