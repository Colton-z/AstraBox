"""A command that prints a NUL character does not stop its conversation.

Tools print binary data (a ``cat`` of an executable, a process's environment),
and PostgreSQL text cannot hold NUL. The tool's output is recorded with that
character replaced, the response ends, and the conversation answers its next
message.

    .venv/bin/python -m pytest tests/e2e/binary_tool_output_test.py -m e2e -s
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from tests.e2e import _sandbox_helpers as h
from tests.e2e._engine_profile import permission_mode

pytestmark = pytest.mark.e2e


def test_a_command_printing_a_nul_is_recorded_and_the_conversation_goes_on(e2e_client: httpx.Client) -> None:
    client = e2e_client
    sid = str(h.create_session(client, permission_mode=permission_mode("unattended")).get("session_id") or "")
    assert sid
    try:
        h.poll_until_agent_ready(client, sid)
        marker = uuid.uuid4().hex[:12]
        before, after = f"BEFORE_{marker}", f"AFTER_{marker}"
        printed = h.send_and_follow(
            client, sid,
            f"Use your shell tool once to run exactly: printf '{before}\\000{after}\\n' "
            "Then reply with only the word DONE.",
            until="end",
        )
        assert printed.error is None, printed.error
        assert printed.ended and printed.tool_names, printed.stream.summary()
        settled = h.wait_until_settled(client, sid)
        assert settled["last_response"]["response_id"] == printed.response_id, settled
        assert settled["last_response"]["outcome"] == "completed", settled
        outputs = [
            str(part.get("output"))
            for part in h.recorded_output(sid, printed.response_id)
            if part.get("type") == "tool-output-available"
        ]
        assert any(before in output and after in output for output in outputs), outputs

        answered = h.send_and_follow(client, sid, "What is 7 times 6? Reply with just the number.", until="end")
        assert answered.error is None, answered.error
        assert "42" in answered.text, answered.text
        assert h.wait_until_settled(client, sid)["last_response"]["outcome"] == "completed"
    finally:
        h.release_session(sid)
