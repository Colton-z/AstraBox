"""The interaction seam end to end: shared mechanics, per-record identity.

These cover the seam as a whole rather than one module: an adapter declares a
structural contract, the platform validates and persists it, a browser answer
is validated against that record, and the record's own ``engine_kind`` selects
the adapter that encodes the native reply.  Every assertion here is about that
round trip; the vendor halves (Claude's codec, Hermes's approval RPC) are
driven through the same shared path a live turn uses.
"""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any, get_args
from unittest.mock import AsyncMock

import pytest
from claude_agent_sdk.types import PermissionMode

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.base import EngineTurnReceipt
from astrabox.core.service.orchestrator.engine.claude_interaction_codec import (
    build_claude_interaction_contract,
    claude_answer_fields,
    normalize_pending_preset_answers,
)
from astrabox.core.service.orchestrator.engine.hermes_client import (
    HermesTuiEngineClient,
    encode_turn_anchor,
    translate_tui_event,
)
from astrabox.core.service.orchestrator.engine.interaction_contract import (
    PRESENTATION_DECISION,
    PRESENTATION_FORM,
    PRESENTATION_TOOL_APPROVAL,
    InteractionContractError,
    build_pending_interaction_record,
    public_interaction_view,
    render_interaction_reply,
    resolve_interaction_permission_mode,
    validate_interaction_contract,
    validate_interaction_response,
)
from astrabox.core.service.orchestrator.runtime.pty_terminal import (
    ResolvedExecdEndpoint,
)

#: Claude's own words. Nothing in a Hermes flow may carry any of them.
_CLAUDE_VOCABULARY = (
    "AskUserQuestion",
    "ExitPlanMode",
    "bypassPermissions",
    "acceptEdits",
    "dontAsk",
)


def _receipt(record: dict[str, Any]) -> EngineTurnReceipt:
    return EngineTurnReceipt(
        engine_turn_id=str(record.get("engine_turn_id") or ""),
        engine_session_key=None,
        started_at_monotonic_ns=0,
        input_consumed=True,
    )


