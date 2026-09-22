"""DeepSeekHarnessEngineClient behaviour over a scripted /api link.

The turn tests are driven by ``product-two-turns.mux.jsonl`` — the downlink of
a real older ``dsh --profile web`` server answering two real prompts (a text reply
and a ``write`` tool call) with a real model. Recorded rather than written,
because the two contracts that matter most here are only true of the product:
the harness echoes the caller's own prompt ``rpcId`` into the session log, and
it emits a SECOND ``user/message`` from a plugin's context snapshot in the same
turn. A hand-authored stream would have had one user message and would have
made the wrong anchor look correct.

The shared fixture adapter repackages its durable chunks into the current
settlement schema. The source recording remains unchanged; this is not a
capture of the current live assistant-stream transport.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from deepseek_harness_fixtures import current_settlement_frames

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.base import (
    EngineChildRunControl,
    EngineConversationBinding,
    EngineInputCommand,
    EngineInteractions,
    EnginePermissionModes,
    EngineServerInfo,
    EngineStreamDetached,
)
from astrabox.core.service.orchestrator.engine.deepseek_harness_client import (
    DeepSeekHarnessEngineClient,
)
from astrabox.core.service.orchestrator.engine.emissions import EngineEmission
from astrabox.core.service.orchestrator.engine.emissions import (
    ChildResourceFact,
)
from astrabox.core.service.orchestrator.engine.interaction_contract import (
    build_pending_interaction_record,
    validate_interaction_contract,
)

_DATA_DIR = Path(__file__).parent / "data" / "deepseek_harness"
_SESSION_ID = "dsh-client-session"
_NATIVE_ID = "session-c4f2b2b8-8a80-4b34-be99-4ff7a63e042b"
_INPUT_ID = "11111111-2222-4333-8444-555555555555"

#: The prompt ids the recorded server echoed back. The client mints its own at
#: runtime, so a test that wants the recording's consumption boundary to fire
#: pins the client's id to the one the recording carries.
_RECORDED_RPC_TURN_1 = "astrabox-input:test-input-1:deadbeef"
_CHILD_ID = "session-child-11111111-2222-4333-8444-555555555555"
_GRANDCHILD_ID = "session-child-aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

_END_OF_STREAM = object()


def _load_mux(*, until_turn_end: int | None = None) -> list[dict[str, Any]]:
    """Recorded content repackaged into the current durable settlement schema."""

    raw = (_DATA_DIR / "product-two-turns.mux.jsonl").read_text()
    raw = raw.replace("{{sessionId}}", _NATIVE_ID)
    frames: list[dict[str, Any]] = []
    turns_ended = 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        message = json.loads(line)
        payload = message["payload"]
        frames.append(
            {
                "rpcId": str(message.get("rpcId") or ""),
                "type": str(payload.get("type") or ""),
                "payload": payload,
            }
        )
        event = payload.get("event")
        if isinstance(event, dict) and event.get("type") == "turn/end":
            turns_ended += 1
            if until_turn_end is not None and turns_ended >= until_turn_end:
                break
    return current_settlement_frames(
        frames, session_id=_NATIVE_ID, until_turn_end=until_turn_end
    )


#: Fields the harness's own typert descriptor declares for a gateway method,
#: transcribed from `@deepseek-ai/dsh-commands`'s
#: `lib/typert.remote-client.d.ts`. The real gateway refuses an args object
#: that omits one — it answers `args fields do not match the descriptor`
#: rather than defaulting the value — so a fake that accepts anything turns a
#: contract break into a green suite and a dead sandbox.
#:
#: Kept here rather than imported from the client on purpose: a fixture that
#: reads the same constant the caller builds from would agree with the caller
#: however wrong both are.
VENDOR_ARGS: dict[str, frozenset[str]] = {
    # dsh-commands 0.1.5-rc.2 accepts ordered submitted attachments.
    "commands/execute": frozenset({"agentId", "line", "submittedAttachments"}),
    # `@Remote('selectModel') selectModel(request: SessionSelectModelRequest)`,
    # from `@deepseek-ai/dsh-api-session-controller`'s generated remote client.
    "session/selectModel": frozenset({"request"}),
}

#: The fields `SessionSelectModelRequest` declares, transcribed from the same
#: descriptor: it extends `ModelSelection`, so the session identity travels
#: with the provider/model pair rather than beside it.
SELECT_MODEL_REQUEST_FIELDS = frozenset({"sessionId", "provider", "model"})


def _require_vendor_args(method: str, payload: dict[str, Any]) -> None:
    declared = VENDOR_ARGS.get(method)
    if declared is None:
        return
    args = payload.get("args")
    assert isinstance(args, dict), f"{method} takes an args object, got {payload!r}"
    missing = sorted(declared - set(args))
    assert not missing, (
        f"{method}: args fields do not match the descriptor: "
        f"missing {', '.join(repr(name) for name in missing)}"
    )


class ScriptedLink:
    """DeepSeekHarnessLink over a queue and a call log the test controls."""

    def __init__(self, frames: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []
        self.responses: list[tuple[str, dict[str, Any]]] = []
        self.respond_accepts = True
        #: What the harness's command endpoint answers. Its default is the
        #: shape a real server returned for `/permission danger-full-access`.
        self.command_reply: Any = {
            "commandId": "cmd-1",
            "result": {"kind": "success", "text": "preset danger-full-access"},
        }
        self.created_session_id = _NATIVE_ID
        #: What `session/selectModel` normalizes the request to. ``None``
        #: keeps the vendor's own behaviour of echoing the requested pair.
        self.selected_model: dict[str, Any] | None = None
        self.permission_options = [
            "read-only",
            "workspace-write",
            "danger-full-access",
        ]
        self.subagent_catalogs: dict[str, list[dict[str, Any]]] = {}
        self.subagent_catalog_sequences: dict[
            str, list[list[dict[str, Any]]]
        ] = {}
        self.subagent_histories: dict[str, list[dict[str, Any]]] = {}
        self.subagent_history_page_size: int | None = None
        self.closed = False
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        for frame in frames or []:
            self._queue.put_nowait(frame)

    def end_stream(self) -> None:
        self._queue.put_nowait(_END_OF_STREAM)

    def push(self, frame: dict[str, Any]) -> None:
        self._queue.put_nowait(frame)

    @property
    def is_live(self) -> bool:
        return not self.closed

    async def call(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        rpc_id: str | None = None,
    ) -> Any:
        self.calls.append((method, dict(payload), rpc_id))
        _require_vendor_args(method, payload)
        args = payload["args"]
        request = args.get("request", {})
        if method == "session/create":
            return {"sessionId": self.created_session_id, "agentPreset": "standard"}
        if method == "commands/execute":
            return self.command_reply
        if method == "session/selectModel":
            unknown = sorted(set(request) - SELECT_MODEL_REQUEST_FIELDS)
            assert not unknown, (
                "session/selectModel: the request carries fields the vendor "
                f"does not declare: {', '.join(repr(n) for n in unknown)}"
            )
            missing = sorted(SELECT_MODEL_REQUEST_FIELDS - set(request))
            assert not missing, (
                "session/selectModel: the request omits declared fields: "
                f"{', '.join(repr(n) for n in missing)}"
            )
            if self.selected_model is not None:
                return {"selected": dict(self.selected_model)}
            return {
                "selected": {
                    "provider": request["provider"],
                    "model": request["model"],
                }
            }
        if method == "session/follow":
            return {
                "type": "snapshot", "cursor": 1000000, "records": [],
                "events": [],
                "hasMore": False,
                "projections": {
                    "asOfSeq": -1,
                    "values": {
                        "permissions": {
                            "options": [
                                {"value": value, "name": value}
                                for value in self.permission_options
                            ],
                            "currentValue": "workspace-write",
                        }
                    },
                },
            }
        if method == "subagents/list":
            parent_session_id = str(args.get("parentSessionId") or "")
            sequence = self.subagent_catalog_sequences.get(parent_session_id)
            entries = (
                sequence.pop(0)
                if sequence
                else self.subagent_catalogs.get(parent_session_id, [])
            )
            return {
                "entries": [
                    dict(entry)
                    for entry in entries
                ],
                "parentAvailable": True,
            }
        if method == "session/page":
            child_session_id = str(request["address"]["childSessionId"])
            events = [
                dict(entry)
                for entry in self.subagent_histories.get(child_session_id, [])
            ]
            before_seq = request.get("beforeSeq")
            if isinstance(before_seq, int) and not isinstance(before_seq, bool):
                events = [
                    entry
                    for entry in events
                    if int((entry.get("event") or {}).get("seq", -1)) < before_seq
                ]
            has_more = False
            if (
                self.subagent_history_page_size is not None
                and len(events) > self.subagent_history_page_size
            ):
                has_more = True
                events = events[-self.subagent_history_page_size :]
            return {"records": events, "hasMore": has_more}
        if method == "subagents/interruptByParent":
            return {"accepted": True}
        return {"accepted": True}

    async def respond(self, rpc_id: str, result: dict[str, Any]) -> bool:
        self.responses.append((rpc_id, result))
        return self.respond_accepts

    async def iter_frames(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            item = await self._queue.get()
            if item is _END_OF_STREAM:
                raise EngineStreamDetached("scripted downlink closed")
            yield item

    async def close(self) -> None:
        self.closed = True


def _client(link: ScriptedLink, *, native: str | None = None) -> DeepSeekHarnessEngineClient:
    return DeepSeekHarnessEngineClient(
        session_id=_SESSION_ID,
        link=link,
        native_session_id=native,
        cwd="/home/agent/workspace",
    )


def _command(*, content: str = "Reply with exactly: PROBE-OK", seq: int = 1) -> EngineInputCommand:
    return EngineInputCommand(
        command_id=f"cmd-{seq}",
        session_id=_SESSION_ID,
        sequence=seq,
        input_id=(
            _INPUT_ID
            if seq == 1
            else f"11111111-2222-4333-8444-{seq:012d}"
        ),
        content=content,
    )


async def _bound(link: ScriptedLink, *, native: str | None = None) -> DeepSeekHarnessEngineClient:
    client = _client(link, native=native)
    await client.bind_conversation(
        EngineConversationBinding(
            platform_session_id=_SESSION_ID, engine_session_key=native
        )
    )
    return client


# ── conversation identity ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_new_conversation_creates_the_session_and_keeps_its_id() -> None:
    link = ScriptedLink()
    client = await _bound(link)

    assert [name for name, _, _ in link.calls] == ["session/create"]
    # Exact: an Agent that names no preset sends no `agentPreset` at all, so
    # the harness profile's own default decides which agent runs.
    assert link.calls[0][1] == {"args": {"request": {"cwd": "/home/agent/workspace"}}}
    # The harness mints the id; the platform persists it as the resume handle.
    assert client.engine_session_key == _NATIVE_ID


@pytest.mark.asyncio
async def test_a_resumed_conversation_rejoins_by_name_without_creating() -> None:
    """A reattach must not create: the box's session never stopped."""

    link = ScriptedLink()
    client = await _bound(link, native=_NATIVE_ID)

    assert link.calls == []
    assert client.engine_session_key == _NATIVE_ID


