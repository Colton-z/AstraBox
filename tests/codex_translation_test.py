"""What one Codex turn looks like on the way to the browser.

The events here are the app-server's own shapes, taken from the protocol
schema, with approval fields checked against the image's 0.153.4 pin. What is
asserted is the part a reader would notice if it were wrong: that a reply
opens and closes on the engine's own item boundaries, that the turn ends when
Codex says the TURN ended rather than when a piece of work did, and that an
event this version has never seen still reaches the console.
"""

from __future__ import annotations

import pytest

from astrabox.core.service.orchestrator.engine.codex_events import (
    CodexProtocolError,
    CodexTurnTranslator,
    build_approval_contract,
    build_question_contract,
    approval_response_value,
    question_response_value,
)
from astrabox.core.service.orchestrator.engine.interaction_contract import (
    validate_interaction_contract,
)

THREAD = "01a010cd-9455-74c3-bcd2-6a3225a5e82e"
TURN = "01a010cd-9455-74c3-0000-000000000001"


def _notify(method: str, params: dict) -> dict:
    return {"method": method, "params": params}


def _item_started(item: dict) -> dict:
    return _notify(
        "item/started",
        {"item": item, "threadId": THREAD, "turnId": TURN, "startedAtMs": 1},
    )


def _item_completed(item: dict) -> dict:
    return _notify(
        "item/completed",
        {"item": item, "threadId": THREAD, "turnId": TURN, "completedAtMs": 2},
    )


def _translate(translator: CodexTurnTranslator, *messages: dict) -> list[dict]:
    frames: list[dict] = []
    for message in messages:
        frames.extend(translator.translate(message))
    return frames


def test_an_assistant_message_opens_streams_and_closes_on_its_item() -> None:
    """The block's identity is the engine's `itemId`, not a counter here."""

    translator = CodexTurnTranslator()
    frames = _translate(
        translator,
        _notify("turn/started", {"threadId": THREAD, "turn": {"id": TURN}}),
        _item_started({"type": "agentMessage", "id": "item-1", "text": ""}),
        _notify(
            "item/agentMessage/delta",
            {"threadId": THREAD, "turnId": TURN, "itemId": "item-1", "delta": "Hel"},
        ),
        _notify(
            "item/agentMessage/delta",
            {"threadId": THREAD, "turnId": TURN, "itemId": "item-1", "delta": "lo"},
        ),
        _item_completed({"type": "agentMessage", "id": "item-1", "text": "Hello"}),
    )

    assert [frame["type"] for frame in frames] == [
        "start-step",
        "text-start",
        "text-delta",
        "text-delta",
        "text-end",
    ]
    assert [frame["id"] for frame in frames[1:]] == ["item-1"] * 4
    assert "".join(f["delta"] for f in frames if f["type"] == "text-delta") == "Hello"


def test_a_delta_with_no_open_block_is_refused_rather_than_guessed() -> None:
    """Silently opening one would attach text to a block nobody started."""

    with pytest.raises(CodexProtocolError, match="no open block"):
        _translate(
            CodexTurnTranslator(),
            _notify(
                "item/agentMessage/delta",
                {"threadId": THREAD, "turnId": TURN, "itemId": "ghost", "delta": "x"},
            ),
        )


def test_a_finished_command_is_not_a_finished_answer() -> None:
    """`item/completed` ends one piece of work; the turn keeps going.

    This is the mistake that truncates replies, so it is asserted directly:
    a command running to completion mid-answer must produce a tool result and
    nothing that reads as a terminal.
    """

    translator = CodexTurnTranslator()
    command = {"type": "commandExecution", "id": "cmd-1", "command": ["ls", "-la"]}
    frames = _translate(translator, _item_started(command), _item_completed(command))

    assert [frame["type"] for frame in frames] == [
        "tool-input-start",
        "tool-input-available",
        "tool-output-available",
    ]
    assert all(frame["type"] != "result" for frame in frames)
    # The engine's own item, not a reshaped one: which field holds a command's
    # argv is Codex's to say.
    assert frames[1]["input"] == command
    # The AI SDK builds a `dynamic-tool` part only from a frame that says so;
    # without the flag the console gets a typed `tool-commandExecution` part
    # that none of its tool-part readers accept.
    assert frames[0]["dynamic"] is True and frames[1]["dynamic"] is True


