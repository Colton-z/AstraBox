"""A Codex sub-agent, from the spawn announcement to a run the console shows.

The parent-stream payloads here were taken verbatim from a live app-server:
the model spawned a sub-agent, and the server emitted an `item/started` whose
`receiverThreadIds` is still empty followed by an `item/completed` that names
the thread. Both are kept because the difference between them is
the whole reason the projector reads a thread rather than trusting the first
announcement.

What no payload here contains is a `subAgentActivity` item. That conversation
produced none: after the spawn settles, the parent stream says nothing further
about the child, which is why closing a child run has to come from reading the
child's own thread.
"""

from __future__ import annotations

from typing import Any

import pytest

from astrabox.core.service.orchestrator.engine.codex_child_runs import (
    CodexChildResources,
    CodexProtocolError,
    child_label,
    spawned_thread_ids,
)
from astrabox.core.service.orchestrator.engine.codex import CodexEngineAdapter
from astrabox.core.service.orchestrator.engine.codex_client import CodexEngineClient, _CodexRelaySeam
from astrabox.core.service.orchestrator.engine.codex_events import (
    CodexTurnTranslator,
)
from astrabox.core.service.orchestrator.engine.codex_link import CodexRpcError
from astrabox.core.service.orchestrator.session_child_run_view import project_session_child_runs

ROOT = "01a086fe-cd7e-77c1-b3fa-153cd0f7261d"
CHILD = "01a0872e-8943-7182-b698-ad8a7468dc40"

#: The settled spawn item, verbatim from the app-server.
SPAWNED_ITEM: dict[str, Any] = {
    "id": "exec-3ec1fc9b-afca-44f9-b90a-a7bc17241849",
    "tool": "spawnAgent",
    "type": "collabAgentToolCall",
    "model": "gpt-5.6-luna",
    "prompt": "Reply exactly GATEWAY-V1100-OK",
    "status": "completed",
    "agentsStates": {CHILD: {"status": "pendingInit", "message": None}},
    "senderThreadId": ROOT,
    "reasoningEffort": "medium",
    "receiverThreadIds": [CHILD],
}

#: The same call while it was still running: no child named yet.
SPAWNING_ITEM: dict[str, Any] = {
    **SPAWNED_ITEM,
    "model": "",
    "status": "inProgress",
    "agentsStates": {},
    "receiverThreadIds": [],
}


def _thread(
    *,
    status: str,
    items: list[dict[str, Any]] | None = None,
    turn_id: str = "turn-1",
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One `thread/read` answer for the spawned child."""

    return {
        "thread": {
            "id": CHILD,
            "cliVersion": "0.153.4",
            "agentNickname": "Carver",
            "agentRole": None,
            "name": None,
            "source": {
                "subAgent": {
                    "thread_spawn": {
                        "parent_thread_id": ROOT,
                        "depth": 1,
                        "agent_path": None,
                        "agent_nickname": "Carver",
                        "agent_role": None,
                    }
                }
            },
            "turns": [
                {
                    "id": turn_id,
                    "status": status,
                    "error": error,
                    "startedAt": 1,
                    "completedAt": None,
                    "items": items or [],
                }
            ],
        }
    }


class _ScriptedLink:
    """The child's own thread, answered as the app-server answers it."""

    def __init__(self, *replies: dict[str, Any]) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._replies = list(replies)

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, dict(params)))
        if method == "thread/read":
            return self._replies.pop(0) if self._replies else None
        return {"ok": True}


