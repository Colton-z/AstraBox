"""Connectors through the hub: the switches, the relay's identity and Google's offline access."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.connectors.contextforge import ContextForge, HubConfig
from astrabox.core.service.connectors.presets import connector_preset
from astrabox.core.service.orchestrator import platform_mcp_service
from astrabox.core.service.orchestrator.platform_mcp_service import PlatformMCPService
from astrabox.core.service.connectors.service import (
    AgentConnectors,
    AssistantConnectors,
    ConnectorService,
)

HUB = HubConfig(
    api_url="http://contextforge:4444",
    public_url="https://astrabox.example.com/connector-hub",
    jwt_secret="0123456789abcdef0123456789abcdef-hub",
    admin_email="hub-admin@example.com",
)


class _Hub(ContextForge):
    """The hub's server list, without its HTTP API."""

    def __init__(self, servers: dict[str, str]) -> None:
        super().__init__(HUB)
        self._servers = servers

    async def find_server(self, name: str) -> dict[str, Any] | None:
        server_id = self._servers.get(name)
        return {"id": server_id, "name": name} if server_id else None


class _Assistants:
    """The Assistant service calls a connector switch makes, over one record."""

    def __init__(self) -> None:
        self.assistant: dict[str, Any] = {"assistant_id": "asst_1", "connector_servers": None}

    async def get_assistant(self, user: UserContext, assistant_id: str) -> dict[str, Any]:
        return dict(self.assistant)


    async def set_connector_servers(
        self,
        user: UserContext,
        assistant_id: str,
        servers: dict[str, str],
        *,
        connectors_off: list[str] | None = None,
    ) -> dict[str, Any]:
        self.assistant["connector_servers"] = dict(servers) or None
        if connectors_off is not None:
            self.assistant["connectors_off"] = connectors_off or None
        return self.assistant


@pytest.mark.asyncio
async def test_an_enabled_connector_is_the_hubs_server_for_that_app() -> None:
    assistants = _Assistants()
    switch = AssistantConnectors(
        service_getter=lambda: assistants, hub=_Hub({"gmail": "srv1"})
    )

    enabled = await switch.set_enabled(UserContext("owner"), "asst_1", "gmail", enabled=True)

    assert enabled == ["gmail"]
    assert assistants.assistant["connector_servers"] == {"gmail": "srv1"}
    with pytest.raises(APIError) as unpublished:
        await switch.set_enabled(UserContext("owner"), "asst_1", "notion", enabled=True)
    assert unpublished.value.code == "CONNECTOR_NOT_AVAILABLE"


def test_a_hub_session_token_carries_the_claims_contextforge_checks() -> None:
    token = ContextForge(HUB).session_token("owner@example.com", ttl_seconds=60)

    claims = jwt.decode(
        token, HUB.jwt_secret, algorithms=["HS256"], audience="mcpgateway-api", issuer="mcpgateway"
    )

    assert claims["sub"] == "owner@example.com"
    # Without it the hub limits a token with no teams claim to public resources.
    assert claims["token_use"] == "session"
    assert claims["jti"] and claims["exp"] > claims["iat"]
    assert "is_admin" not in claims


@pytest.mark.asyncio
async def test_gmail_asks_google_for_a_refresh_token() -> None:
    service = ConnectorService(hub=ContextForge(HUB))
    preset = connector_preset("gmail")
    assert preset is not None

    config = await service._oauth_config(
        preset,
        callback="https://astrabox.example.com/connector-hub/oauth/callback",
        client_id="client-1",
        client_secret="secret-1",
    )

    assert config["extra_auth_params"] == {"access_type": "offline", "prompt": "consent"}
    assert config["omit_resource"] is True
    assert config["authorization_url"] == "https://accounts.google.com/o/oauth2/v2/auth"
    # google_workspace_mcp checks every token at Google's userinfo endpoint.
    assert "https://www.googleapis.com/auth/userinfo.email" in config["scopes"]
    assert config["redirect_uri_after_oauth"] == "https://astrabox.example.com/connections?connected=gmail"


class _Agents:
    """The Agent service calls a connector switch makes, over one record."""

    def __init__(self, mcp_servers: dict[str, Any]) -> None:
        self.agent: dict[str, Any] = {"agent_id": "agent_1", "mcp_servers": mcp_servers}

    async def get_agent(self, user: UserContext, agent_id: str) -> dict[str, Any]:
        return dict(self.agent)


    async def set_connector_servers(
        self, user: UserContext, agent_id: str, servers: dict[str, str]
    ) -> dict[str, Any]:
        self.agent["connector_servers"] = dict(servers) or None
        return self.agent