def _declare(
    *,
    contract: dict[str, Any],
    engine_kind: str,
    interaction_id: str,
    tool_call_id: str | None = None,
    store: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the platform's declaration path for one adapter contract.

    Mirrors ``engine_turn``'s ``interaction.request`` handler: validate the
    adapter's declaration, wrap it in the platform envelope, then tag the
    engine handles the answer path routes on.  ``store`` stands in for the
    durable session record, so a test can assert what did or did not reach it.
    """
    validate_interaction_contract(contract)
    record = build_pending_interaction_record(
        contract=contract,
        session_id=f"session-{engine_kind}",
        turn_id=f"turn-{engine_kind}",
        interaction_id=interaction_id,
        tool_call_id=tool_call_id,
    )
    record["engine_kind"] = engine_kind
    record["engine_turn_id"] = f"engine-turn-{engine_kind}"
    if store is not None:
        store.append(record)
    return record


class _RecordingEngineClient:
    """An adapter that records exactly what the seam handed it."""

    def __init__(self, engine_kind: str) -> None:
        self.engine_kind = engine_kind
        self.submissions: list[tuple[dict[str, Any], dict[str, Any]]] = []

    async def submit_interaction_response(
        self,
        receipt: EngineTurnReceipt,
        *,
        pending: dict[str, Any],
        response: dict[str, Any],
    ) -> bool:
        self.submissions.append((deepcopy(pending), deepcopy(response)))
        return True


async def _answer(
    clients: dict[str, _RecordingEngineClient],
    record: dict[str, Any],
    response: dict[str, Any],
) -> bool:
    """Mirror the answer path: structure first, then the record's own adapter."""
    validate_interaction_response(record, response)
    client = clients[str(record["engine_kind"])]
    return await client.submit_interaction_response(
        _receipt(record), pending=record, response=response
    )


@pytest.mark.asyncio
async def test_two_engines_share_a_native_tool_name_without_colliding() -> None:
    """Two engines may declare the same native tool name and different contracts.

    There is deliberately NO global name registry to collide in — identity is
    scoped per record: the durable record carries ``engine_kind``, the verbatim
    native ``tool_name``, and the structural ``presentation`` the answer is
    checked against, so the same name means one thing in one engine and
    something else in the other without either engine learning about the other.
    """

    alpha = _RecordingEngineClient("alpha_engine")
    beta = _RecordingEngineClient("beta_engine")
    clients = {alpha.engine_kind: alpha, beta.engine_kind: beta}

    alpha_record = _declare(
        contract={
            "tool_name": "Deploy",
            "presentation": PRESENTATION_FORM,
            "prompt": "Please answer the following questions",
            "raw_input": {"release": "2026.08"},
            "questions": [
                {
                    "id": "environment",
                    "header": "Environment",
                    "question": "Which environment should this release reach?",
                    "native_answer_key": "Which environment should this release reach?",
                    "multi_select": False,
                    "allow_free_text": True,
                    "allow_empty_text": False,
                    "options": [{"label": "staging"}, {"label": "production"}],
                }
            ],
            "preset_answers": None,
        },
        engine_kind=alpha.engine_kind,
        interaction_id="alpha-1",
    )
    beta_record = _declare(
        contract={
            "tool_name": "Deploy",
            "presentation": PRESENTATION_TOOL_APPROVAL,
            "prompt": "Allow Deploy to continue?",
            "raw_input": {"command": "deploy --prod"},
        },
        engine_kind=beta.engine_kind,
        interaction_id="beta-1",
        tool_call_id="call-beta-1",
    )

    alpha_answer = {"answers": [{"question_id": "environment", "option_labels": ["production"]}]}
    beta_answer = {"decision": "approve"}
    assert await _answer(clients, alpha_record, alpha_answer) is True
    assert await _answer(clients, beta_record, beta_answer) is True

    assert len(alpha.submissions) == 1
    assert len(beta.submissions) == 1
    alpha_pending, alpha_seen = alpha.submissions[0]
    beta_pending, beta_seen = beta.submissions[0]

    # Each answer reached its own adapter and only its own adapter.
    assert alpha_seen == alpha_answer
    assert beta_seen == beta_answer
    assert alpha_pending["interaction_id"] == "alpha-1"
    assert beta_pending["interaction_id"] == "beta-1"

    # Each record kept its own engine, its own presentation, and the SAME
    # verbatim native name.
    assert alpha_pending["engine_kind"] == "alpha_engine"
    assert beta_pending["engine_kind"] == "beta_engine"
    assert alpha_pending["presentation"] == PRESENTATION_FORM
    assert beta_pending["presentation"] == PRESENTATION_TOOL_APPROVAL
    assert alpha_pending["tool_name"] == "Deploy"
    assert beta_pending["tool_name"] == "Deploy"

    # The other engine's answer shape is refused against this record, and is
    # refused BEFORE routing, so it never reaches any adapter.
    with pytest.raises(APIError):
        await _answer(clients, beta_record, alpha_answer)
    with pytest.raises(APIError):
        await _answer(clients, alpha_record, beta_answer)
    assert len(alpha.submissions) == 1
    assert len(beta.submissions) == 1


def _valid_tool_approval_contract() -> dict[str, Any]:
    return {
        "tool_name": "Deploy",
        "presentation": PRESENTATION_TOOL_APPROVAL,
        "prompt": "Allow Deploy to continue?",
        "raw_input": {"command": "deploy --prod"},
    }


def _unknown_presentation_contract() -> dict[str, Any]:
    contract = _valid_tool_approval_contract()
    # `plan_confirmation` was the retired global semantic vocabulary; it is
    # not a structural presentation and must not be silently accepted.
    contract["presentation"] = "plan_confirmation"
    return contract


def _decision_option_without_reply_contract() -> dict[str, Any]:
    return {
        "tool_name": "Deploy",
        "presentation": PRESENTATION_DECISION,
        "prompt": "Please confirm the rollout",
        "raw_input": {"plan": "ship it"},
        "body": "ship it",
        "options": [
            {"id": "approve", "denial": False, "reply": "Approved."},
            {"id": "reject", "denial": True},
        ],
    }


def _unknown_contract_keys_contract() -> dict[str, Any]:
    contract = _valid_tool_approval_contract()
    # The retired payload vocabulary: a global `kind` plus a `choices` list.
    contract["kind"] = "approval"
    contract["choices"] = [{"id": "approve"}, {"id": "reject"}]
    return contract


def _duplicate_question_id_contract() -> dict[str, Any]:
    question = {
        "id": "environment",
        "header": "Environment",
        "question": "Which environment?",
        "native_answer_key": "Which environment?",
        "multi_select": False,
        "allow_free_text": True,
        "allow_empty_text": False,
        "options": [{"label": "staging"}],
    }
    return {
        "tool_name": "Deploy",
        "presentation": PRESENTATION_FORM,
        "prompt": "Please answer the following questions",
        "raw_input": {"questions": []},
        "questions": [dict(question), dict(question)],
        "preset_answers": None,
    }


def _repair_presentation(contract: dict[str, Any]) -> None:
    contract["presentation"] = PRESENTATION_TOOL_APPROVAL


def _repair_missing_reply(contract: dict[str, Any]) -> None:
    contract["options"][1]["reply"] = "Rejected."


def _repair_unknown_keys(contract: dict[str, Any]) -> None:
    contract.pop("kind")
    contract.pop("choices")


def _repair_duplicate_question_id(contract: dict[str, Any]) -> None:
    contract["questions"][1]["id"] = "region"


@pytest.mark.parametrize(
    ("build_contract", "repair", "message_fragment"),
    [
        pytest.param(
            _unknown_presentation_contract,
            _repair_presentation,
            "unknown presentation 'plan_confirmation'",
            id="unknown-presentation",
        ),
        pytest.param(
            _decision_option_without_reply_contract,
            _repair_missing_reply,
            "option 'reject' must declare a non-empty 'reply'",
            id="decision-without-reply",
        ),
        pytest.param(
            _unknown_contract_keys_contract,
            _repair_unknown_keys,
            "unknown keys ['choices', 'kind']",
            id="unknown-contract-keys",
        ),
        pytest.param(
            _duplicate_question_id_contract,
            _repair_duplicate_question_id,
            "duplicate question id 'environment'",
            id="duplicate-question-id",
        ),
    ],
)
def test_a_malformed_declaration_fails_before_the_record_exists(
    build_contract: Any,
    repair: Any,
    message_fragment: str,
) -> None:
    """A drifting adapter fails its turn at declaration, not at answer time.

    The record is the only reading of the interaction anyone downstream will
    hold, so a shape no validator can answer must never reach the store: the
    failure has to land while the turn can still report it.  Each case is also
    validated once repaired, so the rejection is provably caused by the
    planted defect rather than by an unrelated flaw in the fixture.
    """

    store: list[dict[str, Any]] = []
    with pytest.raises(InteractionContractError) as raised:
        _declare(
            contract=build_contract(),
            engine_kind="alpha_engine",
            interaction_id="alpha-1",
            store=store,
        )
    assert message_fragment in str(raised.value)
    assert store == []

    repaired = build_contract()
    repair(repaired)
    _declare(
        contract=repaired,
        engine_kind="alpha_engine",
        interaction_id="alpha-1",
        store=store,
    )
    assert len(store) == 1


def _ask_user_question_payload() -> dict[str, Any]:
    return {
        "questions": [
            {
                "header": "Database",
                "question": "Which datastore should the service use?",
                "multiSelect": False,
                "options": [
                    {"label": "postgres", "description": "Managed cluster"},
                    {"label": "sqlite", "description": "Local file"},
                ],
            },
            {
                "header": "Regions",
                "multiSelect": True,
                "options": [{"label": "us-east-1"}, {"label": "eu-west-1"}],
            },
            {
                "multiSelect": False,
                "options": [{"label": "yes"}, {"label": "no"}],
            },
        ],
        "answers": {
            # One preset per key form the SDK may use: the question text, the
            # header, and the platform's own synthesized question id.
            "Which datastore should the service use?": "postgres",
            "Regions": "us-east-1",
            "question_3": "yes",
        },
    }


def test_ask_user_question_round_trips_its_exact_native_keys() -> None:
    """The SDK-bound answer is keyed by the native question text, verbatim.

    ``AskUserQuestion`` answers ride inside the tool's effective input, and the
    CLI matches them by the question's own text — so the key derivation
    (question → header → id) and the untouched questions list are a vendor
    contract, not a formatting choice.
    """

    payload = _ask_user_question_payload()
    contract = build_claude_interaction_contract(
        tool_name="AskUserQuestion", input_payload=payload
    )
    record = _declare(
        contract=contract,
        engine_kind="claude_code",
        interaction_id="claude-1",
        tool_call_id="toolu_01",
    )
    assert record["presentation"] == PRESENTATION_FORM
    assert record["tool_name"] == "AskUserQuestion"

    questions = record["questions"]
    assert [row["id"] for row in questions] == ["question_1", "question_2", "question_3"]
    assert [row["native_answer_key"] for row in questions] == [
        "Which datastore should the service use?",  # question text wins
        "Regions",  # no question text: the header
        "question_3",  # neither: the synthesized id
    ]
    assert [row["multi_select"] for row in questions] == [False, True, False]
    assert questions[0]["options"] == [
        {"label": "postgres", "description": "Managed cluster"},
        {"label": "sqlite", "description": "Local file"},
    ]

    # Preset answers normalize onto question ids whichever key form the SDK
    # used — the same three candidate lookups the funnel performed before.
    assert record["preset_answers"] == {
        "question_1": "postgres",
        "question_2": "us-east-1",
        "question_3": "yes",
    }
    assert record["preset_answers"] == normalize_pending_preset_answers(
        raw_questions=payload["questions"],
        raw_answers=payload["answers"],
    )

    response = {
        "answers": [
            {"question_id": "question_1", "option_label": "postgres"},
            {"question_id": "question_2", "option_labels": ["us-east-1", "eu-west-1"]},
            {"question_id": "question_3", "free_text": "yes, with a canary"},
        ]
    }
    validate_interaction_response(record, response)
    choice, updated_input, message = claude_answer_fields(record, response)

    assert (choice, message) == ("allow", None)
    assert updated_input is not None
    # The questions list is returned verbatim — same content, fresh object, so
    # the durable record cannot be mutated through the SDK payload.
    assert updated_input["questions"] == payload["questions"]
    assert updated_input["questions"] is not payload["questions"]
    assert updated_input["answers"] == {
        "Which datastore should the service use?": "postgres",
        "Regions": "us-east-1, eu-west-1",
        "question_3": "yes, with a canary",
    }


def _exit_plan_mode_record() -> dict[str, Any]:
    contract = build_claude_interaction_contract(
        tool_name="ExitPlanMode",
        input_payload={"plan": "1. add the gate\n2. run it"},
    )
    return _declare(
        contract=contract,
        engine_kind="claude_code",
        interaction_id="claude-plan-1",
        tool_call_id="toolu_02",
    )


def test_exit_plan_mode_resolves_only_sdk_declared_permission_modes() -> None:
    """Every mode the plan card can produce is a name the pinned SDK declares.

    ``PermissionLifecycle`` validates the returned string against the engine's
    manifest, so a mode outside the vendor's own union is a request no engine
    can honour.
    """

    record = _exit_plan_mode_record()
    sdk_modes = set(get_args(PermissionMode))

    declared_choices = {
        choice
        for option in record["options"]
        for choice in (option.get("permission_mode_choices") or [])
    }
    assert declared_choices
    assert declared_choices <= sdk_modes

    resolved = {
        "approve-with-mode": resolve_interaction_permission_mode(
            record, {"decision": "approve", "permission_mode": "acceptEdits"}
        ),
        "approve-without-mode": resolve_interaction_permission_mode(
            record, {"decision": "approve"}
        ),
        "revise": resolve_interaction_permission_mode(record, {"decision": "revise"}),
        "reject": resolve_interaction_permission_mode(record, {"decision": "reject"}),
    }
    assert resolved == {
        "approve-with-mode": "acceptEdits",
        "approve-without-mode": "default",
        "revise": "plan",
        "reject": "plan",
    }
    assert set(resolved.values()) <= sdk_modes


def test_exit_plan_mode_replies_are_byte_exact() -> None:
    """The plan replies are a parser contract on both sides of the transcript.

    ``tool_result_semantics`` matches the denial copy as a substring and the
    console matches it exactly, so these bytes may only change together with
    every consumer that reads them.
    """

    record = _exit_plan_mode_record()

    assert render_interaction_reply(record, {"decision": "approve"}) == (
        "Response to your pending plan confirmation:\n"
        "I approve exiting plan mode. Proceed with the plan you proposed."
    )
    assert render_interaction_reply(record, {"decision": "reject"}) == (
        "Response to your pending plan confirmation:\n"
        "User rejected Claude's plan."
    )
    assert render_interaction_reply(
        record, {"decision": "revise", "comment": "cover the rollback"}
    ) == (
        "Response to your pending plan confirmation:\n"
        "Do not exit plan mode yet. Stay in plan mode and continue refining "
        "the plan.\n"
        "Focus next on: cover the rollback"
    )
    assert render_interaction_reply(
        record, {"decision": "reject", "comment": "start over"}
    ) == (
        "Response to your pending plan confirmation:\n"
        "User rejected Claude's plan.\n"
        "Additional notes: start over"
    )

    # A denial carries that exact copy to the model as its feedback.
    choice, updated_input, message = claude_answer_fields(record, {"decision": "reject"})
    assert (choice, updated_input) == ("deny", None)
    assert message == (
        "Response to your pending plan confirmation:\n"
        "User rejected Claude's plan."
    )


def _hermes_client() -> tuple[HermesTuiEngineClient, AsyncMock]:
    request = AsyncMock(return_value={"resolved": True})
    gateway = SimpleNamespace(
        is_live=True,
        request=request,
        subscribe=lambda tui_session_id, after_offset=0: SimpleNamespace(
            tui_session_id=tui_session_id
        ),
        unsubscribe=lambda _subscription: None,
    )
    client = HermesTuiEngineClient(
        gateway=gateway,
        platform_session_id="session-1",
    )
    return client, request


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "hermes_choice"),
    [("approve", "once"), ("reject", "deny")],
)
async def test_hermes_approval_uses_the_shared_mechanics(
    decision: str, hermes_choice: str
) -> None:
    """Hermes answers through the same platform kit, in its own vocabulary.

    Hermes declares one presentation (``tool_approval``), validates the answer
    with the shared validator, and translates it into its own gateway choice.
    No Claude tool name or permission mode exists anywhere along that path.
    """

    frames = [
        frame
        for frame in translate_tui_event(
            {
                "type": "approval.request",
                "payload": {
                    "tool_id": "call-7",
                    "command": "rm -rf /srv/cache",
                    "tool_name": "terminal",
                },
            },
            turn_id="turn-9",
        )
        if frame.get("type") == "interaction.request"
    ]
    assert len(frames) == 1
    contract = dict(frames[0]["payload"])
    tool_use_id = str(contract.pop("tool_use_id", "") or "")
    assert tool_use_id == "call-7"
    assert validate_interaction_contract(contract) == PRESENTATION_TOOL_APPROVAL

    record = _declare(
        contract=contract,
        engine_kind="assistant",
        interaction_id=str(frames[0]["interactionId"]),
        tool_call_id=tool_use_id,
    )
    record["engine_turn_id"] = encode_turn_anchor(
        tui_session_id="tui-1", turn_id="turn-9"
    )
    assert record["tool_name"] == "terminal"
    assert record["raw_input"] == {"command": "rm -rf /srv/cache"}

    client, request = _hermes_client()
    response = {"decision": decision}
    validate_interaction_response(record, response)
    accepted = await client.submit_interaction_response(
        _receipt(record), pending=record, response=response
    )

    assert accepted is True
    request.assert_awaited_once_with(
        "approval.respond", {"session_id": "tui-1", "choice": hermes_choice}
    )

    trace = json.dumps([record, response, request.await_args.args], default=str)
    assert [word for word in _CLAUDE_VOCABULARY if word in trace] == []