def _facts(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [frame["data"] for frame in frames]


@pytest.mark.asyncio
async def test_initializing_child_history_does_not_fail_the_parent_stream() -> None:
    """The observed early history error must not be requested by live projection."""

    task = {"type": "userMessage", "id": "task", "content": [{"type": "text", "text": "Read the receipt"}]}
    command = {
        "type": "commandExecution", "id": "command", "command": "cat receipt.txt",
        "cwd": "/workspace", "status": "inProgress", "aggregatedOutput": None,
        "exitCode": None,
    }
    result = {**command, "status": "completed", "aggregatedOutput": "native receipt", "exitCode": 0}
    answer = {"type": "agentMessage", "id": "answer", "text": "The full native receipt answer"}
    # A stored read can lag the just-received terminal notification. Native
    # completion must be the final lifecycle authority, not this older turn.
    history = _thread(status="inProgress", items=[task, result, answer], turn_id="older-turn")
    metadata = {**history["thread"], "historyMode": "paginated", "turns": []}

    class InitializingHistoryLink:
        completed = False

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def call(self, method: str, params: dict[str, Any]) -> Any:
            self.calls.append((method, dict(params)))
            assert method == "thread/read"
            assert params["threadId"] == CHILD
            if not params.get("includeTurns"):
                return {"thread": metadata}
            if not self.completed:
                raise CodexRpcError(code=-32601, message="list_turns is not supported yet")
            return history

    link = InitializingHistoryLink()
    client = CodexEngineClient(session_id="session-1", native_thread_id=ROOT, link=link)
    seam = _CodexRelaySeam(client)
    frames = await client._child_frames({
        "method": "item/completed",
        "params": {"threadId": ROOT, "turnId": "parent-turn", "item": SPAWNED_ITEM},
    })
    assert [fact["event"] for fact in _facts(frames)] == ["opened"]
    started = {
        "method": "turn/started",
        "params": {"threadId": CHILD, "turn": {"id": "turn-1", "status": "inProgress", "items": []}},
    }
    frames.extend(await client._child_thread_frames(started))
    frames.extend(await client._settle_child_frames())
    # A metadata-only refresh at the parent's terminal must retain the active
    # child's native turn identity instead of treating turns=[] as no work.
    assert client._child_resource_projector()._children[CHILD].turn_id == "turn-1"
    for method, item in [
        ("item/completed", task), ("item/started", command), ("item/completed", result),
        ("item/started", {**answer, "text": "The"}), ("item/completed", answer),
    ]:
        frames.extend(await client._child_thread_frames({
            "method": method, "params": {"threadId": CHILD, "turnId": "turn-1", "item": item},
        }))

    def project(values: list[dict[str, Any]]) -> dict[str, Any]:
        return project_session_child_runs(
            session_id="session-1", events=[], include_messages=True,
            frames=[{
                "session_id": "session-1", "frame_seq": index + 1,
                "turn_id": None, "scope": "session", "payload": frame,
            } for index, frame in enumerate(values)],
        )[0]

    live = project(frames)
    assert live["engine_status"] == "inProgress"
    assert CodexEngineAdapter().child_run_is_active(live) is True
    messages = [fact for fact in _facts(frames) if fact["kind"] == "message"]
    blocks = [block for fact in messages for block in fact["content"]]
    assert [block["text"] for block in blocks if block["type"] == "text"] == [
        "Read the receipt", "The full native receipt answer",
    ]
    assert [block["input"] for block in blocks if block["type"] == "tool_use"] == [command]
    results = [block for block in blocks if block["type"] == "tool_result"]
    assert len(results) == 1 and results[0]["tool_use_id"] == command["id"]
    assert "native receipt" in results[0]["content"]
    live_records = seam.native_records()
    live_replay = [fact.as_frame() for _, fact in CodexEngineAdapter().durable_child_resource_facts(live_records)]
    assert project(live_replay) == live

    link.completed = True
    frames.extend(await client._child_thread_frames({
        "method": "turn/completed",
        "params": {"threadId": CHILD, "turn": {
            "id": "turn-1", "status": "completed", "items": [answer], "error": None,
        }},
    }))
    final = project(frames)
    assert final["closed"] is True and final["engine_status"] == "completed"
    assert CodexEngineAdapter().child_run_is_active(final) is False
    assert client._child_resource_projector()._children[CHILD].turn_id == "turn-1"
    assert final["messages"] == live["messages"]
    records = [*live_records, *seam.native_records()]
    replayed = [fact.as_frame() for _, fact in CodexEngineAdapter().durable_child_resource_facts(records)]
    assert project(replayed) == final
    assert link.calls[-1] == ("thread/read", {"threadId": CHILD, "includeTurns": True})
    assert all(not params["includeTurns"] for _, params in link.calls[:-1])
    assert seam.native_records() == []


def test_metadata_reads_preserve_turn_identity_and_new_history_turns_publish_status() -> None:
    async def no_call(method: str, params: dict[str, Any]) -> Any:
        raise AssertionError("the recorded-history fold must not call the engine")

    projector = CodexChildResources(root_thread_id=ROOT, call=no_call)
    thread = _thread(status="inProgress")["thread"]
    projector.fold_thread(thread)
    assert projector.fold_thread({**thread, "turns": []}) == []
    assert projector._children[CHILD].turn_id == "turn-1"
    frames = projector.fold_thread(_thread(status="inProgress", turn_id="turn-2")["thread"])
    assert len(frames) == 1
    assert frames[0]["data"]["event"] == "updated"
    assert frames[0]["data"]["engineStatus"] == "inProgress"
    assert ":turn-2:" in frames[0]["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("owned", [True, False])
async def test_child_turn_before_spawn_result_requires_native_parent_ownership(owned: bool) -> None:
    metadata = {**_thread(status="inProgress")["thread"], "turns": []}
    if not owned:
        metadata["source"] = {"subAgent": {"thread_spawn": {"parent_thread_id": "another-root"}}}
    link = _ScriptedLink({"thread": metadata})
    client = CodexEngineClient(session_id="session-1", native_thread_id=ROOT, link=link)
    seam = _CodexRelaySeam(client)
    started = {
        "method": "turn/started",
        "params": {"threadId": CHILD, "turn": {"id": "turn-1", "status": "inProgress", "items": []}},
    }
    assert seam.carries_child_facts(started)
    frames = await seam.child_facts(started)
    assert link.calls == [("thread/read", {"threadId": CHILD, "includeTurns": False})]
    if not owned:
        assert frames == []
        assert seam.native_records() == []
        assert not client._child_resource_projector().contains(CHILD)
        return
    assert [frame["data"]["event"] for frame in frames] == ["opened", "updated"]
    assert frames[-1]["data"]["engineStatus"] == "inProgress"
    assert frames[-1]["data"]["parentEngineRef"] == ROOT
    assert await client._child_frames({
        "method": "item/completed", "params": {"threadId": ROOT, "item": SPAWNED_ITEM},
    }) == []
    replayed = [fact.as_frame() for _, fact in CodexEngineAdapter().durable_child_resource_facts(seam.native_records())]
    assert replayed == frames


# ── the announcement ─────────────────────────────────────────────────────
def test_a_settled_spawn_names_the_child_thread() -> None:
    assert spawned_thread_ids(SPAWNED_ITEM) == [CHILD]


def test_a_spawn_still_running_names_nobody() -> None:
    """The in-progress item carries an empty list, not a missing field.

    Registering a child from it would mint a run with no engine identity, so
    the projector waits for the announcement that has one.
    """

    assert spawned_thread_ids(SPAWNING_ITEM) == []


def test_a_collab_call_that_is_not_a_spawn_names_nobody() -> None:
    assert spawned_thread_ids({**SPAWNED_ITEM, "tool": "sendMessage"}) == []


def test_a_malformed_receiver_list_fails_loudly() -> None:
    with pytest.raises(CodexProtocolError, match="receiverThreadIds"):
        spawned_thread_ids({**SPAWNED_ITEM, "receiverThreadIds": CHILD})


def test_the_child_is_named_by_the_nickname_the_vendor_assigned() -> None:
    """`agentNickname` is documented as the sub-agent's own assigned name."""

    assert child_label(_thread(status="completed")["thread"]) == "Carver"


# ── the run the console shows ────────────────────────────────────────────
def test_reading_the_child_thread_opens_and_closes_the_run() -> None:
    """The parent stream never closes a child, so the child's thread must.

    A measured conversation emitted no `subAgentActivity` at all: the spawn
    settles with the child at `pendingInit` and that is the last the parent
    hears. A projector that waited for a parent-side terminal would leave every
    Codex child run open forever.
    """

    history = _thread(
        status="completed",
        items=[
            {
                "type": "agentMessage",
                "id": "msg-child-1",
                "text": "GATEWAY-V1100-OK",
            }
        ],
    )
    link = _ScriptedLink()
    projector = CodexChildResources(root_thread_id=ROOT, call=link.call)

    assert projector.observe_item(SPAWNED_ITEM) is True
    facts = _facts(projector.fold_thread(history["thread"]))
    lifecycle = [fact for fact in facts if fact["kind"] == "lifecycle"]
    assert [fact["event"] for fact in lifecycle] == ["opened", "closed"]
    assert lifecycle[0]["engineRef"] == CHILD
    assert lifecycle[0]["parentEngineRef"] == ROOT
    assert lifecycle[0]["description"] == "Carver"
    assert lifecycle[-1]["engineStatus"] == "completed"
    messages = [fact for fact in facts if fact["kind"] == "message"]
    assert [fact["content"][0]["text"] for fact in messages] == ["GATEWAY-V1100-OK"]
    assert messages[0]["role"] == "assistant"


def test_a_running_child_offers_stop_and_a_control_reference() -> None:
    link = _ScriptedLink()
    projector = CodexChildResources(root_thread_id=ROOT, call=link.call)
    projector.observe_item(SPAWNED_ITEM)

    opened = _facts(projector.fold_thread(_thread(status="inProgress")["thread"]))[0]

    assert opened["operations"] == ["stop"]
    # The thread is the whole handle: the protocol addresses interrupt and
    # archive by thread id, so a second identity would have no reader.
    assert opened["controlRef"] == CHILD


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["item/started", "item/completed"])
async def test_child_item_boundaries_publish_content_before_the_turn_finishes(
    method: str,
) -> None:
    item = {
        "type": "commandExecution", "id": "held-command", "command": "python held.py",
        "cwd": "/workspace", "status": "inProgress" if method == "item/started" else "completed",
        "aggregatedOutput": None if method == "item/started" else "native receipt",
        "exitCode": None if method == "item/started" else 0,
    }
    metadata = {**_thread(status="interrupted")["thread"], "turns": []}
    link = _ScriptedLink(
        {"thread": metadata},
        _thread(status="interrupted"),
        _thread(status="completed", items=[{
            "type": "agentMessage", "id": "child-answer", "text": "real child completion",
        }]),
    )
    client = CodexEngineClient(
        session_id="session-1", native_thread_id=ROOT, link=link,
    )
    projector = client._child_resource_projector()
    projector.observe_item(SPAWNED_ITEM)
    initial = await projector.refresh()
    initial.extend(await client._child_thread_frames({
        "method": "turn/completed",
        "params": {"threadId": CHILD, "turn": {"id": "turn-1", "status": "interrupted", "items": []}},
    }))
    assert not projector.open_thread_ids()

    seam = _CodexRelaySeam(client)
    notification = {
        "method": method,
        "params": {"threadId": CHILD, "turnId": "turn-1", "item": item},
    }
    assert seam.carries_child_facts(notification)
    continued = await seam.child_facts({
        "method": "turn/started",
        "params": {"threadId": CHILD, "turn": {"id": "turn-1", "status": "inProgress", "items": []}},
    })
    continued.extend(await seam.child_facts({
        "method": "item/completed",
        "params": {"threadId": CHILD, "turnId": "turn-1", "item": {
            "type": "userMessage", "id": "child-task",
            "content": [{"type": "text", "text": "Hold the child task"}],
        }},
    }))
    continued.extend(await seam.child_facts(notification))
    facts = _facts(continued)

    messages = [fact for fact in facts if fact["kind"] == "message"]
    assert messages[0]["content"] == [{"type": "text", "text": "Hold the child task"}]
    assert messages[0]["role"] == "user"
    assert messages[1]["content"] == [{
        "type": "tool_use", "id": "held-command", "name": "commandExecution", "input": item,
    }]
    results = [block for message in messages for block in message["content"] if block["type"] == "tool_result"]
    assert len(results) == (1 if method == "item/completed" else 0)
    if results:
        assert "native receipt" in results[0]["content"]
        assert results[0]["tool_use_id"] == "held-command"
    assert CHILD in projector.open_thread_ids()
    assert len(link.calls) == 2
    assert len(projector.native_records) == 6
    def project(frames):
        return project_session_child_runs(
            session_id="session-1", events=[], include_messages=True,
            frames=[{
                "session_id": "session-1", "frame_seq": index + 1,
                "turn_id": None, "scope": "session", "payload": frame,
            } for index, frame in enumerate(frames)],
        )[0]

    assert project([*initial, *continued])["closed"] is False
    finished = await client._child_thread_frames({
        "method": "turn/completed", "params": {
            "threadId": CHILD, "turn": {"id": "turn-1", "status": "completed", "items": []},
        },
    })
    final = project([*initial, *continued, *finished])
    assert final["closed"] is True
    assert final["engine_status"] == "completed"
    assert final["messages"][-1]["content"] == [{"type": "text", "text": "real child completion"}]
    replayed = [
        fact.as_frame() for _, fact in
        CodexEngineAdapter().durable_child_resource_facts(seam.native_records())
    ]
    assert project(replayed) == final
    assert seam.native_records() == []


def test_a_finished_child_offers_no_stop() -> None:
    link = _ScriptedLink()
    projector = CodexChildResources(root_thread_id=ROOT, call=link.call)
    projector.observe_item(SPAWNED_ITEM)

    facts = _facts(projector.fold_thread(_thread(status="completed")["thread"]))

    assert all(fact.get("operations") == [] for fact in facts if fact["kind"] == "lifecycle")
    assert all("controlRef" not in fact for fact in facts)


@pytest.mark.asyncio
async def test_a_closed_child_is_not_read_again() -> None:
    """Closing is final, and re-reading would republish the same run."""

    link = _ScriptedLink()
    projector = CodexChildResources(root_thread_id=ROOT, call=link.call)
    projector.observe_item(SPAWNED_ITEM)
    projector.fold_thread(_thread(status="completed")["thread"])

    assert _facts(await projector.refresh()) == []
    assert len(link.calls) == 0


def test_the_same_child_message_is_published_once() -> None:
    command = {
        "type": "commandExecution", "id": "child-command", "command": "cat child-receipt",
        "cwd": "/workspace", "status": "inProgress", "aggregatedOutput": None,
        "exitCode": None,
    }
    completed = {**command, "status": "completed", "aggregatedOutput": "native-receipt", "exitCode": 0}
    histories = [
        _thread(
            status="inProgress",
            items=[{"type": "agentMessage", "id": "msg-child-1", "text": "half"}, command],
        ),
        _thread(
            status="completed",
            items=[
                {"type": "agentMessage", "id": "msg-child-1", "text": "half"},
                {"type": "agentMessage", "id": "msg-child-2", "text": "done"},
                completed,
            ],
        ),
    ]
    link = _ScriptedLink()
    projector = CodexChildResources(root_thread_id=ROOT, call=link.call)
    projector.observe_item(SPAWNED_ITEM)

    first_frames = projector.fold_thread(histories[0]["thread"])
    second_frames = projector.fold_thread(histories[1]["thread"])
    first = _facts(first_frames)
    second = _facts(second_frames)

    texts = [
        fact["content"][0]["text"]
        for fact in first + second
        if fact["kind"] == "message" and fact["content"][0]["type"] == "text"
    ]
    assert texts == ["half", "done"]
    tool_messages = [fact for fact in first + second if fact.get("messageId") == command["id"]]
    assert len(tool_messages) == 2
    assert tool_messages[0]["content"] == [{
        "type": "tool_use", "id": command["id"], "name": "commandExecution", "input": command,
    }]
    result = tool_messages[1]["content"][0]
    assert result["type"] == "tool_result"
    assert result["tool_use_id"] == command["id"]
    assert result["tool_result_state"] == "output-available"
    assert "native-receipt" in result["content"]
    cold = CodexChildResources(root_thread_id=ROOT, call=link.call)
    cold_frames = cold.fold_thread(_thread(status="completed", items=[completed])["thread"])
    cold_tools = [fact for fact in _facts(cold_frames) if fact.get("messageId") == command["id"]]
    assert cold_tools[0]["content"][0]["input"]["command"] == command["command"]
    assert cold_tools[1]["content"][0] == result
    frames = [*first_frames, *second_frames, *[frame for frame in cold_frames if frame["data"]["kind"] == "message"]]
    children = project_session_child_runs(
        session_id="session-1", events=[], include_messages=True,
        frames=[{
            "session_id": "session-1", "frame_seq": index + 1,
            "turn_id": None, "scope": "session", "payload": frame,
        } for index, frame in enumerate(frames)],
    )
    tools = [block for message in children[0]["messages"] for block in message["content"]
             if block["type"] in {"tool_use", "tool_result"}]
    assert [block["type"] for block in tools] == ["tool_use", "tool_result"]
    assert tools[0]["input"] == completed
    assert tools[1] == result


def test_a_failed_child_turn_carries_the_engines_own_reason() -> None:
    link = _ScriptedLink()
    projector = CodexChildResources(root_thread_id=ROOT, call=link.call)
    projector.observe_item(SPAWNED_ITEM)

    closed = [
        fact
        for fact in _facts(projector.fold_thread(_thread(
            status="failed", error={"message": "model refused the task"},
        )["thread"]))
        if fact["kind"] == "lifecycle" and fact["event"] == "closed"
    ]

    assert closed[0]["engineStatus"] == "failed"
    assert closed[0]["engineReason"] == "model refused the task"


def test_a_turn_status_this_version_does_not_know_fails_loudly() -> None:
    """Guessing an unknown status closes or holds a run on an invention."""

    link = _ScriptedLink()
    projector = CodexChildResources(root_thread_id=ROOT, call=link.call)
    projector.observe_item(SPAWNED_ITEM)

    with pytest.raises(CodexProtocolError, match="unknown status"):
        projector.fold_thread(_thread(status="quantum")["thread"])


@pytest.mark.asyncio
async def test_a_thread_read_without_a_thread_fails_loudly() -> None:
    link = _ScriptedLink({})
    projector = CodexChildResources(root_thread_id=ROOT, call=link.call)
    projector.observe_item(SPAWNED_ITEM)

    with pytest.raises(CodexProtocolError, match="no thread"):
        await projector.refresh()


# ── the console is not told the same thing twice ─────────────────────────
def test_child_items_do_not_also_arrive_as_diagnostic_cards() -> None:
    """The Agents panel owns these; a raw card beside it would duplicate them."""

    translator = CodexTurnTranslator()
    started = list(
        translator.translate(
            {
                "method": "item/started",
                "params": {"item": SPAWNING_ITEM, "threadId": ROOT, "turnId": "t1"},
            }
        )
    )
    completed = list(
        translator.translate(
            {
                "method": "item/completed",
                "params": {"item": SPAWNED_ITEM, "threadId": ROOT, "turnId": "t1"},
            }
        )
    )

    assert started == []
    assert completed == []
