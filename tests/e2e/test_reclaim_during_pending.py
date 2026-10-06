"""Sandbox reclaim during a pending tool approval — clean, non-hanging recovery.

Reclaiming a session's sandbox is a COMPUTE action, not a conversation end. The
tricky moment is a reclaim that lands in the narrow window where a turn is
*paused waiting for a tool-permission approval*: the turn is held open, an
interaction is outstanding, and the sandbox that held the paused tool call is
taken away. This suite pins the platform's contract for that moment across two
proofs:

* **The reclaim is accepted and keeps the conversation wakeable.** Terminating
  the sandbox while an approval is pending is never rejected as busy, and leaves
  the session non-terminal with ``runtime_unavailable`` set — a recoverable
  degradation, not a dead end.

* **Answering the orphaned interaction never strands the turn.** Approving the
  interaction after the reclaim cannot deliver to the dead compute, but the turn
  must still resolve: within the settle window the session returns to ``READY``
  with no pending interaction (never stuck ``BUSY``/waiting forever), and the
  session is immediately sendable again — a fresh short turn reaches a terminal
  result (it completes, or fails loud with a clear error) instead of hanging.

The load-bearing guarantee is liveness: a reclaim in the waiting-for-approval
window must not leave the conversation permanently stuck.

Run it explicitly (deselected in the default unit run):

    .venv/bin/python -m pytest tests/e2e/test_reclaim_during_pending.py -m e2e -s
"""

from __future__ import annotations

import time
import uuid

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    TERMINAL_STATES,
    approval_presentation,
    assert_permission_mode_unavailable,
    contract_supported,
    create_session,
    data,
    decision,
    get_session,
    pending_interaction,
    permission_mode,
    poll_until_agent_ready,
    release_session,
    stream_turn,
    tool_name,
    tool_names,
    wait_for_pending_interaction,
    wait_until_settled,
    workspace_path,
)

pytestmark = pytest.mark.e2e


def _drive_write_to_pending(e2e_client: httpx.Client, sid: str, prompt: str) -> dict:
    """Start a turn that drives a ``Write`` and return the surfaced pending interaction.

    The prompt pins the exact tool (``Write``) and file path so the turn deterministically
    pauses on a single ``tool_approval`` request. Asserts the turn paused
    (finish=tool-calls) rather than completing, so the caller can reclaim the sandbox
    while the tool call is genuinely held open.
    """
    res = stream_turn(e2e_client, sid, content=prompt)
    assert res.saw_ui_header, "missing x-vercel-ai-ui-message-stream: v1 header"
    assert res.error is None, f"turn errored before the permission prompt: {res.error}"

    pi = pending_interaction(e2e_client, sid)
    if pi is None:
        pi = wait_for_pending_interaction(e2e_client, sid, timeout=20.0)
    assert pi is not None, (
        f"no pending interaction surfaced for the Write (finish={res.finish_reason}, "
        f"stream_interactions={[i.get('interaction_id') for i in res.interactions]})"
    )
    expected = tool_names("write")
    assert str(pi.get("tool_name")) in expected, (
        f"expected one of {expected} as the permission prompt: {pi}"
    )
    assert str(pi.get("presentation")) == approval_presentation(), f"expected a tool approval: {pi}"
    assert res.finish_reason in ("tool-calls", None), (
        f"first turn unexpectedly finished with {res.finish_reason!r} (expected a pause on the tool)"
    )
    return pi


def _reclaim_sandbox(e2e_client: httpx.Client, sid: str) -> dict:
    """Reclaim the session's sandbox while an approval is pending.

    A reclaim is a compute action and must be accepted even while a turn is paused on
    an approval. Returns the reclaim result (``status == "sandbox-reclaimed"``).
    A backend that refuses this as busy violates the compute-reclaim contract.
    """
    resp = e2e_client.post(f"/api/v1/sessions/{sid}/sandbox/terminate", timeout=60.0)
    result = data(resp)
    assert str(result.get("session_id") or "") == sid, (
        f"terminate targeted the wrong session: {result}"
    )
    assert str(result.get("status") or "") == "sandbox-reclaimed", (
        f"reclaim did not report sandbox-reclaimed: {result}"
    )
    return result


