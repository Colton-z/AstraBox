"""A superseding input must retire a question whose engine is already gone.

Stopping a parked turn goes through the engine: the runner's wait lives in the
box's memory, so the engine-control path reports a missing runtime, a
confirmed-gone sandbox, or an expired wait as typed 409s rather than pretending
to have stopped anything.

The channel spine stops a parked turn *before* dispatching the independent
message that supersedes its question. A 409 there would be fatal to the
message: an interaction whose sandbox is dead would block every later message
on that thread, and a channel participant has neither a composer nor a card to
clear it. An absent engine is the reason the stop is unnecessary — nothing is
left waiting — so the platform retires the interaction itself and the new
message runs.

The distinction stays narrow. Only those three codes mean there is no wait
left to stop; every other failure propagates, or a turn that *could* have been
stopped would be retired without it.
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.session_kernel.service_mixins.turn_dispatch import (
    TurnDispatchStreamingMixin,
)

_SID = "session-1"
_TURN = "turn-1"
_INTERACTION = "interaction-1"
_USER = UserContext(user_id="user-1")

_SETTLE = (
    "astrabox.core.service.orchestrator.session_kernel.service_mixins"
    ".turn_dispatch.settle_parked_turn"
)


class _Kernel(TurnDispatchStreamingMixin):
    """The mixin with only the collaborators this one operation touches."""

    def __init__(self, *, interrupt: Any, conversation_state: str) -> None:
        self.interrupt_calls: list[str] = []
        self._interrupt = interrupt
        self._sessions_repo = AsyncMock()
        self._session_events_repo = AsyncMock()
        self._interaction_snapshots_repo = AsyncMock()
        self._session_snapshots_repo = AsyncMock()
        self._session_snapshots_repo.get_snapshot.return_value = {
            "current_turn_id": _TURN,
            "conversation_state": conversation_state,
            "active_interaction_id": _INTERACTION,
        }

    async def _must_get_projection_backed_session(  # type: ignore[override]
        self, user: UserContext, session_id: str, **_: Any
    ) -> dict[str, Any]:
        return {"session_id": session_id, "state": "WAITING_INPUT"}

    def _require_turn_eligible(self, session: dict[str, Any], **_: Any) -> None:  # type: ignore[override]
        return None

    async def _interrupt_turn(  # type: ignore[override]
        self,
        user: UserContext,
        session_id: str,
        **_: Any,
    ) -> dict[str, Any]:
        self.interrupt_calls.append(session_id)
        if isinstance(self._interrupt, Exception):
            raise self._interrupt
        return dict(self._interrupt)


def _abandon_error(code: str) -> APIError:
    return APIError(code=code, message=f"{code} for the test", status_code=409)


class SupersedePendingInteractionTest(unittest.IsolatedAsyncioTestCase):
    async def test_live_engine_is_stopped(self) -> None:
        kernel = _Kernel(
            interrupt={"session_id": _SID, "status": "accepted"},
            conversation_state="WAITING_FOR_INTERACTION",
        )

        with patch(_SETTLE, new=AsyncMock()) as settle:
            result = await kernel.supersede_pending_interaction(
                _USER, _SID, _INTERACTION
            )

        self.assertEqual(kernel.interrupt_calls, [_SID])
        self.assertEqual(result["status"], "accepted")
        # The interrupt command settles the parked turn itself; settling it
        # here as well would race the engine's own terminal.
        settle.assert_not_awaited()
        kernel._sessions_repo.clear_pending_interaction.assert_not_awaited()

    async def test_a_gone_engine_retires_the_question_instead_of_raising(
        self,
    ) -> None:
        for code in (
            "ENGINE_RUNTIME_UNAVAILABLE",
            "INTERACTION_EXPIRED",
            "SANDBOX_GONE",
        ):
            with self.subTest(code=code):
                kernel = _Kernel(
                    interrupt=_abandon_error(code),
                    conversation_state="WAITING_FOR_INTERACTION",
                )

                with patch(_SETTLE, new=AsyncMock()) as settle:
                    result = await kernel.supersede_pending_interaction(
                        _USER, _SID, _INTERACTION
                    )

                self.assertEqual(
                    result,
                    {
                        "interaction_id": _INTERACTION,
                        "answered": False,
                        "abandoned": True,
                        "reason": code,
                        "turn_id": _TURN,
                    },
                )
                settle.assert_awaited_once()
                settled = settle.await_args.kwargs
                self.assertEqual(settled["session_id"], _SID)
                self.assertEqual(settled["turn_id"], _TURN)
                self.assertEqual(settled["status"], "FAILED")
                self.assertEqual(
                    settled["failure_phase"], "interaction_engine_gone"
                )
                # The conversation has to be sendable again, or the next
                # message meets the same closed door.
                kernel._sessions_repo.clear_pending_interaction.assert_awaited_once_with(
                    _SID,
                    interaction_id=_INTERACTION,
                )

    async def test_any_other_failure_still_propagates(self) -> None:
        # Paired with the case above on purpose: that one proves a 409 whose
        # code means "the wait is gone" reaches the abandon path, so this one
        # is evidence about the code rather than about answering always
        # failing.
        kernel = _Kernel(
            interrupt=APIError(
                code="SESSION_BUSY",
                message="session is still creating, please retry",
                status_code=409,
            ),
            conversation_state="WAITING_FOR_INTERACTION",
        )

        with patch(_SETTLE, new=AsyncMock()) as settle:
            with self.assertRaises(APIError) as raised:
                await kernel.supersede_pending_interaction(
                    _USER, _SID, _INTERACTION
                )

        self.assertEqual(raised.exception.code, "SESSION_BUSY")
        settle.assert_not_awaited()
        kernel._sessions_repo.clear_pending_interaction.assert_not_awaited()

    async def test_a_turn_that_is_not_parked_on_the_question_is_not_stopped(self) -> None:
        # Once an answer resumes the turn its continuation runs under the same
        # turn id, while the Session still reports the question until that
        # continuation is confirmed. Stopping "the current turn" there would cut
        # off the answered work.
        kernel = _Kernel(
            interrupt={"session_id": _SID, "status": "accepted"},
            conversation_state="PROCESSING",
        )

        with patch(_SETTLE, new=AsyncMock()) as settle:
            result = await kernel.supersede_pending_interaction(
                _USER, _SID, _INTERACTION
            )

        self.assertEqual(result["status"], "not_parked")
        self.assertEqual(kernel.interrupt_calls, [])
        settle.assert_not_awaited()
        kernel._sessions_repo.clear_pending_interaction.assert_not_awaited()

    async def test_an_old_abandon_cannot_clear_a_newer_interaction(self) -> None:
        kernel = _Kernel(
            interrupt=_abandon_error("ENGINE_RUNTIME_UNAVAILABLE"),
            conversation_state="WAITING_FOR_INTERACTION",
        )
        # The repository's compare-and-set reports that another interaction
        # replaced this id before cleanup reached the durable Session row.
        kernel._sessions_repo.clear_pending_interaction.return_value = False

        with patch(_SETTLE, new=AsyncMock()):
            result = await kernel.supersede_pending_interaction(
                _USER, _SID, _INTERACTION
            )

        self.assertTrue(result["abandoned"])
        kernel._sessions_repo.clear_pending_interaction.assert_awaited_once_with(
            _SID,
            interaction_id=_INTERACTION,
        )
        # The CAS's false result is the proof that the newer id was left alone;
        # there is deliberately no unconditional update fallback.
        kernel._sessions_repo.update_session.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
