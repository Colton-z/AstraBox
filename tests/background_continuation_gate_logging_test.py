"""A held background manifest says which gate is holding it.

`_materialize_background_continuation_event` has ten early exits and returned a
bare `False` from every one, so "still polling", "wrong state", "data missing"
and "the subagent has not answered yet" were one answer. Reading the code was
the only way to tell them apart, which is what a log line is for.

The dedup is tested beside the messages because it is the part that decays
silently: the reconcile loop re-reads every open manifest every ten seconds, so
a gate that logs per pass is three hundred identical lines a minute, and a
reader stops looking at the one place the answer was.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from astrabox.core.service.orchestrator.session_kernel.service_mixins.background_continuation import (
    BackgroundContinuationMixin,
)

_OPENED_EVENT: dict[str, Any] = {
    "session_id": "sess-1",
    "turn_id": "turn-1",
    "event_seq": 11,
    "payload": {"control_ids": ["task_a"]},
}

_LOGGER = (
    "astrabox.core.service.orchestrator.session_kernel.service_mixins.background_continuation"
)


class _Snapshots:
    def __init__(self, state: str | None) -> None:
        self._state = state

    async def get_snapshot(self, session_id: str) -> dict[str, Any] | None:
        return None if self._state is None else {"conversation_state": self._state}


class _Messages:
    def __init__(self, message: dict[str, Any] | None) -> None:
        self._message = message

    async def get_assistant_message_for_turn(
        self,
        session_id: str,
        *,
        turn_id: str,
    ) -> dict[str, Any] | None:
        return self._message


class _Journal:
    async def list_events(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []


class _Harness(BackgroundContinuationMixin):
    """Only what the gates ahead of the first failure actually touch.

    Anything past the gate under test is absent rather than stubbed, so a
    silently-widened path fails here instead of being answered by a double.
    """

    def __init__(
        self,
        *,
        conversation_state: str | None = "IDLE",
        parent_message: dict[str, Any] | None = None,
    ) -> None:
        self._session_snapshots_repo = _Snapshots(conversation_state)
        self._message_view = _Messages(parent_message)
        self._session_events_repo = _Journal()


async def _run(harness: _Harness, opened_event: dict[str, Any] | None = None) -> bool:
    return await harness._materialize_background_continuation_event(
        dict(opened_event or _OPENED_EVENT)
    )


async def test_a_manifest_held_because_a_turn_is_running_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = _Harness(conversation_state="RUNNING")

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        assert await _run(harness) is False

    line = "\n".join(record.getMessage() for record in caplog.records)
    assert "gate=conversation_not_idle" in line
    # The state it was actually in, not just that it was not IDLE: "which gate"
    # narrows the code, "which state" narrows the cause.
    assert "conversation_state=RUNNING" in line
    assert "session=sess-1" in line and "opened_event_seq=11" in line


async def test_a_manifest_whose_parent_message_is_gone_says_so_at_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Distinct from the state gates: this one cannot resolve by waiting."""
    harness = _Harness(conversation_state="IDLE", parent_message=None)

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        assert await _run(harness) is False

    held = [r for r in caplog.records if "gate=parent_message_missing" in r.getMessage()]
    assert held, [r.getMessage() for r in caplog.records]
    assert held[0].levelno == logging.WARNING


async def test_a_malformed_manifest_row_says_so_without_touching_a_repository() -> None:
    """The first gate runs before any repository, so the harness has none."""
    harness = BackgroundContinuationMixin()

    result = await harness._materialize_background_continuation_event(
        {"session_id": "", "turn_id": "", "event_seq": 3, "payload": None}
    )

    assert result is False


async def test_the_same_gate_is_reported_once_not_once_per_pass(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The reason a line per early return would not have been an improvement."""
    harness = _Harness(conversation_state="RUNNING")

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        for _ in range(5):
            assert await _run(harness) is False

    held = [r for r in caplog.records if "gate=conversation_not_idle" in r.getMessage()]
    assert len(held) == 1, [r.getMessage() for r in caplog.records]


async def test_a_manifest_that_moves_to_another_gate_reports_the_new_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Deduping must not silence the transition, which is the whole signal."""
    harness = _Harness(conversation_state="RUNNING")
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await _run(harness)
        # The turn ends; the manifest is now held one gate further along.
        harness._session_snapshots_repo = _Snapshots("IDLE")
        await _run(harness)

    gates = [
        record.getMessage().split("gate=")[1].split(" ")[0]
        for record in caplog.records
        if "gate=" in record.getMessage()
    ]
    assert gates == ["conversation_not_idle", "parent_message_missing"]


async def test_two_manifests_in_one_session_are_reported_separately(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The memo is keyed by manifest, not by session.

    Keyed by session, the second manifest's gate would be swallowed as a repeat
    of the first — and a session running two background turns is ordinary.
    """
    harness = _Harness(conversation_state="RUNNING")

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await _run(harness, {**_OPENED_EVENT, "event_seq": 11})
        await _run(harness, {**_OPENED_EVENT, "event_seq": 12})

    held = [r for r in caplog.records if "gate=conversation_not_idle" in r.getMessage()]
    assert len(held) == 2
    assert {"opened_event_seq=11", "opened_event_seq=12"} <= {
        part for record in held for part in record.getMessage().split()
    }
