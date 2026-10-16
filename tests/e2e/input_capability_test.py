"""A text-only Agent refuses images before accepting any part of the message."""

from __future__ import annotations

import uuid

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    create_session,
    engine_kind,
    history,
    poll_until_agent_ready,
    psql_rows,
    release_session,
    send_and_follow,
    sql_literal,
    user_messages,
    wait_until_settled,
)

pytestmark = pytest.mark.e2e

# A real 16×16 red PNG, also used by the clipboard image journey.
_IMAGE = {
    "type": "image",
    "source": {
        "type": "base64",
        "media_type": "image/png",
        "data": (
            "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAFklEQVR42mP4z8BAEmIY1TCqYfhq"
            "AACQ+f8B8u7oVwAAAABJRU5ErkJggg=="
        ),
    },
}


def test_a_text_only_engine_refuses_images_without_accepting_them(
    e2e_client: httpx.Client,
) -> None:
    assert engine_kind() in {"codex", "pi"}, "select a text-only Codex or Pi profile"
    sid = str(create_session(e2e_client)["session_id"])
    try:
        poll_until_agent_ready(e2e_client, sid)
        assert user_messages(history(e2e_client, sid)) == []
        refused_ids = []
        for content in ([{"type": "text", "text": "Describe this picture."}, _IMAGE], [_IMAGE]):
            client_id = str(uuid.uuid4())
            refused_ids.append(client_id)
            refused = e2e_client.post(
                f"/api/v1/sessions/{sid}/inputs",
                json={"content": content, "client_message_id": client_id},
            )
            assert refused.status_code == 409, refused.text
            body = refused.json()
            assert body.get("code") == "ENGINE_CAPABILITY_UNAVAILABLE", body
            assert "image" in body.get("message", ""), body

        assert user_messages(history(e2e_client, sid)) == []
        assert psql_rows(
            f"SELECT input_id FROM session_inputs WHERE session_id = {sql_literal(sid)}"
        ) == [], "a refused image must not leave a durable accepted input"

        marker = f"TEXT_ACCEPTED_{uuid.uuid4().hex[:8]}"
        reply = send_and_follow(
            e2e_client,
            sid,
            f"Reply with only {marker}.",
            client_message_id=refused_ids[0],
            until="end",
        )
        assert reply.error is None and marker in reply.text, reply.response
        wait_until_settled(e2e_client, sid)
        messages = user_messages(history(e2e_client, sid))
        assert [message["id"] for message in messages] == [refused_ids[0]], messages
    finally:
        release_session(sid)