@pytest.mark.asyncio
async def test_the_agents_chosen_preset_rides_the_create_that_pins_it() -> None:
    """Which harness agent runs is fixed at creation, so it rides there."""

    link = ScriptedLink()
    client = DeepSeekHarnessEngineClient(
        session_id=_SESSION_ID,
        link=link,
        cwd="/home/agent/workspace",
        session_create={"agentPreset": "minimal", "futureNativeOption": {"enabled": True}},
    )
    await client.bind_conversation(
        EngineConversationBinding(
            platform_session_id=_SESSION_ID, engine_session_key=None
        )
    )

    assert link.calls[0][1]["args"]["request"]["agentPreset"] == "minimal"
    assert link.calls[0][1]["args"]["request"]["futureNativeOption"] == {"enabled": True}


@pytest.mark.asyncio
async def test_a_rejoin_cannot_re_pin_the_preset() -> None:
    """The preset belongs to the session the box still holds.

    A reattach that re-sent it would either be ignored or would contradict the
    agent the conversation has been running as, and neither is worth risking
    for a value the create already settled.
    """

    link = ScriptedLink()
    client = DeepSeekHarnessEngineClient(
        session_id=_SESSION_ID,
        link=link,
        native_session_id=_NATIVE_ID,
        cwd="/home/agent/workspace",
        session_create={"agentPreset": "cordis"},
    )
    await client.bind_conversation(
        EngineConversationBinding(
            platform_session_id=_SESSION_ID, engine_session_key=_NATIVE_ID
        )
    )

    assert link.calls == []


@pytest.mark.asyncio
async def test_a_durable_key_that_contradicts_the_client_is_refused() -> None:
    link = ScriptedLink()
    client = _client(link, native=_NATIVE_ID)

    with pytest.raises(RuntimeError, match="resume key"):
        await client.bind_conversation(
            EngineConversationBinding(
                platform_session_id=_SESSION_ID,
                engine_session_key="session-somebody-else",
            )
        )


# ── delivery ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_delivery_is_idempotent_per_durable_command() -> None:
    """A redelivered command must not prompt the engine twice."""

    link = ScriptedLink()
    client = await _bound(link, native=_NATIVE_ID)
    command = _command()

    first = await client.begin_delivery(command)
    await client.deliver(command)
    second = await client.begin_delivery(command)

    assert [name for name, _, _ in link.calls] == ["session/prompt"]
    assert first is second


@pytest.mark.asyncio
async def test_the_prompt_names_this_input_with_an_id_the_client_can_recognize() -> None:
    link = ScriptedLink()
    client = await _bound(link, native=_NATIVE_ID)

    await client.begin_delivery(_command())

    method, payload, rpc_id = link.calls[0]
    assert method == "session/prompt"
    assert payload["args"]["request"]["sessionId"] == _NATIVE_ID
    assert payload["args"]["request"]["mode"] == "queue"
    assert payload["args"]["request"]["content"] == [
        {"type": "text", "text": "Reply with exactly: PROBE-OK"}
    ]
    assert rpc_id is not None and _INPUT_ID in rpc_id


@pytest.mark.asyncio
async def test_a_prompt_the_gateway_did_not_accept_fails_loudly() -> None:
    link = ScriptedLink()

    async def refuse(method: str, payload: dict[str, Any], *, rpc_id: str | None = None) -> Any:
        link.calls.append((method, dict(payload), rpc_id))
        return {"accepted": False}

    client = await _bound(link, native=_NATIVE_ID)
    link.call = refuse  # type: ignore[method-assign]

    with pytest.raises(EngineStreamDetached, match="not accepted"):
        await client.begin_delivery(_command())


# ── the recorded turn ────────────────────────────────────────────────────
async def _drive_recorded_turn(
    link: ScriptedLink, client: DeepSeekHarnessEngineClient
) -> list[dict[str, Any]]:
    command = _command()
    receipt = await client.begin_delivery(command)
    # Pin the minted id to the one the recording carries.
    client._prompted = {_RECORDED_RPC_TURN_1: command}  # noqa: SLF001
    return [frame async for frame in client.iter_turn_events(receipt)]


@pytest.mark.asyncio
async def test_the_turn_slot_is_free_once_a_consumer_stops_at_the_result() -> None:
    """A real consumer breaks at the terminal frame; the slot must be free.

    Every other turn test here drains the stream to exhaustion, which resumes
    the generator past its final yield and runs whatever follows it. A turn
    worker does not — it stops the moment it has the result, closing the
    generator — so settling after that yield never happens at all and the
    NEXT message in the same conversation is refused with "already has an
    active turn".

    Reported from the product against Hermes, whose stream had the same shape;
    `pi_client` had it before that. Only a second turn shows it, which is why
    one-turn probes and the live e2e all pass over it.
    """

    link = ScriptedLink(_load_mux(until_turn_end=1))
    client = await _bound(link, native=_NATIVE_ID)

    command = _command()
    receipt = await client.begin_delivery(command)
    client._prompted = {_RECORDED_RPC_TURN_1: command}  # noqa: SLF001

    async for frame in client.iter_turn_events(receipt):
        if frame.get("type") == "result":
            break  # exactly where a turn worker stops

    assert client.active_receipt is None, (
        "a held slot rejects the next message in this conversation"
    )


@pytest.mark.asyncio
async def test_a_recorded_turn_opens_with_consumption_and_closes_with_one_result() -> None:
    link = ScriptedLink(_load_mux(until_turn_end=1))
    client = await _bound(link, native=_NATIVE_ID)

    frames = await _drive_recorded_turn(link, client)

    assert all(isinstance(frame, EngineEmission) for frame in frames)
    assert frames[0]["type"] == "data-input-consumed"
    assert frames[0]["data"]["inputId"] == _INPUT_ID
    assert [f["type"] for f in frames].count("result") == 1
    assert frames[-1] == {
        "type": "result",
        "finishReason": "stop",
        "__engine_terminal_reason": "completed",
    } | {
        k: v for k, v in frames[-1].items() if k == "usage"
    }
    body = "".join(
        str(f.get("delta") or "") for f in frames if f["type"] == "text-delta"
    )
    assert "PROBE-OK" in body


