#!/usr/bin/env python3
"""E2E fault: withhold one runner ``input_ack`` receipt on the runner wire.

Runs inside a spec-owned conversation-tenancy sandbox. The image-baked runner
(``sandbox_runner.py``) is relaunched on a loopback port and this proxy takes
its public port. Every frame of the ``astrabox.runner-wire.v1`` websocket is
forwarded byte-for-byte in both directions, and the plain-HTTP ``GET /health``
readiness probe is answered from the runner behind it.

The one deliberate difference: after the host's ``input`` frame for the armed
Session reaches the runner and the runner answers ``input_ack`` — the receipt
that ``RunnerLink.deliver()`` waits for, and the only proof the platform ever
gets that ``RunnerSession.submit()`` accepted the input — that receipt is held
on the wire until a release marker appears. The runner journaled the receipt,
so a host reattach replays it; the replay is held too. Frames behind the held
receipt queue in their original order. The reader can observe the real SDK
Result while the writer still withholds every frame behind the receipt;
nothing overtakes it on the host wire.

State is a JSON file the spec reads through ``sandboxExec``; the release is a
marker file the spec creates. A hold that exceeds its budget is recorded as
``timed_out`` and the connection is dropped, so the spec fails on evidence
rather than on a watchdog.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

STATE_PATH = Path(os.environ["ASTRABOX_E2E_INPUT_ACK_GATE_STATE"])
RELEASE_PATH = Path(os.environ["ASTRABOX_E2E_INPUT_ACK_GATE_RELEASE"])
LISTEN_PORT = int(os.environ["ASTRABOX_E2E_INPUT_ACK_GATE_PORT"])
UPSTREAM_PORT = int(os.environ["ASTRABOX_E2E_INPUT_ACK_GATE_UPSTREAM_PORT"])


class GateTimeout(Exception):
    """The held receipt outlived its budget without a release."""


def _log(message: str) -> None:
    sys.stderr.write(f"{time.strftime('%H:%M:%S')} input-ack-gate: {message}\n")
    sys.stderr.flush()


def _load_state() -> dict[str, Any]:
    payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("input-ack gate state must be a JSON object")
    return payload


def _save_state(payload: dict[str, Any]) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _frame(raw: str | bytes) -> dict[str, Any] | None:
    if isinstance(raw, bytes):
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _observe_host_frame(raw: str | bytes) -> None:
    """Learn which command the armed Session's first input travels under."""
    frame = _frame(raw)
    if frame is None or frame.get("op") != "input":
        return
    state = _load_state()
    if str(frame.get("session_id") or "") != str(state.get("session_id") or ""):
        return
    sdk_input = frame.get("sdk_input")
    observation = {
        "command_id": str(frame.get("command_id") or ""),
        "sequence": frame.get("sequence"),
        "input_id": str((sdk_input or {}).get("uuid") or "") if isinstance(sdk_input, dict) else "",
        "observed_at": time.time(),
    }
    inputs = state.get("inputs")
    if not isinstance(inputs, list):
        inputs = []
    inputs.append(observation)
    state["inputs"] = inputs[-16:]
    if not isinstance(state.get("target"), dict) and observation["command_id"]:
        state["target"] = dict(observation)
        _log(
            f"armed on command {observation['command_id']} "
            f"input {observation['input_id']} sequence {observation['sequence']}"
        )
    _save_state(state)


async def _gate_runner_frame(raw: str | bytes, connection_no: int) -> None:
    """Hold the armed command's ``input_ack`` until the release marker exists."""
    frame = _frame(raw)
    if frame is None or frame.get("op") != "input_ack":
        return
    state = _load_state()
    if state.get("state") in {"released", "timed_out"}:
        return
    target = state.get("target")
    if not isinstance(target, dict):
        return
    command_id = str(frame.get("command_id") or "")
    if command_id != str(target.get("command_id") or ""):
        return
    state["holds"] = int(state.get("holds") or 0) + 1
    if state.get("state") != "entered":
        state["state"] = "entered"
        state["entered"] = {
            **target,
            "session_id": str(state.get("session_id") or ""),
            "ack_seq": frame.get("seq"),
            "ack_duplicate": frame.get("duplicate"),
            "connection_no": connection_no,
            "entered_at": time.time(),
        }
    _save_state(state)
    _log(
        f"holding input_ack for {command_id} (seq={frame.get('seq')} "
        f"duplicate={frame.get('duplicate')} hold={state['holds']} connection={connection_no})"
    )
    max_wait = float(state.get("max_wait_seconds") or 60.0)
    deadline = time.monotonic() + max_wait
    while True:
        if RELEASE_PATH.exists():
            state = _load_state()
            if state.get("state") != "released":
                state["state"] = "released"
                state["released_at"] = time.time()
                _save_state(state)
            _log(f"released input_ack for {command_id} on connection {connection_no}")
            return
        if time.monotonic() >= deadline:
            state = _load_state()
            state["state"] = "timed_out"
            state["timed_out_at"] = time.time()
            _save_state(state)
            _log(f"hold for {command_id} exceeded {max_wait:g}s without a release")
            raise GateTimeout(command_id)
        await asyncio.sleep(0.1)