@pytest.mark.parametrize(
    ("status", "finish_reason"),
    [("completed", "stop"), ("interrupted", "cancelled"), ("failed", "error")],
)
def test_the_turn_ends_on_the_status_codex_reports(
    status: str, finish_reason: str
) -> None:
    frames = _translate(
        CodexTurnTranslator(),
        _notify(
            "turn/completed",
            {"threadId": THREAD, "turn": {"id": TURN, "status": status}},
        ),
    )

    assert frames[0]["type"] == "result"
    assert frames[0]["finishReason"] == finish_reason
    # The vendor's own word travels alongside the platform's three, so a
    # reader of the stored turn can still tell interrupted from failed.
    assert frames[0]["__engine_terminal_reason"] == status


def test_a_turn_that_says_it_is_still_running_is_refused() -> None:
    """`inProgress` on a completion notification means the protocol moved."""

    with pytest.raises(CodexProtocolError, match="unknown status"):
        _translate(
            CodexTurnTranslator(),
            _notify(
                "turn/completed",
                {"threadId": THREAD, "turn": {"id": TURN, "status": "inProgress"}},
            ),
        )


def test_an_unknown_notification_reaches_the_console_instead_of_vanishing() -> None:
    """A newer Codex must be visible, not silently dropped."""

    frames = _translate(
        CodexTurnTranslator(),
        _notify("item/somethingNewInAFutureRelease", {"threadId": THREAD}),
    )

    assert frames[0]["type"] == "data-raw-event"
    assert frames[0]["data"]["event_type"] == "codex.app_server"
    assert frames[0]["data"]["subtype"] == "item/somethingNewInAFutureRelease"


def test_a_fact_the_platform_already_holds_is_not_shown_twice() -> None:
    """The conversation's own lifecycle is the session row's, not a card's."""

    frames = _translate(
        CodexTurnTranslator(),
        _notify("thread/started", {"threadId": THREAD}),
        _notify("thread/tokenUsage/updated", {"threadId": THREAD}),
    )

    assert frames == []


# ── interactions ─────────────────────────────────────────────────────────
def test_an_approval_request_renders_as_a_decision_among_codexs_own() -> None:
    contract = build_approval_contract(
        "item/commandExecution/requestApproval",
        {
            "threadId": THREAD,
            "turnId": TURN,
            "itemId": "cmd-1",
            "startedAtMs": 1,
            "reason": "This command writes outside the workspace.",
        },
    )

    assert contract.pop("tool_use_id") == "cmd-1"
    assert validate_interaction_contract(contract) == "decision"
    assert contract["prompt"] == "This command writes outside the workspace."
    # The console renders the presentation and never reads Codex's field
    # names, so the request survives whole for anyone who needs the detail.
    assert contract["raw_input"]["itemId"] == "cmd-1"


def test_an_approval_says_which_command_it_is_asking_about() -> None:
    """A reason is why, not what — and only one of them is on screen.

    `CommandExecutionRequestApprovalParams` carries the command and the
    directory as fields of the request, so approving blind is a choice this
    adapter would have been making, not a limit of the protocol.
    """

    contract = build_approval_contract(
        "item/commandExecution/requestApproval",
        {
            "threadId": THREAD,
            "turnId": TURN,
            "itemId": "cmd-1",
            "startedAtMs": 1,
            "reason": "This command writes outside the workspace.",
            "command": "rm -rf /tmp/cache",
            "cwd": "/home/agent/workspace",
        },
    )

    assert contract.pop("tool_use_id") == "cmd-1"
    assert validate_interaction_contract(contract) == "decision"
    assert "rm -rf /tmp/cache" in contract["body"]
    assert "/home/agent/workspace" in contract["body"]


