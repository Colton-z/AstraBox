"""A real MCP client, a key this deployment issued, and one whole conversation.

Everything about the facade has been proven in-process: the protocol handshake,
the tool surface, the scope fence. None of it has been proven against a running
deployment, which is where the identity middleware, the ASGI stack in front of
it and the key's own storage are real. This is that run.

The client is the vendor's own `ClientSession` over `streamable_http_client`,
not a hand-rolled JSON-RPC caller. A hand-rolled one proves that this
repository's encoder agrees with its own decoder, which is not the question.
The question is whether Claude Desktop or Cursor can talk to this facade.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Iterator

import httpx
import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp_types.version import SUPPORTED_PROTOCOL_VERSIONS

from tests.e2e._sandbox_helpers import (
    OPEN_SESSIONS,
    agent_id as matrix_agent_id,
    data,
    release_session,
)

pytestmark = pytest.mark.e2e

TURN_TIMEOUT_S = float(os.getenv("ASTRABOX_E2E_MCP_TURN_TIMEOUT", "180"))

_TOOLS = {
    "list_agents",
    "create_conversation",
    "send_message",
    "get_status",
    "answer_interaction",
    "cancel_task",
}


@pytest.fixture
def issued_keys(e2e_client: httpx.Client) -> Iterator[list[str]]:
    """Keys minted by a test, revoked only if it passed.

    A key is server state, so it follows the same rule as a session: on a
    failure it stays, because reproducing the failure by hand needs the exact
    credential the run used. The deployment's operator can revoke it from the
    list; an automatic revoke would take the one thing that makes the scene
    re-runnable.
    """
    minted: list[str] = []
    yield minted
    if not minted:
        return
    listed = data(e2e_client.get("/api/v1/mcp-tokens")).get("tokens") or []
    by_name = {row.get("name"): row.get("token_id") for row in listed}
    for name in minted:
        token_id = by_name.get(name)
        if token_id:
            e2e_client.delete(f"/api/v1/mcp-tokens/{token_id}")


def _issue(client: httpx.Client, keys: list[str], *, name: str, scope: str) -> str:
    # 201: issuing a key creates a resource. Asserted here rather than relaxed
    # in the shared helper, so this route stays held to the status it promises.
    issued = data(
        client.post("/api/v1/mcp-tokens", json={"name": name, "scope": scope}), expect=201
    )
    keys.append(name)
    secret = str(issued.get("secret") or "")
    assert secret, "issuing a key must return its secret exactly once"
    return secret


def _mcp_session(base_url: str, secret: str) -> Any:
    return streamable_http_client(
        f"{base_url}/api/v1/mcp",
        http_client=httpx2.AsyncClient(
            base_url=base_url,
            timeout=TURN_TIMEOUT_S,
            headers={"Authorization": f"Bearer {secret}"},
        ),
        terminate_on_close=False,
    )


def _recent_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Read the transcript field the MCP ``get_status`` contract publishes.

    Missing and empty are different facts: a missing field means this client and
    the facade disagree about the wire contract, while an empty list means the
    facade successfully reported an empty transcript.  Collapsing both into
    ``[]`` misreports the former as a stalled turn.
    """

    messages = payload.get("recent_messages")
    assert isinstance(messages, list), (
        "get_status did not publish its recent_messages list; this is a wire-contract "
        f"mismatch, not an empty transcript (keys={sorted(payload)})"
    )
    assert all(isinstance(message, dict) for message in messages), (
        "get_status recent_messages must contain message objects"
    )
    return messages


