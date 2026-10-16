"""History e2e — ``GET /messages`` returns the message and its answer once the response ends.

An answered message is durable: the person's message and the runtime's
response are served by ``/messages`` (the endpoint the console loads a session
from) as AI SDK UI messages, the user message under the id the console sent it
with and the answer under its response id. What history shows is what the
stream delivered, not a separate rendering of it.

Run it explicitly (deselected in the default unit run):

    .venv/bin/python -m pytest tests/e2e/test_messages.py -m e2e -s
"""

from __future__ import annotations

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    answer_in_history,
    create_session,
    history,
    message_text,
    poll_until_agent_ready,
    release_session,
    send_and_follow,
    user_messages,
    wait_until_settled,
)

pytestmark = pytest.mark.e2e

_QUESTION = "What is 7 times 6? Reply with just the number, nothing else."


def test_messages_transcript_has_user_and_assistant(e2e_client: httpx.Client) -> None:
    """An answered message and its response are retrievable via /messages, as streamed."""
    created = create_session(e2e_client)
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id in create response: {created}"
    try:
        poll_until_agent_ready(e2e_client, sid)

        # A no-tool question (pure text reply) so there is no permission prompt to clear.
        reply = send_and_follow(e2e_client, sid, _QUESTION, until="end")
        assert reply.error is None, f"the response failed: {reply.error}"
        assert reply.finish_reason == "stop", f"the response did not end normally: {reply.finish_reason!r}"
        assert "42" in reply.text, f"the streamed reply is missing the answer: {reply.text!r}"

        settled = wait_until_settled(e2e_client, sid)
        assert settled["last_response"]["response_id"] == reply.response_id, settled
        assert settled["last_response"]["outcome"] == "completed", settled

        messages = history(e2e_client, sid)
        sent_messages = user_messages(messages)
        assert [m["id"] for m in sent_messages] == [reply.client_message_id], (
            f"history does not hold the one message sent: {messages}"
        )
        sent = sent_messages[0]
        assert messages.index(sent) < messages.index(answer_in_history(messages, reply.input_id)), (
            "history shows the answer before the message it answers"
        )
        assert message_text(sent) == _QUESTION, sent
        assert sent["metadata"].get("input_id") == reply.input_id, sent["metadata"]
        answer = answer_in_history(messages, reply.input_id)
        assert answer["id"] == reply.response_id, answer
        assert answer["metadata"].get("outcome") == "completed", answer["metadata"]
        assert message_text(answer) == reply.text, (
            f"history's answer differs from what was streamed: {message_text(answer)!r} "
            f"vs {reply.text!r}"
        )
    finally:
        release_session(sid)
