"""Sandbox reclaim during a pending tool approval — clean, non-hanging recovery.

Reclaiming a session's sandbox is a COMPUTE action, not a conversation end. The
tricky moment is a reclaim that lands while a response is *paused waiting for a
tool-permission approval*: the response is held open, an interaction is
outstanding, and the sandbox that held the paused tool call is taken away. This
suite pins the platform's contract for that moment:

* **The reclaim is accepted and keeps the conversation wakeable.** Terminating
  the sandbox while an approval is pending is not refused as busy (the runtime is
  waiting, not working), and leaves the session non-terminal with
  ``runtime_unavailable`` set — a recoverable degradation, not a dead end.

* **The paused response ends with the box.** The platform records it ``failed``
  for ``sandbox_lost`` and tells the session's stream so; the interaction it was
  waiting on is closed unanswered, so answering it afterwards is refused with
  ``INTERACTION_NOT_OPEN`` — an approval nobody can deliver is never accepted.

* **The conversation goes on.** The session settles with nothing waiting on the
  owner, and its next message is answered on a live box.

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
    Reply,
    answer_in_history,
    answer_interaction_refusal,
    approval_presentation,
    assert_permission_mode_unavailable,
    contract_supported,
    create_session,
    data,
    deadline_after,
    decision,
    follow_reply,
    get_session,
    history,
    pending_interaction,
    permission_mode,
    poll_until_agent_ready,
    psql_rows,
    recorded_response,
    release_session,
    send_and_follow,
    sql_literal,
    tool_name,
    tool_names,
    wait_until_settled,
    workspace_path,
)

pytestmark = pytest.mark.e2e


def _paused_on_write(e2e_client: httpx.Client, sid: str, prompt: str) -> tuple[Reply, dict]:
    """Send a message that drives a write and return its reply paused on the approval.

    The prompt pins the exact tool and file path so the response deterministically
    pauses on a single ``tool_approval`` request, and the session detail must
    name that interaction on that response as the one waiting on its owner — so
    the caller can reclaim the sandbox while the tool call is genuinely held open.
    """
    reply = send_and_follow(e2e_client, sid, prompt, until="pause")
    assert reply.saw_ui_header, "the session stream is not an AI SDK UI message stream"
    assert reply.error is None, f"the response failed before the permission prompt: {reply.error}"
    assert reply.paused, (
        f"the response did not pause on the write's approval: finish={reply.finish_reason!r} "
        f"text={reply.text[:200]!r}"
    )
    pending = pending_interaction(e2e_client, sid)
    assert pending is not None, (
        f"no pending interaction on the session detail (streamed: {reply.interactions})"
    )
    assert pending.get("response_id") == reply.response_id, (
        f"the pending interaction does not pause this response: {pending} vs {reply.response_id}"
    )
    assert pending.get("interaction_id") == reply.interactions[-1].get("interaction_id"), (
        f"the stream and the detail name different interactions: {reply.interactions[-1]} vs {pending}"
    )
    expected = tool_names("write")
    assert str(pending.get("tool_name")) in expected, (
        f"expected one of {expected} as the permission prompt: {pending}"
    )
    assert str(pending.get("presentation")) == approval_presentation(), f"expected a tool approval: {pending}"
    return reply, pending


def _reclaim_sandbox(e2e_client: httpx.Client, sid: str) -> dict:
    """Reclaim the session's sandbox while an approval is pending.

    A reclaim is a compute action; it is refused only while the runtime reports
    it is working, and a runtime waiting on its owner is not. Returns the
    reclaim result (``status == "sandbox-reclaimed"``).
    """
    result = data(e2e_client.post(f"/api/v1/sessions/{sid}/sandbox/terminate", timeout=60.0))
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
    detail.
    """
    deadline = deadline_after(timeout)
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
        f"session {sid} never reported runtime_unavailable after a mid-approval reclaim; "
        f"state={last.get('state')!r}"
    )


def _assert_the_next_message_is_answered(e2e_client: httpx.Client, sid: str) -> None:
    """The conversation goes on: its next message is answered after the reclaim, on a live box."""
    followup = send_and_follow(
        e2e_client,
        sid,
        "Reply with the single word READY. Do not use any tool or command.",
        until="end",
    )
    assert followup.error is None and followup.finish_reason == "stop", (
        f"the next message after the reclaim was not answered: finish={followup.finish_reason!r} "
        f"error={followup.error!r} outcome={followup.outcome!r}"
    )
    assert followup.text.strip(), "the next message's response carried no text"
    settled = wait_until_settled(e2e_client, sid)
    assert settled["last_response"]["response_id"] == followup.response_id, settled
    assert settled["last_response"]["outcome"] == "completed", settled
    assert str(settled.get("sandbox_id") or "") and not settled.get("runtime_unavailable"), (
        f"the answered conversation has no live box: {settled}"
    )


