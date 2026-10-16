"""An MCP server added to a running Assistant reaches its next conversation.

An Assistant's box is created once and outlives many edits. Two outcomes of an
edit that adds an MCP server are proven on a real deployment:

* On an Environment that admits MCP servers, the next conversation starts and
  answers, and the box now allows the server's host. The box's egress policy
  was fixed when the box was created, so without widening it the conversation
  failed at its first message: the egress sidecar refuses a credential binding
  for a host its policy does not allow.
* On an Environment that forbids MCP servers, the next conversation cannot be
  prepared, and it says so: it ends with the cause on it, rather than showing
  READY and leaving its first message to fail.

Each test runs on a sibling of the lane's Assistant Environment that differs
only in whether it admits MCP servers, so the outcome does not depend on how
the lane Environment happens to be configured.

Removal preserves a real command already running on the original box: the
response running it stays the session's response in progress and ends
``completed`` once the command is released. Hibernate and wake must then
replace the box, drop the old host admission and retain the conversation —
its history unchanged — and the runtime's own context.

    E2E_LANE=assistant \\
      ASTRABOX_E2E_ASSISTANT_ENVIRONMENT=<assistant environment> \\
      ASTRABOX_E2E_ASSISTANT_MODEL=<model> ./scripts/e2e_live.sh
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
import uuid
from fnmatch import fnmatchcase
from urllib.parse import urlsplit

import httpx
import pytest

from tests.e2e._service_containers import SERVER_CONTAINER_HANDLE, require_service_container
from tests.e2e._sandbox_helpers import (
    answer_in_history,
    data,
    file_names,
    follow_reply,
    get_session,
    history,
    list_files,
    poll_until_ready,
    release_assistant,
    release_session,
    send_and_follow,
    user_messages,
    wait_for_file,
    wait_until_settled,
)

pytestmark = [pytest.mark.e2e, pytest.mark.assistant_live]

# A public Streamable HTTP MCP server. The forbidding test never contacts it;
# the admitting test only needs its host to become reachable.
_MCP_URL = "https://mcp.deepwiki.com/mcp"
_MCP_SERVERS = {"wiki": {"type": "http", "url": _MCP_URL}}
# The fields of the lane Environment that decide which program and image its
# boxes run. The sibling copies these and sets its own networking.
_RUNTIME_FIELDS = ("engine_kind", "idle_action", "runtime_template_name")


def _sibling_environment(
    e2e_client: httpx.Client, *, admits_mcp: bool, suffix: str = ""
) -> str:
    lane = os.getenv("ASTRABOX_E2E_ASSISTANT_ENVIRONMENT", "").strip()
    assert lane, "the Assistant lane requires ASTRABOX_E2E_ASSISTANT_ENVIRONMENT"
    matches = [
        item
        for item in data(e2e_client.get("/api/v1/admin/environments"))
        if item.get("name") == lane
    ]
    assert len(matches) == 1, f"expected one Environment {lane!r}: {matches}"
    name = f"{lane}-{'mcp' if admits_mcp else 'no-mcp'}{suffix}"
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


def _assistant(e2e_client: httpx.Client, environment: str, *, with_mcp_server: bool = False) -> str:
    model = os.getenv("ASTRABOX_E2E_ASSISTANT_MODEL", "").strip()
    assert model, "the Assistant lane requires ASTRABOX_E2E_ASSISTANT_MODEL"
    body: dict = {
        "display_name": f"e2e-mcp-change-{uuid.uuid4().hex[:6]}",
        "environment_name": environment,
        "model_config_override": {"model_name": model},
    }
    if with_mcp_server:
        body["mcp_servers"] = _MCP_SERVERS
    created = data(e2e_client.post("/api/v1/assistants", json=body))
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
            json={"mcp_servers": _MCP_SERVERS},
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
    answer = send_and_follow(e2e_client, session_id, "Reply with exactly: READY", until="end")
    assert answer.error is None, f"the first message of the new conversation failed: {answer.error}"
    assert answer.finish_reason == "stop", answer.response
    settled = wait_until_settled(e2e_client, session_id)
    assert settled["last_response"]["response_id"] == answer.response_id, settled
    assert settled["last_response"]["outcome"] == "completed", settled


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


def test_removing_an_mcp_host_keeps_active_work_and_revokes_it_on_idle_restart(
    e2e_client: httpx.Client, live_test_deadline: float
) -> None:
    """A config edit preserves a running command; a fresh idle box drops its host."""
    marker = f"mcp_remove_{uuid.uuid4().hex}"
    environment = _sibling_environment(
        e2e_client, admits_mcp=True, suffix=f"-{marker}"
    )
    # Created with the server: what this test changes is its removal.
    assistant_id = _assistant(e2e_client, environment, with_mcp_server=True)
    old_box = _wake(e2e_client, assistant_id, deadline=live_test_deadline)
    host = str(urlsplit(_MCP_URL).hostname)

    def limited_hosts(sandbox_id: str) -> set[str]:
        posture = data(e2e_client.get(f"/api/v1/admin/sandboxes/{sandbox_id}/security"))
        assert posture.get("available") is True, posture
        assert posture.get("default_action") == "deny", posture
        return {
            str(rule.get("target"))
            for rule in posture.get("egress_rules") or []
            if rule.get("action") == "allow"
        }

    sid = _new_conversation(e2e_client, assistant_id)
    poll_until_ready(e2e_client, sid)
    assert host in limited_hosts(old_box), "the original box never admitted the MCP host"
    root = str(list_files(e2e_client, sid).get("root_path") or "")
    assert root == "/workspace", root
    started_path = f"{root}/{marker}.started"
    release_name = f"{marker}.release"
    release_path = f"{root}/{release_name}"
    exited_name = f"{marker}.exited"
    exited_path = f"{root}/{exited_name}"
    command = "sh -c " + shlex.quote(
        f"printf started > {shlex.quote(started_path)}; "
        f"while [ ! -e {shlex.quote(release_path)} ]; do sleep 0.1; done; "
        f"printf completed > {shlex.quote(exited_path)}; printf {shlex.quote(marker)}"
    )
    prompt = (
        f"Remember the token {marker}. Use the terminal tool to run exactly this "
        "command in the foreground and wait for it to finish. Do not create the "
        "release file or run another command; I will release it. When it finishes, "
        f"reply with the token only: {command}"
    )
    running = send_and_follow(e2e_client, sid, prompt, until="output")
    assert running.saw_ui_header and running.response is not None and running.response.produced_output, (
        f"the response running the command never produced output: {running.stream.summary()}"
    )
    assert wait_for_file(e2e_client, sid, started_path, timeout=30.0) == b"started"
    active = get_session(e2e_client, sid)
    assert active.get("state") == "PROCESSING" and not active.get("pending_interaction"), active
    assert [r["response_id"] for r in active["current_responses"]] == [running.response_id], (
        f"the session does not show the command's response in progress: {active}"
    )
    assert release_name not in file_names(e2e_client, sid)
    assert exited_name not in file_names(e2e_client, sid)

    updated = data(
        e2e_client.patch(
            f"/api/v1/assistants/{assistant_id}", json={"mcp_servers": {}}
        )
    )
    assert updated.get("mcp_servers") == {}, updated
    still_active = get_session(e2e_client, sid)
    assert [r["response_id"] for r in still_active["current_responses"]] == [running.response_id], (
        f"the edit ended or replaced the response in progress: {still_active}"
    )
    assert still_active.get("state") == "PROCESSING", still_active
    assert host in limited_hosts(old_box), "editing one definition revoked a live box's host"
    assert exited_name not in file_names(e2e_client, sid), "the held command was stopped"

    released = data(
        e2e_client.post(
            f"/api/v1/sessions/{sid}/files/upload",
            data={"path": ""}, files={"files": (release_name, b"release", "text/plain")},
        )
    )
    assert released.get("uploaded_count") == 1, released
    follow_reply(e2e_client, running, until="end")
    assert running.error is None and running.finish_reason == "stop", (
        f"the response running the command did not end normally: "
        f"finish={running.finish_reason!r} error={running.error!r} outcome={running.outcome!r}"
    )
    settled = wait_until_settled(e2e_client, sid)
    last = settled["last_response"]
    assert last["response_id"] == running.response_id, settled
    assert last["outcome"] == "completed" and not last.get("error"), settled
    assert wait_for_file(e2e_client, sid, exited_path, timeout=5.0) == b"completed"
    messages = history(e2e_client, sid)
    assert [message.get("role") for message in messages] == ["user", "assistant"], messages
    assert [m["id"] for m in user_messages(messages)] == [running.client_message_id], messages
    answer = answer_in_history(messages, running.input_id)
    assert answer["id"] == running.response_id, answer
    assert answer["metadata"].get("outcome") == "completed", answer["metadata"]

    paused_at = time.time()
    hibernated = data(
        e2e_client.post(f"/api/v1/assistants/{assistant_id}/workspace/hibernate", timeout=120.0)
    )
    assert hibernated.get("released") is True, hibernated
    assert hibernated.get("previous_sandbox_id") == old_box, hibernated
    new_box = _wake(e2e_client, assistant_id, deadline=live_test_deadline)
    # The server lets go of the paused conversation's runtime: its periodic
    # credential refresh (every few seconds, across the wake) never reaches
    # for the released box.
    logs = subprocess.run(
        ["docker", "logs", "--since", str(int(paused_at)), require_service_container(SERVER_CONTAINER_HANDLE)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert logs.returncode == 0, logs.stderr[-400:]
    held = f"assistant credential refresh failed session={sid}"
    assert held not in logs.stdout + logs.stderr, "the server still holds the paused conversation's runtime"
    assert new_box != old_box, "idle restart reused the box with the obsolete host"
    assert not any(fnmatchcase(host, allowed) for allowed in limited_hosts(new_box)), (
        "the replacement box still admits the removed MCP host"
    )
    assert history(e2e_client, sid) == messages, "idle restart changed the saved conversation"
    reply = send_and_follow(
        e2e_client,
        sid,
        "What exact token did I previously ask you to remember? Reply with the token only; no tools.",
        until="end",
    )
    assert reply.error is None and reply.finish_reason == "stop", reply.response
    assert reply.text.strip() == marker, "the replacement lost the original native context"
    final = wait_until_settled(e2e_client, sid)
    assert final["last_response"]["response_id"] == reply.response_id != running.response_id, final
    assert final["last_response"]["outcome"] == "completed", final
    assert not final["last_response"].get("error"), final
