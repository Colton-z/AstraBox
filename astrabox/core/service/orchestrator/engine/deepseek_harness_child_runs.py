"""DeepSeek Harness child Sessions projected across the engine seam.

The Web Host API owns the durable child catalog and transcript.  This adapter
keeps its three native identities private: a child Session is the resource,
its direct parent is the authority, and a continuable address is the control
handle.  Core receives only canonical child-resource facts and never parses a
DSH Session id, mode, activity, event, or content block.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.child_runs import (
    canonical_child_run_data,
)
from astrabox.core.service.orchestrator.engine.deepseek_harness_events import (
    DeepSeekHarnessProtocolError,
    raw_event_frame,
    tool_result_message,
)
from astrabox.core.service.orchestrator.engine.deepseek_harness_history import read_history_events
from astrabox.core.service.orchestrator.engine.frame_scope import (
    session_scoped_engine_frame,
)

logger = get_logger(__name__)

DSH_ENGINE_KIND = "deepseek_harness"

_CATALOG_METHOD = "session/projections"
_SUMMARY_METHOD = "session/list"
_INTERRUPT_METHOD = "subagents/interruptByParent"

_CHILD_MODES = frozenset({"one-shot", "continuable", "unknown"})
_VISIBLE_USER_SOURCES = frozenset({"user", "user-rpc"})
_IGNORED_CHILD_EVENT_TYPES = frozenset(
    {
        "agent/inbox/spliced",
        "assistant/attempt",
        "approval/asked",
        "approval/decided",
        "command/done",
        "command/run",
        "request/context",
        "request/header",
        "session/title",
        "session/title-llm-request",
        "step/end",
        "step/start",
        "subagent/descriptor",
        "subagent/catalog",
        "turn/start",
    }
)

DshCall = Callable[[str, dict[str, Any]], Awaitable[Any]]


@dataclass(slots=True)
class _ChildState:
    parent_session_id: str
    parent_engine_ref: str | None
    child_session_id: str
    mode: str
    activity: str
    label: str | None
    closed: bool = False


def _required_string(value: Any, *, field: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise DeepSeekHarnessProtocolError(f"deepseek_harness child payload lacks {field}")
    return clean


def _control_ref(state: _ChildState) -> str:
    return json.dumps(
        {
            "childSessionId": state.child_session_id,
            "mode": "continuable",
            "parentSessionId": state.parent_session_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def decode_child_control_ref(control_ref: str) -> dict[str, str]:
    """Read one of this adapter's own private child addresses."""

    try:
        raw = json.loads(str(control_ref or ""))
    except (TypeError, ValueError) as exc:
        raise APIError(
            code="CHILD_RUN_CONTROL_UNAVAILABLE",
            message="deepseek_harness child-run control reference is malformed",
            status_code=409,
        ) from exc
    expected = {"parentSessionId", "childSessionId", "mode"}
    if not isinstance(raw, dict) or set(raw) != expected:
        raise APIError(
            code="CHILD_RUN_CONTROL_UNAVAILABLE",
            message="deepseek_harness child-run control reference is malformed",
            status_code=409,
        )
    address = {key: str(raw.get(key) or "").strip() for key in expected}
    if (
        not address["parentSessionId"]
        or not address["childSessionId"]
        or address["mode"] != "continuable"
        or address["parentSessionId"] == address["childSessionId"]
    ):
        raise APIError(
            code="CHILD_RUN_CONTROL_UNAVAILABLE",
            message="deepseek_harness child-run control reference is malformed",
            status_code=409,
        )
    return address


def _lifecycle_frame(
    state: _ChildState,
    *,
    source_id: str,
    event: str,
    engine_event: str,
    engine_status: str | None = None,
    engine_reason: str | None = None,
) -> dict[str, Any]:
    closed = event == "closed"
    can_stop = state.mode == "continuable" and state.activity == "running" and not closed
    data: dict[str, Any] = {
        "kind": "lifecycle",
        "engineRef": state.child_session_id,
        "event": event,
        "engineEvent": engine_event,
        "operations": ["stop"] if can_stop else [],
        "taskType": state.mode,
    }
    if state.parent_engine_ref:
        data["parentEngineRef"] = state.parent_engine_ref
    if state.label:
        data["description"] = state.label
    if engine_status:
        data["engineStatus"] = engine_status
    if engine_reason:
        data["engineReason"] = engine_reason
    if can_stop:
        data["controlRef"] = _control_ref(state)
    canonical = canonical_child_run_data(data, engine_kind=DSH_ENGINE_KIND)
    return session_scoped_engine_frame(
        {
            "type": "data-subagent",
            "id": f"dsh-child:lifecycle:{state.child_session_id}:{source_id}",
            "data": canonical,
        }
    )


