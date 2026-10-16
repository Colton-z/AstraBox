"""Permission-mode e2e — an alternate write mode lets a write proceed with no prompt.

The counterpart to ``test_tool_permission.py``: with the session in the
engine's alternate write mode (``acceptEdits`` for Claude), a file write is
auto-accepted by the runtime, so the response runs to its end (``finish: stop``)
WITHOUT ever pausing on a ``tool_approval`` interaction, and the file is
written. This proves the session's permission mode reaches the runtime (not
silently dropped, which would either prompt anyway or fail open).

Run it explicitly (deselected in the default unit run):

    .venv/bin/python -m pytest tests/e2e/test_permission_mode.py -m e2e -s
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    assert_permission_mode_unavailable,
    contract_supported,
    create_session,
    get_session,
    permission_mode,
    poll_until_agent_ready,
    release_session,
    send_and_follow,
    tool_name,
    tool_names,
    wait_for_file,
    wait_until_settled,
    workspace_path,
)

pytestmark = pytest.mark.e2e


def test_accept_edits_writes_without_a_prompt(e2e_client: httpx.Client) -> None:
    """An alternate write mode, or a declared modeless engine, never raises a gate."""
    selected_mode = permission_mode("alternate")
    created = create_session(e2e_client, permission_mode=selected_mode)
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id in create response: {created}"
    path = workspace_path(e2e_client, sid, f"accept_{uuid.uuid4().hex[:8]}.txt")
    try:
        poll_until_agent_ready(e2e_client, sid)

        # The mode is recorded on the session (host threaded it through, not dropped).
        # Against the name the RUN asked for: the platform stores the engine's
        # own word for the mode, so a literal here tests one engine's spelling.
        detail = get_session(e2e_client, sid)
        if contract_supported("permission_modes"):
            assert selected_mode is not None
            assert str(detail.get("permission_mode")) == selected_mode, (
                f"the requested mode was not recorded on the session: "
                f"{detail.get('permission_mode')!r}"
            )
        else:
            assert selected_mode is None
            assert_permission_mode_unavailable(e2e_client, sid)

        # Followed to a pause or the end: a pause on an approval here is the
        # failure, and it must be reported as soon as it happens.
        reply = send_and_follow(
            e2e_client,
            sid,
            f"Use the {tool_name('write')} tool to create a file at {path} containing the single "
            "word CHERRY, then reply with the word DONE.",
            until="pause",
        )
        assert reply.saw_ui_header, "the session stream is not an AI SDK UI message stream"
        assert reply.error is None, f"the response failed: {reply.error}"

        # The defining assertion: NO permission interaction was surfaced — the
        # write was auto-accepted, so the response never paused.
        assert not reply.interactions and not reply.paused, (
            f"the alternate mode still paused on a permission interaction: "
            f"{[(i.get('tool_name'), i.get('presentation')) for i in reply.interactions]}"
        )
        writers = tool_names("write")
        assert any(w in reply.tool_names for w in writers), (
            f"the agent never called any of {writers}: tools={reply.tool_names}"
        )
        assert reply.finish_reason == "stop", (
            f"the response did not end normally: {reply.finish_reason!r}"
        )

        content = wait_for_file(e2e_client, sid, path, timeout=30.0)
        assert content is not None, f"the alternate-mode write did not produce {path}"
        assert content.strip(), f"the alternate-mode write produced an empty file: {content!r}"

        final = wait_until_settled(e2e_client, sid)
        assert not final.get("pending_interaction")
        assert final["last_response"]["response_id"] == reply.response_id, final
        assert final["last_response"]["outcome"] == "completed", final
    finally:
        release_session(sid)
