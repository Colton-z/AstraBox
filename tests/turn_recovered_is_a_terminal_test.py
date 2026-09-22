"""A coordinator-recovered turn is terminal in the durable journal.

``TurnCoordinator`` writes ``turn.recovered`` through ``try_claim_event`` before
the snapshot CAS. If that CAS misses, the journal is the terminal authority
while the projection can still show a running turn. ``journal_terminal_for_turn``
must therefore query recovered events alongside engine completion and failure;
otherwise the reconcile lane can continue working a turn that already ended.
"""

from __future__ import annotations

from typing import Any

import pytest

from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    _TERMINAL_EVENT_TYPES,
    journal_terminal_for_turn,
)

_SESSION = "s-1"
_TURN = "t-1"


class _Journal:
    """Answers `list_events` from a fixed set, and records what was asked for."""

    def __init__(self, events: dict[str, list[dict[str, Any]]]) -> None:
        self._events = events
        self.asked: list[str] = []

    async def list_events(self, session_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        event_type = str(kwargs.get("event_type") or "")
        self.asked.append(event_type)
        return list(self._events.get(event_type, []))


async def test_a_recovered_turn_reads_as_finished() -> None:
    journal = _Journal({"turn.recovered": [{"event_type": "turn.recovered", "event_seq": 7}]})

    terminal = await journal_terminal_for_turn(
        journal, session_id=_SESSION, turn_id=_TURN
    )

    assert terminal is not None, (
        "a turn settled from the mirror has ended; answering None here keeps the "
        "reconcile lane working a finished turn for ever"
    )
    assert terminal["event_seq"] == 7


async def test_an_engine_terminal_outranks_a_reconstructed_one() -> None:
    """Order matters only when both exist, and then the direct one wins."""
    journal = _Journal(
        {
            "turn.completed": [{"event_type": "turn.completed", "event_seq": 3}],
            "turn.recovered": [{"event_type": "turn.recovered", "event_seq": 9}],
        }
    )

    terminal = await journal_terminal_for_turn(
        journal, session_id=_SESSION, turn_id=_TURN
    )

    assert terminal is not None and terminal["event_type"] == "turn.completed"


async def test_an_unfinished_turn_still_reads_as_unfinished() -> None:
    """An empty journal remains nonterminal after every terminal type is queried."""
    journal = _Journal({})

    assert await journal_terminal_for_turn(journal, session_id=_SESSION, turn_id=_TURN) is None
    assert journal.asked == list(_TERMINAL_EVENT_TYPES), (
        "every terminal type must actually be queried — a type listed and not "
        "asked for is the same defect with a longer tuple"
    )


@pytest.mark.parametrize("missing", ["session_id", "turn_id"])
async def test_a_query_without_both_ids_asks_nothing(missing: str) -> None:
    journal = _Journal({"turn.completed": [{"event_seq": 1}]})
    ids = {"session_id": _SESSION, "turn_id": _TURN} | {missing: ""}

    assert await journal_terminal_for_turn(journal, **ids) is None
    assert journal.asked == []
