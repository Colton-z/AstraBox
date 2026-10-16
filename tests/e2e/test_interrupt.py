"""Interrupt e2e — a stop ends the running response, and the conversation goes on.

``POST /api/v1/sessions/{id}/interrupt`` asks the runtime to stop. The proofs:

* **running command** — a real command writes its started marker and waits on
  a release file the test never creates, so a natural finish cannot stand in
  for the stop. The response ends ``cancelled`` (a user stop, not a failure),
  the conversation settles, and the next message is answered.
* **held tool permission** — a response paused on a tool approval is
  interrupted. The held tool never runs (the uniquely named file is never
  written), the interaction stops waiting on the owner, the response ends
  ``cancelled``, and the next message is answered.

The outcome is read where the console reads it — the session's
``last_response`` and the answer's history metadata — and checked against
the platform's own record of the response.

Run it explicitly (deselected in the default unit run):

    .venv/bin/python -m pytest tests/e2e/test_interrupt.py -m e2e -s
"""

from __future__ import annotations

import shlex
import time
import uuid

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    Reply,
    answer_in_history,
    approval_presentation,
    assert_kubernetes_runtime_image,
    assert_permission_mode_unavailable,
    contract_supported,
    create_session,
    data,
    file_names,
    follow_reply,
    get_session,
    history,
    interrupt,
    list_files,
    pending_interaction,
    permission_mode,
    poll_until_agent_ready,
    poll_until_ready,
    psql_rows,
    recorded_response,
    release_assistant,
    release_session,
    send_and_follow,
    sql_literal,
    tool_name,
    user_messages,
    wait_for_file,
    wait_for_pending_interaction,
    wait_until_settled,
    workspace_path,
)
from tests.e2e.test_conversation_end import _assistant_runtime, _wake_assistant

pytestmark = pytest.mark.e2e


def _assert_next_message_is_answered(e2e_client: httpx.Client, sid: str) -> Reply:
    """Prove the conversation goes on: a short deterministic message is answered with ``2``."""
    reply = send_and_follow(
        e2e_client,
        sid,
        "Reply with only the number 2 and nothing else. Do not use any tool.",
        until="end",
    )
    assert reply.error is None, f"the next message's response failed: {reply.error}"
    assert reply.finish_reason == "stop", (
        f"the next message's response did not end normally: {reply.finish_reason!r}"
    )
    assert "2" in reply.text, f"the next message's reply did not contain '2': {reply.text!r}"
    settled = wait_until_settled(e2e_client, sid)
    assert settled["last_response"]["response_id"] == reply.response_id, settled
    assert settled["last_response"]["outcome"] == "completed", settled
    return reply


def _assert_stopped(e2e_client: httpx.Client, stopped: Reply) -> None:
    """The stopped response ended as a user stop everywhere the platform tells it."""
    sid = stopped.session_id
    assert stopped.ended, f"the stopped response never ended on the stream: {stopped.response}"
    settled = wait_until_settled(e2e_client, sid)
    last = settled["last_response"]
    assert last["response_id"] == stopped.response_id, settled
    assert last["outcome"] == "cancelled" and not last.get("error"), (
        f"a user stop was not recorded as a cancelled response: {last}"
    )
    record = recorded_response(sid, str(stopped.response_id))
    assert record["status"] == "ended" and record["outcome"] == "cancelled", record
    answer = answer_in_history(history(e2e_client, sid), stopped.input_id)
    assert answer["metadata"].get("outcome") == "cancelled", answer["metadata"]


