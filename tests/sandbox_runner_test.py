"""In-box runner core — the behaviors the translation shell stands on.

These test intent, not plumbing: envelope gaps must be *detectable* (single
monotonic seq, disconnected sends drop but still consume seq), input delivery
must be idempotent by command_id (the outbox redelivers on recovery), the
PreToolUse broker must answer allow/deny and fall back to ``defer`` on
silence (the defer/resume semantics are proven separately by the spike), the
native user-input callback must produce exactly one answerable interaction,
and the spool store must never lose an acked batch across flush failure or a
runner restart.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import threading
import uuid
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    ClaudeAgentOptions,
    PermissionResultAllow,
    ToolPermissionContext,
)
from claude_agent_sdk._internal.session_resume import materialize_resume_session

from astrabox.core.service.orchestrator import sandbox_runner
from astrabox.core.service.orchestrator.sandbox_runner import (
    DeliveryCommand,
    InteractionAnswer,
    InteractionBroker,
    EnvelopeSender,
    RunnerSession,
    SpoolSessionStore,
)


class FakeLink:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.connected = True

    def is_connected(self) -> bool:
        return self.connected

    async def send(self, frame: dict[str, Any]) -> bool:
        self.frames.append(frame)
        return True


@dataclasses.dataclass
class ResultMessage:
    """Name-matched fake: the pump keys terminal handling on the type name."""

    subtype: str = "success"
    session_id: str = "sdk-sess"
    deferred_tool_use: dict[str, Any] | None = None


@dataclasses.dataclass
class AssistantMessage:
    content: str = "hello"


@dataclasses.dataclass
class UserMessage:
    content: str = ""
    uuid: str = ""
    parent_tool_use_id: str | None = None


class FakeSdkSession:
    def __init__(self, scripted: list[Any]) -> None:
        self.scripted = scripted
        self.queries: list[Any] = []
        self.interrupted = 0
        self._gate: asyncio.Event = asyncio.Event()
        self.consume_prompt: Any = None

    async def connect(self) -> None:
        pass

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
        self._gate.set()

    async def receive_messages(self):  # noqa: ANN201 — async generator protocol
        await self._gate.wait()
        for message in self.scripted:
            yield message

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


def _session(
    scripted: list[Any] | None = None, store: SpoolSessionStore | None = None
) -> tuple[RunnerSession, FakeLink, FakeSdkSession]:
    link = FakeLink()
    client = FakeSdkSession(scripted or [])
    session = RunnerSession(
        session_id="sess-1",
        link=link,
        client_factory=lambda broker: client,
        store=store,
        interaction_wait_s=0.05,
    )
    client.consume_prompt = session.on_user_prompt_submit
    return session, link, client


def _delivery(command_id: str, content: str, *, sequence: int = 1) -> DeliveryCommand:
    input_id = str(uuid.uuid5(uuid.NAMESPACE_URL, command_id))
    return DeliveryCommand(
        command_id=command_id,
        session_id="sess-1",
        sequence=sequence,
        sdk_input={
            "type": "user",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
            "session_id": "sess-1",
            "uuid": input_id,
        },
    )


def test_prompt_consumption_hook_precedes_configured_hooks() -> None:
    from claude_agent_sdk import HookMatcher

    session, _link, _client = _session()

    async def configured_hook(
        _hook_input: Any,
        _tool_use_id: str | None,
        _context: Any,
    ) -> dict[str, Any]:
        return {}

    configured = HookMatcher(hooks=[configured_hook])
    hooks = session._with_prompt_consumption_hook(
        {"UserPromptSubmit": [configured]}
    )

    assert hooks["UserPromptSubmit"][0].hooks == [session.on_user_prompt_submit]
    assert hooks["UserPromptSubmit"][1] is configured


# --- envelope ---------------------------------------------------------------


async def test_envelope_seq_is_single_and_monotonic_across_ops() -> None:
    link = FakeLink()
    sender = EnvelopeSender(link, "sess-1")
    await sender.send("status", state="idle")
    await sender.send("event", message={"x": 1})
    await sender.send("interaction", interaction_id="i1")
    seqs = [f["seq"] for f in link.frames]
    assert seqs == [1, 2, 3]
    assert all(f["session_id"] == "sess-1" for f in link.frames)


async def test_disconnected_link_drops_frame_but_consumes_seq() -> None:
    # Dropped live frames are BY DESIGN (durable truth is the store); the seq
    # gap is how the host knows to refill. A silent seq reuse would instead
    # present a stale stream as complete.
    link = FakeLink()
    sender = EnvelopeSender(link, "sess-1")
    await sender.send("event", message={"n": 1})
    link.connected = False
    await sender.send("event", message={"n": 2})
    link.connected = True
    await sender.send("event", message={"n": 3})
    delivered = [f["seq"] for f in link.frames]
    assert delivered == [1, 3], "gap at 2 must be visible to the host"


# --- input delivery ---------------------------------------------------------


async def test_input_delivery_is_idempotent_by_command_id() -> None:
    session, link, client = _session()
    await session.start()
    command = _delivery("cmd-1", "do it")
    assert await session.submit(command) == "accepted"
    assert await session.submit(command) == "duplicate"
    assert [
        {key: value for key, value in item.items() if key != "uuid"}
        for item in client.queries
    ] == [
        {key: value for key, value in command.sdk_input.items() if key != "uuid"}
    ], "outbox redelivery must not double-run"
    await session.stop()


async def test_each_attempt_gets_its_own_vendor_message_id() -> None:
    """Claude drops, without a word, a streaming input whose uuid already names
    a message in the session it resumed. A re-delivery into a rebuilt box is
    exactly that case, so the vendor never sees the platform's input id."""

    session, _link, client = _session()
    await session.start()
    command = _delivery("cmd-1", "do it")
    await session.submit(command)

    assert len(client.queries) == 1
    sent = client.queries[0]
    assert sent["message"] == command.sdk_input["message"]
    assert sent["uuid"] != command.sdk_input["uuid"], (
        "the vendor must not be handed the platform input id as a message id"
    )
    uuid.UUID(sent["uuid"])
    await session.stop()


