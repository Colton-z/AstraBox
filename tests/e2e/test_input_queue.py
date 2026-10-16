"""A message sent while a response runs is accepted and answered after it.

A conversation takes input at any time: a send while the runtime is working is
accepted at once, held by the platform and handed to the runtime, which answers
it after the response it is producing. What must never happen is a second
response interleaving the running one, or the queued message being answered
twice, or not at all.

The first response runs a real command that waits on a release file, so it is
demonstrably running when the second message is sent, and only the test can
let it finish.

    .venv/bin/python -m pytest tests/e2e/test_input_queue.py -m e2e -s
"""

from __future__ import annotations

import shlex
import uuid
from datetime import datetime

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    Reply,
    answer_in_history,
    create_session,
    data,
    follow_reply,
    get_session,
    history,
    message_text,
    permission_mode,
    poll_until_agent_ready,
    psql_rows,
    release_session,
    send_and_follow,
    send_input,
    sql_literal,
    tool_name,
    user_messages,
    wait_for_file,
    wait_until_settled,
    workspace_path,
)

pytestmark = pytest.mark.e2e


def test_a_message_sent_while_a_response_runs_is_answered_after_it(
    e2e_client: httpx.Client,
) -> None:
    # Unattended, because the long response this needs runs a COMMAND, and a
    # mode that gates commands pauses it on an approval instead — the contract
    # here is the order two messages are answered in, not the permission matrix.
    created = create_session(e2e_client, permission_mode=permission_mode("unattended"))
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id: {created}"
    first_marker = f"FIRST_DONE_{uuid.uuid4().hex[:8]}"
    second_marker = f"SECOND_DONE_{uuid.uuid4().hex[:8]}"
    try:
        poll_until_agent_ready(e2e_client, sid)
        gate = f"queue_{uuid.uuid4().hex[:12]}"
        started_path = workspace_path(e2e_client, sid, f"{gate}.started")
        release_name = f"{gate}.release"
        release_path = workspace_path(e2e_client, sid, release_name)
        command = "sh -c " + shlex.quote(
            f"printf started > {shlex.quote(started_path)}; "
            f"while [ ! -e {shlex.quote(release_path)} ]; do sleep 0.1; done"
        )
        first = send_and_follow(
            e2e_client,
            sid,
            f"Use the {tool_name('command')} tool to run exactly this one command in the "
            "foreground and wait for it to finish; I will create the release file when it is time. "
            "Do not create the release file yourself or run "
            f"any other command. When it has finished, reply with only {first_marker}: "
            f"{command}",
            until="output",
        )
        assert wait_for_file(e2e_client, sid, started_path, timeout=30.0) == b"started", (
            "the first response's command never started, so nothing was running"
        )
        assert not first.ended, f"the first response ended before the second send: {first.response}"

        # The send is accepted while the first response is demonstrably running.
        receipt = send_input(e2e_client, sid, f"Reply with only {second_marker}.")
        busy = get_session(e2e_client, sid)
        assert busy.get("state") == "PROCESSING", busy
        assert first.response_id in [r["response_id"] for r in busy["current_responses"]], busy
        second = Reply(
            session_id=sid,
            input_id=str(receipt["input_id"]),
            client_message_id=str(receipt["client_message_id"]),
            stream=first.stream,
        )

        released = data(
            e2e_client.post(
                f"/api/v1/sessions/{sid}/files/upload",
                data={"path": ""},
                files={"files": (release_name, b"release", "text/plain")},
            )
        )
        assert released.get("uploaded_count") == 1, released

        follow_reply(e2e_client, first, until="end")
        assert first.error is None and first_marker in first.text, (
            f"the first response did not finish its own answer: {first.error!r} {first.text!r}"
        )
        follow_reply(e2e_client, second, until="end")
        assert second.error is None and second_marker in second.text, (
            f"the queued message was not answered: {second.error!r} {second.text!r}"
        )
        assert second.response_id != first.response_id
        assert list(first.stream.responses).index(first.response_id) < list(
            first.stream.responses
        ).index(second.response_id), "the queued message was answered before the running one"

        final = wait_until_settled(e2e_client, sid)
        assert final["last_response"]["response_id"] == second.response_id, final
        assert final["last_response"]["outcome"] == "completed", final

        # The platform's own record: the two responses never interleave. Every
        # part of the first precedes every part of the second, and the second
        # started only after the first ended.
        log = psql_rows(
            f"SELECT seq, response_id FROM session_output WHERE session_id = {sql_literal(sid)} "
            f"AND response_id IN ({sql_literal(str(first.response_id))}, "
            f"{sql_literal(str(second.response_id))})",
            order_by="seq",
        )
        owners = [row["response_id"] for row in log]
        assert owners == sorted(owners, key=lambda owner: owner != first.response_id), (
            f"the two responses interleaved in the session's output: {owners}"
        )
        timing = psql_rows(
            "SELECT response_id, started_at, ended_at FROM session_responses "
            f"WHERE session_id = {sql_literal(sid)} AND response_id IN "
            f"({sql_literal(str(first.response_id))}, {sql_literal(str(second.response_id))})"
        )
        times = {row["response_id"]: row for row in timing}
        first_ended = datetime.fromisoformat(times[first.response_id]["ended_at"])
        second_started = datetime.fromisoformat(times[second.response_id]["started_at"])
        assert first_ended <= second_started, (
            f"the second response started before the first ended: {timing}"
        )

        # History: each message once, in the order sent, each with its one answer.
        messages = history(e2e_client, sid)
        assert [m["id"] for m in user_messages(messages)] == [
            first.client_message_id,
            second.client_message_id,
        ], [m["id"] for m in user_messages(messages)]
        assert second_marker in message_text(answer_in_history(messages, second.input_id))
        assert first_marker in message_text(answer_in_history(messages, first.input_id))
    finally:
        # The outcome-aware reaper deletes this on pass and keeps the exact
        # session and box on failure.
        release_session(sid)
