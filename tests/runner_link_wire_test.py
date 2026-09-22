"""Runner wire protocol — RunnerWsServer and RunnerLink over real websockets.

Integration at the protocol layer: a real server on an ephemeral port, a real
client, a fake SDK session behind the server. What must hold:

* handshake discipline — prepare then activate on a fresh slot, attach later,
  anything else refused loudly;
* protocol identity — the host accepts only the current runner protocol before
  routing any replay frames;
* the reattach truth model — the runner replays its retained journal strictly
  after the host cursor; an expired cursor produces one ordered ``gap`` before
  the retained suffix so the consumer can rebuild from SessionStore;
* interaction round trip — the broker's PreToolUse wait resolves from a
  frame the host sends, and the host does not call an answer delivered until
  the runner acknowledges whether that interaction still exists.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from unittest import mock
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from astrabox.core.service.orchestrator import sandbox_runner as sandbox_runner_module
from astrabox.core.service.orchestrator.engine.base import EngineStreamDetached
from astrabox.core.service.orchestrator.engine.runner_link import (
    DeliveryCommand,
    RUNNER_PROTOCOL as HOST_RUNNER_PROTOCOL,
    RunnerLink as _RunnerLink,
    RunnerLinkError,
)
from astrabox.core.service.orchestrator.sandbox_runner import (
    EnvelopeSender,
    HistoryStoreSequence,
    HostLink,
    RUNNER_PROTOCOL as IN_BOX_RUNNER_PROTOCOL,
    RunnerSession,
    RunnerWsServer,
)


async def _accept_persistent_event(_frame: dict[str, Any]) -> None:
    pass


class RunnerLink(_RunnerLink):
    def __init__(self, uri: str, **kwargs: Any) -> None:
        kwargs.setdefault("persistent_event_handler", _accept_persistent_event)
        super().__init__(uri, **kwargs)


class QueueSdkSession:
    """Fake SDK session the test feeds messages into explicitly."""

    def __init__(self) -> None:
        self.queries: list[Any] = []
        self.openings: list[dict[str, Any]] = []
        self.store_bindings: list[tuple[str, dict[str, Any] | None]] = []
        self.interrupted = 0
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self.consume_prompt: Any = None

    def emit(self, message: Any) -> None:
        self._queue.put_nowait(message)

    def bind_store(self, session_id: str, store: dict[str, Any] | None) -> None:
        self.store_bindings.append((session_id, dict(store) if store else None))

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

    async def receive_messages(self):  # noqa: ANN201 — async generator protocol
        while True:
            yield await self._queue.get()

    async def interrupt(self) -> None:
        self.interrupted += 1

    async def disconnect(self) -> None:
        pass


class _UnacknowledgingWebSocket:
    """A transport that accepts bytes but never proves runner receipt."""

    async def send(self, payload: str) -> None:
        _ = payload


def _assistant_message(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextBlock(text=text)],
        model="test-model",
        session_id="sdk-session",
        uuid=str(uuid.uuid4()),
    )


def _result_message() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="sdk-session",
    )


@pytest.fixture
async def wire():
    client = QueueSdkSession()

    def factory(opening: dict[str, Any], link: HostLink) -> RunnerSession:
        client.openings.append(opening)
        session = RunnerSession(
            session_id=str(opening["slot_id"]),
            link=link,
            client_factory=lambda broker: client,
            activation_callback=client.bind_store,
            interaction_wait_s=5.0,
        )
        client.consume_prompt = session.on_user_prompt_submit
        return session

    server = RunnerWsServer(host="127.0.0.1", port=0, session_factory=factory)
    await server.start()
    try:
        yield server, client, f"ws://127.0.0.1:{server.port}/"
    finally:
        await server.stop()


async def _next_op(frames: Any, wanted: str) -> dict[str, Any]:
    async for frame in frames:
        if frame["op"] == wanted:
            return frame
    raise AssertionError(f"stream ended before op={wanted}")


async def _next_message_type(frames: Any, wanted: str) -> dict[str, Any]:
    async for frame in frames:
        if frame.get("op") == "event" and frame.get("message_type") == wanted:
            return frame
    raise AssertionError(f"stream ended before message_type={wanted}")


def _as_queried(inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Everything the vendor was handed except the message id it is given per
    attempt — the platform input id travels under its own identity."""

    return [
        {key: value for key, value in item.items() if key != "uuid"}
        for item in inputs
    ]


