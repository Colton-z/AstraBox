"""Published Agent MCP journey: real client protocol, identity, and Agent ACL."""

from __future__ import annotations

import asyncio

from typing import Any, AsyncIterator, Mapping

import httpx
import httpx2
import pytest
from fastapi import FastAPI
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp_types.version import LATEST_HANDSHAKE_VERSION, LATEST_PROTOCOL_VERSION

from astrabox.api.routes import agent_mcp as route_module
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.mcp_client_token_service import (
    SECRET_PREFIX,
    MCPClientTokenService,
)
from astrabox.core.service.orchestrator.agent_access import can_view_agent
from astrabox.web.identity_middleware import WebIdentityMiddleware, _is_exempt_path
from tests.e2e.test_mcp_client_journey import _recent_messages


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
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self.creating_reads_before_ready = 0
        #: Every background task the facade handed over, by name. Modelled
        #: rather than stubbed away: the drain outliving its tool call is part
        #: of the contract, so a double that silently dropped it would agree
        #: with a facade that leaks one.
        self.spawned: list[str] = []

    def _spawn_background_task(self, coro: Any, *, name: str | None = None) -> Any:
        self.spawned.append(str(name or ""))
        return asyncio.ensure_future(coro)

    async def stream_message_events_ds(
        self,
        user: UserContext,
        session_id: str,
        instruction: str,
        client_message_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        # Faithful to the real signature, and the stamp is load-bearing: the
        # dispatch refuses unstamped input, so a facade that stopped minting
        # one would fail here rather than four layers down on a live box.
        assert client_message_id, "the MCP facade must stamp every send"
        session = self.sessions[session_id]
        assert session["owner_id"] == user.user_id
        self.messages[session_id].append(
            {"role": "user", "content": instruction, "turn_id": "turn-1"}
        )
        session["state"] = "WAITING_INPUT"
        session["pending_interaction"] = {
            "interaction_id": "interaction-1",
            "tool_name": "AskUserQuestion",
            "pending_interaction": {
                "presentation": "form",
                "questions": [
                    {
                        "id": "choice",
                        "question": "Continue?",
                        "options": [{"label": "Yes"}, {"label": "No"}],
                    }
                ],
            },
        }
        yield {"type": "turn_started"}
        yield {"type": "turn_waiting_for_input"}

    async def get_session(
        self, user: UserContext, session_id: str
    ) -> dict[str, Any]:
        session = self.sessions[session_id]
        if session["owner_id"] != user.user_id:
            raise APIError("SESSION_NOT_FOUND", "session not found", 404)
        if session["state"] == "CREATING":
            if self.creating_reads_before_ready > 0:
                self.creating_reads_before_ready -= 1
            else:
                session["state"] = "READY"
        return dict(session)

    async def get_messages(
        self,
        user: UserContext,
        session_id: str,
        *,
        limit: int,
    ) -> dict[str, Any]:
        session = self.sessions[session_id]
        assert session["owner_id"] == user.user_id
        return {"messages": list(self.messages[session_id][-limit:])}

    async def answer_pending_interaction(
        self,
        user: UserContext,
        session_id: str,
        interaction_id: str,
        answer: dict[str, Any],
    ) -> None:
        session = self.sessions[session_id]
        assert session["owner_id"] == user.user_id
        assert interaction_id == "interaction-1"
        assert answer == {
            "answers": [{"question_id": "choice", "option_label": "Yes"}]
        }
        session["state"] = "READY"
        session["pending_interaction"] = None
        self.messages[session_id].append(
            {
                "role": "assistant",
                "content": "Finished after your answer.",
                "turn_id": "turn-1",
            }
        )

    async def interrupt(self, user: UserContext, session_id: str) -> None:
        session = self.sessions[session_id]
        assert session["owner_id"] == user.user_id
        session["state"] = "READY"

    async def recover_session(self, user: UserContext, session_id: str) -> None:
        _ = user
        self.sessions[session_id]["state"] = "READY"


class _AgentService:
    def __init__(self) -> None:
        self._agent_repo = _AgentRepository()
        self._platform = _Platform()
        self.started_by: list[str] = []
        self.started_context: list[tuple[str, str, list[str]]] = []
        self.conversation_initial_state = "READY"

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
        self.started_by.append(user.user_id)
        self.started_context.append((user.user_id, user.org_id, list(user.roles)))
        self._platform.sessions[session_id] = {
            "session_id": session_id,
            "session_kind": "agent_chat",
            "agent_id": agent_id,
            "owner_id": user.user_id,
            "state": self.conversation_initial_state,
            "pending_interaction": None,
        }
        self._platform.messages[session_id] = []
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
async def test_real_mcp_client_completes_waiting_input_journey(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, agent_service = _app(monkeypatch)
    http_client = httpx2.AsyncClient(
        base_url="http://astrabox.test",
        headers={"Authorization": "Bearer oidc-alice-access-token"},
        transport=httpx2.ASGITransport(app=app),
    )

    async with http_client:
        async with streamable_http_client(
            "http://astrabox.test/api/v1/mcp",
            http_client=http_client,
            terminate_on_close=False,
        ) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                initialized = await session.initialize()
                assert initialized.protocol_version == LATEST_HANDSHAKE_VERSION

                tools = await session.list_tools()
                assert {tool.name for tool in tools.tools} == {
                    "list_agents",
                    "create_conversation",
                    "send_message",
                    "get_status",
                    "answer_interaction",
                    "cancel_task",
                }

                listed = await session.call_tool("list_agents", {})
                assert listed.is_error is False
                assert listed.structured_content is not None
                assert [
                    agent["agent_id"]
                    for agent in listed.structured_content["agents"]
                ] == ["agent-a"]

                created = await session.call_tool(
                    "create_conversation", {"agent_id": "agent-a"}
                )
                assert created.is_error is False
                assert created.structured_content is not None
                session_id = str(created.structured_content["session_id"])
                assert created.structured_content["session_url_path"] == (
                    f"/sessions/{session_id}"
                )

                sent = await session.call_tool(
                    "send_message",
                    {
                        "agent_id": "agent-a",
                        "session_id": session_id,
                        "instruction": "Do the work",
                    },
                )
                assert sent.structured_content is not None
                assert sent.structured_content["state"] == "SUBMITTED"

                waiting = await session.call_tool(
                    "get_status",
                    {"agent_id": "agent-a", "session_id": session_id},
                )
                assert waiting.structured_content is not None
                assert waiting.structured_content["state"] == "WAITING_INPUT"
                pending = waiting.structured_content["pending_interaction"]
                assert pending["interaction_id"] == "interaction-1"

                answered = await session.call_tool(
                    "answer_interaction",
                    {
                        "agent_id": "agent-a",
                        "session_id": session_id,
                        "interaction_id": "interaction-1",
                        "answer": {
                            "answers": [
                                {
                                    "question_id": "choice",
                                    "option_label": "Yes",
                                }
                            ]
                        },
                    },
                )
                assert answered.is_error is False

                completed = await session.call_tool(
                    "get_status",
                    {"agent_id": "agent-a", "session_id": session_id},
                )
                assert completed.structured_content is not None
                assert completed.structured_content["state"] == "READY"
                assert _recent_messages(completed.structured_content)[-1]["content"] == (
                    "Finished after your answer."
                )

    assert agent_service.started_by == ["alice"]
    assert agent_service.started_context == [
        ("alice", "oidc-tenant", ["developer"])
    ]


@pytest.mark.asyncio
async def test_immediate_message_waits_for_conversation_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, agent_service = _app(monkeypatch)
    agent_service.conversation_initial_state = "CREATING"
    agent_service._platform.creating_reads_before_ready = 1
    monkeypatch.setattr(
        "astrabox.core.service.orchestrator.agent.agent_mcp_service."
        "_SESSION_CREATION_POLL_SECONDS",
        0.001,
    )
    http_client = httpx2.AsyncClient(
        base_url="http://astrabox.test",
        headers={"Authorization": "Bearer oidc-alice-access-token"},
        transport=httpx2.ASGITransport(app=app),
    )

    async with http_client:
        async with streamable_http_client(
            "http://astrabox.test/api/v1/mcp",
            http_client=http_client,
            terminate_on_close=False,
        ) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                created = await session.call_tool(
                    "create_conversation", {"agent_id": "agent-a"}
                )
                assert created.structured_content is not None
                sent = await session.call_tool(
                    "send_message",
                    {
                        "agent_id": "agent-a",
                        "session_id": created.structured_content["session_id"],
                        "instruction": "Start as soon as provisioning is ready",
                    },
                )

    assert sent.structured_content is not None
    assert sent.structured_content["state"] == "SUBMITTED"


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
