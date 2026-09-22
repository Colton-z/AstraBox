"""One resident Hermes gateway per profile, shared by its conversations.

The vendor's TUI gateway is a native multi-session runtime, so the platform
keeps one process per (user, assistant) profile and every conversation is one
``session.create``/``session.resume`` on it. These tests drive the host-side
residency pieces over a fake execd wire: the fenced spawn/reattach resolution,
the event pump's per-session fan-out, replay dedup, sibling isolation on
dispose, and the durable rendezvous record on the assistant workspace row.
"""

from __future__ import annotations

import asyncio
import copy
from unittest import mock
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.assistant.assistant_workspace_service import (
    AssistantWorkspaceService,
)
from astrabox.core.service.orchestrator.engine.base import EngineStreamDetached
from astrabox.core.service.orchestrator.engine.hermes_client import (
    HermesTuiEngineClient,
    HermesTuiWireEvent,
)
from astrabox.core.service.orchestrator.engine import hermes_gateway
from astrabox.core.service.orchestrator.engine.hermes_gateway import (
    HermesGatewayHandle,
    gateway_handle_for_sandbox,
    reset_gateway_registry,
    resolve_gateway_handle,
)


@pytest.fixture(autouse=True)
def _isolated_registry() -> Any:
    reset_gateway_registry()
    yield
    reset_gateway_registry()


class _FakeGatewayWire:
    """One box's resident backend and the gateway RPC behavior behind it."""

    def __init__(self) -> None:
        self.connects: list[tuple[str, bool]] = []
        #: Set to make the next attachment fail, standing in for a backend
        #: that is not listening yet or has gone away.
        self.refuse_connect: BaseException | None = None
        self.rpc_log: list[tuple[str, dict[str, Any]]] = []
        self.event_log: list[HermesTuiWireEvent] = []
        self.attachments: list[_FakeTuiProcess] = []
        self._session_counter = 0

    def rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.rpc_log.append((method, dict(params)))
        if method == "session.create":
            self._session_counter += 1
            return {"session_id": f"tui-{self._session_counter}"}
        if method == "session.resume":
            return {
                "session_id": f"tui-resumed-{params.get('session_id')}",
                "resumed": str(params.get("session_id") or ""),
            }
        if method == "session.title":
            return {"session_key": f"key-{params.get('session_id')}"}
        if method == "session.interrupt":
            return {"status": "interrupted"}
        return {"ok": True}

    def push_event(self, event: dict[str, Any], *, output_offset: int) -> None:
        wire_event = HermesTuiWireEvent(event=dict(event), output_offset=output_offset)
        self.event_log.append(wire_event)
        for attachment in self.attachments:
            if attachment.is_connected:
                attachment.queue.put_nowait(wire_event)

    def factory(self, **kwargs: Any) -> "_FakeTuiProcess":
        return _FakeTuiProcess(wire=self, **kwargs)


class _FakeTuiProcess:
    """The HermesTuiProcess surface the gateway handle drives.

    It attaches; it does not own. The PTY-era fake had to model process
    lifecycle — spawning, a cursor to replay from, deleting — because the
    handle spawned and destroyed a process. The backend is a service of the
    image now, so all this carries is "am I connected" and the event stream.
    """

    def __init__(
        self,
        *,
        wire: "_FakeGatewayWire",
        url: str,
        headers: dict[str, str] | None = None,
        dial: tuple[str, int] | None = None,
    ) -> None:
        self._wire = wire
        self.url = url
        self.headers = dict(headers or {})
        self.dial = dial
        self.queue: asyncio.Queue[HermesTuiWireEvent | BaseException] = asyncio.Queue()
        self._connected = False
        self._fatal: BaseException | None = None

    @property
    def is_connected(self) -> bool:
        return self._connected and self._fatal is None

    @property
    def fatal(self) -> BaseException | None:
        return self._fatal

    async def current_output_offset(self) -> int:
        return 0

    async def connect(self, *, require_gateway_ready: bool) -> None:
        if self._wire.refuse_connect is not None:
            raise self._wire.refuse_connect
        self._wire.connects.append((self.url, bool(require_gateway_ready)))
        self._connected = True
        self._wire.attachments.append(self)

    async def next_event(self) -> HermesTuiWireEvent:
        item = await self.queue.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        if not self.is_connected:
            raise EngineStreamDetached("fake backend is not connected")
        return self._wire.rpc(method, dict(params or {}))

    async def detach(self) -> None:
        self._connected = False

    def die(self, exc: BaseException) -> None:
        self._fatal = exc
        self.queue.put_nowait(exc)

