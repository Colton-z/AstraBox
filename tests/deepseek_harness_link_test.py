"""Native cookie authentication, Remote RPC and multiplexed stream lifecycle."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
import websockets

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.base import EngineOutputAlreadyObserved, EngineStreamDetached
from astrabox.core.service.orchestrator.engine.deepseek_harness_link import DshApiError, DshApiLink
from astrabox.core.service.orchestrator.runtime.pty_terminal import ResolvedExecdEndpoint


_ENDPOINT = ResolvedExecdEndpoint(origin="http://box.test:44780", headers={"X-Route": "box"})


def _link(handler: Any) -> DshApiLink:
    return DshApiLink(endpoint=_ENDPOINT, http_transport=httpx.MockTransport(handler))


def _response(rpc_id: str, value: Any) -> httpx.Response:
    return httpx.Response(200, json={
        "type": "server-response", "rpcId": rpc_id,
        "result": {"ok": True, "value": value},
    })


@pytest.mark.asyncio
async def test_token_exchange_uses_actual_endpoint_and_preserves_vendor_cookie() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(303, headers={"set-cookie": "dsh-session=signed; HttpOnly; Path=/", "location": "/"})
        return _response(json.loads(request.content)["rpcId"], {"items": []})

    link = _link(handler)
    await link._authenticate("http://127.0.0.1:44781/?token=vendor-secret")
    assert await link.call("session/list", {"args": {"_request": {}}}) == {"items": []}
    assert str(seen[0].url) == "http://box.test:44780/?token=vendor-secret"
    assert seen[1].headers["cookie"] == "dsh-session=signed"
    assert seen[1].headers["x-route"] == "box"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,cookie", [(401, None), (200, "x=y"), (303, None)])
async def test_authentication_requires_native_redirect_and_cookie(status: int, cookie: str | None) -> None:
    link = _link(lambda _: httpx.Response(status, headers={"set-cookie": cookie} if cookie else {}))
    with pytest.raises(EngineStreamDetached) as raised:
        await link._authenticate("http://127.0.0.1:44781/?token=private-secret")
    assert "private-secret" not in str(raised.value)


@pytest.mark.asyncio
async def test_call_sends_native_named_arguments_and_caller_rpc_identity() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _response(json.loads(request.content)["rpcId"], {"items": []})

    payload = {"args": {"_request": {}}}
    value = await _link(handler).call("session/list", payload, rpc_id="caller-owned")
    assert value == {"items": []}
    assert str(seen[0].url) == "http://box.test:44780/api/session/list"
    assert json.loads(seen[0].content) == {
        "type": "client-request", "rpcId": "caller-owned",
        "method": "session/list", "payload": payload,
    }
    assert seen[0].headers["x-route"] == "box"


@pytest.mark.asyncio
async def test_an_error_result_raises_rather_than_returning_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "type": "server-response", "rpcId": json.loads(request.content)["rpcId"],
            "result": {"ok": False, "error": {"code": "bad-request", "message": "invalid payload", "details": {}}},
        })

    with pytest.raises(DshApiError) as raised:
        await _link(handler).call("session/prompt", {"args": {}})
    assert raised.value.code == "bad-request"
    assert raised.value.api_message == "invalid payload"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 500])
async def test_http_rejections_are_not_successful_remote_results(status: int) -> None:
    with pytest.raises(EngineStreamDetached, match=f"HTTP {status}"):
        await _link(lambda _: httpx.Response(status)).call("session/list", {"args": {"_request": {}}})


@pytest.mark.asyncio
async def test_a_non_envelope_body_is_refused() -> None:
    with pytest.raises(APIError):
        await _link(lambda _: httpx.Response(200, json={"items": []})).call("session/list", {"args": {"_request": {}}})


@pytest.mark.asyncio
async def test_waterfall_reply_carries_native_identity_and_refusal_raises() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        assert request.url.path == "/api/$events/result"
        if body["payload"]["args"]["eventId"] == "stale":
            return httpx.Response(200, json={
                "type": "server-response", "rpcId": body["rpcId"],
                "result": {"ok": False, "error": {"code": "not-pending", "message": "stale event", "details": {}}},
            })
        return _response(body["rpcId"], None)

    link = _link(handler)
    link._event_client_id = "native-generation"
    value = {"outcome": "allowed-once"}
    assert await link.respond("live", {"ok": True, "value": value}) is True
    assert bodies[0]["payload"] == {"args": {
        "clientId": "native-generation", "eventId": "live",
        "outcome": {"kind": "result", "value": value},
    }}
    with pytest.raises(DshApiError, match="not-pending"):
        await link.respond("stale", {"ok": True, "value": value})


async def _serve(
    *, terminal: str | None = None, seen: list | None = None,
    follow_values: list[dict[str, Any]] | None = None,
):
    async def handler(socket: Any) -> None:
        if seen is not None:
            seen.append((socket.request.path, dict(socket.request.headers)))
        async for raw in socket:
            request = json.loads(raw)
            if request["type"] != "open":
                continue
            stream = request["streamId"]
            endpoint = request["endpoint"]
            if seen is not None:
                seen.append(request)
            values = (
                [{"type": "ready", "clientId": "client-1", "host": {"home": "/home/agent"}},
                 {"type": "emit", "event": "api-session/added", "args": [{"id": "child-1", "parentSessionId": "root-1"}]},
                 {"type": "waterfall", "event": "approval/request", "eventId": "approval-1", "agentId": "root-1", "request": {"command": "ls"}}]
                if endpoint == "$events" else
                [{"type": "snapshot", "header": {"version": 0, "id": "root-1", "createdAt": 1}, "cursor": 0, "records": [], "hasMore": False, "projections": {}, "assistantStream": {"revision": 0}},
                 {"type": "event", "event": {"type": "turn/end", "time": 1}}]
            )
            if endpoint == "session/follow" and follow_values is not None:
                values = follow_values
            for value in values:
                await socket.send(json.dumps({"type": "item", "streamId": stream, "value": value}))
            if endpoint == "session/follow" and terminal:
                if terminal == "close":
                    await socket.close()
                else:
                    await socket.send(json.dumps({"type": terminal, "streamId": stream, "error": {"code": "closed", "message": "closed", "details": {}}}))

    return await websockets.serve(handler, "127.0.0.1", 0)


def _link_for(server: Any) -> DshApiLink:
    return DshApiLink(endpoint=ResolvedExecdEndpoint(
        origin=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", headers={"Cookie": "native=signed"},
    ))


@pytest.mark.asyncio
async def test_one_mux_preserves_root_lifecycle_interaction_and_follow_ordering() -> None:
    seen: list = []
    server = await _serve(seen=seen)
    link = _link_for(server)
    try:
        await link._attach_downlinks()
        assert link.is_live
        snapshot = await link.follow_session({"kind": "session", "sessionId": "root-1"})
        assert snapshot["type"] == "snapshot"
        iterator = link.iter_frames()
        frames = [await asyncio.wait_for(anext(iterator), 1) for _ in range(4)]
        assert seen[0][0] == "/astrabox/output?takeover=1"
        assert seen[0][1]["cookie"] == "native=signed"
        assert [item["endpoint"] for item in seen[1:]] == ["$events", "session/follow"]
        assert seen[2]["payload"]["args"]["request"]["address"] == {"kind": "session", "sessionId": "root-1"}
        assert seen[2]["payload"]["args"]["request"]["assistantStream"] is True
        assert frames[0]["args"][0]["parentSessionId"] == "root-1"
        assert frames[1]["eventId"] == "approval-1"
        assert frames[1]["agentId"] == "root-1"
        assert frames[2] == {"type": "session/assistant-stream-snapshot", "payload": {"sessionId": "root-1", "cursor": 0, "baseline": {"revision": 0}}}
        assert frames[3] == {"type": "session/event", "payload": {"sessionId": "root-1", "event": {"type": "turn/end", "time": 1}}}
        await link.close()
        assert not link.is_live
    finally:
        await link.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["end", "error", "close"])
async def test_logical_or_physical_stream_ending_detaches_complete_view(terminal: str) -> None:
    server = await _serve(terminal=terminal)
    link = _link_for(server)
    try:
        await link._attach_downlinks()
        await link.follow_session({"kind": "session", "sessionId": "root-1"})
        iterator = link.iter_frames()
        for _ in range(4):
            await asyncio.wait_for(anext(iterator), 1)
        with pytest.raises(EngineStreamDetached):
            await asyncio.wait_for(anext(iterator), 1)
        assert not link.is_live
    finally:
        await link.close()
        server.close()
        await server.wait_closed()


def _history_page(seqs: list[int], *, has_more: bool) -> dict[str, Any]:
    return {
        "records": [{"type": "event", "event": {"seq": seq, "type": "turn/end"}} for seq in seqs],
        "hasMore": has_more,
    }


def _follow_values(cursor: int) -> list[dict[str, Any]]:
    return [
        {"type": "snapshot", "cursor": cursor, "assistantStream": {"revision": 7}},
        {"type": "event", "event": {"seq": cursor + 1, "type": "turn/start"}},
    ]


@pytest.mark.asyncio
async def test_resume_pages_the_fixed_cut_before_baseline_and_buffered_live_output() -> None:
    pages = [_history_page([4, 5], has_more=True), _history_page([1, 2, 3], has_more=True)]
    seen: list = []
    requests: list[dict[str, Any]] = []
    reading_history = asyncio.Event()
    release_history = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body["payload"]["args"]["request"])
        reading_history.set()
        await release_history.wait()
        return _response(body["rpcId"], pages[len(requests) - 1])

    server = await _serve(seen=seen, follow_values=_follow_values(5))
    link = _link_for(server)
    link._http_transport = httpx.MockTransport(handler)
    follow = None
    try:
        await link._attach_downlinks()
        iterator = link.iter_frames()
        # Drain the independent Remote events before inspecting the follow suffix.
        await asyncio.wait_for(anext(iterator), 1)
        await asyncio.wait_for(anext(iterator), 1)
        address = {"kind": "session", "sessionId": "root-1"}
        follow = asyncio.create_task(link.follow_session(address, after_sequence=1))
        await asyncio.wait_for(reading_history.wait(), 1)
        assert not follow.done()
        assert link._frames.empty()
        release_history.set()
        snapshot = await asyncio.wait_for(follow, 1)
        assert snapshot["cursor"] == 5
        frames = [await asyncio.wait_for(anext(iterator), 1) for _ in range(6)]
        assert [frame["payload"]["event"]["seq"] for frame in frames[:4]] == [2, 3, 4, 5]
        assert frames[4]["type"] == "session/assistant-stream-snapshot"
        assert frames[5]["payload"]["event"]["seq"] == 6
        assert requests == [
            {"address": address, "throughSeq": 5, "maxMessages": 100},
            {"address": address, "throughSeq": 5, "maxMessages": 100, "beforeSeq": 4},
        ]
        assert seen[2]["payload"]["args"]["request"] == {
            "address": address, "maxMessages": 1, "assistantStream": True,
        }
        with pytest.raises(ValueError, match="already-open"):
            await link.follow_session(address, after_sequence=5)
    finally:
        release_history.set()
        if follow is not None and not follow.done():
            follow.cancel()
            await asyncio.gather(follow, return_exceptions=True)
        await link.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("pages,after_sequence,error", [
    ([_history_page([2, 4, 5], has_more=False)], 1, "contiguous"),
    ([_history_page([2, 3, 4, 5, 6], has_more=False)], 1, "contiguous"),
    ([_history_page([], has_more=True)], 1, "empty page"),
    ([_history_page([4, 5], has_more=True)] * 2, 1, "did not advance"),
    ([_history_page([4, 5], has_more=True), {
        "records": [{"event": {"seq": 4, "type": "turn/start"}}], "hasMore": False,
    }], 1, "reused an event seq"),
    ([], 6, "precedes"),
])
async def test_incomplete_or_inconsistent_resume_never_exposes_live_output(
    pages: list[dict[str, Any]], after_sequence: int, error: str,
) -> None:
    remaining = iter(pages)

    def handler(request: httpx.Request) -> httpx.Response:
        return _response(json.loads(request.content)["rpcId"], next(remaining))

    server = await _serve(follow_values=_follow_values(5))
    link = _link_for(server)
    link._http_transport = httpx.MockTransport(handler)
    try:
        await link._attach_downlinks()
        iterator = link.iter_frames()
        await asyncio.wait_for(anext(iterator), 1)
        await asyncio.wait_for(anext(iterator), 1)
        with pytest.raises(EngineStreamDetached, match=error):
            await link.follow_session({"kind": "session", "sessionId": "root-1"}, after_sequence=after_sequence)
        with pytest.raises(EngineStreamDetached, match=error):
            await asyncio.wait_for(anext(iterator), 1)
        assert not link.is_live
    finally:
        await link.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("observe_only,status", [(True, 409), (False, 409), (True, 401)])
async def test_only_an_observer_conflict_is_reported_as_an_existing_collector(
    observe_only: bool, status: int,
) -> None:
    paths: list[str] = []

    def reject(connection: Any, request: Any) -> Any:
        paths.append(request.path)
        return connection.respond(status, "rejected")

    async def handler(socket: Any) -> None:
        raise AssertionError("rejected handshake must never upgrade")

    server = await websockets.serve(handler, "127.0.0.1", 0, process_request=reject)
    link = DshApiLink(endpoint=ResolvedExecdEndpoint(
        origin=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", headers={},
    ), observe_only=observe_only)
    try:
        expected = EngineOutputAlreadyObserved if observe_only and status == 409 else websockets.exceptions.InvalidStatus
        with pytest.raises(expected):
            await link._attach_downlinks()
        assert paths == [f"/astrabox/output?takeover={0 if observe_only else 1}"]
        assert not link.is_live
    finally:
        await link.close()
        server.close()
        await server.wait_closed()