async def test_the_host_still_reads_the_platform_input_id_off_the_echo() -> None:
    """The host takes a root UserMessage's uuid as the consumption receipt for
    that platform input, so the vendor's own message id must not reach it."""

    session, link, client = _session()
    messages: asyncio.Queue[Any] = asyncio.Queue()

    async def receive_messages():  # noqa: ANN202 — async generator test double
        while True:
            yield await messages.get()

    client.receive_messages = receive_messages  # type: ignore[method-assign]
    client.consume_prompt = None  # let the SDK echo carry consumption instead
    await session.start()

    command = _delivery("cmd-1", "do it")
    await session.submit(command)
    vendor_uuid = client.queries[0]["uuid"]

    messages.put_nowait(
        UserMessage(content="do it", uuid=vendor_uuid, parent_tool_use_id=None)
    )
    for _ in range(50):
        echoed = [
            frame
            for frame in link.frames
            if frame.get("message_type") == "UserMessage"
        ]
        if echoed:
            break
        await asyncio.sleep(0)
    assert echoed, "the root UserMessage must reach the host"
    assert echoed[0]["message"]["uuid"] == command.sdk_input["uuid"], (
        "the host must see the platform input id, not the vendor message id"
    )
    await session.stop()


async def test_next_input_is_accepted_but_waits_for_previous_result() -> None:
    session, _link, client = _session()
    messages: asyncio.Queue[Any] = asyncio.Queue()

    async def receive_messages():  # noqa: ANN202 — async generator test double
        while True:
            yield await messages.get()

    client.receive_messages = receive_messages  # type: ignore[method-assign]
    await session.start()

    await session.submit(_delivery("cmd-1", "first", sequence=1))
    second = _delivery("cmd-2", "during background work", sequence=2)
    assert await asyncio.wait_for(session.submit(second), timeout=0.2) == "accepted"
    assert await session.submit(second) == "duplicate"

    assert [item["message"]["content"] for item in client.queries] == [
        "first",
    ], "adapter acceptance must not steer the vendor's running response"

    messages.put_nowait(ResultMessage())
    for _ in range(20):
        if len(client.queries) == 2:
            break
        await asyncio.sleep(0)
    assert [item["message"]["content"] for item in client.queries] == [
        "first",
        "during background work",
    ], "the buffered input must reach the vendor at its next result boundary"
    await session.stop()


async def test_interrupt_hands_the_vendor_to_the_buffered_successor() -> None:
    session, link, client = _session()
    messages: asyncio.Queue[Any] = asyncio.Queue()

    async def receive_messages():  # noqa: ANN202 — async generator test double
        while True:
            yield await messages.get()

    client.receive_messages = receive_messages  # type: ignore[method-assign]
    await session.start()

    await session.submit(_delivery("cmd-1", "first", sequence=1))
    await session.submit(_delivery("cmd-2", "second", sequence=2))
    assert [item["message"]["content"] for item in client.queries] == ["first"]

    await session.interrupt("cmd-1")

    assert [item["message"]["content"] for item in client.queries] == [
        "first",
        "second",
    ]
    interrupted = [frame for frame in link.frames if frame["op"] == "turn_interrupted"]
    assert interrupted[-1]["continues_fifo"] is True
    await session.stop()


async def test_redelivery_waits_for_the_first_input_receipt_boundary() -> None:
    """A retry cannot claim duplicate while the original query is unresolved."""
    session, link, client = _session()
    await session.start()
    query_started = asyncio.Event()
    release_query = asyncio.Event()

    async def delayed_query(prompt: Any, session_id: str = "default") -> None:
        _ = session_id
        items = [item async for item in prompt]
        client.queries.extend(items)
        for item in items:
            await client.consume_prompt(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": item["message"]["content"],
                },
                None,
                None,
            )
        query_started.set()
        await release_query.wait()
        client._gate.set()

    client.query = delayed_query  # type: ignore[method-assign]
    command = _delivery("cmd-1", "do it")
    first = asyncio.create_task(session.submit(command))
    await asyncio.wait_for(query_started.wait(), timeout=1.0)
    redelivery = asyncio.create_task(session.submit(command))
    await asyncio.sleep(0)

    assert [f for f in link.frames if f["op"] == "input_ack"] == [], (
        "duplicate acknowledgement before query completion is a false receipt"
    )

    release_query.set()
    assert await asyncio.gather(first, redelivery) == ["accepted", "accepted"]
    # One query for both submissions, under a vendor message id of its own:
    # Claude drops a resumed input whose uuid it already holds.
    assert len(client.queries) == 1
    (queried,) = client.queries
    assert {k: v for k, v in queried.items() if k != "uuid"} == {
        k: v for k, v in command.sdk_input.items() if k != "uuid"
    }
    assert queried["uuid"] != command.sdk_input["uuid"]
    await session.stop()


