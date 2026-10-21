"""An Agent's Artifacts switch decides whether its conversations reach the artifacts tools.

Every engine's ``artifacts`` server terminates on one platform route, addressed
by the Session and authorized by a capability only the server can mint. Asking
that route for its tools is the observation: it answers what any model in the
conversation could do with them, independent of what a model chooses to say.
"""

from __future__ import annotations

import subprocess
import time

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    create_agent_variant_session,
    data,
    poll_until_agent_ready,
    release_session,
)
from tests.e2e._service_containers import SERVER_CONTAINER_HANDLE, require_service_container

pytestmark = pytest.mark.e2e

_MINT_CAPABILITY = (
    "import sys; "
    "from astrabox.core.service.orchestrator.transcript_capability import "
    "mint_platform_mcp_capability; "
    "print(mint_platform_mcp_capability(sys.argv[1], 'artifacts'))"
)


def _list_artifact_tools(base_url: str, session_id: str) -> httpx.Response:
    """Ask a Session's artifacts route for its tools, as the box's MCP client does.

    The capability is minted in the server container for exactly this Session
    and server name, so a refusal below is the binding's answer and not a
    forged or mistyped address.
    """

    minted = subprocess.run(
        ["docker", "exec", require_service_container(SERVER_CONTAINER_HANDLE),
         "python", "-c", _MINT_CAPABILITY, session_id],
        capture_output=True, text=True, check=True, timeout=30,
    )
    capability = minted.stdout.strip()
    assert len(capability) == 64, minted
    return httpx.post(
        f"{base_url}/api/v1/platform-mcp/{session_id}/artifacts/{capability}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        timeout=30.0,
    )


def _assert_refused(response: httpx.Response, session_id: str) -> None:
    # The route's own refusal for a binding without the server. A capability
    # that failed verification would answer 404 with a bare JSON-RPC error and
    # no envelope code, which would make this pass for the wrong reason.
    assert response.status_code == 404, (session_id, response.text[:400])
    assert response.json().get("code") == "NOT_FOUND", (session_id, response.text[:400])


def test_turning_artifacts_off_takes_the_tools_from_every_conversation_of_the_agent(
    e2e_client: httpx.Client, e2e_base_url: str, live_test_deadline: float,
) -> None:
    """The owner turns Artifacts off on an Agent that had them by default.

    The conversation started before the change must lose the tools at once,
    and the next conversation must start without them and still reach READY.
    AstraBox hands Codex its platform servers as ``required`` and DeepSeek
    Harness its platform servers with ``failOnStartupError``, so on those
    engines a server still handed over but refused fails startup instead.
    """

    def remaining() -> float:
        return max(1.0, live_test_deadline - time.monotonic())

    # Created through the matrix Agent's own payload, which names no switch.
    before = create_agent_variant_session(e2e_client, name_suffix="artifacts switch", engine_options={})
    agent_id = str(before.get("agent_id") or "")
    before_sid = str(before.get("session_id") or "")
    assert agent_id and before_sid, before
    agent = data(e2e_client.get(f"/api/v1/agents/{agent_id}"))
    assert agent["artifacts_enabled"] is True, "an Agent created without the switch has Artifacts"

    poll_until_agent_ready(e2e_client, before_sid, timeout=remaining())
    listed = _list_artifact_tools(e2e_base_url, before_sid)
    assert listed.status_code == 200, listed.text[:400]
    names = {tool["name"] for tool in listed.json()["result"]["tools"]}
    assert "create_artifact" in names, names

    # The authoring write requires the Agent's model and RuntimeProfile; sending
    # them unchanged leaves the switch as the only change.
    updated = data(e2e_client.put(
        f"/api/v1/agents/{agent_id}",
        json={
            "model": agent["model"],
            "runtime_profile_name": agent["runtime_profile_name"],
            "artifacts_enabled": False,
            "version": agent["version"],
        },
    ))
    assert updated["artifacts_enabled"] is False, updated
    assert updated["version"] == agent["version"] + 1, "the switch is a runtime setting"
    _assert_refused(_list_artifact_tools(e2e_base_url, before_sid), before_sid)

    started = data(e2e_client.post(f"/api/v1/agents/{agent_id}/conversations", json={}))
    after_sid = str(started.get("session_id") or "")
    assert after_sid, started
    release_session(after_sid)
    poll_until_agent_ready(e2e_client, after_sid, timeout=remaining())
    _assert_refused(_list_artifact_tools(e2e_base_url, after_sid), after_sid)
