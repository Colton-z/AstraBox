"""Runner wire protocol — who may prepare, attach to and take over a runner.

Integration at the protocol layer: a real server on an ephemeral port, a real
client, a fake SDK session behind the server. A peer that can dial the runner
must still present the delivered credential to prepare it and the activation
token to attach; activation is single use, a live session cannot be displaced,
and a bound slot refuses another session.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from unittest import mock
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, TextBlock

from astrabox.core.service.orchestrator.engine import runner_link as runner_link_module
from astrabox.core.service.orchestrator.engine.runner_link import (
    DeliveryCommand,
    RunnerLink as _RunnerLink,
    RunnerLinkError,
)
from astrabox.core.service.orchestrator.engine.runtime import RuntimeInputRefused
from astrabox.core.service.orchestrator.sandbox_runner import (
    HostLink,
    RunnerSession,
    RunnerWsServer,
)


#: The token every host link in this module presents unless a test names
#: another, and the one the runner's credential file holds.
HOST_ACTIVATION_TOKEN = "host-activation-token"


def _delivered_token_file(directory: Any, token: str) -> str:
    """The platform's delivery of the runner credential, as a file."""

    path = directory / ".astrabox-runner-token"
    path.write_text(token, encoding="utf-8")
    return str(path)


class RunnerLink(_RunnerLink):
    def __init__(self, uri: str, **kwargs: Any) -> None:
        kwargs.setdefault("activation_token", HOST_ACTIVATION_TOKEN)
        super().__init__(uri, **kwargs)


async def _frames_handed_over(link: _RunnerLink) -> list[dict[str, Any]]:
    """Every frame a closed link received, in order: closing ends its stream."""
    return [frame async for frame in link.frames()]


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


def _assistant_message(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextBlock(text=text)],
        model="test-model",
        session_id="sdk-session",
        uuid=str(uuid.uuid4()),
    )


@pytest.fixture
async def wire(tmp_path):
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

    server = RunnerWsServer(
        host="127.0.0.1",
        port=0,
        session_factory=factory,
        activation_token_file=_delivered_token_file(tmp_path, HOST_ACTIVATION_TOKEN),
    )
    await server.start()
    try:
        yield server, client, f"ws://127.0.0.1:{server.port}/"
    finally:
        await server.stop()


async def _next_message_type(frames: Any, wanted: str) -> dict[str, Any]:
    async for frame in frames:
        if frame.get("op") == "event" and frame.get("message_type") == wanted:
            return frame
    raise AssertionError(f"stream ended before message_type={wanted}")


async def test_prepare_has_no_session_frames_and_activation_is_single_use(wire) -> None:
    server, sdk, uri = wire
    async with RunnerLink(uri) as warmer:
        prepared = await warmer.prepare("slot-1", options={})
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
                    required_option_keys=(),
                    permission_mode="default",
                )
        assert server.session is active, "a duplicate claim must not kill its owner"


async def test_an_empty_runner_admits_its_first_prepare_only_with_the_delivered_credential(
    tmp_path,
) -> None:
    """A peer that reaches an empty runner first cannot prepare it.

    The runner starts before the platform has a credential for it, so it may
    not trust whoever prepares it first: that peer would install its own token
    and get an engine in a box that later serves a user. It compares every
    prepare with the file the platform writes, refuses at once while the file
    is absent (the host is not left waiting), and spawns nothing for a
    refused prepare. The platform's own prepare then succeeds on the same
    runner, so the refusals were the credential's and not the frame's.
    """

    spawned: list[dict[str, Any]] = []
    client = QueueSdkSession()

    def factory(opening: dict[str, Any], link: HostLink) -> RunnerSession:
        spawned.append(opening)
        return RunnerSession(
            session_id=str(opening["slot_id"]),
            link=link,
            client_factory=lambda broker: client,
            activation_callback=client.bind_store,
            interaction_wait_s=5.0,
        )

    token_file = tmp_path / ".astrabox-runner-token"
    server = RunnerWsServer(
        host="127.0.0.1",
        port=0,
        session_factory=factory,
        activation_token_file=str(token_file),
    )
    await server.start()
    uri = f"ws://127.0.0.1:{server.port}/"
    try:
        async with RunnerLink(uri) as early:
            with pytest.raises(RunnerLinkError, match="has not delivered"):
                await asyncio.wait_for(early.prepare("slot-1", options={}), timeout=5)
        assert server.session is None

        token_file.write_text(HOST_ACTIVATION_TOKEN, encoding="utf-8")
        async with RunnerLink(uri, activation_token="a-peers-own-token") as peer:
            with pytest.raises(RunnerLinkError, match="prepare token mismatch"):
                await peer.prepare("slot-1", options={})
        assert server.session is None
        assert spawned == [], "a refused prepare must not start an engine"

        async with RunnerLink(uri) as host:
            prepared = await host.prepare("slot-1", options={})
        assert prepared["slot_id"] == "slot-1"
        assert server.session is not None and server.session.is_prepared
    finally:
        await server.stop()


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

        async with RunnerLink(uri) as sibling:
            with pytest.raises(RunnerLinkError) as caught:
                await sibling.attach("sess-1", last_seen_seq=0)

        assert str(caught.value) == "attach missing session_id"
        assert await asyncio.wait_for(_frames_handed_over(sibling), timeout=2.0) == []

        sdk.emit(_assistant_message("still the configured host"))
        event = await asyncio.wait_for(
            _next_message_type(platform_link.frames(), "AssistantMessage"),
            timeout=2.0,
        )
        assert event["session_id"] == "sess-1"