@pytest.mark.asyncio
async def test_the_consumption_boundary_is_our_prompt_id_not_the_first_user_message() -> None:
    """The recorded turn carries a plugin's context snapshot as a user message.

    Anchoring on "the first user/message" would consume on either one — and on
    a resumed conversation, where the snapshot arrives first, it would consume
    on the wrong one. Only the echo of the id this client minted proves the
    engine dequeued THIS input.
    """

    frames = _load_mux(until_turn_end=1)
    user_messages = [
        f["payload"]["event"]
        for f in frames
        if f["type"] == "session/event"
        and f["payload"]["event"]["type"] == "user/message"
    ]
    sources = [event["data"]["source"] for event in user_messages]
    assert len(sources) >= 2, "the recording must contain the plugin snapshot"
    assert sources[0] == {"kind": "user", "rpcId": _RECORDED_RPC_TURN_1}
    assert any(source.get("kind") == "plugin" for source in sources[1:])

    # With a prompt id the recording does not carry, nothing is consumed — and
    # the turn must end by SAYING so, not by adopting the terminal of whatever
    # turn the stream happened to be carrying.
    link = ScriptedLink(frames)
    link.end_stream()
    client = await _bound(link, native=_NATIVE_ID)
    receipt = await client.begin_delivery(_command())
    client._prompted = {"astrabox-input:some-other-input": _command()}  # noqa: SLF001

    produced: list[dict[str, Any]] = []
    with pytest.raises(EngineStreamDetached):
        async for frame in client.iter_turn_events(receipt):
            produced.append(frame)

    assert not [f for f in produced if f["type"] == "data-input-consumed"]
    assert not [f for f in produced if f["type"] == "result"]


@pytest.mark.asyncio
async def test_frames_for_another_conversation_are_ignored() -> None:
    frames = _load_mux(until_turn_end=1)
    for frame in frames:
        frame["payload"]["sessionId"] = "session-not-ours"
    link = ScriptedLink(frames)
    link.end_stream()
    client = await _bound(link, native=_NATIVE_ID)
    receipt = await client.begin_delivery(_command())

    with pytest.raises(EngineStreamDetached):
        async for _ in client.iter_turn_events(receipt):
            pass


@pytest.mark.asyncio
async def test_a_gateway_stream_error_is_reported_not_filtered_away() -> None:
    """It carries no sessionId, so a session filter would swallow it.

    That frame is the only thing that ever explains why a downlink died; read
    after the filter, a dead stream looks exactly like a quiet one.
    """

    link = ScriptedLink(
        [
            {
                "rpcId": "rpc-err",
                "type": "stream/error",
                "payload": {
                    "type": "stream/error",
                    "error": {
                        "code": "internal",
                        "message": "session source iterator threw",
                        "details": {},
                    },
                },
            }
        ]
    )
    client = await _bound(link, native=_NATIVE_ID)
    receipt = await client.begin_delivery(_command())

    with pytest.raises(EngineStreamDetached, match="session source iterator threw"):
        async for _ in client.iter_turn_events(receipt):
            pass


@pytest.mark.asyncio
async def test_a_downlink_that_ends_before_turn_end_detaches_loudly() -> None:
    link = ScriptedLink()
    link.end_stream()
    client = await _bound(link, native=_NATIVE_ID)
    receipt = await client.begin_delivery(_command())

    with pytest.raises(EngineStreamDetached):
        async for _ in client.iter_turn_events(receipt):
            pass


# ── terminals from an earlier turn ───────────────────────────────────────
def _stale_turn_end(turn: int = 7) -> dict[str, Any]:
    """An earlier turn's terminal, arriving between a prompt and its echo.

    Restarting the host mid-turn produces this: the sandbox keeps running, so
    the turn it was answering finishes onto the connection the reattach just
    opened. A reattached connection replays nothing — it receives
    `session/subscribed` and no events — so the frame is live, and a sequence
    cursor cannot filter it. The turn boundary is what separates them.
    """

    return {
        "rpcId": "",
        "type": "session/event",
        "payload": {
            "type": "session/event",
            "sessionId": _NATIVE_ID,
            "event": {
                "type": "turn/end",
                "seq": 99,
                "time": 0,
                "data": {"turn": turn, "reason": {"kind": "completed"}},
            },
        },
    }


@pytest.mark.asyncio
async def test_another_turns_terminal_does_not_settle_this_one() -> None:
    """A terminal event for another native turn cannot settle this delivery.

    Treating the stale event as current would emit a result before consuming
    the durable FIFO head and leave the active turn without an answer.
    """

    link = ScriptedLink([_stale_turn_end(), *_load_mux(until_turn_end=1)])
    client = await _bound(link, native=_NATIVE_ID)
    command = _command()
    receipt = await client.begin_delivery(command)
    client._prompted = {_RECORDED_RPC_TURN_1: command}  # noqa: SLF001

    produced = [frame async for frame in client.iter_turn_events(receipt)]

    assert produced[0]["type"] == "data-input-consumed"
    assert [f["type"] for f in produced].count("result") == 1
    body = "".join(
        str(f.get("delta") or "") for f in produced if f["type"] == "text-delta"
    )
    assert "PROBE-OK" in body


@pytest.mark.asyncio
async def test_the_turn_frames_that_precede_our_echo_are_held_not_dropped() -> None:
    """The harness opens a turn before it echoes the prompt.

    Measured on the product wire: ``turn/start`` and ``step/start`` come at
    seq 4 and 6, the echo at seq 7. Those frames are this turn's, so dropping
    everything before the boundary would lose the step structure the console
    renders — they are held and released in order instead.
    """

    link = ScriptedLink([_stale_turn_end(), *_load_mux(until_turn_end=1)])
    client = await _bound(link, native=_NATIVE_ID)
    command = _command()
    receipt = await client.begin_delivery(command)
    client._prompted = {_RECORDED_RPC_TURN_1: command}  # noqa: SLF001

    produced = [frame async for frame in client.iter_turn_events(receipt)]
    kinds = [f["type"] for f in produced]

    assert kinds[0] == "data-input-consumed"
    # Released after the boundary, in the order the harness produced them.
    assert "start-step" in kinds
    assert kinds.index("start-step") > 0
    assert kinds.index("start-step") < kinds.index("text-delta")


# ── stopping ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_interrupt_uses_the_engines_own_cancel() -> None:
    """Stopping a turn must not cost the conversation."""

    link = ScriptedLink()
    client = await _bound(link, native=_NATIVE_ID)

    assert await client.interrupt_active_turn() is True
    assert link.calls[-1][0] == "session/cancel"
    assert link.calls[-1][1] == {"args": {"request": {"sessionId": _NATIVE_ID}}}
    assert link.closed is False


# ── child resources ─────────────────────────────────────────────────────
def _catalog_child(
    child_session_id: str,
    *,
    mode: str = "continuable",
    activity: str = "running",
    has_children: bool = False,
    label: str = "researcher",
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "kind": "child",
        "id": child_session_id,
        "mode": mode,
        "activity": activity,
        "hasChildren": has_children,
    }
    if mode == "continuable" or label:
        entry["label"] = label
    return entry


def _mux_session_event(
    session_id: str,
    event_type: str,
    *,
    seq: int,
    data: dict[str, Any],
    surface_op: str | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": event_type,
        "seq": seq,
        "time": seq,
        "data": data,
    }
    if surface_op is not None:
        event["surfaceOp"] = surface_op
    return {
        "rpcId": "",
        "type": "session/event",
        "payload": {
            "type": "session/event",
            "sessionId": session_id,
            "event": event,
        },
    }


def _root_turn_start_frame(turn: int = 1) -> dict[str, Any]:
    """The harness opens a turn before it echoes the prompt (seq 4 before 7
    on the recorded wire); a scripted turn starts the same way."""

    return _mux_session_event(_NATIVE_ID, "turn/start", seq=0, data={"turn": turn})


def _root_consumption_frame(rpc_id: str) -> dict[str, Any]:
    return _mux_session_event(
        _NATIVE_ID,
        "user/message",
        seq=1,
        data={
            "content": [{"type": "text", "text": "Reply with exactly: PROBE-OK"}],
            "source": {"kind": "user", "rpcId": rpc_id},
            "role": "user",
        },
        surface_op="append",
    )


def _root_terminal_frame(
    *, reason: dict[str, Any] | None = None
) -> dict[str, Any]:
    return _mux_session_event(
        _NATIVE_ID,
        "turn/end",
        seq=2,
        data={"turn": 1, "reason": reason or {"kind": "completed"}},
    )


