"""Cards a platform MCP tool shows in a conversation, authorized by a record.

A platform tool that shows the person something (a connection request, an
artifact) records a card for the conversation it serves and names the card's
id in the text result the engine gives the model. When that result is
translated, the platform looks the id up and, only for a card of that
conversation, adds a ``data-tool-card`` part bound to the tool call. The
console renders cards from those parts alone, so no tool output can draw one:
an id is a locator, the record is the authority. A card part has a stable id
per tool call and card, so a turn translated again from the transcript
produces the same part.

A card belongs to the scope of the address the tool was called through. An
Agent's address serves one Session. An Assistant's engine serves all of its
owner's Sessions through one address and its calls do not say which, so its
cards belong to the Assistant and show in whichever of its Sessions names them.
"""

from __future__ import annotations

import json
import re
import secrets
from typing import Any

from astrabox.core.service.orchestrator.engine.frame_scope import mark_engine_public_ui_frame
from astrabox.persistence.repository.tool_card_repository import ToolCardRepository

CARD_PART_TYPE = "data-tool-card"
_CARD_ID = re.compile(r"\bcrd_[0-9a-f]{24}\b")
#: Tool output scanned for card ids; a platform result names its card early.
_SCAN_LIMIT = 64 * 1024
_CARDS_PER_RESULT = 16


async def record_card(
    *, session_id: str = "", assistant_id: str = "", server: str, tool: str, kind: str,
    data: dict[str, Any], repository: ToolCardRepository | None = None,
) -> str:
    """Record one card for a Session or an Assistant and return its id."""

    if bool(session_id) == bool(assistant_id):
        raise ValueError("a card belongs to exactly one Session or Assistant")
    card_id = f"crd_{secrets.token_hex(12)}"
    await (repository or ToolCardRepository()).create({
        "card_id": card_id,
        "session_id": session_id or None,
        "assistant_id": assistant_id or None,
        "server": server,
        "tool": tool,
        "kind": kind,
        "data": dict(data),
    })
    return card_id


def card_ids_in(output: Any) -> list[str]:
    """The card ids a tool output names, in order, without repeats."""

    text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, default=str)
    found: list[str] = []
    for match in _CARD_ID.finditer(text[:_SCAN_LIMIT]):
        if match.group(0) not in found:
            found.append(match.group(0))
        if len(found) == _CARDS_PER_RESULT:
            break
    return found


async def card_parts(
    session_id: str, frame: dict[str, Any], *, repository: ToolCardRepository | None = None,
) -> list[dict[str, Any]]:
    """The card parts a translated tool result adds, for this Session's cards only.

    A Session's cards are those recorded for it and, in an Assistant's
    Session, those recorded for that Assistant.
    """

    if frame.get("type") != "tool-output-available":
        return []
    tool_call_id = str(frame.get("toolCallId") or "").strip()
    ids = card_ids_in(frame.get("output"))
    if not tool_call_id or not ids or not session_id:
        return []
    records = {
        str(row["card_id"]): row
        for row in await (repository or ToolCardRepository()).named(session_id, ids)
    }
    # Marked as public UI, as an adapter's own data parts are, so the part is
    # kept in the turn's message and not only shown while the turn streams.
    return [
        mark_engine_public_ui_frame({
            "type": CARD_PART_TYPE,
            "id": f"tool-card:{tool_call_id}:{card_id}",
            "data": {
                "toolCallId": tool_call_id,
                "cardId": card_id,
                "kind": records[card_id]["kind"],
                **dict(records[card_id].get("data") or {}),
            },
        })
        for card_id in ids
        if card_id in records
    ]
