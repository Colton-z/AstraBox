"""Contracts for the pure-ASGI web identity middleware.

The default no-auth resolver binds the deployment's local administrator. An
authenticated resolver binds ``UserContext`` for downstream handlers, an
``APIError`` rejects the request with its mapped status, and the ContextVar is
reset after every request.
"""

from __future__ import annotations

import asyncio
import unittest

import pytest
from typing import Any

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import (
    UserContext,
    _build_local_debug_user,
    get_current_user_context,
)
from astrabox.providers.identity import LocalNoAuthWebIdentityResolver
from astrabox.web.identity_middleware import WebIdentityMiddleware


@pytest.fixture(autouse=True)
def _environment_membership(environment_member_accounts: set[str]) -> None:
    environment_member_accounts.update({'bob'})

# The env-derived default identity (USER / ASTRABOX_LOCAL_USER_ID / "local-user").
_LOCAL_USER_ID = _build_local_debug_user().user_id


class _RecordingApp:
    """Downstream ASGI app that records the identity a handler would observe."""

    def __init__(self) -> None:
        self.seen_user_id: str | None = None
        self.seen_roles: list[str] | None = None
        self.called = False

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        self.called = True
        user = await get_current_user_context()
        self.seen_user_id = user.user_id
        self.seen_roles = list(user.roles)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


class _StaticResolver:
    def __init__(self, user: UserContext | None) -> None:
        self._user = user

    async def resolve(self, headers):
        return self._user


class _RejectingResolver:
    async def resolve(self, headers):
        raise APIError(code="UNAUTHORIZED", message="no credential", status_code=401)


def _http_scope() -> dict[str, Any]:
    return {"type": "http", "method": "GET", "path": "/api/v1/sessions", "headers": []}


async def _drive(middleware: WebIdentityMiddleware, scope: dict[str, Any]) -> list[dict]:
    sent: list[dict] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await middleware(scope, receive, send)
    return sent


class WebIdentityMiddlewareTest(unittest.TestCase):


    def test_context_resets_between_requests(self) -> None:
        app = _RecordingApp()
        mw = WebIdentityMiddleware(app, _StaticResolver(UserContext(user_id="bob")))
        asyncio.run(_drive(mw, _http_scope()))
        self.assertEqual(app.seen_user_id, "bob")

        # A subsequent request whose resolver asserts nothing must NOT see bob.
        app2 = _RecordingApp()
        mw2 = WebIdentityMiddleware(app2, LocalNoAuthWebIdentityResolver())
        asyncio.run(_drive(mw2, _http_scope()))
        self.assertEqual(app2.seen_user_id, _LOCAL_USER_ID)
        self.assertNotEqual(app2.seen_user_id, "bob")

    def test_rejecting_resolver_returns_401_and_skips_app(self) -> None:
        app = _RecordingApp()
        mw = WebIdentityMiddleware(app, _RejectingResolver())
        sent = asyncio.run(_drive(mw, _http_scope()))
        self.assertFalse(app.called)
        start = next(m for m in sent if m["type"] == "http.response.start")
        self.assertEqual(start["status"], 401)


if __name__ == "__main__":
    unittest.main()
