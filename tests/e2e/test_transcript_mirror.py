"""A lost box: its open response fails, and a replacement box carries the conversation on.

The runtime owns its conversation and writes its own transcript; every engine's
box mirrors that transcript to the platform (``transcript_scopes`` /
``transcript_entries``), and that custody is what a replacement runtime resumes
from when the box that wrote it is gone.

What is proven, against a box destroyed the way a lost node destroys it:

* the response that was running when the box died ends ``failed`` with the
  platform's reason ``sandbox_lost`` — on the stream, in history and in the
  platform's own record — and its input is not run again;
* the next message, which the dead runtime never received, gets a new box,
  and that box answers it from the restored transcript: the project label it
  asks for was said only to the box that is gone.

Conversation tenancy is required for the same reason the kill is: the proof
rests on the box that heard the label being gone. Under agent tenancy the box
is the Agent's and outlives this conversation.

Run it against a running deployment:

    ASTRABOX_E2E_BASE_URL=http://localhost:8000 \\
      .venv/bin/python -m pytest tests/e2e/test_transcript_mirror.py -m e2e -s
"""

from __future__ import annotations

import json
import os
import shlex
import time
import uuid

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    answer_in_history,
    assert_database_is_the_live_stack,
    create_agent_variant_session,
    deadline_after,
    destroy_box,
    engine_kind,
    environment_with_tenancy,
    follow_reply,
    get_admin_session_detail,
    get_session,
    history,
    permission_mode,
    poll_until_agent_ready,
    recorded_input,
    recorded_response,
    release_session,
    send_and_follow,
    tool_name,
    transcript_entries,
    user_messages,
    wait_for_file,
    wait_until_settled,
    workspace_path,
)

pytestmark = pytest.mark.e2e

# The mirror is the box's resident relay on a poll, so "the box answered" and
# "the platform holds the transcript" are different moments; wait for the latter.
MIRROR_LAND_TIMEOUT_S = float(os.getenv("ASTRABOX_E2E_MIRROR_TIMEOUT", "60"))


def _assert_pi_settings(client: httpx.Client, sid: str, expected: dict) -> None:
    """Read native config from the actual original or replacement box."""

    from tests.e2e.test_terminal import _run_terminal

    identity = get_admin_session_detail(client, sid).get("runtime_identity") or {}
    home = str(identity.get("home_dir") or "")
    assert home, identity
    stdout, stderr, code, events = _run_terminal(
        client, sid, "cat " + shlex.quote(f"{home}/.pi/agent/settings.json")
    )
    assert code == 0, (stderr, events)
    actual = json.loads(stdout)
    assert {key: actual[key] for key in expected} == expected


def _wait_for_custody(sid: str, marker: str) -> None:
    """Wait until the platform's copy of the runtime's transcript holds ``marker``."""
    deadline = deadline_after(MIRROR_LAND_TIMEOUT_S)
    entries: list[dict] = []
    while time.monotonic() < deadline:
        entries = transcript_entries(sid)
        if marker in json.dumps(entries):
            return
        time.sleep(1.5)
    pytest.fail(
        f"the platform never held session {sid}'s transcript with {marker!r} before the "
        f"box was destroyed ({len(entries)} entries); the replacement cannot be under test"
    )


