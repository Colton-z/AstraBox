"""Session delete / archive lifecycle e2e — the read-back contract for the two
terminal operations a client can perform on a session.

The backend models a *session* as the unit of history (there is no separate
long-lived agent record), so the two lifecycle operations have distinct,
observable contracts a client can rely on:

* **archive** (``POST /sessions/{id}/archive``) reclaims the Session's sandbox,
  takes the Session offline and hides it from the default session list, but does
  **not** destroy its history: the Session stays individually readable
  (``GET /sessions/{id}`` still returns 200). Archive is the "release compute,
  keep the history, clear it off the active list" action.

* **delete** (``DELETE /sessions/{id}``) soft-deletes the session: it disappears
  from the list *and* becomes unreadable — ``GET /sessions/{id}`` returns HTTP 404
  with code ``SESSION_NOT_FOUND``. Because the session is the unit of history,
  deleting it also removes that history's read path. Delete is the destructive
  action.

Both operations complete synchronously: the HTTP response is returned only after
the runtime is torn down and the session row is updated, so the read-back
assertions below need no polling.

Run it explicitly (deselected in the default unit run):

    .venv/bin/python -m pytest tests/e2e/test_delete_and_archive.py -m e2e -s
"""

from __future__ import annotations

import time
import uuid

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    assert_release_matches_the_box,
    environment_sandbox_tenancy,
    create_session,
    data,
    get_session,
    permission_mode,
    poll_until_agent_ready,
    release_session,
    stream_turn,
    tool_name,
    wait_until_settled,
    workspace_path,
)

pytestmark = pytest.mark.e2e


