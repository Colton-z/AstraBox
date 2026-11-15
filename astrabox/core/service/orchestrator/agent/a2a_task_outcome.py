"""How an Agent says what became of the A2A task it answers, and how A2A carries it.

Inside the platform an Agent conversation that an A2A context created holds one
tool, ``report_outcome`` on the ``a2a`` platform MCP server
(``runtime/mcp_servers.py``). A call records a tool card for the conversation
(``tool_cards.py``); the session reader binds the card to the call in the
response's parts, the same way for every engine. When the response ends, the
A2A projection (``agent_a2a_service.py``) reads the response's last outcome
card and expresses it on the wire with A2A 1.0 core elements only: the task
state (``TASK_STATE_COMPLETED`` or ``TASK_STATE_REJECTED``) and the agent's
text as ``TaskStatus.message``.

What core A2A has no element for is the task outcome extension
(:data:`TASK_OUTCOME_EXTENSION`), declared on the Agent Card and present only
when a client activates it with the ``A2A-Extensions`` header: ``handback`` on a
rejected task's status message, under ``Message.metadata`` keyed by the
extension URI, and the code references (repository, branch, last pushed commit,
pull request) on the result ``Artifact.metadata`` under the same key. The
specification is the "Task outcome extension" section of ``docs/a2a.md``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlparse

from astrabox.core.service.artifacts.service import invalid
from astrabox.core.service.orchestrator.tool_cards import CARD_PART_TYPE

#: The extension's URI, where its specification is published (``docs/a2a.md``).
TASK_OUTCOME_EXTENSION = "https://www.astrabox.ai/docs/a2a#task-outcome-v1"
#: The tool card a ``report_outcome`` call records.
TASK_OUTCOME_CARD = "a2a_task_outcome"
REPORT_OUTCOME_TOOL = "report_outcome"
COMPLETED = "TASK_STATE_COMPLETED"
REJECTED = "TASK_STATE_REJECTED"
#: The extension's fields on the result artifact, in the order the tool lists them.
CODE_REFERENCES = ("repository", "branch", "commit", "pull_request")
_MESSAGE_CHARS = 2000
_REFERENCE_CHARS = 300
_COMMIT = re.compile(r"^[0-9a-f]{7,64}$")


def task_outcome_tools() -> list[dict[str, Any]]:
    """The ``a2a`` server's one tool."""

    reference = {"type": "string", "maxLength": _REFERENCE_CHARS}
    return [
        {
            "name": REPORT_OUTCOME_TOOL,
            "description": (
                "Report what became of the request this conversation answers, which another agent or "
                "service sent over A2A, before your final reply. TASK_STATE_COMPLETED: you did what it "
                "asked; message says what you did. TASK_STATE_REJECTED: you did not and will not do it; "
                "message says why, and with handback true the request belongs to another agent or must "
                "be split, and message says what should happen instead. When the work is in a git "
                "repository, name the repository, the branch and the last commit you pushed, and the "
                "pull request if you opened one. The sender reads the last report of your reply as the "
                "task's outcome; a reply without one is completed."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "state": {"type": "string", "enum": [COMPLETED, REJECTED]},
                    "message": {
                        "type": "string",
                        "maxLength": _MESSAGE_CHARS,
                        "description": "What you did, or why you did not and what should happen instead.",
                    },
                    "handback": {
                        "type": "boolean",
                        "description": (
                            "With TASK_STATE_REJECTED only: the request is for another agent or must be "
                            "split, so its sender should route it again."
                        ),
                    },
                    "repository": {**reference, "description": "The repository, such as owner/name or its URL."},
                    "branch": {**reference, "description": "The branch you pushed the work to."},
                    "commit": {**reference, "description": "The full or abbreviated id of the last commit you pushed."},
                    "pull_request": {**reference, "description": "The pull request's URL."},
                },
                "required": ["state", "message"],
                "additionalProperties": False,
            },
        }
    ]


def task_outcome(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """A ``report_outcome`` call's arguments as the record keeps them.

    Raises ``INVALID_REQUEST`` naming the first argument that is not one the
    tool describes: a state other than the two, a blank or overlong message,
    ``handback`` that is not a boolean or comes with a completed task, a code
    reference that is blank, overlong or contains whitespace, a commit that is
    not a hexadecimal id, or a pull request that is not an http(s) URL.
    """

    state = arguments.get("state")
    if state not in (COMPLETED, REJECTED):
        raise invalid(f"state must be {COMPLETED} or {REJECTED}")
    message = arguments.get("message")
    if not isinstance(message, str) or not message.strip():
        raise invalid("message must say what you did, or why you did not")
    if len(message) > _MESSAGE_CHARS:
        raise invalid(f"message must be at most {_MESSAGE_CHARS} characters")
    outcome: dict[str, Any] = {"state": state, "message": message.strip()}
    handback = arguments.get("handback", False)
    if not isinstance(handback, bool):
        raise invalid("handback must be true or false")
    if handback and state != REJECTED:
        raise invalid(f"handback comes with {REJECTED} only")
    if handback:
        outcome["handback"] = True
    for name in CODE_REFERENCES:
        if name not in arguments:
            continue
        value = arguments[name]
        if not isinstance(value, str) or not value.strip() or len(value) > _REFERENCE_CHARS or any(
            character.isspace() for character in value.strip()
        ):
            raise invalid(f"{name} must be one word of at most {_REFERENCE_CHARS} characters")
        value = value.strip()
        if name == "commit":
            value = value.lower()
            if not _COMMIT.match(value):
                raise invalid("commit must be a hexadecimal commit id")
        if name == "pull_request" and urlparse(value).scheme not in ("http", "https"):
            raise invalid("pull_request must be the pull request's http(s) URL")
        outcome[name] = value
    return outcome


def outcome_of(parts: Iterable[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The outcome the last ``report_outcome`` card among a response's parts records."""

    found: dict[str, Any] | None = None
    for part in parts:
        data = part.get("data")
        if part.get("type") == CARD_PART_TYPE and isinstance(data, Mapping) and data.get("kind") == TASK_OUTCOME_CARD:
            found = {key: data[key] for key in ("state", "message", "handback", *CODE_REFERENCES) if key in data}
    return found


def code_references(outcome: Mapping[str, Any]) -> dict[str, str]:
    """The extension's fields an outcome names for the result artifact."""

    return {name: str(outcome[name]) for name in CODE_REFERENCES if name in outcome}
