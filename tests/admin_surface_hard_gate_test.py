"""Admin hard gate, admin API token, and owner-anchored webhook authorization."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.common.utils.admin_api_auth import require_admin_api_bearer
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.providers.identity import LocalNoAuthWebIdentityResolver
from astrabox.web.identity_middleware import WebIdentityMiddleware


class _StrictNoneResolver:
    """A mis-implemented strict resolver: returns None instead of raising."""

    async def resolve(self, headers: Any) -> None:
        _ = headers
        return None


class _IdentityResolver:
    def __init__(self, roles: list[str]) -> None:
        self._roles = roles

    async def resolve(self, headers: Any) -> UserContext:
        _ = headers
        return UserContext(user_id="casdoor-user", roles=self._roles)


def _scope(path: str) -> dict[str, Any]:
    return {"type": "http", "path": path, "headers": []}


async def _run(middleware: WebIdentityMiddleware, path: str) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def receive() -> dict[str, Any]:  # pragma: no cover - not pulled
        return {"type": "http.request"}

    await middleware(_scope(path), receive, send)
    return sent


async def test_default_resolver_keeps_single_user_admin_open() -> None:
    reached: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        reached.append(scope["path"])

    middleware = WebIdentityMiddleware(app, LocalNoAuthWebIdentityResolver())
    await _run(middleware, "/api/v1/admin/system/overview")

    assert reached == ["/api/v1/admin/system/overview"]


async def test_admin_gate_uses_the_fixed_internal_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inert legacy role knob cannot split Casdoor and resource policy."""

    monkeypatch.setenv("ASTRABOX_ADMIN_ROLE", "platform-admin")
    reached: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        reached.append(scope["path"])

    middleware = WebIdentityMiddleware(app, _IdentityResolver(["admin"]))
    await _run(middleware, "/api/v1/admin/system/overview")

    assert reached == ["/api/v1/admin/system/overview"]


async def test_legacy_role_knob_cannot_grant_platform_administration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_ADMIN_ROLE", "platform-admin")
    reached: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:  # pragma: no cover
        reached.append(scope["path"])

    middleware = WebIdentityMiddleware(app, _IdentityResolver(["platform-admin"]))
    sent = await _run(middleware, "/api/v1/admin/system/overview")

    assert reached == []
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 403


async def test_strict_resolver_none_result_cannot_reopen_admin() -> None:
    reached: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:  # pragma: no cover
        reached.append(scope["path"])

    middleware = WebIdentityMiddleware(app, _StrictNoneResolver())
    sent = await _run(middleware, "/api/v1/admin/system/overview")

    assert reached == []
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 401


async def test_strict_resolver_none_result_keeps_non_admin_paths_working() -> None:
    reached: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        reached.append(scope["path"])

    middleware = WebIdentityMiddleware(app, _StrictNoneResolver())
    await _run(middleware, "/api/v1/sessions")

    assert reached == ["/api/v1/sessions"]


async def test_admin_api_token_enforced_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_ADMIN_API_TOKEN", "s3cret")

    with pytest.raises(APIError) as missing:
        await require_admin_api_bearer(SimpleNamespace(headers={}))
    assert missing.value.status_code == 401

    with pytest.raises(APIError):
        await require_admin_api_bearer(
            SimpleNamespace(headers={"authorization": "Bearer wrong"})
        )

    await require_admin_api_bearer(
        SimpleNamespace(headers={"authorization": "Bearer s3cret"})
    )


async def test_admin_api_stays_open_without_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ASTRABOX_ADMIN_API_TOKEN", raising=False)
    await require_admin_api_bearer(SimpleNamespace(headers={}))