def _delivery(
    command_id: str,
    content: str,
    *,
    sequence: int = 1,
) -> DeliveryCommand:
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


async def test_configure_input_and_events_flow(wire) -> None:
    server, sdk, uri = wire
    async with RunnerLink(uri) as link:
        hello = await link.configure("sess-1", options={})
        assert (
            hello["protocol"]
            == HOST_RUNNER_PROTOCOL
            == IN_BOX_RUNNER_PROTOCOL
        )
        assert hello["engine_contract"]["adapter"] == "claude_code"
        assert hello["engine_contract"]["sdk_version"]
        assert "permission_mode" in hello["engine_contract"]["accepted_option_keys"]
        assert "default" in hello["engine_contract"]["permission_modes"]
        # Activation emits the initial idle status before hello goes out, so
        # the stream position at hello time is already 1.
        assert hello["last_seq"] == 1
        assert sdk.store_bindings == [("sess-1", None)]

        command = _delivery("cmd-1", "hello box")
        ack = await link.deliver(command)
        assert ack["duplicate"] is False
        assert _as_queried(sdk.queries) == _as_queried([command.sdk_input])

        sdk.emit(_assistant_message("hi"))
        frames = link.frames()
        event = await _next_message_type(frames, "AssistantMessage")
        assert event["message_type"] == "AssistantMessage"
        assert event["session_id"] == "sess-1"


async def test_configure_resume_gives_prepare_its_platform_transcript(wire) -> None:
    _server, sdk, uri = wire
    store = {
        "base_url": "http://platform.internal",
        "headers": {"Authorization": "Bearer box-token"},
    }

    async with RunnerLink(uri) as link:
        await link.configure(
            "platform-session",
            options={"resume": "sdk-session"},
            store=store,
        )

    assert sdk.openings[-1]["options"]["resume_transcript"] == {
        "platform_session_id": "platform-session",
        "store": store,
    }
    assert sdk.store_bindings == [("platform-session", store)]
    assert "resume_transcript" in sdk.openings[-1]["engine_requirements"][
        "required_option_keys"
    ]


async def test_prepare_has_no_session_frames_and_activation_is_single_use(wire) -> None:
    server, sdk, uri = wire
    async with RunnerLink(uri) as warmer:
        prepared = await warmer.prepare(
            "slot-1", activation_token="slot-1-secret", options={}
        )
        assert prepared["slot_id"] == "slot-1"
        assert server.session is not None
        assert server.session.is_prepared is True
        assert server.session.is_active is False
        assert server.session.sender.last_seq == 0
        assert "resume_transcript" not in sdk.openings[-1]["options"]
        assert sdk.queries == [], "preparation must not manufacture model input"

    async with RunnerLink(uri) as claimant:
        hello = await claimant.activate(
            "slot-1",
            "sess-1",
            activation_token="slot-1-secret",
            required_option_keys=(),
            permission_mode="default",
        )
        assert hello["session_id"] == "sess-1"
        assert hello["last_seq"] == 1
        assert server.session is not None
        active = server.session
        assert active.is_active is True

        async with RunnerLink(uri) as duplicate:
            with pytest.raises(RunnerLinkError, match="active runner"):
                await duplicate.activate(
                    "slot-1",
                    "sess-2",
                    activation_token="slot-1-secret",
                    required_option_keys=(),
                    permission_mode="default",
                )
        assert server.session is active, "a duplicate claim must not kill its owner"


async def test_permission_mode_returns_only_after_the_runner_applies_it(wire) -> None:
    server, _sdk, uri = wire
    async with RunnerLink(uri) as link:
        await link.configure("sess-1")
        assert server.session is not None
        entered = asyncio.Event()
        release = asyncio.Event()

        async def apply_mode(mode: str) -> None:
            assert mode == "bypassPermissions"
            entered.set()
            await release.wait()

        server.session.set_permission_mode = apply_mode  # type: ignore[method-assign]
        change = asyncio.create_task(link.set_permission_mode("bypassPermissions"))
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        try:
            assert not change.done(), "the host must wait for the runner's apply receipt"
        finally:
            release.set()
            await asyncio.wait_for(change, timeout=1.0)