def _wait_runtime_unavailable(e2e_client: httpx.Client, sid: str, timeout: float = 45.0) -> dict:
    """Poll session detail until it reports the reclaimed (``runtime_unavailable``) state.

    A reclaimed sandbox must surface as a recoverable degradation, so this fails loud if
    the session instead reaches a terminal state, and fails loud on timeout. Returns the
    settled detail.
    """
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = get_session(e2e_client, sid)
        state = str(last.get("state") or "")
        if state in TERMINAL_STATES:
            pytest.fail(
                f"session {sid} went terminal ({state}) after a reclaim while an approval was "
                f"pending; the conversation must stay wakeable (last_error={last.get('last_error')!r})"
            )
        if bool(last.get("runtime_unavailable")):
            return last
        time.sleep(0.3)
    pytest.fail(
        f"session {sid} never reported runtime_unavailable within {timeout:.0f}s after a "
        f"mid-approval reclaim; state={last.get('state')!r}"
    )


def test_reclaim_during_pending_then_approve_recovers_without_hang(e2e_client: httpx.Client) -> None:
    """A reclaim mid-approval settles the parked turn; the orphaned answer is refused loudly.

    The approval wait lived in-memory in the reclaimed box, so an answer after the
    reclaim has nothing to deliver to — accepting it would be a lie. The contract
    (the spike-proven defer semantics): the reclaim itself settles the parked turn
    (no pending interaction survives it), answering afterwards is refused with a
    clear 4xx, and the session is immediately sendable again — the next message
    resumes the conversation and the engine re-requests the approval. The
    load-bearing guarantee is liveness: never stuck waiting/busy forever.
    """
    created = create_session(e2e_client, permission_mode=permission_mode("gated"))  # the mode under test; agent_chat defaults to bypassPermissions
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id in create response: {created}"
    write_path = workspace_path(e2e_client, sid, f"reclaim_{uuid.uuid4().hex[:8]}.txt")
    try:
        poll_until_agent_ready(e2e_client, sid)
        if not contract_supported("tool_approval"):
            assert_permission_mode_unavailable(e2e_client, sid)
            _reclaim_sandbox(e2e_client, sid)
            _wait_runtime_unavailable(e2e_client, sid)
            response = e2e_client.post(
                f"/api/v1/sessions/{sid}/interaction-respond",
                json={
                    "interaction_id": "missing-interaction",
                    "answer": {"decision": "approve"},
                },
            )
            assert response.status_code in (400, 409), (
                "an engine without tool approvals accepted an interaction answer: "
                f"{response.status_code} {response.text[:300]}"
            )
            settled = wait_until_settled(e2e_client, sid)
            assert settled.get("pending_interaction") is None
            followup = stream_turn(
                e2e_client,
                sid,
                content="Reply with the single word READY. Do not use any tool.",
            )
            assert followup.error is None, (
                "the modeless engine did not recover for the next turn after reclaim: "
                f"{followup.error}"
            )
            assert followup.text.strip(), (
                "the modeless engine recovered without producing a usable response"
            )
            wait_until_settled(e2e_client, sid)
            return
        pi = _drive_write_to_pending(
            e2e_client,
            sid,
            f"Use the {tool_name('write')} tool to create a file at {write_path} containing the single "
            f"word ANCHOR. Do not run any other tool or command.",
        )
        iid = str(pi["interaction_id"])

        _reclaim_sandbox(e2e_client, sid)
        _wait_runtime_unavailable(e2e_client, sid)

        # The orphaned answer is refused loudly — the approval wait died with the
        # box. Either refusal is legitimate depending on how far the reclaim's
        # settle has converged: 400 (the pending interaction is absent) or 409
        # (the runtime is detached). A 200 would claim an approval nobody can deliver.
        resp = e2e_client.post(
            f"/api/v1/sessions/{sid}/interaction-respond",
            json={"interaction_id": iid, "answer": {"decision": decision("approve")}},
        )
        assert resp.status_code in (400, 409), (
            f"answering a reclaimed-sandbox interaction must be refused, got "
            f"{resp.status_code}: {resp.text[:300]}"
        )

        # Load-bearing liveness: the reclaim settled the parked turn, so the session
        # reaches READY with no pending interaction within the settle window.
        # wait_until_settled fails loud on a terminal state or on timeout — the
        # stuck-forever case this test guards against.
        settled = wait_until_settled(e2e_client, sid)
        assert not settled.get("pending_interaction"), (
            f"interaction stayed pending after the mid-approval reclaim settled: {settled}"
        )

        # Sendable again: a fresh short turn reaches a terminal result instead of hanging.
        # On a still-reclaimed sandbox this surfaces a loud error frame; a re-provisioned
        # sandbox completes — both are terminal, and only a hang fails this assertion.
        followup = stream_turn(
            e2e_client,
            sid,
            content="Reply with the single word READY. Do not use any tool or command.",
        )
        assert followup.finish_reason is not None or followup.error is not None, (
            "the follow-up turn produced neither a finish nor an error frame — the session "
            "hung on the next send after the reclaim"
        )
    finally:
        release_session(sid)
