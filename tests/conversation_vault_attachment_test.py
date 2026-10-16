"""Managed credential bindings reach Sessions without a user-side selector.

Vaults are administrator-only resources.  A conversation request names only
the Agent/Assistant; SessionService inherits the Vault binding from the
resolved managed configuration.  The authenticated user remains the Session
owner but is not a Vault owner, selector, or lookup key.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from astrabox.api.routes import agents as agent_routes
from astrabox.api.routes import assistant as assistant_routes
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.session_service import SessionService


_USER = UserContext(user_id="user-1")


class _RouteConversationService:
    """Stands in for AgentService/AssistantService at the conversation route.

    ``started`` is the rest of what the real service answers with, beyond the
    session id both of them carry.  The agent route's response model requires
    ``agent_id`` because ``AgentService.start_conversation`` always names the
    agent alongside the session; a stand-in that answered with less would be
    rewritten into a 500 before reaching the assertions these tests make.
    """

    def __init__(self, started: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._started = dict(started or {})

    async def start_conversation(
        self,
        user: UserContext,
        subject_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append({"user": user, "subject_id": subject_id, **kwargs})
        return {"session_id": f"session-{subject_id}", **self._started}


async def _current_user(_request: Any) -> UserContext:
    return _USER


@pytest.mark.parametrize("routes", [agent_routes, assistant_routes])
def test_conversation_routes_refuse_user_supplied_vault_ids(
    routes: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _RouteConversationService(
        {"agent_id": "agent-1"} if routes is agent_routes else None
    )
    monkeypatch.setattr(routes, "_registered_on", None)
    if routes is agent_routes:
        monkeypatch.setattr(routes, "get_agent_service", lambda: service)
        monkeypatch.setattr(routes, "get_current_user_context", _current_user)
        platform_service_calls = 0

        def _unexpected_platform_service() -> Any:
            nonlocal platform_service_calls
            platform_service_calls += 1
            return object()

        monkeypatch.setattr(
            routes,
            "get_platform_service",
            _unexpected_platform_service,
        )
        path = "/api/v1/agents/agent-1/conversations"
    else:
        monkeypatch.setattr(routes, "get_assistant_service", lambda: service)
        monkeypatch.setattr(routes, "get_current_user_context", _current_user)
        path = "/api/v1/assistants/assistant-1/conversations"

    app = FastAPI()
    routes.register_agent_routes(app) if routes is agent_routes else routes.register_assistant_routes(app)

    with TestClient(app) as client:
        injected = client.post(path, json={"vault_ids": ["admin-vault"]})
        ordinary = client.post(path)

    assert injected.status_code == 422
    assert ordinary.status_code == 200
    assert service.calls == [{"user": _USER, "subject_id": "agent-1" if routes is agent_routes else "assistant-1"}]
    if routes is agent_routes:
        assert platform_service_calls == 0


class _CapturingSessions:
    def __init__(self) -> None:
        self.payload: dict[str, Any] | None = None

    async def create_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payload = dict(payload)
        return dict(payload)


class _SessionAgentConfig:
    async def resolve_session_harness(self, _session: dict[str, Any]) -> Any:
        return SimpleNamespace(
            name="Code review",
            sandbox_backend="selected-backend",
            credential_vault_ids=[
                " vault-managed-b ",
                "vault-managed-a",
                "vault-managed-b",
            ],
        )


class _SessionRuntime:
    @staticmethod
    def resolve_template_model_name(_template: Any) -> str | None:
        return "model-1"


class _ValidatingManagedVaults:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def validate_bound_vaults_for_session(
        self,
        vault_ids: list[str],
        *,
        backend_name: str,
        backend_supports_egress_injection: bool,
        egress_injection_unsupported_reason: str | None = None,
    ) -> list[str]:
        self.calls.append(
            {
                "vault_ids": vault_ids,
                "backend_name": backend_name,
                "backend_supports_egress_injection": backend_supports_egress_injection,
                "egress_injection_unsupported_reason": egress_injection_unsupported_reason,
            }
        )
        return ["vault-managed-b", "vault-managed-a"]


async def test_session_inherits_managed_binding_without_using_user_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _CapturingSessions()
    vaults = _ValidatingManagedVaults()
    monkeypatch.setattr(
        "astrabox.core.service.orchestrator.session_service.sandbox_for_name",
        lambda name: SimpleNamespace(
            name=name,
            supports_egress_credential_injection=True,
        ),
    )
    service = SessionService(
        sessions_repo=sessions,  # type: ignore[arg-type]
        agent_config=_SessionAgentConfig(),  # type: ignore[arg-type]
        runtime_manager=_SessionRuntime(),  # type: ignore[arg-type]
        ttl_seconds=3600,
        spawn_background_task=lambda *args, **kwargs: None,
        vault_service=vaults,  # type: ignore[arg-type]
    )

    created = await service.create_session_record(
        _USER,
        workspace_ref={"kind": "agent", "agent_id": "agent-1"},
        agent_id="agent-1",
    )

    assert vaults.calls == [
        {
            "vault_ids": [
                " vault-managed-b ",
                "vault-managed-a",
                "vault-managed-b",
            ],
            "backend_name": "selected-backend",
            "backend_supports_egress_injection": True,
            "egress_injection_unsupported_reason": None,
        }
    ]
    assert sessions.payload is not None
    assert sessions.payload["vault_ids"] == ["vault-managed-b", "vault-managed-a"]
    assert "vault_ids" not in created