def _observe_runner_frame(raw: str | bytes) -> None:
    """Retain actual input/Result boundaries, including identical replay copies."""
    frame = _frame(raw)
    if frame is None:
        return
    state = _load_state()
    if frame.get("op") == "gap":
        state.setdefault("errors", []).append("runner replay reported a gap")
    elif frame.get("session_id") != state.get("session_id"):
        return
    elif frame.get("op") == "event" and frame.get("message_type") in {
        "UserMessage",
        "ResultMessage",
    }:
        state.setdefault("wire_events", []).append(frame)
    else:
        return
    _save_state(state)


def _upstream_health() -> tuple[int, str]:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{UPSTREAM_PORT}/health", timeout=1.0
        ) as response:
            return int(response.status), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return int(HTTPStatus.BAD_GATEWAY), f"input-ack gate: runner not reachable: {exc}"


def _process_request(connection: Any, request: Any) -> Any:
    # Same contract as RunnerWsServer._process_request: a plain GET /health is
    # answered without a websocket upgrade; the exact body comes from the
    # runner so the host checks runner health rather than proxy health.
    if request.path == "/health":
        status, body = _upstream_health()
        return connection.respond(HTTPStatus(status), body)
    return None


_connections = 0


async def _handler(connection: Any) -> None:
    global _connections
    _connections += 1
    connection_no = _connections
    request = getattr(connection, "request", None)
    path = str(getattr(request, "path", "") or "/")
    try:
        upstream = await connect(f"ws://127.0.0.1:{UPSTREAM_PORT}{path}")
    except Exception as exc:  # noqa: BLE001 - the host must see the failure, not a hang
        _log(f"connection {connection_no}: upstream connect failed: {exc}")
        await connection.close(code=1011, reason="input-ack gate upstream unavailable")
        return
    state = _load_state()
    state["connections"] = connection_no
    _save_state(state)
    _log(f"connection {connection_no}: open path={path}")

    async def host_to_runner() -> None:
        async for raw in connection:
            _observe_host_frame(raw)
            await upstream.send(raw)

    pending_frames: asyncio.Queue[str | bytes] = asyncio.Queue()

    async def read_runner() -> None:
        async for raw in upstream:
            _observe_runner_frame(raw)
            await pending_frames.put(raw)
        await pending_frames.join()

    async def runner_to_host() -> None:
        while True:
            raw = await pending_frames.get()
            await _gate_runner_frame(raw, connection_no)
            await connection.send(raw)
            pending_frames.task_done()

    tasks = [
        asyncio.create_task(host_to_runner(), name=f"gate-host-to-runner-{connection_no}"),
        asyncio.create_task(read_runner(), name=f"gate-read-runner-{connection_no}"),
        asyncio.create_task(runner_to_host(), name=f"gate-runner-to-host-{connection_no}"),
    ]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except BaseException:  # noqa: BLE001 - cancelled pump
                pass
        for task in done:
            exc = task.exception()
            if exc is not None:
                _log(f"connection {connection_no}: {task.get_name()} ended: {exc!r}")
    finally:
        for socket in (upstream, connection):
            try:
                await socket.close()
            except Exception:  # noqa: BLE001 - teardown of a dead side
                pass
        _log(f"connection {connection_no}: closed")


async def main() -> None:
    state = _load_state()
    state["proxy_pid"] = os.getpid()
    state.setdefault("state", "armed")
    _save_state(state)
    server = await serve(
        _handler,
        "0.0.0.0",
        LISTEN_PORT,
        process_request=_process_request,
    )
    _log(f"listening on :{LISTEN_PORT} for runner on :{UPSTREAM_PORT} (pid {os.getpid()})")
    try:
        await asyncio.Event().wait()
    finally:
        server.close()
        await server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
