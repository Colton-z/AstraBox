"""Codex app-server items and requests, translated once.

Codex names three nested things and AstraBox needs all three kept straight: a
**thread** is the conversation, a **turn** is one user request and everything
the agent does about it, and an **item** is one piece of that work — the
agent's message, its reasoning, a command it ran, a file it changed. Items
have an explicit lifecycle (``item/started`` … deltas … ``item/completed``)
and carry a stable ``itemId``, so a streamed block opens and closes on the
engine's own boundaries rather than on any counting done here.

This module translates items. Where a response starts and ends — Codex's
``turn/started`` and ``turn/completed`` — and what the thread's status is are
read by :mod:`~astrabox.core.service.orchestrator.engine.codex_runtime_client`,
which owns the response boundaries. ``item/completed`` ends one piece of work
and the turn keeps going: a command finishing is usually the middle of an
answer, not its end.

An item can reach the translator whole. A reader that attaches to a running
thread receives the deltas of an item whose ``item/started`` went to a
connection that is gone, and a reader that catches up reads completed items
back from the thread's persisted history. Either way the completed item is
the authority: a prose item becomes one block holding its completed text, a
tool item its input and output parts.

Interactions arrive as JSON-RPC *requests* rather than notifications, because
Codex expects an answer routed back by the request's id. Four of them can
reach a person: two command/file approvals, a permissions grant, and a
question with options. Each is translated into one of the presentations
AstraBox renders, with the engine's whole request preserved in ``raw_input``.
"""

from __future__ import annotations

import json
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


#: Item types that stream as prose, and the frame family each one opens.
PROSE_ITEMS = {"agentMessage": "text", "reasoning": "reasoning", "plan": "text"}

#: Item types that are a tool call in the console's sense: something the agent
#: ran, with an input worth showing and an output worth showing.
CODEX_TOOL_ITEM_TYPES = frozenset(
    {"commandExecution", "fileChange", "mcpToolCall", "webSearch", "todoList"}
)

#: Items the platform holds as child runs. The Agents panel and the durable
#: `/child-runs` resource own a spawned sub-agent's identity and lifecycle, so
#: a diagnostic beside them would record the same thing twice.
_CHILD_RUN_ITEMS = frozenset({"collabAgentToolCall", "subAgentActivity"})

#: Notifications AstraBox already holds the fact for. Everything NOT listed
#: here — including anything a newer Codex adds — is kept as a diagnostic.
_DROPPED_NOTIFICATIONS = frozenset(
    {
        # The conversation's identity and lifecycle: the platform's own
        # session row is the record of these.
        "thread/started",
        "thread/name/updated",
        "thread/tokenUsage/updated",
        # This connection's control plane, not the conversation's content.
        "remoteControl/status/changed",
        "account/updated",
        "account/rateLimits/updated",
    }
)

#: Delta notifications, and the prose family each extends.
DELTA_FAMILIES = {
    "item/agentMessage/delta": "text",
    "item/reasoning/summaryTextDelta": "reasoning",
    "item/reasoning/textDelta": "reasoning",
    "item/plan/delta": "text",
}


def raw_event_frame(subtype: str, raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "data-raw-event",
        "data": {
            "event_type": "codex.app_server",
            "subtype": subtype or "unknown",
            "raw": dict(raw),
        },
    }


def prose_text(item: dict[str, Any]) -> str:
    """A completed prose item's text, read from the field its type declares.

    ``agentMessage`` and ``plan`` carry ``text``; ``reasoning`` carries its
    summary sections and raw content as lists, which stream as one block.
    """

    if str(item.get("type") or "") == "reasoning":
        return "".join([*(item.get("summary") or []), *(item.get("content") or [])])
    return str(item.get("text") or "")


