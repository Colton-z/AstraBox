"""What Codex tells the relay, in its app-server vocabulary, and the durable fold.

A run is a turn on the conversation's own thread: `turn/started` opens it and
`turn/completed` is the vendor's end. Notifications carry `threadId`, so a
turn on a child thread is not a run of this conversation. Metadata establishes
the child's identity, native notifications carry its live work, and stored
history reconciles at completion. The idle relay journals all three sources
for the adapter's durable fold.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from astrabox.core.service.orchestrator.engine.codex import CodexEngineAdapter
from astrabox.core.service.orchestrator.engine.codex_child_runs import (
    CodexChildResources,
)
from astrabox.core.service.orchestrator.engine.codex_client import (
    CodexEngineClient,
    _CodexRelaySeam,
)
from astrabox.core.service.orchestrator.engine.resident_relay import (
    CountedRecord,
    ResidentRelay,
)

ROOT = "01a086fe-cd7e-77c1-b3fa-153cd0f7261d"
CHILD = "01a0872e-8943-7182-b698-ad8a7468dc40"


def _thread(status: str, *, text: str | None = None) -> dict[str, Any]:
    items = [{"type": "agentMessage", "id": "msg-1", "text": text}] if text else []
    return {
        "id": CHILD,
        "agentNickname": "Carver",
        "source": {"subAgent": {"thread_spawn": {"parent_thread_id": ROOT, "depth": 1}}},
        "turns": [{"id": "t1", "status": status, "error": None, "items": items}],
    }


class _Link:
    def __init__(self, *replies: dict[str, Any]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.is_live = True

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, dict(params or {})))
        if method == "thread/read":
            return {"thread": self.replies.pop(0)} if self.replies else None
        return {}


def _client(link: _Link | None = None) -> CodexEngineClient:
    client = CodexEngineClient(session_id="sess-1", link=link or _Link(), native_thread_id=ROOT)
    return client


def _notification(method: str, thread_id: str, **params: Any) -> dict[str, Any]:
    return {"method": method, "params": {"threadId": thread_id, **params}}


# ── boundaries and identity ──────────────────────────────────────────────
def test_a_run_is_a_turn_on_the_conversations_own_thread() -> None:
    seam = _CodexRelaySeam(_client())

    assert seam.starts_run(_notification("turn/started", ROOT, turn={"id": "turn-9"}))
    assert seam.settles_run(
        _notification("turn/completed", ROOT, turn={"id": "turn-9", "status": "completed"})
    )
    # A child thread's turn is that child's business, not a run here.
    assert not seam.starts_run(_notification("turn/started", CHILD, turn={"id": "turn-c"}))
    assert not seam.settles_run(_notification("turn/completed", CHILD, turn={"id": "turn-c"}))


def test_the_response_identity_is_the_vendors_turn_id() -> None:
    seam = _CodexRelaySeam(_client())

    assert (
        seam.response_id(_notification("turn/started", ROOT, turn={"id": "turn-9"}), 7) == "turn-9"
    )


def test_the_position_is_the_clients_own_count() -> None:
    assert _CodexRelaySeam.sequence(CountedRecord(sequence=42, record={})) == 42


# ── what translates ──────────────────────────────────────────────────────
def test_a_child_threads_notification_is_not_this_responses_output() -> None:
    seam = _CodexRelaySeam(_client())
    translator = seam.new_translator()

    frames = seam.translate(
        translator,
        _notification(
            "item/started",
            CHILD,
            turnId="turn-c",
            item={"type": "agentMessage", "id": "m", "text": "x"},
        ),
    )

    assert frames == []


def test_a_server_request_is_an_interaction_not_output() -> None:
    client = _client()
    seam = _CodexRelaySeam(client)
    request = {
        "id": 5,
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": ROOT,
            "turnId": "turn-9",
            "itemId": "i",
            "command": ["ls"],
            "cwd": "/w",
        },
    }

    frame = seam.interaction(request)

    assert frame is not None and frame["type"] == "interaction.request"
    assert seam.translate(seam.new_translator(), request) == []


# ── child facts ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_spawn_on_the_root_thread_reads_the_child_and_keeps_the_document() -> None:
    link = _Link({**_thread("inProgress"), "turns": []})
    client = _client(link)
    seam = _CodexRelaySeam(client)
    spawn = _notification(
        "item/completed",
        ROOT,
        turnId="turn-9",
        item={
            "type": "collabAgentToolCall",
            "tool": "spawnAgent",
            "id": "exec-1",
            "status": "completed",
            "senderThreadId": ROOT,
            "receiverThreadIds": [CHILD],
        },
    )

    assert seam.carries_child_facts(spawn)
    facts = await seam.child_facts(spawn)

    # Initial discovery needs identity, not the initializing child's history.
    assert [f["data"]["event"] for f in facts] == ["opened"]
    assert link.calls == [("thread/read", {"threadId": CHILD, "includeTurns": False})]
    natives = seam.native_records()
    assert [n["method"] for n in natives] == ["thread/read"]
    assert natives[0]["thread"]["id"] == CHILD
    assert seam.native_records() == []


@pytest.mark.asyncio
async def test_a_known_childs_completed_turn_is_read_again_and_closes_it() -> None:
    link = _Link({**_thread("inProgress"), "turns": []}, _thread("completed", text="done"))
    client = _client(link)
    seam = _CodexRelaySeam(client)
    await seam.child_facts(
        _notification(
            "item/completed",
            ROOT,
            turnId="turn-9",
            item={
                "type": "collabAgentToolCall",
                "tool": "spawnAgent",
                "id": "e",
                "status": "completed",
                "senderThreadId": ROOT,
                "receiverThreadIds": [CHILD],
            },
        )
    )
    seam.native_records()

    done = _notification("turn/completed", CHILD, turn={"id": "t1", "status": "completed", "items": []})
    assert seam.carries_child_facts(done)
    facts = await seam.child_facts(done)

    kinds = [(f["data"]["kind"], f["data"].get("event")) for f in facts]
    assert kinds == [("message", None), ("lifecycle", "closed")]
    # A later native notification can supersede this terminal history read.
    assert seam.carries_child_facts(done)


@pytest.mark.asyncio
async def test_a_stranger_threads_notification_carries_no_facts() -> None:
    seam = _CodexRelaySeam(_client())
    stray = _notification(
        "turn/completed", "some-other-thread", turn={"id": "t", "status": "completed"}
    )

    assert not seam.carries_child_facts(stray)
    assert await seam.child_facts(stray) == []


# ── the durable fold ─────────────────────────────────────────────────────
def test_the_adapter_folds_journaled_thread_documents() -> None:
    facts = CodexEngineAdapter().durable_child_resource_facts(
        [
            {"method": "thread/read", "thread": _thread("inProgress")},
            {"method": "turn/completed", "params": {}},
            {"method": "thread/read", "thread": _thread("completed", text="GATEWAY-OK")},
        ]
    )

    assert [
        (i, f.as_frame()["data"]["kind"], f.as_frame()["data"].get("event")) for i, f in facts
    ] == [
        (0, "lifecycle", "opened"),
        (0, "lifecycle", "updated"),
        (2, "message", None),
        (2, "lifecycle", "closed"),
    ]
    assert facts[0][1].as_frame()["data"]["parentEngineRef"] == ROOT


def test_a_document_without_a_parent_folds_to_nothing() -> None:
    orphan = {"id": CHILD, "turns": [{"id": "t", "status": "completed", "items": []}]}

    assert (
        CodexEngineAdapter().durable_child_resource_facts(
            [{"method": "thread/read", "thread": orphan}]
        )
        == []
    )


def test_fold_thread_ignores_a_thread_that_is_not_this_conversations_child() -> None:
    async def _no_call(method: str, params: dict[str, Any]) -> Any:
        raise AssertionError("unused")

    projector = CodexChildResources(root_thread_id="another-root", call=_no_call)

    assert projector.fold_thread(_thread("inProgress")) == []


class _Journal:
    """The engine event sink: what the idle relay journals for the durable fold."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def persist_event(
        self, *, engine_kind: str, causation_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self.rows.append({"causation_id": causation_id, **payload})
        return {}