def test_reclaim_during_pending_then_approve_recovers_without_hang(e2e_client: httpx.Client) -> None:
    """A reclaim mid-approval ends the paused response; the orphaned answer is refused loudly.

    The approval wait lived in the reclaimed box, so an answer after the reclaim
    has nothing to deliver to — accepting it would be a lie. The contract: the
    reclaim ends the paused response (``failed``, ``sandbox_lost``) and closes
    its interaction unanswered, answering afterwards is refused with ``409
    INTERACTION_NOT_OPEN``, and the session is immediately sendable again — the
    next message is answered on a live box. The load-bearing guarantee is
    liveness: never stuck waiting or busy forever.
    """
    created = create_session(e2e_client, permission_mode=permission_mode("gated"))
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id in create response: {created}"
    write_path = workspace_path(e2e_client, sid, f"reclaim_{uuid.uuid4().hex[:8]}.txt")
    try:
        poll_until_agent_ready(e2e_client, sid)
        if not contract_supported("tool_approval"):
            assert_permission_mode_unavailable(e2e_client, sid)
            _reclaim_sandbox(e2e_client, sid)
            _wait_runtime_unavailable(e2e_client, sid)
            status, code = answer_interaction_refusal(
                e2e_client, sid, "missing-interaction", {"decision": "approve"}
            )
            assert (status, code) == (404, "INTERACTION_NOT_FOUND"), (
                "an engine without tool approvals accepted an interaction answer: "
                f"status={status} code={code!r}"
            )
            settled = wait_until_settled(e2e_client, sid)
            assert settled.get("pending_interaction") is None
            _assert_the_next_message_is_answered(e2e_client, sid)
            return

        paused, pending = _paused_on_write(
            e2e_client,
            sid,
            f"Use the {tool_name('write')} tool to create a file at {write_path} containing the single "
            f"word ANCHOR. Do not run any other tool or command.",
        )
        interaction_id = str(pending["interaction_id"])
        response_id = str(paused.response_id)

        _reclaim_sandbox(e2e_client, sid)
        _wait_runtime_unavailable(e2e_client, sid)

        # The paused response ends with the box, on the stream as the console
        # follows it: an error, then a finish the platform wrote naming it.
        follow_reply(e2e_client, paused, until="end")
        assert paused.outcome == "failed" and paused.error, (
            f"the stream ended the lost response without saying why: outcome={paused.outcome!r} "
            f"error={paused.error!r} finish={paused.finish_reason!r}"
        )
        record = recorded_response(sid, response_id)
        assert (record["status"], record["outcome"], record["native_reason"]) == (
            "ended", "failed", "sandbox_lost",
        ), f"the platform did not record the reclaim on the paused response: {record}"
        interaction = psql_rows(
            "SELECT status FROM session_interactions "
            f"WHERE session_id = {sql_literal(sid)} AND interaction_id = {sql_literal(interaction_id)}"
        )
        assert len(interaction) == 1 and interaction[0]["status"] not in ("open", "answered"), (
            f"the lost response's interaction is still waiting or was answered: {interaction}"
        )

        # The orphaned answer is refused loudly — the approval wait died with the box.
        status, code = answer_interaction_refusal(
            e2e_client, sid, interaction_id, {"decision": decision("approve")}
        )
        assert (status, code) == (409, "INTERACTION_NOT_OPEN"), (
            f"answering a reclaimed-sandbox interaction must be refused, got {status} {code!r}"
        )

        # Load-bearing liveness: the session settles with nothing waiting on the
        # owner. wait_until_settled fails loud on a terminal state or on timeout —
        # the stuck-forever case this test guards against.
        settled = wait_until_settled(e2e_client, sid)
        assert not settled.get("pending_interaction"), (
            f"interaction stayed pending after the mid-approval reclaim: {settled}"
        )
        assert settled["last_response"]["response_id"] == response_id, settled
        assert settled["last_response"]["outcome"] == "failed", settled
        lost = answer_in_history(history(e2e_client, sid), paused.input_id)
        assert lost["metadata"].get("native_reason") == "sandbox_lost", lost["metadata"]

        _assert_the_next_message_is_answered(e2e_client, sid)
    finally:
        release_session(sid)
