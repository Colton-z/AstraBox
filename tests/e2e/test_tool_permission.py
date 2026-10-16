"""Tool-permission interaction e2e for the user's approve and deny paths.

This is the end-to-end proof that a runtime holding a tool for the owner's
permission surfaces it as an interaction, and that the console's answer route
(``POST /sessions/{id}/interactions/{iid}/answer``) carries the decision back
to the runtime:

* **approve** — the agent's write is held; once approved the runtime runs the
  tool, so the file appears with the agent's content, and the response that
  paused on the approval continues to its end.
* **deny** — the held write is denied; the tool does **not** run, so the
  (uniquely named) file is never created. This is the load-bearing security
  assertion: a denied tool has no side effect.

The pending interaction surfaces two ways, and both are checked to agree: the
``data-interaction`` part on the session's stream, which pauses the response's
message with ``finish(tool-calls)``, and ``pending_interaction`` on the session
detail. The platform's own record of the interaction (``session_interactions``)
must end ``answered``: the runtime took the answer.

Run it explicitly (deselected in the default unit run):

    .venv/bin/python -m pytest tests/e2e/test_tool_permission.py -m e2e -s
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    Reply,
    answer_in_history,
    answer_interaction,
    approval_presentation,
    assert_permission_mode_unavailable,
    contract_supported,
    create_session,
    decision,
    file_names,
    follow_reply,
    get_session,
    history,
    pending_interaction,
    permission_mode,
    poll_until_agent_ready,
    psql_rows,
    release_session,
    send_and_follow,
    sql_literal,
    tool_names,
    user_messages,
    wait_for_file,
    wait_for_pending_interaction,
    wait_until_settled,
    workspace_path,
)

pytestmark = pytest.mark.e2e

#: Follow-on approvals an approved write may raise (a read-back, a second
#: edit) before its response ends; bounded so a runtime that asks forever fails.
_FOLLOW_ON_APPROVALS = 5


def _write_request(path: str, content: str) -> str:
    """Ask for one file write in words every engine's model can act on.

    The request names the action and nothing about how. The profile's write
    roles are what an approval carries, which for Codex are app-server item
    types (`commandExecution`, `fileChange`), not tools its model can call:
    asked to use one, it answered that no such tool exists. Told to run no
    other tool or command, a Codex session whose only way to write a file
    is its shell refused for that reason instead.
    """

    return f"Create a file at {path} containing {content}, and do nothing else."


def _interaction_record(sid: str, interaction_id: str) -> dict:
    rows = psql_rows(
        "SELECT interaction_id, response_id, status, answered_by FROM session_interactions "
        f"WHERE session_id = {sql_literal(sid)} AND interaction_id = {sql_literal(interaction_id)}"
    )
    assert len(rows) == 1, f"session {sid} recorded interaction {interaction_id} {len(rows)} times"
    return rows[0]


def _assert_modeless_write_has_no_gate(
    e2e_client: httpx.Client, sid: str, path: str, content: str
) -> None:
    """Prove an engine without tool approvals runs a write without a fake gate."""

    assert contract_supported("tool_approval") is False
    assert_permission_mode_unavailable(e2e_client, sid)
    reply = send_and_follow(
        e2e_client, sid, _write_request(path, f"exactly {content}"), until="end"
    )
    assert reply.error is None, f"modeless write failed: {reply.error}"
    assert not reply.interactions and "tool-calls" not in reply.response.finish_reasons, (
        f"an engine without tool approvals paused on an interaction: {reply.interactions}"
    )
    assert pending_interaction(e2e_client, sid) is None
    written = wait_for_file(e2e_client, sid, path, timeout=60.0)
    assert written is not None and written.strip(), f"modeless write did not produce {path}"
    wait_until_settled(e2e_client, sid)


def _paused_on_write(e2e_client: httpx.Client, sid: str, prompt: str) -> tuple[Reply, dict]:
    """Send a message that drives a write and return its reply paused on the approval.

    The response must pause on the write's approval — ``finish(tool-calls)``
    after a ``data-interaction`` part — and the session detail must name the
    same interaction, on the same response, as the one waiting on its owner.
    """
    reply = send_and_follow(e2e_client, sid, prompt, until="pause")
    assert reply.saw_ui_header, "the session stream is not an AI SDK UI message stream"
    assert reply.error is None, f"the response failed before the permission prompt: {reply.error}"
    assert reply.paused, (
        f"the response did not pause on an approval: finish={reply.finish_reason!r} "
        f"text={reply.text[:200]!r}"
    )
    assert reply.interactions, (
        f"the response paused without an interaction part: {reply.response.parts[-5:]}"
    )
    streamed = reply.interactions[-1]
    # The stream hears the pause as the runtime reports it; the detail reads
    # the recorded fact, which the reader commits a moment later.
    pending = wait_for_pending_interaction(e2e_client, sid, timeout=15.0)
    detail = get_session(e2e_client, sid)
    assert pending is not None, f"no pending interaction on the session detail: {detail}"
    assert detail.get("state") == "WAITING_INPUT", detail
    assert pending.get("interaction_id") == streamed.get("interaction_id"), (
        f"the stream and the detail name different interactions: {streamed} vs {pending}"
    )
    assert pending.get("response_id") == reply.response_id, (
        f"the pending interaction does not pause this response: {pending} vs {reply.response_id}"
    )
    expected = tool_names("write")
    assert str(pending.get("tool_name")) in expected, (
        f"expected one of {expected} as the permission prompt: {pending}"
    )
    assert str(pending.get("presentation")) == approval_presentation(), (
        f"expected a tool approval: {pending}"
    )
    return reply, pending


def _approve_until_ended(e2e_client: httpx.Client, reply: Reply, first_id: str) -> list[str]:
    """Approve the pending interaction and any follow-on ones until the response ends.

    Returns the ids approved. Each approval must reopen the same response: the
    reply is followed on the stream, not polled, so a response that never
    continues fails here with what the stream delivered.
    """
    sid = reply.session_id
    answered: list[str] = []
    interaction_id = first_id
    for _ in range(_FOLLOW_ON_APPROVALS):
        answer_interaction(e2e_client, sid, interaction_id, {"decision": decision("approve")})
        answered.append(interaction_id)
        follow_reply(e2e_client, reply, until="pause")
        if reply.ended:
            return answered
        interaction_id = str(reply.interactions[-1].get("interaction_id") or "")
        assert interaction_id and interaction_id not in answered, (
            f"the response paused again on an interaction already answered: {reply.interactions}"
        )
    pytest.fail(f"the response asked for more than {_FOLLOW_ON_APPROVALS} approvals: {answered}")


def test_tool_permission_approve_runs_the_write(e2e_client: httpx.Client) -> None:
    """A held write runs once approved, and the response it paused continues to its end."""
    created = create_session(e2e_client, permission_mode=permission_mode("gated"))
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id in create response: {created}"
    poem_path = workspace_path(e2e_client, sid, f"poem_{uuid.uuid4().hex[:8]}.txt")
    try:
        poll_until_agent_ready(e2e_client, sid)

        if not contract_supported("tool_approval"):
            _assert_modeless_write_has_no_gate(
                e2e_client, sid, poem_path, "a two-line poem about the sea"
            )
            return

        reply, pending = _paused_on_write(
            e2e_client,
            sid,
            _write_request(poem_path, "a two-line poem about the sea"),
        )
        # The approval a person sees must say something about what it is for.
        # Which FIELD carries it is the engine's: `raw_input` is its request
        # verbatim, deliberately un-normalised, so Claude Code puts the path in
        # `file_path` while Codex puts it in the `reason` it wrote for the
        # reader. Whether the target is in there at all is the engine's too,
        # and declared: the deepseek-harness asks to escalate the sandbox and
        # a Codex `fileChange` approval carries the item id and an optional
        # reason, never the path.
        if contract_supported("approval_names_target"):
            assert poem_path in json.dumps(pending.get("raw_input") or {}), (
                f"the approval does not name the file it is about: {pending.get('raw_input')}"
            )
        else:
            # Narrowed, not dropped: an approval a person cannot read at all is
            # a defect under every engine's vocabulary.
            described = str(
                (pending.get("raw_input") or {}).get("reason")
                or (pending.get("raw_input") or {}).get("prompt")
                or pending.get("prompt")
                or ""
            ).strip()
            assert described, (
                f"the approval says nothing about what it is for: {pending.get('raw_input')}"
            )

        # The file must NOT exist yet — the tool is still held pending approval.
        assert poem_path.rsplit("/", 1)[-1] not in file_names(e2e_client, sid), (
            "the write ran BEFORE approval (the permission gate did not hold the tool)"
        )

        approved = _approve_until_ended(e2e_client, reply, str(pending["interaction_id"]))
        assert reply.error is None and reply.finish_reason == "stop", (
            f"the approved response did not run to its normal end: finish={reply.finish_reason!r} "
            f"error={reply.error!r} outcome={reply.outcome!r}"
        )

        # The approved write actually executed: the file exists with the agent's content.
        content = wait_for_file(e2e_client, sid, poem_path, timeout=60.0)
        assert content is not None, f"approved write did not produce {poem_path}"
        text = content.decode("utf-8", "replace").strip()
        lines = [ln for ln in text.splitlines() if ln.strip()]
        assert len(lines) >= 2, f"expected a two-line poem, got {len(lines)} line(s): {text!r}"

        settled = wait_until_settled(e2e_client, sid)
        assert settled["last_response"]["response_id"] == reply.response_id, settled
        assert settled["last_response"]["outcome"] == "completed", settled
        for interaction_id in approved:
            record = _interaction_record(sid, interaction_id)
            assert record["status"] == "answered" and record["response_id"] == reply.response_id, (
                f"the runtime did not take the approval it acted on: {record}"
            )

        # History shows the message once and its one answer, as it ended.
        messages = history(e2e_client, sid)
        assert [m["id"] for m in user_messages(messages)] == [reply.client_message_id], messages
        answer = answer_in_history(messages, reply.input_id)
        assert answer["id"] == reply.response_id, answer
        assert answer["metadata"].get("outcome") == "completed", answer["metadata"]
        # A reloaded page shows the decision on the call it was about, as the
        # page that approved it did. The engine's record names that call when
        # its approval is attached to one.
        tool_call_id = str(pending.get("tool_call_id") or "")
        if pending.get("presentation") == "tool_approval" and tool_call_id:
            calls = [part for part in answer["parts"] if part.get("toolCallId") == tool_call_id]
            assert len(calls) == 1, f"history does not hold the approved call once: {answer['parts']}"
            assert calls[0].get("approval") == {"id": pending["interaction_id"], "approved": True}, (
                f"history lost the approval on the call it was about: {calls[0]}"
            )
    finally:
        release_session(sid)


def test_tool_permission_deny_blocks_the_write(e2e_client: httpx.Client) -> None:
    """A held write that is denied never runs, and its response still ends."""
    created = create_session(e2e_client, permission_mode=permission_mode("gated"))
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id in create response: {created}"
    deny_path = workspace_path(e2e_client, sid, f"deny_{uuid.uuid4().hex[:8]}.txt")
    deny_name = deny_path.rsplit("/", 1)[-1]
    try:
        poll_until_agent_ready(e2e_client, sid)

        if not contract_supported("tool_approval"):
            _assert_modeless_write_has_no_gate(e2e_client, sid, deny_path, "BANANA")
            assert deny_name in file_names(e2e_client, sid), (
                "a modeless engine unexpectedly exposed a deny gate for its write"
            )
            return

        reply, pending = _paused_on_write(
            e2e_client,
            sid,
            _write_request(deny_path, "the single word BANANA"),
        )
        interaction_id = str(pending["interaction_id"])

        # A decision card lists the answers this request accepts and marks the
        # ones that deny. The engine narrows them per request: a Codex command
        # approval has offered accept and cancel with no decline. So the denial
        # comes from the request when the engine's usual reject word is not
        # among its denials.
        denials = [
            str(option.get("id"))
            for option in pending.get("options") or []
            if isinstance(option, dict) and option.get("denial") is True
        ]
        deny = decision("reject")
        if denials and deny not in denials:
            deny = denials[0]
        answer_interaction(e2e_client, sid, interaction_id, {"decision": deny, "comment": "no thanks"})

        # The response continues past the denial without the tool. Once it has
        # ended nothing of it can still write the file, so its absence then is
        # the whole answer rather than a sample of a polling window.
        follow_reply(e2e_client, reply, until="end")
        wait_until_settled(e2e_client, sid)
        assert deny_name not in file_names(e2e_client, sid), (
            f"denied write still created {deny_path} (the deny did not block the tool)"
        )
        record = _interaction_record(sid, interaction_id)
        assert record["status"] == "answered", (
            f"the runtime did not take the denial: {record}"
        )
    finally:
        release_session(sid)
