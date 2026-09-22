"""Turn-fencing sentinel.

Raised when the worker loses a snapshot/checkpoint CAS because its turn was
superseded; it must propagate to the ``run_once`` boundary rather than be
swallowed. Imported by the worker and the split-brain fencing tests.
"""
from __future__ import annotations


class _TurnFencedOut(Exception):
    """Raised when the worker's snapshot CAS fails because the turn was
    superseded (stale detection or a new turn overwrote current_turn_id)."""
    def __init__(self, turn_id: str) -> None:
        super().__init__(f"turn {turn_id} fenced out: snapshot current_turn_id no longer matches")
        self.turn_id = turn_id
