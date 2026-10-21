"""Published Agent MCP facade: real client protocol, identity, and Agent ACL."""

from __future__ import annotations

from typing import Any, Mapping

import httpx
import pytest
from fastapi import FastAPI
from mcp_types.version import LATEST_PROTOCOL_VERSION

from astrabox.api.routes import agent_mcp as route_module
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.mcp_client_token_service import (
    SECRET_PREFIX,
    MCPClientTokenService,
)
from astrabox.core.service.orchestrator.agent_access import can_view_agent
from astrabox.web.identity_middleware import WebIdentityMiddleware, _is_exempt_path


@pytest.fixture(autouse=True)
def _environment_membership(environment_member_accounts: set[str]) -> None:
    environment_member_accounts.update({'alice'})


class _AgentRepository:
    def __init__(self) -> None:
        self.agents = {
            "agent-a": {
                "agent_id": "agent-a",
                "name": "Agent A",
                "state": "ACTIVE",
                "visibility": "private",
                "created_by": "alice",
                "admins": [],
            },
            "agent-b": {
                "agent_id": "agent-b",
                "name": "Agent B",
                "state": "ACTIVE",
                "visibility": "private",
                "created_by": "bob",
                "admins": [],
            },
        }

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        agent = self.agents.get(agent_id)
        return dict(agent) if agent is not None else None


class _Platform:
    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}


class _AgentService:
    def __init__(self) -> None:
        self._agent_repo = _AgentRepository()
        self._platform = _Platform()

    async def list_agents(self, user: UserContext) -> list[dict[str, Any]]:
        return [
            dict(agent)
            for agent in self._agent_repo.agents.values()
            if can_view_agent(agent, user.user_id, user.roles)
        ]

    async def start_conversation(
        self, user: UserContext, agent_id: str
    ) -> dict[str, str]:
        session_id = f"session-{len(self._platform.sessions) + 1}"
        self._platform.sessions[session_id] = {
            "session_id": session_id,
            "session_kind": "agent_chat",
            "agent_id": agent_id,
            "owner_id": user.user_id,
        }
        return {"session_id": session_id, "agent_id": agent_id}

    async def wake_agent(self, user: UserContext, agent_id: str) -> None:
        _ = user
        self._agent_repo.agents[agent_id]["state"] = "ACTIVE"


class _AuthenticatedIdentity:
    async def resolve(self, headers: Mapping[str, str]) -> UserContext:
        authorization = str(headers.get("authorization") or "")
        token = authorization.removeprefix("Bearer ")
        if token != "oidc-alice-access-token":
            raise APIError(
                "AUTH_REQUIRED",
                "OIDC access token is invalid",
                401,
            )
        return UserContext("alice", org_id="oidc-tenant", roles=["developer"])


def _app(monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, _AgentService]:
    monkeypatch.setattr(route_module, "_registered_on", None)
    app = FastAPI()
    agent_service = _AgentService()
    route_module.register_agent_mcp_routes(
        app,
        agent_service=agent_service,
    )
    # Mirror the production wiring (astrabox/api/app.py): the front door
    # passes the deployment's own MCP client keys through to the facade's
    # verifier instead of judging them as OIDC tokens.
    app.add_middleware(
        WebIdentityMiddleware,
        resolver=_AuthenticatedIdentity(),
        capability_bearer_paths={"/api/v1/mcp": (SECRET_PREFIX,)},
    )
    return app, agent_service


@pytest.mark.asyncio
async def test_missing_wrong_credentials_and_inaccessible_agent_call_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = _app(monkeypatch)
    transport = httpx.ASGITransport(app=app)
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": LATEST_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    async with httpx.AsyncClient(
        base_url="http://astrabox.test", transport=transport
    ) as client:
        missing = await client.post(
            "/api/v1/mcp", json=initialize
        )
        wrong = await client.post(
            "/api/v1/mcp",
            headers={"Authorization": "Bearer wrong"},
            json=initialize,
        )
        inaccessible_agent = await client.post(
            "/api/v1/mcp",
            headers={"Authorization": "Bearer oidc-alice-access-token"},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "create_conversation",
                    "arguments": {"agent_id": "agent-b"},
                },
            },
        )

    assert missing.status_code == 401
    assert missing.headers["www-authenticate"].startswith("Bearer")
    assert wrong.status_code == 401
    assert inaccessible_agent.status_code == 200
    inaccessible_result = inaccessible_agent.json()["result"]
    assert inaccessible_result["isError"] is True
    assert inaccessible_result["structuredContent"]["error"]["code"] == (
        "AGENT_NOT_FOUND"
    )