async def test_input_flips_status_to_busy_once() -> None:
    session, link, _client = _session()
    await session.start()
    await session.submit(_delivery("cmd-1", "a", sequence=1))
    await session.submit(_delivery("cmd-2", "b", sequence=2))
    busy = [f for f in link.frames if f["op"] == "status" and f["state"] == "busy"]
    assert len(busy) == 1, "the adapter buffer remains part of one busy edge"
    await session.stop()


# --- interaction broker -----------------------------------------------------


async def test_ask_user_uses_one_native_callback_interaction() -> None:
    """AskUser passes the hook and returns the browser's answers via can_use_tool."""
    link = FakeLink()
    broker = InteractionBroker(EnvelopeSender(link, "s"), wait_budget_s=5.0)
    question_input = {
        "questions": [
            {
                "question": "Pick a color",
                "header": "Color",
                "options": [
                    {"label": "RED", "description": "Warm"},
                    {"label": "BLUE", "description": "Cool"},
                ],
                "multiSelect": False,
            }
        ]
    }

    hook_out = await broker.pre_tool_use(
        {
            "tool_name": "AskUserQuestion",
            "tool_input": question_input,
            "tool_use_id": "toolu_ask",
        },
        None,
        None,
    )
    assert hook_out == {}
    assert link.frames == [], "the earlier hook must not publish the question twice"

    callback = asyncio.create_task(
        broker.can_use_tool(
            "AskUserQuestion",
            question_input,
            ToolPermissionContext(tool_use_id="toolu_ask"),
        )
    )
    await asyncio.sleep(0)
    assert len(link.frames) == 1
    frame = link.frames[0]
    assert frame["tool_name"] == "AskUserQuestion"
    assert frame["tool_input"] == question_input
    assert frame["tool_use_id"] == "toolu_ask"

    answered_input = {
        "questions": question_input["questions"],
        "answers": {"Pick a color": "RED"},
    }
    assert broker.answer(
        frame["interaction_id"],
        InteractionAnswer(decision="allow", updated_input=answered_input),
    )
    result = await callback
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input == answered_input
    assert broker.pending_ids() == []


async def test_can_use_tool_does_not_reask_an_ordinary_hook_gated_tool() -> None:
    """If the CLI consults both seams, the hook remains the one platform gate."""
    link = FakeLink()
    broker = InteractionBroker(EnvelopeSender(link, "s"), wait_budget_s=5.0)
    hook = asyncio.create_task(
        broker.pre_tool_use(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "pwd"},
                "tool_use_id": "toolu_bash",
            },
            None,
            None,
        )
    )
    await asyncio.sleep(0)
    interaction_id = link.frames[0]["interaction_id"]
    assert broker.answer(interaction_id, InteractionAnswer(decision="allow"))
    assert (await hook)["hookSpecificOutput"]["permissionDecision"] == "allow"

    result = await broker.can_use_tool(
        "Bash",
        {"command": "pwd"},
        ToolPermissionContext(tool_use_id="toolu_bash"),
    )
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input == {"command": "pwd"}
    assert len(link.frames) == 1, "one tool decision must not become two prompts"


async def test_interaction_allow_routes_updated_input() -> None:
    link = FakeLink()
    broker = InteractionBroker(EnvelopeSender(link, "s"), wait_budget_s=5.0)
    task = asyncio.create_task(
        broker.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": "x"}}, "t1", None)
    )
    await asyncio.sleep(0)  # let the interaction frame go out
    interaction_id = link.frames[-1]["interaction_id"]
    assert broker.answer(
        interaction_id,
        InteractionAnswer(decision="allow", updated_input={"command": "y"}),
    )
    out = (await task)["hookSpecificOutput"]
    assert out["permissionDecision"] == "allow"
    assert out["updatedInput"] == {"command": "y"}


async def test_the_gate_reads_the_tool_id_the_VENDOR_sends() -> None:
    # The vendor puts it on the hook INPUT and declares it required there:
    # `PreToolUseHookInput.tool_use_id: str`. The callback's positional
    # `tool_use_id` is a separate, optional argument (`HookCallback`'s second
    # parameter is `str | None` — not every hook event has one), and the CLI
    # leaves it unset for PreToolUse.
    #
    # Reading the optional positional value gives a real gate an empty id. The
    # fixture must therefore put the id on the input, matching the shape the CLI
    # sends and exercising the field that binds an approval to its tool block.
    link = FakeLink()
    broker = InteractionBroker(EnvelopeSender(link, "s"), wait_budget_s=5.0)
    task = asyncio.create_task(
        broker.pre_tool_use(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "x"},
                "tool_use_id": "toolu_from_input",
            },
            None,  # exactly what the CLI passes here
            None,
        )
    )
    await asyncio.sleep(0)
    assert link.frames[-1]["tool_use_id"] == "toolu_from_input"
    task.cancel()


