"""Codex collaboration vocabulary at the engine-neutral child-run seam.

A Codex sub-agent is an ordinary thread. The parent's ``collabAgentToolCall``
announces its ``receiverThreadIds``. The app-server also attaches initialized
connections to new child threads. Item notifications carry live tools that
need not yet appear in ``thread/read``; both sources feed the same projection.

That read is also where the child's identity comes from. ``Thread.source``
carries ``{"subAgent": {"thread_spawn": {...}}}`` with the spawning
``parent_thread_id``, and ``Thread.agentNickname`` / ``agentRole`` are the
vendor's own words for the child — the platform invents no identity here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from astrabox.core.service.orchestrator.engine.child_runs import (
    ChildRunProjectionError,
    canonical_child_run_data,
)
from astrabox.core.service.orchestrator.engine.frame_scope import (
    session_scoped_engine_frame,
)
from astrabox.core.service.orchestrator.engine.codex_events import CODEX_TOOL_ITEM_TYPES
from astrabox.core.service.orchestrator.tool_result_semantics import tool_result_block

CODEX_ENGINE_KIND = "codex"

#: The parent-stream item that announces a spawn, and the tool name it carries.
#: Both are the generated protocol's own spelling (``ThreadItem`` and
#: ``CollabAgentTool``); the rollout log spells the same item differently, so a
#: reader built for one face must not be pointed at the other.
COLLAB_ITEM_TYPE = "collabAgentToolCall"
SPAWN_TOOL = "spawnAgent"

#: Metadata identifies a newly spawned child before its history is readable.
#: Full turns are reconciled after the child's native completion notification.
THREAD_READ_METHOD = "thread/read"

#: ``TurnStatus``. A turn that is still ``inProgress`` is the only state in
#: which the child has work left to interrupt.
_LIVE_TURN_STATUS = "inProgress"
_TERMINAL_TURN_STATUSES = frozenset({"completed", "interrupted", "failed"})

#: ``ThreadItem`` prose roles; tool items use the same vocabulary as the root
#: translator and retain their native input, result and item identity.
_MESSAGE_ITEM_ROLES = {"agentMessage": "assistant", "userMessage": "user"}


class CodexProtocolError(RuntimeError):
    """Codex answered outside the shape its published protocol declares."""


class CodexCall(Protocol):
    """One JSON-RPC call against the conversation's own app-server."""

    async def __call__(
        self, method: str, params: dict[str, Any]
    ) -> Any: ...


@dataclass
class _ChildState:
    """What the platform has already told the console about one child."""

    thread_id: str
    parent_thread_id: str | None = None
    label: str | None = None
    opened: bool = False
    closed: bool = False
    turn_id: str | None = None
    turn_status: str | None = None
    reported_message_refs: set[str] = field(default_factory=set)
    reported_tool_results: set[str] = field(default_factory=set)


def spawned_thread_ids(item: dict[str, Any]) -> list[str]:
    """The children one ``collabAgentToolCall`` announces, if any.

    The in-progress item carries an empty ``receiverThreadIds``; only the
    settled one names the thread. Both are read rather than one, because the
    identity is what matters and the vendor fills it when it has it.
    """

    if str(item.get("type") or "") != COLLAB_ITEM_TYPE:
        return []
    if str(item.get("tool") or "") != SPAWN_TOOL:
        return []
    raw = item.get("receiverThreadIds")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise CodexProtocolError(
            "codex collabAgentToolCall receiverThreadIds is not a list"
        )
    return [str(entry).strip() for entry in raw if str(entry or "").strip()]


def _spawn_source(thread: dict[str, Any]) -> dict[str, Any] | None:
    """The ``thread_spawn`` record when this thread is a spawned sub-agent.

    ``SessionSource`` is a tagged union whose sub-agent arm is itself tagged.
    Its inner fields are the vendor's snake_case while the surrounding
    protocol is camelCase; each is read in the spelling it is declared with,
    because normalizing them to one spelling drops whichever face changes.
    """

    source = thread.get("source")
    if not isinstance(source, dict):
        return None
    sub_agent = source.get("subAgent")
    if not isinstance(sub_agent, dict):
        return None
    spawn = sub_agent.get("thread_spawn")
    return spawn if isinstance(spawn, dict) else None


def child_label(thread: dict[str, Any]) -> str | None:
    """The vendor's own name for a child, preferring its assigned nickname."""

    for key in ("agentNickname", "agentRole", "name"):
        value = str(thread.get(key) or "").strip()
        if value:
            return value
    spawn = _spawn_source(thread)
    if spawn:
        for key in ("agent_nickname", "agent_role"):
            value = str(spawn.get(key) or "").strip()
            if value:
                return value
    return None


