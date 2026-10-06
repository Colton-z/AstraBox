"""Read Hermes' complete delegated SessionStores from its saved native SQLite.

Hermes SessionDatabase distinguishes delegation from user-created branches with
``model_config._delegate_from`` and ``parent_session_id``. These durable native
identities survive a gateway restart; ``subagent_id`` is a live control handle.
The reader preserves every native message column, including tool arguments and
results, rather than reconstructing messages from progress previews.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
from typing import Any

from astrabox.core.service.orchestrator.engine.child_runs import (
    ChildRunProjectionError,
    canonical_child_run_data,
)
from astrabox.core.service.orchestrator.engine.emissions import ChildResourceFact
from astrabox.core.service.orchestrator.engine.frame_scope import session_scoped_engine_frame


def native_child_sessions(payload: bytes, *, native_session_key: str) -> list[dict[str, Any]]:
    """Read only the selected native conversation's delegated descendants.

    The caller supplies the verified profile snapshot and the owning platform
    Session's native key. No sandbox, model call or workspace volume is needed.
    Session and message dictionaries contain the supplier's original columns.
    """
    if not payload.startswith(b"SQLite format 3\x00") or not native_session_key:
        raise ChildRunProjectionError("Hermes child history requires its native SQLite and Session")
    with tempfile.TemporaryDirectory(prefix="astrabox-hermes-history-") as directory:
        path = Path(directory) / "state.db"
        path.write_bytes(payload)
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            if (
                connection.execute(
                    "SELECT id FROM sessions WHERE id=?", (native_session_key,)
                ).fetchone()
                is None
            ):
                # Hermes creates an empty gateway conversation before its
                # first native DB write. A profile's older snapshot can exist
                # before this new conversation has any saved child history.
                return []
            visited = {native_session_key}
            frontier = [native_session_key]
            children: list[dict[str, Any]] = []
            while frontier:
                parent = frontier.pop(0)
                rows = connection.execute(
                    "SELECT * FROM sessions "
                    "WHERE json_extract(COALESCE(model_config,'{}'),'$._delegate_from')=? "
                    "OR (parent_session_id=? "
                    "AND json_extract(COALESCE(model_config,'{}'),'$._delegate_from') IS NOT NULL) "
                    "ORDER BY started_at,id",
                    (parent, parent),
                ).fetchall()
                for row in rows:
                    session = dict(row)
                    config = json.loads(session.get("model_config") or "{}")
                    if not isinstance(config, dict):
                        raise ChildRunProjectionError(
                            "Hermes native model_config must be an object"
                        )
                    if config.get("_delegate_from") is None:
                        continue
                    child_id = session["id"]
                    if child_id in visited:
                        # SessionDatabase's delegation walk deduplicates both
                        # native lineage links and never returns its seed.
                        continue
                    visited.add(child_id)
                    frontier.append(child_id)
                    messages = [
                        dict(message)
                        for message in connection.execute(
                            "SELECT * FROM messages WHERE session_id=? ORDER BY id", (child_id,)
                        )
                    ]
                    children.append({"session": session, "messages": messages})
            return children


def _fact(data: dict[str, Any], identity: str) -> ChildResourceFact:
    return ChildResourceFact(
        session_scoped_engine_frame(
            {
                "type": "data-subagent",
                "id": identity,
                "data": canonical_child_run_data(data, engine_kind="assistant"),
            }
        )
    )


def _native_message_content(row: dict[str, Any]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    for field in ("reasoning", "reasoning_content"):
        text = row.get(field)
        block = {"type": "thinking", "thinking": text}
        if isinstance(text, str) and text and block not in content:
            content.append(block)
    if row["role"] == "tool":
        result = row.get("content") or ""
        try:
            decoded = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            decoded = None
        is_error = isinstance(decoded, dict) and bool(decoded.get("error"))
        content.append(
            {
                "type": "tool_result",
                "tool_use_id": row["tool_call_id"],
                "content": result,
                "is_error": is_error,
            }
        )
        return content
    text = row.get("content")
    if text:
        content.append({"type": "text", "text": text})
    for call in json.loads(row.get("tool_calls") or "[]"):
        function = call["function"]
        arguments = function["arguments"]
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                # Malformed function arguments are themselves native data;
                # preserve their exact text alongside the native error result.
                pass
        content.append(
            {
                "type": "tool_use",
                "id": call["id"],
                "name": function["name"],
                "input": arguments,
            }
        )
    return content


def _native_child_usage(session: dict[str, Any]) -> dict[str, Any]:
    """Carry SessionDB counters and the supplier's full prompt-token total.

    Hermes CanonicalUsage stores uncached input separately from cache buckets;
    its delegation progress reports session_prompt_tokens including both.
    Retain those native counters alongside the common display measurements.
    """
    prompt_tokens = sum(
        session[key] for key in ("input_tokens", "cache_read_tokens", "cache_write_tokens")
    )
    usage: dict[str, Any] = {
        "input_tokens": prompt_tokens,
        "output_tokens": session["output_tokens"],
        "reasoning_tokens": session["reasoning_tokens"],
        "total_tokens": prompt_tokens + session["output_tokens"],
        "tool_uses": session["tool_call_count"],
        "api_calls": session["api_call_count"],
        "native_usage": {
            key: session[key]
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "reasoning_tokens",
                "tool_call_count",
                "api_call_count",
                "started_at",
                "ended_at",
                "estimated_cost_usd",
                "actual_cost_usd",
                "cost_status",
                "cost_source",
                "pricing_version",
            )
            if key in session
        },
    }
    if session["ended_at"] is not None:
        usage["duration_ms"] = round((session["ended_at"] - session["started_at"]) * 1000)
    return usage


def stored_child_resource_facts(
    *,
    native_session_key: str | None,
    runtime_state: bytes | None,
    live_frames: list[dict[str, Any]],
) -> list[ChildResourceFact]:
    """Combine native saved conversations with current SDK control bindings.

    Only explicit SDK ``child_session_id`` links associate a live subagent with
    a durable conversation. Old preview-only frames are retained in the journal
    but cannot replace the complete native catalog or manufacture tool calls.
    """
    live: list[dict[str, Any]] = []
    aliases: dict[str, str] = {}
    for frame in sorted(live_frames, key=lambda item: item.get("frame_seq", 0)):
        payload = frame.get("payload") or {}
        data = payload.get("data") or {}
        state = data.get("nativeState") or {}
        if payload.get("type") != "data-subagent" or data.get("kind") != "lifecycle":
            continue
        if not state.get("subagent_id"):
            continue
        live.append(dict(data))
        if state.get("child_session_id"):
            aliases[state["subagent_id"]] = state["child_session_id"]
    latest: dict[str, dict[str, Any]] = {}
    for data in live:
        state = data["nativeState"]
        identity = aliases.get(state["subagent_id"], data["engineRef"])
        data["engineRef"] = identity
        parent = data.get("parentEngineRef")
        if parent:
            data["parentEngineRef"] = aliases.get(parent, parent)
        latest[identity] = data
    children = (
        native_child_sessions(
            runtime_state,
            native_session_key=native_session_key,
        )
        if runtime_state is not None and native_session_key
        else []
    )
    stored_ids = {child["session"]["id"] for child in children}
    facts: list[ChildResourceFact] = []
    for child in children:
        session, messages = child["session"], child["messages"]
        identity = session["id"]
        data = dict(latest.pop(identity, {}))
        ended = session["ended_at"] is not None
        data.update(
            kind="lifecycle",
            engineRef=identity,
            event="closed" if ended or data.get("event") == "closed" else "opened",
            nativeState={"ended_at": session["ended_at"], "end_reason": session["end_reason"]},
        )
        parent = (
            session["parent_session_id"] or json.loads(session["model_config"])["_delegate_from"]
        )
        if parent in stored_ids:
            data["parentEngineRef"] = parent
        else:
            data.pop("parentEngineRef", None)
        data["operations"] = [] if data["event"] == "closed" else data.get("operations", [])
        if session["end_reason"]:
            data["engineReason"] = session["end_reason"]
        if session["model"]:
            data.setdefault("model", session["model"])
        native_usage = _native_child_usage(session)
        data["usage"] = {
            **native_usage,
            **dict(data.get("usage") or {}),
            "native_usage": native_usage["native_usage"],
        }
        first_user = next((row for row in messages if row["role"] == "user"), None)
        if "description" not in data and first_user is not None and first_user.get("content"):
            data["description"] = first_user["content"]
        if ended and messages and messages[-1]["role"] == "assistant":
            if messages[-1].get("content"):
                data.setdefault("summary", messages[-1]["content"])
        calls = [call for row in messages for call in json.loads(row.get("tool_calls") or "[]")]
        if calls:
            data.setdefault("lastToolName", calls[-1]["function"]["name"])
        facts.append(_fact(data, "hermes-native-child:" + identity))
        for row in messages:
            content = _native_message_content(row)
            if not content:
                continue
            message = {
                "kind": "message",
                "engineRef": identity,
                "messageId": str(row["id"]),
                "role": "user" if row["role"] == "user" else "assistant",
                "content": content,
            }
            if parent in stored_ids:
                message["parentEngineRef"] = parent
            facts.append(_fact(message, "hermes-native-message:" + identity + ":" + str(row["id"])))
    for identity, data in latest.items():
        facts.append(_fact(data, "hermes-live-child:" + identity))
    return facts
