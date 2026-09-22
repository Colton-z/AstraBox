"""Engine capability declarations cross the platform boundary unchanged.

The manifest is engine vocabulary.  Session-detail rendering may serialize the
dataclass for JSON, but it must not reconstruct capabilities from ``engine_kind``
or keep a platform-owned copy that can drift from the engine client.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from astrabox.api.routes import sessions as session_routes
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.engine.base import EngineCapabilityManifest
from astrabox.core.service.orchestrator.platform_service import AgentPlatformService


class _DeclaringEngineClient:
    def __init__(self, manifest: EngineCapabilityManifest) -> None:
        self.manifest = manifest

    async def get_capabilities(self) -> EngineCapabilityManifest:
        return self.manifest

    async def set_permission_mode(self, mode: str) -> None:
        assert mode

    async def cancel_turn(self, _receipt) -> bool:
        return True

    async def interrupt_active_turn(self) -> bool:
        return True


class _RuntimeManager:
    def __init__(self, runtime: object | None) -> None:
        self.runtime = runtime

    def get_runtime(
        self, session_id: str, *, sandbox_id: str | None = None
    ) -> object | None:
        assert session_id == "session-1"
        assert sandbox_id == "sandbox-1"
        return self.runtime


def _platform_with_runtime(
    runtime: object | None,
    *,
    persisted_manifest: dict[str, object] | None = None,
) -> AgentPlatformService:
    session = {
        "session_id": "session-1",
        "sandbox_id": "sandbox-1",
        "engine_kind": "test-engine",
    }
    if persisted_manifest is not None:
        session["engine_capabilities"] = persisted_manifest
    platform = AgentPlatformService.__new__(AgentPlatformService)
    platform.ensure_bootstrap = AsyncMock()  # type: ignore[method-assign]
    platform._session_service = SimpleNamespace(  # type: ignore[attr-defined]
        must_get_owned_session=AsyncMock(return_value=session)
    )
    platform._session_kernel = SimpleNamespace(  # type: ignore[attr-defined]
        get_session=AsyncMock(return_value=dict(session))
    )
    platform._runtime_manager = _RuntimeManager(runtime)  # type: ignore[attr-defined]
    return platform


def test_session_detail_uses_the_manifest_bound_when_the_runtime_was_created(
    monkeypatch,
) -> None:
    declared = EngineCapabilityManifest(
        engine_kind="test-engine",
        tools=["first-tool"],
        permission_modes=["observe"],
        supports_interaction=False,
        extra={"vendor_extension": {"level": 7}},
    )
    engine_client = _DeclaringEngineClient(declared)
    platform = _platform_with_runtime(
        SimpleNamespace(
            engine_client=engine_client,
            engine_kind="test-engine",
            engine_manifest=declared,
            conversation_bound=True,
        )
    )

    async def _resolve_user(_request) -> UserContext:
        return UserContext(user_id="user-1")

    monkeypatch.setattr(session_routes, "_resolve_user", _resolve_user)
    monkeypatch.setattr(session_routes, "_svc", lambda: platform)
    app = FastAPI()
    app.include_router(session_routes.router)

    with TestClient(app) as client:
        first = client.get("/api/v1/sessions/session-1")
        assert first.status_code == 200
        assert first.json()["data"]["engine_capabilities"] == {
            "engine_kind": "test-engine",
            "tools": ["first-tool"],
            "input_content_types": ["text"],
            "permission_modes": ["observe"],
            "supports_interaction": False,
            "supports_child_run_control": False,
            "supports_server_info": False,
            "extra": {"vendor_extension": {"level": 7}},
        }

        engine_client.manifest = EngineCapabilityManifest(
            engine_kind="test-engine",
            tools=["newly-declared-tool"],
            extra={"vendor_extension": {"level": 8}},
        )
        second = client.get("/api/v1/sessions/session-1")

    assert second.json()["data"]["engine_capabilities"]["tools"] == ["first-tool"]
    assert second.json()["data"]["engine_capabilities"]["extra"] == {
        "vendor_extension": {"level": 7}
    }


async def test_session_detail_does_not_invent_a_manifest_without_a_live_client() -> None:
    platform = _platform_with_runtime(None)

    detail = await platform.get_session(UserContext(user_id="user-1"), "session-1")

    assert detail["engine_capabilities"] is None


async def test_session_detail_keeps_the_last_verified_manifest_after_host_restart() -> None:
    persisted = {
        "engine_kind": "test-engine",
        "tools": ["persisted-tool"],
        "permission_modes": ["observe"],
        "supports_interaction": True,
        "supports_child_run_control": False,
        "supports_server_info": True,
        "extra": {"vendor_extension": {"level": 7}},
    }
    platform = _platform_with_runtime(None, persisted_manifest=persisted)

    detail = await platform.get_session(UserContext(user_id="user-1"), "session-1")

    assert detail["engine_capabilities"] == persisted


async def test_a_live_runtime_with_no_capability_method_fails_loud() -> None:
    platform = _platform_with_runtime(
        SimpleNamespace(
            engine_client=object(),
            engine_kind="test-engine",
            engine_manifest=None,
            conversation_bound=True,
        )
    )

    try:
        await platform.get_session(UserContext(user_id="user-1"), "session-1")
    except APIError as exc:
        assert exc.code == "ENGINE_CAPABILITY_CONTRACT_VIOLATION"
        assert "validated engine capability manifest" in str(exc)
    else:  # pragma: no cover - contract violation
        raise AssertionError("a live engine without the manifest contract must fail")
