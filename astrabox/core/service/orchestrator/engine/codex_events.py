"""Codex app-server notifications and requests, translated once.

Codex names three nested things and AstraBox needs all three kept straight: a
**thread** is the conversation, a **turn** is one user request and everything
the agent does about it, and an **item** is one piece of that work — the
agent's message, its reasoning, a command it ran, a file it changed. Items
have an explicit lifecycle (``item/started`` … deltas … ``item/completed``)
and carry a stable ``itemId``, so a streamed block opens and closes on the
engine's own boundaries rather than on any counting done here.

The end of a response is ``turn/completed`` and nothing else. ``item/completed``
ends one piece of work and the turn keeps going — a command finishing is
usually the middle of an answer, not its end. The turn's own
``status`` (``completed`` / ``interrupted`` / ``failed``) is the vendor's word
for how it ended and is carried through rather than re-derived from what was
seen on the way.

Interactions arrive as JSON-RPC *requests* rather than notifications, because
Codex expects an answer routed back by the request's id. Four of them can
reach a person: two command/file approvals, a permissions grant, and a
question with options. Each is translated into one of the presentations
AstraBox renders, with the engine's whole request preserved in ``raw_input``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from astrabox.core.service.orchestrator.engine.emissions import public_ui_frame

from astrabox.core.service.orchestrator.engine.file_changes import (
    ContentDiff, FileChange, UnifiedDiff, file_changes_frame,
)

from astrabox.core.service.orchestrator.engine.interaction_contract import (
    PRESENTATION_DECISION,
    PRESENTATION_FORM,
)


class CodexProtocolError(RuntimeError):
    """The app-server sent something this translator cannot honour."""


#: `turn.status` → the platform's three finish reasons. `inProgress` is
#: deliberately absent: on a `turn/completed` it would mean the vendor changed
#: what that notification means, and guessing would hide it.
_FINISH_REASONS = {
    "completed": "stop",
    "interrupted": "cancelled",
    "failed": "error",
}

#: Item types that stream as prose, and the frame family each one opens.
_PROSE_ITEMS = {"agentMessage": "text", "reasoning": "reasoning", "plan": "text"}

#: Item types that are a tool call in the console's sense: something the agent
#: ran, with an input worth showing and an output worth showing.
CODEX_TOOL_ITEM_TYPES = frozenset(
    {"commandExecution", "fileChange", "mcpToolCall", "webSearch", "todoList"}
)

#: Items the platform holds as child runs. The Agents panel and the durable
#: `/child-runs` resource own a spawned sub-agent's identity and lifecycle, so
#: a diagnostic card beside them would show the same thing twice.
_CHILD_RUN_ITEMS = frozenset({"collabAgentToolCall", "subAgentActivity"})

#: Notifications AstraBox already holds the fact for, so passing them through
#: as raw-event cards would show the reader the same thing twice. Everything
#: NOT listed here — including anything a newer Codex adds — still reaches the
#: console as a card.
_DROPPED_NOTIFICATIONS = frozenset(
    {
        # The conversation's identity and lifecycle: the platform's own
        # session row is the record of these.
        "thread/started",
        "thread/status/changed",
        "thread/closed",
        "thread/name/updated",
        "thread/tokenUsage/updated",
        # Answered as interactions, and resolved by the platform; the
        # server's own bookkeeping about them adds nothing.
        "serverRequest/resolved",
        # This connection's control plane, not the conversation's content.
        "remoteControl/status/changed",
        "account/updated",
        "account/rateLimits/updated",
    }
)


def raw_event_frame(subtype: str, raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "data-raw-event",
        "data": {
            "event_type": "codex.app_server",
            "subtype": subtype or "unknown",
            "raw": dict(raw),
        },
    }


class CodexTurnTranslator:
    """One turn's notifications → AstraBox stream frames.

    Held across `iter_turn_events` calls rather than rebuilt per call: an
    interaction ends the browser's stream while the engine's turn stays open,
    and a translator rebuilt on re-entry would have forgotten which blocks the
    engine still has open.
    """

    def __init__(self) -> None:
        #: itemId → the frame family opened for it, so a delta can be refused
        #: when nothing is open and `-end` closes what `-start` opened.
        self._open: dict[str, str] = {}

    def translate(self, message: dict[str, Any]) -> Iterator[dict[str, Any]]:
        method = str(message.get("method") or "").strip()
        params = message.get("params")
        params = params if isinstance(params, dict) else {}

        if method == "turn/started":
            yield {"type": "start-step"}
            return
        if method == "turn/completed":
            yield self._turn_completed(params)
            return
        if method == "item/started":
            yield from self._item_started(params)
            return
        if method == "item/completed":
            yield from self._item_completed(params)
            return
        if method == "item/agentMessage/delta":
            yield from self._delta(params, "text", str(params.get("delta") or ""))
            return
        if method in {
            "item/reasoning/summaryTextDelta",
            "item/reasoning/textDelta",
        }:
            yield from self._delta(params, "reasoning", str(params.get("delta") or ""))
            return
        if method == "item/plan/delta":
            yield from self._delta(params, "text", str(params.get("delta") or ""))
            return
        if method in _DROPPED_NOTIFICATIONS:
            return
        yield raw_event_frame(method, message)

    # ── items ────────────────────────────────────────────────────────────
    def _item_started(self, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        item = params.get("item")
        if not isinstance(item, dict):
            raise CodexProtocolError("item/started carries no item object")
        item_id = str(item.get("id") or "").strip()
        item_type = str(item.get("type") or "").strip()
        if not item_id:
            raise CodexProtocolError(f"item/started for {item_type!r} has no id")
        family = _PROSE_ITEMS.get(item_type)
        if family is not None:
            self._open[item_id] = family
            frame: dict[str, Any] = {"type": f"{family}-start", "id": item_id}
            # Codex's own classification of an assistant message, carried
            # rather than dropped. A turn can emit more than one
            # `agentMessage`, and the app-server spec says the answer is the
            # one whose phase is `final_answer`; the others are mid-turn
            # narration. Dropping it here is what put two replies in one
            # message. `phase` is optional by that same spec — providers do
            # not all emit it — so its absence travels as absence.
            phase = item.get("phase")
            if isinstance(phase, str) and phase.strip():
                frame["phase"] = phase.strip()
            yield frame
            return
        if item_type in CODEX_TOOL_ITEM_TYPES:
            yield public_ui_frame({
                "type": "tool-input-start",
                "toolCallId": item_id,
                "toolName": item_type,
                "dynamic": True,
            })
            yield public_ui_frame({
                "type": "tool-input-available",
                "toolCallId": item_id,
                "toolName": item_type,
                "dynamic": True,
                # The item verbatim: Codex names a command's argv, a patch's
                # changes and an MCP call's arguments differently, and which
                # of those the reader is looking at is the engine's to say.
                "input": dict(item),
            })
            return
        # A userMessage item is the platform's own input coming back, and
        # anything else is a shape this version does not know. Both reach the
        # console as a card rather than being dropped.
        if item_type != "userMessage" and item_type not in _CHILD_RUN_ITEMS:
            yield raw_event_frame(f"item/started:{item_type}", params)

    def _item_completed(self, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        item = params.get("item")
        if not isinstance(item, dict):
            raise CodexProtocolError("item/completed carries no item object")
        item_id = str(item.get("id") or "").strip()
        item_type = str(item.get("type") or "").strip()
        family = self._open.pop(item_id, None)
        if family is not None:
            yield {"type": f"{family}-end", "id": item_id}
            return
        if item_type in CODEX_TOOL_ITEM_TYPES:
            yield {
                "type": "tool-output-available",
                "toolCallId": item_id,
                "output": dict(item),
            }
            if item_type == "fileChange" and item.get("status") == "completed":
                changes = []
                for change in item["changes"]:
                    kind = change["kind"]["type"]
                    diff = change["diff"]
                    view: ContentDiff | UnifiedDiff
                    if kind == "add":
                        view = ContentDiff(before="", after=diff)
                    elif kind == "delete":
                        view = ContentDiff(before=diff, after="")
                    elif kind == "update":
                        view = UnifiedDiff(patch=diff)
                    else:
                        raise CodexProtocolError(f"unknown file change kind {kind!r}")
                    changes.append(FileChange(
                        path=change["kind"].get("move_path") or change["path"], diff=view,
                    ))
                if changes:
                    yield file_changes_frame(item_id, item_type, changes)
            return
        if (
            item_type not in _PROSE_ITEMS
            and item_type != "userMessage"
            and item_type not in _CHILD_RUN_ITEMS
        ):
            yield raw_event_frame(f"item/completed:{item_type}", params)

    def _delta(
        self, params: dict[str, Any], family: str, delta: str
    ) -> Iterator[dict[str, Any]]:
        item_id = str(params.get("itemId") or "").strip()
        open_family = self._open.get(item_id)
        if open_family is None:
            raise CodexProtocolError(
                f"{family} delta for item {item_id!r} has no open block"
            )
        if open_family != family:
            raise CodexProtocolError(
                f"item {item_id!r} is open as {open_family!r}, not {family!r}"
            )
        yield {"type": f"{family}-delta", "id": item_id, "delta": delta}

    # ── turn terminal ────────────────────────────────────────────────────
    def _turn_completed(self, params: dict[str, Any]) -> dict[str, Any]:
        turn = params.get("turn")
        if not isinstance(turn, dict):
            raise CodexProtocolError("turn/completed carries no turn object")
        status = str(turn.get("status") or "").strip()
        finish_reason = _FINISH_REASONS.get(status)
        if finish_reason is None:
            raise CodexProtocolError(
                f"turn/completed carries unknown status {status!r}"
            )
        # Blocks the engine never closed cannot stay open into the next turn.
        self._open.clear()
        frame: dict[str, Any] = {
            "type": "result",
            "finishReason": finish_reason,
            "__engine_terminal_reason": status,
        }
        error = turn.get("error")
        if isinstance(error, dict):
            frame["error"] = dict(error)
        return frame


# ── interactions ─────────────────────────────────────────────────────────
#: Server requests that need a person. Each maps to the presentation AstraBox
#: renders and to the decision vocabulary its answer must use.
APPROVAL_METHODS = {
    "item/commandExecution/requestApproval": "commandExecution",
    "item/fileChange/requestApproval": "fileChange",
    "item/permissions/requestApproval": "permissions",
}
QUESTION_METHOD = "item/tool/requestUserInput"

#: The decisions these approvals take, verbatim.
#:
#: Read off the response type each METHOD names, which is the part worth being
#: careful about: `CommandExecutionRequestApproval` answers with a
#: `CommandExecutionApprovalDecision` and `FileChangeRequestApproval` with a
#: `FileChangeApprovalDecision`. The similar-looking `ReviewDecision`
#: (`approved`/`denied`/`abort`) belongs to the older `ExecCommandApproval`
#: and `ApplyPatchApproval` methods, which this adapter does not use — reading
#: it by name instead of by the method that returns it sends words the engine
#: does not know, and an unparseable decision is not a refusal it reports: the
#: tool simply never runs.
#:
#: Both types share these four. The two object variants of the command type
#: (`acceptWithExecpolicyAmendment`, `applyNetworkPolicyAmendment`) carry a
#: policy payload a console cannot compose, and declaring a choice this
#: adapter cannot honour would be worse than not offering it.
CODEX_DECISIONS: tuple[dict[str, Any], ...] = (
    {
        "id": "accept",
        "denial": False,
        "reply": "Approved.",
        "comment_prefix": "Additional notes",
    },
    {
        "id": "acceptForSession",
        "denial": False,
        "reply": "Approved, and the same is approved for the rest of this session.",
        "comment_prefix": "Additional notes",
    },
    {
        "id": "decline",
        "denial": True,
        "reply": "Declined. Continue with something else.",
        "comment_prefix": "Reason",
    },
    {
        "id": "cancel",
        "denial": True,
        "reply": "Declined, and the turn was interrupted.",
        "comment_prefix": "Reason",
    },
)

#: PermissionsRequestApprovalResponse carries a granted profile and scope,
#: not a decision enum. These adapter-defined ids are encoded by
#: approval_response_value: grant returns the requested profile; deny returns
#: an empty profile. Distinct ids prevent mixing them with command/file choices.
#: Interrupting a turn requires a separate call and is not offered here.
CODEX_PERMISSION_DECISIONS: tuple[dict[str, Any], ...] = (
    {
        "id": "grant",
        "denial": False,
        "reply": "Granted for this turn.",
        "comment_prefix": "Additional notes",
    },
    {
        "id": "grantForSession",
        "denial": False,
        "reply": "Granted for the rest of this session.",
        "comment_prefix": "Additional notes",
    },
    {
        "id": "deny",
        "denial": True,
        "reply": "Denied. Nothing was granted.",
        "comment_prefix": "Reason",
    },
)

#: Which vocabulary each request is answered in.
_DECISIONS_BY_KIND: dict[str, tuple[dict[str, Any], ...]] = {
    "commandExecution": CODEX_DECISIONS,
    "fileChange": CODEX_DECISIONS,
    "permissions": CODEX_PERMISSION_DECISIONS,
}

#: What is being asked, for a request that carries no `reason` of its own.
_APPROVAL_PROMPTS = {
    "commandExecution": "Codex is asking to run a command.",
    "fileChange": "Codex is asking to change files.",
    "permissions": "Codex is asking for additional permissions.",
}

#: The scope a granting decision asks for. `PermissionGrantScope`, verbatim;
#: `turn` is the vendor's own default.
_PERMISSION_GRANT_SCOPES = {"grant": "turn", "grantForSession": "session"}


#: The name the console shows for a question — the vendor's name for the tool
#: the model actually called, not a transcription of the routing method. The
#: generated schema for these very params says so twice: "Params sent with a
#: `request_user_input` event", "options for `request_user_input`". Deriving
#: `requestUserInput` from `item/tool/requestUserInput` instead would put a
#: spelling in the console that appears nowhere in the engine.
CODEX_QUESTION_TOOL_NAME = "request_user_input"

INTERACTION_METHODS = frozenset({*APPROVAL_METHODS, QUESTION_METHOD})


def _approval_body(kind: str, params: dict[str, Any]) -> str | None:
    """Describe the requested operation using Codex's request fields.

    A reason alone may omit the command, working directory, network target or
    write root. Include the applicable fields and serialize permission profiles
    as JSON so nested entries and modes are visible. The complete request is
    retained in the approval contract's ``raw_input``.
    """

    lines: list[str] = []
    if kind == "commandExecution":
        network = params.get("networkApprovalContext")
        command = str(params.get("command") or "").strip()
        if command and not isinstance(network, dict):
            lines.append(f"Command: {command}")
        cwd = str(params.get("cwd") or "").strip()
        if cwd:
            lines.append(f"Working directory: {cwd}")
        if isinstance(network, dict):
            host = str(network.get("host") or "").strip()
            protocol = str(network.get("protocol") or "").strip()
            if host:
                lines.append(f"Network: {host}" + (f" ({protocol})" if protocol else ""))
    elif kind == "fileChange":
        grant_root = str(params.get("grantRoot") or "").strip()
        if grant_root:
            lines.append(f"Write access under: {grant_root}")
    elif kind == "permissions":
        cwd = str(params.get("cwd") or "").strip()
        if cwd:
            lines.append(f"Working directory: {cwd}")
        permissions = params.get("permissions")
        if isinstance(permissions, dict):
            lines.append(
                "Requested permissions: "
                + json.dumps(permissions, ensure_ascii=False, sort_keys=True)
            )
    return "\n".join(lines) or None


def _declared_decisions(kind: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """The decisions to offer for one request, narrowed to the ones it lists.

    A command's `availableDecisions` narrows and orders the choices for that
    request. Preserve that order instead of imposing the adapter's default.

    Object variants are dropped, because they are the ones whose reply carries
    a policy payload this adapter does not compose. A list naming only those is
    a request nobody here can answer, and it says so rather than presenting a
    set it made up.
    """

    decisions = _DECISIONS_BY_KIND[kind]
    available = params.get("availableDecisions")
    if not isinstance(available, list):
        # Not every request narrows the set; a file change answers with the
        # whole of its own enum, which is what these already are.
        return [dict(option) for option in decisions]
    by_id = {option["id"]: option for option in decisions}
    named = [value for value in available if isinstance(value, str)]
    offered = [dict(by_id[value]) for value in named if value in by_id]
    if not offered:
        raise CodexProtocolError(
            "codex offered no decision this adapter can send: "
            f"{sorted(named) or 'only decisions carrying a policy payload'}"
        )
    return offered


def build_approval_contract(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """One Codex approval request → a decision among Codex's own choices.

    `decision` rather than `tool_approval` because Codex's decisions are not a
    yes/no: "approve for the rest of this session" and "no, and stop
    everything" are choices a person has and the platform's two buttons cannot
    express. The presentation exists for exactly this — its options are
    adapter-declared and answered by id — so the engine's words travel whole
    and nothing here is mapped.

    Which options are declared depends on which request this is, because the
    three do not share one answer type: a command and a file change answer
    with a decision the vendor enumerates, and a permissions request answers
    with a profile and a scope.
    """

    kind = APPROVAL_METHODS[method]
    tool_use_id = str(params.get("itemId") or "").strip()
    if not tool_use_id:
        raise CodexProtocolError(f"{method} carries no itemId")
    reason = str(params.get("reason") or "").strip()
    prompt = _APPROVAL_PROMPTS[kind]
    if kind == "commandExecution" and isinstance(params.get("networkApprovalContext"), dict):
        prompt = "Codex is asking for network access."
    return {
        "tool_use_id": tool_use_id,
        "tool_name": kind,
        "presentation": PRESENTATION_DECISION,
        "prompt": reason or prompt,
        "body": _approval_body(kind, params),
        "options": _declared_decisions(kind, params),
        # The engine's whole request. The console renders the presentation and
        # never reads Codex's field names, so nothing here is normalized.
        "raw_input": dict(params),
    }


def build_question_contract(params: dict[str, Any]) -> dict[str, Any]:
    """One `item/tool/requestUserInput` → the console's `form` shape."""

    tool_use_id = str(params.get("itemId") or "").strip()
    if not tool_use_id:
        raise CodexProtocolError("requestUserInput carries no itemId")
    rows = params.get("questions")
    if not isinstance(rows, list) or not rows:
        raise CodexProtocolError("requestUserInput carries no questions")
    questions: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise CodexProtocolError("requestUserInput question is not an object")
        question_id = str(row.get("id") or "").strip()
        if not question_id:
            raise CodexProtocolError("requestUserInput question has no id")
        text = str(row.get("question") or "").strip()
        if not text:
            raise CodexProtocolError(f"question {question_id!r} has no question text")
        entry: dict[str, Any] = {
            "id": question_id,
            "question": text,
            # Codex asks one thing per row and takes one answer per row; a
            # row that also accepts free text (`isOther`) is still one answer.
            "multi_select": False,
            "allow_free_text": bool(row.get("isOther")) or not row.get("options"),
            "allow_empty_text": False,
            "options": [
                {
                    "label": str(option.get("label") or ""),
                    "description": str(option.get("description") or ""),
                }
                for option in (row.get("options") or [])
                if isinstance(option, dict)
            ],
        }
        header = str(row.get("header") or "").strip()
        if header:
            entry["header"] = header
        questions.append(entry)
    lead = questions[0].get("header") or questions[0]["question"]
    return {
        # Codex defines `itemId` as the request_user_input call id. The
        # platform carries that vendor identity to bind the pending form to
        # the tool call; it does not mint or infer a parallel id.
        "tool_use_id": tool_use_id,
        "tool_name": CODEX_QUESTION_TOOL_NAME,
        "presentation": PRESENTATION_FORM,
        "prompt": str(lead),
        "questions": questions,
        "raw_input": dict(params),
    }


