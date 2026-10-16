"""Trusted-host gate — DNS-rebinding guard for the browser-facing surface.

Drives the pure-ASGI :class:`TrustedHostMiddleware` and asserts: an off-allowlist
Host is rejected 400, a disallowed websocket is policy-closed rather than
accepted, and a browser path under a mount is still host-gated.
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


    def test_off_allowlist_host_rejected_400(self) -> None:
        app = _RecordingApp()
        sent = asyncio.run(
            _drive(TrustedHostMiddleware(app), _scope(host="evil.example.com"))
        )
        self.assertFalse(app.called)
        self.assertEqual(_status(sent), 400)


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

    def test_mounted_browser_path_is_still_host_gated(self) -> None:
        app = _RecordingApp()
        scope = _scope(path="/astrabox/api/v1/sessions", host="evil.example.com")
        scope["root_path"] = "/astrabox"
        sent = asyncio.run(_drive(TrustedHostMiddleware(app), scope))
        self.assertFalse(app.called)
        self.assertEqual(_status(sent), 400)