@pytest.mark.asyncio
async def test_public_mcp_uses_the_shared_web_identity_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = _app(monkeypatch)
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": LATEST_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    async with httpx.AsyncClient(
        base_url="http://astrabox.test",
        transport=httpx.ASGITransport(app=app),
    ) as client:
        accepted = await client.post(
            "/api/v1/mcp",
            headers={"Authorization": "Bearer oidc-alice-access-token"},
            json=initialize,
        )
        missing = await client.post(
            "/api/v1/mcp",
            json=initialize,
        )

    assert accepted.status_code == 200
    assert accepted.json()["result"]["serverInfo"]["name"] == "astrabox-agents"
    assert missing.status_code == 401
    assert missing.json()["code"] == "AUTH_REQUIRED"


@pytest.mark.asyncio
async def test_streamable_http_get_and_origin_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = _app(monkeypatch)
    transport = httpx.ASGITransport(app=app)
    path = "/api/v1/mcp"
    async with httpx.AsyncClient(
        base_url="http://astrabox.test",
        transport=transport,
    ) as client:
        no_event_stream = await client.get(
            path,
            headers={"Authorization": "Bearer oidc-alice-access-token"},
        )
        invalid_origin = await client.get(
            path,
            headers={
                "Authorization": "Bearer oidc-alice-access-token",
                "Origin": "https://attacker.example",
            },
        )
        per_agent_endpoint = await client.post(
            "/api/v1/agents/agent-a/mcp",
            headers={"Authorization": "Bearer oidc-alice-access-token"},
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
        unsupported_version = await client.post(
            path,
            headers={
                "Authorization": "Bearer oidc-alice-access-token",
                "MCP-Protocol-Version": "1900-01-01",
            },
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )

    assert no_event_stream.status_code == 405
    assert no_event_stream.headers["allow"] == "POST"
    assert invalid_origin.status_code == 403
    assert per_agent_endpoint.status_code == 404
    assert unsupported_version.status_code == 400
    assert unsupported_version.json()["code"] == "MCP_PROTOCOL_VERSION_UNSUPPORTED"


def test_public_mcp_is_not_exempt_from_shared_http_identity() -> None:
    assert _is_exempt_path("/api/v1/mcp") is False
    assert _is_exempt_path("/api/v1/mcp/") is False
    assert _is_exempt_path("/api/v1/agents/agent-a") is False


@pytest.mark.asyncio
@pytest.mark.usefixtures("database")
async def test_a_deployment_issued_key_passes_the_strict_front_door(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The OIDC front door must not eat the platform's own MCP client keys.

    Three sides of the same rule: a key the facade recognises reaches its
    verifier and works; a key-shaped secret the deployment never issued is
    refused BY THE FACADE (fail-closed, with the discovery challenge); and
    the same key on any other path still meets the front door, so the
    carve-out cannot become a general bypass.
    """
    app, _agent_service = _app(monkeypatch)
    issued = await MCPClientTokenService().issue(
        UserContext("alice", org_id="oidc-tenant", roles=["developer"]),
        name="front-door-carveout",
        scope="converse",
    )
    key = str(issued["secret"])
    transport = httpx.ASGITransport(app=app)
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": LATEST_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    async with httpx.AsyncClient(
        base_url="http://astrabox.test", transport=transport
    ) as client:
        accepted = await client.post(
            "/api/v1/mcp",
            headers={"Authorization": f"Bearer {key}"},
            json=initialize,
        )
        unknown_key = await client.post(
            "/api/v1/mcp",
            headers={"Authorization": f"Bearer {SECRET_PREFIX}never-issued"},
            json=initialize,
        )
        other_path = await client.get(
            "/api/v1/agents",
            headers={"Authorization": f"Bearer {key}"},
        )

    assert accepted.status_code == 200, accepted.text
    assert unknown_key.status_code == 401
    assert unknown_key.headers["www-authenticate"].startswith("Bearer")
    assert other_path.status_code == 401