def _interrupt_running_command(
    e2e_client: httpx.Client,
    sid: str,
    *,
    command_tool: str,
    workspace_root: str | None = None,
) -> None:
    marker = f"interrupt_{uuid.uuid4().hex}"

    def path_for(suffix: str) -> str:
        if workspace_root is not None:
            assert workspace_root == "/workspace", workspace_root
            return f"{workspace_root}/{marker}.{suffix}"
        return workspace_path(e2e_client, sid, f"{marker}.{suffix}")

    started_path = path_for("started")
    release_path = path_for("release")
    exited_path = path_for("exited")
    on_exit = f"printf exited > {shlex.quote(exited_path)}"
    command = "sh -c " + shlex.quote(
        f"trap {shlex.quote(on_exit)} EXIT; "
        f"printf started > {shlex.quote(started_path)}; "
        f"while [ ! -e {shlex.quote(release_path)} ]; do sleep 0.1; done"
    )
    running = send_and_follow(
        e2e_client,
        sid,
        f"Use the {command_tool} tool to run exactly this one command in the "
        "foreground and wait for it to finish. Do not create the release file or "
        f"run any other command. I will stop the running command: {command}",
        until="output",
    )
    assert running.saw_ui_header, "the session stream is not an AI SDK UI message stream"
    assert running.response is not None and running.response.produced_output, (
        "the response produced no model or tool output before it could be interrupted"
    )

    # Model output alone does not prove the tool started. The command owns
    # both markers; the release file is never created, so only the stop can end it.
    started = wait_for_file(e2e_client, sid, started_path, timeout=30.0)
    assert started == b"started", f"the real command never confirmed execution: {started!r}"
    names = file_names(e2e_client, sid)
    assert f"{marker}.release" not in names, "the command gate was released before the interrupt"
    assert f"{marker}.exited" not in names, "the command exited before the interrupt"
    in_flight = get_session(e2e_client, sid)
    assert in_flight.get("state") == "PROCESSING" and not in_flight.get("pending_interaction"), (
        f"the command was not running at the interrupt boundary: {in_flight}"
    )
    assert running.response_id in [r["response_id"] for r in in_flight["current_responses"]], (
        f"the detail does not show the running response {running.response_id}: {in_flight}"
    )

    interrupt(e2e_client, sid)
    follow_reply(e2e_client, running, until="end")
    _assert_stopped(e2e_client, running)
    assert f"{marker}.release" not in file_names(e2e_client, sid), (
        "something released the command gate; a normal finish could have ended the response"
    )

    following = _assert_next_message_is_answered(e2e_client, sid)
    assert following.response_id != running.response_id
    messages = history(e2e_client, sid)
    assert [m["id"] for m in user_messages(messages)] == [
        running.client_message_id,
        following.client_message_id,
    ], [m["id"] for m in user_messages(messages)]


def test_interrupt_stops_a_running_command_and_the_next_message_is_answered(
    e2e_client: httpx.Client,
) -> None:
    """Interrupt an executing command: the response ends cancelled and the next message is answered."""
    created = create_session(e2e_client, permission_mode=permission_mode("unattended"))
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id in create response: {created}"
    try:
        poll_until_agent_ready(e2e_client, sid)
        _interrupt_running_command(e2e_client, sid, command_tool=tool_name("command"))
    finally:
        release_session(sid)


@pytest.mark.assistant_live
def test_assistant_interrupt_stops_a_running_command_and_the_next_message_is_answered(
    e2e_client: httpx.Client,
    live_test_deadline: float,
) -> None:
    """Hermes ends a stopped response cancelled in its resident workspace and answers the next message."""
    assistant_id = ""
    sid = ""
    try:
        environment, image, digest, model, kubeconfig, namespace = _assistant_runtime(e2e_client)
        assistant = data(
            e2e_client.post(
                "/api/v1/assistants",
                json={
                    "display_name": f"__e2e_interrupt_{uuid.uuid4().hex[:8]}",
                    "environment_name": environment,
                    "model_config_override": {"model_name": model},
                },
            )
        )
        assistant_id = str(assistant.get("assistant_id") or "")
        assert assistant_id, assistant
        workspace = _wake_assistant(
            e2e_client,
            assistant_id,
            timeout=max(0.1, live_test_deadline - time.monotonic()),
        )
        assert_kubernetes_runtime_image(
            sandbox_id=str(workspace.get("current_sandbox_id") or ""),
            expected_image=image,
            expected_digest=digest,
            kubeconfig=kubeconfig,
            namespace=namespace,
        )
        created = data(
            e2e_client.post(
                f"/api/v1/assistants/{assistant_id}/conversations",
                json={},
            )
        )
        sid = str(created.get("session_id") or "")
        assert sid, created
        poll_until_ready(e2e_client, sid, expected_image=image)
        # Shared workspace identity belongs to the Assistant, not to each
        # new conversation. The Files API resolves that same owner identity.
        workspace_root = str(list_files(e2e_client, sid).get("root_path") or "")
        assert workspace_root == "/workspace", workspace_root
        _interrupt_running_command(
            e2e_client, sid, command_tool="terminal", workspace_root=workspace_root
        )
    finally:
        release_session(sid)
        release_assistant(assistant_id)