def test_a_network_approval_says_which_host_it_wants() -> None:
    contract = build_approval_contract(
        "item/commandExecution/requestApproval",
        {
            "threadId": THREAD,
            "turnId": TURN,
            "itemId": "cmd-2",
            "startedAtMs": 1,
            "command": "curl https://api.example.com/v1/things",
            "cwd": "/home/agent/workspace",
            "networkApprovalContext": {
                "host": "api.example.com",
                "protocol": "https",
            },
        },
    )

    assert "api.example.com" in contract["body"]
    # Managed-network requests are not general command approvals, even when
    # the native request also carries a command field.
    assert contract["prompt"] == "Codex is asking for network access."
    assert "Command:" not in contract["body"]
    assert contract["raw_input"]["command"] == "curl https://api.example.com/v1/things"


def test_a_file_change_approval_says_which_root_it_wants_to_write() -> None:
    contract = build_approval_contract(
        "item/fileChange/requestApproval",
        {
            "threadId": THREAD,
            "turnId": TURN,
            "itemId": "patch-1",
            "startedAtMs": 1,
            "reason": "Apply the security patch.",
            "grantRoot": "/home/agent/workspace/vendor",
        },
    )

    assert "/home/agent/workspace/vendor" in contract["body"]


def test_an_approval_answer_carries_codexs_own_decision_value() -> None:
    """The id chosen in the console IS what goes on the wire.

    Command and file-change approvals use `accept`/`decline`, rather than the
    differently named values of the older `ReviewDecision` protocol.
    """

    method = "item/fileChange/requestApproval"

    for value in ("accept", "acceptForSession", "decline", "cancel"):
        assert approval_response_value(method, {}, {"decision": value}) == {
            "decision": value
        }


def test_the_decision_type_comes_from_the_method_not_a_similar_name() -> None:
    """`ReviewDecision` looks like the answer here and is a different method's.

    `CommandExecutionRequestApproval` answers with a
    `CommandExecutionApprovalDecision`; `ReviewDecision`
    (`approved`/`denied`/`abort`) belongs to `ExecCommandApproval`, which this
    adapter does not use. Sending its words is not refused by anything — the
    tool just never runs.
    """

    for method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
        with pytest.raises(CodexProtocolError, match="not one of the decisions"):
            approval_response_value(method, {}, {"decision": "approved"})


def test_a_decision_codex_never_declared_is_refused() -> None:
    with pytest.raises(CodexProtocolError, match="not one of the decisions"):
        approval_response_value(
            "item/fileChange/requestApproval", {}, {"decision": "yes"}
        )


def _live_command_approval() -> dict:
    """One `item/commandExecution/requestApproval`, recorded off a real server.

    Captured from codex 0.147.0 answering a write that its read-only sandbox
    refused, under `approvalPolicy: "on-request"`. Kept verbatim because the
    fields that matter here are ones nobody would have written down: the
    request narrows the decisions to three, and the one it names first for
    "approve and stop asking" is the object variant, not `acceptForSession`.
    """

    return {
        "availableDecisions": [
            "accept",
            {
                "acceptWithExecpolicyAmendment": {
                    "execpolicy_amendment": [
                        "/bin/bash",
                        "-lc",
                        "printf 'APPROVED' > /tmp/astrabox-probe.txt && cat /tmp/astrabox-probe.txt",
                    ]
                }
            },
            "cancel",
        ],
        "command": (
            "/bin/bash -lc \"printf 'APPROVED' > /tmp/astrabox-probe.txt "
            "&& cat /tmp/astrabox-probe.txt\""
        ),
        "commandActions": [
            {
                "command": (
                    "printf 'APPROVED' > /tmp/astrabox-probe.txt "
                    "&& cat /tmp/astrabox-probe.txt"
                ),
                "type": "unknown",
            }
        ],
        "cwd": "/workspace",
        "environmentId": "local",
        "itemId": "call_00_c3ctG2xoVQ6o4XTTvrRn9804",
        "proposedExecpolicyAmendment": [
            "/bin/bash",
            "-lc",
            "printf 'APPROVED' > /tmp/astrabox-probe.txt && cat /tmp/astrabox-probe.txt",
        ],
        "reason": "Do you want to allow writing the probe file to /tmp and reading it back?",
        "startedAtMs": 1787216900068,
        "threadId": THREAD,
        "turnId": TURN,
    }


