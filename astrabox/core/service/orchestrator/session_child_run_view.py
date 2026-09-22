"""Session-scoped child-run read model derived from the canonical event log."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.core.service.orchestrator.engine.base import (
    ENGINE_MESSAGE_EVENT_TYPE,
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

logger = get_logger(__name__)

_CHILD_RUN_EVENT_TYPES = frozenset(
    {
        "turn.completed",
        "turn.failed",
        "turn.recovered",
        "turn.background_tasks_materialized",
    }
)
_CHILD_RUN_SOURCE_EVENT_TYPES = _CHILD_RUN_EVENT_TYPES | frozenset(
    {ENGINE_MESSAGE_EVENT_TYPE}
)


def _frame_seq(row: dict[str, Any]) -> int:
    value = row.get("frame_seq")
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


def _event_seq(row: dict[str, Any]) -> int:
    value = row.get("event_seq")
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


def _child_block_from_frame(frame: dict[str, Any]) -> dict[str, Any] | None:
    payload = frame.get("payload")
    if not isinstance(payload, dict):
        return None
    if str(payload.get("type") or "").strip() != "data-subagent":
        return None
    if (
        str(frame.get("scope") or "").strip() != "session"
        or str(frame.get("turn_id") or "").strip()
    ):
        raise ChildRunProjectionError("child-run frames must be session scoped and have no turn_id")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ChildRunProjectionError("child-run frame data must be an object")
    block_id = str(payload.get("id") or "").strip()
    if not block_id:
        raise ChildRunProjectionError("child-run frame lacks stable id")
    return {"type": "subagent", "id": block_id, "data": dict(data)}


def _child_blocks_from_event(event: dict[str, Any]) -> list[dict[str, Any]]:
    if str(event.get("event_type") or "").strip() not in _CHILD_RUN_EVENT_TYPES:
        return []
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return []
    blocks = payload.get("blocks")
    if not isinstance(blocks, list):
        return []
    return [
        dict(block)
        for block in blocks
        if isinstance(block, dict) and str(block.get("type") or "").strip() == "subagent"
    ]


def _durable_child_frames(
    *,
    session_id: str,
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for event in sorted(events, key=_event_seq):
        if str(event.get("event_type") or "").strip() != ENGINE_MESSAGE_EVENT_TYPE:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise ChildRunProjectionError("durable engine message payload must be an object")
        engine_kind = str(payload.get("engine_kind") or "").strip()
        message = payload.get("message")
        if not engine_kind or not isinstance(message, dict):
            raise ChildRunProjectionError(
                "durable engine message requires engine_kind and message"
            )
        grouped[engine_kind].append((_event_seq(event), dict(message)))

    frames: list[dict[str, Any]] = []
    for engine_kind, source_rows in grouped.items():
        facts = get_engine_adapter(engine_kind).durable_child_resource_facts(
            [message for _event_seq_value, message in source_rows]
        )
        for message_index, fact in facts:
            if (
                isinstance(message_index, bool)
                or not isinstance(message_index, int)
                or message_index < 0
                or message_index >= len(source_rows)
            ):
                raise TypeError(
                    f"engine_kind={engine_kind!r} returned an invalid durable "
                    f"message index: {message_index!r}"
                )
            if not isinstance(fact, ChildResourceFact):
                raise TypeError(
                    f"engine_kind={engine_kind!r} returned a non-child durable fact: "
                    f"{type(fact).__name__}"
                )
            frame_payload = fact.as_frame()
            if pop_engine_frame_scope(frame_payload) != "session":
                raise TypeError(
                    f"engine_kind={engine_kind!r} returned a non-Session durable fact"
                )
            frames.append(
                {
                    "session_id": session_id,
                    "frame_seq": source_rows[message_index][0],
                    "turn_id": None,
                    "scope": "session",
                    "payload": frame_payload,
                    "engine_kind": engine_kind,
                    "source_kind": ENGINE_MESSAGE_EVENT_TYPE,
                }
            )
    return frames


def _empty_child_run(
    *,
    child_run_id: str,
    engine_kind: str,
    engine_ref: str,
    first_seen_order: int,
) -> dict[str, Any]:
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
        "_first_seen_order": first_seen_order,
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
            "child-run changed parent "
            f"child_run_id={child_run_id!r} parents={existing!r},{parent_child_run_id!r}"
        )
    entry["parent_child_run_id"] = parent_child_run_id


def _apply_lifecycle(entry: dict[str, Any], data: dict[str, Any]) -> None:
    _apply_parent(entry, str(data.get("parentChildRunId") or "").strip())
    tool_call_id = data.get("toolCallId")
    if isinstance(tool_call_id, str) and tool_call_id and tool_call_id not in entry["tool_call_ids"]:
        entry["tool_call_ids"].append(tool_call_id)
    control_ref = str(data.get("controlRef") or "").strip()
    if control_ref:
        existing_control_ref = str(entry.get("_control_ref") or "")
        if existing_control_ref and existing_control_ref != control_ref:
            raise ChildRunProjectionError(
                "child-run changed control reference "
                f"child_run_id={entry['child_run_id']!r} "
                f"controls={existing_control_ref!r},{control_ref!r}"
            )
        entry["_control_ref"] = control_ref
    for source_name, target_name in (
        ("description", "description"),
        ("taskType", "task_type"),
        ("lastToolName", "last_tool_name"),
        ("summary", "summary"),
    ):
        value = data.get(source_name)
        if isinstance(value, str) and value.strip():
            entry[target_name] = value.strip()
    usage = data.get("usage")
    if isinstance(usage, dict):
        entry["usage"] = {**dict(entry.get("usage") or {}), **dict(usage)}

    event = str(data["event"])
    if event == "opened":
        # A new activation has no terminal status until the engine supplies one.
        entry.pop("engine_status", None)
        entry.pop("engine_reason", None)
    if entry.get("closed") and event != "closed":
        # Whether a child run may live again is the ENGINE's answer, and this
        # projection carries what the engine said. Refusing it here made the
        # whole child-run projection 409 — the list a person reads, gone
        # because one background subagent reported activity after it went
        # quiet. The adapter that produces these frames stopped enforcing the
        # same rule for the same reason; this is the second copy of it.
        logger.info(
            "child-run %s reports %r after it closed; reopening the entry",
            entry.get("child_run_id"),
            event,
        )
        entry["closed"] = False
    entry["engine_event"] = str(data["engineEvent"])
    if "engineStatus" in data:
        entry["engine_status"] = str(data["engineStatus"])
    if "engineReason" in data:
        entry["engine_reason"] = str(data["engineReason"])
    entry["operations"] = list(data["operations"])
    if event == "closed":
        entry["closed"] = True


def _message_key(data: dict[str, Any], block_id: str) -> str:
    message_id = str(data.get("messageId") or "").strip()
    if message_id:
        return f"message:{message_id}"
    return f"block:{block_id}:{json.dumps(data, sort_keys=True, separators=(',', ':'))}"


def _apply_message(
    entry: dict[str, Any],
    data: dict[str, Any],
    *,
    block_id: str,
    public_message_id: str,
) -> None:
    _apply_parent(entry, str(data.get("parentChildRunId") or "").strip())
    role = str(data.get("role") or "").strip()
    content = data.get("content")
    if role not in {"assistant", "user"} or not isinstance(content, list):
        raise ChildRunProjectionError(
            f"child-run message is malformed child_run_id={entry['child_run_id']!r}"
        )
    key = _message_key(data, block_id)
    message_indexes = entry["_message_indexes"]
    existing_index = message_indexes.get(key)
    if existing_index is not None:
        existing = entry["messages"][existing_index]
        existing_role = str(existing.get("role") or "").strip()
        if existing_role != role:
            raise ChildRunProjectionError(
                "child-run message changed role "
                f"child_run_id={entry['child_run_id']!r} "
                f"message_key={key!r} roles={existing_role!r},{role!r}"
            )
        if str(existing.get("message_id") or "") != public_message_id:
            raise ChildRunProjectionError(
                "child-run message changed public identity "
                f"child_run_id={entry['child_run_id']!r} message_key={key!r}"
            )
        existing["content"] = merge_projected_message_blocks(existing.get("content"), content)
        return
    message: dict[str, Any] = {
        "role": role,
        "content": [dict(block) for block in content if isinstance(block, dict)],
    }
    message["message_id"] = public_message_id
    message_indexes[key] = len(entry["messages"])
    entry["messages"].append(message)


def _tree_order(entries: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    children: dict[str, list[dict[str, Any]]] = defaultdict(list)
    roots: list[dict[str, Any]] = []
    for entry in entries.values():
        parent_id = str(entry.get("parent_child_run_id") or "")
        if parent_id and parent_id in entries:
            children[parent_id].append(entry)
        else:
            roots.append(entry)

    def by_seen(item: dict[str, Any]) -> int:
        return int(item["_first_seen_order"])

    roots.sort(key=by_seen)
    for siblings in children.values():
        siblings.sort(key=by_seen)

    ordered: list[dict[str, Any]] = []
    visited: set[str] = set()

    def visit(entry: dict[str, Any], depth: int, ancestors: frozenset[str]) -> None:
        child_run_id = str(entry["child_run_id"])
        if child_run_id in ancestors:
            raise ChildRunProjectionError(
                f"child-run parent cycle includes child_run_id={child_run_id!r}"
            )
        if child_run_id in visited:
            return
        visited.add(child_run_id)
        entry["depth"] = depth
        ordered.append(entry)
        next_ancestors = ancestors | frozenset({child_run_id})
        for child in children.get(child_run_id, []):
            visit(child, depth + 1, next_ancestors)

    for root in roots:
        visit(root, 1, frozenset())
    for entry in sorted(entries.values(), key=by_seen):
        if str(entry["child_run_id"]) not in visited:
            visit(entry, 1, frozenset())
    return ordered


def project_session_child_runs(
    *,
    session_id: str,
    events: list[dict[str, Any]],
    frames: list[dict[str, Any]],
    include_messages: bool = False,
    include_control: bool = False,
    include_engine_ref: bool = False,
) -> list[dict[str, Any]]:
    """Project every child run without assigning it to a root platform turn."""

    clean_session_id = str(session_id or "").strip()
    if not clean_session_id:
        raise ChildRunProjectionError("child-run projection lacks session id")

    # A child can run again. Its latest complete transcript replaces earlier
    # snapshots; later live messages extend that baseline until the next one.
    message_snapshot_seq: dict[tuple[str, str], int] = {}
    for event in events:
        if str(event.get("event_type") or "").strip() != (
            "turn.background_tasks_materialized"
        ):
            continue
        event_seq = _event_seq(event)
        for block in _child_blocks_from_event(event):
            data = block.get("data")
            if (
                not isinstance(data, dict)
                or str(data.get("kind") or "").strip() != "message"
            ):
                continue
            key = (
                str(data.get("engineKind") or "").strip(),
                str(data.get("engineRef") or "").strip(),
            )
            if not all(key):
                continue
            message_snapshot_seq[key] = max(message_snapshot_seq.get(key, 0), event_seq)

    ordered_blocks: list[tuple[int, int, dict[str, Any], bool]] = []
    for frame in sorted(frames, key=_frame_seq):
        block = _child_block_from_frame(frame)
        if block is not None:
            ordered_blocks.append((_frame_seq(frame), 0, block, False))
    for event in sorted(events, key=_event_seq):
        ordered_blocks.extend(
            (
                _event_seq(event), block_index, block,
                event.get("event_type") == "turn.background_tasks_materialized",
            )
            for block_index, block in enumerate(_child_blocks_from_event(event), start=1)
        )
    entries: dict[str, dict[str, Any]] = {}
    seen_blocks: dict[str, str] = {}
    applied_blocks: set[str] = set()
    for order, (block_seq, _block_index, block, is_snapshot) in enumerate(
        sorted(ordered_blocks, key=lambda item: (item[0], item[1]))
    ):
        block_id = str(block.get("id") or "").strip()
        data_raw = block.get("data")
        if not block_id or not isinstance(data_raw, dict):
            raise ChildRunProjectionError("child-run block lacks stable id or data")
        engine_kind = str(data_raw.get("engineKind") or "").strip()
        data = canonical_child_run_data(data_raw, engine_kind=engine_kind)
        if data["kind"] == "message":
            data["content"] = get_engine_adapter(engine_kind).canonical_child_message_content(
                data["content"]
            )
        engine_ref = str(data["engineRef"])
        snapshot_seq = message_snapshot_seq.get((engine_kind, engine_ref))
        if (
            snapshot_seq is not None
            and block_seq < snapshot_seq
            and (is_snapshot or str(data["kind"]) == "message")
        ):
            continue
        block_signature = json.dumps(
            {**block, "data": data}, sort_keys=True, separators=(",", ":")
        )
        prior_signature = seen_blocks.get(block_id)
        if prior_signature is not None:
            if prior_signature != block_signature:
                raise ChildRunProjectionError(
                    f"child-run block id was reused block_id={block_id!r}"
                )
        else:
            seen_blocks[block_id] = block_signature
        if block_id in applied_blocks:
            continue
        applied_blocks.add(block_id)
        child_run_id = public_child_run_id(
            session_id=clean_session_id,
            engine_kind=engine_kind,
            engine_ref=engine_ref,
        )
        parent_engine_ref = str(data.get("parentEngineRef") or "").strip()
        if parent_engine_ref:
            data["parentChildRunId"] = public_child_run_id(
                session_id=clean_session_id,
                engine_kind=engine_kind,
                engine_ref=parent_engine_ref,
            )
        entry = entries.get(child_run_id)
        if entry is None:
            entry = _empty_child_run(
                child_run_id=child_run_id,
                engine_kind=engine_kind,
                engine_ref=engine_ref,
                first_seen_order=order,
            )
            entries[child_run_id] = entry
        elif str(entry["engine_kind"]) != engine_kind:
            raise ChildRunProjectionError(
                "child-run changed engine kind "
                f"child_run_id={child_run_id!r} engines={entry['engine_kind']!r},{engine_kind!r}"
            )
        elif str(entry["_engine_ref"]) != engine_ref:
            raise ChildRunProjectionError(
                "public child-run id collision "
                f"child_run_id={child_run_id!r} "
                f"engine_refs={entry['_engine_ref']!r},{engine_ref!r}"
            )

        if str(data["kind"]) == "lifecycle":
            _apply_lifecycle(entry, data)
        else:
            native_message_ref = str(data.get("messageId") or "").strip() or block_id
            _apply_message(
                entry,
                data,
                block_id=block_id,
                public_message_id=public_child_message_id(
                    session_id=clean_session_id,
                    engine_kind=engine_kind,
                    engine_ref=engine_ref,
                    message_ref=native_message_ref,
                ),
            )

    projected: list[dict[str, Any]] = []
    for entry in _tree_order(entries):
        if not str(entry.get("engine_event") or "").strip():
            raise ChildRunProjectionError(
                "child-run has messages but no lifecycle fact "
                f"child_run_id={entry['child_run_id']!r}"
            )
        clean = {
            key: value
            for key, value in entry.items()
            if not key.startswith("_") and (include_messages or key != "messages")
        }
        if include_control and entry.get("_control_ref"):
            clean["control_ref"] = str(entry["_control_ref"])
        if include_engine_ref:
            clean["_engine_ref"] = str(entry["_engine_ref"])
        projected.append(clean)
    return projected


class SessionChildRunView:
    """Read child-run summaries and transcripts from canonical session facts."""

    def __init__(
        self, session_events_repo: Any, *, transcript_entries_repo: Any = None
    ) -> None:
        self._session_events_repo = session_events_repo
        self._transcript_entries_repo = transcript_entries_repo

    async def _events(self, session_id: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        after_seq = 0
        while True:
            batch = await self._session_events_repo.list_events(
                session_id,
                after_seq=after_seq,
                event_types=_CHILD_RUN_SOURCE_EVENT_TYPES,
                limit=500,
            )
            if not batch:
                return rows
            rows.extend(dict(row) for row in batch)
            next_seq = max(_event_seq(row) for row in batch)
            if next_seq <= after_seq:
                raise RuntimeError("session child-run event scan did not advance")
            after_seq = next_seq
            if len(batch) < 500:
                return rows

    async def _frames(self, session_id: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        after_seq = -1
        while True:
            batch = await self._session_events_repo.list_frames(
                session_id,
                scope="session",
                after_seq=after_seq,
                limit=500,
            )
            if not batch:
                return rows
            rows.extend(dict(row) for row in batch)
            next_seq = max(_frame_seq(row) for row in batch)
            if next_seq <= after_seq:
                raise RuntimeError("session child-run frame scan did not advance")
            after_seq = next_seq
            if len(batch) < 500:
                return rows

    async def _projection_inputs(
        self,
        session_id: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        events = await self._events(session_id)
        frames = await self._frames(session_id)
        persisted_child_ids = {
            block["id"]
            for frame in frames
            if (block := _child_block_from_frame(frame)) is not None
        }
        # Persisted adapter frames retain live parent context that lifecycle-only
        # replay lacks. Replay supplies post-turn facts, not a second translation
        # of an already materialized event.
        for frame in _durable_child_frames(session_id=session_id, events=events):
            block = _child_block_from_frame(frame)
            if block is not None and block["id"] not in persisted_child_ids:
                frames.append(frame)
        return events, frames

    async def list_child_runs(self, session_id: str) -> list[dict[str, Any]]:
        events, frames = await self._projection_inputs(session_id)
        child_runs = project_session_child_runs(
            session_id=session_id,
            events=events,
            frames=frames,
        )
        for child_run in child_runs:
            child_run["active"] = get_engine_adapter(
                str(child_run["engine_kind"])
            ).child_run_is_active(child_run)
        return child_runs

    async def get_child_run_messages(
        self,
        session_id: str,
        child_run_id: str,
    ) -> list[dict[str, Any]] | None:
        clean_child_run_id = str(child_run_id or "").strip()
        events, frames = await self._projection_inputs(session_id)
        for child_run in project_session_child_runs(
            session_id=session_id,
            events=events,
            frames=frames,
            include_messages=True,
            include_engine_ref=True,
        ):
            if str(child_run["child_run_id"]) == clean_child_run_id:
                engine_kind = str(child_run["engine_kind"])
                adapter = get_engine_adapter(engine_kind)
                if isinstance(adapter, EngineStoredChildTranscript):
                    return await self._stored_child_messages(
                        session_id, child_run, adapter, events
                    )
                return [dict(message) for message in child_run.get("messages", [])]
        return None

    async def _stored_child_messages(
        self,
        session_id: str,
        child_run: dict[str, Any],
        adapter: EngineStoredChildTranscript,
        events: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if self._transcript_entries_repo is None:
            raise ChildRunProjectionError("stored child transcript repository is not configured")
        raw_scopes: list[dict[str, Any]] = []
        for scope in await self._transcript_entries_repo.list_scopes_by_platform_session(
            session_id
        ):
            entries = await self._transcript_entries_repo.load_subpath_entries_by_platform_session(
                session_id, subpath=scope.get("subpath")
            )
            raw_scopes.append({**scope, "entries": entries})
        engine_kind = str(child_run["engine_kind"])
        engine_ref = str(child_run["_engine_ref"])
        raw_messages = [
            event["payload"]["message"]
            for event in sorted(events, key=_event_seq)
            if event.get("event_type") == ENGINE_MESSAGE_EVENT_TYPE
            and isinstance(event.get("payload"), dict)
            and event["payload"].get("engine_kind") == engine_kind
            and isinstance(event["payload"].get("message"), dict)
        ]
        entry = _empty_child_run(
            child_run_id=str(child_run["child_run_id"]),
            engine_kind=engine_kind,
            engine_ref=engine_ref,
            first_seen_order=0,
        )
        for fact in adapter.stored_child_transcript_facts(
            engine_ref=engine_ref, closed=bool(child_run["closed"]),
            raw_scopes=raw_scopes, raw_messages=raw_messages
        ):
            if not isinstance(fact, ChildResourceFact):
                raise ChildRunProjectionError("stored child history returned a non-child fact")
            payload = fact.as_frame()
            scope = pop_engine_frame_scope(payload)
            block = _child_block_from_frame(
                {"scope": scope, "turn_id": None, "payload": payload}
            )
            if block is None:
                raise ChildRunProjectionError("stored child history returned a non-child frame")
            data = canonical_child_run_data(block["data"], engine_kind=engine_kind)
            if data["kind"] != "message" or data["engineRef"] != engine_ref:
                raise ChildRunProjectionError("stored child history changed the selected child")
            parent_ref = data.get("parentEngineRef")
            if parent_ref:
                data["parentChildRunId"] = public_child_run_id(
                    session_id=session_id, engine_kind=engine_kind, engine_ref=parent_ref
                )
                if data["parentChildRunId"] != child_run.get("parent_child_run_id"):
                    raise ChildRunProjectionError("stored child history changed the selected parent")
            _apply_message(
                entry,
                data,
                block_id=block["id"],
                public_message_id=public_child_message_id(
                    session_id=session_id,
                    engine_kind=engine_kind,
                    engine_ref=engine_ref,
                    message_ref=str(data.get("messageId") or block["id"]),
                ),
            )
        return [dict(message) for message in entry["messages"]]

    async def get_child_run_control(
        self,
        session_id: str,
        child_run_id: str,
    ) -> dict[str, Any] | None:
        """Return one child run with its adapter-authored control reference."""

        clean_child_run_id = str(child_run_id or "").strip()
        events, frames = await self._projection_inputs(session_id)
        for child_run in project_session_child_runs(
            session_id=session_id,
            events=events,
            frames=frames,
            include_control=True,
        ):
            if str(child_run["child_run_id"]) == clean_child_run_id:
                return dict(child_run)
        return None