async def test_the_link_refuses_what_it_cannot_carry_without_a_reconnect_loop(
    wire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both directions honour the link's message limit, and neither loops.

    A message within the limit arrives whole. An input over it is refused
    before it is sent, and the link keeps serving. A runner message over it
    ends the frame stream with a protocol fault: the runner replays the same
    message after the same cursor, so a reader must not reattach for it. The
    host's limit is lowered here so the test sends kilobytes, not megabytes.
    """

    server, sdk, uri = wire
    limit = 64 * 1024
    monkeypatch.setattr(runner_link_module, "RUNNER_MESSAGE_MAX_BYTES", limit)
    async with RunnerLink(uri) as host:
        await host.configure("sess-1")
        assert server.session is not None

        sdk.emit(_assistant_message("a" * (limit // 2)))
        within = await asyncio.wait_for(
            _next_message_type(host.frames(), "AssistantMessage"), timeout=2.0
        )
        assert within["message"]["content"][0]["text"] == "a" * (limit // 2)

        with pytest.raises(RuntimeInputRefused) as refused:
            await host.deliver(DeliveryCommand(
                command_id=str(uuid.uuid4()),
                session_id="sess-1",
                sequence=1,
                sdk_input={"type": "user", "message": {"role": "user", "content": "b" * limit}},
            ))
        assert refused.value.code == "RUNTIME_INPUT_TOO_LARGE"
        assert host.is_live and host.protocol_fault is None

        sdk.emit(_assistant_message("c" * (limit * 2)))
        after = await asyncio.wait_for(_frames_handed_over(host), timeout=5.0)
        assert [frame for frame in after if frame.get("message_type") == "AssistantMessage"] == []
        assert host.protocol_fault is not None
        assert f"{limit}-byte limit" in host.protocol_fault
        assert host.is_live is False


@pytest.mark.parametrize(
    ("presented", "session_named"),
    [(None, "sess-1"), ("a-guessed-token", "sess-1"), (None, "a-guessed-session")],
    ids=["session-id-without-token", "session-id-with-wrong-token", "nothing"],
)
async def test_attach_without_the_activation_token_is_refused_before_anything_is_named(
    wire,
    monkeypatch: pytest.MonkeyPatch,
    presented: str | None,
    session_named: str,
) -> None:
    """A peer that can dial the runner cannot take the session.

    The session id is not a secret: the API surfaces it, and before the token
    was checked a peer with nothing learned it from the mismatch refusal. So
    each attempt is the producer's own attach frame with only the credential
    (and, for the peer with nothing, the name) replaced, the refusal must not
    name the held session, and the configured host keeps its link. The same
    frame carrying the host's token is then accepted, so the refusal was the
    token's and not the frame's.
    """

    server, sdk, uri = wire
    async with RunnerLink(uri) as platform_link:
        await platform_link.configure("sess-1")
        assert server.session is not None

        original_send_wire = _RunnerLink._send_wire

        async def replace_credential(
            self: Any,
            payload: str,
            *,
            action: str,
        ) -> None:
            frame = json.loads(payload)
            if frame.get("op") == "attach" and self is not platform_link:
                frame.pop("activation_token", None)
                if presented is not None:
                    frame["activation_token"] = presented
                frame["session_id"] = session_named
            await original_send_wire(self, json.dumps(frame), action=action)

        monkeypatch.setattr(_RunnerLink, "_send_wire", replace_credential)
        async with RunnerLink(uri) as peer:
            with pytest.raises(RunnerLinkError) as caught:
                await peer.attach(session_named, last_seen_seq=0)

        assert str(caught.value) == "attach token mismatch"
        assert "sess-1" not in str(caught.value)
        assert await asyncio.wait_for(_frames_handed_over(peer), timeout=2.0) == []
        sdk.emit(_assistant_message("still the configured host"))
        event = await asyncio.wait_for(
            _next_message_type(platform_link.frames(), "AssistantMessage"),
            timeout=2.0,
        )
        assert event["session_id"] == "sess-1"

        monkeypatch.setattr(_RunnerLink, "_send_wire", original_send_wire)
        async with RunnerLink(uri) as reconnected_host:
            hello = await reconnected_host.attach("sess-1", last_seen_seq=0)
        assert hello["session_id"] == "sess-1"


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