def test_a_request_that_narrows_its_decisions_is_offered_only_those() -> None:
    """`availableDecisions` is this request's set, not the type's.

    The recorded request offers three, and `decline` is not among them —
    "refuse this and carry on" is not a thing the server will accept here.
    Declaring the whole enum anyway would put two choices on screen that this
    command never offered.
    """

    contract = build_approval_contract(
        "item/commandExecution/requestApproval", _live_command_approval()
    )

    assert contract.pop("tool_use_id") == "call_00_c3ctG2xoVQ6o4XTTvrRn9804"
    assert validate_interaction_contract(contract) == "decision"
    assert [option["id"] for option in contract["options"]] == ["accept", "cancel"]


def test_a_decision_the_request_withheld_is_refused() -> None:
    params = _live_command_approval()

    with pytest.raises(CodexProtocolError, match="not one of the decisions"):
        approval_response_value(
            "item/commandExecution/requestApproval", params, {"decision": "decline"}
        )
    assert approval_response_value(
        "item/commandExecution/requestApproval", params, {"decision": "accept"}
    ) == {"decision": "accept"}


def test_available_decisions_preserve_the_native_order() -> None:
    params = _live_command_approval()
    params["availableDecisions"] = ["cancel", "acceptForSession", "accept"]

    contract = build_approval_contract("item/commandExecution/requestApproval", params)

    assert [option["id"] for option in contract["options"]] == [
        "cancel", "acceptForSession", "accept",
    ]


def test_a_request_offering_only_payload_decisions_says_so() -> None:
    """The two object variants carry a policy payload no console composes.

    A request narrowed to only those has no answer this adapter can send, and
    saying that is the whole of what it can honestly do — a contract built from
    the enum instead would show buttons whose answers the server refuses.
    """

    params = _live_command_approval()
    params["availableDecisions"] = [
        {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["true"]}}
    ]

    with pytest.raises(CodexProtocolError, match="no decision this adapter can send"):
        build_approval_contract("item/commandExecution/requestApproval", params)


def _permissions_request() -> dict:
    """One `item/permissions/requestApproval`, shaped as the server sends it."""

    return {
        "threadId": THREAD,
        "turnId": TURN,
        "itemId": "perm-1",
        "startedAtMs": 1,
        "cwd": "/home/agent/workspace",
        "reason": "Connect to the package registry.",
        "permissions": {
            "network": {"enabled": True},
            "fileSystem": {"read": ["/etc/ssl"], "write": None},
        },
    }


def test_a_permissions_request_offers_its_own_vocabulary() -> None:
    """It is not answered with the command enum, so it does not offer it.

    `PermissionsRequestApprovalResponse` has no decision field at all: it
    carries a granted profile and a scope. An `accept` reaching this request
    would be an id from another request's answer type.
    """

    contract = build_approval_contract(
        "item/permissions/requestApproval", _permissions_request()
    )

    assert contract.pop("tool_use_id") == "perm-1"
    assert validate_interaction_contract(contract) == "decision"
    assert [option["id"] for option in contract["options"]] == [
        "grant",
        "grantForSession",
        "deny",
    ]
    # What is being granted is on screen, not only in the reason.
    assert "network" in contract["body"]
    with pytest.raises(CodexProtocolError, match="not one of the decisions"):
        approval_response_value(
            "item/permissions/requestApproval",
            _permissions_request(),
            {"decision": "accept"},
        )


def test_granting_permissions_sends_back_the_profile_that_was_asked_for() -> None:
    """Granting is not composing: the request already names the profile.

    The server intersects what it is granted with what it requested, so
    returning the requested profile whole grants exactly what was asked and
    can never grant more. The scope is the only other half, and it is the one
    a person actually chose.
    """

    params = _permissions_request()

    assert approval_response_value(
        "item/permissions/requestApproval", params, {"decision": "grant"}
    ) == {"permissions": params["permissions"], "scope": "turn"}
    assert approval_response_value(
        "item/permissions/requestApproval", params, {"decision": "grantForSession"}
    ) == {"permissions": params["permissions"], "scope": "session"}


