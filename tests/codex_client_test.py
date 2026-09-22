"""The client's half of the app-server contract, against a scripted link.

What is worth pinning here is what the platform depends on and the protocol
does not enforce: that a redelivered command does not prompt Codex twice, that
a resumed conversation is rejoined rather than recreated, that the reply opens
with the consumption boundary, and that an approval is answered on the id
Codex is waiting on rather than on the one that happened to arrive last.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from astrabox.core.service.orchestrator.engine.base import (
    EngineConversationBinding,
    EngineInputCommand,
    EngineStreamDetached,
)
from astrabox.core.service.orchestrator.engine.codex_client import (
    CODEX_SANDBOX_POLICIES,
    CodexEngineClient,
)
from astrabox.core.service.orchestrator.engine.input_delivery import (
    input_response_message_id,
)

_SESSION = "s-1"
_THREAD = "01a010cd-9455-74c3-bcd2-6a3225a5e82e"
_TURN = "01a010cd-0000-0000-0000-000000000001"
_END = object()
# Real UUIDs: the platform derives the consumption frame's response id from
# the input id and refuses anything that is not one.
_INPUT_IDS = {
    1: "11111111-1111-4111-8111-111111111111",
    2: "22222222-2222-4222-8222-222222222222",
}


class ScriptedLink:
    """A link that records calls and replays a scripted inbound stream."""

    def __init__(
        self,
        inbound: list[Any] | None = None,
        *,
        turns: list[list[Any]] | None = None,
        collaboration_modes: list[dict[str, Any]] | None = None,
        resumed_model: str = "resumed-model",
    ) -> None:
        # What the app-server puts on the wire for each `turn/start` it
        # accepts, in order. A turn's notifications exist only once it was
        # prompted, so a held input's turn cannot be pre-queued.
        self._turns = [list(turn) for turn in turns or []]
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: list[tuple[Any, dict[str, Any]]] = []
        self.closed = False
        self.server_info = {"userAgent": "astrabox/0.147.0", "codexHome": "/home/gem/.codex"}
        self.collaboration_modes = collaboration_modes or [
            {
                "name": "Plan",
                "mode": "plan",
                "model": None,
                "reasoning_effort": "medium",
            },
            {
                "name": "Default",
                "mode": "default",
                "model": None,
                "reasoning_effort": None,
            },
        ]
        self.resumed_model = resumed_model
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        for item in inbound or []:
            self._queue.put_nowait(item)

    @property
    def is_live(self) -> bool:
        return not self.closed

    async def call(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        self.calls.append((method, dict(params or {})))
        if method == "thread/start":
            return {
                "thread": {"id": _THREAD},
                "model": (params or {}).get("model") or "started-model",
            }
        if method == "thread/resume":
            return {"thread": {"id": _THREAD}, "model": self.resumed_model}
        if method == "collaborationMode/list":
            return {"data": list(self.collaboration_modes)}
        if method == "turn/start":
            if self._turns:
                for item in self._turns.pop(0):
                    self._queue.put_nowait(item)
            return {"turn": {"id": _TURN}}
        return {}

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.calls.append((method, dict(params or {})))

    async def respond(self, request_id: Any, result: dict[str, Any]) -> None:
        self.responses.append((request_id, result))

    def iter_inbound(self) -> AsyncIterator[dict[str, Any]]:
        async def _iter() -> AsyncIterator[dict[str, Any]]:
            while True:
                item = await self._queue.get()
                if item is _END:
                    raise EngineStreamDetached("scripted stream ended")
                yield item

        return _iter()

    async def close(self) -> None:
        self.closed = True


def _client(link: ScriptedLink, **kwargs: Any) -> CodexEngineClient:
    return CodexEngineClient(session_id=_SESSION, link=link, cwd="/home/agent/workspace", **kwargs)


async def _bound(link: ScriptedLink, *, native: str | None = None, **kwargs: Any):
    client = _client(link, native_thread_id=native, **kwargs)
    await client.bind_conversation(
        EngineConversationBinding(platform_session_id=_SESSION, engine_session_key=native)
    )
    return client


def _command(*, seq: int = 1, content: str = "hello") -> EngineInputCommand:
    return EngineInputCommand(
        command_id=f"cmd-{seq}",
        session_id=_SESSION,
        sequence=seq,
        input_id=_INPUT_IDS[seq],
        content=content,
    )


def _notify(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"method": method, "params": params}


# ── conversation identity ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_new_conversation_starts_a_thread_and_keeps_its_id() -> None:
    link = ScriptedLink()
    client = await _bound(link)

    assert [name for name, _ in link.calls] == ["thread/start"]
    assert client.engine_session_key == _THREAD


@pytest.mark.asyncio
async def test_a_resumed_conversation_is_rejoined_not_recreated() -> None:
    """`thread/resume` names the thread; a start would answer with amnesia."""

    link = ScriptedLink()
    client = await _bound(link, native=_THREAD)

    assert [name for name, _ in link.calls] == ["thread/resume"]
    assert link.calls[0][1] == {"threadId": _THREAD}
    assert client.engine_session_key == _THREAD


@pytest.mark.asyncio
async def test_a_rejoin_carries_the_provider_table_but_not_the_model() -> None:
    """The endpoint is process state; the model is thread state.

    `model_providers` lives in the app-server process, not in the thread. A
    start carrying `resume_engine_session_key` — a new box for a conversation
    that already has a thread — therefore rejoins a server that never saw the
    table, and refuses the resume outright without it. The model must NOT ride
    along: the thread already holds one, and re-sending it would override that
    with the Agent's configured model, moving a running conversation onto a
    different one without anyone asking.
    """

    config = {
        "model_provider": "astrabox",
        "model_providers": {"astrabox": {"base_url": "http://gw.test"}},
    }
    link = ScriptedLink()
    await _bound(link, native=_THREAD, thread_config=config, model="some-model")

    assert [name for name, _ in link.calls] == ["thread/resume"]
    params = link.calls[0][1]
    assert params["threadId"] == _THREAD
    assert params["config"] == config
    assert "model" not in params


@pytest.mark.asyncio
async def test_a_durable_key_that_contradicts_the_client_is_refused() -> None:
    link = ScriptedLink()
    client = _client(link, native_thread_id=_THREAD)

    with pytest.raises(RuntimeError, match="resume key"):
        await client.bind_conversation(
            EngineConversationBinding(
                platform_session_id=_SESSION, engine_session_key="somebody-elses-thread"
            )
        )


@pytest.mark.asyncio
async def test_the_agents_instructions_and_gateway_ride_thread_start() -> None:
    """Neither is written into the box: Codex takes both as parameters."""

    link = ScriptedLink()
    await _bound(
        link,
        instructions="You are the AstraBox reviewer agent.",
        model="gpt-5.3-codex",
        sandbox_mode="read-only",
        thread_config={"model_provider": "astrabox", "approval_policy": "untrusted"},
    )

    params = link.calls[0][1]
    assert params["developerInstructions"] == "You are the AstraBox reviewer agent."
    assert params["model"] == "gpt-5.3-codex"
    assert params["sandbox"] == "read-only"
    assert params["config"] == {"model_provider": "astrabox", "approval_policy": "untrusted"}


# ── sending ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_turn_start_response_is_the_consumption_evidence() -> None:
    """Codex creates the turn before answering, so nothing has to be watched."""

    link = ScriptedLink()
    client = await _bound(link)
    receipt = await client.begin_delivery(_command())

    assert receipt.engine_turn_id == _TURN
    # Platform state, not a vendor fact: a fresh delivery has proved no
    # durable crossing, and claiming one is refused by turn dispatch with
    # "consumption state disagrees with durable FIFO evidence".
    assert receipt.input_consumed is False
    method, params = link.calls[-1]
    assert method == "turn/start"
    # The platform's own input id travels as Codex's client message id, so a
    # redelivery is recognisable on either side of the wire.
    assert params["clientUserMessageId"] == _INPUT_IDS[1]
    assert params["input"] == [{"type": "text", "text": "hello", "text_elements": []}]


@pytest.mark.asyncio
async def test_a_redelivered_command_does_not_prompt_codex_twice() -> None:
    link = ScriptedLink()
    client = await _bound(link)
    first = await client.begin_delivery(_command())
    second = await client.begin_delivery(_command())

    assert second is first
    assert [name for name, _ in link.calls].count("turn/start") == 1


@pytest.mark.asyncio
async def test_a_mode_chosen_after_the_thread_exists_rides_the_next_turn() -> None:
    """`thread/start`'s `sandbox` is spent; the per-turn override is not."""

    link = ScriptedLink()
    client = await _bound(link)
    await client.set_permission_mode("danger-full-access")
    await client.begin_delivery(_command())

    params = link.calls[-1][1]
    assert params["sandboxPolicy"] == CODEX_SANDBOX_POLICIES["danger-full-access"]


