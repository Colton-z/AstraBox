"""Decision and form interaction end-to-end coverage.

``test_tool_permission.py`` covers the ``tool_approval`` presentation. This
file drives the engines' decision and form interactions end-to-end through the
same public ``interaction-respond`` path:

* **ExitPlanMode** (session in ``plan`` mode) → a ``decision`` interaction
  carrying the proposed plan as its ``body``; APPROVE it (with a follow-on
  ``permission_mode``) and the turn settles.
* **AskUserQuestion** → a ``form`` interaction carrying ``questions`` +
  ``options``; answer it by ``option_label`` and the turn settles.
* **Codex Plan** → a native ``request_user_input`` form; answer it and prove
  the turn resumes under Codex's own Plan collaboration preset.

Together they prove that each adapter's declared contract survives to the
browser and that the decision/answer relay works through the engine control
connection, not just the approval path.

Run it explicitly (deselected in the default unit run):

    .venv/bin/python -m pytest tests/e2e/test_interactions.py -m e2e -s
"""

from __future__ import annotations

import httpx
import pytest

from tests.e2e._sandbox_helpers import (
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
    pending_interaction,
    permission_mode,
    poll_until_agent_ready,
    release_session,
    respond_interaction,
    stream_turn,
    tool_name,
    wait_for_pending_interaction,
    wait_until_settled,
)

pytestmark = pytest.mark.e2e


def _surface_interaction(e2e_client: httpx.Client, sid: str, prompt: str) -> dict:
    res = stream_turn(e2e_client, sid, content=prompt)
    assert res.error is None, f"turn errored before the interaction: {res.error}"
    pi = pending_interaction(e2e_client, sid) or wait_for_pending_interaction(e2e_client, sid, timeout=20.0)
    assert pi is not None, (
        f"no pending interaction surfaced (stream_interactions="
        f"{[i.get('tool_name') for i in res.interactions]}, finish={res.finish_reason})"
    )
    return pi


def test_exit_plan_mode_approve_settles(e2e_client: httpx.Client) -> None:
    """plan mode -> ExitPlanMode decision -> APPROVE -> turn settles."""
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

        pi = _surface_interaction(
            e2e_client,
            sid,
            "Make a short two-step plan to create a file hello.txt, then call the "
            "ExitPlanMode tool to present that plan. Do not do the work yet.",
        )
        # The CLI may legally stage the plan on disk first (a Write into
        # ~/.claude/plans/), and plan mode gates that write as an ordinary
        # tool approval. Approve such leading gates — bounded, so a model
        # that never presents the plan still fails — until the plan decision
        # itself surfaces. The oracle is the decision and its settle, not
        # which incidental steps the vendor took on the way there.
        for _ in range(2):
            if str(pi.get("presentation")) != approval_presentation():
                break
            result = respond_interaction(
                e2e_client,
                sid,
                str(pi["interaction_id"]),
                {"decision": decision("approve")},
            )
            assert result.get("answered") is True, f"leading gate not accepted: {result}"
            pi = wait_for_pending_interaction(
                e2e_client,
                sid,
                exclude_id=str(pi["interaction_id"]),
                timeout=30.0,
            )
            assert pi is not None, "no further interaction after approving a leading gate"
        assert str(pi.get("tool_name")) == "ExitPlanMode", f"expected ExitPlanMode: {pi}"
        assert str(pi.get("presentation")) == "decision", f"expected a decision: {pi}"
        assert str(pi.get("body") or "").strip(), f"the decision carried no plan text: {pi}"

        result = respond_interaction(
            e2e_client,
            sid,
            str(pi["interaction_id"]),
            # bypassPermissions, so the work the approved plan then performs
            # (writes AND read-backs) raises no further gates: this test pins
            # the decision mechanics and the settle, not the permission
            # matrix of the post-approval work.
            {"decision": decision("approve"), "permission_mode": permission_mode("unattended")},
        )
        assert result.get("answered") is True, f"plan approval not accepted: {result}"

        # Approving exits plan mode and the turn proceeds to completion.
        final = wait_until_settled(e2e_client, sid)
        assert str(final.get("state")) == "READY"
        assert not final.get("pending_interaction")
    finally:
        release_session(sid)


def test_ask_user_question_answer_settles(e2e_client: httpx.Client) -> None:
    """AskUserQuestion form -> answer by option_label -> turn settles."""
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
            ordinary = stream_turn(
                e2e_client,
                sid,
                content=(
                    "If this engine has a built-in user-question tool, use it to ask "
                    "whether I prefer RED or BLUE. Otherwise reply with exactly "
                    "NO_QUESTION_TOOL."
                ),
            )
            assert ordinary.error is None, ordinary.error
            assert not ordinary.interactions, (
                "an engine whose adapter declares no question interaction surfaced "
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
        pi = _surface_interaction(
            e2e_client,
            sid,
            f"Use the {asking} tool to ask me whether I prefer the color RED or "
            "BLUE. Ask exactly one question offering those two options.",
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

        result = respond_interaction(
            e2e_client,
            sid,
            str(pi["interaction_id"]),
            {"answers": [{"question_id": qid, "option_label": chosen}]},
        )
        assert result.get("answered") is True, f"form answer not accepted: {result}"

        # The answer is relayed to the CLI and the turn runs to completion.
        final = wait_until_settled(e2e_client, sid)
        assert str(final.get("state")) == "READY"
        assert not final.get("pending_interaction")
    finally:
        release_session(sid)


def test_codex_plan_mode_question_answer_settles(e2e_client: httpx.Client) -> None:
    """Codex Plan exposes request_user_input and resumes after its answer."""

    profile = current_profile()
    environment_options = data(
        e2e_client.get("/api/v1/agent-configuration/environments")
    )
    assert isinstance(environment_options, list), environment_options
    matches = [
        item
        for item in environment_options
        if item.get("name") == profile["environment_name"]
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
        pi = _surface_interaction(
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

        answered = respond_interaction(
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
        assert answered.get("answered") is True, answered
        final = wait_until_settled(e2e_client, sid)
        assert str(final.get("state")) == "READY"
        assert not final.get("pending_interaction")
    finally:
        release_session(sid)
