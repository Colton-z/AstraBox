"""A platform message's image and file reach the native engine through the bundled channel gateway.

The platform is a Satori server this test runs, reached through the shipped
``@satorijs/adapter-satori`` adapter. It announces one proxy prefix, as a real
adapter registers its CDN, and serves one file through an ``internal:`` link.
"""
from __future__ import annotations

import asyncio
import base64
import json
import socket
import threading
import time
import uuid

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from tests.e2e._sandbox_helpers import (
    current_profile, data, psql_rows, release_agent, release_session, sql_literal, transcript_entries,
)
from tests.e2e._service_containers import SERVER_CONTAINER_HANDLE, require_service_container
from tests.e2e.satori_server_test import _solid_png

pytestmark = [pytest.mark.e2e, pytest.mark.deployment_shape]


class Platform:
    """A Satori server with one bot login, as a messaging platform's adapter sees it."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.image = _solid_png((0, 0, 255))
        self.table = b"item,amount\nalpha,3\nbeta,4\n"
        self.requests: list[str] = []
        self.replies: list[dict] = []
        self.identified = threading.Event()
        self.outbox: asyncio.Queue | None = None
        self.loop: asyncio.AbstractEventLoop | None = None

    def app(self) -> Starlette:
        async def events(websocket: WebSocket) -> None:
            await websocket.accept()
            self.loop, self.outbox = asyncio.get_running_loop(), asyncio.Queue()
            identify = await websocket.receive_json()
            assert identify["op"] == 3, identify
            await websocket.send_json({"op": 4, "body": {
                "logins": [{"sn": 1, "platform": "fake", "user": {"id": "bot", "name": "Bot", "is_bot": True},
                            "status": 1, "adapter": "fake", "features": ["message.create"]}],
                "proxy_urls": [f"{self.base}/cdn/"],
            }})
            self.identified.set()

            async def pings() -> None:
                while True:
                    frame = await websocket.receive_json()
                    if frame.get("op") == 1:
                        await websocket.send_json({"op": 2, "body": {}})

            reader = asyncio.create_task(pings())
            try:
                while True:
                    await websocket.send_json(await self.outbox.get())
            except WebSocketDisconnect:
                pass
            finally:
                reader.cancel()

        async def resource(request: Request) -> Response:
            self.requests.append(request.url.path)
            path = request.url.path
            if path == "/cdn/blue.png":
                return Response(self.image, media_type="image/png")
            if path == "/v1/internal/fake/bot/files/data.csv":
                return Response(self.table, media_type="text/csv",
                                headers={"content-disposition": "attachment; filename=data.csv"})
            return Response(b"secret", media_type="text/plain")

        async def api(request: Request) -> Response:
            body = await request.json()
            if request.path_params["method"] == "message.create":
                self.replies.append(body)
                return JSONResponse([{"id": f"reply-{len(self.replies)}", "content": body.get("content", "")}])
            return JSONResponse({})

        return Starlette(routes=[
            WebSocketRoute("/v1/events", events),
            Route("/cdn/{name}", resource),
            Route("/private/{name}", resource),
            Route("/v1/internal/{path:path}", resource),
            Route("/v1/{method}", api, methods=["POST"]),
        ])

    def send(self, event: dict) -> None:
        assert self.loop is not None and self.outbox is not None
        self.loop.call_soon_threadsafe(self.outbox.put_nowait, {"op": 0, "body": event})


def _reachable_host() -> str:
    """The address the server container reaches this runner at: its network's gateway."""
    import subprocess

    row = json.loads(subprocess.check_output(["docker", "inspect", require_service_container(SERVER_CONTAINER_HANDLE)]))[0]
    networks = row["NetworkSettings"]["Networks"]
    if not networks:
        # A replica sharing another container's network namespace reports none of its own.
        mode = row["HostConfig"]["NetworkMode"].removeprefix("container:")
        networks = json.loads(subprocess.check_output(["docker", "inspect", mode]))[0]["NetworkSettings"]["Networks"]
    return next(network["Gateway"] for network in networks.values() if network.get("Gateway"))