@pytest.mark.asyncio
async def test_a_mode_codex_does_not_offer_is_refused() -> None:
    client = await _bound(ScriptedLink())

    with pytest.raises(ValueError, match="does not offer"):
        await client.set_permission_mode("yolo")


@pytest.mark.asyncio
async def test_plan_collaboration_mode_passes_native_settings_to_codex() -> None:
    """Plan is a Codex turn preset, not one of its sandbox permission modes."""

    link = ScriptedLink()
    client = await _bound(
        link,
        model="deepseek-v4-flash",
        turn_options={
            "collaborationMode": {
                "mode": "plan",
                "settings": {
                    "reasoning_effort": "medium",
                    "developer_instructions": None,
                },
            }
        },
    )
    await client.begin_delivery(_command())

    assert [name for name, _ in link.calls] == [
        "thread/start",
        "turn/start",
    ]
    params = link.calls[-1][1]
    assert params["collaborationMode"] == {
        "mode": "plan",
        "settings": {
            "model": "deepseek-v4-flash",
            "reasoning_effort": "medium",
            "developer_instructions": None,
        },
    }
    assert "sandboxPolicy" not in params
    assert "plan" not in CODEX_SANDBOX_POLICIES


@pytest.mark.asyncio
async def test_plan_after_reattach_uses_the_threads_actual_model() -> None:
    """A changed Agent config must not move an existing thread to that model."""

    link = ScriptedLink(resumed_model="thread-owned-model")
    client = await _bound(
        link,
        native=_THREAD,
        model="new-agent-model",
        turn_options={
            "collaborationMode": {
                "mode": "plan",
                "settings": {
                    "reasoning_effort": None,
                    "developer_instructions": None,
                },
            }
        },
    )
    await client.begin_delivery(_command())

    assert link.calls[-1][1]["collaborationMode"]["settings"]["model"] == ("thread-owned-model")


