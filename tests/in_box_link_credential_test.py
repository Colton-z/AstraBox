"""The host's links into a box carry a credential only the deployment can make.

Two in-box listeners answer any peer that can route to the box: the Claude
runner and the Codex forwarder. On Kubernetes that is any pod, because a
sandbox Pod has no ingress policy; under the shared tenancy it is every sibling
conversation. Each now demands a credential derived from the deployment secret
and the seat's identity, and compares it with a file the platform writes into
the seat's home. These tests hold three things: the value is keyed to the
secret, so knowing every platform id is not enough; each seat and each service
gets its own, and a seat without its identity gets none; and the Codex
forwarder relays nothing until the credential matches.
"""

from __future__ import annotations

import asyncio
import importlib.machinery
import importlib.util
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from astrabox.core.service.orchestrator.engine import codex, runner_link
from astrabox.core.service.orchestrator.engine.codex_link import (
    CODEX_FORWARD_TOKEN_FILE_NAME,
    CODEX_FORWARD_TOKEN_HEADER,
)
from astrabox.core.service.orchestrator.engine.provisioning import in_box_service_token

_REPO = Path(__file__).resolve().parents[1]
_IDENTITY = {
    "linux_user": "agent",
    "home_dir": "/home/agent",
    "workspace_dir": "/workspace",
    "config_dir": "/home/agent/.claude",
    "sandbox_tenancy": "conversation",
}


# ── the derivation ────────────────────────────────────────────────────────


def test_the_token_is_keyed_to_the_deployment_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_TRANSCRIPT_SIGNING_KEY", "deployment-one")
    first = runner_link.runner_activation_token("box-1", _IDENTITY)
    assert first == runner_link.runner_activation_token("box-1", dict(_IDENTITY))

    monkeypatch.setenv("ASTRABOX_TRANSCRIPT_SIGNING_KEY", "deployment-two")
    assert runner_link.runner_activation_token("box-1", _IDENTITY) != first, (
        "a peer that knows the box and the account must still need the secret"
    )


def test_each_seat_and_each_service_gets_its_own_token() -> None:
    runner = runner_link.runner_activation_token("box-1", _IDENTITY)
    assert runner_link.runner_activation_token("box-2", _IDENTITY) != runner
    assert (
        runner_link.runner_activation_token(
            "box-1", {**_IDENTITY, "linux_user": "conv_b", "home_dir": "/home/conv_b"}
        )
        != runner
    )
    assert codex._forward_token("box-1", _IDENTITY) != runner


def test_a_seat_without_its_identity_gets_no_token() -> None:
    with pytest.raises(RuntimeError, match="account and home"):
        in_box_service_token(purpose="claude-runner", sandbox_id="box-1", runtime_identity={})


# ── the Codex forwarder ───────────────────────────────────────────────────


def _load_forwarder() -> Any:
    path = _REPO / "containers" / "sandbox-codex" / "astrabox-codex-forward"
    loader = importlib.machinery.SourceFileLoader("astrabox_codex_forward", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


_UPGRADE = (
    b"GET / HTTP/1.1\r\nHost: box\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
    b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n"
    b"Sec-WebSocket-Extensions: permessage-deflate\r\n"
)


@pytest.mark.parametrize(
    ("token_file", "header", "admitted"),
    [
        (None, b"the-seat-token", False),
        (b"the-seat-token\n", None, False),
        (b"the-seat-token\n", b"a-guessed-token", False),
        (b"the-seat-token\n", b"the-seat-token", True),
    ],
    ids=["before-delivery", "no-credential", "wrong-credential", "the-platform"],
)
async def test_the_forwarder_relays_only_an_upgrade_carrying_the_credential(
    monkeypatch: pytest.MonkeyPatch,
    token_file: bytes | None,
    header: bytes | None,
    admitted: bool,
) -> None:
    forwarder = _load_forwarder()
    received: list[bytes] = []
    with tempfile.TemporaryDirectory() as scratch:
        socket_path = os.path.join(scratch, "app-server.sock")
        token_path = os.path.join(scratch, CODEX_FORWARD_TOKEN_FILE_NAME)
        if token_file is not None:
            Path(token_path).write_bytes(token_file)
        monkeypatch.setattr(forwarder, "SOCKET", socket_path)
        monkeypatch.setattr(forwarder, "TOKEN_FILE", token_path)

        async def _app_server(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            received.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 101 Switching Protocols\r\n\r\n")
            await writer.drain()
            writer.close()

        upstream = await asyncio.start_unix_server(_app_server, path=socket_path)
        listener = await asyncio.start_server(forwarder._handle, "127.0.0.1", 0)
        port = listener.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            head = _UPGRADE
            if header is not None:
                head += CODEX_FORWARD_TOKEN_HEADER.encode("ascii") + b": " + header + b"\r\n"
            writer.write(head + b"\r\n")
            await writer.drain()
            status = await asyncio.wait_for(reader.readline(), timeout=5)
            writer.close()
        finally:
            listener.close()
            upstream.close()

    if admitted:
        assert b"101" in status
        assert len(received) == 1
        relayed = received[0].lower()
        assert b"x-astrabox-codex-token" not in relayed, "the server must never see the credential"
        assert b"sec-websocket-extensions" not in relayed
    else:
        assert b"403" in status
        assert received == [], "nothing reaches the app-server before the credential matches"
