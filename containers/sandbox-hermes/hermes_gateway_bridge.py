"""Publish the box's Hermes gateway to the platform host as one WebSocket per host connection.

``hermes gateway run`` serves its canonical protocol (``hermes-gateway-v1``) on a
loopback WebSocket that admits only a client holding a session ticket. Tickets
come from the gateway's private control socket (``$HERMES_HOME/gateway.sock``),
which answers only a peer of the gateway's own uid, and each ticket is valid
for 30 seconds and one connection (``hermes_cli/gateway_client.py`` and
``gateway/control_socket.py`` at the pinned commit). A host outside the box can
do none of that, so this bridge runs as the profile's workload account and does
it for each host connection:

1. The host upgrades on :data:`LISTEN_PORT` with the profile's bridge
   credential in :data:`TOKEN_HEADER`.
2. The bridge finds the ready gateway without starting one
   (``discover_gateway_endpoint``), mints a ticket for it and dials its
   WebSocket: the steps ``hermes_cli.gateway_client.connect_gateway`` performs
   for an endpoint it is given. Its ``GatewayClient`` reader cannot be used here:
   it settles only the request ids that client issued and drops every other
   reply, while the host issues its own.
3. Text frames are relayed unchanged in both directions. When either side
   closes, the bridge closes the other, so the host's attachment ends with the
   gateway connection it stood for.

The gateway is not reachable when it is restarting. A host upgrade, or a
``GET`` of :data:`HEALTH_PATH`, then answers HTTP 503, which a caller treats as
"not ready yet"; a missing or wrong credential answers 401 or 403.
"""

from __future__ import annotations

# Native bootstrap selects the profile's installed dependency environment first.
import hermes_bootstrap  # noqa: F401

import asyncio
import hmac
from http import HTTPStatus
import json
import os
import sys
import time
from typing import Any

from hermes_constants import get_hermes_home
from hermes_cli.gateway_client import GATEWAY_WS_PROTOCOL, _session_ticket, gateway_ws_target
from hermes_cli.gateway_runtime import attachable, discover_gateway_endpoint, gateway_profile_home
from websockets.asyncio.client import connect
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

#: The header the platform sends the bridge credential in. It must match
#: ``HERMES_BACKEND_TOKEN_HEADER`` in
#: ``astrabox/core/service/orchestrator/engine/hermes.py``.
TOKEN_HEADER = "X-AstraBox-Hermes-Token"
#: The path the platform upgrades on, and the readiness path the image's probes read.
SOCKET_PATH = "/api/ws"
HEALTH_PATH = "/healthz"
#: The largest frame relayed: the same bound ``connect_gateway`` sets on its own
#: gateway connection, and ``MAX_FRAME_BYTES`` in
#: ``astrabox/core/service/orchestrator/runtime/hermes_backend_channel.py``.
MAX_FRAME_BYTES = 8 * 1024 * 1024
#: How long one discovery may wait on the gateway's control socket.
_DISCOVERY_TIMEOUT_SECONDS = 5.0


def _endpoint() -> Any | None:
    """The ready gateway endpoint of this profile, or ``None`` while it is not ready."""

    result = discover_gateway_endpoint(gateway_profile_home(get_hermes_home()),
                                       timeout=_DISCOVERY_TIMEOUT_SECONDS)
    return result.endpoint if attachable(result) else None


class ProtocolMismatch(RuntimeError):
    """The gateway accepted the connection under a protocol other than ``hermes-gateway-v1``."""


async def _dial_gateway() -> Any | None:
    """Open one canonical connection to the ready gateway, or ``None`` when there is none.

    A ticket refused or a dial that fails means the gateway went away between
    discovery and connection; the caller answers that like any other moment the
    gateway is not ready. A gateway speaking another protocol raises
    :class:`ProtocolMismatch`: waiting does not change it.
    """

    endpoint = await asyncio.to_thread(_endpoint)
    if endpoint is None:
        return None
    try:
        ticket = await asyncio.to_thread(
            _session_ticket, gateway_profile_home(get_hermes_home()), endpoint
        )
        url, protocols = gateway_ws_target(endpoint, ticket)
        # A loopback peer is never dialled through a proxy (connect_gateway does the same).
        upstream = await connect(url, subprotocols=protocols, open_timeout=10,
                                 max_size=MAX_FRAME_BYTES, proxy=None)
    except (OSError, TimeoutError, ValueError) as exc:
        print(f"hermes_gateway_bridge: gateway connection failed: {type(exc).__name__}: {exc}",
              flush=True)
        return None
    if upstream.subprotocol != GATEWAY_WS_PROTOCOL:
        await upstream.close()
        raise ProtocolMismatch(
            f"the Hermes gateway selected {upstream.subprotocol!r}, not {GATEWAY_WS_PROTOCOL!r}"
        )
    return upstream