async def _drive_minimal_root_turn(
    link: ScriptedLink,
    *,
    before_terminal: list[dict[str, Any]] | None = None,
    terminal: dict[str, Any] | None = None,
) -> tuple[DeepSeekHarnessEngineClient, list[EngineEmission]]:
    client = await _bound(link, native=_NATIVE_ID)
    receipt = await client.begin_delivery(_command())
    prompt_rpc_id = next(
        str(rpc_id)
        for method, _, rpc_id in link.calls
        if method == "session/prompt" and rpc_id is not None
    )
    link.push(_root_turn_start_frame())
    link.push(_root_consumption_frame(prompt_rpc_id))
    for frame in before_terminal or []:
        link.push(frame)
    link.push(terminal or _root_terminal_frame())
    return client, [frame async for frame in client.iter_turn_events(receipt)]


@pytest.mark.asyncio
async def test_dsh_child_catalog_and_history_cross_as_session_owned_facts() -> None:
    """A child Session is projected without letting its terminal settle the parent."""

    link = ScriptedLink()
    link.subagent_catalogs[_NATIVE_ID] = [_catalog_child(_CHILD_ID)]
    link.subagent_histories[_CHILD_ID] = [
        {
            "event": {
                "type": "user/message",
                "seq": 1,
                "time": 1,
                "surfaceOp": "append",
                "data": {
                    "content": [{"type": "text", "text": "inspect the queue"}],
                    "source": {"kind": "user"},
                    "role": "user",
                    "id": "message-user-1",
                },
            }
        },
        {
            "event": {
                "type": "assistant/message",
                "seq": 2,
                "time": 2,
                "surfaceOp": "append",
                "data": {
                    "message": {
                        "role": "assistant",
                        "id": "message-assistant-1",
                        "content": [
                            {"type": "reasoning", "text": "Checking."},
                            {"type": "text", "text": "Queue is empty."},
                            {"type": "vendor-widget", "value": {"count": 0}},
                        ],
                    }
                },
            }
        },
        {
            "event": {
                "type": "turn/end",
                "seq": 3,
                "time": 3,
                "data": {"turn": 1, "reason": {"kind": "completed"}},
            }
        },
    ]

    _, produced = await _drive_minimal_root_turn(link)

    assert produced[0]["type"] == "data-input-consumed"
    child_facts = [
        frame for frame in produced if frame.get("__engine_frame_scope") == "session"
    ]
    lifecycle = [frame["data"] for frame in child_facts if frame["data"]["kind"] == "lifecycle"]
    assert lifecycle[0]["engineRef"] == _CHILD_ID
    assert lifecycle[0]["event"] == "opened"
    assert lifecycle[0]["engineStatus"] == "running"
    assert lifecycle[0]["operations"] == ["stop"]
    assert lifecycle[-1]["event"] == "updated"
    assert lifecycle[-1]["engineReason"] == "completed"
    assert all(fact["event"] != "closed" for fact in lifecycle)

    messages = [frame["data"] for frame in child_facts if frame["data"]["kind"] == "message"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[1]["content"] == [
        {"type": "thinking", "thinking": "Checking."},
        {"type": "text", "text": "Queue is empty."},
        {"type": "vendor-widget", "value": {"count": 0}},
    ]
    assert [frame["type"] for frame in produced].count("result") == 1


@pytest.mark.asyncio
async def test_dsh_child_reconcile_reads_durable_state_without_a_root_turn() -> None:
    link = ScriptedLink()
    link.subagent_catalogs[_NATIVE_ID] = [_catalog_child(_CHILD_ID)]
    link.subagent_histories[_CHILD_ID] = [
        {
            "event": {
                "type": "assistant/message",
                "seq": 7,
                "time": 7,
                "surfaceOp": "append",
                "data": {
                    "message": {
                        "content": [{"type": "text", "text": "caught up"}]
                    }
                },
            }
        }
    ]
    client = await _bound(link, native=_NATIVE_ID)

    emissions = await client.reconcile_child_resources()

    assert all(isinstance(emission, ChildResourceFact) for emission in emissions)
    assert [emission["data"]["kind"] for emission in emissions] == [
        "lifecycle",
        "message",
    ]


@pytest.mark.asyncio
async def test_concurrent_dsh_child_reconciles_fold_one_authoritative_snapshot() -> None:
    link = ScriptedLink()
    link.subagent_catalogs[_NATIVE_ID] = [_catalog_child(_CHILD_ID)]
    client = await _bound(link, native=_NATIVE_ID)

    first, second = await asyncio.gather(
        client.reconcile_child_resources(),
        client.reconcile_child_resources(),
    )

    assert sorted((len(first), len(second))) == [0, 1]
    assert sum(
        emission["data"]["kind"] == "lifecycle"
        for emission in [*first, *second]
    ) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("discover_before_idle", [True, False])
async def test_idle_child_receipt_reaches_a_fresh_view_through_the_native_journal(
    discover_before_idle: bool,
) -> None:
    from astrabox.core.service.orchestrator.engine.deepseek_harness import (
        DeepSeekHarnessEngineAdapter,
    )
    from astrabox.core.service.orchestrator.engine.deepseek_harness_client import (
        _DshRelaySeam,
    )
    from astrabox.core.service.orchestrator.engine.platform_events import PlatformEngineEventSink
    from astrabox.core.service.orchestrator.engine.resident_relay import (
        CountedRecord,
        ResidentRelay,
    )
    from astrabox.core.service.orchestrator.session_child_run_view import SessionChildRunView

    class InitialCutLink(ScriptedLink):
        async def call(self, method, payload, *, rpc_id=None):
            value = await super().call(method, payload, rpc_id=rpc_id)
            if method == "session/follow":
                return {**value, "cursor": 7}
            if method == "session/page":
                cut = payload["args"]["request"]["throughSeq"]
                assert cut == 7
                return {
                    **value,
                    "records": [row for row in value["records"] if row["event"]["seq"] <= cut],
                }
            return value

    class Journal:
        def __init__(self):
            self.events = []
            self.frames = []

        async def try_claim_event(self, event):
            row = {**event, "event_seq": len(self.events) + 1}
            self.events.append(row)
            return row, True

        async def list_events(self, session_id, **kwargs):
            return [row for row in self.events if row["event_seq"] > kwargs.get("after_seq", 0)]

        async def list_frames(self, session_id, **kwargs):
            return [row for row in self.frames if row["frame_seq"] > kwargs.get("after_seq", -1)]

    async def unused(*args):
        raise AssertionError("the idle route must not start or command a parent turn")

    link = InitialCutLink()
    link.subagent_catalogs[_NATIVE_ID] = [_catalog_child(_CHILD_ID)]
    initial = {
        "type": "user/message",
        "seq": 7,
        "time": 7,
        "surfaceOp": "append",
        "data": {
            "source": {"kind": "user"},
            "content": [{"type": "text", "text": "delegated task"}],
        },
    }
    receipt = {
        "type": "assistant/message",
        "seq": 25,
        "time": 25,
        "surfaceOp": "append",
        "data": {
            "message": {
                "content": [{"type": "text", "text": "RECEIPT_3a9b605b2c434ee4b754d90407317b16"}]
            }
        },
    }
    link.subagent_histories[_CHILD_ID] = [{"event": initial}, {"event": receipt}]
    client = await _bound(link, native=_NATIVE_ID)
    journal = Journal()
    if discover_before_idle:
        for index, fact in enumerate(await client.reconcile_child_resources()):
            journal.frames.append(
                {
                    "scope": "session",
                    "turn_id": None,
                    "engine_kind": "deepseek_harness",
                    "frame_seq": index,
                    "payload": fact.as_frame(),
                }
            )
    relay = ResidentRelay(
        seam=_DshRelaySeam(client),
        session_id=_SESSION_ID,
        engine_session_key=_NATIVE_ID,
        next_record=unused,
        send_command=unused,
        current_sequence=unused,
        resident_output_sink=None,
        event_sink=PlatformEngineEventSink(_SESSION_ID, journal_repo=journal),
    )
    if not discover_before_idle:
        await relay._route(
            CountedRecord(
                sequence=1,
                record=_mux_session_event(_NATIVE_ID, "subagent/catalog", seq=10, data={}),
            )
        )
    calls_before_receipt = len(link.calls)
    receipt_frame = _mux_session_event(
        _CHILD_ID, "assistant/message", seq=25, data=receipt["data"], surface_op="append"
    )
    await relay._route(CountedRecord(sequence=2, record=receipt_frame))
    # The supplier can repush the same event; it must not duplicate a journal row.
    await relay._route(CountedRecord(sequence=3, record=receipt_frame))
    assert len(link.calls) == calls_before_receipt
    assert (
        sum(
            observed["record"].get("payload", {}).get("event", {}).get("seq") == 25
            for row in journal.events
            for observed in row["payload"]["message"]["records"]
        )
        == 1
    )
    for sequence, running in ((4, False), (5, True), (6, False)):
        await relay._route(
            CountedRecord(
                sequence=sequence,
                record={
                    "type": "emit",
                    "event": "api-session/status",
                    "args": [_CHILD_ID, running],
                },
            )
        )
        # Old catalog context says running; it must not override an idle update.
        row = (await SessionChildRunView(journal).list_child_runs(_SESSION_ID))[0]
        assert row["active"] is running
        assert row["closed"] is False
    await relay._route(
        CountedRecord(
            sequence=7,
            record={"type": "emit", "event": "api-session/removed", "args": [_CHILD_ID]},
        )
    )
    await client.close()
    view = SessionChildRunView(journal)
    child = (await view.list_child_runs(_SESSION_ID))[0]
    assert child["engine_event"] == "api-session/removed"
    assert child["engine_status"] == "inactive"
    assert child["active"] is False
    assert child["closed"] is False
    messages = await view.get_child_run_messages(_SESSION_ID, child["child_run_id"])
    assert messages is not None
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert "RECEIPT_3a9b605b2c434ee4b754d90407317b16" in json.dumps(messages)
    native_records = [row["payload"]["message"] for row in journal.events]
    adapter = DeepSeekHarnessEngineAdapter()
    assert [
        fact.as_frame() for _, fact in adapter.durable_child_resource_facts(native_records)
    ] == [fact.as_frame() for _, fact in adapter.durable_child_resource_facts(native_records)]


@pytest.mark.asyncio
async def test_native_catalog_replay_preserves_sibling_and_nested_frame_identity() -> None:
    from astrabox.core.service.orchestrator.engine.deepseek_harness import (
        DeepSeekHarnessEngineAdapter,
    )
    from astrabox.core.service.orchestrator.engine.deepseek_harness_client import _DshRelaySeam

    link = ScriptedLink()
    link.subagent_catalogs[_NATIVE_ID] = [
        _catalog_child(_CHILD_ID, has_children=True),
        _catalog_child("sibling-child"),
    ]
    link.subagent_catalogs[_CHILD_ID] = [_catalog_child(_GRANDCHILD_ID)]
    link.subagent_histories[_CHILD_ID] = [
        {
            "event": {
                "type": "user/message",
                "seq": 7,
                "time": 7,
                "surfaceOp": "append",
                "data": {"source": {"kind": "user"}, "content": [{"type": "text", "text": "task"}]},
            }
        }
    ]
    client = await _bound(link, native=_NATIVE_ID)
    seam = _DshRelaySeam(client)
    live = await seam.child_facts(
        _mux_session_event(_NATIVE_ID, "subagent/catalog", seq=10, data={})
    )
    records = seam.native_records()
    assert seam.native_records() == []
    cold = [
        fact.as_frame()
        for _, fact in DeepSeekHarnessEngineAdapter().durable_child_resource_facts(records)
    ]
    assert len({frame["id"] for frame in live}) == len(live)
    assert len({frame["id"] for frame in cold}) == len(cold)
    assert {frame["id"]: frame for frame in cold} == {
        frame["id"]: frame for frame in live
    }
    nested = next(frame["data"] for frame in cold if frame["data"]["engineRef"] == _GRANDCHILD_ID)
    assert nested["parentEngineRef"] == _CHILD_ID
    await client.close()


@pytest.mark.asyncio
async def test_a_live_child_is_discovered_when_the_parent_publishes_it() -> None:
    """The followed parent's catalog publishes the child's mode and identity."""

    link = ScriptedLink()
    link.subagent_catalogs[_NATIVE_ID] = [_catalog_child(_CHILD_ID)]
    link.subagent_catalog_sequences[_NATIVE_ID] = [
        [{"kind": "diagnostic", "id": _CHILD_ID, "reason": "unavailable"}],
        [_catalog_child(_CHILD_ID)],
    ]
    child_added = {
        "rpcId": "",
        "type": "emit",
        "event": "api-session/added",
        "args": [{
            "sessionId": _CHILD_ID,
            "parentSessionId": _NATIVE_ID,
            "origin": "subagent",
            "blank": True,
        }],
    }
    catalog_published = _mux_session_event(
        _NATIVE_ID,
        "subagent/catalog",
        seq=3,
        data={
            "version": 0,
            "childId": _CHILD_ID,
            "childCreatedAt": 2,
            "mode": "continuable",
            "label": "researcher",
        },
    )
    answer = _mux_session_event(
        _CHILD_ID,
        "assistant/message",
        seq=4,
        data={
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "live answer"}],
            }
        },
        surface_op="append",
    )
    child_terminal = _mux_session_event(
        _CHILD_ID,
        "turn/end",
        seq=5,
        data={"turn": 1, "reason": {"kind": "completed"}},
    )

    _, produced = await _drive_minimal_root_turn(
        link,
        before_terminal=[child_added, catalog_published, answer, child_terminal],
    )

    facts = [
        frame["data"]
        for frame in produced
        if frame.get("__engine_frame_scope") == "session"
    ]
    assert [fact["kind"] for fact in facts] == [
        "lifecycle",
        "message",
        "lifecycle",
    ]
    assert facts[0]["event"] == "opened"
    assert facts[1]["content"] == [{"type": "text", "text": "live answer"}]
    assert facts[2]["event"] == "updated"
    assert facts[2]["engineReason"] == "completed"
    assert any(
        frame.get("data", {}).get("subtype") == "subagent.catalog.unavailable"
        for frame in produced
    )
    assert [frame["type"] for frame in produced].count("result") == 1


