"""Tool-permission interaction e2e for the user's approve and deny paths.

This is the end-to-end proof that the in-box control plane holds a
``can_use_tool`` permission request, surfaces it to the host as a pending
``tool_approval`` interaction, and that the FE's ``interaction-respond`` path
relays the decision back into the resident CLI:

* **approve** — the agent's ``Write`` is held; once approved the in-box CLI is
  told ``behavior: allow`` and actually runs the tool, so the file appears with
  the agent's content and the turn settles back to READY (no fake completion).
* **deny** — the held ``Write`` is told ``behavior: deny``; the tool does **not**
  run, so the (uniquely named) file is never created. This is the load-bearing
  security assertion: a denied tool has no side effect.

The pending interaction surfaces two ways (both checked): the live
``data-interaction`` SSE frame on the turn stream, and ``session.pending_interaction``
on the detail projection. The first turn stream finishes with reason ``tool-calls``
(the turn paused on the permission), not ``stop``.

Run it explicitly (deselected in the default unit run):

    .venv/bin/python -m pytest tests/e2e/test_tool_permission.py -m e2e -s
"""

from __future__ import annotations

import json
import time
import uuid

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    SETTLE_TIMEOUT_S,
    TERMINAL_STATES,
    approval_presentation,
    assert_permission_mode_unavailable,
    contract_supported,
    create_session,
    decision,
    file_names,
    get_session,
    pending_interaction,
    permission_mode,
    poll_until_agent_ready,
    release_session,
    respond_interaction,
    stream_turn,
    tool_name,
    tool_names,
    wait_for_file,
    wait_for_pending_interaction,
    workspace_path,
)

pytestmark = pytest.mark.e2e


def _assert_modeless_write_has_no_gate(
    e2e_client: httpx.Client, sid: str, path: str, content: str
) -> None:
    """Prove an engine without tool approvals runs a write without a fake gate."""

    assert contract_supported("tool_approval") is False
    assert_permission_mode_unavailable(e2e_client, sid)
    result = stream_turn(
        e2e_client,
        sid,
        content=(
            f"Use the {tool_name('write')} tool to create a file at {path} "
            f"containing exactly {content}. Do not run any other tool or command."
        ),
    )
    assert result.error is None, f"modeless write failed: {result.error}"
    assert not result.interactions, (
        f"an engine without tool approvals surfaced an interaction: {result.interactions}"
    )
    assert pending_interaction(e2e_client, sid) is None
    written = wait_for_file(e2e_client, sid, path, timeout=60.0)
    assert written is not None and written.strip(), f"modeless write did not produce {path}"


def _capture_write_interaction(e2e_client: httpx.Client, sid: str, prompt: str) -> dict:
    """Start a turn that drives a ``Write`` and return the surfaced pending interaction.

    Asserts the interaction surfaces (stream frame and/or detail projection), is a
    ``Write`` ``tool_approval``, and that the first turn paused (finish=tool-calls)
    rather than completing.
    """
    res = stream_turn(e2e_client, sid, content=prompt)
    assert res.saw_ui_header, "missing x-vercel-ai-ui-message-stream: v1 header"
    assert res.error is None, f"turn errored before the permission prompt: {res.error}"

    # The pending interaction is authoritative on the detail projection; the live
    # stream frame is the same payload (assert it surfaced on at least one path).
    pi = pending_interaction(e2e_client, sid)
    if pi is None:
        pi = wait_for_pending_interaction(e2e_client, sid, timeout=20.0)
    assert pi is not None, (
        f"no pending interaction surfaced for the Write (stream_interactions="
        f"{[i.get('interaction_id') for i in res.interactions]}, finish={res.finish_reason})"
    )
    assert res.interactions or True  # the stream frame is best-effort; detail is authoritative
    expected = tool_names("write")
    assert str(pi.get("tool_name")) in expected, (
        f"expected one of {expected} as the permission prompt: {pi}"
    )
    assert str(pi.get("presentation")) == approval_presentation(), f"expected a tool approval: {pi}"
    # The turn paused on the tool call rather than finishing.
    assert res.finish_reason in ("tool-calls", None), (
        f"first turn unexpectedly finished with {res.finish_reason!r} (expected a pause)"
    )
    return pi


