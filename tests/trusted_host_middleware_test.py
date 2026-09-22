"""Trusted-host gate — DNS-rebinding guard for the browser-facing surface.

Drives the pure-ASGI :class:`TrustedHostMiddleware` and asserts: an allowlisted
Host passes (bare, with-port, and IPv6 loopback forms), an off-allowlist Host is
rejected 400, the platform-MCP path and ``/healthz`` are exempt regardless of Host,
``ASTRABOX_ALLOWED_HOSTS`` REPLACES the default list, and a disallowed websocket
is policy-closed rather than accepted.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from typing import Any
from unittest import mock

from astrabox.web.trusted_host_middleware import TrustedHostMiddleware


class _RecordingApp:
    """Downstream ASGI app that records whether it was reached."""

    def __init__(self) -> None:
        self.called = False

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        self.called = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _scope(
    path: str = "/api/v1/sessions",
    host: str | None = "127.0.0.1",
    type_: str = "http",
) -> dict[str, Any]:
    headers: list[tuple[bytes, bytes]] = []
    if host is not None:
        headers.append((b"host", host.encode("latin-1")))
    return {"type": type_, "method": "GET", "path": path, "headers": headers}


async def _drive(mw: TrustedHostMiddleware, scope: dict[str, Any]) -> list[dict]:
    sent: list[dict] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await mw(scope, receive, send)
    return sent


def _status(sent: list[dict]) -> int | None:
    for message in sent:
        if message["type"] == "http.response.start":
            return int(message["status"])
    return None


class TrustedHostMiddlewareTest(unittest.TestCase):
    def setUp(self) -> None:
        # Default-list tests must see the built-in allowlist, not an env leaked
        # from the surrounding shell/CI. Restored on tearDown.
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("ASTRABOX_ALLOWED_HOSTS", None)

    def tearDown(self) -> None:
        self._env.stop()

    def test_allowlisted_host_passes(self) -> None:
        app = _RecordingApp()
        sent = asyncio.run(_drive(TrustedHostMiddleware(app), _scope(host="127.0.0.1")))
        self.assertTrue(app.called)
        self.assertEqual(_status(sent), 200)

    def test_allowlisted_host_with_port_passes(self) -> None:
        # The Host header carries a port; the gate matches the host part only.
        app = _RecordingApp()
        sent = asyncio.run(
            _drive(TrustedHostMiddleware(app), _scope(host="localhost:5173"))
        )
        self.assertTrue(app.called)
        self.assertEqual(_status(sent), 200)

    def test_ipv6_loopback_with_port_passes(self) -> None:
        # Bracketed IPv6 literal + port must survive port stripping.
        app = _RecordingApp()
        sent = asyncio.run(_drive(TrustedHostMiddleware(app), _scope(host="[::1]:8088")))
        self.assertTrue(app.called)
        self.assertEqual(_status(sent), 200)

    def test_off_allowlist_host_rejected_400(self) -> None:
        app = _RecordingApp()
        sent = asyncio.run(
            _drive(TrustedHostMiddleware(app), _scope(host="evil.example.com"))
        )
        self.assertFalse(app.called)
        self.assertEqual(_status(sent), 400)

    def test_platform_mcp_path_exempt_regardless_of_host(self) -> None:
        # Sandbox->host platform MCP traffic carries a bridge-IP Host.
        app = _RecordingApp()
        scope = _scope(
            path="/api/v1/platform-mcp/deployment-1/html-preview/mcp",
            host="172.17.0.1",
        )
        sent = asyncio.run(_drive(TrustedHostMiddleware(app), scope))
        self.assertTrue(app.called)
        self.assertEqual(_status(sent), 200)

    def test_sbxcap_transcript_path_exempt_regardless_of_host(self) -> None:
        # The in-box runner's transcript flush arrives with a bridge-IP/Pod-IP
        # Host and is authenticated by the capability token in the path; gating
        # it on Host blocks the flush (observed live: HTTP 400 HOST_NOT_ALLOWED
        # from the box while the same request passed from the host loopback).
        app = _RecordingApp()
        scope = _scope(
            path="/api/v1/sbxcap/deadbeef/api/v1/transcript/sess-1/append",
            host="172.17.0.1",
        )
        sent = asyncio.run(_drive(TrustedHostMiddleware(app), scope))
        self.assertTrue(app.called)
        self.assertEqual(_status(sent), 200)

    def test_healthz_exempt_regardless_of_host(self) -> None:
        app = _RecordingApp()
        sent = asyncio.run(
            _drive(
                TrustedHostMiddleware(app),
                _scope(path="/healthz", host="evil.example.com"),
            )
        )
        self.assertTrue(app.called)

    def test_env_override_replaces_default_allowlist(self) -> None:
        with mock.patch.dict(
            os.environ, {"ASTRABOX_ALLOWED_HOSTS": "myhost.internal"}
        ):
            # Overriding the list REPLACES it: the default loopback host is
            # not carried over…
            app = _RecordingApp()
            sent = asyncio.run(
                _drive(TrustedHostMiddleware(app), _scope(host="127.0.0.1"))
            )
            self.assertFalse(app.called)
            self.assertEqual(_status(sent), 400)
            # …and the configured host is.
            app2 = _RecordingApp()
            asyncio.run(
                _drive(TrustedHostMiddleware(app2), _scope(host="myhost.internal"))
            )
            self.assertTrue(app2.called)

    def test_disallowed_websocket_is_policy_closed(self) -> None:
        app = _RecordingApp()
        sent = asyncio.run(
            _drive(
                TrustedHostMiddleware(app),
                _scope(host="evil.example.com", type_="websocket"),
            )
        )
        self.assertFalse(app.called)
        self.assertTrue(
            any(
                m["type"] == "websocket.close" and m["code"] == 1008 for m in sent
            )
        )


if __name__ == "__main__":
    unittest.main()


# ── machine exemptions under a root_path deployment ──────────────────────────


class RootPathClassificationTests(unittest.TestCase):
    def test_mounted_machine_exemption_still_bypasses_the_host_gate(self) -> None:
        """Under a root_path mount the raw path carries the prefix;
        classification now follows the route path, so the sandbox->host
        bridge exemptions keep matching instead of silently becoming
        Host-gated."""
        app = _RecordingApp()
        scope = _scope(
            path="/astrabox/api/v1/platform-mcp/deployment-1/html-preview/mcp",
            host="evil.example.com",
        )
        scope["root_path"] = "/astrabox"
        sent = asyncio.run(_drive(TrustedHostMiddleware(app), scope))
        self.assertTrue(app.called)
        self.assertEqual(_status(sent), 200)

    def test_mounted_browser_path_is_still_host_gated(self) -> None:
        app = _RecordingApp()
        scope = _scope(path="/astrabox/api/v1/sessions", host="evil.example.com")
        scope["root_path"] = "/astrabox"
        sent = asyncio.run(_drive(TrustedHostMiddleware(app), scope))
        self.assertFalse(app.called)
        self.assertEqual(_status(sent), 400)
