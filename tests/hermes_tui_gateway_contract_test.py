"""Hermes is embedded through its official TUI JSON-RPC entry point.

The host reaches the sandbox's execd PTY endpoint, starts one official TUI
gateway process for each AstraBox conversation, and keeps model credentials on
the outbound Credential Vault path.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from astrabox.core.service.orchestrator.engine.base import (
    EngineConversationBinding,
    EngineInputCommand,
    EngineOutputCheckpoint,
    EngineStreamDetached,
)
from astrabox.core.service.orchestrator.engine.emissions import EngineEmission, TurnTerminal
from astrabox.core.service.orchestrator.engine.hermes_client import (
    HermesTuiEngineClient,
    HermesTuiProcess,
    HermesTuiProtocolError,
    HermesTuiWireEvent,
    decode_turn_anchor,
    encode_turn_anchor,
    _open_hermes_output_blocks,
    translate_tui_event,
)
from astrabox.core.service.orchestrator.session_kernel.service_mixins.durable_recovery_assistant import (
    build_engine_output_checkpoint,
)


def test_turn_anchor_carries_what_outlives_an_attachment() -> None:
    """Two fields, because those are the two that name anything durable.

    Deliberately not the gateway process a turn was established on: the engine
    is a supervised service and nothing names an instance of it — checked on a
    real box, `gateway.ready` carries only a UI skin and `/api/status` is
    byte-identical across a restart. A third field naming one would be a
    constant dressed as a fence.
    """

    encoded = encode_turn_anchor(tui_session_id="tui-1", turn_id="turn-1")

    assert decode_turn_anchor(encoded) == {
        "tui_session_id": "tui-1",
        "turn_id": "turn-1",
    }


class _FakeSubscription:
    """One session's live event stream: what was pushed, then nothing until more is."""

    def __init__(self, tui_session_id: str, after_offset: int = 0) -> None:
        self.tui_session_id = tui_session_id
        self.last_offset = after_offset
        self._queue: asyncio.Queue[HermesTuiWireEvent] = asyncio.Queue()

    def push(self, *events: HermesTuiWireEvent) -> None:
        for event in events:
            self._queue.put_nowait(event)

    async def next_event(self) -> HermesTuiWireEvent:
        wire = await self._queue.get()
        self.last_offset = wire.output_offset
        return wire


def _run(tui_session_id: str, text: str, *, start: int) -> list[HermesTuiWireEvent]:
    """The records Hermes emits for one accepted ``prompt.submit``."""

    return [
        HermesTuiWireEvent(
            event={"type": "message.start", "session_id": tui_session_id}, output_offset=start
        ),
        HermesTuiWireEvent(
            event={"type": "message.delta", "session_id": tui_session_id, "payload": {"text": text}},
            output_offset=start + 1,
        ),
        HermesTuiWireEvent(
            event={
                "type": "message.complete",
                "session_id": tui_session_id,
                "payload": {"status": "complete"},
            },
            output_offset=start + 2,
        ),
    ]


def _fake_gateway(request: AsyncMock) -> SimpleNamespace:
    """The narrow gateway port a session-scoped client consumes."""

    return SimpleNamespace(
        is_live=True,
        request=request,
        subscribe=lambda tui_session_id, after_offset=0: _FakeSubscription(
            tui_session_id, after_offset
        ),
        unsubscribe=lambda _subscription: None,
    )


@pytest.mark.asyncio
async def test_final_disposal_closes_only_this_conversations_session() -> None:
    """Dispose ends one vendor session; the shared gateway PTY survives.

    The gateway process is the profile's resident runtime, shared with the
    profile's other conversations. A dispose that deleted the PTY would kill
    every sibling conversation with it.
    """

    calls: list[str] = []

    async def request(method: str, params: dict[str, str]) -> dict[str, bool]:
        calls.append(f"rpc:{method}:{params['session_id']}")
        return {"closed": True}

    gateway = _fake_gateway(AsyncMock(side_effect=request))
    client = HermesTuiEngineClient(
        gateway=gateway,
        platform_session_id="session-1",
    )
    client._tui_session_id = "tui-1"

    closed = await client.dispose()

    assert closed is True
    assert calls == ["rpc:session.close:tui-1"]
    assert client.is_live is False


