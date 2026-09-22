"""Transcript e2e — ``GET /messages`` returns the persisted turn after it settles.

A completed turn is durable: the user prompt and the assistant reply are
projected into the conversation transcript and served by ``/messages`` (the
endpoint the FE rehydrates a session from on load). This proves the dd turn's
frames were persisted to the store, not just streamed-and-forgotten.

Run it explicitly (deselected in the default unit run):

    .venv/bin/python -m pytest tests/e2e/test_messages.py -m e2e -s
"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    release_session,
    create_session,
    data,
    poll_until_agent_ready,
    stream_turn,
    wait_until_settled,
)

pytestmark = pytest.mark.e2e


def test_messages_transcript_has_user_and_assistant(e2e_client: httpx.Client) -> None:
    """A settled turn persists a user + assistant pair retrievable via /messages."""
    created = create_session(e2e_client)
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id in create response: {created}"
    try:
        poll_until_agent_ready(e2e_client, sid)

        # A no-tool turn (pure text reply) so there is no permission prompt to clear.
        res = stream_turn(
            e2e_client,
            sid,
            content="What is 7 times 6? Reply with just the number, nothing else.",
        )
        assert res.error is None, f"turn errored: {res.error}"
        assert res.finish_reason == "stop", f"turn did not finish cleanly: {res.finish_reason!r}"
        assert "42" in res.text, f"stream reply missing the answer: {res.text!r}"

        # Let the turn settle, then poll /messages — the transcript projection is
        # eventually-consistent and lands just after the stream's finish frame.
        wait_until_settled(e2e_client, sid)
        messages: list = []
        assistant_blob = ""
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            transcript = data(
                e2e_client.get(f"/api/v1/sessions/{sid}/messages", params={"limit": "20"})
            )
            messages = transcript.get("messages") or []
            roles = {str(m.get("role")) for m in messages if isinstance(m, dict)}
            assistant_blob = " ".join(
                json.dumps(m)
                for m in messages
                if isinstance(m, dict) and str(m.get("role")) == "assistant"
            )
            if "user" in roles and "assistant" in roles and "42" in assistant_blob:
                break
            time.sleep(1.0)

        assert isinstance(messages, list) and len(messages) >= 2, (
            f"transcript too short to hold the turn: {messages}"
        )
        roles = {str(m.get("role")) for m in messages if isinstance(m, dict)}
        assert "user" in roles, f"no user message persisted: roles={roles}"
        assert "assistant" in roles, f"no assistant message persisted: roles={roles}"
        # The assistant message actually carries the answer (serialize-and-search is
        # robust to the exact content-part schema).
        assert "42" in assistant_blob, f"assistant transcript missing the answer: {assistant_blob[:400]}"
    finally:
        release_session(sid)