@pytest.mark.parametrize(
    "protocol",
    ["astrabox-runner-v1", "astrabox.runner-wire.v999", None],
    ids=["old", "unknown", "missing"],
)
async def test_attach_rejects_a_protocol_mismatch_before_persisting_replay(
    wire,
    monkeypatch: pytest.MonkeyPatch,
    protocol: str | None,
) -> None:
    server, _sdk, uri = wire
    async with RunnerLink(uri) as first:
        await first.configure("sess-1")
        assert server.session is not None
        await server.session.sender.send(
            "event",
            message_type="AssistantMessage",
            message={"__sdk_type": "AssistantMessage"},
            requires_persistence=True,
        )

        original_send = sandbox_runner_module._WsHostLink.send

        async def mutate_hello(self: Any, frame: dict[str, Any]) -> bool:
            outgoing = dict(frame)
            if outgoing.get("op") == "hello":
                if protocol is None:
                    outgoing.pop("protocol", None)
                else:
                    outgoing["protocol"] = protocol
            return await original_send(self, outgoing)

        monkeypatch.setattr(sandbox_runner_module._WsHostLink, "send", mutate_hello)
        persisted: list[dict[str, Any]] = []

        async def record_persisted(frame: dict[str, Any]) -> None:
            persisted.append(frame)

        async with RunnerLink(
            uri,
            persistent_event_handler=record_persisted,
        ) as incompatible:
            with pytest.raises(RunnerLinkError) as caught:
                await incompatible.attach("sess-1", last_seen_seq=0)

        assert str(caught.value) == (
            "runner protocol mismatch "
            f"expected={HOST_RUNNER_PROTOCOL!r} actual={protocol!r}"
        )
        assert persisted == []