def approval_response_value(
    method: str, params: dict[str, Any], response: dict[str, Any]
) -> dict[str, Any]:
    """Encode a declared console choice in the request's Codex response type.

    Command and file-change replies carry the selected native decision id.
    Permission replies carry the requested profile and selected scope, or an
    empty profile for denial. Undeclared choices and grants without a requested
    profile raise ``CodexProtocolError``.
    """

    kind = APPROVAL_METHODS[method]
    chosen = str(response.get("decision") or "").strip()
    if chosen not in {option["id"] for option in _declared_decisions(kind, params)}:
        raise CodexProtocolError(
            f"codex approval answered with {chosen!r}, which is not one of the "
            "decisions this request declared"
        )
    if kind != "permissions":
        return {"decision": chosen}

    scope = _PERMISSION_GRANT_SCOPES.get(chosen)
    if scope is None:
        # Denial requires a scope even though its granted profile is empty.
        return {"permissions": {}, "scope": "turn"}
    requested = params.get("permissions")
    if not isinstance(requested, dict):
        raise CodexProtocolError(
            "codex asked for permissions without saying which: the request "
            "carries no 'permissions' profile to grant"
        )
    # Copy the requested profile without adding permissions.
    return {"permissions": dict(requested), "scope": scope}


def question_response_value(
    params: dict[str, Any], response: dict[str, Any]
) -> dict[str, Any]:
    """The console's form answer → `{answers: {questionId: {answers: [...]}}}`.

    Keyed by the ids Codex sent, and only by those: an answer to a question it
    did not ask is dropped rather than passed on, because the reply is a map
    and an unknown key would be silently ignored at the far end instead of
    telling anyone.
    """

    asked = {
        str(row.get("id") or "").strip(): row
        for row in params.get("questions") or []
        if isinstance(row, dict) and str(row.get("id") or "").strip()
    }
    answers: dict[str, Any] = {}
    for row in response.get("answers") or []:
        if not isinstance(row, dict):
            continue
        question_id = str(row.get("question_id") or "").strip()
        if question_id not in asked:
            continue

        raw_labels = row.get("option_labels")
        labels = (
            [str(value).strip() for value in raw_labels if str(value).strip()]
            if isinstance(raw_labels, list)
            else []
        )
        single = str(row.get("option_label") or "").strip()
        if single and single not in labels:
            labels.append(single)
        free_text = str(row.get("free_text") or "").strip()

        if labels and free_text:
            raise CodexProtocolError(
                f"answer for Codex question {question_id!r} selects an option "
                "and supplies free text"
            )
        if len(labels) > 1:
            raise CodexProtocolError(
                f"answer for Codex question {question_id!r} selects more than one option"
            )

        question = asked[question_id]
        declared = {
            str(option.get("label") or "").strip()
            for option in question.get("options") or []
            if isinstance(option, dict) and str(option.get("label") or "").strip()
        }
        if labels and labels[0] not in declared:
            raise CodexProtocolError(
                f"answer for Codex question {question_id!r} selects undeclared "
                f"option {labels[0]!r}"
            )
        if free_text and declared and not bool(question.get("isOther")):
            raise CodexProtocolError(
                f"Codex question {question_id!r} does not accept free text"
            )

        chosen = labels or ([f"user_note: {free_text}"] if free_text else [])
        if not chosen:
            raise CodexProtocolError(
                f"answer for Codex question {question_id!r} is empty"
            )
        answers[question_id] = {"answers": chosen}
    if set(answers) != set(asked):
        raise CodexProtocolError(
            "the answer does not name every question Codex asked"
        )
    return {"answers": answers}
