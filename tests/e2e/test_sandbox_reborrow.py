"""Sandbox reclaim + re-borrow e2e — terminating the sandbox never dead-ends a session.

The sandbox that backs a live session can be taken away from under it: an operator
terminates it, or an idle lease lapses and the backend recycles the instance. This
suite pins the platform's recovery contract for that event:

* **A reclaim keeps the conversation wakeable.** ``POST /sandbox/terminate`` takes
  the conversation's compute away, but the session never reaches a terminal state
  and reports ``runtime_unavailable`` — the reclaim is a recoverable degradation,
  not a dead end.

* **The next message runs on live compute.** Sending after the reclaim recovers the
  session: the turn streams a real model reply and settles back to ``READY`` on a
  working placement.

Whether the reclaim destroys the BOX is not a property of this endpoint and the test
does not assume it. Under ``conversation`` tenancy the box is the conversation's own and
is always destroyed. Under ``agent`` it belongs to the Agent, and the answer depends
on who else is in it at that instant: a sibling conversation or an unclaimed prepared
slot keeps it alive, an empty one is destroyed rather than held for the rest of its
lease.

So the test reads the reclaim's own ``killed`` and holds the re-borrow to it: a box
that really died must not be the one the next turn runs on.

Sessions are created with ``bypassPermissions`` so a trivial text turn settles without
pausing on a tool-permission prompt — this suite exercises the sandbox lifecycle, not
the approval gate.

Run it explicitly (deselected in the default unit run):

    .venv/bin/python -m pytest tests/e2e/test_sandbox_reborrow.py -m e2e -s
"""

from __future__ import annotations

import time
import uuid

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    assert_release_matches_the_box,
    permission_mode,
    TERMINAL_STATES,
    box_is_running,
    environment_sandbox_tenancy,
    release_session,
    create_session,
    data,
    get_session,
    poll_until_agent_ready,
    stream_turn,
    wait_until_settled,
)

pytestmark = pytest.mark.e2e


def _sandbox_id(detail: dict) -> str:
    """The session's currently-bound sandbox id ("" when it has none)."""
    return str(detail.get("sandbox_id") or "").strip()


def _terminate_sandbox(client: httpx.Client, sid: str, *, sandbox_id: str) -> dict:
    """Reclaim the session's compute and assert what the reclaim was entitled to kill.

    Returns the reclaim result. The response echoes the reclaimed sandbox id, so the
    caller can prove the replacement (borrowed later) is a different instance.

    ``killed`` is not a constant, and pinning it to either value is wrong.
    Under ``conversation`` the box IS the conversation, so a reclaim always
    kills it. Under ``agent`` the box belongs to the Agent, and whether the
    reclaim kills it depends on who else is in it AT THAT MOMENT — a sibling
    conversation or an unclaimed prepared slot keeps it, and an empty box is
    destroyed rather than held for the rest of its lease. Measured: the two
    cases in this file, on the same deployment, get opposite answers.

    So what is asserted is not the value but its TRUTHFULNESS — a killed box
    must be gone and a kept box must still be running. That is stronger than
    either fixed expectation, because it is the one claim a wrong answer
    cannot satisfy in either direction, and it stops this assertion from
    encoding a deployment's tenancy at all.

    A turn's stream can close (``[DONE]``) a beat before the server's
    ``conversation_state`` leaves STREAMING, so a terminate issued right after a
    settled turn can briefly race a still-active turn — a 409 SESSION_BUSY that the
    server marks ``retryable``. Retry that specific race before failing loud.
    """
    deadline = time.monotonic() + 30.0
    while True:
        resp = client.post(f"/api/v1/sessions/{sid}/sandbox/terminate", timeout=60.0)
        if resp.status_code == 409 and time.monotonic() < deadline:
            body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            if str((body.get("data") or {}).get("reason") or "") == "active_conversation_turn":
                time.sleep(2.0)
                continue
        result = data(resp)  # 200 unwraps; any other status fails loud with the envelope
        break
    assert str(result.get("session_id") or "") == sid, f"terminate targeted the wrong session: {result}"
    assert str(result.get("status") or "") == "sandbox-reclaimed", f"unexpected terminate status: {result}"
    if environment_sandbox_tenancy(client) != "agent":
        assert result.get("killed") is True, (
            f"a conversation-owned sandbox must be killed by its reclaim: {result}"
        )
    assert_release_matches_the_box(
        client,
        result,
        sandbox_id=str(result.get("sandbox_id") or ""),
        operation="terminate",
    )
    return result