def _list_session_ids(client: httpx.Client) -> set[str]:
    """Return the set of session_ids in the default (non-archived) session list."""
    resp = client.get("/api/v1/sessions")
    assert resp.status_code == 200, (
        f"GET /sessions -> {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    assert body.get("code") == "OK", f"non-OK envelope from GET /sessions: {body}"
    rows = body.get("data")
    assert isinstance(rows, list), f"GET /sessions data is not a list: {body}"
    return {str(r.get("session_id") or "") for r in rows}


def _messages(client: httpx.Client, sid: str) -> list:
    """Return the durable messages in the session's transcript (``GET /messages``)."""
    resp = client.get(f"/api/v1/sessions/{sid}/messages")
    assert resp.status_code == 200, (
        f"GET /sessions/{sid}/messages -> {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    assert body.get("code") == "OK", f"non-OK envelope from GET messages: {body}"
    msgs = (body.get("data") or {}).get("messages")
    assert isinstance(msgs, list), f"messages payload is not a list: {body}"
    return msgs


def _message_count(client: httpx.Client, sid: str) -> int:
    """Return the number of durable messages in the session's history."""
    return len(_messages(client, sid))


def _run_deterministic_turn(client: httpx.Client, sid: str, path: str, line: str) -> None:
    """Drive one Write turn that creates an exact file, then settle it.

    The session is created with ``bypassPermissions`` so the Write executes without
    pausing on a permission gate; the prompt pins an exact path + content so the
    turn is deterministic and produces concrete, tool-driven history. Returns after
    the turn settles back to READY.
    """
    res = stream_turn(
        client,
        sid,
        content=(
            f"Use the {tool_name('write')} tool exactly once to create a file at {path} whose only "
            f"content is the line {line!r}. Do not run any other tool or command."
        ),
    )
    assert res.saw_ui_header, "turn was not a valid AI-SDK message stream"
    assert res.error is None, f"turn errored: {res.error}"
    wait_until_settled(client, sid)


def test_archive_reclaims_sandbox_and_keeps_history_readable(
    e2e_client: httpx.Client,
) -> None:
    """archive reclaims compute and removes a session from the default list.

    End-to-end contract:
      * a session with a real completed turn in its transcript appears in ``GET /sessions``;
      * ``POST /sessions/{id}/archive`` confirms that it reclaimed the bound sandbox;
      * the archived session is EXCLUDED from the default ``GET /sessions`` list;
      * it is still individually readable via ``GET /sessions/{id}`` (200) and its
        sandbox pointer is cleared while message history stays intact.
    """
    created = create_session(e2e_client, permission_mode=permission_mode("unattended"))
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id in create response: {created}"
    try:
        poll_until_agent_ready(e2e_client, sid)
        sandbox_id = str(get_session(e2e_client, sid).get("sandbox_id") or "").strip()
        assert sandbox_id, "a READY conversation should hold a sandbox before archive"

        # A no-tool text turn establishes concrete history. History lives in the
        # transcript, not on disk — a Write landing a file is model-fragile, and the
        # archive contract is about the message history surviving, not any file.
        res = stream_turn(e2e_client, sid, content="Reply with just the number 2.")
        assert res.saw_ui_header, "turn was not a valid AI-SDK message stream"
        assert res.error is None, f"turn errored: {res.error}"
        wait_until_settled(e2e_client, sid)

        # The settled turn persists a user + assistant pair (eventually-consistent, it
        # lands just after the finish frame) — the durable history archive must preserve.
        messages: list = []
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            messages = _messages(e2e_client, sid)
            roles = {str(m.get("role")) for m in messages if isinstance(m, dict)}
            if "user" in roles and "assistant" in roles:
                break
            time.sleep(1.0)
        roles = {str(m.get("role")) for m in messages if isinstance(m, dict)}
        assert "user" in roles and "assistant" in roles, (
            f"session has no durable user+assistant history before archive: roles={roles}"
        )
        history_before = len(messages)

        # The live session is listed.
        assert sid in _list_session_ids(e2e_client), (
            "fresh session is missing from the default GET /sessions list"
        )

        # Archive it (synchronous: runtime torn down + row hidden before the response).
        archived = data(e2e_client.post(f"/api/v1/sessions/{sid}/archive"))
        assert str(archived.get("session_id") or "") == sid, (
            f"archive result targets the wrong session: {archived}"
        )
        assert archived.get("archived") is True, (
            f"archive did not report archived=True: {archived}"
        )
        assert str(archived.get("status") or "") == "sandbox-reclaimed", (
            f"archive did not report sandbox-reclaimed: {archived}"
        )
        if environment_sandbox_tenancy(e2e_client) != "agent":
            assert archived.get("killed") is True, (
                "a conversation-owned sandbox must be killed by its archive: "
                f"{archived}"
            )
        assert_release_matches_the_box(
            e2e_client, archived, sandbox_id=sandbox_id, operation="archive"
        )
        assert str(archived.get("sandbox_id") or "") == sandbox_id, (
            f"archive reclaimed the wrong sandbox (expected {sandbox_id}): {archived}"
        )

        # It is now excluded from the default list ...
        assert sid not in _list_session_ids(e2e_client), (
            "archived session still appears in the default GET /sessions list"
        )
        # ... but remains individually readable (200, same id) ...
        detail = get_session(e2e_client, sid)
        assert str(detail.get("session_id") or "") == sid, (
            f"archived session is not readable via GET /sessions/{{id}}: {detail}"
        )
        assert not str(detail.get("sandbox_id") or "").strip(), (
            f"archived session still exposes sandbox binding {detail.get('sandbox_id')!r}"
        )
        # ... with its history intact — archive must preserve history, not mutate it.
        history_after = _message_count(e2e_client, sid)
        assert history_after == history_before, (
            f"archive changed the message history ({history_before} -> {history_after})"
        )
    finally:
        release_session(sid)


def test_delete_makes_session_unreadable(e2e_client: httpx.Client) -> None:
    """delete soft-deletes a session: it leaves the list AND reads back a hard 404.

    This is the load-bearing distinction from archive: because the session is the
    unit of history, deleting it removes that history's read path entirely.
    End-to-end contract:
      * a session with real history is listed and readable;
      * ``DELETE /sessions/{id}`` returns OK with ``deleted=True``;
      * afterwards the session is EXCLUDED from ``GET /sessions``;
      * and ``GET /sessions/{id}`` returns HTTP 404 with code ``SESSION_NOT_FOUND``
        (a hard not-found, not a lingering readable DELETED projection).
    """
    marker = uuid.uuid4().hex[:8]
    created = create_session(e2e_client, permission_mode=permission_mode("unattended"))
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id in create response: {created}"
    file_path = workspace_path(e2e_client, sid, f"delete_{marker}.txt")
    try:
        poll_until_agent_ready(e2e_client, sid)

        _run_deterministic_turn(e2e_client, sid, file_path, f"soon-deleted-{marker}")
        assert _message_count(e2e_client, sid) >= 1, (
            "session has no durable message history before delete"
        )
        assert sid in _list_session_ids(e2e_client), (
            "fresh session is missing from the default GET /sessions list"
        )

        deleted = data(e2e_client.delete(f"/api/v1/sessions/{sid}"))
        assert str(deleted.get("session_id") or "") == sid, (
            f"delete result targets the wrong session: {deleted}"
        )
        assert deleted.get("deleted") is True, (
            f"delete did not report deleted=True: {deleted}"
        )

        assert sid not in _list_session_ids(e2e_client), (
            "deleted session still appears in the default GET /sessions list"
        )
        # And unreadable: the detail read is a hard 404 carrying SESSION_NOT_FOUND.
        missing = e2e_client.get(f"/api/v1/sessions/{sid}")
        assert missing.status_code == 404, (
            f"GET a deleted session should be 404, got {missing.status_code}: "
            f"{missing.text[:300]}"
        )
        assert missing.json().get("code") == "SESSION_NOT_FOUND", (
            f"deleted-session read should carry code SESSION_NOT_FOUND: {missing.json()}"
        )
    finally:
        release_session(sid)
