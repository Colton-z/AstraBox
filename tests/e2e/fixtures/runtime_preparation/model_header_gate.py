"""Hold one actual model response's headers before the sandbox edge reads them.

The selected latency deployment runs this fixture on model port 80 in the
server's network namespace, with an external inference gateway. Configuration,
controls and receipts stay in its private state volume, outside the workspace.
The configuration names the fixed upstream URL and the actual edge Docker
handle as ``upstream`` and ``edge_container``. The case arms one response;
catalogue reads and other requests pass through without a hold.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from starlette.responses import StreamingResponse

CONFIG = Path("/data/runtime-preparation-model-gate-config.json")
CONTROL = Path("/data/runtime-preparation-model-gate-control.json")
RECEIPTS = Path("/data/runtime-preparation-model-gate-receipts.jsonl")
_HOP_HEADERS = {"host", "connection", "transfer-encoding", "content-length", "keep-alive"}


def _record(marker: str, request_id: str, phase: str, **values: object) -> None:
    with RECEIPTS.open("a") as output:
        output.write(json.dumps({
            "marker": marker, "request_id": request_id, "phase": phase,
            "at": time.time(), **values,
        }) + "\n")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    config = json.loads(CONFIG.read_text())
    app.state.upstream = str(config["upstream"]).rstrip("/")
    async with httpx.AsyncClient(timeout=180, trust_env=False) as client:
        app.state.client = client
        yield


app = FastAPI(lifespan=_lifespan)


@app.api_route("/{path:path}", methods=["GET", "POST"])
async def forward(request: Request, path: str) -> StreamingResponse:
    body = await request.body()
    control = json.loads(CONTROL.read_text()) if CONTROL.is_file() else None
    held = (
        control is not None and request.method == "POST"
        and request.url.path.rstrip("/") in {"/v1/messages", "/messages"}
        and str(control["marker"]).encode() in body
    )
    marker = str(control["marker"]) if held else ""
    request_id = uuid.uuid4().hex
    entered = time.monotonic()
    if held:
        assert request.client is not None
        _record(marker, request_id, "received", path=request.url.path,
                peer=request.client.host,
                body_sha256=hashlib.sha256(body).hexdigest())
    target = request.app.state.upstream + "/" + path
    if request.url.query:
        target += "?" + request.url.query
    headers = {key: value for key, value in request.headers.items() if key.lower() not in _HOP_HEADERS}
    client: httpx.AsyncClient = request.app.state.client
    upstream = client.build_request(request.method, target, headers=headers, content=body)
    response: httpx.Response | None = None
    try:
        response = await client.send(upstream, stream=True)
        if held:
            await asyncio.sleep(float(control["seconds"]))
            _record(marker, request_id, "headers", status=response.status_code,
                    quiet_seconds=time.monotonic() - entered)
    except BaseException as error:
        if response is not None:
            await response.aclose()
        if held:
            _record(marker, request_id, "failed", error_type=type(error).__name__)
        raise

    async def content():
        size = 0
        try:
            async for chunk in response.aiter_raw():
                size += len(chunk)
                yield chunk
        finally:
            await response.aclose()
            if held:
                _record(marker, request_id, "closed", bytes=size)

    return StreamingResponse(
        content(), status_code=response.status_code,
        headers={key: value for key, value in response.headers.items() if key.lower() not in _HOP_HEADERS},
    )