async def _resolve(
    wire: _FakeGatewayWire,
    *,
    fingerprint: str | None = "fp-a",
    profile_key: str = "user-1:assistant-1",
) -> HermesGatewayHandle:
    return await resolve_gateway_handle(
        url="ws://127.0.0.1:9119/api/ws?token=t",
        dial=("10.42.0.7", 9118),
        headers={"x-route": "sb-1"},
        sandbox_id="sb-1",
        profile_key=profile_key,
        spawn_fingerprint=fingerprint,
        process_factory=wire.factory,
    )


@pytest.mark.asyncio
async def test_the_attachment_dials_the_box_and_addresses_the_bind() -> None:
    """The two addresses must not collapse into one.

    Hermes binds loopback and refuses an upgrade whose `Host` names anything
    else — its DNS-rebinding defence. A first attempt built one URL out of the
    box's endpoint and every upgrade came back `HTTP 403`. So the handle has to
    carry both: the URL names the interface the backend bound to, which is what
    the `Host` header is built from, and the dial names where the forwarder
    publishes it.
    """

    wire = _FakeGatewayWire()
    handle = await _resolve(wire)

    process = wire.attachments[-1]
    assert process.url.startswith("ws://127.0.0.1:9119/"), (
        "the URL must address the backend's own bind, not the box"
    )
    assert process.dial == ("10.42.0.7", 9118), (
        "the connection must go to the box's published forwarder"
    )
    await handle.shutdown()


# ── attaching to the box's resident backend ──────────────────────────────
#
# Deliberately no ownership arbitration below — no durable record of one true
# gateway, no compare-and-set against a second host spawning one, no probe
# separating a dead record from a live process, no destroy for a loser. The
# backend is a service of the image, supervised there and owned by no host, so
# there is no winner to elect. What is covered here is what a host decides for
# itself.


@pytest.mark.asyncio
async def test_concurrent_attachers_share_one_attachment() -> None:
    """The per-host lock still has a job, even with nothing to elect.

    Two conversations of one profile race the first attach. They must end up
    on one handle: the pump and its subscriptions are per-handle state, and a
    second attachment would deliver every event twice.
    """

    wire = _FakeGatewayWire()

    first, second = await asyncio.gather(_resolve(wire), _resolve(wire))

    assert first is second
    assert len(wire.connects) == 1
    assert wire.connects[0][1] is True, "a fresh attachment waits for gateway.ready"


@pytest.mark.asyncio
async def test_changed_profile_config_detaches_and_reattaches() -> None:
    """The configuration fence holds without naming a process.

    ``spawn_fingerprint`` is the identity of the profile the backend serves. A
    standing attachment established under different content must not keep
    gaining sessions under the new one. The remedy available here is detaching
    and attaching again — restarting the backend for a changed profile is
    supervisord's call, not this function's.
    """

    wire = _FakeGatewayWire()
    first = await _resolve(wire, fingerprint="fp-a")
    second = await _resolve(wire, fingerprint="fp-b")

    assert second is not first
    assert first.is_live is False, "the superseded attachment is released"
    assert len(wire.connects) == 2


@pytest.mark.asyncio
async def test_lightweight_resolution_accepts_the_standing_attachment() -> None:
    """``None`` means "whatever is attached", for callers that rewrite nothing."""

    wire = _FakeGatewayWire()
    first = await _resolve(wire, fingerprint="fp-a")
    second = await _resolve(wire, fingerprint=None)

    assert second is first
    assert len(wire.connects) == 1