async def test_configure_rejects_host_options_missing_from_the_image_contract(
    wire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, _sdk, uri = wire
    contract = sandbox_runner_module._runner_engine_contract()
    monkeypatch.setattr(
        sandbox_runner_module,
        "_runner_engine_contract",
        lambda: {
            **contract,
            "accepted_option_keys": ["permission_mode"],
        },
    )

    async with RunnerLink(uri) as link:
        with pytest.raises(
            RunnerLinkError,
            match="does not accept required Claude options.*model",
        ):
            await link.configure(
                "sess-1",
                options={"model": "claude-sonnet", "permission_mode": "default"},
            )

    assert server.session is None


async def test_attach_rejects_a_runner_that_omits_its_engine_contract(
    wire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, _sdk, uri = wire
    async with RunnerLink(uri) as first:
        await first.configure("sess-1")
        assert server.session is not None

        original_send = sandbox_runner_module._WsHostLink.send

        async def remove_contract(self: Any, frame: dict[str, Any]) -> bool:
            outgoing = dict(frame)
            if outgoing.get("op") == "hello":
                outgoing.pop("engine_contract", None)
            return await original_send(self, outgoing)

        monkeypatch.setattr(sandbox_runner_module._WsHostLink, "send", remove_contract)
        async with RunnerLink(uri) as incompatible:
            with pytest.raises(RunnerLinkError, match="has no engine_contract"):
                await incompatible.attach("sess-1", last_seen_seq=0)


async def test_unconfirmed_input_reattaches_redelivers_and_completes_turn(
    wire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A StartTurn crossing a link exchange still executes exactly once.

    The first write is accepted by the websocket but never reaches the runner,
    reproducing the missing dispatch receipt from the retained field case. A
    second attach owns runner output at that instant. The platform link must
    reattach by cursor, redeliver the same command id, and receive the turn's
    terminal on that new connection.
    """
    server, sdk, uri = wire
    original_dispatch = server._dispatch
    dropped_first_input = False

    async def drop_first_input(frame: dict[str, Any], link: Any) -> None:
        nonlocal dropped_first_input
        if frame.get("op") == "input" and not dropped_first_input:
            dropped_first_input = True
            return
        await original_dispatch(frame, link)

    monkeypatch.setattr(server, "_dispatch", drop_first_input)

    async with RunnerLink(uri) as platform_link:
        await platform_link.configure("sess-1")
        async with RunnerLink(uri) as superseding_link:
            await superseding_link.attach("sess-1", last_seen_seq=0)

            command = _delivery("cmd-after-link-swap", "finish this turn")
            ack = await platform_link.deliver(
                command,
                timeout_s=0.02,
            )

            assert dropped_first_input is True
            assert ack["duplicate"] is False
            assert _as_queried(sdk.queries) == _as_queried([command.sdk_input]), (
                "redelivery uses one command identity and must not double-run"
            )

            sdk.emit(_assistant_message("done"))
            sdk.emit(_result_message())
            frames = platform_link.frames()
            assistant = await _next_message_type(frames, "AssistantMessage")
            terminal = await _next_message_type(frames, "ResultMessage")
            idle = await _next_op(frames, "status")

            assert assistant["message_type"] == "AssistantMessage"
            assert terminal["message_type"] == "ResultMessage"
            assert "command_id" not in terminal
            assert idle["state"] == "idle"


async def test_input_receipt_connection_loss_rebuilds_link_with_a_fresh_budget(
    wire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport restart during StartTurn receipt wait is recoverable.

    Each connection lives for most of one receipt budget. The command can only
    complete if the first close rebuilds the link, the redelivery uses that new
    link, and its acknowledgement gets a fresh budget rather than the remainder
    of the first one. The terminal then proves the stale EOF from the replaced
    connection cannot end the rebuilt stream.
    """
    server, sdk, uri = wire
    original_dispatch = server._dispatch
    input_links: list[Any] = []

    async def restart_during_first_receipt(
        frame: dict[str, Any], link: Any
    ) -> None:
        if frame.get("op") == "input":
            input_links.append(link)
            await asyncio.sleep(0.12)
            if len(input_links) == 1:
                await link._ws.close()
                return
        await original_dispatch(frame, link)

    monkeypatch.setattr(server, "_dispatch", restart_during_first_receipt)

    async with RunnerLink(uri) as link:
        await link.configure("sess-1")
        command = _delivery("cmd-across-runner-restart", "finish once")
        ack = await link.deliver(
            command,
            timeout_s=0.2,
        )

        assert ack["duplicate"] is False
        assert len(input_links) == 2
        assert input_links[0] is not input_links[1], (
            "redelivery must use the connection established by recovery"
        )
        assert _as_queried(sdk.queries) == _as_queried([command.sdk_input])

        sdk.emit(_assistant_message("done"))
        sdk.emit(_result_message())
        frames = link.frames()
        assistant = await _next_message_type(frames, "AssistantMessage")
        terminal = await _next_message_type(frames, "ResultMessage")

        assert assistant["message_type"] == "AssistantMessage"
        assert terminal["message_type"] == "ResultMessage"
        assert "command_id" not in terminal


async def test_input_receipt_sent_to_superseding_link_replays_on_reattach(wire) -> None:
    """An accepted input replays its original receipt instead of running twice."""
    _server, sdk, uri = wire

    async with RunnerLink(uri) as platform_link:
        await platform_link.configure("sess-1")
        async with RunnerLink(uri) as superseding_link:
            await superseding_link.attach("sess-1", last_seen_seq=0)

            command = _delivery("cmd-with-orphaned-ack", "run once")
            ack = await platform_link.deliver(
                command,
                timeout_s=0.02,
            )

            assert ack["duplicate"] is False, (
                "reattach must replay the first receipt before considering redelivery"
            )
            assert _as_queried(sdk.queries) == _as_queried([command.sdk_input])


async def test_input_missing_both_receipts_fails_within_the_dispatch_budget(
    wire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-dispatch silence cannot fall through to the 90-second bridge watch."""
    server, sdk, uri = wire
    original_dispatch = server._dispatch

    async def drop_inputs(frame: dict[str, Any], link: Any) -> None:
        if frame.get("op") != "input":
            await original_dispatch(frame, link)

    monkeypatch.setattr(server, "_dispatch", drop_inputs)

    async with RunnerLink(uri) as link:
        await link.configure("sess-1")
        original_reattach = link._reattach_for_unconfirmed_input
        reattach_attempted = asyncio.Event()

        async def observe_reattach(command_id: str, *, timeout_s: float) -> None:
            reattach_attempted.set()
            await original_reattach(command_id, timeout_s=timeout_s)

        monkeypatch.setattr(link, "_reattach_for_unconfirmed_input", observe_reattach)
        async with asyncio.timeout(2.0):
            with pytest.raises(
                EngineStreamDetached,
                match=r"runner did not acknowledge (?:reattach for input|input .* after reattach)",
            ):
                await link.deliver(
                    _delivery("cmd-never-received", "lost"),
                    timeout_s=0.05,
                )

        assert reattach_attempted.is_set()

    assert sdk.queries == []


async def test_a_second_opening_never_displaces_a_live_session(wire) -> None:
    """A runner already holding a session refuses to open a second one.

    The refusal names the barrier that failed rather than the composite the
    host called: cold starts compose prepare then activate, and a runner whose
    slot is occupied stops at prepare — the host that wants this session's
    stream asks for it by attaching."""

    _server, _sdk, uri = wire
    async with RunnerLink(uri) as first:
        await first.configure("sess-1")
        async with RunnerLink(uri) as second:
            with pytest.raises(RunnerLinkError, match="non-empty runner slot"):
                await second.configure("sess-1")


async def test_interaction_round_trip_over_the_wire(wire) -> None:
    server, _sdk, uri = wire
    async with RunnerLink(uri) as link:
        await link.configure("sess-1")
        assert server.session is not None
        gate = asyncio.create_task(
            server.session.broker.pre_tool_use(
                {"tool_name": "Bash", "tool_input": {"command": "ls"}}, "t1", None
            )
        )
        interaction = await _next_op(link.frames(), "interaction")
        assert interaction["tool_name"] == "Bash"
        assert await link.answer(
            interaction["interaction_id"], "allow", updated_input={"command": "ls -a"}
        )
        out = (await gate)["hookSpecificOutput"]
        assert out["permissionDecision"] == "allow"
        assert out["updatedInput"] == {"command": "ls -a"}


async def test_answer_after_expiry_returns_the_runners_rejection(wire) -> None:
    _server, _sdk, uri = wire
    async with RunnerLink(uri) as link:
        await link.configure("sess-1")
        assert await link.answer("never-issued", "allow") is False


async def test_answer_without_runner_acknowledgement_fails_loudly() -> None:
    link = RunnerLink("ws://unused")
    link._ws = _UnacknowledgingWebSocket()

    with pytest.raises(EngineStreamDetached, match="did not acknowledge"):
        await link.answer("interaction-1", "allow", timeout_s=0.01)


async def test_detach_reattach_emits_ordered_gap_for_expired_cursor(
    wire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, sdk, uri = wire
    monkeypatch.setattr(EnvelopeSender, "_JOURNAL_COMPACTION_THRESHOLD", 1)

    async with RunnerLink(uri) as first:
        await first.configure("sess-1")
        sdk.emit(_assistant_message("before detach"))
        await _next_op(first.frames(), "event")
        seen = first.last_seen_seq

    # Host away; the session keeps producing. A store-covered Result compacts
    # only the complete prefix, leaving the Result itself as the retained suffix.
    assert server.session is not None
    sdk.emit(_assistant_message("while detached"))
    for _ in range(50):
        if server.session.sender.last_seq > seen:
            break
        await asyncio.sleep(0.01)
    assert server.session.sender.last_seq > seen
    result_sequence = await server.session.sender.send(
        "event",
        result_store_sequence=HistoryStoreSequence(11),
        message_type="ResultMessage",
        message={"__sdk_type": "ResultMessage"},
    )
    removed = await server.session.sender.compact_after_result(
        result_sequence,
        store_fully_flushed=True,
    )
    assert removed > 0
    assert server.session.sender.first_retained_seq == result_sequence

    async with RunnerLink(uri) as second:
        hello = await second.attach("sess-1", last_seen_seq=seen)
        assert hello["last_seq"] == result_sequence
        assert hello["first_retained_sequence"] == result_sequence
        gap = await _next_op(second.frames(), "gap")
        assert gap == {
            "op": "gap",
            "after_sequence": seen,
            "first_retained_sequence": result_sequence,
            "last_sequence": result_sequence,
        }
        terminal = await _next_op(second.frames(), "event")
        assert terminal["message_type"] == "ResultMessage"
        assert terminal["seq"] == result_sequence

        # The retained suffix and subsequent live stream remain contiguous.
        sdk.emit(_assistant_message("after reattach"))
        event = await _next_op(second.frames(), "event")
        assert event["seq"] == result_sequence + 1


async def test_interrupt_reaches_the_sdk_session(wire) -> None:
    server, sdk, uri = wire
    async with RunnerLink(uri) as link:
        await link.configure("sess-1")
        await link.deliver(_delivery("cmd-interrupt", "keep working"))
        await link.interrupt("cmd-interrupt")
        for _ in range(50):
            if sdk.interrupted:
                break
            await asyncio.sleep(0.01)
        assert sdk.interrupted == 1
        interrupted = await _next_op(link.frames(), "turn_interrupted")
        assert interrupted["command_id"] == "cmd-interrupt"


async def test_is_live_reports_the_links_death(wire) -> None:
    """The link's death certificate, both directions.

    True while the conversation can carry frames; False after close(), and —
    the case that matters — False once the PEER goes away: the runtime
    manager reads this before reusing a registered runtime, so a crashed
    runner's runtime is evicted instead of failing every later turn on the
    same closed websocket.
    """
    server, sdk, uri = wire
    link = RunnerLink(uri)
    await link.__aenter__()
    try:
        await link.configure("sess-1")
        assert link.is_live is True
        # The peer goes away: the server closes underneath the link.
        await server.stop()
        for _ in range(100):
            if not link.is_live:
                break
            await asyncio.sleep(0.01)
        assert link.is_live is False
    finally:
        await link.close()
    assert link.is_live is False


async def test_is_live_is_false_after_an_explicit_close(wire) -> None:
    server, sdk, uri = wire
    link = RunnerLink(uri)
    await link.__aenter__()
    await link.configure("sess-1")
    await link.close()
    assert link.is_live is False


async def test_attach_without_a_session_id_cannot_take_the_live_link(
    wire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unnamed attach is refused, and the configured host keeps the link.

    Under shared tenancy this is reachable without any secret: sibling
    conversations share the box network namespace and the runner port is
    derived from the conversation's uid, so a sibling can dial this port. An
    empty ``session_id`` is falsy, so a mismatch comparison guarded on the name
    being present does not run, and the newest connection wins the link
    unconditionally.

    The load-bearing assertion is the second one. Refusing the frame matters
    only because the session's live link survives it; a refusal that still
    evicted the host would leave the conversation dead in a different way.
    """

    server, sdk, uri = wire
    async with RunnerLink(uri) as platform_link:
        await platform_link.configure("sess-1")
        assert server.session is not None

        original_send_wire = _RunnerLink._send_wire

        async def drop_session_id(
            self: Any,
            payload: str,
            *,
            action: str,
        ) -> None:
            frame = json.loads(payload)
            if frame.get("op") == "attach":
                frame.pop("session_id", None)
            await original_send_wire(self, json.dumps(frame), action=action)

        # The frame is the producer's own, minus the one field under test, so
        # this cannot pass for a reason unrelated to the missing identity.
        monkeypatch.setattr(_RunnerLink, "_send_wire", drop_session_id)

        replayed: list[dict[str, Any]] = []

        async def record_replayed(frame: dict[str, Any]) -> None:
            replayed.append(frame)

        async with RunnerLink(
            uri,
            persistent_event_handler=record_replayed,
        ) as sibling:
            with pytest.raises(RunnerLinkError) as caught:
                await sibling.attach("sess-1", last_seen_seq=0)

        assert str(caught.value) == "attach missing session_id"
        assert replayed == []

        sdk.emit(_assistant_message("still the configured host"))
        event = await asyncio.wait_for(
            _next_message_type(platform_link.frames(), "AssistantMessage"),
            timeout=2.0,
        )
        assert event["session_id"] == "sess-1"


async def test_prepare_options_carry_the_deployment_host_absence_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runner reads this from `prepare.options`, and nowhere else.

    A value sent at the top level of the activate frame is accepted and
    ignored: the runner's activate handler reads named keys only, so a
    client-side assertion on that frame certifies a wire the other side never
    reads and leaves the knob dead. What the runner consumes is `options_in`,
    which is the `options` of the prepare frame, which every prepare path
    builds with `_runner_configure_options`. That is what this asserts on.
    """
    from claude_agent_sdk import ClaudeAgentOptions

    from astrabox.core.service.orchestrator.engine import claude_code_runtime
    from astrabox.core.service.orchestrator import sandbox_runner

    monkeypatch.setenv("ASTRABOX_RUNNER_INTERACTION_WAIT_SECONDS", "30")
    # `load_astrabox_settings` is uncached by design, so the env change above
    # is what the next call reads.
    options = claude_code_runtime._runner_configure_options(ClaudeAgentOptions())

    assert options["interaction_wait_s"] == 30.0, (
        "prepare.options must carry the deployment's host-absence budget"
    )
    assert "interaction_wait_s" in sandbox_runner.RUNNER_OPTION_KEYS, (
        "and the runner must declare it, or its option validation refuses the "
        "prepare as carrying an unknown key"
    )


async def test_a_prepare_carrying_resume_without_a_store_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resume the store cannot serve is refused where it is asked for.

    The vendor materializes `resume` from the session store when the box has no
    local transcript — every rebuild after a sandbox death. An unclaimed slot
    has no store, so a prepare that carries a resume anyway can only end one
    way: the CLI exits 1, the runner answers 1011, and the platform reads
    "runner link closed during preparation" four layers from the cause. That is
    a real round this cost.
    """
    from astrabox.core.service.orchestrator.engine import runner_link as link_module

    async def _unreachable(self: object, wire: str, *, action: str) -> None:
        raise AssertionError("the refusal must precede the wire")

    monkeypatch.setattr(link_module.RunnerLink, "_send_wire", _unreachable)
    link = link_module.RunnerLink("ws://runner.invalid")
    link._ws = object()  # connected enough to reach the guard

    with pytest.raises(link_module.RunnerLinkError, match="without a claimant and store"):
        await link.prepare(
            "slot-1",
            activation_token="slot-1-secret",
            options={"resume": "a5b84c16-c219-4bd9-a989-7e81cc6ed831"},
        )


def test_a_claimed_slot_binds_its_store_before_the_sdk_connects() -> None:
    """Bound at prepare, because connect is what reads `resume`.

    Binding on activate — one step later — is what left the CLI with no local
    file and an empty store.
    """
    from astrabox.core.service.orchestrator import sandbox_runner

    bound: list[tuple[str, object]] = []
    session = sandbox_runner.RunnerSession(
        session_id="slot-1",
        link=mock.Mock(),
        client_factory=lambda broker: mock.Mock(),
        activation_callback=lambda target, store: bound.append((target, store)),
    )

    session.bind_store("sess-1", {"base_url": "http://platform.invalid"})

    assert bound == [("sess-1", {"base_url": "http://platform.invalid"})]
    assert session._store_bound, "the slot must remember it is bound"


def test_a_bound_slot_refuses_a_claim_by_another_session() -> None:
    """The store is scoped to one Session; the binding is the identity.

    Without this, a slot bound for one conversation could be activated as
    another and mirror its transcript into the wrong Session's store.
    """
    from astrabox.core.service.orchestrator import sandbox_runner

    session = sandbox_runner.RunnerSession(
        session_id="slot-1",
        link=mock.Mock(),
        client_factory=lambda broker: mock.Mock(),
        activation_callback=lambda target, store: None,
    )
    session.bind_store("sess-1", {"base_url": "http://platform.invalid"})
    session._prepared = True
    session._client = mock.Mock()

    with pytest.raises(sandbox_runner.RunnerProtocolError, match="bound to 'sess-1'"):
        asyncio.run(session.activate("sess-2", mock.Mock(), permission_mode="default"))


def test_binding_a_store_after_prepare_is_refused() -> None:
    """The point of the binding is to precede the connect."""
    from astrabox.core.service.orchestrator import sandbox_runner

    session = sandbox_runner.RunnerSession(
        session_id="slot-1",
        link=mock.Mock(),
        client_factory=lambda broker: mock.Mock(),
        activation_callback=lambda target, store: None,
    )
    session._prepared = True

    with pytest.raises(sandbox_runner.RunnerProtocolError, match="after the slot is prepared"):
        session.bind_store("sess-1", {"base_url": "http://platform.invalid"})
