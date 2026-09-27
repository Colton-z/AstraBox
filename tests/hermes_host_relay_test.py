"""The Hermes image's relay presents what Hermes requires on its loopback bind.

Hermes accepts a request only when its `Host` names the loopback bind and, on
that bind, only when its credential is the `?token=` query parameter. No route
into a sandbox delivers either: the OpenSandbox ingress gateway replaces
`Host` with the Pod address, and the platform keeps the credential out of the
request line because every proxy on the way logs it. The relay in
`containers/sandbox-hermes/hermes_host_relay.py` supplies both on the loopback
hop, so these tests hold it to what depends on it: the first request head
names the backend's bind and carries the credential from its header into the
query, a wrong or missing credential is still refused, everything after that
head passes through byte for byte, and a plain HTTP request cannot leave a
kept-alive connection whose later requests go out unrewritten.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType
from urllib.parse import parse_qs, urlsplit

import pytest

from astrabox.core.service.orchestrator.engine.hermes import HERMES_BACKEND_TOKEN_HEADER

_RELAY_PATH = Path(__file__).resolve().parents[1] / "containers/sandbox-hermes/hermes_host_relay.py"


def _load_relay() -> ModuleType:
    specification = importlib.util.spec_from_file_location("hermes_host_relay", _RELAY_PATH)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


relay = _load_relay()

_TOKEN = "t0k/en"


def _upgrade(credential_lines: bytes) -> bytes:
    """An upgrade as the ingress gateway forwards it, with ``credential_lines``."""

    return (
        b"GET /api/ws HTTP/1.1\r\n"
        b"Host: 10.42.0.7:9118\r\n"
        + credential_lines
        + b"Upgrade: websocket\r\n"
        b"Connection: Upgrade\r\n"
        b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
        b"Sec-WebSocket-Version: 13\r\n"
        b"OpenSandbox-Secure-Access: signed\r\n"
        b"\r\n"
    )


_UPGRADE_FROM_THE_GATEWAY = _upgrade(
    f"{HERMES_BACKEND_TOKEN_HEADER}: {_TOKEN}\r\n".encode("ascii")
)


def test_the_relay_reads_the_header_the_platform_sends() -> None:
    assert relay.TOKEN_HEADER == HERMES_BACKEND_TOKEN_HEADER


def test_an_upgrade_names_the_backend_and_carries_the_credential_into_the_query() -> None:
    head = _UPGRADE_FROM_THE_GATEWAY[:-4]

    rewritten = relay.rewrite_request_head(head, b"127.0.0.1:9119")

    lines = rewritten.split(b"\r\n")
    assert lines[0] == b"GET /api/ws?token=t0k%2Fen HTTP/1.1"
    assert [line for line in lines if line.lower().startswith(b"host:")] == [
        b"Host: 127.0.0.1:9119"
    ]
    # Hermes has no use for the header, and a copy of the credential it does
    # not need is one more place it could be recorded.
    assert not [line for line in lines if b"hermes-token" in line.lower()]
    # The upgrade must stay an upgrade: dropping `Connection: Upgrade` would
    # turn it into a plain GET that the backend answers and closes.
    for kept in (
        b"Upgrade: websocket",
        b"Connection: Upgrade",
        b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
        b"Sec-WebSocket-Version: 13",
        b"OpenSandbox-Secure-Access: signed",
    ):
        assert kept in lines


def test_the_header_credential_replaces_one_already_in_the_query() -> None:
    head = (
        b"GET /api/ws?a=1&token=other HTTP/1.1\r\n"
        b"Host: 10.42.0.7:9118\r\n"
        + f"{HERMES_BACKEND_TOKEN_HEADER.lower()}: {_TOKEN}".encode("ascii")
    )

    request_line = relay.rewrite_request_head(head, b"127.0.0.1:9119").split(b"\r\n")[0]

    target = request_line.split(b" ")[1].decode("ascii")
    assert parse_qs(urlsplit(target).query) == {"a": ["1"], "token": [_TOKEN]}


@pytest.mark.parametrize(
    "credential_lines",
    [
        f"{HERMES_BACKEND_TOKEN_HEADER}: a\r\n{HERMES_BACKEND_TOKEN_HEADER}: b\r\n",
        f"{HERMES_BACKEND_TOKEN_HEADER}:\r\n",
    ],
    ids=["repeated", "empty"],
)
def test_an_ambiguous_credential_is_forwarded_as_none(credential_lines: str) -> None:
    head = (
        b"GET /api/ws?token=other HTTP/1.1\r\nHost: 10.42.0.7:9118\r\n"
        + credential_lines.encode("ascii").rstrip(b"\r\n")
    )

    lines = relay.rewrite_request_head(head, b"127.0.0.1:9119").split(b"\r\n")

    assert lines[0] == b"GET /api/ws HTTP/1.1"
    assert not [line for line in lines if b"hermes-token" in line.lower()]


def test_a_plain_request_closes_its_connection_and_duplicate_hosts_are_dropped() -> None:
    # A keep-alive client's second request on the same connection would pass
    # through unrewritten, so the relay makes the backend end the connection
    # after its reply.
    head = (
        b"GET /api/auth/me HTTP/1.1\r\n"
        b"host: 10.42.0.7:9118\r\n"
        b"Host: attacker.example\r\n"
        b"Connection: keep-alive\r\n"
        b"Accept: */*"
    )

    lines = relay.rewrite_request_head(head, b"127.0.0.1:9119").split(b"\r\n")

    assert lines[0] == b"GET /api/auth/me HTTP/1.1"
    assert [line for line in lines if line.lower().startswith(b"host:")] == [
        b"Host: 127.0.0.1:9119"
    ]
    assert [line for line in lines if line.lower().startswith(b"connection:")] == [
        b"Connection: close"
    ]
    assert b"Accept: */*" in lines


class _HermesLikeBackend:
    """Answers an upgrade the way Hermes does on a loopback bind.

    101 only for a loopback `Host` and the session token as the `token` query
    parameter; 403 otherwise, as Hermes' close before accept reaches a client.
    After a 101 it echoes every byte back, standing in for the WebSocket
    session the relay must carry without parsing.
    """

    def __init__(self, token: str) -> None:
        self.token = token
        self.request_lines: list[bytes] = []
        self.hosts: list[bytes] = []
        self.port = 0
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> "_HermesLikeBackend":
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_exc: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        request_line, *fields = head.split(b"\r\n")
        self.request_lines.append(request_line)
        host = next(
            field.split(b":", 1)[1].strip()
            for field in fields
            if field.lower().startswith(b"host:")
        )
        self.hosts.append(host)
        target = request_line.split(b" ")[1].decode("ascii")
        presented = parse_qs(urlsplit(target).query).get("token", [""])[-1]
        if host.split(b":")[0] not in {b"127.0.0.1", b"localhost"} or presented != self.token:
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        writer.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n")
        while data := await reader.read(4096):
            writer.write(data)
            await writer.drain()
        writer.close()


@contextlib.asynccontextmanager
async def _serving_relay(backend_port: int) -> AsyncIterator[int]:
    async def _accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await relay.relay_connection(reader, writer, upstream=("127.0.0.1", backend_port))

    server = await asyncio.start_server(_accept, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


async def _status_line(
    port: int, request: bytes = _UPGRADE_FROM_THE_GATEWAY
) -> tuple[bytes, asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    response = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    return response.split(b"\r\n", 1)[0], reader, writer


@pytest.mark.asyncio
async def test_an_upgrade_from_the_gateway_is_accepted_through_the_relay() -> None:
    async with _HermesLikeBackend(_TOKEN) as backend:
        # Control: the same upgrade sent straight to the backend is refused, so
        # an accepted upgrade below is the relay's doing.
        direct, _, direct_writer = await _status_line(backend.port)
        direct_writer.close()
        assert direct == b"HTTP/1.1 403 Forbidden"

        async with _serving_relay(backend.port) as relay_port:
            status, reader, writer = await _status_line(relay_port)
            # Closed on every path: an open client connection would keep the
            # relay's server from closing and turn a failure into a hang.
            try:
                assert status == b"HTTP/1.1 101 Switching Protocols"

                # Session bytes that look like a request head must not be rewritten.
                session = (
                    b"\x81\x05hello"
                    + f"{HERMES_BACKEND_TOKEN_HEADER}: x\r\n\r\n".encode("ascii")
                )
                writer.write(session)
                await writer.drain()
                echoed = await asyncio.wait_for(reader.readexactly(len(session)), 5)
                assert echoed == session

                # The client's close reaches the backend, and the backend's close
                # reaches the client, so no half of the relay is left waiting.
                writer.write_eof()
                assert await asyncio.wait_for(reader.read(), 5) == b""
            finally:
                writer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "credential_lines",
    [f"{HERMES_BACKEND_TOKEN_HEADER}: wrong\r\n".encode("ascii"), b""],
    ids=["wrong", "missing"],
)
async def test_a_wrong_or_missing_credential_is_still_refused(credential_lines: bytes) -> None:
    async with _HermesLikeBackend(_TOKEN) as backend:
        async with _serving_relay(backend.port) as relay_port:
            status, _, writer = await _status_line(relay_port, _upgrade(credential_lines))
            writer.close()

    assert status == b"HTTP/1.1 403 Forbidden"
    # Refused for the credential alone: the relay did present the loopback Host.
    assert backend.hosts == [f"127.0.0.1:{backend.port}".encode()]