def _surface_appends(event: dict[str, Any]) -> bool:
    surface_op = event.get("surfaceOp")
    if surface_op is None or surface_op == "append":
        return True
    return isinstance(surface_op, dict) and surface_op.get("op") == "append"


def _dsh_content_blocks(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise DeepSeekHarnessProtocolError("deepseek_harness child message content is not a list")
    blocks: list[dict[str, Any]] = []
    for raw_block in raw:
        if not isinstance(raw_block, dict):
            raise DeepSeekHarnessProtocolError(
                "deepseek_harness child message contains a non-object block"
            )
        block = deepcopy(raw_block)
        block_type = str(block.get("type") or "").strip()
        if block_type == "reasoning":
            text = str(block.pop("text", "") or "")
            block["type"] = "thinking"
            block["thinking"] = text
        elif block_type == "tool-call":
            arguments = block.pop("arguments", None)
            if isinstance(arguments, str):
                try:
                    tool_input: Any = json.loads(arguments)
                except ValueError:
                    tool_input = arguments
            else:
                tool_input = arguments
            block["type"] = "tool_use"
            block["input"] = tool_input
        blocks.append(block)
    return blocks


def _message_frame(
    state: _ChildState,
    *,
    event: dict[str, Any],
) -> dict[str, Any] | None:
    if not _surface_appends(event):
        return None
    event_type = str(event.get("type") or "").strip()
    seq = event.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise DeepSeekHarnessProtocolError(
            f"deepseek_harness child {event_type!r} lacks a non-negative seq"
        )
    data = event.get("data")
    if not isinstance(data, dict):
        raise DeepSeekHarnessProtocolError(
            f"deepseek_harness child {event_type!r} lacks a data object"
        )

    role = ""
    content_raw: Any = None
    tool_message: dict[str, Any] | None = None
    if event_type == "assistant/message":
        message = data.get("message")
        if not isinstance(message, dict):
            raise DeepSeekHarnessProtocolError(
                "deepseek_harness child assistant/message lacks message"
            )
        role = "assistant"
        content_raw = message.get("content")
    elif event_type == "user/message":
        source = data.get("source")
        source_kind = str(source.get("kind") or "").strip() if isinstance(source, dict) else ""
        if source_kind not in _VISIBLE_USER_SOURCES:
            return None
        role = "user"
        content_raw = data.get("content")
    elif event_type == "tool/result":
        tool_message = tool_result_message(data)
        role = "user"
    else:
        return None

    content = [{
        "type": "tool_result",
        "tool_use_id": tool_message["toolCallId"],
        "content": tool_message["content"],
        "is_error": tool_message.get("isError", False),
    }] if tool_message is not None else _dsh_content_blocks(content_raw)
    if not content:
        return None
    message_ref = f"{event_type}:{seq}"
    message: dict[str, Any] = {
        "kind": "message",
        "engineRef": state.child_session_id,
        "role": role,
        "content": content,
        "messageId": message_ref,
    }
    if state.parent_engine_ref:
        message["parentEngineRef"] = state.parent_engine_ref
    canonical = canonical_child_run_data(message, engine_kind=DSH_ENGINE_KIND)
    return session_scoped_engine_frame(
        {
            "type": "data-subagent",
            "id": f"dsh-child:message:{state.child_session_id}:{seq}",
            "data": canonical,
        }
    )


#: Runtime status follows the durable parent-owned child catalog.
_CHILD_SESSION_EMITS = frozenset(
    {"api-session/added", "api-session/status", "api-session/removed"}
)


class DeepSeekHarnessChildResources:
    """Fold the official DSH catalog, history, and mux into child facts.

    ``history_positions`` seeds, per child Session, the last transcript event
    already reported, so a projector rebuilt for a later connection reports
    only newer child messages; :meth:`history_positions` reads them back.
    """

    def __init__(
        self, *, root_session_id: str, call: DshCall,
        history_positions: dict[str, int] | None = None,
    ) -> None:
        self._root_session_id = _required_string(root_session_id, field="root session id")
        self._call = call
        self._children: dict[str, _ChildState] = {}
        self._history_max_seq: dict[str, int] = dict(history_positions or {})
        self._lock = asyncio.Lock()
        self._running: dict[str, bool] = {}
        self._source_id = ""

    def _observe(self) -> None:
        """Name the observation the following lifecycle facts come from."""
        self._source_id = uuid.uuid4().hex

    def history_positions(self) -> dict[str, int]:
        """Per child Session, the last transcript event this projector reported."""
        return dict(self._history_max_seq)

    def any_running(self) -> bool:
        """Whether a child the harness reports as running is still open."""
        return any(state.activity == "running" and not state.closed for state in self._children.values())

    def contains(self, session_id: str) -> bool:
        return str(session_id or "").strip() in self._children

    def may_own(self, frame: dict[str, Any]) -> bool:
        """Whether ``observe_mux_frame`` could fold this frame into a child.

        Parent catalog facts establish children; their own followed events and
        gateway status then update them. No child stream exists before discovery.
        """

        frame_type = str(frame.get("type") or "").strip()
        if frame_type == "emit":
            return str(frame.get("event") or "") in _CHILD_SESSION_EMITS
        payload = frame.get("payload")
        if not isinstance(payload, dict):
            return False
        session_id = str(payload.get("sessionId") or "").strip()
        event = payload.get("event")
        if (
            frame_type == "session/event"
            and session_id == self._root_session_id
            and isinstance(event, dict)
            and event.get("type") == "subagent/catalog"
        ):
            return True
        return session_id in self._children

    async def refresh(self) -> list[dict[str, Any]]:
        """Read the durable catalog recursively and catch up every transcript."""

        async with self._lock:
            return await self._refresh_unlocked()

    async def _refresh_unlocked(self) -> list[dict[str, Any]]:
        """Refresh while holding the projector's state lock."""

        # The supplier's Web client joins durable parent catalog projections
        # with Session summaries for live running state. Neither read activates
        # an Agent, and catalog entries remain visible after its disposal.
        summary = await self._call(_SUMMARY_METHOD, {"args": {"_request": {}}})
        self._read_summary(summary)
        frames: list[dict[str, Any]] = []
        pending_parents = [self._root_session_id]
        visited_parents: set[str] = set()
        while pending_parents:
            parent_session_id = pending_parents.pop(0)
            if parent_session_id in visited_parents:
                continue
            visited_parents.add(parent_session_id)
            value = await self._call(
                _CATALOG_METHOD,
                {"args": {"request": {"sessionId": parent_session_id}}},
            )
            entries = self._catalog_entries(value)
            self._observe()
            catalog_source = self._source_id
            for raw_entry in entries:
                # Every child's lifecycle fact from this read names the read.
                self._source_id = catalog_source
                state, lifecycle = self._observe_catalog_child(parent_session_id, raw_entry)
                if lifecycle is not None:
                    frames.append(lifecycle)
                frames.extend(await self._refresh_history(state))
                pending_parents.append(state.child_session_id)
        return frames

    def _read_summary(self, value: Any) -> None:
        items = value.get("items") if isinstance(value, dict) else None
        if not isinstance(items, list):
            raise DeepSeekHarnessProtocolError("deepseek_harness session/list returned no items")
        running: dict[str, bool] = {}
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("running"), bool):
                raise DeepSeekHarnessProtocolError("deepseek_harness Session summary lacks running state")
            session_id = _required_string(item.get("sessionId"), field="Session summary id")
            running[session_id] = item["running"]
        self._running = running

    @staticmethod
    def _catalog_entries(value: Any) -> list[dict[str, Any]]:
        values = value.get("values") if isinstance(value, dict) else None
        entries = values.get("subagentCatalog") if isinstance(values, dict) else None
        if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
            raise DeepSeekHarnessProtocolError("deepseek_harness Session has no subagentCatalog projection")
        return entries

    def _observe_catalog_child(
        self,
        parent_session_id: str,
        raw: dict[str, Any],
    ) -> tuple[_ChildState, dict[str, Any] | None]:
        child_session_id = _required_string(raw.get("id"), field="child id")
        if child_session_id == self._root_session_id:
            raise DeepSeekHarnessProtocolError(
                "deepseek_harness child catalog contains the root session"
            )
        mode = _required_string(raw.get("mode"), field="child mode")
        if mode not in _CHILD_MODES:
            raise DeepSeekHarnessProtocolError(
                f"deepseek_harness child has unsupported mode={mode!r}"
            )
        activity = "running" if self._running.get(child_session_id, False) else "inactive"
        label = str(raw.get("label") or "").strip() or None
        if mode == "continuable" and label is None:
            raise DeepSeekHarnessProtocolError("deepseek_harness continuable child has no label")

        existing = self._children.get(child_session_id)
        if existing is None:
            state = _ChildState(
                parent_session_id=parent_session_id,
                parent_engine_ref=(
                    None if parent_session_id == self._root_session_id else parent_session_id
                ),
                child_session_id=child_session_id,
                mode=mode,
                activity=activity,
                label=label,
            )
            event = "closed" if mode == "one-shot" and activity == "inactive" else "opened"
            state.closed = event == "closed"
            self._children[child_session_id] = state
            return state, _lifecycle_frame(
                state,
                source_id=self._source_id,
                event=event,
                engine_event=_CATALOG_METHOD,
                engine_status=activity,
            )

        if existing.parent_session_id != parent_session_id or (
            existing.mode != mode and existing.mode != "unknown"
        ):
            raise DeepSeekHarnessProtocolError(
                "deepseek_harness child changed its durable parent or mode"
            )
        existing.mode = mode
        existing.label = label
        if existing.activity == activity:
            return existing, None
        if existing.closed:
            # `session/list` is a SNAPSHOT, not an ordered stream: a refresh
            # issued before the engine finished closing a one-shot child comes
            # back describing it as running, after the close was already
            # observed. Treating that as a contradiction and failing the
            # protocol killed the whole turn — the user's answer lost to a
            # stale catalog page (p141, p178). The close stands; a snapshot
            # cannot reopen what an event closed.
            logger.info(
                "deepseek_harness: ignoring a stale catalog page that shows "
                "closed child %s as %s",
                child_session_id,
                activity,
            )
            return existing, None
        existing.activity = activity
        event = "closed" if mode == "one-shot" and activity == "inactive" else "updated"
        existing.closed = event == "closed"
        return existing, _lifecycle_frame(
            existing,
            source_id=self._source_id,
            event=event,
            engine_event=_CATALOG_METHOD,
            engine_status=activity,
        )

    async def _refresh_history(self, state: _ChildState) -> list[dict[str, Any]]:
        previous_max = self._history_max_seq.get(state.child_session_id, -1)
        address = {
            "kind": "subagent", "parentSessionId": state.parent_session_id,
            "childSessionId": state.child_session_id, "mode": state.mode,
        }
        snapshot = await self._call("session/follow", {"args": {"request": {"address": address}}})
        events = await read_history_events(
            self._call, address=address, through_seq=snapshot["cursor"],
            after_seq=previous_max,
        )

        frames: list[dict[str, Any]] = []
        for event in events:
            self._observe()
            frames.extend(self._event_frames(state, event))
        if events:
            self._history_max_seq[state.child_session_id] = max(previous_max, events[-1]["seq"])
        return frames

    def _event_frames(
        self,
        state: _ChildState,
        event: dict[str, Any],
    ) -> list[dict[str, Any]]:
        event_type = str(event.get("type") or "").strip()
        message = _message_frame(state, event=event)
        if message is not None:
            return [message]
        if event_type == "turn/end":
            data = event.get("data")
            reason = data.get("reason") if isinstance(data, dict) else None
            engine_reason = (
                str(reason.get("kind") or "").strip() if isinstance(reason, dict) else ""
            )
            if not engine_reason:
                raise DeepSeekHarnessProtocolError(
                    "deepseek_harness child turn/end lacks reason.kind"
                )
            lifecycle_event = "updated"
            if state.mode == "one-shot":
                lifecycle_event = "closed"
                state.activity = "inactive"
                state.closed = True
            return [
                _lifecycle_frame(
                    state,
                    source_id=self._source_id,
                    event=lifecycle_event,
                    engine_event=event_type,
                    engine_reason=engine_reason,
                )
            ]
        if event_type in _IGNORED_CHILD_EVENT_TYPES or event_type in {
            "user/message",
            "assistant/message",
            "tool/result",
        }:
            return []
        return [raw_event_frame(event_type, event)]

    async def observe_mux_frame(
        self,
        frame: dict[str, Any],
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Consume a child-owned mux frame; leave root and foreign frames alone."""

        async with self._lock:
            return await self._observe_mux_frame_unlocked(frame)

    async def _observe_mux_frame_unlocked(
        self,
        frame: dict[str, Any],
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Fold one mux frame while holding the projector's state lock."""

        frame_type = str(frame.get("type") or "").strip()
        payload = frame.get("payload")
        if frame_type == "emit":
            native_event = str(frame.get("event") or "")
            args = frame.get("args") or []
            if native_event == "api-session/status":
                frame_type, payload = native_event, {"sessionId": args[0], "running": args[1]}
            elif native_event == "api-session/added":
                frame_type, payload = native_event, args[0]
            elif native_event == "api-session/removed":
                frame_type, payload = native_event, {"sessionId": args[0]}
        if not isinstance(payload, dict):
            return False, []
        session_id = str(payload.get("sessionId") or "").strip()

        event = payload.get("event")
        if (
            frame_type == "session/event"
            and isinstance(event, dict)
            and event.get("type") == "subagent/catalog"
            and (session_id == self._root_session_id or session_id in self._children)
        ):
            # The supplier publishes this on the already-followed parent after
            # child creation succeeds. Its catalog owns mode and lineage, so no
            # subscription to an as-yet-unclassified child's descriptor is needed.
            return True, await self._refresh_unlocked()

        self._observe()
        return self._fold_child_mux_frame(frame)

    def _fold_child_mux_frame(
        self, frame: dict[str, Any]
    ) -> tuple[bool, list[dict[str, Any]]]:
        frame_type = str(frame.get("type") or "").strip()
        payload = frame.get("payload")
        if frame_type == "emit":
            args = frame.get("args") or []
            native_event = frame.get("event")
            if native_event == "api-session/status":
                frame_type, payload = native_event, {"sessionId": args[0], "running": args[1]}
            elif native_event == "api-session/added":
                frame_type, payload = native_event, args[0]
            elif native_event == "api-session/removed":
                frame_type, payload = native_event, {"sessionId": args[0]}
        if not isinstance(payload, dict):
            return False, []
        session_id = str(payload.get("sessionId") or "").strip()
        state = self._children.get(session_id)
        if state is None:
            return False, []

        if frame_type in {"session/assistant-stream", "session/assistant-stream-snapshot"}:
            # Child transcripts are the committed native messages, not transient
            # token attempts. The followed durable settlement supplies their content.
            return True, []

        if frame_type in {"api-session/status", "api-session/added"}:
            running = payload.get("running")
            if not isinstance(running, bool):
                raise DeepSeekHarnessProtocolError(
                    "deepseek_harness api-session/status lacks running"
                )
            activity = "running" if running else "inactive"
            if state.activity == activity:
                return True, []
            if state.closed:
                # A native activity event may reopen a closed child. Follow
                # the engine's lifecycle so later transcript events are accepted.
                logger.info(
                    "deepseek_harness: child %s reports %s after it closed; "
                    "reopening it",
                    session_id,
                    activity,
                )
                state.closed = False
            state.activity = activity
            lifecycle_event = (
                "closed" if state.mode == "one-shot" and activity == "inactive" else "updated"
            )
            state.closed = lifecycle_event == "closed"
            return True, [
                _lifecycle_frame(
                    state,
                    source_id=self._source_id,
                    event=lifecycle_event,
                    engine_event=frame_type,
                    engine_status=activity,
                )
            ]

        if frame_type == "api-session/removed":
            if state.closed:
                return True, []
            # Removal is from the live Host registry, not the durable catalog.
            # A continuable child remains resumable after its activation ends.
            state.closed = state.mode == "one-shot"
            state.activity = "inactive"
            return True, [
                _lifecycle_frame(
                    state,
                    source_id=self._source_id,
                    event="closed" if state.closed else "updated",
                    engine_event=frame_type,
                    engine_status=state.activity,
                )
            ]

        if frame_type != "session/event":
            return True, [raw_event_frame(f"child.{frame_type}", frame)]
        event = payload.get("event")
        if not isinstance(event, dict):
            raise DeepSeekHarnessProtocolError("deepseek_harness child session/event lacks event")
        seq = event.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise DeepSeekHarnessProtocolError(
                "deepseek_harness child session/event lacks a non-negative seq"
            )
        previous_max = self._history_max_seq.get(session_id, -1)
        if seq <= previous_max:
            return True, []
        self._history_max_seq[session_id] = seq
        return True, self._event_frames(state, dict(event))


async def interrupt_dsh_child(
    call: DshCall,
    control_ref: str,
) -> None:
    """Interrupt a continuable child through the exact vendor address."""

    address = decode_child_control_ref(control_ref)
    value = await call(_INTERRUPT_METHOD, {"args": address})
    if not isinstance(value, dict) or value.get("accepted") is not True:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message="deepseek_harness did not accept the child interrupt",
            status_code=502,
        )


__all__ = [
    "DSH_ENGINE_KIND",
    "DeepSeekHarnessChildResources",
    "decode_child_control_ref",
    "interrupt_dsh_child",
]