class CodexTurnTranslator:
    """One response's Codex items → AI SDK UI parts.

    A prose item streams as one text or reasoning block keyed by its
    ``itemId``: ``item/started`` opens it, deltas extend it, ``item/completed``
    closes it. A tool item shows its input at ``item/started`` and its output
    at ``item/completed``; its parts are keyed by the tool call id.

    Translation reads the state without changing it; :meth:`advance` applies
    each part once it is shown. The state is therefore a function of the parts
    shown, which is what lets :meth:`state` resume a stream exactly after any
    of them.
    """

    def __init__(self) -> None:
        #: itemId → the family of its prose block open in the output.
        self._open: dict[str, str] = {}
        #: itemId → characters of its open block shown.
        self._lengths: dict[str, int] = {}
        #: Open blocks restored from an earlier reader: their deltas are not
        #: shown again, and completion supplies the text after what was shown.
        self._resumed: set[str] = set()
        #: itemId → how many of its tool parts are shown, in the order
        #: input-start, input-available, output-available, file changes.
        self._tools: dict[str, int] = {}

    # ── state ────────────────────────────────────────────────────────────
    def state(self) -> dict[str, Any]:
        """The open blocks with the characters each has shown, and the tool parts shown."""

        return {
            "streaming": {
                item_id: {"family": family, "length": self._lengths.get(item_id, 0)}
                for item_id, family in self._open.items()
            },
            "tools": dict(self._tools),
        }

    def restore(self, state: dict[str, Any]) -> None:
        """Continue from :meth:`state` taken by an earlier reader.

        Deltas streamed while no reader was attached are gone, so later deltas
        of a restored block are not shown either; its completion finishes it
        with the completed text after the characters already shown.
        """

        for item_id, block in (state.get("streaming") or {}).items():
            family = str(block["family"])
            if family not in {"text", "reasoning"}:
                raise CodexProtocolError(f"cannot resume a {family!r} block")
            self._open[str(item_id)] = family
            self._lengths[str(item_id)] = int(block["length"])
            self._resumed.add(str(item_id))
        for item_id, shown in (state.get("tools") or {}).items():
            self._tools[str(item_id)] = int(shown)

    def advance(self, part: dict[str, Any]) -> None:
        """Record one part as shown."""

        kind = str(part.get("type") or "")
        if kind in {"text-start", "reasoning-start"}:
            self._open[str(part["id"])] = kind.removesuffix("-start")
            self._lengths[str(part["id"])] = 0
        elif kind in {"text-delta", "reasoning-delta"}:
            self._lengths[str(part["id"])] += len(str(part.get("delta") or ""))
        elif kind in {"text-end", "reasoning-end"}:
            self.forget(str(part["id"]))
        elif kind in _TOOL_PART_STAGES:
            self._tools[str(part["toolCallId"])] = _TOOL_PART_STAGES[kind]
        elif kind == "data-file-changes":
            self.forget(str(part["data"]["toolCallId"]))

    def forget(self, item_id: str) -> None:
        """Drop an item whose parts are all shown."""

        self._open.pop(item_id, None)
        self._lengths.pop(item_id, None)
        self._resumed.discard(item_id)
        self._tools.pop(item_id, None)

    # ── translation ──────────────────────────────────────────────────────
    def translate(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        method = str(message.get("method") or "").strip()
        params = message.get("params")
        params = params if isinstance(params, dict) else {}

        if method == "item/started":
            return self._item_started(params)
        if method == "item/completed":
            return self.item_completed(params.get("item"))
        family = DELTA_FAMILIES.get(method)
        if family is not None:
            return self._delta(params, family)
        if method in _DROPPED_NOTIFICATIONS:
            return []
        return [raw_event_frame(method, message)]

    def _item_started(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        item = params.get("item")
        if not isinstance(item, dict):
            raise CodexProtocolError("item/started carries no item object")
        item_id = str(item.get("id") or "").strip()
        item_type = str(item.get("type") or "").strip()
        if not item_id:
            raise CodexProtocolError(f"item/started for {item_type!r} has no id")
        family = PROSE_ITEMS.get(item_type)
        if family is not None:
            return [] if item_id in self._open else [_block_start(item, family)]
        if item_type in CODEX_TOOL_ITEM_TYPES:
            return _tool_input(item)[self._tools.get(item_id, 0):]
        # A userMessage item is the platform's own input coming back, which
        # the client reads as admission; anything else is kept as evidence.
        if item_type != "userMessage" and item_type not in _CHILD_RUN_ITEMS:
            return [raw_event_frame(f"item/started:{item_type}", params)]
        return []

    def item_completed(self, item: Any) -> list[dict[str, Any]]:
        """The parts one completed item still owes, whether or not its start was shown."""

        if not isinstance(item, dict):
            raise CodexProtocolError("item/completed carries no item object")
        item_id = str(item.get("id") or "").strip()
        item_type = str(item.get("type") or "").strip()
        if not item_id:
            raise CodexProtocolError(f"item/completed for {item_type!r} has no id")
        family = PROSE_ITEMS.get(item_type)
        if family is not None:
            return self._prose_completed(item_id, item, family)
        if item_type in CODEX_TOOL_ITEM_TYPES:
            parts = [*_tool_input(item), *_tool_output(item_id, item_type, item)]
            return parts[self._tools.get(item_id, 0):]
        if item_type != "userMessage" and item_type not in _CHILD_RUN_ITEMS:
            return [raw_event_frame(f"item/completed:{item_type}", {"item": dict(item)})]
        return []

    def _prose_completed(self, item_id: str, item: dict[str, Any], family: str) -> list[dict[str, Any]]:
        open_family = self._open.get(item_id)
        text = prose_text(item)
        if open_family is None:
            # Its start was not shown here: the completed text is the block.
            parts = [_block_start(item, family)]
            if text:
                parts.append({"type": f"{family}-delta", "id": item_id, "delta": text})
            return [*parts, {"type": f"{family}-end", "id": item_id}]
        rest = text[self._lengths.get(item_id, 0):] if item_id in self._resumed else ""
        parts = [{"type": f"{open_family}-delta", "id": item_id, "delta": rest}] if rest else []
        return [*parts, {"type": f"{open_family}-end", "id": item_id}]

    def _delta(self, params: dict[str, Any], family: str) -> list[dict[str, Any]]:
        item_id = str(params.get("itemId") or "").strip()
        open_family = self._open.get(item_id)
        if open_family is None or item_id in self._resumed:
            # The block's start went to an earlier reader or never reached
            # this one; its completion shows the text this reader lacks.
            return []
        if open_family != family:
            raise CodexProtocolError(
                f"item {item_id!r} is open as {open_family!r}, not {family!r}"
            )
        return [{"type": f"{family}-delta", "id": item_id, "delta": str(params.get("delta") or "")}]


#: Tool parts in the order they are shown, and how many are shown after each.
_TOOL_PART_STAGES = {"tool-input-start": 1, "tool-input-available": 2, "tool-output-available": 3}


def _block_start(item: dict[str, Any], family: str) -> dict[str, Any]:
    frame: dict[str, Any] = {"type": f"{family}-start", "id": str(item["id"])}
    # Codex's own classification of an assistant message, carried rather
    # than dropped. A turn can emit more than one `agentMessage`, and the
    # app-server spec says the answer is the one whose phase is
    # `final_answer`; the others are mid-turn narration. `phase` is optional
    # by that same spec, so its absence travels as absence.
    phase = item.get("phase")
    if isinstance(phase, str) and phase.strip():
        frame["phase"] = phase.strip()
    return frame


def _tool_input(item: dict[str, Any]) -> list[dict[str, Any]]:
    item_id = str(item["id"])
    item_type = str(item["type"])
    return [
        public_ui_frame({
            "type": "tool-input-start",
            "toolCallId": item_id,
            "toolName": item_type,
            "dynamic": True,
        }),
        public_ui_frame({
            "type": "tool-input-available",
            "toolCallId": item_id,
            "toolName": item_type,
            "dynamic": True,
            # The item verbatim: Codex names a command's argv, a patch's
            # changes and an MCP call's arguments differently, and which of
            # those the reader is looking at is the engine's to say.
            "input": dict(item),
        }),
    ]


def _tool_output(item_id: str, item_type: str, item: dict[str, Any]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = [{
        "type": "tool-output-available",
        "toolCallId": item_id,
        "output": dict(item),
    }]
    if item_type != "fileChange" or item.get("status") != "completed":
        return parts
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
        parts.append(file_changes_frame(item_id, item_type, changes))
    return parts


# ── interactions ─────────────────────────────────────────────────────────
#: Server requests that need a person. Each maps to the presentation AstraBox
#: renders and to the decision vocabulary its answer must use.
APPROVAL_METHODS = {
    "item/commandExecution/requestApproval": "commandExecution",
    "item/fileChange/requestApproval": "fileChange",
    "item/permissions/requestApproval": "permissions",
    "mcpServer/elicitation/request": "mcpToolCall",
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

#: McpServerElicitationAction at rust-v0.160.0. A tool approval accepts an
#: empty form; it does not grant persistent approval or fabricate form values.
CODEX_MCP_DECISIONS: tuple[dict[str, Any], ...] = (
    {"id": "accept", "denial": False, "reply": "Approved.", "comment_prefix": "Additional notes"},
    {"id": "decline", "denial": True, "reply": "Declined.", "comment_prefix": "Reason"},
    {"id": "cancel", "denial": True, "reply": "Cancelled.", "comment_prefix": "Reason"},
)

#: Which vocabulary each request is answered in.
_DECISIONS_BY_KIND: dict[str, tuple[dict[str, Any], ...]] = {
    "commandExecution": CODEX_DECISIONS,
    "fileChange": CODEX_DECISIONS,
    "permissions": CODEX_PERMISSION_DECISIONS,
    "mcpToolCall": CODEX_MCP_DECISIONS,
}

#: What is being asked, for a request that carries no `reason` of its own.
_APPROVAL_PROMPTS = {
    "commandExecution": "Codex is asking to run a command.",
    "fileChange": "Codex is asking to change files.",
    "permissions": "Codex is asking for additional permissions.",
    "mcpToolCall": "Codex is asking to call an MCP tool.",
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
    elif kind == "mcpToolCall":
        lines.append(f"MCP server: {params.get('serverName', '')}")
        if isinstance(params.get("_meta"), dict):
            lines.append(json.dumps(params["_meta"], ensure_ascii=False, sort_keys=True))
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
    if kind == "mcpToolCall":
        schema = params.get("requestedSchema")
        if params.get("mode") != "form" or not isinstance(schema, dict) or schema.get("properties") != {}:
            raise CodexProtocolError("platform MCP approvals require an empty native elicitation form")
        # The vendor cannot correlate elicitations with a tool item id.
        # The client retains the JSON-RPC id separately for the response.
    elif not tool_use_id:
        raise CodexProtocolError(f"{method} carries no itemId")
    reason = str(params.get("message") if kind == "mcpToolCall" else params.get("reason") or "").strip()
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
    if kind == "mcpToolCall":
        return {"action": chosen, "content": {} if chosen == "accept" else None, "_meta": None}
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