@pytest.mark.asyncio
async def test_tui_request_translates_vendor_send_failure_at_the_engine_seam() -> None:
    class _BrokenWebSocket:
        async def send(self, _payload: str) -> None:
            raise OSError("socket closed")

        async def close(self) -> None:
            return None

    process = HermesTuiProcess(url="ws://127.0.0.1:9119/api/ws?token=t")
    process._channel._ws = _BrokenWebSocket()
    process._channel._connected.set()

    with pytest.raises(EngineStreamDetached, match="Hermes backend transport detached"):
        await process.request("session.prompt", {"text": "hello"})

    assert isinstance(process._fatal, EngineStreamDetached)


def test_hermes_rebuilds_its_own_output_state_from_committed_frames() -> None:
    frames = [
        {"payload": {"type": "text-start", "id": "hermes-text:turn-1"}},
        {"payload": {"type": "text-delta", "id": "hermes-text:turn-1", "delta": "hi"}},
    ]

    checkpoint: EngineOutputCheckpoint = build_engine_output_checkpoint(
        frames,
        after_sequence=7,
    )

    assert checkpoint.after_sequence == 7
    assert checkpoint.committed_frames == tuple(frames)
    assert _open_hermes_output_blocks(checkpoint.committed_frames) == (
        "hermes-text:turn-1",
        None,
    )


def test_tui_message_delta_and_completion_translate_to_engine_events() -> None:
    delta = list(
        translate_tui_event(
            {
                "type": "message.delta",
                "session_id": "tui-1",
                "payload": {"text": "hello"},
            },
            turn_id="turn-1",
        )
    )
    complete = list(
        translate_tui_event(
            {
                "type": "message.complete",
                "session_id": "tui-1",
                "payload": {
                    "text": "hello",
                    "status": "complete",
                    "usage": {"input": 2, "output": 1, "total": 3},
                },
            },
            turn_id="turn-1",
        )
    )

    assert delta == [{"type": "text-delta", "id": "hermes-text:turn-1", "delta": "hello"}]
    assert complete == [
        {
            "type": "result",
            "finishReason": "stop",
            "__engine_terminal_reason": "complete",
            "usage": {"input": 2, "output": 1, "total": 3},
        }
    ]


def test_tui_unknown_terminal_status_fails_in_the_adapter() -> None:
    with pytest.raises(HermesTuiProtocolError, match="future-status"):
        list(
            translate_tui_event(
                {
                    "type": "message.complete",
                    "session_id": "tui-1",
                    "payload": {"status": "future-status"},
                },
                turn_id="turn-1",
            )
        )


def test_tui_subagent_events_project_one_session_owned_child_run() -> None:
    started = list(
        translate_tui_event(
            {
                "type": "subagent.start",
                "session_id": "tui-1",
                "payload": {
                    "subagent_id": "sa-0-child",
                    "parent_id": "sa-parent",
                    "goal": "Inspect the failing path",
                    "model": "openai/test-model",
                },
            },
            turn_id="turn-1",
        )
    )
    completed = list(
        translate_tui_event(
            {
                "type": "subagent.complete",
                "session_id": "tui-1",
                "payload": {
                    "subagent_id": "sa-0-child",
                    "parent_id": "sa-parent",
                    "goal": "Inspect the failing path",
                    "status": "completed",
                    "stop_reason": "finished",
                    "summary": "Found the cause",
                    "tool_count": 2,
                    "input_tokens": 11,
                    "output_tokens": 7,
                    "duration_seconds": 1.25,
                    "output_tail": [{"tool": "terminal", "preview": "ok", "is_error": False}],
                },
            },
            turn_id="turn-1",
        )
    )

    assert len(started) == 1
    assert started[0]["type"] == "data-subagent"
    assert started[0]["__engine_frame_scope"] == "session"
    assert started[0]["transient"] is True
    assert started[0]["data"] == {
        "kind": "lifecycle",
        "engineRef": "sa-0-child",
        "parentEngineRef": "sa-parent",
        "controlRef": "sa-0-child",
        "event": "opened",
        "engineEvent": "subagent.start",
        "operations": ["stop"],
        "description": "Inspect the failing path",
        "taskType": "delegate_task",
        "model": "openai/test-model",
        "engineKind": "assistant",
        "nativeState": {"subagent_id": "sa-0-child"},
    }

    assert [frame["data"]["kind"] for frame in completed] == [
        "lifecycle",
        "message",
    ]
    assert completed[0]["data"] == {
        "kind": "lifecycle",
        "engineRef": "sa-0-child",
        "parentEngineRef": "sa-parent",
        "controlRef": "sa-0-child",
        "event": "closed",
        "engineEvent": "subagent.complete",
        "engineStatus": "completed",
        "engineReason": "finished",
        "operations": [],
        "description": "Inspect the failing path",
        "taskType": "delegate_task",
        "summary": "Found the cause",
        "usage": {
            "input_tokens": 11,
            "output_tokens": 7,
            "tool_uses": 2,
            "duration_ms": 1250,
        },
        "engineKind": "assistant",
        "nativeState": {"subagent_id": "sa-0-child"},
    }
    assert completed[1]["data"]["engineRef"] == "sa-0-child"
    assert completed[1]["data"]["parentEngineRef"] == "sa-parent"
    assert completed[1]["data"]["content"] == [
        {
            "type": "tool_use",
            "id": "hermes:sa-0-child:tail:0",
            "name": "terminal",
            "input": {},
        },
        {
            "type": "tool_result",
            "tool_use_id": "hermes:sa-0-child:tail:0",
            "content": "ok",
            "is_error": False,
        },
        {"type": "text", "text": "Found the cause"},
    ]