@pytest.mark.asyncio
async def test_dsh_child_history_pages_rebuild_once_then_catch_up_by_sequence() -> None:
    link = ScriptedLink()
    link.subagent_catalogs[_NATIVE_ID] = [_catalog_child(_CHILD_ID)]
    link.subagent_history_page_size = 2
    link.subagent_histories[_CHILD_ID] = [
        {
            "event": {
                "type": "user/message",
                "seq": 1,
                "time": 1,
                "surfaceOp": "append",
                "data": {
                    "content": [{"type": "text", "text": "first"}],
                    "source": {"kind": "user"},
                },
            }
        },
        {
            "event": {
                "type": "assistant/message",
                "seq": 2,
                "time": 2,
                "surfaceOp": "append",
                "data": {
                    "message": {
                        "content": [{"type": "text", "text": "second"}]
                    }
                },
            }
        },
        {
            "event": {
                "type": "user/message",
                "seq": 3,
                "time": 3,
                "surfaceOp": "append",
                "data": {
                    "content": [{"type": "text", "text": "third"}],
                    "source": {"kind": "user-rpc", "rpcId": "follow-up"},
                },
            }
        },
        {
            "event": {
                "type": "assistant/message",
                "seq": 4,
                "time": 4,
                "surfaceOp": "append",
                "data": {
                    "message": {
                        "content": [{"type": "text", "text": "fourth"}]
                    }
                },
            }
        },
        {
            "event": {
                "type": "turn/end",
                "seq": 5,
                "time": 5,
                "data": {"turn": 2, "reason": {"kind": "completed"}},
            }
        },
    ]

    client, first = await _drive_minimal_root_turn(link)

    first_messages = [
        frame["data"]
        for frame in first
        if frame.get("__engine_frame_scope") == "session"
        and frame["data"]["kind"] == "message"
    ]
    assert [message["content"][0]["text"] for message in first_messages] == [
        "first",
        "second",
        "third",
        "fourth",
    ]
    history_calls = [
        payload
        for method, payload, _ in link.calls
        if method == "session/page"
    ]
    assert [payload["args"]["request"].get("beforeSeq") for payload in history_calls] == [None, 4, 2]

    link.subagent_histories[_CHILD_ID].append(
        {
            "event": {
                "type": "assistant/message",
                "seq": 6,
                "time": 6,
                "surfaceOp": "append",
                "data": {
                    "message": {
                        "content": [{"type": "text", "text": "sixth"}]
                    }
                },
            }
        }
    )
    command = _command(seq=2)
    receipt = await client.begin_delivery(command)
    prompt_rpc_id = next(
        str(rpc_id)
        for method, _, rpc_id in reversed(link.calls)
        if method == "session/prompt" and rpc_id is not None
    )
    link.push(_root_turn_start_frame())
    link.push(_root_consumption_frame(prompt_rpc_id))
    link.push(_root_terminal_frame())
    second = [frame async for frame in client.iter_turn_events(receipt)]

    second_messages = [
        frame["data"]
        for frame in second
        if frame.get("__engine_frame_scope") == "session"
        and frame["data"]["kind"] == "message"
    ]
    assert [message["content"][0]["text"] for message in second_messages] == [
        "sixth"
    ]