async def test_interaction_frame_reports_the_gates_tool_use_id() -> None:
    # Identity stays runner-minted (it must survive defer/resume), but the tool
    # id rides along: it is the only authoritative name for the tool_use block
    # the gate belongs to, and this hook is the one place both are in hand.
    # Dropping it left the host matching gate inputs against parsed stream
    # blocks to guess the binding.
    link = FakeLink()
    broker = InteractionBroker(EnvelopeSender(link, "s"), wait_budget_s=5.0)
    task = asyncio.create_task(
        broker.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": "x"}}, "toolu_42", None)
    )
    await asyncio.sleep(0)
    frame = link.frames[-1]
    assert frame["tool_use_id"] == "toolu_42"
    assert frame["interaction_id"] != "toolu_42"
    broker.answer(frame["interaction_id"], InteractionAnswer(decision="allow"))
    await task


async def test_interaction_deny_carries_user_message() -> None:
    link = FakeLink()
    broker = InteractionBroker(EnvelopeSender(link, "s"), wait_budget_s=5.0)
    task = asyncio.create_task(broker.pre_tool_use({"tool_name": "Bash"}, None, None))
    await asyncio.sleep(0)
    broker.answer(
        link.frames[-1]["interaction_id"],
        InteractionAnswer(decision="deny", message="use tar instead"),
    )
    out = (await task)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"] == "use tar instead"


async def test_interaction_defers_when_the_HOST_is_gone() -> None:
    # The budget is on the host, not on the human. A link that is already down
    # has nobody to answer, so the wait converts to the SDK's official defer
    # rather than pinning the run to a host that will not come back.
    link = FakeLink()
    link.connected = False
    broker = InteractionBroker(EnvelopeSender(link, "s"), wait_budget_s=0.02)
    out = (await broker.pre_tool_use({"tool_name": "Bash"}, None, None))[
        "hookSpecificOutput"
    ]
    assert out["permissionDecision"] == "defer"


async def test_interaction_keeps_waiting_while_a_host_is_listening() -> None:
    # Replaces "user silence defers": a person deciding whether to allow a tool
    # routinely takes longer than any budget worth setting, and a platform
    # restart certainly does — answering an approval after a restart returns
    # INTERACTION_EXPIRED because the box had deferred a wait nobody abandoned.
    # While a host is connected the vendor's own model applies: can_use_tool
    # blocks until it is answered.
    link = FakeLink()
    broker = InteractionBroker(EnvelopeSender(link, "s"), wait_budget_s=0.02)
    task = asyncio.create_task(broker.pre_tool_use({"tool_name": "Bash"}, None, None))
    await asyncio.sleep(0.15)  # many budgets' worth
    assert not task.done(), "a connected host means someone is still deciding"

    interaction_id = link.frames[-1]["interaction_id"]
    broker.answer(interaction_id, InteractionAnswer(decision="allow"))
    out = (await task)["hookSpecificOutput"]
    assert out["permissionDecision"] == "allow"


async def test_a_connected_wait_still_defers_before_the_CLI_aborts_the_hook() -> None:
    # "Wait while a host is listening" cannot mean "wait forever": the CLI runs
    # this callback under HookMatcher.timeout, and when THAT expires it aborts
    # the callback instead of taking a decision from the broker. An aborted
    # callback drops the gate — the interaction id is gone and a later answer
    # resolves nothing (INTERACTION_EXPIRED, on the vendor's 60s default, which
    # this code never set). So the broker must reach its own defer first, with
    # the host present the whole time.
    link = FakeLink()
    broker = InteractionBroker(
        EnvelopeSender(link, "s"),
        wait_budget_s=3600.0,  # host-absence budget must NOT be what fires
        hook_timeout_s=60.2,  # ceiling = timeout - margin, floored at 1.0s
    )
    out = (await broker.pre_tool_use({"tool_name": "Bash"}, None, None))[
        "hookSpecificOutput"
    ]
    assert link.connected, "the host never left; only the hook's own clock ran out"
    assert out["permissionDecision"] == "defer"


def test_the_gate_tells_the_CLI_how_long_it_may_take() -> None:
    # The vendor's default is 60s (HookMatcher: "Timeout in seconds for all
    # hooks in this matcher (default: 60)"). A person reading a diff routinely
    # takes longer, so an unset timeout expires approvals that nobody
    # abandoned. Pinned because the loss is silent: nothing fails, the gate
    # just stops existing.
    from claude_agent_sdk import HookMatcher

    matcher = HookMatcher(
        matcher=None, hooks=[], timeout=sandbox_runner.INTERACTION_HOOK_TIMEOUT_S
    )
    assert matcher.timeout is not None
    assert matcher.timeout > 60.0, "an unset-or-default timeout is the defect"
    # And the broker's own wait has to give up first, or the CLI's abort wins the race.
    ceiling = sandbox_runner.INTERACTION_HOOK_TIMEOUT_S - sandbox_runner._INTERACTION_HOOK_MARGIN_S
    assert ceiling < sandbox_runner.INTERACTION_HOOK_TIMEOUT_S
    assert ceiling > sandbox_runner.DEFAULT_INTERACTION_WAIT_S