class Bridge:
    """The listener: one gateway connection for each authenticated host connection."""

    def __init__(self, token: str) -> None:
        self._token = token.encode("utf-8")
        #: Gateway connections opened for an upgrade the handshake has not finished.
        self._dialled: dict[ServerConnection, Any] = {}

    def _authorized(self, request: Request) -> bool:
        values = request.headers.get_all(TOKEN_HEADER)
        return len(values) == 1 and hmac.compare_digest(values[0].encode("utf-8"), self._token)

    async def process_request(self, connection: ServerConnection, request: Request) -> Response | None:
        path = request.path.split("?", 1)[0]
        if path == HEALTH_PATH:
            ready = await asyncio.to_thread(_endpoint) is not None
            return connection.respond(HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE,
                                      "ready\n" if ready else "the Hermes gateway is not ready\n")
        if path != SOCKET_PATH:
            return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")
        if not request.headers.get_all(TOKEN_HEADER):
            return connection.respond(HTTPStatus.UNAUTHORIZED, "missing bridge credential\n")
        if not self._authorized(request):
            return connection.respond(HTTPStatus.FORBIDDEN, "bridge credential refused\n")
        try:
            upstream = await _dial_gateway()
        except ProtocolMismatch as exc:
            return connection.respond(HTTPStatus.INTERNAL_SERVER_ERROR, f"{exc}\n")
        if upstream is None:
            return connection.respond(HTTPStatus.SERVICE_UNAVAILABLE, "the Hermes gateway is not ready\n")
        self._dialled[connection] = upstream
        return None

    async def process_response(
        self, connection: ServerConnection, request: Request, response: Response
    ) -> None:
        # A handshake refused after process_request (a malformed upgrade) never
        # reaches the handler, so the gateway connection opened for it closes here.
        if response.status_code != HTTPStatus.SWITCHING_PROTOCOLS:
            upstream = self._dialled.pop(connection, None)
            if upstream is not None:
                await upstream.close()

    async def handle(self, connection: ServerConnection) -> None:
        upstream = self._dialled.pop(connection, None)
        if upstream is None:
            await connection.close(1011, "no gateway connection for this upgrade")
            return
        started = time.monotonic()
        legs = [
            asyncio.create_task(_relay(connection, upstream), name="host-to-gateway"),
            asyncio.create_task(_relay(upstream, connection), name="gateway-to-host"),
        ]
        try:
            await asyncio.wait(legs, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for leg in legs:
                leg.cancel()
            await asyncio.gather(*legs, return_exceptions=True)
            await upstream.close()
            await connection.close()
            print(json.dumps({
                "event": "hermes_gateway_bridge.closed",
                "seconds": round(time.monotonic() - started, 3),
                "host_close": connection.close_code,
                "gateway_close": upstream.close_code,
            }), flush=True)


async def _relay(source: Any, destination: Any) -> None:
    """Copy frames from ``source`` to ``destination`` until either side closes."""

    try:
        async for message in source:
            await destination.send(message)
    except ConnectionClosed:
        return


async def _wait_for_gateway(deadline_seconds: float) -> None:
    """Return once the gateway is first ready; a listener bound earlier would accept
    upgrades it can only refuse, and the platform's readiness gate is a connect to it."""

    deadline = time.monotonic() + deadline_seconds
    while await asyncio.to_thread(_endpoint) is None:
        if time.monotonic() >= deadline:
            raise SystemExit(
                f"hermes_gateway_bridge: the Hermes gateway was not ready within {deadline_seconds:.0f}s"
            )
        await asyncio.sleep(0.5)


async def main(listen_port: int, wait_seconds: float) -> None:
    token = os.environ.get("HERMES_DASHBOARD_SESSION_TOKEN", "")
    if not token:
        raise SystemExit("hermes_gateway_bridge: the profile env carries no bridge credential")
    await _wait_for_gateway(wait_seconds)
    bridge = Bridge(token)
    async with serve(
        bridge.handle, "0.0.0.0", listen_port,
        process_request=bridge.process_request, process_response=bridge.process_response,
        max_size=MAX_FRAME_BYTES,
    ) as server:
        print(f"hermes_gateway_bridge: publishing the Hermes gateway on {listen_port}", flush=True)
        await server.serve_forever()


if __name__ == "__main__":
    # `astrabox-hermes-serve --bridge` owns both settings and passes them here.
    asyncio.run(main(int(sys.argv[1]), float(sys.argv[2])))