@pytest.mark.asyncio
async def test_native_turn_options_are_not_filtered_by_an_adapter_whitelist() -> None:
    link = ScriptedLink()
    options = {"effort": "low", "outputSchema": {"type": "object"}, "futureOption": {"value": 1}}
    client = await _bound(link, turn_options=options)
    await client.begin_delivery(_command())
    assert all(link.calls[-1][1][key] == value for key, value in options.items())


# ── streaming ────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_reply_opens_with_the_consumption_boundary_and_ends_on_the_turn() -> None:
    link = ScriptedLink(
        [
            _notify("turn/started", {"threadId": _THREAD, "turn": {"id": _TURN}}),
            _notify(
                "item/started",
                {
                    "item": {"type": "agentMessage", "id": "i1", "text": ""},
                    "threadId": _THREAD,
                    "turnId": _TURN,
                    "startedAtMs": 1,
                },
            ),
            _notify(
                "item/agentMessage/delta",
                {"threadId": _THREAD, "turnId": _TURN, "itemId": "i1", "delta": "hi"},
            ),
            _notify(
                "turn/completed",
                {"threadId": _THREAD, "turn": {"id": _TURN, "status": "completed"}},
            ),
        ]
    )
    client = await _bound(link)
    receipt = await client.begin_delivery(_command())

    seen = [emission.as_frame() async for emission in client.iter_turn_events(receipt)]

    assert seen[0]["type"] == "data-input-consumed"
    assert seen[0]["data"]["inputId"] == _INPUT_IDS[1]
    # Derived by the platform from the input id. Dispatch compares it exactly,
    # so Codex's own turn id here reads as a different message being consumed
    # and the turn is refused.
    assert seen[0]["data"]["responseMessageId"] == input_response_message_id(_INPUT_IDS[1])
    # The message is carried back verbatim rather than as an empty string.
    assert seen[0]["data"]["content"] == "hello"
    assert [frame["type"] for frame in seen[1:]] == [
        "start-step",
        "text-start",
        "text-delta",
        "result",
    ]
    assert seen[-1]["finishReason"] == "stop"


@pytest.mark.asyncio
async def test_another_clients_turn_on_the_same_thread_is_not_ours() -> None:
    """Codex tags every notification with its turn; one box can hold more."""

    link = ScriptedLink(
        [
            _notify("turn/started", {"threadId": _THREAD, "turn": {"id": _TURN}}),
            _notify(
                "item/agentMessage/delta",
                {"threadId": _THREAD, "turnId": "someone-elses", "itemId": "x", "delta": "no"},
            ),
            _notify(
                "turn/completed",
                {"threadId": _THREAD, "turn": {"id": _TURN, "status": "completed"}},
            ),
        ]
    )
    client = await _bound(link)
    receipt = await client.begin_delivery(_command())

    seen = [emission.as_frame() async for emission in client.iter_turn_events(receipt)]

    # The foreign delta would have raised "no open block" had it been taken.
    assert [frame["type"] for frame in seen] == ["data-input-consumed", "start-step", "result"]


@pytest.mark.asyncio
async def test_a_stream_that_ends_without_a_terminal_detaches() -> None:
    link = ScriptedLink([_END])
    client = await _bound(link)
    receipt = await client.begin_delivery(_command())

    with pytest.raises(EngineStreamDetached):
        async for _ in client.iter_turn_events(receipt):
            pass


