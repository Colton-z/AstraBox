"""A silent engine stream is not a finished turn.

Whether a turn is over belongs to the agent runtime. A stream that stays open
and says nothing reports only that the engine has not spoken, and a model can
spend minutes on one step without emitting anything: measured on a real box, a
turn went five minutes without a frame, then delivered a complete answer two
minutes after a spent silence budget had already recorded it as failed.

What the platform may decide from silence is its own question — whether the
sandbox is still there — so the first quiet interval asks the control plane
and nothing here ends the read.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.core.service.orchestrator.session_kernel.workers.turn import bridge_loop
from astrabox.core.service.orchestrator.session_kernel.workers.turn.state import (
    _BridgeRunState,
)


class _Repo:
    def __init__(self, *, fails: bool = False) -> None:
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self._fails = fails

    async def update_session(
        self, session_id: str, patch: dict[str, Any], **_: Any
    ) -> bool:
        if self._fails:
            raise RuntimeError("repo unavailable")
        self.updates.append((session_id, patch))
        return True


def _worker(repo: _Repo) -> SimpleNamespace:
    return SimpleNamespace(_bridge_event_stall_timeout_s=90.0, _sessions_repo=repo)


def _state() -> _BridgeRunState:
    state = _BridgeRunState()
    state.effective_turn_id = "turn-1"
    return state


@pytest.mark.asyncio
async def test_silence_never_ends_the_read() -> None:
    """The engine has not said the turn is over, so the reader keeps reading."""

    repo, state = _Repo(), _state()

    for _ in range(10):
        assert (
            await bridge_loop._note_quiet_interval(_worker(repo), state, "sess-1")
            is None
        )

    assert state.consecutive_quiet_intervals == 10


@pytest.mark.asyncio
async def test_the_first_quiet_interval_asks_whether_the_box_is_alive() -> None:
    """Silence licenses the platform's own question, not the engine's."""

    repo, state = _Repo(), _state()

    await bridge_loop._note_quiet_interval(_worker(repo), state, "sess-1")

    assert len(repo.updates) == 1
    session_id, patch = repo.updates[0]
    assert session_id == "sess-1"
    assert set(patch) == {"sandbox_liveness_suspect_at"}
    assert patch["sandbox_liveness_suspect_at"]


@pytest.mark.asyncio
async def test_the_probe_is_asked_for_once_per_silence() -> None:
    """The mark is a standing request; rewriting it every interval buys nothing."""

    repo, state = _Repo(), _state()

    for _ in range(4):
        await bridge_loop._note_quiet_interval(_worker(repo), state, "sess-1")

    assert len(repo.updates) == 1


@pytest.mark.asyncio
async def test_a_failed_probe_request_still_leaves_the_turn_to_the_engine() -> None:
    """A bookkeeping write that failed is not evidence about the engine's turn."""

    state = _BridgeRunState()

    await bridge_loop._note_quiet_interval(
        _worker(_Repo(fails=True)), state, "sess-1"
    )

    assert state.consecutive_quiet_intervals == 1
