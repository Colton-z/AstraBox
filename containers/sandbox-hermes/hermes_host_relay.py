"""Publish the loopback-bound Hermes backend on the sandbox's outward port.

`hermes serve` binds 127.0.0.1 (`astrabox-hermes-serve` explains why) and then
accepts a request only when its `Host` header names that bind. The HTTP check
is `host_header_middleware` and the WebSocket check is
`_ws_host_origin_reason`, both in `hermes_cli/web_server.py` of hermes-agent
0.21.0 (commit 29112bef): Hermes' DNS-rebinding defence, advisory
GHSA-ppp5-vxwm-4cf7. A refused WebSocket upgrade closes with code 4403 and
logs `host_mismatch`.

No route into a sandbox delivers that `Host`. A caller that dials the Pod
addresses the Pod, and the OpenSandbox ingress gateway that Secure Access puts
in front of a sandbox replaces `Host` with the Pod address before it forwards
(`r.Host = targetHost` in `components/ingress/pkg/proxy/proxy.go`, which its
WebSocket proxy sends on). This relay therefore names its upstream in `Host`,
as a reverse proxy does.

On that bind Hermes also takes its credential only as a `?token=` query
parameter (`_ws_auth_reason` in the same file), and every proxy in front of
the box logs the request line. The platform therefore sends the credential in
the `X-AstraBox-Hermes-Token` header, and this relay moves it into the query
on the loopback hop. Every other header, including the WebSocket upgrade,
passes through unchanged.

Only the first request head on a connection is rewritten; the bytes after it
are relayed verbatim so a WebSocket session is never parsed. A request without
an `Upgrade` header is sent with `Connection: close`, so Hermes ends that
connection after its response and a client's next request arrives on a new
connection whose head is rewritten too.

The relay adds no reach. Hermes already sees every connection through it as a
loopback peer, the credential is still required, and the port is reachable
only through the sandbox's endpoint.
"""

from __future__ import annotations

import asyncio
import sys
from urllib.parse import quote_from_bytes

UPSTREAM_HOST = "127.0.0.1"
#: A request head longer than this closes the connection instead of being buffered.
MAX_HEAD_BYTES = 64 * 1024
_RELAY_ERRORS = (ConnectionError, OSError, RuntimeError)
#: The header the platform sends the backend credential in. It must match
#: `HERMES_BACKEND_TOKEN_HEADER` in
#: `astrabox/core/service/orchestrator/engine/hermes.py`.
TOKEN_HEADER = "X-AstraBox-Hermes-Token"


def rewrite_request_head(head: bytes, authority: bytes) -> bytes:
    """Return ``head`` addressed to ``authority``.

    ``head`` is a request line and its header lines without the blank line
    that ends them. Every `Host` line is replaced by one naming
    ``authority``; a request that is not an upgrade also has its `Connection`
    lines replaced by `Connection: close`.

    A :data:`TOKEN_HEADER` line is removed, and its value replaces any `token`
    in the request target's query. A request carrying the header more than
    once, or with an empty value, is forwarded with no `token` at all, so
    Hermes refuses it rather than choosing between credentials.
    """

    request_line, *fields = head.split(b"\r\n")
    names = [field.split(b":", 1)[0].strip().lower() for field in fields]
    token_name = TOKEN_HEADER.lower().encode("ascii")
    tokens = [
        field.split(b":", 1)[1].strip() if b":" in field else b""
        for field, name in zip(fields, names)
        if name == token_name
    ]
    upgrade = b"upgrade" in names
    kept = [
        field
        for field, name in zip(fields, names)
        if name not in (b"host", token_name) and (upgrade or name != b"connection")
    ]
    if tokens:
        token = tokens[0] if len(tokens) == 1 else b""
        request_line = _with_query_token(request_line, token)
    added = [b"Host: " + authority]
    if not upgrade:
        added.append(b"Connection: close")
    return b"\r\n".join([request_line, *added, *kept])


def _with_query_token(request_line: bytes, token: bytes) -> bytes:
    """``request_line`` with its query's `token` replaced by ``token``.

    An empty ``token`` removes the parameter. A request line that is not
    ``METHOD TARGET VERSION`` loses nothing and gains no credential.
    """

    parts = request_line.split(b" ")
    if len(parts) != 3:
        return request_line
    method, target, version = parts
    path, _, query = target.partition(b"?")
    params = [
        param
        for param in query.split(b"&")
        if param and param.split(b"=", 1)[0] != b"token"
    ]
    if token:
        params.append(b"token=" + quote_from_bytes(token, safe="").encode("ascii"))
    target = path + (b"?" + b"&".join(params) if params else b"")
    return b" ".join((method, target, version))


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Copy ``reader`` to ``writer`` until end of stream.

    A clean end is passed on as a half-close so the peer can finish its side.
    A broken stream closes ``writer``, which ends the opposite direction too.
    """

    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except _RELAY_ERRORS:
        writer.close()


async def relay_connection(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    *,
    upstream: tuple[str, int],
) -> None:
    """Relay one client connection to the backend at ``upstream``."""

    upstream_writer: asyncio.StreamWriter | None = None
    try:
        try:
            head = await client_reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            return
        upstream_reader, upstream_writer = await asyncio.open_connection(*upstream)
        authority = f"{upstream[0]}:{upstream[1]}".encode("ascii")
        upstream_writer.write(rewrite_request_head(head[:-4], authority) + b"\r\n\r\n")
        await upstream_writer.drain()
        await asyncio.gather(
            _pipe(client_reader, upstream_writer),
            _pipe(upstream_reader, client_writer),
        )
    except _RELAY_ERRORS:
        pass
    finally:
        for writer in (upstream_writer, client_writer):
            if writer is not None:
                writer.close()


async def serve(listen_port: int, upstream_port: int) -> None:
    """Accept connections on every interface and relay each to the backend."""

    upstream = (UPSTREAM_HOST, upstream_port)

    async def _accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await relay_connection(reader, writer, upstream=upstream)

    server = await asyncio.start_server(
        _accept, "0.0.0.0", listen_port, limit=MAX_HEAD_BYTES, reuse_address=True
    )
    print(
        f"hermes_host_relay: relaying {listen_port} to {UPSTREAM_HOST}:{upstream_port}",
        flush=True,
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    # `astrabox-hermes-forward` owns both port settings and passes them here.
    listen, backend = (int(value) for value in sys.argv[1:3])
    asyncio.run(serve(listen, backend))
