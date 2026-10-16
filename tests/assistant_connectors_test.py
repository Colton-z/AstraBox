"""Connectors through the hub: the session token's claims and who the relay acts as."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest

from astrabox.core.service.connectors.contextforge import ContextForge, HubConfig
from astrabox.core.service.orchestrator import platform_mcp_service
from astrabox.core.service.orchestrator.platform_mcp_service import PlatformMCPService

HUB = HubConfig(
    api_url="http://contextforge:4444",
    public_url="https://astrabox.example.com/connector-hub",
    jwt_secret="0123456789abcdef0123456789abcdef-hub",
    admin_email="hub-admin@example.com",
)


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