async def test_the_budget_restarts_when_the_host_comes_back() -> None:
    # A reattach means someone is listening again, so the countdown that a
    # disconnect started must not carry over — otherwise a brief blip during a
    # long human decision still throws the decision away.
    link = FakeLink()
    broker = InteractionBroker(EnvelopeSender(link, "s"), wait_budget_s=1.5)
    task = asyncio.create_task(broker.pre_tool_use({"tool_name": "Bash"}, None, None))
    await asyncio.sleep(0.05)
    link.connected = False
    await asyncio.sleep(1.2)          # most of the budget, host down
    link.connected = True             # reattach
    await asyncio.sleep(1.2)          # past the original deadline
    assert not task.done(), "the reattach must have reset the countdown"

    interaction_id = link.frames[-1]["interaction_id"]
    broker.answer(interaction_id, InteractionAnswer(decision="allow"))
    assert (await task)["hookSpecificOutput"]["permissionDecision"] == "allow"


async def test_bypass_permissions_allows_without_touching_the_wire() -> None:
    # The CLI runs with its permission engine disarmed — the hook is the ONE
    # gate — so bypassPermissions lives here: every tool is allowed locally,
    # and NOTHING goes on the wire. Without this a bypass session parks on
    # every tool call and waits for an answer nobody is expected to give
    # (e2e turns wedge WAITING_INPUT for good).
    link = FakeLink()
    broker = InteractionBroker(
        EnvelopeSender(link, "s"), wait_budget_s=5.0,
        permission_mode="bypassPermissions",
    )
    out = (await broker.pre_tool_use({"tool_name": "Bash"}, None, None))[
        "hookSpecificOutput"
    ]
    assert out["permissionDecision"] == "allow"
    assert link.frames == [], "bypass must not surface an interaction to the host"


async def test_accept_edits_allows_the_edit_set_and_asks_for_the_rest() -> None:
    link = FakeLink()
    broker = InteractionBroker(
        EnvelopeSender(link, "s"), wait_budget_s=0.02,
        permission_mode="acceptEdits",
    )
    out = (await broker.pre_tool_use({"tool_name": "Write"}, None, None))[
        "hookSpecificOutput"
    ]
    assert out["permissionDecision"] == "allow"
    assert link.frames == [], "an auto-accepted edit must not reach the host"

    # A non-edit tool still asks. The ask goes out while a host is listening —
    # which is what this test is about — and then the host goes away, which is
    # what ends the wait: the budget runs on the host's absence, not on the
    # user's silence.
    task = asyncio.create_task(broker.pre_tool_use({"tool_name": "Bash"}, None, None))
    await asyncio.sleep(0.05)
    assert link.frames, "a non-edit tool under acceptEdits must still ask the host"
    link.connected = False
    out = (await task)["hookSpecificOutput"]
    assert out["permissionDecision"] == "defer"


async def test_mode_switch_takes_effect_on_the_live_broker() -> None:
    # set_permission_mode mid-conversation: the next gate decision follows the
    # NEW mode — the same op that updates the SDK updates the broker.
    link = FakeLink()
    link.connected = False  # the ask ends in defer; the mode is what is asserted
    broker = InteractionBroker(EnvelopeSender(link, "s"), wait_budget_s=0.02)
    out = (await broker.pre_tool_use({"tool_name": "Bash"}, None, None))[
        "hookSpecificOutput"
    ]
    assert out["permissionDecision"] == "defer"  # default mode asks
    broker.set_permission_mode("bypassPermissions")
    out = (await broker.pre_tool_use({"tool_name": "Bash"}, None, None))[
        "hookSpecificOutput"
    ]
    assert out["permissionDecision"] == "allow"


async def test_answer_for_unknown_interaction_reports_false() -> None:
    link = FakeLink()
    broker = InteractionBroker(EnvelopeSender(link, "s"), wait_budget_s=0.02)
    assert broker.answer("never-issued", InteractionAnswer(decision="allow")) is False


# --- spool store ------------------------------------------------------------


KEY = {"project_key": "p", "session_id": "s"}