@pytest.mark.asyncio
async def test_a_continuable_child_can_become_inactive_and_run_again() -> None:
    link = ScriptedLink()
    link.subagent_catalogs[_NATIVE_ID] = [
        _catalog_child(_CHILD_ID, activity="inactive")
    ]
    running = {
        "type": "emit",
        "event": "api-session/status",
        "args": [_CHILD_ID, True],
    }

    _, produced = await _drive_minimal_root_turn(link, before_terminal=[running])

    lifecycle = [
        frame["data"]
        for frame in produced
        if frame.get("__engine_frame_scope") == "session"
        and frame["data"]["kind"] == "lifecycle"
    ]
    assert [(fact["event"], fact["engineStatus"], fact["operations"]) for fact in lifecycle] == [
        ("opened", "inactive", []),
        ("updated", "running", ["stop"]),
    ]
    assert all(fact["event"] != "closed" for fact in lifecycle)


@pytest.mark.asyncio
async def test_an_inactive_one_shot_child_is_closed_not_resumable() -> None:
    link = ScriptedLink()
    link.subagent_catalogs[_NATIVE_ID] = [
        _catalog_child(_CHILD_ID, mode="one-shot", activity="inactive")
    ]

    _, produced = await _drive_minimal_root_turn(link)

    lifecycle = next(
        frame["data"]
        for frame in produced
        if frame.get("__engine_frame_scope") == "session"
        and frame["data"]["kind"] == "lifecycle"
    )
    assert lifecycle["event"] == "closed"
    assert lifecycle["engineStatus"] == "inactive"
    assert lifecycle["operations"] == []
    assert "controlRef" not in lifecycle


@pytest.mark.asyncio
async def test_nested_dsh_children_keep_the_vendors_parent_identity() -> None:
    link = ScriptedLink()
    link.subagent_catalogs[_NATIVE_ID] = [
        _catalog_child(_CHILD_ID, has_children=True)
    ]
    link.subagent_catalogs[_CHILD_ID] = [_catalog_child(_GRANDCHILD_ID)]

    _, produced = await _drive_minimal_root_turn(link)

    lifecycle = {
        str(frame["data"]["engineRef"]): frame["data"]
        for frame in produced
        if frame.get("__engine_frame_scope") == "session"
        and frame["data"]["kind"] == "lifecycle"
    }
    assert "parentEngineRef" not in lifecycle[_CHILD_ID]
    assert lifecycle[_GRANDCHILD_ID]["parentEngineRef"] == _CHILD_ID


@pytest.mark.asyncio
async def test_dsh_child_stop_keeps_the_native_address_private_to_the_adapter() -> None:
    link = ScriptedLink()
    link.subagent_catalogs[_NATIVE_ID] = [_catalog_child(_CHILD_ID)]
    client, produced = await _drive_minimal_root_turn(link)
    lifecycle = next(
        frame["data"]
        for frame in produced
        if frame.get("__engine_frame_scope") == "session"
        and frame["data"]["kind"] == "lifecycle"
    )
    control_ref = str(lifecycle["controlRef"])

    assert isinstance(client, EngineChildRunControl)
    assert control_ref not in {_CHILD_ID, _NATIVE_ID}
    assert json.loads(control_ref) == {
        "childSessionId": _CHILD_ID,
        "mode": "continuable",
        "parentSessionId": _NATIVE_ID,
    }

    await client.stop_child_run(control_ref)

    assert link.calls[-1] == (
        "subagents/interruptByParent",
        {"args": {
            "childSessionId": _CHILD_ID,
            "mode": "continuable",
            "parentSessionId": _NATIVE_ID,
        }},
        None,
    )
    assert (await client.get_capabilities()).supports_child_run_control is True


@pytest.mark.asyncio
async def test_dsh_child_stop_rejects_a_malformed_native_address() -> None:
    link = ScriptedLink()
    client = await _bound(link, native=_NATIVE_ID)

    with pytest.raises(APIError, match="child-run control reference"):
        await client.stop_child_run(json.dumps({"childSessionId": _CHILD_ID}))

    assert not [call for call in link.calls if call[0] == "subagents/interruptByParent"]


# ── permission presets ───────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_engines_own_presets_are_the_modes_the_console_offers() -> None:
    """One selector, because that is the shape the harness itself publishes.

    Its `permissions` projection carries the current list, and each option
    bundles a sandbox mode with an approval policy on the engine's side.
    """

    client = _client(ScriptedLink(), native=_NATIVE_ID)
    manifest = await client.get_capabilities()

    assert manifest.permission_modes == [
        "read-only",
        "workspace-write",
        "danger-full-access",
    ]
    assert isinstance(client, EnginePermissionModes)


@pytest.mark.asyncio
async def test_a_permission_preset_added_by_the_harness_is_discovered() -> None:
    link = ScriptedLink()
    link.permission_options.append("vendor-new-mode")
    client = _client(link, native=_NATIVE_ID)

    manifest = await client.get_capabilities()

    assert manifest.permission_modes[-1] == "vendor-new-mode"
    assert link.calls[-1] == (
        "session/follow",
        {"args": {"request": {"address": {"kind": "session", "sessionId": _NATIVE_ID}}}},
        None,
    )


@pytest.mark.asyncio
async def test_a_missing_permission_projection_fails_the_handshake() -> None:
    link = ScriptedLink()
    link.permission_options.clear()
    client = _client(link, native=_NATIVE_ID)

    with pytest.raises(APIError, match="permissions projection has no presets"):
        await client.get_capabilities()


@pytest.mark.asyncio
async def test_selecting_a_preset_uses_the_harnesss_own_command() -> None:
    link = ScriptedLink()
    client = await _bound(link, native=_NATIVE_ID)

    await client.set_permission_mode("danger-full-access")

    method, payload, _ = link.calls[-1]
    assert method == "commands/execute"
    assert payload == {
        "args": {
            "agentId": _NATIVE_ID,
            "line": "/permission danger-full-access",
            "submittedAttachments": [],
        }
    }


@pytest.mark.asyncio
async def test_a_preset_the_harness_did_not_take_fails_loudly() -> None:
    """The command endpoint answers ok for a line it did not recognise.

    Measured: `/permissionPresets danger-full-access` — the name from the
    vendor's README rather than its command table — came back
    `{"ok": true}` with no command result, and nothing changed. Reading the
    envelope alone would report success on a mode that never applied.
    """

    link = ScriptedLink()
    link.command_reply = None
    client = await _bound(link, native=_NATIVE_ID)

    with pytest.raises(APIError, match="refused permission preset"):
        await client.set_permission_mode("danger-full-access")


# ── the model the platform chose ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_conversation_is_moved_to_the_platforms_model() -> None:
    """`session/create` has no model field, so this is the only way in.

    The vendor's `SessionCreateRequest` declares workspace, cwd, session
    identity and the Agent preset and nothing else, so a created conversation
    always runs on the deployment's own default until a separate Remote moves
    it. Without this call every turn asked the DeepSeek adapter's advertised
    default no matter which model the Environment named, and the gateway
    refused a model nobody selected.
    """

    link = ScriptedLink()
    client = await _bound(link, native=_NATIVE_ID)

    await client.select_model("gpt-5.6-luna")

    method, payload, _ = link.calls[-1]
    assert method == "session/selectModel"
    assert payload == {
        "args": {
            "request": {
                "sessionId": _NATIVE_ID,
                "provider": "deepseek-official",
                "model": "gpt-5.6-luna",
            }
        }
    }


@pytest.mark.asyncio
async def test_a_gateway_model_outside_the_harnesss_catalogue_is_selectable() -> None:
    """The advisory catalogue is not the set of selectable models.

    The DeepSeek route advertises its own three models and states that any
    other id passes through to the wire, which is what lets an AstraBox
    gateway model be chosen here. A client that checked the catalogue first
    would refuse exactly the models this platform serves.
    """

    link = ScriptedLink()
    client = await _bound(link, native=_NATIVE_ID)

    await client.select_model("gpt-5.6-luna")

    assert "session/modelCatalog" not in [name for name, _, _ in link.calls]


