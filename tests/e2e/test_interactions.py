"""Decision and form interaction end-to-end coverage.

``test_tool_permission.py`` covers the ``tool_approval`` presentation. This
file drives the engines' decision and form interactions end-to-end through the
same answer route the console uses
(``POST /sessions/{id}/interactions/{iid}/answer``):

* **ExitPlanMode** (session in ``plan`` mode) → a ``decision`` interaction
  carrying the proposed plan as its ``body``; APPROVE it (with a follow-on
  ``permission_mode``) and the paused response runs to its end.
* **AskUserQuestion** → a ``form`` interaction carrying ``questions`` +
  ``options``; answer it by ``option_label`` and the paused response runs to
  its end.
* **Codex Plan** → a native ``request_user_input`` form; answer it and prove
  the response resumes under Codex's own Plan collaboration preset.

Each interaction pauses its response on the session's stream (a
``data-interaction`` part, then ``finish(tool-calls)``) and is the one the
session detail names as waiting on its owner; the answer reopens the same
response, which then ends ``completed``. Together they prove that each
adapter's declared contract survives to the browser and that the answer reaches
the runtime, not just the approval path.

Run it explicitly (deselected in the default unit run):

    .venv/bin/python -m pytest tests/e2e/test_interactions.py -m e2e -s
"""

from __future__ import annotations

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
    Reply,
    answer_interaction,
    approval_presentation,
    assert_permission_mode_unavailable,
    contract_supported,
    create_agent_variant_session,
    create_session,
    current_profile,
    data,
    decision,
    engine_capabilities,
    engine_kind,
    follow_reply,
    pending_interaction,
    permission_mode,
    poll_until_agent_ready,
    release_session,
    send_and_follow,
    tool_name,
    wait_for_pending_interaction,
    wait_until_settled,
)

pytestmark = pytest.mark.e2e


def _waiting_interaction(
    e2e_client: httpx.Client, reply: Reply, *, exclude_id: str | None = None
) -> dict:
    """The interaction the paused response waits on, as the stream and the detail both name it.

    The stream hears the pause as the reader reads it; the detail reads what
    the reader recorded, so it is polled for the same interaction.
    """
    sid = reply.session_id
    assert reply.error is None, f"the response failed before the interaction: {reply.error}"
    assert reply.paused, (
        f"the response did not pause on an interaction: finish={reply.finish_reason!r} "
        f"text={reply.text[:200]!r}"
    )
    streamed = reply.interactions[-1]
    pending = wait_for_pending_interaction(e2e_client, sid, exclude_id=exclude_id, timeout=20.0)
    assert pending is not None, (
        f"the session detail names no interaction waiting on its owner; the stream "
        f"paused on {streamed}"
    )
    assert pending.get("interaction_id") == streamed.get("interaction_id"), (
        f"the stream and the detail name different interactions: {streamed} vs {pending}"
    )
    assert pending.get("response_id") == reply.response_id, (
        f"the waiting interaction does not pause this response: {pending} vs {reply.response_id}"
    )
    return pending


def _surface_interaction(e2e_client: httpx.Client, sid: str, prompt: str) -> tuple[Reply, dict]:
    reply = send_and_follow(e2e_client, sid, prompt, until="pause")
    return reply, _waiting_interaction(e2e_client, reply)


def _answered_to_the_end(e2e_client: httpx.Client, reply: Reply) -> None:
    """Follow the answered response to its end and require the conversation at rest."""
    follow_reply(e2e_client, reply, until="end")
    assert reply.error is None, f"the response failed after its answer: {reply.error}"
    final = wait_until_settled(e2e_client, reply.session_id)
    assert not final.get("pending_interaction")
    last = final["last_response"]
    assert last["response_id"] == reply.response_id and last["outcome"] == "completed", (
        f"the answered response did not end completed: {last}"
    )