def test_a_platform_message_s_image_and_file_reach_the_engine(
    e2e_client: httpx.Client, live_test_deadline: float,
) -> None:
    client = e2e_client
    profile = current_profile()
    assert profile["engine_kind"] == "claude_code"
    with socket.socket() as probe:
        probe.bind(("0.0.0.0", 0))
        port = probe.getsockname()[1]
    platform = Platform(f"http://{_reachable_host()}:{port}")
    server = uvicorn.Server(uvicorn.Config(platform.app(), host="0.0.0.0", port=port, log_level="warning"))
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    nonce = uuid.uuid4().hex[:10]
    try:
        agent = data(client.post("/api/v1/agents", json={
            "name": f"Channel attachments {nonce}", "model": profile["model"],
            "runtime_profile_name": profile["runtime_profile_name"], "prepared_capacity": 0,
        }))
        release_agent(agent["agent_id"])
        # The deployment's preparation provider may require the account the
        # channel runs as to authorize the Agent first; perform what it offers.
        preflight = f"/api/satori/agents/{agent['agent_id']}/v1/internal/astrabox.preparation."
        routing = {"Satori-Platform": "astrabox", "Satori-User-ID": agent["agent_id"]}
        state = client.post(preflight + "get", json={}, headers=routing).json()
        for action in state.get("actions") or ():
            if state["ready"]:
                break
            state = client.post(preflight + "perform", json={"action_id": action["id"]}, headers=routing).json()
        assert state["ready"], state
        data(client.post(f"/api/v1/admin/agents/{agent['agent_id']}/deployments", json={
            "scene": "channel:satori", "prompt_prefix": "", "enabled": True,
            "channel_config": {"endpoint": platform.base}, "credentials": {"token": "platform-token"},
        }))
        assert platform.identified.wait(timeout=max(1, live_test_deadline - time.monotonic())), "gateway never connected"
        platform.send({
            "sn": 1, "type": "message-created", "timestamp": int(time.time() * 1000),
            "login": {"platform": "fake", "user": {"id": "bot"}},
            "channel": {"id": f"room-{nonce}", "type": 1},
            "user": {"id": "alice", "name": "Alice"},
            "message": {"id": f"message-{nonce}", "content": (
                "Reply with the color of the image and the total of the amount column, nothing else."
                f'<img src="{platform.base}/cdn/blue.png" title="blue.png"/>'
                '<file src="internal:fake/bot/files/data.csv" title="data.csv"/>'
                f'<file src="{platform.base}/private/secret.txt" title="secret.txt"/>'
            )},
        })

        def wait(predicate, label):
            while time.monotonic() < live_test_deadline:
                if result := predicate():
                    return result
                time.sleep(0.3)
            pytest.fail(f"{label} did not happen; requests={platform.requests} replies={platform.replies}")

        row = wait(lambda: psql_rows(
            "SELECT i.session_id, i.input_id, i.content FROM session_inputs i JOIN sessions s USING (session_id) "
            f"WHERE s.agent_id={sql_literal(agent['agent_id'])}"), "the platform message's input")[0]
        sid, input_id = row["session_id"], row["input_id"]
        release_session(sid)
        assert f"uploads/{input_id}/blue.png" in row["content"] and f"uploads/{input_id}/data.csv" in row["content"]
        assert "secret.txt: the channel does not serve it" in row["content"], row["content"]
        assert "/private/secret.txt" not in platform.requests, platform.requests
        stored = psql_rows("SELECT kind, name, size FROM input_attachments "
                           f"WHERE session_id={sql_literal(sid)} AND input_id={sql_literal(input_id)} ORDER BY ordinal")
        assert [(r["kind"], r["name"], r["size"]) for r in stored] == [
            ("image", "blue.png", len(platform.image)), ("file", "data.csv", len(platform.table))], stored

        reply = wait(lambda: platform.replies and platform.replies[-1], "the agent's reply")
        assert "blue" in reply["content"].lower() and "7" in reply["content"], reply
        for name, expected in (("blue.png", platform.image), ("data.csv", platform.table)):
            download = client.get(f"/api/v1/sessions/{sid}/files/download",
                                  params={"path": f"uploads/{input_id}/{name}"})
            assert download.status_code == 200 and download.content == expected, (name, download.status_code)
        encoded = base64.b64encode(platform.image).decode()
        native = [entry["entry"] for entry in transcript_entries(sid) if entry["entry"].get("type") == "user"]
        assert any(isinstance(entry.get("message", {}).get("content"), list) and any(
            block.get("type") == "image" and block.get("source", {}).get("data") == encoded
            for block in entry["message"]["content"]) for entry in native), "no native image block"
        print(json.dumps({"phase": "channel-attachments-accepted", "session_id": sid, "input_id": input_id}), flush=True)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
