"""Permission-mode e2e — ``acceptEdits`` lets a Write proceed with no prompt.

The counterpart to ``test_tool_permission.py``: with the session created in
``acceptEdits`` mode, a file Write is auto-accepted by the in-box CLI, so the
turn runs to completion (``finish: stop``) WITHOUT ever surfacing a pending
``tool_approval`` interaction, and the file is written in a single turn. This
proves the host-resolved permission_mode is threaded into the in-box CLI options
(not silently dropped, which would either prompt anyway or fail open).

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
    stream_turn,
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

        res = stream_turn(
            e2e_client,
            sid,
            content=(
                f"Use the {tool_name('write')} tool to create a file at {path} containing the single "
                f"word CHERRY, then reply with the word DONE."
            ),
        )
        assert res.saw_ui_header, "missing x-vercel-ai-ui-message-stream: v1 header"
        assert res.error is None, f"acceptEdits turn emitted an error frame: {res.error}"

        # The defining assertion: NO permission interaction was surfaced — the Write
        # was auto-accepted. (Neither on the live stream nor on the detail projection.)
        assert not res.interactions, (
            f"acceptEdits still surfaced a permission interaction: "
            f"{[(i.get('tool_name'), i.get('presentation')) for i in res.interactions]}"
        )
        assert not detail.get("pending_interaction"), "unexpected pending interaction in acceptEdits"
        writers = tool_names("write")
        assert any(w in res.tool_names for w in writers), (
            f"the agent never called any of {writers}: tools={res.tool_names}"
        )
        # The turn completed (it did not pause on a tool-permission).
        assert res.finish_reason == "stop", (
            f"acceptEdits turn did not finish with stop: {res.finish_reason!r}"
        )

        content = wait_for_file(e2e_client, sid, path, timeout=30.0)
        assert content is not None, f"acceptEdits Write did not produce {path}"
        assert content.strip(), f"acceptEdits Write produced an empty file: {content!r}"

        # The session settled (READY, no lingering pending interaction). The
        # stream's finish frame precedes the kernel's terminal projection by a
        # beat, so this waits rather than reading once.
        final = wait_until_settled(e2e_client, sid)
        assert not final.get("pending_interaction")
    finally:
        release_session(sid)