@pytest.mark.asyncio
async def test_a_child_found_during_a_turn_keeps_its_idle_tool_call_in_the_durable_fold() -> None:
    """A background child is found while the platform turn runs, and the turn
    persists that child's facts itself. After the turn, the child's status push
    changes nothing, and then its command starts. The durable fold learns a
    child only from a thread document. So the discovery read has to reach the
    journal, or the running command is missing from the child's transcript
    until the child ends."""

    identity = {**_thread("inProgress"), "turns": []}
    link = _Link(identity, identity)
    client = _client(link)
    # The platform turn's own path: the spawn announcement on the root thread.
    discovered = await client._child_frames(
        _notification(
            "item/completed",
            ROOT,
            turnId="turn-9",
            item={
                "type": "collabAgentToolCall",
                "tool": "spawnAgent",
                "id": "exec-1",
                "status": "completed",
                "senderThreadId": ROOT,
                "receiverThreadIds": [CHILD],
            },
        )
    )
    assert [f["data"]["event"] for f in discovered] == ["opened"]

    wire: asyncio.Queue[CountedRecord] = asyncio.Queue()
    journal = _Journal()

    async def _floor() -> int:
        return 0

    async def _send(payload: dict[str, Any]) -> None:
        raise AssertionError(f"nothing is owed: {payload}")

    relay = ResidentRelay(
        seam=_CodexRelaySeam(client),
        session_id="sess-1",
        engine_session_key=None,
        next_record=wire.get,
        send_command=_send,
        current_sequence=_floor,
        resident_output_sink=None,
        event_sink=journal,
    )
    command = {
        "type": "commandExecution",
        "id": "call-1",
        "command": "/bin/bash -lc 'sleep 90'",
        "cwd": "/workspace",
        "status": "inProgress",
    }
    relay.start()
    try:
        await wire.put(CountedRecord(sequence=20, record=_notification(
            "thread/status/changed", CHILD, status={"type": "active"},
        )))
        await wire.put(CountedRecord(sequence=21, record=_notification(
            "item/started", CHILD, turnId="t1", item=command,
        )))
        for _ in range(50):
            await asyncio.sleep(0)
            if any(row["runner_sequence"] == 21 for row in journal.rows):
                break
    finally:
        await relay.stop()
    assert relay.failure is None

    facts = CodexEngineAdapter().durable_child_resource_facts(
        [row["message"] for row in journal.rows]
    )
    tool_calls = [
        block
        for _index, fact in facts
        for block in fact.as_frame()["data"].get("content") or []
        if block.get("type") == "tool_use"
    ]
    assert [(block["id"], block["name"]) for block in tool_calls] == [
        ("call-1", "commandExecution")
    ]