@pytest.mark.asyncio
async def test_a_selection_that_landed_on_another_model_fails_loudly() -> None:
    """The reply is the vendor's normalized selection, so it is evidence.

    A normalization that quietly resolved elsewhere would otherwise read as
    success here and surface as another model's answers, which is the exact
    shape of the defect this call exists to close.
    """

    link = ScriptedLink()
    link.selected_model = {
        "provider": "deepseek-official",
        "model": "deepseek-v4-flash",
    }
    client = await _bound(link, native=_NATIVE_ID)

    with pytest.raises(APIError, match="installed a different model"):
        await client.select_model("gpt-5.6-luna")


@pytest.mark.asyncio
async def test_a_reply_without_a_selection_is_not_read_as_success() -> None:
    link = ScriptedLink()
    link.selected_model = {}
    client = await _bound(link, native=_NATIVE_ID)

    with pytest.raises(APIError, match="installed a different model"):
        await client.select_model("gpt-5.6-luna")


# ── failures the harness reports beside the turn ─────────────────────────
@pytest.mark.asyncio
async def test_a_reported_failure_lets_the_durable_terminal_settle_the_turn() -> None:
    """`api-session/error` is a failure report, not a lost connection.

    The vendor declares it as "a step or turn errored ... even when the error
    has no in-turn position", relays it in its own client as the session's
    last agent error, and keeps the session live; every turn-scoped failure is
    ALSO appended durably as `turn/end` with reason kind `error`. The emit
    arrives first, so reading it as a detached stream replaced the vendor's
    own message with a lost connection and sent the platform into recovery —
    a rejected model name reached the user as "the live engine client does
    not implement output reconnect".
    """

    link = ScriptedLink()
    reported = {
        "type": "emit",
        "event": "api-session/error",
        "args": [_NATIVE_ID, "Invalid model name passed in model=deepseek-v4-flash"],
    }
    _, produced = await _drive_minimal_root_turn(
        link,
        before_terminal=[reported],
        terminal=_root_terminal_frame(
            reason={
                "kind": "error",
                "error": {
                    "code": "MODEL_UNAVAILABLE",
                    "message": (
                        "Invalid model name passed in model=deepseek-v4-flash"
                    ),
                },
            }
        ),
    )

    terminals = [frame for frame in produced if frame["type"] == "result"]
    assert len(terminals) == 1, "the durable turn/end settles the turn"
    assert terminals[0]["finishReason"] == "error"
    assert "deepseek-v4-flash" in terminals[0]["error"]["message"]


@pytest.mark.asyncio
async def test_a_downlink_that_stops_after_a_reported_failure_names_it() -> None:
    """A stream that stops instead of settling still has to say why.

    The report is the only account of the cause when no `turn/end` follows,
    so it rides on the detach rather than being dropped with it.
    """

    link = ScriptedLink()
    client = await _bound(link, native=_NATIVE_ID)
    receipt = await client.begin_delivery(_command())
    prompt_rpc_id = next(
        str(rpc_id)
        for method, _, rpc_id in link.calls
        if method == "session/prompt" and rpc_id is not None
    )
    link.push(_root_turn_start_frame())
    link.push(_root_consumption_frame(prompt_rpc_id))
    link.push(
        {
            "type": "emit",
            "event": "api-session/error",
            "args": [_NATIVE_ID, "session log is unwritable"],
        }
    )
    link.end_stream()

    with pytest.raises(EngineStreamDetached) as detached:
        async for _ in client.iter_turn_events(receipt):
            pass

    # Both halves: the transport fact the link reported, and the cause the
    # harness reported beside it. Reading that report as the detach itself
    # carries the second without the first, which is what this replaced.
    assert "scripted downlink closed" in str(detached.value)
    assert "session log is unwritable" in str(detached.value)


@pytest.mark.asyncio
async def test_another_conversations_reported_failure_is_not_ours() -> None:
    link = ScriptedLink()
    other = {
        "type": "emit",
        "event": "api-session/error",
        "args": ["session-someone-elses", "a different conversation failed"],
    }
    _, produced = await _drive_minimal_root_turn(link, before_terminal=[other])

    assert [frame["type"] for frame in produced].count("result") == 1
    assert produced[-1]["finishReason"] == "stop"


# ── interactions ─────────────────────────────────────────────────────────
def _approval_frame() -> dict[str, Any]:
    return {
        "eventId": "rpc-approval-1",
        "type": "waterfall",
        "event": "approval/request",
        "agentId": _NATIVE_ID,
        "request": {
            "toolName": "write",
            "callId": "call_00_ET",
            "reason": "writing outside the workspace",
        },
    }


def _question_frame() -> dict[str, Any]:
    return {
        "eventId": "rpc-question-1",
        "type": "waterfall",
        "event": "user-questions/request",
        "agentId": _NATIVE_ID,
        "request": {
            "questions": [
                {
                    "id": "q1",
                    "question": "Which database?",
                    "header": "Database",
                    "detail": "This decides the migration path.",
                    "options": [
                        {"label": "Postgres", "description": "the default"},
                        {"label": "SQLite"},
                    ],
                },
                {
                    "id": "q2",
                    "question": "Which extras?",
                    "multiSelect": True,
                    "options": [{"label": "pgvector"}, {"label": "PostGIS"}],
                },
            ],
        },
    }


async def _park_on(frame: dict[str, Any]) -> tuple[ScriptedLink, DeepSeekHarnessEngineClient, dict[str, Any]]:
    link = ScriptedLink()
    client = await _bound(link, native=_NATIVE_ID)
    receipt = await client.begin_delivery(_command())
    prompt_rpc_id = next(
        str(rpc_id)
        for method, _, rpc_id in link.calls
        if method == "session/prompt" and rpc_id is not None
    )
    link.push(_root_turn_start_frame())
    link.push(_root_consumption_frame(prompt_rpc_id))
    link.push(frame)
    produced = [f async for f in client.iter_turn_events(receipt)]
    assert [f["type"] for f in produced] == [
        "data-input-consumed",
        "interaction.request",
    ]
    return link, client, produced[-1]


@pytest.mark.asyncio
async def test_the_client_declares_the_interaction_capability_it_implements() -> None:
    client = _client(ScriptedLink(), native=_NATIVE_ID)
    manifest = await client.get_capabilities()

    assert manifest.supports_interaction is True
    assert isinstance(client, EngineInteractions)


@pytest.mark.asyncio
async def test_the_client_does_not_claim_server_metadata_the_gateway_never_supplies() -> None:
    client = _client(ScriptedLink(), native=_NATIVE_ID)
    manifest = await client.get_capabilities()

    assert manifest.supports_server_info is False
    assert not isinstance(client, EngineServerInfo)


@pytest.mark.asyncio
async def test_an_approval_parks_the_turn_with_a_contract_the_platform_accepts() -> None:
    _, _, request = await _park_on(_approval_frame())
    contract = dict(request["payload"])
    tool_use_id = contract.pop("tool_use_id")

    assert request["interactionId"] == "rpc-approval-1"
    assert validate_interaction_contract(contract) == "tool_approval"
    assert contract["tool_name"] == "write"
    assert contract["prompt"] == "writing outside the workspace"
    # The vendor's frame rides verbatim, which is what the answer is built from.
    assert contract["raw_input"]["toolName"] == "write"
    assert tool_use_id == "call_00_ET"


@pytest.mark.asyncio
async def test_an_approval_answer_carries_the_vendors_own_outcome() -> None:
    link, client, request = await _park_on(_approval_frame())
    contract = dict(request["payload"])
    contract.pop("tool_use_id")
    pending = build_pending_interaction_record(
        contract=contract,
        session_id=_SESSION_ID,
        turn_id="turn-1",
        interaction_id=request["interactionId"],
        tool_call_id="call_00_ET",
    )

    assert await client.submit_interaction_response(
        client.active_receipt or _fake_receipt(), pending=pending, response={"decision": "approve"}
    )
    rpc_id, result = link.responses[-1]
    assert rpc_id == "rpc-approval-1"
    assert result == {
        "ok": True,
        "value": "allowed-once",
    }

    await client.submit_interaction_response(
        client.active_receipt or _fake_receipt(), pending=pending, response={"decision": "reject"}
    )
    assert link.responses[-1][1]["value"] == "rejected"


@pytest.mark.asyncio
async def test_a_question_becomes_a_form_that_keeps_every_vendor_string() -> None:
    _, _, request = await _park_on(_question_frame())
    contract = request["payload"]

    assert validate_interaction_contract(contract) == "form"
    rows = contract["questions"]
    assert [row["id"] for row in rows] == ["q1", "q2"]
    assert rows[0]["header"] == "Database"
    # detail has no field of its own here; joining keeps it visible instead of
    # dropping a string the vendor wrote for the user to read.
    assert "This decides the migration path." in rows[0]["question"]
    assert rows[0]["multi_select"] is False
    assert rows[1]["multi_select"] is True
    assert rows[0]["options"][0] == {
        "label": "Postgres",
        "description": "the default",
    }