def test_exit_plan_mode_approve_settles(e2e_client: httpx.Client) -> None:
    """plan mode -> ExitPlanMode decision -> APPROVE -> the response runs to its end."""
    plan_mode = permission_mode("plan")
    created = create_session(e2e_client, permission_mode=plan_mode)
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id: {created}"
    try:
        poll_until_agent_ready(e2e_client, sid)

        if not contract_supported("plan_interaction"):
            assert plan_mode is None
            capabilities = engine_capabilities(e2e_client, sid)
            modes = capabilities.get("permission_modes")
            assert isinstance(modes, list)
            assert "plan" not in modes, (
                "the live adapter advertises a plan mode while the matrix says "
                f"plan interaction is unavailable: {modes}"
            )
            response = e2e_client.post(
                f"/api/v1/sessions/{sid}/permission-mode",
                json={"permission_mode": "plan"},
            )
            body = response.json()
            expected_code = (
                "INVALID_REQUEST" if contract_supported("permission_modes")
                else "ENGINE_CAPABILITY_UNAVAILABLE"
            )
            assert response.status_code == 400 and body.get("code") == expected_code, (
                "an engine without plan interaction accepted Claude's plan mode: "
                f"{response.status_code} {response.text[:400]}"
            )
            return

        reply, pi = _surface_interaction(
            e2e_client,
            sid,
            "Make a short two-step plan to create a file hello.txt, then call the "
            "ExitPlanMode tool to present that plan. Do not do the work yet.",
        )
        # The CLI may legally stage the plan on disk first (a Write into
        # ~/.claude/plans/), and plan mode gates that write as an ordinary
        # tool approval. Approve such leading gates — bounded, so a model
        # that never presents the plan still fails — until the plan decision
        # itself surfaces. The oracle is the decision and the response's end,
        # not which incidental steps the vendor took on the way there.
        for _ in range(2):
            if str(pi.get("presentation")) != approval_presentation():
                break
            approved = str(pi["interaction_id"])
            answer_interaction(e2e_client, sid, approved, {"decision": decision("approve")})
            follow_reply(e2e_client, reply, until="pause")
            pi = _waiting_interaction(e2e_client, reply, exclude_id=approved)
        assert str(pi.get("tool_name")) == "ExitPlanMode", f"expected ExitPlanMode: {pi}"
        assert str(pi.get("presentation")) == "decision", f"expected a decision: {pi}"
        assert str(pi.get("body") or "").strip(), f"the decision carried no plan text: {pi}"

        answer_interaction(
            e2e_client,
            sid,
            str(pi["interaction_id"]),
            # bypassPermissions, so the work the approved plan then performs
            # (writes AND read-backs) raises no further gates: this test pins
            # the decision mechanics and the response's end, not the
            # permission matrix of the post-approval work.
            {"decision": decision("approve"), "permission_mode": permission_mode("unattended")},
        )

        # Approving exits plan mode and the same response proceeds to its end.
        _answered_to_the_end(e2e_client, reply)
    finally:
        release_session(sid)


def test_ask_user_question_answer_settles(e2e_client: httpx.Client) -> None:
    """AskUserQuestion form -> answer by option_label -> the response runs to its end."""
    # agent_chat defaults to bypassPermissions, which the vendor documents as
    # shadowing can_use_tool. This contract exercises the native user-input
    # callback, so select the same permission mode as the canonical browser E2E.
    created = create_session(e2e_client, permission_mode=permission_mode("gated"))
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id: {created}"
    try:
        poll_until_agent_ready(e2e_client, sid)

        if not contract_supported("question_interaction"):
            capabilities = engine_capabilities(e2e_client, sid)
            if contract_supported("permission_modes"):
                gated = permission_mode("gated")
                assert gated in (capabilities.get("permission_modes") or [])
            else:
                assert permission_mode("gated") is None
                assert_permission_mode_unavailable(e2e_client, sid)
            ordinary = send_and_follow(
                e2e_client,
                sid,
                "If this engine has a built-in user-question tool, use it to ask "
                "whether I prefer RED or BLUE. Otherwise reply with exactly "
                "NO_QUESTION_TOOL.",
                until="pause",
            )
            assert ordinary.error is None, ordinary.error
            assert ordinary.ended and not ordinary.interactions, (
                "an engine whose adapter declares no question interaction paused on "
                f"one: {ordinary.interactions}"
            )
            assert ordinary.text.strip(), "modeless question probe produced no response"
            assert wait_until_settled(e2e_client, sid).get("pending_interaction") is None
            return

        # The prompt names the tool as well as the assertion. Unlike a file
        # write, which an engine performs whatever the prompt calls the tool,
        # asking the USER is something the model only does through the one tool
        # that exists for it — name a tool the engine does not have and it
        # answers the question itself.
        asking = tool_name("question")
        reply, pi = _surface_interaction(
            e2e_client,
            sid,
            f"Use the {asking} tool to ask which accent color to use in a sample chart: RED or "
            "BLUE. Ask exactly one question offering those two options. After I answer, "
            "acknowledge the selected color and finish. This choice is only for this reply; "
            "do not save it to memory, read or write files, or use any other tools.",
        )
        assert str(pi.get("tool_name")) == asking, f"expected {asking}: {pi}"
        assert str(pi.get("presentation")) == "form", f"expected a form: {pi}"
        questions = pi.get("questions") or []
        assert questions and isinstance(questions, list), f"the form carried no questions: {pi}"
        q0 = questions[0]
        qid = str(q0.get("id") or "").strip()
        options = q0.get("options") or []
        assert qid and options, f"question missing id/options: {q0}"
        chosen = str(options[0].get("label") or "").strip()
        assert chosen, f"first option has no label: {q0}"

        answer_interaction(
            e2e_client,
            sid,
            str(pi["interaction_id"]),
            {"answers": [{"question_id": qid, "option_label": chosen}]},
        )

        # The answer reaches the runtime and the same response runs to its end.
        _answered_to_the_end(e2e_client, reply)
    finally:
        release_session(sid)