# ── interactions ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_an_approval_parks_the_turn_and_answers_on_codexs_own_id() -> None:
    link = ScriptedLink(
        [
            _notify("turn/started", {"threadId": _THREAD, "turn": {"id": _TURN}}),
            {
                "id": 42,
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "threadId": _THREAD,
                    "turnId": _TURN,
                    "itemId": "cmd-1",
                    "startedAtMs": 1,
                    "reason": "writes outside the workspace",
                },
            },
        ]
    )
    client = await _bound(link)
    receipt = await client.begin_delivery(_command())

    seen = [emission.as_frame() async for emission in client.iter_turn_events(receipt)]

    assert seen[-1]["type"] == "interaction.request"
    interaction_id = seen[-1]["interactionId"]

    # Called the way the platform calls it: the durable record carries the
    # interaction id, and every other argument arrives by keyword.
    accepted = await client.submit_interaction_response(
        receipt,
        pending={"interaction_id": interaction_id},
        response={"decision": "accept"},
    )

    assert accepted is True
    # Routed by the request id Codex is blocked on, not by arrival order.
    assert link.responses == [(42, {"decision": "accept"})]


@pytest.mark.asyncio
async def test_an_answer_to_an_interaction_nobody_is_waiting_on_is_reported() -> None:
    """A stale answer is an outcome for the platform, not a transport error."""

    client = await _bound(ScriptedLink())

    receipt = await client.begin_delivery(_command())
    answered = await client.submit_interaction_response(
        receipt,
        pending={"interaction_id": "codex-999"},
        response={"decision": "accept"},
    )
    assert answered is False


@pytest.mark.asyncio
async def test_a_recovered_input_is_rejoined_rather_than_sent_again() -> None:
    """`consumption_confirmed` is durable evidence Codex already took it.

    Re-sending would run the same message twice, and re-emitting the
    consumption frame would show the user's message twice in the transcript.
    """

    link = ScriptedLink(
        [
            _notify(
                "turn/completed",
                {"threadId": _THREAD, "turn": {"id": _TURN, "status": "completed"}},
            )
        ]
    )
    client = await _bound(link)
    before = [name for name, _ in link.calls]

    receipt = await client.begin_delivery(_command(), consumption_confirmed=True)

    assert [name for name, _ in link.calls] == before, "nothing may be sent again"
    assert receipt.input_consumed is True

    seen = [emission.as_frame() async for emission in client.iter_turn_events(receipt)]

    assert [frame["type"] for frame in seen] == ["result"]


@pytest.mark.asyncio
async def test_a_send_during_a_turn_waits_for_it_and_answers_after() -> None:
    """Strict order, on an engine that cannot queue.

    The seam requires strictly ordered inputs and says how an engine without a
    queue meets it: the adapter buffers until the vendor accepts its next
    prompt. Codex has no queue — `turn/start` on a busy thread is taken by the
    running turn, and `turn/steer` is its interrupt, which answers the new
    message BEFORE the running one (measured: INTERLOPER at character 374,
    DONE at 514).

    So the held input goes when the running turn is terminal, and its answer
    rides the same stream: one dispatch, two consumptions, in order.
    """

    turn = [
        _notify("turn/started", {"threadId": _THREAD, "turn": {"id": _TURN}}),
        _notify(
            "turn/completed",
            {"threadId": _THREAD, "turn": {"id": _TURN, "status": "completed"}},
        ),
    ]
    link = ScriptedLink(turns=[turn, turn])
    client = await _bound(link)
    first = await client.begin_delivery(_command(seq=1, content="one"))
    await asyncio.wait_for(client.deliver(_command(seq=2, content="two")), timeout=1.0)

    # Nothing reached the engine for it yet: one prompt so far.
    assert [m for m, _ in link.calls if m == "turn/start"] == ["turn/start"]
    assert "turn/steer" not in [m for m, _ in link.calls]

    frames = [e.as_frame() async for e in client.iter_turn_events(first)]

    consumed = [f["data"]["inputId"] for f in frames if f["type"] == "data-input-consumed"]
    assert consumed == [_INPUT_IDS[1], _INPUT_IDS[2]], consumed
    # The held one was prompted only after the first turn ended, and exactly
    # one terminal closes the batch.
    assert [m for m, _ in link.calls if m == "turn/start"] == [
        "turn/start",
        "turn/start",
    ]
    assert [f["type"] for f in frames].count("result") == 1
    assert frames[-1]["type"] == "result", frames[-1]


@pytest.mark.asyncio
async def test_an_idle_thread_prompts_immediately() -> None:
    """Buffering is for a busy thread; the first message has nothing to wait for."""

    link = ScriptedLink()
    client = await _bound(link)
    await client.deliver(_command(seq=1, content="one"))
    assert [m for m, _ in link.calls if m == "turn/start"] == ["turn/start"]
