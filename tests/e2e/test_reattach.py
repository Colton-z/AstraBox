"""A response in progress survives a restart of the platform.

The deployed AstraBox server is restarted while a response runs a real command
in its box. The box and the runtime are not the platform's to restart: they
keep working, and the restarted platform's session reader takes over from the
position the previous one recorded. What must hold:

* the API serves again without waiting for spare Agent capacity;
* the reader lease moves to the new process (a new owner and generation);
* the response then runs to its normal end, exactly once — the command ran
  once, and every part the console received across the restart is the one
  part the platform recorded, with nothing missing or repeated;
* history shows the message once and its one answer, as streamed;
* the conversation answers its next message in the same box.

This test restarts the deployment server selected by the shared
service-container handle. Records stay in the deployment's PostgreSQL database
and the Agent workload stays in its real OpenSandbox runtime.
"""

from __future__ import annotations

import shlex
import subprocess
import uuid

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    answer_in_history,
    assert_database_is_the_live_stack,
    create_session,
    data,
    follow_reply,
    get_session,
    history,
    message_text,
    permission_mode,
    poll_until_agent_ready,
    reader_lease,
    recorded_output,
    recorded_response,
    release_session,
    send_and_follow,
    tool_name,
    user_messages,
    wait_for_file,
    wait_until_settled,
    workspace_path,
)
from tests.e2e._service_containers import (
    SERVER_CONTAINER_HANDLE,
    require_service_container,
)
from tests.e2e.conftest import _wait_health

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.backend_restart,
    pytest.mark.xdist_group("backend-restart"),
]


def _restart_server_container(container: str) -> None:
    result = subprocess.run(
        ["docker", "restart", "--time", "30", container],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"docker restart {container} failed ({result.returncode}): "
            f"{result.stderr.strip()}"
        )


def test_a_response_in_progress_completes_once_across_a_platform_restart(
    e2e_client: httpx.Client,
    e2e_base_url: str,
    e2e_auth_headers: dict[str, str],
) -> None:
    server = require_service_container(SERVER_CONTAINER_HANDLE)
    created = create_session(e2e_client, permission_mode=permission_mode("unattended"))
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id: {created}"
    try:
        poll_until_agent_ready(e2e_client, sid)
        assert_database_is_the_live_stack(sid)
        sandbox_id = str(get_session(e2e_client, sid).get("sandbox_id") or "")
        assert sandbox_id, "session has no sandbox_id after READY"

        gate = f"restart_{uuid.uuid4().hex[:12]}"
        ran_path = workspace_path(e2e_client, sid, f"{gate}.ran")
        started_path = workspace_path(e2e_client, sid, f"{gate}.started")
        release_name = f"{gate}.release"
        release_path = workspace_path(e2e_client, sid, release_name)
        command = "sh -c " + shlex.quote(
            f"echo ran >> {shlex.quote(ran_path)}; "
            f"printf started > {shlex.quote(started_path)}; "
            f"while [ ! -e {shlex.quote(release_path)} ]; do sleep 0.1; done; "
            "echo released"
        )
        running = send_and_follow(
            e2e_client,
            sid,
            f"Use the {tool_name('command')} tool to run exactly this one command in the "
            "foreground and wait for it to finish; I will create the release file when it is time. "
            "Do not create the release file yourself or run "
            "any other command. When the command has finished, reply with exactly DONE: "
            f"{command}",
            until="output",
        )
        assert wait_for_file(e2e_client, sid, started_path, timeout=30.0) == b"started", (
            "the command never started, so no response was in progress across the restart"
        )
        assert not running.ended, f"the response ended before the restart: {running.response}"
        before = reader_lease(sid)
        assert before is not None and before["live"], (
            f"no live reader held the session while its response ran: {before}"
        )

        _restart_server_container(server)
        _wait_health(e2e_base_url)

        # Connections pooled before the restart are dead; talk to the new process afresh.
        with httpx.Client(base_url=e2e_base_url, headers=e2e_auth_headers, timeout=30.0) as client:
            agents = client.get("/api/v1/agents")
            assert agents.status_code == 200, (
                "the Agent catalog must be available after restart while spare "
                f"runtime capacity is being prepared: HTTP {agents.status_code}"
            )
            during = get_session(client, sid)
            assert during.get("sandbox_id") == sandbox_id, (
                f"the restart moved the conversation off its box: {during}"
            )
            assert running.response_id in [r["response_id"] for r in during["current_responses"]], (
                f"the restarted platform lost the response in progress: {during}"
            )
            released = data(
                client.post(
                    f"/api/v1/sessions/{sid}/files/upload",
                    data={"path": ""},
                    files={"files": (release_name, b"release", "text/plain")},
                )
            )
            assert released.get("uploaded_count") == 1, released

            # The console reconnects from the last position it was given.
            follow_reply(client, running, until="end")
            assert running.error is None and running.finish_reason == "stop", (
                f"the response did not run to its normal end across the restart: "
                f"finish={running.finish_reason!r} error={running.error!r} outcome={running.outcome!r}"
            )
            assert "DONE" in running.text.upper(), (
                f"the response did not finish its answer: {running.text!r}"
            )

            after = reader_lease(sid)
            assert after is not None and after["owner"] != before["owner"], (
                f"no reader of the restarted platform took the session over: {before} -> {after}"
            )
            assert after["generation"] > before["generation"], (before, after)

            settled = wait_until_settled(client, sid)
            assert settled["last_response"]["response_id"] == running.response_id, settled
            assert settled["last_response"]["outcome"] == "completed", settled
            record = recorded_response(sid, str(running.response_id))
            assert (record["status"], record["outcome"]) == ("ended", "completed"), record

            # Exactly once. The command's own count is the independent fact; the
            # durable parts the console received across both connections must be the
            # parts the platform recorded for the response, in the same order.
            ran = wait_for_file(client, sid, ran_path, timeout=10.0)
            assert ran == b"ran\n", f"the command ran other than once: {ran!r}"
            recorded = recorded_output(sid, str(running.response_id))
            # Transient notifications update session state outside response content.
            streamed = [
                part
                for part in running.response.parts
                if part.get("transient") is not True
            ]
            assert [p.get("type") for p in streamed] == [p.get("type") for p in recorded], (
                "the console's parts across the restart differ from the recorded ones: "
                f"streamed={[p.get('type') for p in streamed]} "
                f"recorded={[p.get('type') for p in recorded]}"
            )
            assert running.stream.connections >= 2, running.stream.summary()

            # History shows the message once and its one answer, as it was streamed.
            messages = history(client, sid)
            assert [m["id"] for m in user_messages(messages)] == [running.client_message_id], (
                [m["id"] for m in user_messages(messages)]
            )
            answer = answer_in_history(messages, running.input_id)
            assert answer["id"] == running.response_id
            assert answer["metadata"].get("outcome") == "completed", answer["metadata"]
            assert message_text(answer) == running.text, (
                f"history's answer differs from what was streamed: {message_text(answer)!r} "
                f"vs {running.text!r}"
            )

            # The conversation goes on in the same box.
            following = send_and_follow(
                client,
                sid,
                "Reply with only the number 2 and nothing else. Do not use any tool.",
                until="end",
            )
            assert following.error is None and "2" in following.text, (
                f"the next message was not answered after the restart: {following.response}"
            )
            assert wait_until_settled(client, sid).get("sandbox_id") == sandbox_id
    finally:
        release_session(sid)