@pytest.mark.asyncio
async def test_a_dead_attachment_is_replaced_rather_than_reused() -> None:
    """A handle whose socket died is not a handle.

    There is no recorded gateway to chase and no PTY to tell dead from live:
    the backend is supervised and either answers or does not. So the only
    question is whether THIS host is still attached, and a dead one is
    replaced.
    """

    wire = _FakeGatewayWire()
    handle = await _resolve(wire)
    wire.attachments[-1].die(EngineStreamDetached("socket closed"))
    await asyncio.sleep(0)

    replacement = await _resolve(wire)

    assert replacement is not handle
    assert replacement.is_live is True
    assert len(wire.connects) == 2


@pytest.mark.asyncio
async def test_a_backend_that_is_still_starting_is_waited_for() -> None:
    """The first materialization's timing, which the first attempt missed.

    The host writes the profile env file and then attaches. Inside the box
    that write is what releases `astrabox-hermes-serve` to start Hermes, and
    `astrabox-hermes-forward` publishes the port only once the backend
    answers — so for a few seconds there is legitimately nothing listening.
    Measured on a real box before this: attaching immediately failed with
    `[Errno 111] Connection refused` and took the whole materialization with
    it.

    A resume does not need it: the profile is already on the persistent
    mount, so the backend comes back with the box. Only a first
    materialization has the gap, which is why the wait is bounded rather than
    a retry over a race.
    """

    wire = _FakeGatewayWire()
    wire.refuse_connect = ConnectionRefusedError("[Errno 111] Connection refused")

    async def _listen_soon() -> None:
        await asyncio.sleep(0)
        wire.refuse_connect = None

    task = asyncio.create_task(_listen_soon())
    handle = await _resolve(wire)
    await task

    assert handle.is_live is True
    assert len(wire.connects) == 1, "only the successful attach connects"


@pytest.mark.asyncio
async def test_an_attachment_that_cannot_connect_fails_loudly() -> None:
    """A backend that is not listening is an error, not an empty handle.

    The image publishes the port only once the backend answers
    (`astrabox-hermes-forward`), so a refused connection here means something
    is actually wrong — and a caller handed a handle that never attached would
    discover it as a hang on the first turn.
    """

    wire = _FakeGatewayWire()
    wire.refuse_connect = OSError("connection refused")

    with mock.patch.object(hermes_gateway, "_ATTACH_BUDGET_SECONDS", 0.0):
        with pytest.raises(APIError) as excinfo:
            await _resolve(wire)

    assert excinfo.value.code == "HERMES_GATEWAY_START_FAILED"


# ── event pump fan-out and replay ────────────────────────────────────────


