"""An MCP server added to a running Assistant reaches its next conversation.

An Assistant's box is created once and outlives many edits. Two outcomes of an
edit that adds an MCP server are proven on a real deployment:

* On an Environment that admits MCP servers, the next conversation starts and
  answers, and the box now allows the server's host. The box's egress policy
  was fixed when the box was created, so without widening it the conversation
  failed at its first message: the egress sidecar refuses a credential binding
  for a host its policy does not allow.
* On an Environment that forbids MCP servers, the next conversation cannot be
  prepared, and it says so: it ends with the cause on it. Before, it showed
  READY with no error and the first message was refused with a 409 carrying
  the same cause.

Each test runs on a sibling of the lane's Assistant Environment that differs
only in whether it admits MCP servers, so the outcome does not depend on how
the lane Environment happens to be configured.

    E2E_LANE=assistant \\
      ASTRABOX_E2E_ASSISTANT_ENVIRONMENT=<assistant environment> \\
      ASTRABOX_E2E_ASSISTANT_MODEL=<model> ./scripts/e2e_live.sh
"""

from __future__ import annotations

import os
import time
import uuid
from urllib.parse import urlsplit

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    data,
    get_session,
    poll_until_ready,
    release_assistant,
    release_session,
    stream_turn,
)

pytestmark = [pytest.mark.e2e, pytest.mark.assistant_live]

# A public Streamable HTTP MCP server. The forbidding test never contacts it;
# the admitting test only needs its host to become reachable.
_MCP_URL = "https://mcp.deepwiki.com/mcp"
# The fields of the lane Environment that decide which program and image its
# boxes run. The sibling copies these and sets its own networking.
_RUNTIME_FIELDS = ("engine_kind", "idle_action", "runtime_template_name")


def _sibling_environment(e2e_client: httpx.Client, *, admits_mcp: bool) -> str:
    lane = os.getenv("ASTRABOX_E2E_ASSISTANT_ENVIRONMENT", "").strip()
    assert lane, "the Assistant lane requires ASTRABOX_E2E_ASSISTANT_ENVIRONMENT"
    matches = [
        item
        for item in data(e2e_client.get("/api/v1/admin/environments"))
        if item.get("name") == lane
    ]
    assert len(matches) == 1, f"expected one Environment {lane!r}: {matches}"
    name = f"{lane}-{'mcp' if admits_mcp else 'no-mcp'}"
    body = {key: matches[0][key] for key in _RUNTIME_FIELDS if key in matches[0]}
    body.update(
        enabled=True,
        networking={
            "type": "limited",
            "allowed_hosts": [],
            "allow_mcp_servers": admits_mcp,
        },
    )
    data(e2e_client.put(f"/api/v1/admin/environments/{name}", json=body))
    return name


def _assistant(e2e_client: httpx.Client, environment: str) -> str:
    model = os.getenv("ASTRABOX_E2E_ASSISTANT_MODEL", "").strip()
    assert model, "the Assistant lane requires ASTRABOX_E2E_ASSISTANT_MODEL"
    created = data(
        e2e_client.post(
            "/api/v1/assistants",
            json={
                "display_name": f"e2e-mcp-change-{uuid.uuid4().hex[:6]}",
                "environment_name": environment,
                "model_config_override": {"model_name": model},
            },
        )
    )
    assistant_id = str(created.get("assistant_id") or "")
    assert assistant_id, f"Assistant creation returned no id: {created}"
    release_assistant(assistant_id)
    return assistant_id


def _wake(e2e_client: httpx.Client, assistant_id: str, *, deadline: float) -> str:
    last: dict = {}
    while time.monotonic() < deadline:
        last = data(
            e2e_client.post(
                f"/api/v1/assistants/{assistant_id}/workspace/wake", timeout=120.0
            )
        )
        if last.get("state") == "READY" and last.get("current_sandbox_id"):
            return str(last["current_sandbox_id"])
        time.sleep(3.0)
    pytest.fail(f"assistant {assistant_id} workspace did not become READY: {last}")


def _add_mcp_server(e2e_client: httpx.Client, assistant_id: str) -> None:
    data(
        e2e_client.patch(
            f"/api/v1/assistants/{assistant_id}",
            json={"mcp_config_override": {"wiki": {"type": "http", "url": _MCP_URL}}},
        )
    )


def _new_conversation(e2e_client: httpx.Client, assistant_id: str) -> str:
    conversation = data(
        e2e_client.post(f"/api/v1/assistants/{assistant_id}/conversations", json={})
    )
    session_id = str(conversation.get("session_id") or "")
    assert session_id, f"Assistant conversation returned no session: {conversation}"
    release_session(session_id)
    return session_id


def _allowed_hosts(e2e_client: httpx.Client, sandbox_id: str) -> set[str]:
    posture = data(e2e_client.get(f"/api/v1/admin/sandboxes/{sandbox_id}/security"))
    assert posture.get("available") is True, f"the box reported no egress policy: {posture}"
    return {
        str(rule.get("target"))
        for rule in posture.get("egress_rules") or []
        if rule.get("action") == "allow"
    }


def test_an_mcp_server_added_to_a_running_assistant_is_reachable(
    e2e_client: httpx.Client, live_test_deadline: float
) -> None:
    environment = _sibling_environment(e2e_client, admits_mcp=True)
    assistant_id = _assistant(e2e_client, environment)
    sandbox_id = _wake(e2e_client, assistant_id, deadline=live_test_deadline)
    host = str(urlsplit(_MCP_URL).hostname)
    assert host not in _allowed_hosts(e2e_client, sandbox_id), (
        "the box admits the MCP host before any server named it; the test proves nothing"
    )

    _add_mcp_server(e2e_client, assistant_id)
    session_id = _new_conversation(e2e_client, assistant_id)
    poll_until_ready(e2e_client, session_id)

    assert host in _allowed_hosts(e2e_client, sandbox_id), (
        "the Assistant's box does not admit the MCP server it was given"
    )
    answer = stream_turn(e2e_client, session_id, content="Reply with exactly: READY")
    assert answer.error is None, f"the first message of the new conversation failed: {answer.error}"
    assert answer.finish_reason == "stop", answer


def test_a_conversation_whose_setup_fails_says_why(
    e2e_client: httpx.Client, live_test_deadline: float
) -> None:
    environment = _sibling_environment(e2e_client, admits_mcp=False)
    assistant_id = _assistant(e2e_client, environment)
    _wake(e2e_client, assistant_id, deadline=live_test_deadline)

    _add_mcp_server(e2e_client, assistant_id)
    session_id = _new_conversation(e2e_client, assistant_id)

    session = get_session(e2e_client, session_id)
    while str(session.get("state") or "") == "CREATING":
        assert time.monotonic() < live_test_deadline, "the conversation never left CREATING"
        time.sleep(0.5)
        session = get_session(e2e_client, session_id)
    assert session.get("state") == "TERMINATED", (
        f"a conversation whose setup failed shows {session.get('state')!r}: {session}"
    )
    # The cause as the refusal states it: the Environment's switch and the host.
    cause = str(session.get("last_error") or "")
    assert "allow_mcp_servers=false" in cause and str(urlsplit(_MCP_URL).hostname) in cause, (
        f"the conversation does not say why it ended: {cause!r}"
    )

    # The workspace is ready; that does not change why this conversation ended.
    time.sleep(3.0)
    again = get_session(e2e_client, session_id)
    assert again.get("state") == "TERMINATED", again
    assert again.get("last_error") == session.get("last_error"), again
