"""Claude Code engine client + SDK message translator.

Two layers. The translator tests pin the wire contract: every SDK message
type and content block has an explicit mapping, and anything unregistered
hard-fails (UnknownWireEvent) instead of leaking half-rendered frames. The
client tests run the real stack below the engine seam — RunnerWsServer and
RunnerLink over localhost websockets, a fake SDK session emitting
dataclass messages the runner serializer stamps — and assert the AI SDK frame
stream the turn-service will consume: translated frames in order, a terminal
result per turn, cancelled decided by the client, and a dead link surfacing
as a loud error result rather than a hang.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from claude_agent_sdk import (
    AssistantMessage as NativeAssistantMessage,
    InMemorySessionStore,
    PermissionResultAllow,
    ResultMessage as NativeResultMessage,
    TextBlock as NativeTextBlock,
    ThinkingBlock as NativeThinkingBlock,
    ToolPermissionContext,
    ToolResultBlock as NativeToolResultBlock,
    ToolUseBlock as NativeToolUseBlock,
    UserMessage as NativeUserMessage,
    project_key_for_directory,
)

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.claude_code_client import (
    ClaudeCodeEngineClient,
)
from astrabox.core.service.orchestrator.engine.base import (
    EngineConversationBinding,
    EngineInputCommand,
)
from astrabox.core.service.orchestrator.engine.emissions import EngineEmission
from astrabox.core.service.orchestrator.engine.frame_translator import (
    UnknownWireEvent,
    translate_claude_sdk_message,
)
from astrabox.core.service.orchestrator.engine.interaction_contract import (
    build_pending_interaction_record,
    validate_interaction_contract,
)
from astrabox.core.service.orchestrator.engine.runner_link import RunnerLink as _RunnerLink
from astrabox.core.service.orchestrator.session_kernel.service_mixins.turn_dispatch import (
    TurnDispatchStreamingMixin,
)
from astrabox.core.service.orchestrator.sandbox_runner import (
    HostLink,
    RunnerSession,
    RunnerWsServer,
)


async def _accept_persistent_event(_frame: dict[str, Any]) -> None:
    pass


@pytest.mark.asyncio
async def test_child_store_reads_preserve_a_native_string_user_message() -> None:
    store = InMemorySessionStore()
    session_id = "f278429f-80dc-47a2-b28b-37e6ce037a81"
    agent_id = "ae2379d33dad4ce25"
    user_id = "5c8e2e27-9c19-4921-8d2c-bdb2d702f3ee"
    task = "Process CHILD_claude_code_9eaa622e9c18402f985ca2c441a6d05a.\n"
    await store.append(
        {
            "project_key": project_key_for_directory("/workspace"),
            "session_id": session_id,
            "subpath": f"subagents/agent-{agent_id}",
        },
        [
            {
                "type": "user", "uuid": user_id, "parentUuid": None,
                "sessionId": session_id, "isSidechain": True,
                "message": {"role": "user", "content": task},
            },
            {
                "type": "assistant", "uuid": "assistant-child", "parentUuid": user_id,
                "sessionId": session_id, "isSidechain": True,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "bash-held", "name": "Bash", "input": {"command": "sleep 90"}}],
                },
            },
        ],
    )
    client = ClaudeCodeEngineClient(
        AsyncMock(), session_id=session_id, transcript_store=store,
        workspace_dir="/workspace", resume_session_key=session_id,
    )
    frames = [fact.as_frame() for fact in await client.reconcile_child_resources()]
    assert [frame["data"]["role"] for frame in frames] == ["user", "assistant"]
    assert all(frame["data"]["engineRef"] == agent_id for frame in frames)
    assert frames[0]["id"] == f"subagent:msg:{user_id}"
    assert frames[0]["data"]["content"] == [{"type": "text", "text": task}]
    assert await client.reconcile_child_resources() == []


class RunnerLink(_RunnerLink):
    def __init__(self, uri: str, **kwargs: Any) -> None:
        kwargs.setdefault("persistent_event_handler", _accept_persistent_event)
        super().__init__(uri, **kwargs)


async def _begin_delivery(
    client: ClaudeCodeEngineClient,
    user_message: str,
) -> Any:
    """Drive tests through the same durable FIFO surface as production."""

    command = EngineInputCommand(
        command_id=uuid.uuid4().hex,
        session_id=client._session_id,
        sequence=client._delivery_sequence + 1,
        input_id=str(uuid.uuid4()),
        content=user_message,
    )
    await client.deliver(command)
    return await client.begin_delivery(command)


def _pending_record(frame: dict[str, Any]) -> dict[str, Any]:
    """The durable record the platform builds from this adapter declaration.

    Answering goes through the record, not the frame, so the tests assemble it
    the way the seam does — validation included, since a declaration that
    cannot be validated is one no answer could be checked against.
    """
    payload = dict(frame["payload"])
    tool_use_id = payload.pop("tool_use_id", None)
    validate_interaction_contract(payload)
    return build_pending_interaction_record(
        contract=payload,
        session_id="sess-1",
        turn_id="turn-1",
        interaction_id=str(frame["interactionId"]),
        tool_call_id=str(tool_use_id or "") or None,
    )


# --- SDK-shaped fakes (names matter: the serializer stamps __sdk_type) ------


@dataclasses.dataclass
class TextBlock:
    text: str


@dataclasses.dataclass
class ThinkingBlock:
    thinking: str
    signature: str = ""


@dataclasses.dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]


@dataclasses.dataclass
class ToolResultBlock:
    tool_use_id: str
    content: Any = None
    is_error: bool | None = None


@dataclasses.dataclass
class AssistantMessage:
    content: list[Any]
    model: str = "claude-test"


@dataclasses.dataclass
class UserMessage:
    content: Any
    uuid: str | None = None
    parent_tool_use_id: str | None = None
    tool_use_result: dict[str, Any] | None = None


@dataclasses.dataclass
class ResultMessage:
    subtype: str = "success"
    session_id: str = "sdk-sess"
    result: str | None = None
    usage: dict[str, Any] | None = None
    deferred_tool_use: dict[str, Any] | None = None


# --- translator unit tests ---------------------------------------------------


def _msg(obj: Any) -> dict[str, Any]:
    from astrabox.core.service.orchestrator.sandbox_runner import _jsonable

    out = _jsonable(obj)
    assert isinstance(out, dict)
    return out


def test_assistant_blocks_translate_in_order() -> None:
    message = _msg(
        AssistantMessage(
            content=[
                ThinkingBlock(thinking="let me see"),
                TextBlock(text="Hello"),
                ToolUseBlock(id="toolu_1", name="Bash", input={"command": "ls"}),
            ]
        )
    )
    frames = list(translate_claude_sdk_message(message, envelope_seq=7))
    kinds = [f["type"] for f in frames]
    # One SDK message is one step of the turn, and the frames say where it
    # begins and ends rather than leaving a flat run of blocks.
    assert kinds == [
        "start-step",
        "reasoning-start", "reasoning-delta", "reasoning-end",
        "text-start", "text-delta", "text-end",
        "tool-input-start", "tool-input-available",
        "finish-step",
    ]
    # By type, not by position: a frame's identity is what it says, and an
    # index moves whenever the sequence around it grows a boundary.
    text_delta = next(f for f in frames if f["type"] == "text-delta")
    assert text_delta == {"type": "text-delta", "id": "claude-text:7:1", "delta": "Hello"}
    tool_input = next(f for f in frames if f["type"] == "tool-input-available")
    assert tool_input["toolCallId"] == "toolu_1"
    assert tool_input["input"] == {"command": "ls"}


def test_tool_result_maps_output_and_error() -> None:
    ok = _msg(UserMessage(content=[ToolResultBlock(tool_use_id="toolu_1", content="done")]))
    (frame,) = list(translate_claude_sdk_message(ok, envelope_seq=1))
    assert frame == {
        "type": "tool-output-available", "toolCallId": "toolu_1", "output": "done",
    }
    bad = _msg(
        UserMessage(content=[ToolResultBlock(tool_use_id="toolu_1", content="boom", is_error=True)])
    )
    (frame,) = list(translate_claude_sdk_message(bad, envelope_seq=1))
    assert frame["type"] == "tool-output-error"
    assert frame["errorText"] == "boom"


def test_result_message_maps_success_and_error() -> None:
    ok = list(
        translate_claude_sdk_message(_msg(ResultMessage(subtype="success")), envelope_seq=1)
    )
    assert ok == [{"type": "result", "finishReason": "stop"}]
    err = list(
        translate_claude_sdk_message(
            _msg(ResultMessage(subtype="error_max_turns", result="ran out")), envelope_seq=1
        )
    )
    assert err[0]["finishReason"] == "error"
    assert err[0]["error"] == {"code": "error_max_turns", "message": "ran out"}


def test_unknown_message_and_block_types_hard_fail() -> None:
    with pytest.raises(UnknownWireEvent, match="do not relax the gate"):
        list(translate_claude_sdk_message({"__sdk_type": "BrandNewMessage"}, envelope_seq=1))

    @dataclasses.dataclass
    class NovelBlock:
        payload: str

    message = _msg(AssistantMessage(content=[NovelBlock(payload="x")]))
    with pytest.raises(UnknownWireEvent, match="do not relax the gate"):
        list(translate_claude_sdk_message(message, envelope_seq=1))


# --- engine client over the real wire ---------------------------------------


class QueueSdkSession:
    def __init__(self) -> None:
        self.queries: list[Any] = []
        self.interrupted = 0
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self.consume_prompt: Any = None
        self.store_bindings: list[tuple[str, dict[str, Any] | None]] = []

    def emit(self, message: Any) -> None:
        self._queue.put_nowait(_native_message(message))

    def bind_store(self, session_id: str, store: dict[str, Any] | None) -> None:
        self.store_bindings.append((session_id, dict(store) if store else None))

    async def connect(self) -> None:
        assert self.store_bindings, "the runner must bind transcript custody before SDK connect"

    async def query(self, prompt: Any, session_id: str = "default") -> None:
        _ = session_id
        items = [item async for item in prompt]
        self.queries.extend(items)
        if self.consume_prompt is not None:
            for item in items:
                await self.consume_prompt(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": item["message"]["content"],
                    },
                    None,
                    None,
                )

    async def receive_messages(self):  # noqa: ANN201 — async generator protocol
        while True:
            yield await self._queue.get()

    async def interrupt(self) -> None:
        self.interrupted += 1

    async def stop_task(self, task_id: str) -> None:
        self.stopped_tasks = getattr(self, "stopped_tasks", [])
        self.stopped_tasks.append(task_id)

    async def set_permission_mode(self, mode: str) -> None:
        self.permission_modes = getattr(self, "permission_modes", [])
        self.permission_modes.append(mode)

    async def get_server_info(self) -> dict:
        return {"commands": [{"name": "compact"}], "output_style": "default"}

    async def disconnect(self) -> None:
        pass


def _native_block(block: Any) -> Any:
    if isinstance(block, TextBlock):
        return NativeTextBlock(text=block.text)
    if isinstance(block, ThinkingBlock):
        return NativeThinkingBlock(
            thinking=block.thinking,
            signature=block.signature,
        )
    if isinstance(block, ToolUseBlock):
        return NativeToolUseBlock(id=block.id, name=block.name, input=block.input)
    if isinstance(block, ToolResultBlock):
        return NativeToolResultBlock(
            tool_use_id=block.tool_use_id,
            content=block.content,
            is_error=block.is_error,
        )
    return block


def _native_message(message: Any) -> Any:
    """The real runner receives the pinned SDK's concrete dataclasses.

    Translator unit tests above deliberately use name-matched wire fakes. The
    websocket integration fixture must not: the active snapshot is an SDK
    adapter and correctly rejects objects that merely borrow vendor names.
    """

    if isinstance(message, AssistantMessage):
        return NativeAssistantMessage(
            content=[_native_block(block) for block in message.content],
            model=message.model,
        )
    if isinstance(message, UserMessage):
        content = message.content
        if isinstance(content, list):
            content = [_native_block(block) for block in content]
        return NativeUserMessage(
            content=content,
            uuid=message.uuid,
            parent_tool_use_id=message.parent_tool_use_id,
            tool_use_result=message.tool_use_result,
        )
    if isinstance(message, ResultMessage):
        return NativeResultMessage(
            subtype=message.subtype,
            duration_ms=1,
            duration_api_ms=1,
            is_error=message.subtype != "success",
            num_turns=1,
            session_id=message.session_id,
            result=message.result,
            usage=message.usage,
            deferred_tool_use=message.deferred_tool_use,
        )
    return message


@pytest.fixture
async def stack():
    sdk = QueueSdkSession()

    def factory(opening: dict[str, Any], link: HostLink) -> RunnerSession:
        session = RunnerSession(
            session_id=str(opening["slot_id"]),
            link=link,
            client_factory=lambda broker: sdk,
            activation_callback=sdk.bind_store,
            interaction_wait_s=5.0,
        )
        sdk.consume_prompt = session.on_user_prompt_submit
        return session

    server = RunnerWsServer(host="127.0.0.1", port=0, session_factory=factory)
    await server.start()
    link = RunnerLink(f"ws://127.0.0.1:{server.port}/")
    await link.__aenter__()
    await link.configure("sess-1", store={"base_url": "http://transcript.invalid"})
    assert sdk.store_bindings == [("sess-1", {"base_url": "http://transcript.invalid"})]
    client = ClaudeCodeEngineClient(
        link, session_id="sess-1", transcript_store=AsyncMock(), workspace_dir="/workspace"
    )
    try:
        yield server, sdk, client
    finally:
        await client.close()
        await server.stop()


async def _next_event_type(events: Any, event_type: str) -> dict[str, Any]:
    async for event in events:
        if event.get("type") == event_type:
            return event
    raise AssertionError(f"engine stream ended before {event_type!r}")


async def test_turn_streams_translated_frames_and_terminates_on_result(stack) -> None:
    server, sdk, client = stack
    receipt = await _begin_delivery(client, "list files")
    assert sdk.queries[0]["message"]["content"] == "list files"

    sdk.emit(AssistantMessage(content=[TextBlock(text="Sure.")]))
    sdk.emit(
        AssistantMessage(
            content=[ToolUseBlock(id="toolu_9", name="Bash", input={"command": "ls"})]
        )
    )
    sdk.emit(UserMessage(content=[ToolResultBlock(tool_use_id="toolu_9", content="a.txt")]))
    sdk.emit(ResultMessage(subtype="success", usage={"output_tokens": 5}))

    frames = [f async for f in client.iter_turn_events(receipt)]
    assert all(isinstance(frame, EngineEmission) for frame in frames)
    # Raw SDK messages never cross the engine seam. The adapter-owned
    # translator is the sole producer of live UI frames.
    kinds = [f["type"] for f in frames]
    # Two SDK messages answer this input — the text, then the tool call — and
    # each is enclosed by its own step boundary. The tool's OUTPUT arrives on
    # the following UserMessage, outside either step, which is where the engine
    # actually puts it.
    assert kinds == [
        "data-input-consumed",
        "start-step", "text-start", "text-delta", "text-end", "finish-step",
        "start-step", "tool-input-start", "tool-input-available", "finish-step",
        "tool-output-available",
        "data-result",
        "result",
    ]
    assert frames[-1] == {
        "type": "result",
        "finishReason": "stop",
        "usage": {"output_tokens": 5},
        "num_turns": 1,
    }
    assert client.engine_session_key == "sdk-sess"


async def test_background_manifest_crosses_as_neutral_ids_not_vendor_messages(
    stack,
) -> None:
    _server, sdk, client = stack
    receipt = await _begin_delivery(client, "launch a background agent")
    sdk.emit(
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="call_00_bg",
                    content="Async agent launched.",
                )
            ],
            tool_use_result={
                "status": "async_launched",
                "agentId": "agent-session-bg",
                "isAsync": True,
            },
        )
    )
    sdk.emit(ResultMessage(subtype="success"))

    frames = [frame async for frame in client.iter_turn_events(receipt)]
    manifest_frames = [
        frame for frame in frames if frame["type"] == "background-tasks-opened"
    ]
    assert manifest_frames == [
        {
            "type": "background-tasks-opened",
            "manifest": {
                "transcript_refs": ["agent-session-bg"],
                "engine_refs": ["agent-session-bg"],
                "transcript_to_engine_ref": {
                    "agent-session-bg": "agent-session-bg",
                },
                "control_to_engine_ref": {
                    "agent-session-bg": "agent-session-bg",
                },
                "activation_to_engine_ref": {
                    "call_00_bg": "agent-session-bg",
                },
            },
        }
    ]
    assert frames.index(manifest_frames[0]) < next(
        index for index, frame in enumerate(frames) if frame["type"] == "result"
    )
    assert all("message" not in frame for frame in frames)


async def test_fifo_prompts_the_next_root_after_the_current_result(stack) -> None:
    _server, sdk, client = stack
    first = await _begin_delivery(client, "first")
    second_input_id = "00000000-0000-0000-0000-000000000002"

    await client.deliver(
        EngineInputCommand(
            command_id="command-2",
            session_id="sess-1",
            sequence=2,
            input_id=second_input_id,
            content="second",
        )
    )

    assert [item["message"]["content"] for item in sdk.queries] == ["first"]

    async def collect() -> list[EngineEmission]:
        return [frame async for frame in client.iter_turn_events(first)]

    stream = asyncio.create_task(collect())
    sdk.emit(AssistantMessage(content=[TextBlock(text="FIRST_DONE")]))
    sdk.emit(ResultMessage(subtype="success"))

    for _ in range(20):
        if len(sdk.queries) == 2:
            break
        await asyncio.sleep(0)
    assert [item["message"]["content"] for item in sdk.queries] == [
        "first",
        "second",
    ]

    sdk.emit(AssistantMessage(content=[TextBlock(text="SECOND_DONE")]))
    sdk.emit(ResultMessage(subtype="success"))
    frames = await asyncio.wait_for(stream, timeout=1.0)

    consumed = [
        frame["data"]
        for frame in frames
        if frame.get("type") == "data-input-consumed"
    ]
    assert [item["content"] for item in consumed] == ["first", "second"]
    assert consumed[1]["inputId"] == second_input_id
    visible = "".join(
        str(frame.get("delta") or "")
        for frame in frames
        if frame.get("type") == "text-delta"
    )
    assert visible.index("FIRST_DONE") < visible.index("SECOND_DONE")
    assert [frame["type"] for frame in frames].count("response-result") == 1
    assert [frame["type"] for frame in frames].count("result") == 1


async def test_result_keeps_stream_open_until_queued_root_is_consumed(stack) -> None:
    server, sdk, client = stack
    consume_prompt = sdk.consume_prompt
    assert consume_prompt is not None

    async def consume_only_first(
        hook_input: dict[str, Any],
        tool_use_id: str | None,
        context: Any,
    ) -> dict[str, Any]:
        if hook_input["prompt"] != "first":
            return {}
        return await consume_prompt(hook_input, tool_use_id, context)

    sdk.consume_prompt = consume_only_first
    first = await _begin_delivery(client, "first")
    second_input_id = "00000000-0000-0000-0000-000000000003"
    await client.deliver(
        EngineInputCommand(
            command_id="command-3",
            session_id="sess-1",
            sequence=3,
            input_id=second_input_id,
            content="second",
        )
    )

    events = client.iter_turn_events(first)
    sdk.emit(ResultMessage(subtype="success"))
    boundary = await _next_event_type(events, "response-result")
    assert boundary["type"] == "response-result"

    assert server.session is not None
    await server.session.on_user_prompt_submit(
        {"hook_event_name": "UserPromptSubmit", "prompt": "second"},
        None,
        None,
    )
    sdk.emit(ResultMessage(subtype="success"))
    tail = [frame async for frame in events]
    assert any(
        frame.get("type") == "data-input-consumed"
        and frame.get("data", {}).get("inputId") == second_input_id
        for frame in tail
    )
    assert tail[-1]["type"] == "result"


async def test_interaction_surfaces_and_an_approval_routes_allow(stack) -> None:
    server, sdk, client = stack
    receipt = await _begin_delivery(client, "delete it")
    assert server.session is not None
    gate = asyncio.create_task(
        server.session.broker.pre_tool_use(
            {"tool_name": "Bash", "tool_input": {"command": "rm x"}}, "t1", None
        )
    )

    events = client.iter_turn_events(receipt)
    frame = await _next_event_type(events, "interaction.request")
    assert frame["type"] == "interaction.request"
    # The native name rides verbatim; the presentation is the structure the
    # user answers through, and an ungated tool has no other flow than an
    # approval over its call.
    assert frame["payload"]["tool_name"] == "Bash"
    assert frame["payload"]["presentation"] == "tool_approval"
    assert frame["payload"]["raw_input"] == {"command": "rm x"}
    # The gate's tool id survives the wire: it is what binds the pending
    # interaction to the tool_use block, and it is not the interaction id.
    assert frame["payload"]["tool_use_id"] == "t1"
    assert frame["interactionId"] != "t1"

    # The id comes from the durable record the caller is holding — NOT from
    # whatever this client happens to remember. A reattached client remembers
    # nothing.
    assert (
        await client.submit_interaction_response(
            receipt,
            pending=_pending_record(frame),
            response={"decision": "approve"},
        )
        is True
    )
    out = (await gate)["hookSpecificOutput"]
    assert out["permissionDecision"] == "allow"

    sdk.emit(ResultMessage(subtype="success"))
    tail = [f async for f in events]
    assert tail[-1]["finishReason"] == "stop"


async def test_answer_continuation_keeps_the_observed_input_boundary(stack) -> None:
    """A parked turn resumes after its root input, not before it.

    The interaction closes one host-side stream segment.  The continuation
    opens a new iterator over the same engine receipt, so the root-input
    boundary observed by the first segment must survive on that receipt.
    Otherwise every post-answer frame, including the terminal Result, is
    mistaken for replay from before the turn and silently discarded.
    """
    server, sdk, client = stack
    receipt = await _begin_delivery(client, "write the file")
    assert server.session is not None
    gate = asyncio.create_task(
        server.session.broker.pre_tool_use(
            {"tool_name": "Write", "tool_input": {"file_path": "x"}},
            "write-1",
            None,
        )
    )

    first_segment = client.iter_turn_events(receipt)
    interaction = await _next_event_type(first_segment, "interaction.request")
    await first_segment.aclose()
    assert await client.submit_interaction_response(
        receipt,
        pending=_pending_record(interaction),
        response={"decision": "reject"},
    )
    await gate

    sdk.emit(AssistantMessage(content=[TextBlock(text="The write was cancelled.")]))
    sdk.emit(ResultMessage(subtype="success", result="The write was cancelled."))

    async def collect_continuation() -> list[dict[str, Any]]:
        return [frame async for frame in client.iter_turn_events(receipt)]

    continuation = await asyncio.wait_for(collect_continuation(), timeout=0.5)
    assert any(
        frame.get("type") == "text-delta"
        and frame.get("delta") == "The write was cancelled."
        for frame in continuation
    )
    assert continuation[-1]["finishReason"] == "stop"


async def test_a_form_answer_reaches_the_sdk_as_the_tools_effective_input(stack) -> None:
    # AskUserQuestion answers ride inside the tool's effective input; the
    # protocol's updated_input is how they reach the SDK control callback.
    # The client derives that payload from the durable record — the caller
    # sends the user's answers, never a hand-built SDK input.
    server, sdk, client = stack
    receipt = await _begin_delivery(client, "ask me things")
    assert server.session is not None
    raw_questions = [
        {
            "header": "Database",
            "question": "Which db?",
            "multiSelect": False,
            "options": [{"label": "sqlite"}, {"label": "postgres"}],
        }
    ]
    callback = asyncio.create_task(
        server.session.broker.can_use_tool(
            "AskUserQuestion",
            {"questions": raw_questions},
            ToolPermissionContext(tool_use_id="t2"),
        )
    )
    events = client.iter_turn_events(receipt)
    frame = await _next_event_type(events, "interaction.request")
    assert frame["type"] == "interaction.request"
    assert frame["payload"]["presentation"] == "form"
    pending = _pending_record(frame)
    # The SDK keys its answer map by the question text, so the record carries
    # that native key next to the platform id the browser answers by.
    assert pending["questions"][0]["native_answer_key"] == "Which db?"
    assert await client.submit_interaction_response(
        receipt,
        pending=pending,
        response={
            "answers": [
                {"question_id": pending["questions"][0]["id"], "option_label": "sqlite"}
            ]
        },
    )
    result = await callback
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input == {
        "questions": raw_questions,
        "answers": {"Which db?": "sqlite"},
    }

    sdk.emit(ResultMessage(subtype="success"))
    tail = [f async for f in events]
    assert tail[-1]["finishReason"] == "stop"


@pytest.mark.parametrize(
    ("tool_name", "answer"),
    [
        ("Bash", {"decision": "approve"}),
        (
            "AskUserQuestion",
            {"answers": [{"question_id": "q-1", "option_label": "DIRECT"}]},
        ),
    ],
    ids=("tool-approval", "form"),
)
async def test_runner_rejection_does_not_settle_the_interaction(
    stack, tool_name: str, answer: dict[str, object]
) -> None:
    _server, _sdk, client = stack
    receipt = await _begin_delivery(client, "answer an expired gate")
    runtime = SimpleNamespace(
        engine_client=client,
        engine_kind="claude_code",
        engine_manifest=await client.get_capabilities(),
        conversation_bound=True,
    )
    service = TurnDispatchStreamingMixin()
    service._turn_service = SimpleNamespace(
        acquire_engine_control_runtime=AsyncMock(return_value=runtime)
    )
    service._sessions_repo = AsyncMock()
    service._append_command_accepted = AsyncMock()
    interaction: dict[str, Any] = {
        "interaction_id": "never-issued",
        "engine_turn_id": receipt.engine_turn_id,
        "tool_name": tool_name,
        "presentation": "tool_approval",
        "raw_input": {"command": "ls"},
    }
    if tool_name == "AskUserQuestion":
        interaction["presentation"] = "form"
        interaction["questions"] = [
            {
                "id": "q-1",
                "header": "Mode",
                "question": "Which mode?",
                "native_answer_key": "Which mode?",
                "allow_free_text": True,
                "allow_empty_text": False,
            }
        ]
        interaction["raw_input"] = {
            "questions": [
                {
                    "header": "Mode",
                    "question": "Which mode?",
                    "multiSelect": False,
                }
            ]
        }

    with pytest.raises(APIError) as rejected:
        await service._answer_via_engine_client(
            session={"sandbox_id": "box-1", "user_id": "owner"},
            session_id="sess-1",
            interaction=interaction,
            interaction_id="never-issued",
            turn_id=receipt.engine_turn_id,
            answer=answer,
        )

    assert rejected.value.code == "INTERACTION_EXPIRED"
    service._turn_service.acquire_engine_control_runtime.assert_awaited_once_with(
        {"sandbox_id": "box-1", "user_id": "owner"},
        operation="answer interaction",
    )
    service._sessions_repo.update_session.assert_not_awaited()
    service._append_command_accepted.assert_not_awaited()


async def test_cancel_turn_maps_terminal_to_cancelled(stack) -> None:
    server, sdk, client = stack
    receipt = await _begin_delivery(client, "long task")
    assert await client.cancel_turn(receipt) is True
    for _ in range(50):
        if sdk.interrupted:
            break
        await asyncio.sleep(0.01)
    assert sdk.interrupted == 1

    # The SDK reports an interrupted run as an error subtype; only the client
    # knows the interrupt was requested, so IT owns the cancelled mapping.
    sdk.emit(ResultMessage(subtype="error_during_execution", result="interrupted"))
    frames = [f async for f in client.iter_turn_events(receipt)]
    assert frames[-1]["finishReason"] == "cancelled"
    assert "error" not in frames[-1]


async def test_interrupt_active_turn_without_receipt_maps_to_cancelled(stack) -> None:
    # The platform's interrupt command arrives from another worker and cannot
    # present a receipt; the session-level entry must reach the runner and
    # the turn must still settle as cancelled, not error.
    server, sdk, client = stack
    receipt = await _begin_delivery(client, "long task")
    assert await client.interrupt_active_turn() is True
    for _ in range(50):
        if sdk.interrupted:
            break
        await asyncio.sleep(0.01)
    assert sdk.interrupted == 1

    sdk.emit(ResultMessage(subtype="error_during_execution", result="interrupted"))
    frames = [f async for f in client.iter_turn_events(receipt)]
    assert frames[-1]["finishReason"] == "cancelled"
    assert "error" not in frames[-1]

    # The turn settled — a late interrupt is a no-op, never a runner call.
    assert await client.interrupt_active_turn() is False


async def test_interrupt_active_turn_settles_when_sdk_emits_no_result(stack) -> None:
    """An accepted interrupt is terminal even when the SDK stream stays quiet.

    Claude Code can finish writing its local transcript just as an interrupt
    arrives, then acknowledge the interrupt without emitting another
    ResultMessage.  The runner link remains healthy in that race, so EOF cannot
    be used as the cancellation signal.
    """
    server, sdk, client = stack
    receipt = await _begin_delivery(client, "long task")

    async def collect() -> list[dict[str, Any]]:
        return [frame async for frame in client.iter_turn_events(receipt)]

    stream = asyncio.create_task(collect())
    await asyncio.sleep(0)
    assert await client.interrupt_active_turn() is True

    frames = await asyncio.wait_for(stream, timeout=0.5)
    assert sdk.interrupted == 1
    assert frames[-1] == {"type": "result", "finishReason": "cancelled"}

    # A late vendor Result belongs to the cancelled command.  It must not
    # terminate the next root input, and the resident SDK session must remain
    # usable after the platform control terminal.
    assert server.session is not None
    previous_sequence = server.session.sender.last_seq
    sdk.emit(ResultMessage(subtype="error_during_execution", result="interrupted"))
    for _ in range(50):
        if server.session.sender.last_seq > previous_sequence:
            break
        await asyncio.sleep(0.01)

    next_receipt = await _begin_delivery(client, "continue after stop")
    sdk.emit(AssistantMessage(content=[TextBlock(text="ready again")]))
    sdk.emit(ResultMessage(subtype="success", result="ready again"))
    next_frames = [
        frame async for frame in client.iter_turn_events(next_receipt)
    ]
    assert any(
        frame.get("type") == "text-delta"
        and frame.get("delta") == "ready again"
        for frame in next_frames
    )
    assert next_frames[-1]["finishReason"] == "stop"


async def test_interrupt_before_prompt_boundary_blocks_the_queued_input(stack) -> None:
    """A control acknowledgement cannot let an unconsumed prompt lose FIFO ownership."""

    server, sdk, client = stack
    sdk.consume_prompt = None
    receipt = await _begin_delivery(client, "do not run this prompt")

    async def collect() -> list[EngineEmission]:
        return [frame async for frame in client.iter_turn_events(receipt)]

    stream = asyncio.create_task(collect())
    await asyncio.sleep(0)
    assert await client.interrupt_active_turn() is True
    for _ in range(50):
        if sdk.interrupted:
            break
        await asyncio.sleep(0.01)
    assert sdk.interrupted == 1
    assert not stream.done(), (
        "interrupt acknowledgement alone must not terminal an input still queued in the CLI"
    )

    assert server.session is not None
    hook_result = await server.session.on_user_prompt_submit(
        {
            "hook_event_name": "UserPromptSubmit",
            "prompt": "do not run this prompt",
        },
        None,
        None,
    )
    assert hook_result == {
        "decision": "block",
        "reason": "user interrupted the session",
    }

    sdk.emit(ResultMessage(subtype="error_during_execution", result=""))
    frames = await asyncio.wait_for(stream, timeout=0.5)
    assert frames[0]["type"] == "data-input-consumed"
    assert frames[0]["data"]["inputId"] == receipt.input_id
    assert frames[-1]["type"] == "result"
    assert frames[-1]["finishReason"] == "cancelled"


async def test_interrupt_hands_an_already_queued_root_to_its_own_result(stack) -> None:
    """The interrupt terminal covers the active root, not its FIFO successor."""

    server, sdk, client = stack
    receipt = await _begin_delivery(client, "long task")
    consume_prompt = sdk.consume_prompt
    assert consume_prompt is not None

    async def hold_queued_root(
        hook_input: dict[str, Any],
        tool_use_id: str | None,
        context: Any,
    ) -> dict[str, Any]:
        if hook_input["prompt"] == "continue after stop":
            return {}
        return await consume_prompt(hook_input, tool_use_id, context)

    sdk.consume_prompt = hold_queued_root
    successor_input_id = "00000000-0000-0000-0000-000000000004"
    await client.deliver(
        EngineInputCommand(
            command_id="command-after-stop",
            session_id="sess-1",
            sequence=4,
            input_id=successor_input_id,
            content="continue after stop",
        )
    )

    async def collect() -> list[dict[str, Any]]:
        return [frame async for frame in client.iter_turn_events(receipt)]

    stream = asyncio.create_task(collect())
    await asyncio.sleep(0)
    assert await client.interrupt_active_turn() is True
    await asyncio.sleep(0.05)
    assert not stream.done(), "the queued successor must keep the engine stream open"

    assert server.session is not None
    await server.session.on_user_prompt_submit(
        {"hook_event_name": "UserPromptSubmit", "prompt": "continue after stop"},
        None,
        None,
    )
    sdk.emit(AssistantMessage(content=[TextBlock(text="continued")]))
    sdk.emit(ResultMessage(subtype="success", result="continued"))

    frames = await asyncio.wait_for(stream, timeout=0.5)
    assert any(
        frame.get("type") == "data-input-consumed"
        and frame.get("data", {}).get("inputId") == successor_input_id
        for frame in frames
    )
    assert any(
        frame.get("type") == "text-delta" and frame.get("delta") == "continued"
        for frame in frames
    )
    assert frames[-1] == {
        "type": "result",
        "finishReason": "stop",
        "num_turns": 1,
    }
    assert client.engine_session_key == "sdk-sess"


async def test_stop_child_run_reaches_the_sdk(stack) -> None:
    # The kernel's child-run control op rides the runner link and
    # lands on the SDK's stop_task — the CLI settles the subagent through its
    # own task_notification, so nothing terminal is expected on this channel.
    server, sdk, client = stack
    await _begin_delivery(client, "spawn background work")
    await client.stop_child_run("task-abc")
    for _ in range(50):
        if getattr(sdk, "stopped_tasks", None):
            break
        await asyncio.sleep(0.01)
    assert sdk.stopped_tasks == ["task-abc"]


async def test_set_permission_mode_reaches_the_sdk(stack) -> None:
    # The permission lifecycle's reconcile (mid-session switches and the
    # post-reattach preflight) rides the runner link to the SDK's
    # set_permission_mode.
    server, sdk, client = stack
    await client.set_permission_mode("bypassPermissions")
    for _ in range(50):
        if getattr(sdk, "permission_modes", None):
            break
        await asyncio.sleep(0.01)
    assert sdk.permission_modes == ["bypassPermissions"]


async def test_get_server_info_round_trips_the_init_snapshot(stack) -> None:
    # The startup metadata fetch rides the envelope as a request/response:
    # the SDK's initialize snapshot (slash commands / skills) comes back as
    # a plain dict, available as soon as the runner connected.
    server, sdk, client = stack
    info = await client.get_server_info()
    assert info == {"commands": [{"name": "compact"}], "output_style": "default"}


async def test_dead_link_raises_detached_instead_of_authoring_a_terminal(stack) -> None:
    # A closed link is a transport statement, not a turn verdict: the box may
    # still be running the turn. Synthesizing an error here would let a worker
    # turn the loss of its own link into a durable `turn.failed` result. The
    # typed exception requires every consumer to hand the turn to recovery
    # instead of treating the transport event as a terminal outcome.
    from astrabox.core.service.orchestrator.engine.base import EngineStreamDetached

    server, sdk, client = stack
    receipt = await _begin_delivery(client, "hello")
    events = client.iter_turn_events(receipt)
    await server.stop()  # box side goes away mid-turn
    with pytest.raises(EngineStreamDetached):
        async for _frame in events:
            pass


async def test_durable_binding_requires_the_configured_native_resume_key(stack) -> None:
    _server, _sdk, client = stack
    with pytest.raises(RuntimeError, match="resume key does not match"):
        await client.bind_conversation(
            EngineConversationBinding(
                platform_session_id="sess-1",
                engine_session_key="sdk-existing-session",
            )
        )


# --- reattach: attach-first, configure(resume) fallback ----------------------


async def test_reattach_attaches_to_a_live_runner_session(
    stack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Host restart against a box whose runner still holds the session: the
    # attach-first path adopts it without reconfiguring (the SDK session in
    # the box is the conversation truth; a configure would be refused).
    from astrabox.core.service.orchestrator.engine import claude_code_runtime

    monkeypatch.setattr(
        claude_code_runtime,
        "_runner_event_persister",
        lambda _session_id, **_kwargs: _accept_persistent_event,
    )

    server, sdk, client = stack
    engine_client, how = await claude_code_runtime._attach_runner_engine_client(
        f"ws://127.0.0.1:{server.port}/",
        session_id="sess-1",
        workspace_dir="/workspace",
        sdk_options={},
        transcript_store={"base_url": "http://host/api"},
        sandbox_death_notice={"url": "http://host/death"},
        event_sink=AsyncMock(),
    )
    assert how == "attached"
    receipt = await _begin_delivery(engine_client, "after restart")
    assert sdk.queries[-1]["message"]["content"] == "after restart"
    sdk.emit(ResultMessage(subtype="success"))
    frames = [f async for f in engine_client.iter_turn_events(receipt)]
    assert frames[-1]["finishReason"] == "stop"
    await engine_client.close()


async def test_consumed_input_after_reattach_still_waits_for_its_stream_boundary(
    stack,
) -> None:
    """Durable consumption must not correlate a fresh host to an old Result."""

    server, sdk, original_client = stack
    original_receipt = await _begin_delivery(original_client, "before restart")
    sdk.emit(AssistantMessage(content=[TextBlock(text="old reply")]))
    sdk.emit(ResultMessage(subtype="success"))
    assert [
        frame async for frame in original_client.iter_turn_events(original_receipt)
    ][-1]["type"] == "result"
    await original_client.close()

    link = RunnerLink(f"ws://127.0.0.1:{server.port}/")
    await link.__aenter__()
    await link.attach("sess-1", last_seen_seq=0)
    replacement = ClaudeCodeEngineClient(
        link, session_id="sess-1", transcript_store=AsyncMock(), workspace_dir="/workspace"
    )
    input_id = "00000000-0000-0000-0000-000000000099"
    command = EngineInputCommand(
        command_id="command-after-restart",
        session_id="sess-1",
        sequence=2,
        input_id=input_id,
        content="after restart",
    )
    try:
        # The independent FIFO reaches the resident SDK before the turn worker
        # binds its stream. Its durable consumption flag prevents redelivery;
        # it does not mean this replacement host has observed the UUID boundary.
        await replacement.deliver(command)
        receipt = await replacement.begin_delivery(
            command,
            consumption_confirmed=True,
        )
        sdk.emit(AssistantMessage(content=[TextBlock(text="new reply")]))
        sdk.emit(ResultMessage(subtype="success"))

        frames = [frame async for frame in replacement.iter_turn_events(receipt)]
        text = "".join(
            str(frame.get("delta") or "")
            for frame in frames
            if frame.get("type") == "text-delta"
        )
        consumed = [
            frame.get("data", {}).get("inputId")
            for frame in frames
            if frame.get("type") == "data-input-consumed"
        ]
        assert text == "new reply"
        assert consumed == [input_id]
        assert frames[-1]["type"] == "result"
    finally:
        await replacement.close()


async def test_reattach_configures_a_fresh_runner_with_resume() -> None:
    # A relaunched runner holds no session: attach is refused loudly and the
    # host opens one from scratch — the resume id rides in the spawn options
    # so the SDK restores the conversation. The opening frame is `prepare`
    # because a cold open is the prepared-slot barriers composed back to back.
    from astrabox.core.service.orchestrator.engine.claude_code_runtime import (
        _attach_runner_engine_client,
    )

    sdk = QueueSdkSession()
    openings: list[dict[str, Any]] = []

    def factory(opening: dict[str, Any], link: HostLink) -> RunnerSession:
        openings.append(dict(opening))
        session = RunnerSession(
            session_id=str(opening["slot_id"]),
            link=link,
            client_factory=lambda broker: sdk,
            activation_callback=sdk.bind_store,
            interaction_wait_s=5.0,
        )
        sdk.consume_prompt = session.on_user_prompt_submit
        return session

    server = RunnerWsServer(host="127.0.0.1", port=0, session_factory=factory)
    await server.start()
    try:
        engine_client, how = await _attach_runner_engine_client(
            f"ws://127.0.0.1:{server.port}/",
            session_id="sess-9",
            workspace_dir="/workspace",
            sdk_options={"resume": "claude-sess-42"},
            transcript_store={"base_url": "http://host/api"},
            sandbox_death_notice={"url": "http://host/death"},
            event_sink=AsyncMock(),
        )
        assert how == "configured"
        assert sdk.store_bindings == [("sess-9", {"base_url": "http://host/api"})]
        assert openings[-1]["op"] == "prepare"
        assert openings[-1]["options"] == {
            "resume": "claude-sess-42",
            "resume_transcript": {
                "platform_session_id": "sess-9",
                "store": {"base_url": "http://host/api"},
            },
        }
        await engine_client.close()
    finally:
        await server.stop()


async def test_attach_refuses_a_session_mismatch(stack) -> None:
    # A runner holding a DIFFERENT session must refuse the attach: adopting a
    # mis-assigned box would cross two conversations' streams.
    from astrabox.core.service.orchestrator.engine.runner_link import (
        RunnerLink,
        RunnerLinkError,
    )

    server, _sdk, _client = stack
    link = RunnerLink(f"ws://127.0.0.1:{server.port}/")
    await link.__aenter__()
    try:
        with pytest.raises(RunnerLinkError):
            await link.attach("some-other-session", last_seen_seq=0)
    finally:
        await link.close()


async def test_an_answer_reaches_the_box_on_a_client_that_never_saw_the_gate(stack) -> None:
    # The restart case, with nothing left to remember. A platform restart
    # replaces the engine client: the new one never watched the gate open, so
    # its in-flight interaction state is empty while the durable record still
    # names the interaction. Answering must go by the id the caller was handed.
    server, sdk, client = stack
    receipt = await _begin_delivery(client, "delete it")
    assert server.session is not None
    gate = asyncio.create_task(
        server.session.broker.pre_tool_use(
            {"tool_name": "Bash", "tool_input": {"command": "rm x"}}, "t9", None
        )
    )
    events = client.iter_turn_events(receipt)
    frame = await _next_event_type(events, "interaction.request")
    assert frame["type"] == "interaction.request"
    pending = _pending_record(frame)

    # Whatever this client learned by watching, forget it.
    client._pending_interaction_id = None

    assert (
        await client.submit_interaction_response(
            receipt, pending=pending, response={"decision": "approve"}
        )
        is True
    )
    out = (await gate)["hookSpecificOutput"]
    assert out["permissionDecision"] == "allow"


async def test_answer_after_restart_skips_the_replayed_gate_and_reaches_terminal(
    stack,
) -> None:
    """An acknowledged answer resumes beyond its pre-restart interaction.

    Store-backed attach replays the runner journal from the new process's
    cursor, including the interaction that parked the original segment. Once
    the runner accepts that interaction's answer, replaying it into the
    continuation would park the same turn again and hide its real Result.
    """
    server, sdk, original_client = stack
    receipt = await _begin_delivery(original_client, "write it")
    assert server.session is not None
    gate = asyncio.create_task(
        server.session.broker.pre_tool_use(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "answer.txt"},
                "tool_use_id": "tool-restart",
            },
            None,
            None,
        )
    )
    original_events = original_client.iter_turn_events(receipt)
    interaction = await _next_event_type(original_events, "interaction.request")
    pending = _pending_record(interaction)
    await original_events.aclose()
    await original_client.close()

    uri = f"ws://127.0.0.1:{server.port}/"
    replacement_link = RunnerLink(uri)
    await replacement_link.__aenter__()
    await replacement_link.attach("sess-1", last_seen_seq=0)
    replacement_client = ClaudeCodeEngineClient(
        replacement_link,
        session_id="sess-1",
        transcript_store=AsyncMock(),
        workspace_dir="/workspace",
    )
    try:
        assert await replacement_client.submit_interaction_response(
            receipt,
            pending=pending,
            response={"decision": "approve"},
        )
        assert (await gate)["hookSpecificOutput"]["permissionDecision"] == "allow"

        sdk.emit(AssistantMessage(content=[TextBlock(text="continued")]))
        sdk.emit(ResultMessage(subtype="success"))
        resumed = [
            frame
            async for frame in replacement_client.iter_turn_events(receipt)
        ]

        assert not any(frame["type"] == "interaction.request" for frame in resumed), (
            "the answered pre-restart gate must not terminate the continuation"
        )
        assert resumed[-1]["type"] == "result"
        assert resumed[-1]["finishReason"] == "stop"
    finally:
        await replacement_client.close()