@pytest.mark.asyncio
async def test_pump_routes_each_session_its_own_events_in_order() -> None:
    wire = _FakeGatewayWire()
    handle = await _resolve(wire)
    stream_a = handle.subscribe("tui-a")
    stream_b = handle.subscribe("tui-b")

    wire.push_event(
        {"type": "message.delta", "session_id": "tui-a", "payload": {"text": "a1"}},
        output_offset=1,
    )
    wire.push_event(
        {"type": "message.delta", "session_id": "tui-b", "payload": {"text": "b1"}},
        output_offset=2,
    )
    wire.push_event({"type": "gateway.stderr", "payload": {}}, output_offset=3)
    wire.push_event(
        {"type": "message.delta", "session_id": "tui-a", "payload": {"text": "a2"}},
        output_offset=4,
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    a_events = [await stream_a.next_event() for _ in range(3)]
    b_events = [await stream_b.next_event() for _ in range(2)]
    assert [event.output_offset for event in a_events] == [1, 3, 4]
    assert [event.output_offset for event in b_events] == [2, 3]
    assert [
        event.event.get("payload", {}).get("text")
        for event in a_events
        if event.event.get("session_id")
    ] == ["a1", "a2"]


@pytest.mark.asyncio
async def test_a_recovering_subscriber_is_not_handed_what_it_already_saw() -> None:
    """The half of recovery this transport still keeps.

    A sibling subscription has already delivered the events before the cursor,
    and a recovery subscription asking for what comes after must not see them
    again. What it also cannot see is the suffix it missed: execd replayed a
    PTY's bytes from a cursor and the resident backend has no equivalent in
    the pinned Hermes, so recovery resumes live. That gap is stated at its
    caller in `hermes_client` and in docs/maintainers/hermes-transport.md; what
    is pinned here is that the deduplication still holds.
    """

    wire = _FakeGatewayWire()
    handle = await _resolve(wire)
    sibling = handle.subscribe("tui-a")
    wire.push_event(
        {"type": "message.delta", "session_id": "tui-a", "payload": {"text": "a1"}},
        output_offset=1,
    )
    await asyncio.sleep(0)
    assert (await sibling.next_event()).output_offset == 1

    recovery = handle.subscribe("tui-a", after_offset=1)
    wire.push_event(
        {"type": "message.delta", "session_id": "tui-a", "payload": {"text": "a2"}},
        output_offset=2,
    )
    await asyncio.sleep(0)

    assert (await recovery.next_event()).output_offset == 2
    assert (await sibling.next_event()).output_offset == 2


@pytest.mark.asyncio
async def test_gateway_death_reaches_every_subscriber() -> None:
    wire = _FakeGatewayWire()
    handle = await _resolve(wire)
    stream_a = handle.subscribe("tui-a")
    stream_b = handle.subscribe("tui-b")

    wire.attachments[-1].die(EngineStreamDetached("gateway exited"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    with pytest.raises(EngineStreamDetached):
        await stream_a.next_event()
    with pytest.raises(EngineStreamDetached):
        await stream_b.next_event()
    assert handle.is_live is False


# ── sibling isolation through the conversation client ────────────────────


@pytest.mark.asyncio
async def test_disposing_one_conversation_leaves_its_sibling_streaming() -> None:
    # Two conversations of one profile share the resident gateway. Ending
    # one must close only its vendor session: the PTY survives, and the
    # sibling keeps receiving its events.
    wire = _FakeGatewayWire()
    handle = await _resolve(wire)
    first = HermesTuiEngineClient(gateway=handle, platform_session_id="session-a")
    second = HermesTuiEngineClient(gateway=handle, platform_session_id="session-b")
    await first._ensure_session()
    await second._ensure_session()
    assert (first._tui_session_id, second._tui_session_id) == ("tui-1", "tui-2")

    closed = await first.dispose()

    assert closed is True
    assert ("session.close", {"session_id": "tui-1"}) in wire.rpc_log
    # Disposing one conversation must not take the backend with it — it is
    # the workspace's resident runtime and the sibling is still on it. This
    # path has no way to stop it even by mistake: the backend belongs to the
    # image's supervisor, and detaching is all a handle can do.
    assert handle.is_live is True
    assert second.is_live is True
    wire.push_event(
        {"type": "message.delta", "session_id": "tui-2", "payload": {"text": "hi"}},
        output_offset=9,
    )
    await asyncio.sleep(0)
    assert second._subscription is not None
    assert (await second._subscription.next_event()).output_offset == 9


@pytest.mark.asyncio
async def test_a_client_whose_attachment_was_superseded_reports_not_live() -> None:
    """The client must not carry a session across an attachment it lost.

    Nothing names a backend instance, so the only question a client can ask is
    whether its own attachment still stands — and when a config change
    supersedes that attachment, it does not.

    The vendor closes the other half: resuming a session id on a restarted
    backend answers `4007 session not found`, measured on a real box. So a
    stale client is caught here, or there, and never by silently answering out
    of an engine that has forgotten the conversation.
    """

    wire = _FakeGatewayWire()
    handle = await _resolve(wire, fingerprint="fp-a")
    client = HermesTuiEngineClient(gateway=handle, platform_session_id="session-a")
    await client._ensure_session()
    assert client.is_live is True

    await _resolve(wire, fingerprint="fp-b")

    assert client.is_live is False


@pytest.mark.asyncio
async def test_dispose_lookup_finds_this_hosts_attachment_by_sandbox() -> None:
    """Disposal holds an opaque turn id, and a box holds one workspace.

    `astrabox-hermes-serve` refuses to start against a second profile rather
    than guess, so a sandbox has at most one live attachment here — which is
    what lets a caller with no profile in hand still find it.
    """

    wire = _FakeGatewayWire()
    handle = await _resolve(wire)

    assert gateway_handle_for_sandbox("sb-1") is handle
    assert gateway_handle_for_sandbox("sb-2") is None
    assert gateway_handle_for_sandbox("") is None


@pytest.mark.asyncio
async def test_half_activated_session_is_destroyed_not_kept() -> None:
    # Activation is single and destructive on failure: a session that
    # returned an id but no durable key would otherwise linger in the shared
    # gateway as an orphan nothing can resume.
    wire = _FakeGatewayWire()
    handle = await _resolve(wire)
    original_rpc = wire.rpc

    def rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "session.title":
            wire.rpc_log.append((method, dict(params)))
            return {}
        return original_rpc(method, params)

    wire.rpc = rpc  # type: ignore[method-assign]
    client = HermesTuiEngineClient(gateway=handle, platform_session_id="session-a")

    with pytest.raises(RuntimeError, match="durable session key"):
        await client._ensure_session()

    assert ("session.close", {"session_id": "tui-1"}) in wire.rpc_log
    assert client._tui_session_id is None



# ── the durable rendezvous record on the workspace row ───────────────────


class _FakeWorkspaceRepo:
    """Dotted-path document semantics of the assistant workspace collection."""

    def __init__(self, doc: dict[str, Any]) -> None:
        self.doc = copy.deepcopy(doc)

    async def get_workspace(self, user_id: str, assistant_id: str) -> dict[str, Any]:
        return copy.deepcopy(self.doc)

    async def update_workspace(
        self, user_id: str, assistant_id: str, updates: dict[str, Any]
    ) -> bool:
        for key, value in updates.items():
            self._set(key, value)
        return True

    async def compare_and_update_workspace(
        self,
        user_id: str,
        assistant_id: str,
        *,
        expected: dict[str, Any],
        updates: dict[str, Any],
    ) -> bool:
        for key, value in expected.items():
            if self._get(key) != value:
                return False
        for key, value in updates.items():
            self._set(key, value)
        return True

    def _get(self, dotted: str) -> Any:
        node: Any = self.doc
        for part in dotted.split("."):
            if not isinstance(node, dict):
                return None
            node = node.get(part)
        return node

    def _set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self.doc
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value


def _service(doc: dict[str, Any]) -> tuple[AssistantWorkspaceService, _FakeWorkspaceRepo]:
    repo = _FakeWorkspaceRepo(doc)
    return AssistantWorkspaceService(workspace_repo=repo), repo  # type: ignore[arg-type]


_ROW = {
    "assistant_id": "assistant-1",
    "created_by_user_id": "user-1",
    "state": "READY",
    "current_sandbox_id": "sb-1",
}


@pytest.mark.asyncio
async def test_gateway_record_round_trips_and_validates_the_sandbox() -> None:
    service, _repo = _service(_ROW)

    recorded = await service.record_profile_gateway_process(
        user_id="user-1",
        assistant_id="assistant-1",
        sandbox_id="sb-1",
        engine_kind="assistant",
        pty_session_id="pty-1",
        spawn_fingerprint="fp-a",
        expected_pty_session_id=None,
    )
    assert recorded is True

    current = await service.get_profile_gateway_process(
        user_id="user-1", assistant_id="assistant-1", sandbox_id="sb-1"
    )
    assert current == {"pty_session_id": "pty-1", "spawn_fingerprint": "fp-a"}
    # A record for one box must not be served as another box's gateway.
    assert (
        await service.get_profile_gateway_process(
            user_id="user-1", assistant_id="assistant-1", sandbox_id="sb-2"
        )
        is None
    )


@pytest.mark.asyncio
async def test_gateway_record_swap_has_exactly_one_winner() -> None:
    service, _repo = _service(_ROW)
    await service.record_profile_gateway_process(
        user_id="user-1",
        assistant_id="assistant-1",
        sandbox_id="sb-1",
        engine_kind="assistant",
        pty_session_id="pty-1",
        spawn_fingerprint="fp-a",
        expected_pty_session_id=None,
    )

    winner = await service.record_profile_gateway_process(
        user_id="user-1",
        assistant_id="assistant-1",
        sandbox_id="sb-1",
        engine_kind="assistant",
        pty_session_id="pty-2",
        spawn_fingerprint="fp-a",
        expected_pty_session_id="pty-1",
    )
    loser = await service.record_profile_gateway_process(
        user_id="user-1",
        assistant_id="assistant-1",
        sandbox_id="sb-1",
        engine_kind="assistant",
        pty_session_id="pty-3",
        spawn_fingerprint="fp-a",
        expected_pty_session_id="pty-1",
    )

    assert (winner, loser) == (True, False)
    current = await service.get_profile_gateway_process(
        user_id="user-1", assistant_id="assistant-1", sandbox_id="sb-1"
    )
    assert current is not None and current["pty_session_id"] == "pty-2"


@pytest.mark.asyncio
async def test_gateway_record_for_a_replaced_box_is_overwritten_under_cas() -> None:
    # A record naming a box other than the workspace's current one reads as
    # absent, but replacing it still fences on the stale value so concurrent
    # writers for the new box have one winner.
    service, _repo = _service(_ROW)
    await service.record_profile_gateway_process(
        user_id="user-1",
        assistant_id="assistant-1",
        sandbox_id="sb-old",
        engine_kind="assistant",
        pty_session_id="pty-old",
        spawn_fingerprint="fp-a",
        expected_pty_session_id=None,
    )
    assert (
        await service.get_profile_gateway_process(
            user_id="user-1", assistant_id="assistant-1", sandbox_id="sb-1"
        )
        is None
    )

    recorded = await service.record_profile_gateway_process(
        user_id="user-1",
        assistant_id="assistant-1",
        sandbox_id="sb-1",
        engine_kind="assistant",
        pty_session_id="pty-new",
        spawn_fingerprint="fp-b",
        expected_pty_session_id=None,
    )

    assert recorded is True
    current = await service.get_profile_gateway_process(
        user_id="user-1", assistant_id="assistant-1", sandbox_id="sb-1"
    )
    assert current == {"pty_session_id": "pty-new", "spawn_fingerprint": "fp-b"}


@pytest.mark.asyncio
async def test_finishing_hibernation_clears_every_gateway_record() -> None:
    # Hibernation destroys every process in the box; a record surviving the
    # final transition would send the wake-side attach chasing a dead PTY.
    service, repo = _service(_ROW)
    await service.record_profile_gateway_process(
        user_id="user-1",
        assistant_id="assistant-1",
        sandbox_id="sb-1",
        engine_kind="assistant",
        pty_session_id="pty-1",
        spawn_fingerprint="fp-a",
        expected_pty_session_id=None,
    )

    begun = await service.begin_hibernation(
        user_id="user-1",
        assistant_id="assistant-1",
        sandbox_id="sb-1",
        hibernated_at="2026-08-25T00:00:00+00:00",
    )
    finished = await service.finish_hibernation(
        user_id="user-1",
        assistant_id="assistant-1",
        destroyed_sandbox_id="sb-1",
        expected_state="HIBERNATING",
    )

    assert begun is True
    assert finished is True
    assert repo.doc["assistant_gateways"] == {}
    assert (
        await service.get_profile_gateway_process(
            user_id="user-1", assistant_id="assistant-1", sandbox_id="sb-1"
        )
        is None
    )


@pytest.mark.asyncio
async def test_converging_a_dead_box_clears_every_gateway_record() -> None:
    service, repo = _service(_ROW)
    await service.record_profile_gateway_process(
        user_id="user-1",
        assistant_id="assistant-1",
        sandbox_id="sb-1",
        engine_kind="assistant",
        pty_session_id="pty-1",
        spawn_fingerprint="fp-a",
        expected_pty_session_id=None,
    )
    workspace = await repo.get_workspace("user-1", "assistant-1")

    converged = await service.converge_dead_sandbox(
        workspace=workspace,
        sandbox_id="sb-1",
        last_error="sandbox terminated",
    )

    assert converged is True
    assert repo.doc["assistant_gateways"] == {}


@pytest.mark.asyncio
async def test_recording_a_gateway_requires_the_sandbox_and_pty() -> None:
    service, _repo = _service(_ROW)

    with pytest.raises(APIError):
        await service.record_profile_gateway_process(
            user_id="user-1",
            assistant_id="assistant-1",
            sandbox_id="",
            engine_kind="assistant",
            pty_session_id="pty-1",
            spawn_fingerprint="fp-a",
            expected_pty_session_id=None,
        )