@pytest.mark.asyncio
async def test_hermes_refuses_a_decision_it_has_no_gateway_choice_for() -> None:
    """An unmapped decision fails loudly rather than defaulting to a choice."""

    record = _declare(
        contract={
            "tool_name": "terminal",
            "presentation": PRESENTATION_TOOL_APPROVAL,
            "prompt": "Allow terminal to continue?",
            "raw_input": {"command": "rm -rf /srv/cache"},
        },
        engine_kind="assistant",
        interaction_id="hermes-approval-1",
        tool_call_id="call-7",
    )
    record["engine_turn_id"] = encode_turn_anchor(
        tui_session_id="tui-1", turn_id="turn-9"
    )
    client, request = _hermes_client()

    with pytest.raises(ValueError):
        await client.submit_interaction_response(
            _receipt(record), pending=record, response={"decision": "revise"}
        )
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_hermes_declares_interaction_support_without_a_mode_vocabulary() -> None:
    """Hermes has no permission modes at all, so none can leak into its flow."""

    client, _ = _hermes_client()
    manifest = await client.get_capabilities()

    assert manifest.engine_kind == "assistant"
    assert manifest.supports_interaction is True
    assert manifest.permission_modes == []


def _camel_cased_decision_record() -> dict:
    """A decision whose ids are an engine's, not this platform's spelling."""

    return _declare(
        contract={
            "tool_name": "commandExecution",
            "presentation": PRESENTATION_DECISION,
            "prompt": "Codex is asking to run a command.",
            "body": "Command: rm -rf /tmp/cache",
            "options": [
                {
                    "id": "accept",
                    "denial": False,
                    "reply": "Approved.",
                },
                {
                    "id": "acceptForSession",
                    "denial": False,
                    "reply": "Approved for the session.",
                },
            ],
            "raw_input": {"command": "rm -rf /tmp/cache"},
        },
        engine_kind="codex_decision",
        interaction_id="codex-1",
        tool_call_id="item-1",
    )


def test_a_decision_id_is_matched_exactly_not_case_folded() -> None:
    """The ids are the engine's vocabulary, so their case is part of them.

    Folding case here refused `acceptForSession` — an id the adapter had
    declared and the console had rendered — as though the browser had made it
    up. Every id this platform authored itself is lower case, so nothing but
    an engine's own spelling ever reached the fold.
    """

    record = _camel_cased_decision_record()

    validate_interaction_response(record, {"decision": "acceptForSession"})
    assert (
        render_interaction_reply(record, {"decision": "acceptForSession"})
        == "Approved for the session."
    )
    with pytest.raises(APIError):
        validate_interaction_response(record, {"decision": "acceptforsession"})


def test_an_option_id_reaches_the_browser_and_its_reply_does_not() -> None:
    """The browser renders native ids; model-facing reply text stays private."""

    record = _camel_cased_decision_record()
    public = public_interaction_view(record)

    assert [option["id"] for option in public["options"]] == [
        "accept",
        "acceptForSession",
    ]
    assert all("label" not in option for option in public["options"])
    assert all("reply" not in option for option in public["options"])