def _latest_turn(thread: dict[str, Any]) -> dict[str, Any] | None:
    turns = thread.get("turns")
    if turns is None:
        return None
    if not isinstance(turns, list):
        raise CodexProtocolError("codex thread/read returned a non-list turns field")
    for turn in reversed(turns):
        if isinstance(turn, dict):
            return turn
    return None


def _turn_status(turn: dict[str, Any] | None) -> str | None:
    if turn is None:
        return None
    status = str(turn.get("status") or "").strip()
    if not status:
        raise CodexProtocolError("codex turn carries no status")
    if status != _LIVE_TURN_STATUS and status not in _TERMINAL_TURN_STATUSES:
        raise CodexProtocolError(f"codex turn carries unknown status {status!r}")
    return status


def _turn_error_message(turn: dict[str, Any] | None) -> str | None:
    if turn is None:
        return None
    error = turn.get("error")
    if not isinstance(error, dict):
        return None
    message = str(error.get("message") or "").strip()
    return message or None


def _item_text(item: dict[str, Any]) -> str:
    """One item's prose, in the shape each declared item type carries it."""

    text = item.get("text")
    if isinstance(text, str):
        return text
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        value = block.get("text")
        if isinstance(value, str):
            parts.append(value)
    return "".join(parts)


def lifecycle_fact(
    state: _ChildState,
    *,
    event: str,
    engine_event: str,
    engine_status: str | None = None,
    engine_reason: str | None = None,
) -> dict[str, Any]:
    """One private lifecycle fact, ready for the platform's read model."""

    can_stop = event != "closed" and engine_status == _LIVE_TURN_STATUS
    data: dict[str, Any] = {
        "kind": "lifecycle",
        "engineRef": state.thread_id,
        "event": event,
        "engineEvent": engine_event,
        "operations": ["stop"] if can_stop else [],
    }
    if state.parent_thread_id:
        data["parentEngineRef"] = state.parent_thread_id
    if state.label:
        data["description"] = state.label
    if engine_status:
        data["engineStatus"] = engine_status
    if engine_reason:
        data["engineReason"] = engine_reason
    if can_stop:
        # The child thread is the whole control handle: `turn/interrupt` and
        # `thread/archive` are both addressed by thread id, so nothing else
        # needs to travel with it.
        data["controlRef"] = state.thread_id
    canonical = canonical_child_run_data(data, engine_kind=CODEX_ENGINE_KIND)
    return session_scoped_engine_frame(
        {
            "type": "data-subagent",
            "id": (
                f"codex-child:lifecycle:{state.thread_id}:{state.turn_id}:"
                f"{engine_event}:{event}:{engine_status}"
            ),
            "data": canonical,
        }
    )


def message_fact(
    state: _ChildState, *, role: str, content: list[dict[str, Any]], message_ref: str,
    fact_ref: str | None = None,
) -> dict[str, Any]:
    """One child item, addressed by the vendor's own item id."""

    data: dict[str, Any] = {
        "kind": "message",
        "engineRef": state.thread_id,
        "role": role,
        "content": content,
        "messageId": message_ref,
    }
    if state.parent_thread_id:
        data["parentEngineRef"] = state.parent_thread_id
    canonical = canonical_child_run_data(data, engine_kind=CODEX_ENGINE_KIND)
    return session_scoped_engine_frame(
        {
            "type": "data-subagent",
            "id": f"codex-child:message:{state.thread_id}:{fact_ref or message_ref}",
            "data": canonical,
        }
    )