def test_denying_permissions_grants_nothing_rather_than_saying_no() -> None:
    """An empty profile IS the refusal Codex reads.

    The server maps a granted profile it reads as empty to its default of
    nothing granted, and answers with that same default itself when a client
    errors. There is no decision field to say no with.
    """

    assert approval_response_value(
        "item/permissions/requestApproval", _permissions_request(), {"decision": "deny"}
    ) == {"permissions": {}, "scope": "turn"}


def test_a_permissions_request_with_no_profile_is_not_granted_blind() -> None:
    params = _permissions_request()
    del params["permissions"]

    with pytest.raises(CodexProtocolError, match="carries no 'permissions' profile"):
        approval_response_value(
            "item/permissions/requestApproval", params, {"decision": "grant"}
        )


def test_a_question_renders_as_a_form_and_answers_by_the_ids_it_asked() -> None:
    params = {
        "threadId": THREAD,
        "turnId": TURN,
        "itemId": "ask-1",
        "isBlocking": True,
        "questions": [
            {
                "id": "q1",
                "header": "Deploy",
                "question": "Which environment?",
                "isOther": False,
                "isSecret": False,
                "options": [{"label": "staging"}, {"label": "production"}],
            }
        ],
    }
    contract = build_question_contract(params)

    tool_use_id = contract.pop("tool_use_id")
    assert tool_use_id == "ask-1"
    assert validate_interaction_contract(contract) == "form"
    assert contract["questions"][0]["options"][0]["label"] == "staging"

    value = question_response_value(
        params, {"answers": [{"question_id": "q1", "option_label": "staging"}]}
    )
    assert value == {"answers": {"q1": {"answers": ["staging"]}}}


def test_a_codex_question_requires_its_native_tool_call_identity() -> None:
    with pytest.raises(CodexProtocolError, match="no itemId"):
        build_question_contract(
            {
                "questions": [
                    {
                        "id": "q1",
                        "question": "Which environment?",
                        "options": [{"label": "staging"}],
                    }
                ]
            }
        )


def test_a_codex_question_accepts_the_public_plural_option_field() -> None:
    params = {
        "questions": [
            {
                "id": "q1",
                "question": "Which environment?",
                "options": [{"label": "staging"}, {"label": "production"}],
            }
        ]
    }

    value = question_response_value(
        params,
        {"answers": [{"question_id": "q1", "option_labels": ["production"]}]},
    )

    assert value == {"answers": {"q1": {"answers": ["production"]}}}


def test_a_codex_free_text_answer_uses_the_vendor_note_encoding() -> None:
    params = {
        "questions": [
            {
                "id": "q1",
                "question": "What should change?",
                "isOther": True,
                "options": [{"label": "Nothing"}],
            }
        ]
    }

    value = question_response_value(
        params, {"answers": [{"question_id": "q1", "free_text": "Add tests"}]}
    )

    assert value == {"answers": {"q1": {"answers": ["user_note: Add tests"]}}}


@pytest.mark.parametrize(
    "answer, message",
    [
        ({"option_label": "preview"}, "undeclared option"),
        ({"option_labels": ["staging", "production"]}, "more than one option"),
        (
            {"option_label": "staging", "free_text": "also preview"},
            "selects an option and supplies free text",
        ),
    ],
)
def test_a_codex_question_rejects_answers_its_native_contract_cannot_represent(
    answer: dict[str, object], message: str
) -> None:
    params = {
        "questions": [
            {
                "id": "q1",
                "question": "Which environment?",
                "isOther": True,
                "options": [{"label": "staging"}, {"label": "production"}],
            }
        ]
    }

    with pytest.raises(CodexProtocolError, match=message):
        question_response_value(
            params,
            {"answers": [{"question_id": "q1", **answer}]},
        )


def test_an_answer_to_a_question_codex_did_not_ask_is_not_forwarded() -> None:
    """The reply is a map, so an unknown key would be dropped silently there."""

    params = {
        "questions": [{"id": "q1", "question": "Which environment?", "options": []}]
    }

    with pytest.raises(CodexProtocolError, match="does not name every question"):
        question_response_value(
            params,
            {"answers": [{"question_id": "not-asked", "option_label": "x"}]},
        )
