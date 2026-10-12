"""E2E: administrators see a conversation's owner by name.

The name is the one the identity provider gave the signed-in user, read back
from ``GET /api/v1/user/current``. A conversation that user starts through the
API is created by a worker from a command that carries only the user id, so the
admin session list can show the name only if it was kept when the request was
admitted.

Run:

    .venv/bin/python -m pytest tests/e2e/test_user_profile.py -m e2e -s
"""

from __future__ import annotations

import httpx
import pytest

from tests.e2e._sandbox_helpers import create_session, data, release_session

pytestmark = pytest.mark.e2e


def test_an_administrator_sees_a_conversations_owner_by_name(
    e2e_client: httpx.Client,
) -> None:
    me = data(e2e_client.get("/api/v1/user/current"))
    name = str(me.get("display_name") or "").strip()
    user_id = str(me.get("user_id") or "").strip()
    assert name and name != user_id, (
        f"this case needs an identity whose display name differs from its id: {me}"
    )

    created = create_session(e2e_client)
    session_id = str(created["session_id"])
    release_session(session_id)
    agent_id = str(created["agent_id"])

    page = data(
        e2e_client.get(
            "/api/v1/admin/sessions/all",
            params={"agent_id": agent_id, "page": "1", "page_size": "200"},
        )
    )
    rows = [row for row in page["items"] if row.get("session_id") == session_id]
    assert rows, f"the new conversation {session_id} is missing from the admin list"
    assert rows[0].get("display_name") == name, (
        f"the admin list shows {rows[0].get('display_name')!r} for the owner, "
        f"not the name {name!r} the identity provider gave"
    )
