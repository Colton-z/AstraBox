"""Pi's native child-to-session linkage and database-backed conversation history."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import PurePosixPath
from typing import Any

from astrabox.core.service.orchestrator.engine.base import EngineEventSink
from astrabox.core.service.orchestrator.engine.child_runs import canonical_child_run_data
from astrabox.core.service.orchestrator.engine.emissions import ChildResourceFact
from astrabox.core.service.orchestrator.engine.frame_scope import session_scoped_engine_frame
from astrabox.core.service.orchestrator.engine.pi_child_runs import (
    PiChildRunError,
    async_status_snapshot,
    inspect_reply,
    split_child_reference,
    step_child_id,
    workflow_result_run_id,
)
from astrabox.core.service.orchestrator.engine.pi_events import raw_event_frame
from astrabox.core.service.orchestrator.tool_result_semantics import (
    TOOL_RESULT_STATE_AVAILABLE,
    TOOL_RESULT_STATE_ERROR,
    tool_result_block,
)

STORED_CHILD_SOURCE = "pi.child-transcript-source"


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


class PiChildTranscriptCapture:
    """Retain vendor file links while the native process can still read them."""

    def __init__(self, *, filesystem: Any, session_root: str, temp_root: str, sink: EngineEventSink,
                 owner_session_file: str | None = None) -> None:
        self._filesystem = filesystem
        self._session_root = session_root
        self._async_root = PurePosixPath(temp_root) / "async-subagent-runs"
        if not self._async_root.is_absolute() or ".." in self._async_root.parts:
            raise PiChildRunError("Pi subagent temp root must be absolute")
        self._sink = sink
        self._runs: dict[str, dict[str, str]] = {}
        self._stored: set[str] = set()
        self._activity: dict[str, str] = {}
        self._owner_session_file = owner_session_file

    def bind_owner(self, session_file: str) -> None:
        """Bind ownership to get_state's native sessionFile, not launch hints."""
        self._owner_session_file = session_file

    async def _persist(self, source: str, payload: dict[str, Any]) -> None:
        record = {
            "type": STORED_CHILD_SOURCE, "source": source,
            "sessionRoot": self._session_root, **payload,
        }
        digest = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
        if digest in self._stored:
            return
        await self._sink.persist_event(
            engine_kind="pi", causation_id=f"pi:child-transcript:{digest}",
            payload={"message": record},
        )
        self._stored.add(digest)

    async def observe(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        """Read on native launch/activity, with failures isolated from the relay."""
        try:
            await self._observe(record)
        except Exception as error:
            return [raw_event_frame("child-transcript-read-failed", {
                "error": str(error), "source": STORED_CHILD_SOURCE,
            })]
        return []

    async def _observe(self, record: dict[str, Any]) -> None:
        payload = record.get("partialResult") if record.get("type") == "tool_execution_update" else record.get("result")
        tool_name = record.get("toolName")
        message = record.get("message", record)
        if isinstance(message, dict) and message.get("role") == "toolResult":
            payload, tool_name = message, message.get("toolName")
        details = payload.get("details") if isinstance(payload, dict) else None
        selected: set[str] = set()
        if tool_name == "subagent" and isinstance(details, dict) and details.get("runId"):
            run_id = str(details["runId"])
            if details.get("asyncId") is None:
                await self._persist("tool-result", {"details": details})
            else:
                if details["asyncId"] != run_id:
                    raise PiChildRunError("Pi child launch has conflicting run identities")
                mission = details.get("mission")
                owner = self._owner_session_file
                async_dir = details.get("asyncDir")
                if not isinstance(owner, str) or not owner or not isinstance(async_dir, str) or not async_dir:
                    raise PiChildRunError("Pi child launch lacks native owner or asyncDir")
                if isinstance(mission, dict) and mission.get("ownerSessionId") not in {None, owner}:
                    raise PiChildRunError("Pi child mission belongs to a different native session")
                expected_path = str(PurePosixPath(async_dir) / "status.json")
                self._runs[run_id] = {"path": expected_path, "owner": owner}
                selected.add(run_id)
        snapshot = async_status_snapshot(record)
        if snapshot is not None:
            for run in snapshot.get("runs", []):
                if not isinstance(run, dict):
                    continue
                run_id = str(run.get("id") or "")
                if not run_id or PurePosixPath(run_id).name != run_id or run_id in {".", ".."}:
                    raise PiChildRunError("Pi snapshot run id is not a native path segment")
                if run_id not in self._runs:
                    if not self._owner_session_file:
                        raise PiChildRunError("Pi snapshot lacks its native session owner")
                    self._runs[run_id] = {
                        "path": str(self._async_root / run_id / "status.json"),
                        "owner": self._owner_session_file,
                    }
                activity = json.dumps(run, sort_keys=True)
                if run_id in self._runs and self._activity.get(run_id) != activity:
                    self._activity[run_id] = activity
                    selected.add(run_id)
        reply = inspect_reply(record)
        if reply is not None and reply.get("asyncId") in self._runs:
            selected.add(str(reply["asyncId"]))
        for run_id in selected:
            target = self._runs[run_id]
            status = json.loads(await self._filesystem.read_file(target["path"]))
            if not isinstance(status, dict) or status.get("runId") != run_id or status.get("sessionId") != target["owner"]:
                raise PiChildRunError("Pi child status does not belong to the launched run and owner")
            await self._persist("status", {
                "statusPath": target["path"], "ownerSessionFile": target["owner"], "status": status,
            })


def stored_child_transcript_facts(
    *, engine_ref: str, closed: bool, raw_scopes: list[dict[str, Any]],
    raw_messages: list[dict[str, Any]],
) -> list[ChildResourceFact]:
    """Resolve exact vendor sessionFile links, then project mirrored native messages."""
    run_id, child_id = split_child_reference(engine_ref)
    root_kind: str | None = None
    for record in raw_messages:
        details = record.get("details")
        if (record.get("type") == STORED_CHILD_SOURCE and record.get("source") == "tool-result"
                and isinstance(details, dict) and details.get("runId") == run_id
                and details.get("mode") == "workflow"):
            root_kind = "workflow"
        snapshot = async_status_snapshot(record)
        if snapshot is not None:
            for run in snapshot.get("runs", []):
                if isinstance(run, dict) and run.get("id") == run_id:
                    root_kind = run.get("kind")
    if child_id is None and root_kind == "workflow":
        return []
    source: dict[str, Any] | None = None
    direct_source = False
    steps: list[dict[str, Any]] = []
    for record in raw_messages:
        if record.get("type") != STORED_CHILD_SOURCE:
            continue
        if record.get("source") == "status":
            status = record.get("status")
            if not isinstance(status, dict):
                continue
            native_steps = status.get("steps", [])
            if status.get("runId") == run_id:
                selected_steps = native_steps
                direct_source = True
            elif not direct_source and child_id is None and status.get("mode") == "workflow":
                # Pi also publishes the worker as its own root. The workflow's
                # step.runId names that same native run, not a platform parent.
                selected_steps = [
                    step for step in native_steps
                    if isinstance(step, dict) and step.get("runId") == run_id
                ] if isinstance(native_steps, list) else []
                if not selected_steps:
                    continue
                if len(selected_steps) > 1:
                    raise PiChildRunError("stored Pi workflow links one run to multiple steps")
            else:
                continue
            if status.get("sessionId") != record.get("ownerSessionFile"):
                raise PiChildRunError("stored Pi child status changed owner")
            steps = selected_steps
            source = record
        elif record.get("source") == "tool-result":
            details = record.get("details")
            if not isinstance(details, dict):
                continue
            if details.get("runId") == run_id:
                steps = details.get("results", [])
                source = record
                direct_source = True
            elif not direct_source and details.get("mode") == "workflow":
                results = details.get("results", [])
                if not isinstance(results, list) or any(not isinstance(result, dict) for result in results):
                    raise PiChildRunError("stored Pi workflow results are malformed")
                selected_results = [result for result in results if workflow_result_run_id(details, result) == run_id]
                if selected_results:
                    steps = selected_results
                    source = record
    if source is None:
        if closed:
            raise PiChildRunError("closed Pi child has no stored native transcript linkage")
        return []
    if not isinstance(steps, list) or any(not isinstance(step, dict) for step in steps):
        raise PiChildRunError("stored Pi child steps are malformed")
    if child_id is None:
        if len(steps) > 1:
            return []
        matches = steps
    else:
        matches = []
        for index, step in enumerate(steps):
            if source["source"] == "tool-result":
                native_index = step.get("index")
                if not isinstance(native_index, int) or isinstance(native_index, bool):
                    raise PiChildRunError("Pi foreground child result lacks its native index")
                candidates = [step_child_id(native_index)]
            else:
                candidates = [value for value in (
                    step.get("childId"), step.get("workflowKey"), step.get("runId"), step_child_id(index)
                ) if isinstance(value, str)]
            if child_id in candidates:
                matches.append(step)
    if len(matches) > 1:
        raise PiChildRunError("stored Pi child identity names multiple sessions")
    session_file = matches[0].get("sessionFile") if matches else None
    if not session_file:
        if closed:
            raise PiChildRunError("closed Pi child has no native sessionFile")
        return []
    root = PurePosixPath(str(source["sessionRoot"]))
    path = PurePosixPath(str(session_file))
    if not root.is_absolute() or not path.is_absolute() or ".." in path.parts:
        raise PiChildRunError("stored Pi child session path is not absolute")
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise PiChildRunError("Pi child sessionFile is outside its configured session root") from error
    subpath = f"pi/{relative}"
    scopes = [scope for scope in raw_scopes if scope.get("subpath") == subpath]
    if len(scopes) != 1:
        if not scopes and not closed:
            return []
        raise PiChildRunError("Pi child native session scope is missing or ambiguous")
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