async def test_a_converse_key_carries_a_client_through_a_whole_conversation(
    e2e_base_url: str,
    e2e_client: httpx.Client,
    issued_keys: list[str],
) -> None:
    secret = _issue(e2e_client, issued_keys, name="e2e-mcp-converse", scope="converse")

    async with _mcp_session(e2e_base_url, secret) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            initialized = await session.initialize()
            assert initialized.protocol_version in SUPPORTED_PROTOCOL_VERSIONS
            assert initialized.server_info.name == "astrabox-agents"

            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == _TOOLS

            listed = await session.call_tool("list_agents", {})
            assert listed.is_error is False
            agents = (listed.structured_content or {}).get("agents") or []
            matches = [item for item in agents if item.get("agent_id") == matrix_agent_id()]
            assert len(matches) == 1, (
                "the MCP facade did not expose this matrix engine's exact Investment "
                f"Agent once: expected={matrix_agent_id()!r} agents={agents}"
            )
            agent_id = matches[0]["agent_id"]

            created = await session.call_tool("create_conversation", {"agent_id": agent_id})
            assert created.is_error is False, created.structured_content
            session_id = str((created.structured_content or {}).get("session_id") or "")
            assert session_id, "create_conversation must answer with a session id"

            # Registered before anything else can fail. The reaper deletes it
            # only if this test passes; asserting the registration separately is
            # the lesson from converting the browser suite — "the delete moved"
            # and "the tracker received it" are two facts, and a batch that
            # checked only the first left sessions leaking behind a green run.
            release_session(session_id)
            assert session_id in OPEN_SESSIONS

            sent = await session.call_tool(
                "send_message",
                {
                    "agent_id": agent_id,
                    "session_id": session_id,
                    # `instruction` is the name the tool declares. Any other name
                    # arrives as empty content, which the facade now refuses.
                    "instruction": "Reply with the single word MCPOK and nothing else.",
                },
            )
            assert sent.is_error is False, sent.structured_content

            # `send_message` answers SUBMITTED while the engine is still working,
            # so the conversation is polled rather than read once. Reading once
            # raced persistence — at the instant the tool returns, not even the
            # user's own message has been written yet — and a race that usually
            # loses is worse than one that always does, because it fails as a
            # flake instead of as a defect.
            payload: dict[str, Any] = {}
            seen_states: list[str] = []
            deadline = time.monotonic() + TURN_TIMEOUT_S
            while time.monotonic() < deadline:
                status = await session.call_tool(
                    "get_status",
                    {"agent_id": agent_id, "session_id": session_id},
                )
                assert status.is_error is False, status.structured_content
                payload = status.structured_content or {}
                state = str(payload.get("state") or "").upper()
                if not seen_states or seen_states[-1] != state:
                    seen_states.append(state)
                if state not in {"BUSY", "PROCESSING"}:
                    break
                await asyncio.sleep(2.0)

            state = str(payload.get("state") or "").upper()
            assert state, "get_status must report a state"
            messages = _recent_messages(payload)
            roles = [str(message.get("role") or "") for message in messages]
            assert state == "READY", (
                f"get_status never settled READY within {TURN_TIMEOUT_S:.0f}s; "
                f"states={seen_states}, current_turn_id={payload.get('current_turn_id')!r}, "
                f"roles={roles}"
            )
            # The reply has to have reached the transcript through the facade,
            # not merely been accepted by it: a send that returned OK and stored
            # nothing is the failure this whole journey exists to catch.
            assert messages, (
                "get_status settled READY without the synchronously projected user "
                "message or an assistant reply"
            )
            assistant_messages = [
                message
                for message in messages
                if str(message.get("role") or "") == "assistant"
            ]
            assert assistant_messages, (
                f"the conversation holds only {[m.get('role') for m in messages]} — the "
                "instruction reached the transcript but no reply came back, which is a "
                "turn that was dispatched and never concluded"
            )
            assistant_text = "\n".join(
                str(message.get("content") or "") for message in assistant_messages
            )
            assert "MCPOK" in assistant_text.upper(), (
                "the turn settled but did not carry the requested model reply; an empty "
                f"failure projection must not count as success (assistant={assistant_text!r})"
            )


async def test_a_read_key_is_refused_the_moment_it_tries_to_spend(
    e2e_base_url: str,
    e2e_client: httpx.Client,
    issued_keys: list[str],
) -> None:
    """The scope fence, against a deployment rather than a dispatch call.

    In-process coverage proves the gate is wired into `handle_tool_call`. This
    proves the credential arrives narrowed after a real HTTP hop, through the
    identity middleware and whatever sits in front of it — the two places a
    scope can be lost between issuing a key and honouring it.
    """
    secret = _issue(e2e_client, issued_keys, name="e2e-mcp-read", scope="read")

    async with _mcp_session(e2e_base_url, secret) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            # Reading is what the key is for, so this must work — a key that
            # refused everything would satisfy the refusal below on its own.
            listed = await session.call_tool("list_agents", {})
            assert listed.is_error is False
            agents = (listed.structured_content or {}).get("agents") or []
            matches = [item for item in agents if item.get("agent_id") == matrix_agent_id()]
            assert len(matches) == 1, (
                "the read-scoped MCP key did not see this matrix Agent exactly once: "
                f"expected={matrix_agent_id()!r} agents={agents}"
            )
            agent_id = matches[0]["agent_id"]

            refused = await session.call_tool(
                "create_conversation", {"agent_id": agent_id}
            )
            assert refused.is_error is True, refused.structured_content
            error = (refused.structured_content or {}).get("error") or {}
            assert error.get("code") == "MCP_TOKEN_SCOPE_INSUFFICIENT", error


async def test_a_revoked_key_stops_working_against_the_deployment(
    e2e_base_url: str,
    e2e_client: httpx.Client,
    issued_keys: list[str],
) -> None:
    """Revocation is only real if the next call over the wire is refused."""
    secret = _issue(e2e_client, issued_keys, name="e2e-mcp-revoked", scope="read")

    listed = data(e2e_client.get("/api/v1/mcp-tokens")).get("tokens") or []
    token_id = next(
        row["token_id"] for row in listed if row.get("name") == "e2e-mcp-revoked"
    )
    # The listing must never carry the secret back — asserted here because this
    # is the one test that reads the list for its own purposes anyway.
    assert all("secret" not in row for row in listed)

    async with _mcp_session(e2e_base_url, secret) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            assert (await session.call_tool("list_agents", {})).is_error is False

    assert e2e_client.delete(f"/api/v1/mcp-tokens/{token_id}").status_code == 204
    issued_keys.remove("e2e-mcp-revoked")

    # A revoked key cannot even open the transport: the refusal lands on the
    # initialize POST, before any tool is named.
    with pytest.raises(Exception):
        async with _mcp_session(e2e_base_url, secret) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
