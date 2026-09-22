"""Sandbox reclaim + re-borrow e2e — terminating the sandbox never dead-ends a session.

The sandbox that backs a live session can be taken away from under it: an operator
terminates it, or an idle lease lapses and the backend recycles the instance. This
suite pins the platform's recovery contract for that event across two proofs:

* **A reclaim keeps the conversation wakeable.** ``POST /sandbox/terminate`` takes
  the conversation's compute away and drops the session's sandbox pointer, but the
  session stays ``READY`` (never a terminal failure state) and reports
  ``runtime_unavailable`` — the reclaim is a recoverable degradation, not a dead end.

* **The next turn runs on live compute.** After the sandbox that served a real turn
  is reclaimed, recovering the session gives it a working placement again and the
  following turn streams a real model reply and settles back to ``READY``.

Whether the reclaim destroys the BOX is not a property of this endpoint and neither
case assumes it. Under ``conversation`` tenancy the box is the conversation's own and
is always destroyed. Under ``agent`` it belongs to the Agent, and the answer depends
on who else is in it at that instant: a sibling conversation or an unclaimed prepared
slot keeps it alive, an empty one is destroyed rather than held for the rest of its
lease. The two cases below get opposite answers on the same deployment for exactly
that reason.

So both read the reclaim's own ``killed`` and hold it to its word — a killed box has
to be gone, a kept box has to still be running — and the re-borrow proof follows the
same field: a box that really died must not be the one the next turn runs on, while a
box that was kept is where the conversation is supposed to come back to.

Recovery is driven explicitly with ``POST /recover`` before the second turn: on this
backend a plain chat session does not transparently re-borrow a sandbox on the next
message, so the test provisions the replacement deterministically rather than probing
for auto-recovery.

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
    create_agent_variant_session,
    environment_with_tenancy,
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


def test_deployment_provides_both_box_tenancies(e2e_client: httpx.Client) -> None:
    """Each live engine fixture supports both shared and dedicated-box journeys."""
    shared = environment_with_tenancy(e2e_client, "agent")
    dedicated = environment_with_tenancy(e2e_client, "conversation")
    assert shared != dedicated, "one Environment cannot supply both box ownership modes"


def test_terminate_sandbox_keeps_session_wakeable(e2e_client: httpx.Client) -> None:
    """Terminating the sandbox reclaims it yet leaves the session recoverable, not terminal.

    The terminate endpoint reports the reclaim and echoes the killed sandbox id; the
    session then drops its sandbox pointer and — the load-bearing invariant — stays
    ``READY`` with ``runtime_unavailable`` set instead of falling into a terminal failure
    state. A reclaimed sandbox is a recoverable non-event for the conversation.
    """
    created = create_session(e2e_client, permission_mode=permission_mode("unattended"))
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id in create response: {created}"
    try:
        poll_until_agent_ready(e2e_client, sid)
        old_sandbox = _sandbox_id(get_session(e2e_client, sid))
        assert old_sandbox, "a READY session should hold a sandbox_id"

        result = _terminate_sandbox(e2e_client, sid, sandbox_id=old_sandbox)
        assert str(result.get("sandbox_id") or "") == old_sandbox, (
            f"terminate should reclaim the bound sandbox {old_sandbox}, got: {result}"
        )

        # The reclaim keeps the session wakeable: READY, sandbox pointer cleared, and
        # flagged runtime_unavailable (a recoverable degradation, never terminal).
        reclaimed = _wait_runtime_unavailable(e2e_client, sid)
        assert str(reclaimed.get("state") or "") == "READY", (
            f"reclaimed session must stay READY (wakeable), got state={reclaimed.get('state')!r}"
        )
        assert not _sandbox_id(reclaimed), (
            f"reclaimed session should drop its sandbox pointer, still bound to "
            f"{_sandbox_id(reclaimed)!r}"
        )
    finally:
        release_session(sid)


@pytest.mark.xdist_group("conversation-tenancy-box")
def test_next_turn_reborrows_a_fresh_sandbox(e2e_client: httpx.Client) -> None:
    """A live turn -> terminate the sandbox -> recover -> the next turn runs on a NEW sandbox.

    This is the end-to-end re-borrow proof. A first turn makes the conversation genuinely
    live on its sandbox; that sandbox is then terminated out from under it. After an
    explicit recover, a fresh sandbox is provisioned and the following turn streams a real
    model reply and settles ``READY``. The rebuilt sandbox id differs from the terminated
    one, so the turn provably ran on fresh compute — not the dead instance.

    It runs under **conversation** tenancy because that is the tenancy the claim
    belongs to: a box that belongs to one conversation dies with it. Under agent
    tenancy the box outlives the call by design — terminate closes this
    conversation's isolated session and leaves the Agent's box running
    (``RETAINED``, "a release, not a failure"), so recovering lands on the same
    id. Asserting a new id there would be wrong, and asserting the same id would
    be asserting that a shared box stayed itself.
    """
    created = create_agent_variant_session(
        e2e_client,
        name_suffix="reborrow",
        engine_options={},
        permission_mode=permission_mode("unattended"),
        environment_name=environment_with_tenancy(e2e_client, "conversation"),
    )
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id in create response: {created}"
    try:
        poll_until_agent_ready(e2e_client, sid)
        old_sandbox = _sandbox_id(get_session(e2e_client, sid))
        assert old_sandbox, "a READY session should hold a sandbox_id"

        # A first working turn so the conversation is genuinely live on old_sandbox.
        _reply_turn(
            e2e_client,
            sid,
            "Reply with exactly the single digit 2 and nothing else. Do not use any tool or command.",
            "2",
        )
        wait_until_settled(e2e_client, sid)

        # Reclaim that live sandbox, then confirm the session parks in the recoverable state.
        result = _terminate_sandbox(e2e_client, sid, sandbox_id=old_sandbox)
        assert str(result.get("sandbox_id") or "") == old_sandbox, (
            f"terminate reclaimed an unexpected sandbox (old={old_sandbox}): {result}"
        )
        _wait_runtime_unavailable(e2e_client, sid)

        # Recover the reclaimed session: this provisions a fresh sandbox (CREATING -> READY).
        data(e2e_client.post(f"/api/v1/sessions/{sid}/recover", timeout=60.0))
        poll_until_agent_ready(e2e_client, sid)

        # The next turn must complete end-to-end on the rebuilt sandbox. A unique marker
        # makes the reply assertion exact.
        marker = "RB" + uuid.uuid4().hex[:6].upper()
        _reply_turn(
            e2e_client,
            sid,
            f"Reply with exactly this text and nothing else: {marker}. Do not use any tool or command.",
            marker,
        )
        settled = wait_until_settled(e2e_client, sid)
        assert str(settled.get("state") or "") == "READY", (
            f"session not READY after the re-borrow turn: state={settled.get('state')!r}"
        )

        # The re-borrow is real. What "real" means is the tenancy's to say.
        new_sandbox = _sandbox_id(settled)
        assert new_sandbox, "the recovered session should hold a sandbox_id again"
        if result.get("killed"):
            # The reclaim really destroyed that box, so reusing its id would be
            # dispatching against a dead instance — the failure this case
            # exists to catch.
            assert new_sandbox != old_sandbox, (
                f"re-borrow must run on a NEW sandbox, not reuse the terminated one "
                f"(old={old_sandbox}, new={new_sandbox})"
            )
        else:
            # The box was kept for its Agent, so coming back INTO it is the
            # correct outcome rather than a reused corpse. The marker reply
            # above already proved the placement answers; what is left is that
            # the box it named is alive.
            assert box_is_running(sandbox_id=new_sandbox, client=e2e_client), (
                f"the recovered session names box {new_sandbox}, which is not running"
            )
    finally:
        release_session(sid)
