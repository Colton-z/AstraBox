"""Archiving a conversation may retain the Agent-shared box successfully.

The archive releases this conversation's isolated placement. ``RETAINED`` is a
successful disposal outcome when the Agent remains the durable owner of the
box; only the conversation's binding is cleared.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.model import SessionState
from astrabox.core.service.orchestrator.session_kernel.workers.lifecycle.commands import (
    _LifecycleCommandsMixin,
)
from astrabox.seams.sandbox_disposal import SandboxDestruction

_SESSION_ID = "s-1"
_SANDBOX_ID = "sb-1"


class _Commands(_LifecycleCommandsMixin):
    """The mixin with only what `_terminate_session_direct` reaches for."""

    def __init__(self, destruction: SandboxDestruction) -> None:
        self.updates: list[dict[str, Any]] = []
        self._runtime_manager = SimpleNamespace(
            terminate_runtime=self._terminate_runtime,
            get_runtime=lambda _session_id, sandbox_id=None: None,
        )
        self._sessions_repo = SimpleNamespace(update_session=self._update_session)
        self._destruction = destruction

    async def _terminate_runtime(self, session_id: str, **_kwargs: Any) -> SandboxDestruction:
        return self._destruction

    async def _update_session(self, session_id: str, updates: dict[str, Any]) -> None:
        self.updates.append(updates)

    async def _assert_conversation_safe_to_take_offline(self, **_kwargs: Any) -> None:
        return None

    def _is_assistant_user_conversation(self, _session: dict[str, Any]) -> bool:
        return False

    async def _settle_parked_turn_on_reclaim(self, _session_id: str) -> None:
        return None


def _session() -> dict[str, Any]:
    return {
        "session_id": _SESSION_ID,
        "state": SessionState.READY.value,
        "sandbox_id": _SANDBOX_ID,
    }


async def test_a_retained_shared_box_archives_instead_of_erroring() -> None:
    commands = _Commands(
        SandboxDestruction(
            outcome="RETAINED",
            sandbox_id=_SANDBOX_ID,
            detail=(
                "session ran in an isolated session of an agent-owned box; the "
                "session was closed and the box remains durably named"
            ),
        )
    )

    result = await commands._terminate_session_direct(
        session=_session(), session_id=_SESSION_ID
    )

    assert result["status"] == "sandbox-reclaimed"
    # The box was not killed and the result must not claim it was: RETAINED
    # says a longer-lived owner still holds it.
    assert result["killed"] is False
    assert commands.updates, "a released placement still settles the session row"


async def test_an_unconfirmed_termination_still_refuses() -> None:
    """The control: an outcome that leaves a box unaccounted for still fails.

    Without this, the case above would pass just as well against code that
    accepted every outcome, which is the opposite defect — a conversation
    archived while its box runs on unnamed.
    """
    commands = _Commands(
        SandboxDestruction(
            outcome="UNCONFIRMED",
            sandbox_id=_SANDBOX_ID,
            detail="the terminate request never reached the control plane",
        )
    )

    with pytest.raises(APIError) as caught:
        await commands._terminate_session_direct(
            session=_session(), session_id=_SESSION_ID
        )

    assert caught.value.code == "AGENT_RUNTIME_ERROR"
    assert caught.value.status_code == 502
    assert "UNCONFIRMED" in caught.value.message
    assert not commands.updates, (
        "a box whose death nobody established must keep its name: the refusal "
        "has to abort before the clearing write"
    )