@pytest.mark.xdist_group("conversation-tenancy-box")
def test_a_replacement_box_answers_from_the_restored_transcript(
    e2e_client: httpx.Client,
) -> None:
    marker = "MEM" + uuid.uuid4().hex[:6].upper()
    pi_settings = {
        "defaultThinkingLevel": "off",
        "compaction": {"enabled": False, "keepRecentTokens": 12000},
        "futureVendorSetting": {"marker": marker},
    }
    created = create_agent_variant_session(
        e2e_client,
        name_suffix="mirror-restore",
        engine_options={"settings": pi_settings} if engine_kind() == "pi" else {},
        environment_name=environment_with_tenancy(e2e_client, "conversation"),
        permission_mode=permission_mode("unattended"),
    )
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id in create response: {created}"
    try:
        poll_until_agent_ready(e2e_client, sid)
        assert_database_is_the_live_stack(sid)
        first_box = str(get_session(e2e_client, sid).get("sandbox_id") or "")
        assert first_box, "a READY session should hold a sandbox_id"
        if engine_kind() == "pi":
            _assert_pi_settings(e2e_client, sid, pi_settings)

        labelled = send_and_follow(
            e2e_client,
            sid,
            f"Our hypothetical investment research project is labelled {marker}. "
            "For this project, explain in one sentence what revenue growth measures. "
            "Include the project label in your answer. This is a general definition, "
            "so no external research or workspace artifact is needed.",
            until="end",
        )
        assert labelled.error is None and labelled.finish_reason == "stop", (
            f"the first response did not end normally: {labelled.error!r} {labelled.finish_reason!r}"
        )
        assert marker.upper() in labelled.text.upper(), (
            f"the first response did not establish the project label: {labelled.text!r}"
        )
        wait_until_settled(e2e_client, sid)
        _wait_for_custody(sid, marker)

        # A response that is running when the box dies: a real command waiting
        # on a release file nobody creates.
        gate = f"lost_box_{uuid.uuid4().hex[:12]}"
        started_path = workspace_path(e2e_client, sid, f"{gate}.started")
        release_path = workspace_path(e2e_client, sid, f"{gate}.release")
        command = "sh -c " + shlex.quote(
            f"printf started > {shlex.quote(started_path)}; "
            f"while [ ! -e {shlex.quote(release_path)} ]; do sleep 0.1; done"
        )
        running = send_and_follow(
            e2e_client,
            sid,
            f"Use the {tool_name('command')} tool to run exactly this one command in the "
            "foreground and wait for it to finish; I will create the release file when it is time. "
            "Do not create the release file yourself or run "
            f"any other command: {command}",
            until="output",
        )
        assert wait_for_file(e2e_client, sid, started_path, timeout=30.0) == b"started", (
            "the command never started, so no response was running when the box died"
        )
        assert running.response is not None and not running.ended, running.response

        assert recorded_input(sid, running.input_id)["state"] == "admitted", (
            "the running response's own input was never taken by the runtime"
        )

        destroy_box(first_box)

        # The response the box took with it ends failed, on the stream as the
        # console sees it: an error, then a finish the platform wrote.
        follow_reply(e2e_client, running, until="end")
        assert running.outcome == "failed" and running.error, (
            f"the lost response did not end failed with an error on the stream: "
            f"outcome={running.outcome!r} error={running.error!r} finish={running.finish_reason!r}"
        )
        lost = recorded_response(sid, str(running.response_id))
        assert (lost["status"], lost["outcome"], lost["native_reason"]) == (
            "ended", "failed", "sandbox_lost",
        ), f"the platform did not record the box loss on the response: {lost}"

        # The next message: the dead runtime never received it, and a
        # replacement box answers it from the conversation it resumed.
        recalled = send_and_follow(
            e2e_client,
            sid,
            "What was the project label I gave you for our revenue-growth discussion? "
            "Reply with that original label.",
            until="end",
        )
        assert recalled.error is None and recalled.finish_reason == "stop", (
            f"the replacement's response did not end normally: "
            f"{recalled.error!r} {recalled.finish_reason!r}"
        )
        assert marker.upper() in recalled.text.upper(), (
            f"the replacement box did not recall {marker!r} from the restored transcript; "
            f"it answered: {recalled.text!r}"
        )
        settled = wait_until_settled(e2e_client, sid)
        second_box = str(settled.get("sandbox_id") or "")
        assert second_box and second_box != first_box, (
            f"the conversation is still on the destroyed box {first_box!r}: {second_box!r}"
        )
        assert recorded_input(sid, recalled.input_id)["state"] == "admitted"
        if engine_kind() == "pi":
            _assert_pi_settings(e2e_client, sid, pi_settings)

        # History tells the same story once: three messages, the lost response
        # failed and never re-run, the replacement's answer completed.
        messages = history(e2e_client, sid)
        assert [m["id"] for m in user_messages(messages)] == [
            labelled.client_message_id,
            running.client_message_id,
            recalled.client_message_id,
        ], [m["id"] for m in user_messages(messages)]
        lost_answer = answer_in_history(messages, running.input_id)
        assert lost_answer["id"] == running.response_id
        assert lost_answer["metadata"].get("outcome") == "failed", lost_answer["metadata"]
        assert lost_answer["metadata"].get("native_reason") == "sandbox_lost", lost_answer["metadata"]
        answer = answer_in_history(messages, recalled.input_id)
        assert answer["metadata"].get("outcome") == "completed", answer["metadata"]
    finally:
        release_session(sid)
