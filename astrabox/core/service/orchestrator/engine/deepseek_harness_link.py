"""Authenticated Remote RPC and streams for the supplier's Web runtime.

The vendor owns launch tokens, browser cookies, Remote descriptors and stream
lifetimes. This adapter carries its HTTP envelopes and one remote.mux socket;
platform lifecycle and transcript storage remain outside this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import websockets

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.base import EngineStreamDetached
from astrabox.core.service.orchestrator.runtime.pty_terminal import (
    ResolvedExecdEndpoint,
    resolve_sandbox_endpoint,
    websocket_header_kwargs,
)

DSH_API_PORT = 44780
_REQUEST_TIMEOUT_SECONDS = 30.0
_CONNECT_TIMEOUT_SECONDS = 20.0


class DshApiError(RuntimeError):
    """Native gateway refusal with its machine-readable code retained."""

    def __init__(self, method: str, code: str, message: str) -> None:
        super().__init__(f"{method}: {code}: {message}")
        self.method = method
        self.code = code
        self.api_message = message


class DshApiLink:
    """One authenticated connection to the supplier's already-running service."""

    def __init__(
        self, *, endpoint: ResolvedExecdEndpoint, sandbox: Any = None,
        http_transport: Any = None,
    ) -> None:
        self.endpoint = endpoint
        self._sandbox = sandbox
        self._http_transport = http_transport
        self._headers = dict(endpoint.headers)
        self._frames: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._socket: Any = None
        self._pump: asyncio.Task[None] | None = None
        self._closed = False
        self._detached: str | None = None
        self._streams: dict[str, tuple[str, dict[str, Any]]] = {}
        self._openings: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._event_client_id: str | None = None

    @classmethod
    async def connect(
        cls, sandbox: Any, *, port: int = DSH_API_PORT,
        launch_url_path: str = "/home/agent/.deepseek-harness/web-url",
    ) -> "DshApiLink":
        endpoint = await resolve_sandbox_endpoint(sandbox, port)
        link = cls(endpoint=endpoint, sandbox=sandbox)
        try:
            launch_url = str(await sandbox.files.read_file(launch_url_path) or "").strip()
            await link._authenticate(launch_url)
            await link._attach_downlinks()
        except BaseException:
            await link.close()
            raise
        return link

    @property
    def is_live(self) -> bool:
        return not self._closed and self._pump is not None and not self._pump.done()

    async def _authenticate(self, launch_url: str) -> None:
        tokens = parse_qs(urlparse(launch_url).query).get("token", [])
        if len(tokens) != 1 or not tokens[0]:
            raise EngineStreamDetached("deepseek_harness launch URL has no unique vendor token")
        try:
            async with self._http_client() as client:
                response = await client.get(
                    f"{self.endpoint.origin}/?{urlencode({'token': tokens[0]})}",
                    follow_redirects=False,
                )
        except httpx.HTTPError:
            raise EngineStreamDetached("deepseek_harness token exchange transport failed") from None
        cookies = response.headers.get_list("set-cookie")
        if response.status_code != 303 or not cookies:
            raise EngineStreamDetached(
                f"deepseek_harness vendor token exchange failed (HTTP {response.status_code})"
            )
        self._headers["Cookie"] = "; ".join(value.split(";", 1)[0] for value in cookies)

    async def call(
        self, method: str, payload: dict[str, Any], *, rpc_id: str | None = None,
    ) -> Any:
        """Send native named arguments; never invent parameter defaults."""
        if method == "session/follow":
            return await self.follow_session(payload["args"]["request"]["address"])
        body = {
            "type": "client-request", "rpcId": str(rpc_id or uuid.uuid4().hex),
            "method": method, "payload": dict(payload),
        }
        async with self._http_client() as client:
            try:
                response = await client.post(f"{self.endpoint.origin}/api/{method}", json=body)
            except httpx.HTTPError as exc:
                raise EngineStreamDetached(f"deepseek_harness RPC {method!r} transport failed") from exc
        if response.status_code != 200:
            raise EngineStreamDetached(
                f"deepseek_harness RPC {method!r} returned HTTP {response.status_code}"
            )
        return _unwrap_result(method, response.json())

    async def respond(self, rpc_id: str, result: dict[str, Any]) -> bool:
        if self._event_client_id is None:
            raise EngineStreamDetached("deepseek_harness Remote event stream is not ready")
        if result.get("ok") is True:
            outcome = {"kind": "result", "value": result.get("value")}
        else:
            outcome = {"kind": "rejected", "error": result.get("error")}
        await self.call("$events/result", {"args": {
            "clientId": self._event_client_id, "eventId": rpc_id, "outcome": outcome,
        }})
        return True

    async def follow_session(self, address: dict[str, Any]) -> dict[str, Any]:
        """Open the supplier's snapshot-then-events stream before prompting."""
        key = json.dumps(address, sort_keys=True)
        if key not in self._snapshots:
            snapshot = await self._open_stream("session/follow", {"args": {"request": {
                "address": address, "maxMessages": 1, "assistantStream": True,
            }}})
            if snapshot.get("type") != "snapshot":
                raise EngineStreamDetached("deepseek_harness follow did not open with a snapshot")
            self._snapshots[key] = snapshot
        return self._snapshots[key]

    async def iter_frames(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            if self._detached is not None and self._frames.empty():
                raise EngineStreamDetached(self._detached)
            frame = await self._frames.get()
            if frame is None:
                raise EngineStreamDetached(self._detached or "deepseek_harness stream closed")
            yield frame

    async def _attach_downlinks(self) -> None:
        self._socket = await websockets.connect(
            f"{self._ws_origin()}/api/remote.mux", open_timeout=_CONNECT_TIMEOUT_SECONDS,
            **websocket_header_kwargs(self._headers),
        )
        self._pump = asyncio.create_task(self._pump_frames())
        ready = await self._open_stream("$events", {"args": {}})
        if ready.get("type") != "ready" or not ready.get("clientId"):
            raise EngineStreamDetached("deepseek_harness Remote events missing ready identity")
        self._event_client_id = str(ready["clientId"])

    async def _open_stream(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        stream_id = uuid.uuid4().hex
        opening: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._streams[stream_id] = (endpoint, payload)
        self._openings[stream_id] = opening
        await self._socket.send(json.dumps({
            "type": "open", "streamId": stream_id, "endpoint": endpoint, "payload": payload,
        }))
        try:
            return await asyncio.wait_for(opening, timeout=_REQUEST_TIMEOUT_SECONDS)
        except BaseException:
            self._streams.pop(stream_id, None)
            self._openings.pop(stream_id, None)
            await self._socket.send(json.dumps({"type": "cancel", "streamId": stream_id}))
            raise

    async def _pump_frames(self) -> None:
        try:
            async for raw in self._socket:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise EngineStreamDetached("deepseek_harness Remote mux emitted a non-object")
                stream_id = str(message.get("streamId") or "")
                if stream_id not in self._streams:
                    continue
                kind = message.get("type")
                if kind != "item":
                    raise EngineStreamDetached(
                        f"deepseek_harness Remote stream {kind}: {message.get('error', {})}"
                    )
                value = message.get("value")
                if not isinstance(value, dict):
                    raise EngineStreamDetached("deepseek_harness Remote stream item is not an object")
                opening = self._openings.get(stream_id)
                if opening is not None:
                    endpoint, payload = self._streams[stream_id]
                    if endpoint == "session/follow" and value.get("type") == "snapshot":
                        address = payload["args"]["request"]["address"]
                        session_id = address.get("sessionId") or address["childSessionId"]
                        baseline = value.get("assistantStream")
                        if not isinstance(baseline, dict):
                            raise EngineStreamDetached("deepseek_harness follow omitted assistant baseline")
                        await self._frames.put({
                            "type": "session/assistant-stream-snapshot",
                            "payload": {"sessionId": session_id, "baseline": baseline},
                        })
                    opening.set_result(value)
                    self._openings.pop(stream_id, None)
                    continue
                endpoint, payload = self._streams[stream_id]
                if endpoint == "session/follow":
                    address = payload["args"]["request"]["address"]
                    session_id = address.get("sessionId") or address["childSessionId"]
                    if value.get("type") == "event":
                        await self._frames.put({
                            "type": "session/event",
                            "payload": {"sessionId": session_id, "event": value["event"]},
                        })
                    elif value.get("type") == "assistant-stream":
                        await self._frames.put({
                            "type": "session/assistant-stream",
                            "payload": {"sessionId": session_id, "frame": value["frame"]},
                        })
                    else:
                        raise EngineStreamDetached("deepseek_harness follow emitted an unknown frame")
                elif endpoint == "$events":
                    await self._frames.put(value)
            raise EngineStreamDetached("deepseek_harness Remote mux closed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._detached = f"deepseek_harness Remote stream failed: {type(exc).__name__}: {exc}"
        finally:
            self._detached = self._detached or "deepseek_harness Remote stream closed"
            for future in self._openings.values():
                if not future.done():
                    future.set_exception(EngineStreamDetached(self._detached))
            self._openings.clear()
            await self._frames.put(None)

    async def close(self) -> None:
        self._closed = True
        self._detached = self._detached or "deepseek_harness link closed"
        if self._socket is not None:
            with contextlib.suppress(Exception):
                await self._socket.close()
        if self._pump is not None:
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump
        await self._frames.put(None)

    def _http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._http_transport, timeout=_REQUEST_TIMEOUT_SECONDS,
            headers=self._headers,
        )

    def _ws_origin(self) -> str:
        return self.endpoint.origin.replace("https://", "wss://", 1).replace("http://", "ws://", 1)

def _unwrap_result(method: str, body: Any) -> Any:
    """The value of a server-response, or the gateway's error raised."""

    if not isinstance(body, dict) or body.get("type") != "server-response":
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=(
                f"deepseek_harness gateway call {method!r} did not answer with "
                "a server-response envelope"
            ),
            status_code=502,
        )
    result = body.get("result")
    if not isinstance(result, dict):
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=f"deepseek_harness gateway call {method!r} carries no result",
            status_code=502,
        )
    if result.get("ok") is True:
        return result.get("value")
    error = result.get("error")
    code = message = ""
    if isinstance(error, dict):
        code = str(error.get("code") or "")
        message = str(error.get("message") or "")
    raise DshApiError(method, code or "unknown", message or "no message")
