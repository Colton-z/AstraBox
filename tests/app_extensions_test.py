"""App extension points — routers / middlewares / lifespan hooks / service factories.

Pins the contracts documented in :mod:`astrabox.api.extensions` and
:mod:`astrabox.core.service.orchestrator.service_factories`:

* both router shapes mount (APIRouter object, register(app) callable); junk
  fails loud naming the entry point;
* both middleware shapes install; junk fails loud;
* lifespan hooks enter in entry-point order and exit in REVERSE; a hook's
  startup failure aborts boot (fail-loud), a teardown failure is tolerated;
* service-factory overrides: unknown service names, duplicate overrides and
  non-callable factories all fail loud; a registered override receives the
  default builder and its return value is what the platform keeps.

The loaders are exercised through their real group-resolution seam
(``_select_entry_points``) with fake entry points — the same object shape
``importlib.metadata`` yields — so the tests cover the load path itself, not
a parallel code path.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import APIRouter, FastAPI

import astrabox.api.extensions as extensions
import astrabox.core.service.orchestrator.service_factories as service_factories


class _FakeEp:
    def __init__(self, name: str, obj: Any, *, load_error: Exception | None = None) -> None:
        self.name = name
        self._obj = obj
        self._load_error = load_error

    def load(self) -> Any:
        if self._load_error is not None:
            raise self._load_error
        return self._obj


def _patch_group(monkeypatch: pytest.MonkeyPatch, module: Any, group: str, eps: list[_FakeEp]) -> None:
    def _fake_select(wanted_group: str):
        return {ep.name: ep for ep in eps} if wanted_group == group else {}

    monkeypatch.setattr(module, "_select_entry_points", _fake_select)


# ── routers ──────────────────────────────────────────────────────────────────


def test_extension_router_object_and_register_callable_both_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = APIRouter()

    @router.get("/ext/ping")
    async def _ping() -> dict[str, bool]:  # pragma: no cover - route body
        return {"ok": True}

    def register(app: FastAPI) -> None:
        @app.get("/ext/registered")
        async def _registered() -> dict[str, bool]:  # pragma: no cover
            return {"ok": True}

    _patch_group(
        monkeypatch,
        extensions,
        extensions.ROUTERS_GROUP,
        [_FakeEp("a_router", router), _FakeEp("b_register", register)],
    )
    app = FastAPI()
    extensions.include_extension_routers(app)
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        assert client.get("/ext/ping").status_code == 200
        assert client.get("/ext/registered").status_code == 200


def test_extension_router_junk_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_group(
        monkeypatch, extensions, extensions.ROUTERS_GROUP, [_FakeEp("junk", object())]
    )
    with pytest.raises(RuntimeError, match="junk"):
        extensions.include_extension_routers(FastAPI())


def test_extension_router_load_error_names_the_entry_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_group(
        monkeypatch,
        extensions,
        extensions.ROUTERS_GROUP,
        [_FakeEp("broken", None, load_error=ImportError("nope"))],
    )
    with pytest.raises(RuntimeError, match="broken"):
        extensions.include_extension_routers(FastAPI())


# ── middlewares ──────────────────────────────────────────────────────────────


def test_extension_middleware_class_and_install_callable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NoopMiddleware:
        def __init__(self, app: Any) -> None:  # pragma: no cover - shape only
            self.app = app

        async def __call__(self, scope, receive, send):  # pragma: no cover
            await self.app(scope, receive, send)

    installed: list[str] = []

    def install(app: FastAPI) -> None:
        installed.append("install-called")
        app.add_middleware(NoopMiddleware)

    _patch_group(
        monkeypatch,
        extensions,
        extensions.MIDDLEWARES_GROUP,
        [_FakeEp("a_class", NoopMiddleware), _FakeEp("b_install", install)],
    )
    app = FastAPI()
    extensions.install_extension_middlewares(app)
    assert installed == ["install-called"]
    assert sum(1 for m in app.user_middleware if m.cls is NoopMiddleware) == 2


def test_extension_middleware_junk_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_group(
        monkeypatch, extensions, extensions.MIDDLEWARES_GROUP, [_FakeEp("junk", 42)]
    )
    with pytest.raises(RuntimeError, match="junk"):
        extensions.install_extension_middlewares(FastAPI())


# ── lifespan hooks ───────────────────────────────────────────────────────────


async def test_lifespan_hooks_enter_in_order_exit_in_reverse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import contextlib

    events: list[str] = []

    def _hook(tag: str):
        @contextlib.asynccontextmanager
        async def _cm(app: FastAPI):
            events.append(f"enter:{tag}")
            try:
                yield
            finally:
                events.append(f"exit:{tag}")

        return _cm

    _patch_group(
        monkeypatch,
        extensions,
        extensions.LIFESPAN_HOOKS_GROUP,
        [_FakeEp("a", _hook("a")), _FakeEp("b", _hook("b"))],
    )
    async with extensions.extension_lifespan(FastAPI()):
        assert events == ["enter:a", "enter:b"]
    assert events == ["enter:a", "enter:b", "exit:b", "exit:a"]


async def test_lifespan_hook_startup_failure_aborts_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import contextlib

    @contextlib.asynccontextmanager
    async def _boom(app: FastAPI):
        raise RuntimeError("enterprise hook broken")
        yield  # pragma: no cover

    _patch_group(
        monkeypatch, extensions, extensions.LIFESPAN_HOOKS_GROUP, [_FakeEp("boom", lambda app: _boom(app))]
    )
    with pytest.raises(RuntimeError, match="enterprise hook broken"):
        async with extensions.extension_lifespan(FastAPI()):
            pass  # pragma: no cover


async def test_lifespan_hook_teardown_failure_is_tolerated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import contextlib

    entered: list[str] = []

    @contextlib.asynccontextmanager
    async def _bad_teardown(app: FastAPI):
        entered.append("in")
        try:
            yield
        finally:
            raise RuntimeError("teardown boom")

    _patch_group(
        monkeypatch,
        extensions,
        extensions.LIFESPAN_HOOKS_GROUP,
        [_FakeEp("bad", lambda app: _bad_teardown(app))],
    )
    async with extensions.extension_lifespan(FastAPI()):
        assert entered == ["in"]
    # reaching here without raising IS the assertion


# ── service factories ────────────────────────────────────────────────────────


def test_service_factory_override_wraps_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Wrapped:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

    def factory(build_default):
        return Wrapped(build_default())

    _patch_group(
        monkeypatch,
        service_factories,
        service_factories.SERVICE_FACTORIES_GROUP,
        [_FakeEp("ent", {"turn_service": factory})],
    )
    overrides = service_factories.load_service_factory_overrides(refresh=True)
    try:
        sentinel = object()
        built = overrides["turn_service"](lambda: sentinel)
        assert isinstance(built, Wrapped) and built.inner is sentinel
    finally:
        service_factories._cached = None


def test_service_factory_unknown_name_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_group(
        monkeypatch,
        service_factories,
        service_factories.SERVICE_FACTORIES_GROUP,
        [_FakeEp("ent", {"not_a_service": lambda d: d()})],
    )
    with pytest.raises(RuntimeError, match="not_a_service"):
        service_factories.load_service_factory_overrides(refresh=True)
    service_factories._cached = None


def test_service_factory_duplicate_override_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_group(
        monkeypatch,
        service_factories,
        service_factories.SERVICE_FACTORIES_GROUP,
        [
            _FakeEp("dist_a", {"admin_service": lambda d: d()}),
            _FakeEp("dist_b", {"admin_service": lambda d: d()}),
        ],
    )
    with pytest.raises(RuntimeError, match="more than one"):
        service_factories.load_service_factory_overrides(refresh=True)
    service_factories._cached = None


def test_platform_construct_routes_through_override() -> None:
    from astrabox.core.service.orchestrator.platform_service import (
        AgentPlatformService,
    )

    class FakePlatform:
        _construct = AgentPlatformService._construct
        _service_factory_overrides = {
            "session_service": lambda build_default: ("wrapped", build_default())
        }

    fake = FakePlatform()
    assert fake._construct("session_service", lambda: "stock") == ("wrapped", "stock")
    assert fake._construct("turn_service", lambda: "stock") == "stock"