@pytest.mark.asyncio
async def test_hermes_child_run_stop_uses_the_official_subagent_id_field() -> None:
    request = AsyncMock(return_value={"found": True})
    gateway = _fake_gateway(request)
    client = HermesTuiEngineClient(
        gateway=gateway,
        platform_session_id="session-1",
    )
    client._tui_session_id = "tui-1"
    client._subscription = SimpleNamespace(tui_session_id="tui-1")

    await client.stop_child_run("sa-0-child")

    request.assert_awaited_once_with(
        "subagent.interrupt",
        {"session_id": "tui-1", "subagent_id": "sa-0-child"},
    )
    assert (await client.get_capabilities()).supports_child_run_control is True


def _recovering_client(live_sessions: list[dict[str, str]]) -> tuple[HermesTuiEngineClient, list[str]]:
    """A client reattached to a backend whose ``session.active_list`` is given."""

    subscribed: list[str] = []

    async def request(method: str, params: dict[str, str]) -> dict[str, object]:
        if method == "session.resume":
            return {"session_id": "fresh001", "resumed": params["session_id"]}
        assert method == "session.active_list"
        return {"sessions": live_sessions}

    gateway = _fake_gateway(AsyncMock(side_effect=request))

    def subscribe(tui_session_id: str, after_offset: int = 0) -> _FakeSubscription:
        subscribed.append(tui_session_id)
        subscription = _FakeSubscription(tui_session_id, after_offset)
        # The rest of a run that began before the reattach: no start record.
        subscription.push(
            HermesTuiWireEvent(
                event={
                    "type": "message.complete",
                    "session_id": tui_session_id,
                    "payload": {"status": "complete"},
                },
                output_offset=after_offset + 1,
            )
        )
        return subscription

    gateway.subscribe = subscribe
    client = HermesTuiEngineClient(
        gateway=gateway,
        platform_session_id="session-1",
        resume_session_key="engine-session-1",
    )
    return client, subscribed


def _usage(percent: int) -> dict[str, object]:
    """``session.usage`` as Hermes emits it (``tui_gateway/server.py`` ``_get_usage``)."""

    return {
        "type": "session.usage",
        "session_id": "tui1",
        "payload": {"usage": {"context_used": percent * 1000, "context_max": 100_000, "context_percent": percent}},
    }


_COMPLETE = {"type": "message.complete", "session_id": "tui1", "payload": {"status": "complete"}}


def _compacting_client(monkeypatch: pytest.MonkeyPatch) -> HermesTuiEngineClient:
    from astrabox.core.service.orchestrator.engine import hermes_client

    monkeypatch.setattr(hermes_client, "_IDLE_COMPACT_QUIET_SECONDS", 0.0)
    gateway = _fake_gateway(AsyncMock(return_value={"status": "compressed"}))
    client = HermesTuiEngineClient(
        gateway=gateway, platform_session_id="session-1", resume_session_key="engine-session-1"
    )
    client._tui_session_id = "tui1"
    return client


@pytest.mark.asyncio
async def test_a_reply_that_leaves_the_context_40_percent_full_is_compressed_once_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _compacting_client(monkeypatch)

    client._observe_context(_usage(45))
    client._observe_context(_COMPLETE)
    assert client._compaction is not None
    await client._compaction

    client._gateway.request.assert_awaited_once()
    assert client._gateway.request.await_args.args == ("session.compress", {"session_id": "tui1"})