def test_interrupt_clears_pending_interaction_and_settles(e2e_client: httpx.Client) -> None:
    """Interrupt a response paused on a tool permission: the tool never runs and the response ends.

    The gated mode holds the write as a ``tool_approval`` interaction and the
    response pauses. Interrupting while it waits ends the response
    ``cancelled``; the interaction stops waiting on the owner without being
    answered, the uniquely named file is never created, and the conversation
    answers its next message.
    """
    created = create_session(e2e_client, permission_mode=permission_mode("gated"))
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id in create response: {created}"
    write_path = workspace_path(e2e_client, sid, f"interrupt_{uuid.uuid4().hex[:8]}.txt")
    write_name = write_path.rsplit("/", 1)[-1]
    prompt = (
        f"Use the {tool_name('write')} tool to create a file at {write_path} containing the "
        "single word MANGO. Do not run any other tool or command."
    )
    try:
        poll_until_agent_ready(e2e_client, sid)

        if not contract_supported("tool_approval"):
            assert_permission_mode_unavailable(e2e_client, sid)
            written = send_and_follow(e2e_client, sid, prompt, until="end")
            assert written.error is None, written.error
            assert not written.interactions
            assert pending_interaction(e2e_client, sid) is None
            assert wait_for_file(e2e_client, sid, write_path, timeout=60.0) is not None
            wait_until_settled(e2e_client, sid)
            _assert_next_message_is_answered(e2e_client, sid)
            return

        paused = send_and_follow(e2e_client, sid, prompt, until="pause")
        assert paused.saw_ui_header, "the session stream is not an AI SDK UI message stream"
        assert paused.paused, (
            f"the response did not pause on the write's approval: finish={paused.finish_reason!r}"
        )
        pending = wait_for_pending_interaction(e2e_client, sid, timeout=15.0)
        assert pending is not None and pending.get("response_id") == paused.response_id, (
            f"no pending interaction on the paused response: {pending}"
        )
        assert str(pending.get("presentation")) == approval_presentation(), (
            f"expected a tool approval interaction: {pending}"
        )
        interaction_id = str(pending["interaction_id"])

        interrupt(e2e_client, sid)
        follow_reply(e2e_client, paused, until="end")
        _assert_stopped(e2e_client, paused)
        assert get_session(e2e_client, sid).get("pending_interaction") is None
        record = psql_rows(
            "SELECT status FROM session_interactions "
            f"WHERE session_id = {sql_literal(sid)} AND interaction_id = {sql_literal(interaction_id)}"
        )
        assert len(record) == 1 and record[0]["status"] not in ("open", "answered"), (
            f"the interrupted interaction is still open or was answered: {record}"
        )

        # Load-bearing: the stopped write had no side effect. The response has
        # ended, so nothing of it can write the file any more.
        assert write_name not in file_names(e2e_client, sid), (
            f"the interrupted (never approved) write still created {write_path}"
        )
        _assert_next_message_is_answered(e2e_client, sid)
    finally:
        release_session(sid)