class FlushTarget:
    def __init__(self) -> None:
        self.batches: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        self.append_ids: list[str] = []
        self.fail = False

    async def flush(
        self, key: dict[str, Any], entries: list[dict[str, Any]], append_id: str
    ) -> None:
        if self.fail:
            raise ConnectionError("host unreachable")
        self.batches.append((key, entries))
        self.append_ids.append(append_id)

    async def load(self, key: dict[str, Any]) -> list[dict[str, Any]] | None:
        out = [e for k, es in self.batches if k == key for e in es]
        return out or None

    async def list_subkeys(self, key: dict[str, Any]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for batch_key, _entries in self.batches:
            if (
                batch_key.get("project_key") != key.get("project_key")
                or batch_key.get("session_id") != key.get("session_id")
            ):
                continue
            subpath = batch_key.get("subpath")
            if isinstance(subpath, str) and subpath and subpath not in seen:
                seen.add(subpath)
                out.append(subpath)
        return out


def _store(tmp_path: Path, target: FlushTarget) -> SpoolSessionStore:
    return SpoolSessionStore(
        tmp_path / "spool",
        flush_fn=target.flush,
        load_fn=target.load,
        list_subkeys_fn=target.list_subkeys,
        retry_delay_s=0.01,
    )


async def test_append_acks_locally_before_any_flush(tmp_path: Path) -> None:
    target = FlushTarget()
    store = _store(tmp_path, target)
    await store.append(KEY, [{"type": "user", "uuid": "u1"}])
    assert store.pending_batch_count() == 1
    assert target.batches == [], "ack must not depend on the host being up"


async def test_resume_lists_durable_and_spooled_subagent_transcripts(
    tmp_path: Path,
) -> None:
    """A replacement runner must let the SDK restore every child transcript."""

    target = FlushTarget()

    async def list_subkeys(key: dict[str, Any]) -> list[str]:
        assert key == KEY
        return ["subagents/agent-durable"]

    store = SpoolSessionStore(
        tmp_path / "spool",
        flush_fn=target.flush,
        load_fn=target.load,
        list_subkeys_fn=list_subkeys,
    )
    await store.append(
        {**KEY, "subpath": "subagents/agent-durable"},
        [{"type": "assistant", "uuid": "durable-tail"}],
    )
    await store.append(
        {**KEY, "subpath": "subagents/agent-spooled"},
        [{"type": "assistant", "uuid": "spooled-only"}],
    )
    await store.append(
        KEY,
        [{"type": "user", "uuid": "main-tail"}],
    )

    assert await store.list_subkeys(KEY) == [
        "subagents/agent-durable",
        "subagents/agent-spooled",
    ], (
        "the SDK materializes only subpaths returned by list_subkeys; omit a "
        "durable or fsync-spooled child and sandbox replacement loses that "
        "child's resumable history"
    )


async def test_list_subkeys_keeps_child_when_durable_snapshot_races_flush(
    tmp_path: Path,
) -> None:
    """A stale durable snapshot must not hide a child unlinked by the flusher."""

    target = FlushTarget()
    durable_snapshot_taken = asyncio.Event()
    release_durable_snapshot = asyncio.Event()

    async def blocked_list_subkeys(key: dict[str, Any]) -> list[str]:
        snapshot = await target.list_subkeys(key)
        assert snapshot == []
        durable_snapshot_taken.set()
        await release_durable_snapshot.wait()
        return snapshot

    store = SpoolSessionStore(
        tmp_path / "spool",
        flush_fn=target.flush,
        load_fn=target.load,
        list_subkeys_fn=blocked_list_subkeys,
    )
    child_subpath = "subagents/agent-racing-flush"
    await store.append(
        {**KEY, "subpath": child_subpath},
        [{"type": "assistant", "uuid": "child-tail"}],
    )
    listed = asyncio.create_task(store.list_subkeys(KEY))

    try:
        await asyncio.wait_for(durable_snapshot_taken.wait(), timeout=1)
        assert await store.flush_once() == 1
        assert store.pending_batch_count() == 0
    finally:
        release_durable_snapshot.set()

    assert await listed == [child_subpath]


async def test_vendor_resume_materializes_child_transcript_from_spool_store(
    tmp_path: Path,
) -> None:
    """Exercise the pinned SDK's real feature-detection and restore path."""

    sdk_session_id = str(uuid.uuid4())
    child_subpath = "subagents/agent-child"
    main_entries = [{"type": "user", "uuid": "main-entry"}]
    child_entries = [{"type": "assistant", "uuid": "child-entry"}]
    target = FlushTarget()

    async def load(key: dict[str, Any]) -> list[dict[str, Any]] | None:
        if key.get("session_id") != sdk_session_id:
            return None
        if key.get("subpath") == child_subpath:
            return child_entries
        if "subpath" not in key:
            return main_entries
        return None

    async def list_subkeys(key: dict[str, Any]) -> list[str]:
        assert key.get("session_id") == sdk_session_id
        assert "subpath" not in key
        return [child_subpath]

    store = SpoolSessionStore(
        tmp_path / "spool",
        flush_fn=target.flush,
        load_fn=load,
        list_subkeys_fn=list_subkeys,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    materialized = await materialize_resume_session(
        ClaudeAgentOptions(
            cwd=str(workspace),
            resume=sdk_session_id,
            session_store=store,
        )
    )

    assert materialized is not None
    try:
        project_dirs = list((materialized.config_dir / "projects").iterdir())
        assert len(project_dirs) == 1
        child_file = (
            project_dirs[0]
            / sdk_session_id
            / "subagents"
            / "agent-child.jsonl"
        )
        restored = [
            json.loads(line)
            for line in child_file.read_text(encoding="utf-8").splitlines()
        ]
        assert restored == child_entries
    finally:
        await materialized.cleanup()


async def test_append_waits_for_disk_without_blocking_the_event_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The SDK sees success only after durability, while other runner work proceeds."""
    target = FlushTarget()
    store = _store(tmp_path, target)
    loop = asyncio.get_running_loop()
    write_started = asyncio.Event()
    release_write = threading.Event()
    original_write = store._write_batch

    def slow_write(
        name: str, key: dict[str, Any], entries: list[dict[str, Any]]
    ) -> None:
        loop.call_soon_threadsafe(write_started.set)
        if not release_write.wait(timeout=1):
            raise TimeoutError("test did not release the durable spool write")
        original_write(name, key, entries)

    monkeypatch.setattr(store, "_write_batch", slow_write)
    append_task = asyncio.create_task(
        store.append(KEY, [{"type": "user", "uuid": "u1"}])
    )

    try:
        await asyncio.wait_for(write_started.wait(), timeout=1)
        sentinel = asyncio.Event()
        loop.call_soon(sentinel.set)
        await asyncio.wait_for(sentinel.wait(), timeout=1)

        assert not append_task.done(), (
            "a frame cannot be acknowledged while its durable write is unfinished"
        )
        assert store.pending_batch_count() == 0, (
            "a process death here must leave the frame unacknowledged, not falsely durable"
        )
    finally:
        release_write.set()
        await asyncio.gather(append_task, return_exceptions=True)

    assert store.pending_batch_count() == 1, "the ack follows the atomic spool rename"


async def test_flush_failure_keeps_batch_for_retry(tmp_path: Path) -> None:
    target = FlushTarget()
    target.fail = True
    store = _store(tmp_path, target)
    await store.append(KEY, [{"type": "user", "uuid": "u1"}])
    with pytest.raises(ConnectionError):
        await store.flush_once()
    assert store.pending_batch_count() == 1
    target.fail = False
    assert await store.flush_once() == 1
    assert store.pending_batch_count() == 0
    assert target.batches[0][1] == [{"type": "user", "uuid": "u1"}]
    assert target.append_ids[0], "a retried batch reaches the platform identified"


async def test_result_compacts_after_async_spool_drain_without_blocking(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A late platform ack closes the Result compaction decision."""

    monkeypatch.setattr(EnvelopeSender, "_JOURNAL_COMPACTION_THRESHOLD", 1)
    flush_started = asyncio.Event()
    release_flush = asyncio.Event()
    confirmed = {"sequence": 0}

    async def blocked_flush(
        key: dict[str, Any], entries: list[dict[str, Any]], append_id: str
    ) -> None:
        assert key == KEY
        assert entries
        assert append_id
        flush_started.set()
        await release_flush.wait()
        confirmed["sequence"] = 1

    async def empty_load(_key: dict[str, Any]) -> None:
        return None

    async def empty_subkeys(_key: dict[str, Any]) -> list[str]:
        return []

    store = SpoolSessionStore(
        tmp_path / "spool",
        flush_fn=blocked_flush,
        load_fn=empty_load,
        list_subkeys_fn=empty_subkeys,
        sequence_fn=lambda: confirmed["sequence"],
        retry_delay_s=0.01,
    )
    await store.append(KEY, [{"type": "assistant", "uuid": "u1"}])
    session, link, _client = _session(
        [AssistantMessage(content="stored later"), ResultMessage()], store
    )
    caplog.set_level(logging.INFO, logger="astrabox.sandbox_runner")

    async def result_event() -> dict[str, Any]:
        while True:
            result = next(
                (
                    frame
                    for frame in link.frames
                    if frame.get("message_type") == "ResultMessage"
                ),
                None,
            )
            if result is not None:
                return result
            await asyncio.sleep(0.01)

    try:
        await session.start()
        await asyncio.wait_for(flush_started.wait(), timeout=1)
        await session.submit(_delivery("cmd-1", "go"))
        result = await asyncio.wait_for(result_event(), timeout=1)

        assert store.pending_batch_count() == 1
        assert session.sender.first_retained_seq == 1, (
            "Result must not wait for or compact ahead of the platform ack"
        )

        for frame in link.frames:
            if (
                frame.get("requires_persistence") is True
                and int(frame["seq"]) < int(result["seq"])
            ):
                await session.sender.acknowledge_event_persistence(
                    int(frame["seq"])
                )
        release_flush.set()
        await asyncio.wait_for(
            _wait_for_result_compaction(session, int(result["seq"])), timeout=1
        )

        assert session.sender.cursor_window(0).gap is True
        assert "runner journal compacted:" in caplog.text
    finally:
        release_flush.set()
        await session.stop()


async def _wait_for_result_compaction(
    session: RunnerSession, sequence: int
) -> None:
    while session.sender.first_retained_seq != sequence:
        await asyncio.sleep(0.01)


async def test_runner_restart_reflushes_spool_left_behind(tmp_path: Path) -> None:
    target = FlushTarget()
    first = _store(tmp_path, target)
    await first.append(KEY, [{"type": "user", "uuid": "u1"}])
    # Box process dies before flushing; a fresh runner over the same dir
    # must deliver the acked batch with no handshake.
    second = _store(tmp_path, target)
    assert await second.flush_once() == 1
    assert target.batches[0][1][0]["uuid"] == "u1"


async def test_load_appends_unflushed_tail_for_same_key(tmp_path: Path) -> None:
    target = FlushTarget()
    store = _store(tmp_path, target)
    await store.append(KEY, [{"type": "user", "uuid": "u1"}])
    await store.flush_once()
    await store.append(KEY, [{"type": "assistant", "uuid": "u2"}])
    await store.append({**KEY, "subpath": "subagents/agent-1"}, [{"type": "x", "uuid": "s1"}])
    loaded = await store.load(KEY)
    assert loaded is not None
    assert [e["uuid"] for e in loaded] == ["u1", "u2"], "resume racing the flusher sees its tail"


# --- event pump -------------------------------------------------------------


async def test_pump_envelopes_events_and_settles_idle_on_result() -> None:
    scripted = [AssistantMessage(content="hi"), ResultMessage(subtype="success")]
    session, link, _client = _session(scripted)
    await session.start()
    await session.submit(_delivery("cmd-1", "go"))
    await asyncio.sleep(0.05)
    ops = [(f["op"], f.get("message_type"), f.get("state")) for f in link.frames]
    assert ("event", "AssistantMessage", None) in ops
    assert ("event", "ResultMessage", None) in ops
    assert ops[-1] == ("status", None, "idle"), "result settles the envelope to idle"
    await session.stop()


async def test_deferred_tool_use_rides_the_result_event_itself() -> None:
    # No separate op: it was redundant (the field is ON the ResultMessage) and
    # unreachable (sent after the result, past the host iterator's return).
    # The host reads it off the result event, which is the one frame the turn
    # is guaranteed to consume.
    deferred = {"id": "t1", "name": "Bash", "input": {"command": "rm -rf x"}}
    scripted = [ResultMessage(subtype="success", deferred_tool_use=deferred)]
    session, link, _client = _session(scripted)
    await session.start()
    await session.submit(_delivery("cmd-1", "go"))
    await asyncio.sleep(0.05)
    assert [f for f in link.frames if f["op"] == "deferred"] == []
    (result_event,) = [
        f for f in link.frames
        if f["op"] == "event" and f.get("message_type") == "ResultMessage"
    ]
    assert result_event["message"]["deferred_tool_use"]["name"] == "Bash"
    await session.stop()


async def test_spool_batch_files_are_self_describing_json(tmp_path: Path) -> None:
    # The restart-replay contract depends on the on-disk format carrying its
    # own key; guard the format, not the implementation detail of the name.
    target = FlushTarget()
    store = _store(tmp_path, target)
    await store.append(KEY, [{"type": "user", "uuid": "u1"}])
    (batch_file,) = list((tmp_path / "spool").glob("*.batch.json"))
    batch = json.loads(batch_file.read_text(encoding="utf-8"))
    assert batch["key"] == KEY
    assert batch["entries"][0]["uuid"] == "u1"


def test_interrupt_releases_pending_interaction_gates() -> None:
    # SDK interrupt cannot finish aborting while a PreToolUse hook is blocked
    # on the broker — interrupt must resolve every pending gate as a deny so
    # the hook returns and the abort proceeds.
    async def run() -> None:
        session, link, sdk = _session()
        await session.start()
        await session.submit(_delivery("cmd-1", "do it"))
        gate = asyncio.ensure_future(
            session.broker.pre_tool_use(
                {"tool_name": "Bash", "tool_input": {"command": "ls"}}, None, None
            )
        )
        await asyncio.sleep(0.01)  # let the gate park on its future
        await session.interrupt("cmd-1")
        decision = await asyncio.wait_for(gate, timeout=1)
        assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert sdk.interrupted == 1
        await session.stop()

    asyncio.run(run())


async def test_a_poll_tick_never_escapes_the_gate() -> None:
    """The wait's poll timeout is a return value, not an exception.

    Python 3.10 distinguishes asyncio.TimeoutError from the built-in class.
    Pairing `wait_for` with the built-in exception can let a poll timeout escape
    the hook, after which the CLI's bypass mode may execute an unapproved tool.
    The wait must survive an answer that arrives after many poll periods.
    """
    link = FakeLink()
    broker = InteractionBroker(EnvelopeSender(link, "s"), wait_budget_s=5.0)
    task = asyncio.create_task(
        broker.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": "x"}}, "t1", None)
    )
    await asyncio.sleep(0)
    interaction_id = link.frames[-1]["interaction_id"]
    await asyncio.sleep(0.18)  # More than three polls; the gate must still wait.
    assert not task.done(), "the gate must still be waiting, not errored out"
    assert broker.answer(interaction_id, InteractionAnswer(decision="allow"))
    out = (await task)["hookSpecificOutput"]
    assert out["permissionDecision"] == "allow"


async def test_a_gate_that_errors_denies_instead_of_leaking() -> None:
    """Fail CLOSED: an exception inside the gate must become a deny decision.

    Under the CLI's bypass base mode an errored hook falls through to "run
    it", so an exception escaping the gate executes an unapproved tool. The
    belt converts any unexpected error into the one answer that cannot be
    wrong.
    """

    class _ExplodingSender:
        host_connected = True

        async def send(self, *args, **kwargs):
            raise RuntimeError("wire fell over")

    broker = InteractionBroker(_ExplodingSender(), wait_budget_s=5.0)  # type: ignore[arg-type]
    out = await broker.pre_tool_use(
        {"tool_name": "Bash", "tool_input": {"command": "x"}}, "t1", None
    )

    hook = out["hookSpecificOutput"]
    assert hook["permissionDecision"] == "deny"
    assert "permission gate error" in hook["permissionDecisionReason"]