@pytest.mark.asyncio
async def test_no_compression_below_the_threshold_or_while_the_next_turn_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _compacting_client(monkeypatch)

    client._observe_context(_usage(39))
    client._observe_context(_COMPLETE)
    assert client._compaction is None

    # Usage crossed the threshold, but a message arrived before the quiet
    # period ended: the turn compresses itself if it must, not this task.
    client._observe_context(_usage(45))
    client._observe_context(_COMPLETE)
    client._submitting = True
    assert client._compaction is not None
    await client._compaction
    client._gateway.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_compressed_conversation_resumes_under_the_key_the_platform_holds() -> None:
    """Resume answers with the compression child; the conversation keeps its key.

    Hermes ends a session when it compresses it and continues in a child, and
    ``session.resume`` reports the child it followed the lineage to. Adopting
    that child id made every compressed conversation refuse its next message
    after a backend or host restart, as a different native conversation.
    """

    async def request(method: str, params: dict[str, str]) -> dict[str, object]:
        assert method == "session.resume"
        return {"session_id": "fresh001", "resumed": "20261006_compressed_child"}

    gateway = _fake_gateway(AsyncMock(side_effect=request))
    client = HermesTuiEngineClient(
        gateway=gateway,
        platform_session_id="session-1",
        resume_session_key="engine-session-1",
    )

    await client.bind_conversation(
        EngineConversationBinding(
            platform_session_id="session-1", engine_session_key="engine-session-1"
        )
    )

    assert client.engine_session_key == "engine-session-1"
    gateway.request.assert_awaited_once_with(
        "session.resume", {"session_id": "engine-session-1", "cols": 120}
    )


@pytest.mark.asyncio
async def test_a_recovered_turn_whose_session_the_backend_lost_ends_as_a_failure() -> None:
    """A restarted backend never finishes a turn whose session it lost.

    Recovery re-reads a turn by subscribing to its session on the backend the
    host reattached to. When that backend has restarted, the session is gone
    and no event will ever arrive; the conversation stayed PROCESSING for
    good. The backend's own list of live sessions says the session is gone,
    so the turn ends here as a failure that says why.
    """

    client, subscribed = _recovering_client(
        [{"id": "other123", "status": "idle"}]
    )

    emissions = [
        emission
        async for emission in client.iter_reconnected_turn_events(
            engine_turn_id=encode_turn_anchor(tui_session_id="lost0001", turn_id="turn-1"),
            output_checkpoint=EngineOutputCheckpoint(after_sequence=40),
        )
    ]

    assert subscribed == [], "nothing will arrive on a session the backend lost"
    assert len(emissions) == 1 and isinstance(emissions[0], TurnTerminal)
    terminal = emissions[0]
    assert terminal.finish_reason == "error"
    assert terminal.error is not None
    assert terminal.error["code"] == "HERMES_SESSION_LOST"
    assert "restart" in terminal.error["message"]

    # The conversation outlives the lost session: the next message re-enters
    # it by its durable key instead of addressing the id the backend forgot.
    await client._ensure_session()
    request = client._gateway.request
    request.assert_any_await(
        "session.resume", {"session_id": "engine-session-1", "cols": 120}
    )
    assert subscribed == ["fresh001"]


@pytest.mark.asyncio
async def test_a_recovered_turn_on_a_session_the_backend_holds_keeps_reading() -> None:
    client, subscribed = _recovering_client(
        [{"id": "live0001", "status": "working"}]
    )

    emissions = [
        emission
        async for emission in client.iter_reconnected_turn_events(
            engine_turn_id=encode_turn_anchor(tui_session_id="live0001", turn_id="turn-1"),
            output_checkpoint=EngineOutputCheckpoint(after_sequence=40),
        )
    ]

    assert subscribed == ["live0001"]
    assert isinstance(emissions[-1], TurnTerminal)
    assert emissions[-1].finish_reason == "stop"


def test_tui_tool_and_approval_events_keep_their_real_ids() -> None:
    tool_frames = list(
        translate_tui_event(
            {
                "type": "tool.start",
                "session_id": "tui-1",
                "payload": {
                    "tool_id": "tool-7",
                    "name": "terminal",
                    # A textual preview must not be presented as parsed args.
                    "context": "$ pwd",
                },
            },
            turn_id="turn-1",
        )
    )
    approval_frames = list(
        translate_tui_event(
            {
                "type": "approval.request",
                "session_id": "tui-1",
                "payload": {
                    "command": "rm -r build",
                    "description": "recursive delete",
                },
            },
            turn_id="turn-1",
            active_tool_id="tool-7",
        )
    )

    assert tool_frames == [
        {
            "type": "tool-input-start",
            "toolCallId": "tool-7",
            "toolName": "terminal",
            "dynamic": True,
        },
        {
            "type": "tool-input-available",
            "toolCallId": "tool-7",
            "toolName": "terminal",
            "input": {"preview": "$ pwd"},
            "dynamic": True,
        },
    ]
    assert approval_frames[0]["type"] == "interaction.request"
    assert approval_frames[0]["payload"]["tool_use_id"] == "tool-7"
    assert approval_frames[0]["payload"]["presentation"] == "tool_approval"
    assert approval_frames[0]["payload"]["raw_input"] == {"command": "rm -r build"}