def test_codex_plan_mode_question_answer_settles(e2e_client: httpx.Client) -> None:
    """Codex Plan exposes request_user_input and resumes after its answer."""

    profile = current_profile()
    environment_options = data(
        e2e_client.get("/api/v1/agent-configuration/runtime-profiles")
    )
    assert isinstance(environment_options, list), environment_options
    matches = [
        item
        for item in environment_options
        if item.get("name") == profile["runtime_profile_name"]
    ]
    assert len(matches) == 1, matches
    option_schema = matches[0].get("engine_options_schema") or []
    collaboration_fields = [
        field
        for field in option_schema
        if isinstance(field, dict) and field.get("key") == "turn_start"
    ]
    if engine_kind() != "codex":
        assert collaboration_fields == [], (
            "a non-Codex adapter exposed Codex collaboration mode configuration: "
            f"{collaboration_fields}"
        )
        return
    assert len(collaboration_fields) == 1
    assert collaboration_fields[0].get("type") == "object"
    assert "item_schema" not in collaboration_fields[0]
    created = create_agent_variant_session(
        e2e_client,
        name_suffix="Plan E2E",
        engine_options={"turn_start": {"collaborationMode": {
            "mode": "plan",
            "settings": {"reasoning_effort": None, "developer_instructions": None},
        }}},
        permission_mode=permission_mode("gated"),
    )
    sid = str(created.get("session_id") or "")
    assert sid, f"no session_id: {created}"
    try:
        poll_until_agent_ready(e2e_client, sid)
        reply, pi = _surface_interaction(
            e2e_client,
            sid,
            "Plan a one-page research-note template with three sections: "
            "Summary, Evidence, and Risks. This is only a generic layout, "
            "not research on any company or market; no files or external "
            "information are needed. The only undecided detail is the "
            "heading color. Use request_user_input to ask whether I prefer "
            "RED or BLUE, with exactly one question and those two options. "
            "After I answer, finish with a short two-step plan using the "
            "chosen color. Do not implement it.",
        )
        assert str(pi.get("tool_name")) == "request_user_input", pi
        assert str(pi.get("presentation")) == "form", pi
        questions = pi.get("questions") or []
        assert isinstance(questions, list) and len(questions) == 1, pi
        question = questions[0]
        question_id = str(question.get("id") or "").strip()
        options = question.get("options") or []
        assert question_id and isinstance(options, list) and options, question
        option_label = str(options[0].get("label") or "").strip()
        assert option_label, question

        answer_interaction(
            e2e_client,
            sid,
            str(pi["interaction_id"]),
            {
                "answers": [
                    {
                        "question_id": question_id,
                        "option_label": option_label,
                    }
                ]
            },
        )
        _answered_to_the_end(e2e_client, reply)
        assert pending_interaction(e2e_client, sid) is None
    finally:
        release_session(sid)
