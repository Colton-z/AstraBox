"""Authored Agent instructions reach both cold and prepared conversations."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    current_profile,
    data,
    download,
    environment_with_tenancy,
    get_admin_session_detail,
    poll_until_agent_ready,
    release_agent,
    release_session,
    stream_turn,
    wait_until_settled,
)
from tests.e2e.test_prepared_runtime_activation import _wait_prepared

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("tenancy", ["conversation", "agent"])
@pytest.mark.parametrize("prepared", [False, True], ids=["cold", "prepared"])
def test_authored_instructions_reach_the_first_reply(
    e2e_client: httpx.Client, live_test_deadline: float,
    tenancy: str, prepared: bool, request: pytest.FixtureRequest,
) -> None:
    profile = current_profile()
    canonical = data(e2e_client.get(f"/api/v1/agents/{profile['agent_id']}"))
    token = "configured_" + uuid.uuid4().hex
    instructions = (
        "When asked for your configured confirmation token, reply with exactly "
        f"{token} and nothing else. Do not call tools for that request.\n"
    )
    if prepared:
        # Cross command chunks with Unicode and shell-looking text, all inert.
        instructions += (
            "\nThe following is literal reference data, not commands to execute:\n```text\n"
            + "中文 'quoted' $HOME $(unchanged) `literal`\nPY\n" * 300
            + "```\n"
        )
    agent = data(e2e_client.post("/api/v1/agents", json={
        "name": f"Instruction installation {uuid.uuid4().hex[:8]}",
        "model": profile["model"],
        "environment_name": environment_with_tenancy(e2e_client, tenancy),
        "prewarm_enabled": prepared,
        "engine_options": canonical.get("engine_options") or {},
        "system": instructions,
    }))
    agent_id = agent["agent_id"]
    release_agent(agent_id)
    waiting = _wait_prepared(e2e_client, agent_id, live_test_deadline) if prepared else None
    started = data(e2e_client.post(f"/api/v1/agents/{agent_id}/conversations", json={}))
    sid = started["session_id"]
    release_session(sid)
    poll_until_agent_ready(e2e_client, sid)
    session = get_admin_session_detail(e2e_client, sid)
    assert session["engine_kind"] == profile["engine_kind"]
    identity = session.get("runtime_identity") or {}
    assert identity.get("sandbox_tenancy") == tenancy, identity
    if waiting is not None:
        assert session["sandbox_id"] == waiting["sandbox_id"], (
            "the conversation must claim the prepared box to prove its instruction path"
        )
    if profile["engine_kind"] in {"pi", "deepseek_harness"}:
        # Both suppliers load AGENTS.md from the conversation workspace.
        # Reading it through Files proves the authored bytes, not an API echo.
        response = download(e2e_client, sid, "AGENTS.md")
        assert response.status_code == 200, response.text[:500]
        assert response.content == (instructions.strip() + "\n").encode("utf-8")

    result = stream_turn(
        e2e_client, sid,
        content="What is your configured confirmation token? Reply with only that token, without using tools.",
    )
    assert result.error is None, result.error
    assert result.text.strip() == token, result.text
    assert result.tool_names == [], result.tool_names
    settled = wait_until_settled(e2e_client, sid)
    assert settled["last_turn_status"] == "COMPLETED", settled
    request.node.user_properties.append(("instruction_installation", json.dumps({
        "engine_kind": profile["engine_kind"], "tenancy": tenancy,
        "prepared": prepared, "session_id": sid,
        "sandbox_id": session["sandbox_id"],
        "instruction_bytes": len((instructions.strip() + "\n").encode("utf-8")),
    })))
