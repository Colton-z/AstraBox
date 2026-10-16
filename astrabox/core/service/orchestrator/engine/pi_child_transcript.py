"""Pi child history from the vendor session file retained in the SessionStore mirror."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import PurePosixPath
from typing import Any

from astrabox.core.service.orchestrator.engine.child_runs import canonical_child_run_data
from astrabox.core.service.orchestrator.engine.emissions import ChildResourceFact
from astrabox.core.service.orchestrator.engine.frame_scope import session_scoped_engine_frame
from astrabox.core.service.orchestrator.engine.pi_child_runs import PiChildRunError
from astrabox.core.service.orchestrator.tool_result_semantics import (
    TOOL_RESULT_STATE_AVAILABLE,
    TOOL_RESULT_STATE_ERROR,
    tool_result_block,
)


def _stored_message_content(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Project Pi Message blocks without replaying its already-committed RPC events."""
    content = message.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list) or any(not isinstance(block, dict) for block in content):
        raise PiChildRunError("Pi child native message content is malformed")
    if message["role"] == "toolResult":
        call_id = message.get("toolCallId")
        if not isinstance(call_id, str) or not call_id.strip():
            raise PiChildRunError("Pi child tool result lacks toolCallId")
        if not isinstance(message.get("isError"), bool):
            raise PiChildRunError("Pi child tool result lacks a boolean isError")
        return [tool_result_block(
            tool_call_id=call_id,
            content=json.dumps(message, ensure_ascii=False),
            tool_result_state=(
                TOOL_RESULT_STATE_ERROR if message["isError"] else TOOL_RESULT_STATE_AVAILABLE
            ),
        )]
    blocks = deepcopy(content)
    for block in blocks:
        if block.get("type") != "toolCall":
            continue
        if any(not isinstance(block.get(key), str) or not block[key].strip() for key in ("id", "name")):
            raise PiChildRunError("Pi child tool call lacks id or name")
        if not isinstance(block.get("arguments"), dict):
            raise PiChildRunError("Pi child tool call arguments are not an object")
        block["type"] = "tool_use"
        block["input"] = block.pop("arguments")
    return blocks


def stored_child_transcript_facts(
    *, engine_ref: str, closed: bool, native_state: dict[str, Any],
    raw_scopes: list[dict[str, Any]],
) -> list[ChildResourceFact]:
    """Read the exact sessionFile the supplier attached to this child's lifecycle."""
    session_file = native_state.get("sessionFile")
    if not session_file:
        return []
    root = PurePosixPath(str(native_state["sessionRoot"]))
    path = PurePosixPath(str(session_file))
    if not root.is_absolute() or not path.is_absolute() or ".." in path.parts:
        raise PiChildRunError("stored Pi child session path is not absolute")
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise PiChildRunError("Pi child sessionFile is outside its configured session root") from error
    subpath = f"pi/{relative}"
    scopes = [scope for scope in raw_scopes if scope.get("subpath") == subpath]
    if not scopes:
        return []
    if len(scopes) != 1:
        raise PiChildRunError("Pi child native session scope is ambiguous")
    entries = scopes[0].get("entries")
    if not isinstance(entries, list):
        raise PiChildRunError("Pi child native session entries are malformed")
    facts: list[ChildResourceFact] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or entry.get("type") != "message":
            continue
        message = entry.get("message")
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant", "toolResult"}:
            continue
        content = _stored_message_content(message)
        message_ref = f"{subpath}:{entry.get('id') or index}"
        data = canonical_child_run_data({
            "kind": "message", "engineRef": engine_ref, "messageId": message_ref,
            "role": "user" if message["role"] == "user" else "assistant", "content": content,
        }, engine_kind="pi")
        facts.append(ChildResourceFact(session_scoped_engine_frame({
            "type": "data-subagent", "id": f"pi-child:stored:{message_ref}", "data": data,
        })))
    if closed and not facts:
        raise PiChildRunError("closed Pi child has no mirrored native messages")
    return facts
