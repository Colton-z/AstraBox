"""The LEGACY interaction-answer path is refused; the command path settles it.

The FE once answered a held tool permission by POSTing the decision back on
the SAME ``/ai-stream`` endpoint as ``interaction_response`` — dispatching a
NEW turn to carry the answer. In the engine-client world that funnel is a
design refusal, not a fallback: answers go through the interaction-answer
command (``/interaction-respond`` → ``answer_pending_interaction`` →
``engine_client.submit_interaction_response``, a live side-channel into the ORIGINAL
turn's worker), and a turn dispatch carrying ``interaction_response`` is
answered ``409 ENGINE_ANSWER_VIA_ANSWER_COMMAND``
(``session_kernel/service_mixins/turn_dispatch.py``). The FE has exactly one
answer path (``frontend/src/api.ts::answerPendingInteraction``).

This file pins BOTH halves against a live deployment, in one session:

* **the refusal** — with a ``Write`` genuinely held, the legacy POST answers
  409 with the named code, and the interaction is still pending afterwards
  (the refused dispatch must not consume it);
* **the real path** — ``/interaction-respond`` approves the same interaction,
  the held ``Write`` actually runs (the uniquely-named file appears), and the
  turn settles back to READY.

The full approve/deny matrix over the command path lives in
``test_tool_permission.py``; this file exists for the refusal semantics.

Run it explicitly (deselected in the default unit run):

    .venv/bin/python -m pytest tests/e2e/test_interaction_ai_stream.py -m e2e -s
"""

from __future__ import annotations

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


def _capture_write_interaction(e2e_client: httpx.Client, sid: str, prompt: str) -> dict:
    """Start a turn that drives a ``Write`` and return the surfaced pending interaction.

    Asserts the interaction surfaces (detail projection is authoritative), is a
    ``Write`` ``tool_approval``, and that the first turn paused (finish=tool-calls)
    rather than completing.
    """
    res = stream_turn(e2e_client, sid, content=prompt)
    assert res.saw_ui_header, "missing x-vercel-ai-ui-message-stream: v1 header"
    assert res.error is None, f"turn errored before the permission prompt: {res.error}"

    pi = pending_interaction(e2e_client, sid)
    if pi is None:
        pi = wait_for_pending_interaction(e2e_client, sid, timeout=20.0)
    assert pi is not None, (
        f"no pending interaction surfaced for the Write (stream_interactions="
        f"{[i.get('interaction_id') for i in res.interactions]}, finish={res.finish_reason})"
    )
    expected = tool_names("write")
    assert str(pi.get("tool_name")) in expected, (
        f"expected one of {expected} as the permission prompt: {pi}"
    )
    assert str(pi.get("presentation")) == approval_presentation(), f"expected a tool approval: {pi}"
    assert res.finish_reason in ("tool-calls", None), (
        f"first turn unexpectedly finished with {res.finish_reason!r} (expected a pause)"
    )
    return pi


def test_legacy_ai_stream_answer_is_refused_and_the_command_path_settles(
    e2e_client: httpx.Client,
) -> None:
    """Write held -> legacy /ai-stream answer 409s (interaction survives) ->
    /interaction-respond approves -> the tool runs -> READY."""
    created = create_session(e2e_client, permission_mode=permission_mode("gated"))
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id in create response: {created}"
    poem_path = workspace_path(e2e_client, sid, f"poem_{uuid.uuid4().hex[:8]}.txt")
    try:
        poll_until_agent_ready(e2e_client, sid)

        if not contract_supported("tool_approval"):
            assert_permission_mode_unavailable(e2e_client, sid)
            response = e2e_client.post(
                f"/api/v1/sessions/{sid}/ai-stream",
                json={
                    "content": "",
                    "interaction_response": {
                        "interaction_id": "missing-interaction",
                        "decision": "approve",
                    },
                },
                headers={"Accept": "text/event-stream"},
            )
            assert response.status_code == 409
            assert "ENGINE_ANSWER_VIA_ANSWER_COMMAND" in response.text
            assert pending_interaction(e2e_client, sid) is None
            return

        pi = _capture_write_interaction(
            e2e_client,
            sid,
            f"Use the {tool_name('write')} tool to create a file at {poem_path} containing a two-line "
            f"poem about the sea. Do not run any other tool or command.",
        )
        iid = str(pi["interaction_id"])

        # The file must NOT exist yet — the tool is held pending the answer.
        assert poem_path.rsplit("/", 1)[-1] not in file_names(e2e_client, sid), (
            "the Write ran BEFORE the answer (the permission gate did not hold the tool)"
        )

        # ── the refusal: a turn dispatch must not carry the answer ──────────
        resp = e2e_client.post(
            f"/api/v1/sessions/{sid}/ai-stream",
            json={
                "content": "",
                "interaction_response": {"interaction_id": iid, "decision": decision("approve")},
            },
            headers={"Accept": "text/event-stream"},
        )
        assert resp.status_code == 409, (
            f"legacy ai-stream answer must 409, got {resp.status_code}: {resp.text[:300]}"
        )
        assert "ENGINE_ANSWER_VIA_ANSWER_COMMAND" in resp.text, (
            f"expected the named refusal code in the body: {resp.text[:300]}"
        )
        # The refused dispatch must not have consumed the interaction.
        still = pending_interaction(e2e_client, sid)
        assert still is not None and str(still.get("interaction_id")) == iid, (
            f"the refused answer consumed the pending interaction: {still}"
        )

        # ── the real path: the command answers, the tool actually runs ──────
        # Approve until settled: the model may chain further tool calls after
        # the approved Write (each surfacing its own interaction), so a single
        # approve is not a settle — mirror test_tool_permission's loop.
        answered = [iid]
        respond_interaction(e2e_client, sid, iid, {"decision": decision("approve")})
        deadline = time.monotonic() + SETTLE_TIMEOUT_S
        final = get_session(e2e_client, sid)
        while time.monotonic() < deadline:
            final = get_session(e2e_client, sid)
            state = str(final.get("state") or "")
            assert state not in TERMINAL_STATES, (
                f"session went terminal ({state}) after approve: {final.get('last_error')!r}"
            )
            nxt = final.get("pending_interaction")
            if isinstance(nxt, dict):
                cand = str(nxt.get("interaction_id") or "")
                if cand and cand not in answered:
                    assert len(answered) < 6, f"exceeded 6 interactions: {answered}"
                    answered.append(cand)
                    respond_interaction(e2e_client, sid, cand, {"decision": decision("approve")})
            elif state == "READY":
                break
            time.sleep(0.3)

        content = wait_for_file(e2e_client, sid, poem_path, timeout=60.0)
        assert content is not None, f"approved Write did not produce {poem_path}"
        assert content.decode("utf-8", "replace").strip(), (
            f"approved Write produced an empty file: {content!r}"
        )
        assert str(final.get("state")) == "READY", (
            f"session not READY after the command-path approve: {final.get('state')!r}"
        )
        assert not final.get("pending_interaction"), (
            "pending interaction lingered after the approve"
        )
    finally:
        release_session(sid)