@pytest.mark.asyncio
async def test_an_agent_connector_is_refused_where_its_name_is_taken() -> None:
    agents = _Agents({"gmail": {"type": "http", "url": "https://mail.example.com/mcp"}})
    switch = AgentConnectors(service_getter=lambda: agents, hub=_Hub({"gmail": "srv1", "notion": "srv2"}))

    with pytest.raises(APIError) as refused:
        await switch.set_enabled(UserContext("author"), "agent_1", "gmail", enabled=True)
    assert refused.value.code == "AGENT_MCP_NAME_CONFLICT"

    enabled = await switch.set_enabled(UserContext("author"), "agent_1", "notion", enabled=True)
    assert enabled == ["notion"]
    assert agents.agent["connector_servers"] == {"notion": "srv2"}


class _RelayHub(ContextForge):
    """The hub's MCP endpoint, recording who each relayed request acts as."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []

        def answer(request: httpx.Request) -> httpx.Response:
            token = request.headers["authorization"].removeprefix("Bearer ")
            claims = jwt.decode(token, HUB.jwt_secret, algorithms=["HS256"], audience="mcpgateway-api")
            body = json.loads(request.content)
            self.seen.append({"path": request.url.path, "as": claims["sub"], "method": body["method"]})
            event = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": {"ok": True}})
            return httpx.Response(200, text=f"event: message\ndata: {event}\n\n",
                                  headers={"content-type": "text/event-stream", "mcp-session-id": "hub-1"})

        super().__init__(HUB, transport=httpx.MockTransport(answer))

    async def ensure_member(self, *, email: str, full_name: str) -> None:
        return None


def _relay_service(session: dict[str, Any] | None, binding: dict[str, Any] | None) -> PlatformMCPService:
    return PlatformMCPService(
        sessions_repo=SimpleNamespace(get_session=AsyncMock(return_value=session)),
        binding_repo=SimpleNamespace(get_binding=AsyncMock(return_value=binding)),
        agent_config=None,
        expose_port_service=None,
    )


@pytest.mark.asyncio
async def test_the_relay_acts_as_the_sessions_person_and_lists_tools_for_anyone(monkeypatch) -> None:
    hub = _RelayHub()
    monkeypatch.setattr(platform_mcp_service, "hub_from_env", lambda: hub)
    server = {"deployment_id": "dep-1", "server_name": "gmail", "hub_server": "srv1"}
    call = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "search", "arguments": {}}}

    person = _relay_service({"user_id": "caller", "agent_id": "agent-1", "source_type": "console"}, None)
    answer = await person.relay_connector(server, call, session_id="hub-1", protocol_version=None)
    assert answer.message == {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}
    assert answer.session_id == "hub-1"
    assert hub.seen[-1] == {"path": "/servers/srv1/mcp", "as": hub.member_email("caller"), "method": "tools/call"}

    # A prepared runtime before its claim has no person: it may list the
    # tools as the hub's administrator, and a call is refused here.
    prepared = _relay_service(None, {"deployment_id": "dep-1", "template_name": "agent-1"})
    await prepared.relay_connector(server, {**call, "method": "tools/list"}, session_id=None, protocol_version=None)
    assert hub.seen[-1]["as"] == HUB.admin_email
    refused = await prepared.relay_connector(server, call, session_id=None, protocol_version=None)
    assert refused.message is not None and refused.message["result"]["isError"] is True
    assert len(hub.seen) == 2

    # An Assistant's engine serves its owner's conversations through one
    # address bound to the Assistant, and acts for that owner.
    owners = _relay_service(None, {"scope_kind": "assistant_workspace", "assistant_id": "asst_1", "user_id": "owner"})
    await owners.relay_connector(server, call, session_id=None, protocol_version=None)
    assert hub.seen[-1]["as"] == hub.member_email("owner")

    # A channel conversation of an Agent acts for no one either.
    channel = _relay_service({"user_id": "author", "agent_id": "agent-1", "source_type": "channel"}, None)
    assert (await channel.relay_connector(server, call, session_id=None, protocol_version=None)).message["result"]["isError"]
    assert len(hub.seen) == 3
