"""A busy Codex input cannot bypass its configured native output schema."""

from __future__ import annotations

import json
import shlex
import time
import uuid

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    Reply,
    create_agent_variant_session,
    data,
    deadline_after,
    engine_kind,
    follow_reply,
    get_session,
    permission_mode,
    poll_until_agent_ready,
    psql_rows,
    release_session,
    send_and_follow,
    send_input,
    sql_literal,
    tool_name,
    transcript_entries,
    wait_for_file,
    wait_until_settled,
    workspace_path,
)

pytestmark = pytest.mark.e2e


def test_a_busy_codex_input_is_refused_instead_of_losing_its_output_schema(
    e2e_client: httpx.Client,
) -> None:
    assert engine_kind() == "codex", "select the Codex native-schema profile"
    token = uuid.uuid4().hex[:12]
    answer = f"SCHEMA_{token}"
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string", "enum": [answer]}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    created = create_agent_variant_session(
        e2e_client,
        name_suffix="Queued schema",
        engine_options={"turn_start": {"outputSchema": schema}},
        permission_mode=permission_mode("unattended"),
    )
    sid = str(created["session_id"])
    try:
        poll_until_agent_ready(e2e_client, sid)
        started_path = workspace_path(e2e_client, sid, f"schema_{token}.started")
        release_name = f"schema_{token}.release"
        release_path = workspace_path(e2e_client, sid, release_name)
        command = "sh -c " + shlex.quote(
            f"printf started > {shlex.quote(started_path)}; "
            f"while [ ! -e {shlex.quote(release_path)} ]; do sleep 0.1; done"
        )
        first_marker = f"FIRST_NATIVE_{token}"
        first = send_and_follow(
            e2e_client,
            sid,
            f"Task {first_marker}: use the {tool_name('command')} tool to run exactly this "
            "command in the foreground and wait for it to finish. I will create the release "
            "file; do not create it yourself or run any other command. After it finishes, "
            f"reply with only the bare word done: {command}",
            until="output",
        )
        assert wait_for_file(e2e_client, sid, started_path, timeout=30.0) == b"started"
        assert not first.ended, first.response

        refused_marker = f"REFUSED_NATIVE_{token}"
        receipt = send_input(e2e_client, sid, f"Reply with only {refused_marker}.")
        refused = Reply(
            session_id=sid,
            input_id=str(receipt["input_id"]),
            client_message_id=str(receipt["client_message_id"]),
            stream=first.stream,
        )
        follow_reply(e2e_client, refused, until="end", allow_refusal=True)
        assert refused.input_failure is not None
        assert refused.input_failure.get("code") == "ENGINE_CAPABILITY_UNAVAILABLE", refused.input_failure
        assert "outputSchema" in refused.input_failure.get("message", ""), refused.input_failure
        assert refused.response is None, "a refused input must start no native response"
        busy = get_session(e2e_client, sid)
        assert first.response_id in [row["response_id"] for row in busy["current_responses"]], busy
        assert psql_rows(
            "SELECT state, response_id FROM session_inputs "
            f"WHERE input_id = {sql_literal(refused.input_id)}"
        ) == [{"state": "failed", "response_id": None}]

        uploaded = data(
            e2e_client.post(
                f"/api/v1/sessions/{sid}/files/upload",
                data={"path": ""},
                files={"files": (release_name, b"release", "text/plain")},
            )
        )
        assert uploaded.get("uploaded_count") == 1, uploaded
        follow_reply(e2e_client, first, until="end")
        assert first.error is None, first.response
        assert json.loads(first.text) == {"answer": answer}, first.text
        wait_until_settled(e2e_client, sid)

        idle_marker = f"IDLE_NATIVE_{token}"
        idle = send_and_follow(
            e2e_client,
            sid,
            f"Task {idle_marker}: do not use any tool. Reply with only the bare word plain.",
            until="end",
        )
        assert idle.error is None, idle.response
        assert json.loads(idle.text) == {"answer": answer}, idle.text
        settled = wait_until_settled(e2e_client, sid)
        assert settled["last_response"]["response_id"] == idle.response_id, settled
        responses = psql_rows(
            "SELECT response_id FROM session_responses "
            f"WHERE session_id = {sql_literal(sid)}"
        )
        assert {row["response_id"] for row in responses} == {first.response_id, idle.response_id}, responses

        # Positive native custody for both executed inputs bounds the absence
        # check: the mirror must have caught up past the refused input first.
        native = ""
        deadline = deadline_after(30)
        while time.monotonic() < deadline:
            native = json.dumps([row["entry"] for row in transcript_entries(sid)])
            if first_marker in native and idle_marker in native:
                break
            time.sleep(0.5)
        assert first_marker in native and idle_marker in native, "executed native inputs were not mirrored"
        assert refused_marker not in native, "the refused input reached Codex despite the schema refusal"
    finally:
        release_session(sid)