def _box_leaves_running(client: httpx.Client, sandbox_id: str, timeout: float = 30.0) -> bool:
    """Whether the box stops reporting RUNNING within the budget.

    Polled rather than asked once: destruction is confirmed to the caller
    before the control plane's own inventory has caught up, so a single read
    would fail on the lag rather than on the claim.
    """
    deadline = time.monotonic() + timeout
    while True:
        if not box_is_running(sandbox_id=sandbox_id, client=client):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(1.0)


def _wait_runtime_unavailable(client: httpx.Client, sid: str, timeout: float = 45.0) -> dict:
    """Poll session detail until it reports the reclaimed (``runtime_unavailable``) state.

    A reclaimed sandbox must surface as a recoverable degradation, so this fails loud if
    the session instead reaches a terminal state, and fails loud on timeout. Returns the
    settled detail.
    """
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = get_session(client, sid)
        state = str(last.get("state") or "")
        if state in TERMINAL_STATES:
            pytest.fail(
                f"session {sid} went terminal ({state}) after sandbox terminate; a reclaimed "
                f"sandbox must stay wakeable (last_error={last.get('last_error')!r})"
            )
        if bool(last.get("runtime_unavailable")):
            return last
        time.sleep(1.0)
    pytest.fail(
        f"session {sid} never reported runtime_unavailable within {timeout:.0f}s after "
        f"sandbox terminate; state={last.get('state')!r}"
    )


def _reply_turn(client: httpx.Client, sid: str, prompt: str, expect: str) -> None:
    """Drive one text-only turn and assert the model streamed the expected token.

    ``expect`` is matched case-insensitively as a substring of the streamed reply.
    """
    res = stream_turn(client, sid, content=prompt)
    assert res.saw_ui_header, "missing x-vercel-ai-ui-message-stream: v1 header"
    assert res.error is None, f"turn errored: {res.error}"
    assert res.n_text_delta > 0, f"turn streamed no text-delta (finish={res.finish_reason})"
    assert expect.upper() in res.text.upper(), f"expected {expect!r} in the reply, got: {res.text!r}"


def test_active_session_automatically_recovers_after_sandbox_reclaim(e2e_client: httpx.Client) -> None:
    """Missing compute is recoverable; sending a new message must not terminate the session."""
    created = create_session(e2e_client, permission_mode=permission_mode("unattended"))
    sid = str(created["session_id"])
    try:
        poll_until_agent_ready(e2e_client, sid)
        _reply_turn(e2e_client, sid, "Reply with exactly the single digit 2. Do not use tools.", "2")
        before = wait_until_settled(e2e_client, sid)
        original_box = _sandbox_id(before)
        reclaimed = _terminate_sandbox(e2e_client, sid, sandbox_id=original_box)
        _wait_runtime_unavailable(e2e_client, sid)
        assert get_session(e2e_client, sid)["state"] not in {"TERMINATED", "DELETED"}
        marker = "AUTO" + uuid.uuid4().hex[:6].upper()
        _reply_turn(e2e_client, sid, f"Reply with exactly {marker}. Do not use tools.", marker)
        after = wait_until_settled(e2e_client, sid)
        assert after["state"] == "READY"
        assert _sandbox_id(after)
        if reclaimed.get("killed"):
            assert _sandbox_id(after) != original_box
    finally:
        release_session(sid)