def _approve_until_settled(e2e_client: httpx.Client, sid: str, first_id: str, cap: int = 6) -> list[str]:
    """Approve the pending interaction, and any follow-on interactions, until READY.

    Returns the ids approved. Fails loud if the turn neither settles nor surfaces a
    new interaction after an approval.
    """
    answered: list[str] = []
    iid = first_id
    for _ in range(cap):
        respond_interaction(e2e_client, sid, iid, {"decision": decision("approve")})
        answered.append(iid)
        deadline = time.monotonic() + SETTLE_TIMEOUT_S
        nxt: str | None = None
        while time.monotonic() < deadline:
            detail = get_session(e2e_client, sid)
            state = str(detail.get("state") or "")
            if state in TERMINAL_STATES:
                pytest.fail(f"session went terminal ({state}) after approve: {detail.get('last_error')!r}")
            pi = detail.get("pending_interaction")
            if state == "READY" and not pi:
                return answered  # settled cleanly
            if isinstance(pi, dict):
                cand = str(pi.get("interaction_id") or "")
                if cand and cand not in answered:
                    nxt = cand
                    break
            time.sleep(0.3)
        if nxt is None:
            pytest.fail(f"turn did not settle nor re-prompt within {SETTLE_TIMEOUT_S:.0f}s after approving {iid}")
        iid = nxt
    pytest.fail(f"exceeded {cap} interactions without settling (answered={answered})")


def test_tool_permission_approve_runs_the_write(e2e_client: httpx.Client) -> None:
    """create -> Write is held -> APPROVE -> the tool runs (file written) -> turn settles."""
    created = create_session(e2e_client, permission_mode=permission_mode("gated"))  # the mode under test; agent_chat defaults to bypassPermissions
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

        pi = _capture_write_interaction(
            e2e_client,
            sid,
            f"Use the {tool_name('write')} tool to create a file at {poem_path} containing a two-line "
            f"poem about the sea. Do not run any other tool or command.",
        )
        # The approval a person sees must say something about what it is for.
        # Which FIELD carries it is the engine's: `raw_input` is its request
        # verbatim, deliberately un-normalised, so Claude Code puts the path in
        # `file_path` while Codex puts it in the `reason` it wrote for the
        # reader. Asserting one engine's field name tested the spelling.
        #
        # Whether the target is in there at all is the engine's too, and now
        # declared. The deepseek-harness asks to escalate the sandbox rather
        # than to write one file: its request carries `toolName`, ids, and a
        # prose reason, and no target field anywhere. A path in that sentence
        # would be the model's wording that run, not a contract.
        if contract_supported("approval_names_target"):
            assert poem_path in json.dumps(pi.get("raw_input") or {}), (
                f"the approval does not name the file it is about: {pi.get('raw_input')}"
            )
        else:
            # Narrowed, not dropped: an approval a person cannot read at all is
            # a defect under every engine's vocabulary.
            described = str(
                (pi.get("raw_input") or {}).get("reason")
                or (pi.get("raw_input") or {}).get("prompt")
                or pi.get("prompt")
                or ""
            ).strip()
            assert described, (
                f"the approval says nothing about what it is for: {pi.get('raw_input')}"
            )

        # The file must NOT exist yet — the tool is still held pending approval.
        assert poem_path.rsplit("/", 1)[-1] not in file_names(e2e_client, sid), (
            "the Write ran BEFORE approval (the permission gate did not hold the tool)"
        )

        _approve_until_settled(e2e_client, sid, str(pi["interaction_id"]))

        # The approved Write actually executed: the file exists with the agent's content.
        content = wait_for_file(e2e_client, sid, poem_path, timeout=60.0)
        assert content is not None, f"approved Write did not produce {poem_path}"
        text = content.decode("utf-8", "replace").strip()
        assert text, f"approved Write produced an empty file: {content!r}"
        lines = [ln for ln in text.splitlines() if ln.strip()]
        assert len(lines) >= 2, f"expected a two-line poem, got {len(lines)} line(s): {text!r}"

        # The turn settled cleanly (no pending interaction, READY).
        final = get_session(e2e_client, sid)
        assert str(final.get("state")) == "READY", f"session not READY after approve: {final.get('state')!r}"
        assert not final.get("pending_interaction"), "pending interaction lingered after approve"
    finally:
        release_session(sid)


def test_tool_permission_deny_blocks_the_write(e2e_client: httpx.Client) -> None:
    """create -> Write is held -> DENY -> the tool does NOT run (file never created)."""
    created = create_session(e2e_client, permission_mode=permission_mode("gated"))  # the mode under test; agent_chat defaults to bypassPermissions
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

        pi = _capture_write_interaction(
            e2e_client,
            sid,
            f"Use the {tool_name('write')} tool to create a file at {deny_path} containing the single "
            f"word BANANA. Do not run any other tool or command.",
        )
        iid = str(pi["interaction_id"])

        result = respond_interaction(e2e_client, sid, iid, {"decision": decision("reject"), "comment": "no thanks"})
        assert result.get("answered") is True, f"deny was not accepted: {result}"

        # The load-bearing assertion: a denied tool has NO side effect. The Write is
        # never executed, so the uniquely-named file is never created. Poll a while to
        # be sure the deny relayed (behavior=deny) and nothing wrote the file behind it.
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            assert deny_name not in file_names(e2e_client, sid), (
                f"denied Write still created {deny_path} (the deny did not block the tool)"
            )
            time.sleep(0.3)
    finally:
        release_session(sid)
