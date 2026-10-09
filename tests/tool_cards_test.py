"""A card is drawn only from a record of the same Session; a tool output only locates it."""

from __future__ import annotations

import json
from typing import Any

import pytest

from astrabox.core.service.orchestrator.engine.frame_scope import (
    is_engine_public_ui_frame,
    public_engine_frame_payload,
)
from astrabox.core.service.orchestrator.tool_cards import card_parts, record_card


class _Cards:
    """The card collection, without a database."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def create(self, card: dict[str, Any]) -> None:
        self.rows.append(dict(card))

    async def named(self, card_ids: list[str]) -> list[dict[str, Any]]:
        return [row for row in self.rows if row["card_id"] in card_ids]


@pytest.mark.asyncio
async def test_only_a_card_recorded_for_the_session_becomes_a_part() -> None:
    cards = _Cards()
    mine = await record_card(
        session_id="s1", server="connections", tool="request_connection",
        kind="connection_request", data={"connectorId": "gmail", "name": "Gmail"}, repository=cards,
    )
    theirs = await record_card(
        session_id="s2", server="connections", tool="request_connection",
        kind="connection_request", data={"connectorId": "notion", "name": "Notion"}, repository=cards,
    )
    invented = "crd_" + "0" * 24
    # A page the agent fetched could repeat any id it saw; only this
    # Session's record draws a card, and only where the id appears.
    output = {"content": [{"type": "text", "text": json.dumps({"card": mine, "also": [theirs, invented, mine]})}]}
    frame = {"type": "tool-output-available", "toolCallId": "call-1", "output": output}

    parts = await card_parts("s1", frame, repository=cards)

    # Kept in the turn's message, not only shown while it streams.
    assert all(is_engine_public_ui_frame(part) for part in parts)
    assert [public_engine_frame_payload(part, frame_seq=None, scope="turn") for part in parts] == [{
        "type": "data-tool-card",
        "id": f"tool-card:call-1:{mine}",
        "data": {
            "toolCallId": "call-1", "cardId": mine, "kind": "connection_request",
            "connectorId": "gmail", "name": "Gmail",
        },
    }]
    assert await card_parts("s1", {**frame, "type": "tool-input-available"}, repository=cards) == []