def test_official_tui_reasoning_deltas_translate_without_stopping_the_turn() -> None:
    frames = list(
        translate_tui_event(
            {
                "type": "reasoning.delta",
                "session_id": "tui-1",
                "payload": {"text": "working it out"},
            },
            turn_id="turn-1",
        )
    )

    assert frames == [
        {
            "type": "reasoning-delta",
            "id": "hermes-reasoning:turn-1",
            "delta": "working it out",
        }
    ]


def test_thinking_delta_is_a_spinner_label_not_reasoning() -> None:
    """The vendor's two callbacks must not be merged into one stream.

    Hermes wires ``reasoning_callback`` to ``reasoning.delta`` and
    ``thinking_callback`` to ``thinking.delta``. Every call site of the second
    one in `agent/conversation_loop.py` passes either `f"{face} {verb}..."`
    before an API call or `""` to clear it — a spinner label for a terminal,
    never model content. Translating it as a delta is what put
    `٩(๑❛ᴗ❛๑)۶ cogitating...` at the head of a thinking block in the product.

    Pinned with the exact payload the vendor sends, so a future merge of the
    two events fails here rather than in a screenshot.
    """

    frames = list(
        translate_tui_event(
            {
                "type": "thinking.delta",
                "session_id": "tui-1",
                "payload": {"text": "٩(๑❛ᴗ❛๑)۶ cogitating..."},
            },
            turn_id="turn-1",
        )
    )

    assert not [f for f in frames if f.get("type") == "reasoning-delta"]
    # Preserved without being rendered, the same path `reasoning.available`
    # takes: the platform carries what the engine said even when it does not
    # translate it.
    assert [f.get("type") for f in frames] == ["data-raw-event"]


def test_the_spinner_clear_is_not_reasoning_either() -> None:
    """The same callback also fires with an empty string to erase the label."""

    frames = list(
        translate_tui_event(
            {
                "type": "thinking.delta",
                "session_id": "tui-1",
                "payload": {"text": ""},
            },
            turn_id="turn-1",
        )
    )

    assert not [f for f in frames if f.get("type") == "reasoning-delta"]


def test_reasoning_available_is_not_an_increment_of_the_reasoning_stream() -> None:
    """The vendor's snapshot must not be appended as a delta.

    Hermes emits ``reasoning.available`` from its tool-progress callback with a
    ``preview`` — what a terminal UI would display — and its own ACP adapter
    ignores it for that reason. Treated as a delta it put the finished ANSWER
    into the transcript a second time as a thinking block, because the stream
    layer opens a fresh reasoning segment for any reasoning delta that arrives
    after a text end.

    Pinned with the text that made it visible: a preview carrying the answer.
    """

    frames = list(
        translate_tui_event(
            {
                "type": "reasoning.available",
                "session_id": "tui-1",
                "payload": {"text": "I am Hermes Agent, built by Nous Research."},
            },
            turn_id="turn-1",
        )
    )

    assert not [f for f in frames if f.get("type") == "reasoning-delta"]
    # Not rendered, but not silently dropped either: it takes the same
    # informational path as any event this adapter does not translate.
    assert [f.get("type") for f in frames] == ["data-raw-event"]


def test_new_informational_tui_events_are_preserved_without_crashing_the_turn() -> None:
    frames = list(
        translate_tui_event(
            {
                "type": "future.telemetry",
                "session_id": "tui-1",
                "payload": {"detail": "added by a newer Hermes release"},
            },
            turn_id="turn-1",
        )
    )

    assert frames == [
        {
            "type": "data-raw-event",
            "data": {
                "event_type": "hermes.tui_gateway",
                "subtype": "future.telemetry",
                "raw": {
                    "type": "future.telemetry",
                    "session_id": "tui-1",
                    "payload": {"detail": "added by a newer Hermes release"},
                },
            },
        }
    ]