@pytest.mark.asyncio
async def test_a_form_answer_matches_the_request_the_harness_will_check_it_against() -> None:
    """Same count, same order, same ids, labels only from declared options."""

    link, client, request = await _park_on(_question_frame())
    pending = build_pending_interaction_record(
        contract=request["payload"],
        session_id=_SESSION_ID,
        turn_id="turn-1",
        interaction_id=request["interactionId"],
        tool_call_id=None,
    )

    assert await client.submit_interaction_response(
        client.active_receipt or _fake_receipt(),
        pending=pending,
        response={
            "answers": [
                {"question_id": "q1", "option_label": "SQLite"},
                {"question_id": "q2", "option_labels": ["pgvector", "PostGIS"]},
            ]
        },
    )

    _, result = link.responses[-1]
    assert set(result["value"]) == {"answers"}
    assert result["value"]["answers"] == [
        {"id": "q1", "selected": ["SQLite"]},
        {"id": "q2", "selected": ["pgvector", "PostGIS"]},
    ]


@pytest.mark.asyncio
async def test_free_text_answers_a_single_select_as_custom_never_as_a_selection() -> None:
    """The harness refuses a selection that is not one of its own labels."""

    link, client, request = await _park_on(_question_frame())
    pending = build_pending_interaction_record(
        contract=request["payload"],
        session_id=_SESSION_ID,
        turn_id="turn-1",
        interaction_id=request["interactionId"],
        tool_call_id=None,
    )

    await client.submit_interaction_response(
        client.active_receipt or _fake_receipt(),
        pending=pending,
        response={
            "answers": [
                {"question_id": "q1", "free_text": "MySQL, actually"},
                {"question_id": "q2", "option_labels": ["pgvector"]},
            ]
        },
    )

    answers = link.responses[-1][1]["value"]["answers"]
    assert answers[0] == {"id": "q1", "selected": [], "custom": "MySQL, actually"}


@pytest.mark.asyncio
async def test_a_declined_question_is_the_harnesss_own_cancellation() -> None:
    link, client, request = await _park_on(_question_frame())
    pending = build_pending_interaction_record(
        contract=request["payload"],
        session_id=_SESSION_ID,
        turn_id="turn-1",
        interaction_id=request["interactionId"],
        tool_call_id=None,
    )

    await client.submit_interaction_response(
        client.active_receipt or _fake_receipt(),
        pending=pending,
        response={"decline": True},
    )

    result = link.responses[-1][1]
    assert result["ok"] is False
    assert result["error"]["code"] == "cancelled"


@pytest.mark.asyncio
async def test_a_refused_answer_is_reported_as_not_accepted() -> None:
    link, client, request = await _park_on(_approval_frame())
    link.respond_accepts = False
    contract = dict(request["payload"])
    contract.pop("tool_use_id")
    pending = build_pending_interaction_record(
        contract=contract,
        session_id=_SESSION_ID,
        turn_id="turn-1",
        interaction_id=request["interactionId"],
        tool_call_id=None,
    )

    accepted = await client.submit_interaction_response(
        client.active_receipt or _fake_receipt(),
        pending=pending,
        response={"decision": "approve"},
    )

    assert accepted is False


@pytest.mark.asyncio
async def test_a_parked_turn_resumes_into_the_same_engine_turn() -> None:
    """The engine never stopped; the platform only ended a stream segment.

    Rebuilding the turn state on re-entry would wait for a consumption
    boundary that was already crossed, and the continuation would hang.
    """

    frames = _load_mux(until_turn_end=1)
    link = ScriptedLink()
    client = await _bound(link, native=_NATIVE_ID)
    command = _command()
    receipt = await client.begin_delivery(command)
    prompt_rpc_id = next(
        str(rpc_id)
        for method, _, rpc_id in link.calls
        if method == "session/prompt" and rpc_id is not None
    )
    link.push(_root_turn_start_frame())
    link.push(_root_consumption_frame(prompt_rpc_id))
    link.push(_approval_frame())

    parked = [f async for f in client.iter_turn_events(receipt)]
    assert [f["type"] for f in parked] == [
        "data-input-consumed",
        "interaction.request",
    ]
    client._prompted = {_RECORDED_RPC_TURN_1: command}  # noqa: SLF001

    for frame in frames:
        link.push(frame)
    resumed = [f async for f in client.iter_turn_events(receipt)]

    assert resumed[-1]["type"] == "result"


def _fake_receipt() -> Any:
    """Only the interaction path is under test; the receipt is unread there."""

    return object()


# ── the transcript carries conversation, not bookkeeping ─────────────────
@pytest.mark.asyncio
async def test_an_ordinary_turn_puts_no_bookkeeping_cards_in_the_transcript() -> None:
    """A user asked why one message produced a pile of them.

    Every event the translator does not recognise becomes a ``data-raw-event``
    frame, and the console renders each one as a card. A plain turn carries
    the session's permission knobs and the command log of the preset the
    platform itself selected — nine cards for one question. None of it is
    conversation, and all of it is state the platform already holds.
    """

    link = ScriptedLink(_load_mux(until_turn_end=1))
    client = await _bound(link, native=_NATIVE_ID)
    command = _command()
    receipt = await client.begin_delivery(command)
    client._prompted = {_RECORDED_RPC_TURN_1: command}  # noqa: SLF001

    produced = [frame async for frame in client.iter_turn_events(receipt)]

    cards = [
        str((f.get("data") or {}).get("subtype"))
        for f in produced
        if f["type"] == "data-raw-event"
    ]
    assert cards == [], f"bookkeeping reached the transcript: {cards}"
    # and the turn itself is still whole
    assert produced[0]["type"] == "data-input-consumed"
    assert produced[-1]["type"] == "result"
    assert "PROBE-OK" in "".join(
        str(f.get("delta") or "") for f in produced if f["type"] == "text-delta"
    )


@pytest.mark.asyncio
async def test_an_event_the_adapter_cannot_name_is_still_carried() -> None:
    """The drop list names identified events; it is not a general filter.

    An event this adapter has never seen reaches the reader as a card rather
    than disappearing, so each entry in the drop list stays a deliberate
    choice.
    """

    unknown = {
        "rpcId": "",
        "type": "session/event",
        "payload": {
            "type": "session/event",
            "sessionId": _NATIVE_ID,
            "event": {
                "type": "something/nobody-has-shipped-yet",
                "seq": 5,
                "time": 0,
                "data": {"detail": "hello"},
            },
        },
    }
    # Inside the turn: a record outside any run has no response to be
    # carried on, which is the relay's routing rather than the drop list.
    frames = _load_mux(until_turn_end=1)
    opened = next(
        i for i, f in enumerate(frames) if (f["payload"].get("event") or {}).get("type") == "turn/start"
    )
    link = ScriptedLink([*frames[: opened + 1], unknown, *frames[opened + 1 :]])
    client = await _bound(link, native=_NATIVE_ID)
    command = _command()
    receipt = await client.begin_delivery(command)
    client._prompted = {_RECORDED_RPC_TURN_1: command}  # noqa: SLF001

    produced = [frame async for frame in client.iter_turn_events(receipt)]

    subtypes = [
        str((f.get("data") or {}).get("subtype"))
        for f in produced
        if f["type"] == "data-raw-event"
    ]
    assert subtypes == ["something/nobody-has-shipped-yet"]


@pytest.mark.asyncio
async def test_a_stale_catalog_page_cannot_reopen_a_closed_child() -> None:
    """`subagent.list` is a snapshot, and snapshots arrive out of order.

    A refresh issued before the engine finished closing a one-shot child
    comes back describing it as running, after the close was already seen.
    Failing the protocol on that killed the whole turn — the user's answer
    lost to a stale page (p141, p178). The close stands, and no second
    lifecycle frame claims otherwise.
    """

    link = ScriptedLink()
    link.subagent_catalogs[_NATIVE_ID] = [
        _catalog_child(_CHILD_ID, mode="one-shot", activity="inactive"),
        _catalog_child(_CHILD_ID, mode="one-shot", activity="running"),
    ]

    _, produced = await _drive_minimal_root_turn(link)

    lifecycle = [
        frame["data"]
        for frame in produced
        if frame.get("__engine_frame_scope") == "session"
        and frame["data"]["kind"] == "lifecycle"
        and frame["data"]["engineRef"] == _CHILD_ID
    ]
    assert [item["event"] for item in lifecycle] == ["closed"]