class CodexChildResources:
    """Fold Codex's spawn announcements and child threads into child facts."""

    def __init__(self, *, root_thread_id: str, call: CodexCall) -> None:
        root = str(root_thread_id or "").strip()
        if not root:
            raise ChildRunProjectionError("codex child projection needs a root thread")
        self._root_thread_id = root
        self._call = call
        self._children: dict[str, _ChildState] = {}
        self._lock = asyncio.Lock()
        #: Native reads and turn/item notifications in observation order. The
        #: idle relay journals them for the same lifecycle and message replay.
        self.native_records: list[dict[str, Any]] = []

    def contains(self, thread_id: str) -> bool:
        return str(thread_id or "").strip() in self._children

    def control_thread_id(self, thread_id: str) -> str | None:
        state = self._children.get(str(thread_id or "").strip())
        return state.thread_id if state is not None else None

    def observe_item(self, item: dict[str, Any]) -> bool:
        """Register the children one parent-stream item announces.

        Returns whether anything new was registered, so the caller can decide
        to refresh without the projector reaching for the link itself.
        """

        discovered = False
        for thread_id in spawned_thread_ids(item):
            if thread_id == self._root_thread_id or thread_id in self._children:
                continue
            self._children[thread_id] = _ChildState(
                thread_id=thread_id,
                parent_thread_id=str(item.get("senderThreadId") or "").strip() or None,
            )
            discovered = True
        return discovered

    async def refresh(self) -> list[dict[str, Any]]:
        """Refresh known child identities without loading their active history."""

        async with self._lock:
            frames: list[dict[str, Any]] = []
            for thread_id in list(self._children):
                frames.extend(await self._refresh_child(thread_id))
            return frames

    def known_thread_ids(self) -> set[str]:
        """The child threads announced so far, closed ones included."""

        return set(self._children)

    def open_thread_ids(self) -> set[str]:
        return {thread_id for thread_id, state in self._children.items() if not state.closed}

    async def refresh_thread(
        self, thread_id: str, *, notification: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """A native child notification can supersede a previous terminal read."""

        async with self._lock:
            method = notification.get("method")
            if thread_id not in self._children:
                thread = await self._read_thread(thread_id, include_turns=False)
                frames = self.fold_thread(thread)
                if thread_id not in self._children:
                    return []
                self.native_records.append({"method": THREAD_READ_METHOD, "thread": dict(thread)})
            else:
                state = self._children[thread_id]
                frames = (
                    await self._refresh_child(thread_id, notified=True)
                    if not state.opened or method == "thread/status/changed" else []
                )
            if method == "turn/completed":
                frames.extend(await self._refresh_child(
                    thread_id, notified=True, include_turns=True,
                ))
            if method in {"turn/started", "turn/completed", "item/started", "item/completed"}:
                self.native_records.append(notification)
                frames.extend(self.fold_notification(notification))
            return frames

    async def _refresh_child(
        self, thread_id: str, *, notified: bool = False, include_turns: bool = False
    ) -> list[dict[str, Any]]:
        state = self._children[thread_id]
        if state.closed and not notified:
            return []
        thread = await self._read_thread(thread_id, include_turns=include_turns)
        self.native_records.append({"method": THREAD_READ_METHOD, "thread": dict(thread)})
        return self.fold_thread(thread)

    async def _read_thread(self, thread_id: str, *, include_turns: bool) -> dict[str, Any]:
        value = await self._call(
            THREAD_READ_METHOD, {"threadId": thread_id, "includeTurns": include_turns}
        )
        thread = (value or {}).get("thread") if isinstance(value, dict) else None
        if not isinstance(thread, dict):
            raise CodexProtocolError(
                f"codex thread/read returned no thread for {thread_id!r}"
            )
        return thread

    def fold_notification(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        """Project native live lifecycle and items without requiring stored turns."""

        method = record.get("method")
        if method not in {"turn/started", "turn/completed", "item/started", "item/completed"}:
            return []
        params = record.get("params")
        if not isinstance(params, dict):
            raise CodexProtocolError("codex child notification carries no params")
        state = self._children.get(str(params.get("threadId") or ""))
        if state is None:
            return []
        if method in {"turn/started", "turn/completed"}:
            turn = params.get("turn")
            if not isinstance(turn, dict) or not turn.get("id"):
                raise CodexProtocolError("codex turn notification carries no turn identity")
            frames = self._message_frames(state, {"turns": [turn]})
            frames.extend(self._turn_frames(state, turn, engine_event=str(method)))
            return frames
        item = params.get("item")
        if not isinstance(item, dict) or not item.get("id"):
            raise CodexProtocolError("codex item notification carries no item identity")
        # An agentMessage starts before its text is complete. Its completed
        # item, unlike the turn summary, retains the complete native message.
        if item.get("type") == "agentMessage" and method != "item/completed":
            return []
        return self._message_frames(
            state, {"turns": [{"items": [item]}]},
            item_completed=method == "item/completed",
        )

    def fold_thread(self, thread: dict[str, Any]) -> list[dict[str, Any]]:
        """Fold one thread document, as ``thread/read`` returns it, into facts.

        Separated from the read so the same fold serves a document the
        platform persisted: the relay journals the thread it read while no
        run was open, and the adapter's durable fold replays it here.
        """

        thread_id = str(thread.get("id") or "").strip()
        if not thread_id:
            raise CodexProtocolError("codex thread document carries no id")
        state = self._children.get(thread_id)
        if state is None:
            spawn = _spawn_source(thread)
            parent = str((spawn or {}).get("parent_thread_id") or "").strip() or None
            if thread_id == self._root_thread_id or parent != self._root_thread_id:
                return []
            state = self._children[thread_id] = _ChildState(
                thread_id=thread_id, parent_thread_id=parent
            )
        spawn = _spawn_source(thread)
        if spawn:
            parent = str(spawn.get("parent_thread_id") or "").strip()
            if parent and parent != thread_id:
                state.parent_thread_id = parent
        label = child_label(thread)
        if label:
            state.label = label

        frames: list[dict[str, Any]] = []
        turn = _latest_turn(thread)
        status = _turn_status(turn)
        if turn is not None and not state.opened:
            state.turn_id = str(turn.get("id") or "") or None
        if not state.opened:
            state.opened = True
            frames.append(
                lifecycle_fact(
                    state,
                    event="opened",
                    engine_event="thread/read",
                    engine_status=status,
                )
            )
        frames.extend(self._message_frames(state, thread))
        if turn is not None:
            frames.extend(self._turn_frames(state, turn, engine_event=THREAD_READ_METHOD))
        return frames

    @staticmethod
    def _turn_frames(
        state: _ChildState, turn: dict[str, Any], *, engine_event: str,
    ) -> list[dict[str, Any]]:
        status = _turn_status(turn)
        turn_id = str(turn.get("id") or "") or None
        changed_turn = turn_id != state.turn_id
        state.turn_id = turn_id
        frames: list[dict[str, Any]] = []
        if status is not None and (status != state.turn_status or changed_turn):
            state.turn_status = status
            state.closed = status in _TERMINAL_TURN_STATUSES
            if status in _TERMINAL_TURN_STATUSES:
                frames.append(
                    lifecycle_fact(
                        state,
                        event="closed",
                        engine_event=engine_event,
                        engine_status=status,
                        engine_reason=_turn_error_message(turn),
                    )
                )
            elif state.opened:
                frames.append(
                    lifecycle_fact(
                        state,
                        event="updated",
                        engine_event=engine_event,
                        engine_status=status,
                    )
                )
        return frames

    def _message_frames(
        self, state: _ChildState, thread: dict[str, Any], *, item_completed: bool = False
    ) -> list[dict[str, Any]]:
        frames: list[dict[str, Any]] = []
        turns = thread.get("turns")
        if not isinstance(turns, list):
            return frames
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            items = turn.get("items")
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type") or "")
                message_ref = str(item.get("id") or "").strip()
                if not message_ref:
                    continue
                if item_type in CODEX_TOOL_ITEM_TYPES:
                    content = self._tool_blocks(state, item, turn, item_completed=item_completed)
                    role = "assistant"
                else:
                    role = _MESSAGE_ITEM_ROLES.get(item_type)
                    if role is None or message_ref in state.reported_message_refs:
                        continue
                    text = _item_text(item)
                    content = [{"type": "text", "text": text}] if text else []
                if not content:
                    continue
                state.reported_message_refs.add(message_ref)
                if item_type in CODEX_TOOL_ITEM_TYPES:
                    # A native item has mutable snapshots but one message
                    # identity. Each immutable input/result fact is distinct.
                    for block in content:
                        digest = hashlib.sha256(json.dumps(
                            block, sort_keys=True, separators=(",", ":"),
                        ).encode()).hexdigest()
                        frames.append(message_fact(
                            state, role=role, content=[block], message_ref=message_ref,
                            fact_ref=f"{message_ref}:{block['type']}:{digest}",
                        ))
                    continue
                frames.append(
                    message_fact(
                        state, role=role, content=content, message_ref=message_ref
                    )
                )
        return frames

    @staticmethod
    def _tool_blocks(
        state: _ChildState, item: dict[str, Any], turn: dict[str, Any], *,
        item_completed: bool = False,
    ) -> list[dict[str, Any]]:
        """Emit an input once and append its native terminal result once.

        CommandExecutionStatus, PatchApplyStatus and McpToolCallStatus come
        from Codex 0.153.4's generated app-server protocol. Status-less items
        settle on their native completion notification or a terminal turn snapshot.
        """

        item_id = str(item["id"])
        blocks: list[dict[str, Any]] = []
        if item_id not in state.reported_message_refs:
            blocks.append({
                "type": "tool_use", "id": item_id,
                "name": item["type"], "input": dict(item),
            })
        status = item.get("status")
        settled = status in {"completed", "failed", "declined"}
        if status is None:
            settled = item_completed or turn.get("status") in _TERMINAL_TURN_STATUSES
        elif status not in {"inProgress", "completed", "failed", "declined"}:
            raise CodexProtocolError(f"codex tool item carries unknown status {status!r}")
        if settled and item_id not in state.reported_tool_results:
            state.reported_tool_results.add(item_id)
            result_state = {
                "failed": "output-error", "declined": "output-denied",
            }.get(str(status), "output-available")
            blocks.append(tool_result_block(
                tool_call_id=item_id,
                content=json.dumps(item, ensure_ascii=False),
                tool_result_state=result_state,
            ))
        return blocks
