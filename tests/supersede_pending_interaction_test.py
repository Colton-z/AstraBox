"""A superseding input must retire a question whose engine is already gone.

Answering an interaction is a live side channel: the runner's wait lives in
the box's memory, so the engine-control path reports a missing runtime, a
confirmed-gone sandbox, or an expired wait as typed 409s rather than pretending
to have delivered an answer. That contract is right for a user answering their
own question.

It cannot be the whole answer for the channel spine, which declines an open
question *before* dispatching the independent message that supersedes it. A
409 there is fatal to the message: with the decline as a precondition, an
interaction whose sandbox is dead blocks every later message on that thread,
and a channel participant has neither a composer nor a card to clear it. An
absent engine is the reason the decline is unnecessary — nothing can record a
refusal — so the platform retires the interaction itself and the new message
runs.

The distinction stays narrow. Only those three codes mean there is no wait
left to answer; every other failure propagates, or a question that *could*
have been declined would be dropped without one.
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

    def __init__(self, *, answer: Any, conversation_state: str) -> None:
        self.answer_calls: list[tuple[str, str, dict[str, Any]]] = []
        self._answer = answer
        self._sessions_repo = AsyncMock()
        self._session_events_repo = AsyncMock()
        self._interaction_snapshots_repo = AsyncMock()
        self._session_snapshots_repo = AsyncMock()
        self._session_snapshots_repo.get_snapshot.return_value = {
            "current_turn_id": _TURN,
            "conversation_state": conversation_state,
        }

    async def answer_pending_interaction(  # type: ignore[override]
        self,
        user: UserContext,
        session_id: str,
        interaction_id: str,
        answer: dict[str, Any],
    ) -> dict[str, Any]:
        self.answer_calls.append((session_id, interaction_id, dict(answer)))
        if isinstance(self._answer, Exception):
            raise self._answer
        return dict(self._answer)


def _abandon_error(code: str) -> APIError:
    return APIError(code=code, message=f"{code} for the test", status_code=409)


class SupersedePendingInteractionTest(unittest.IsolatedAsyncioTestCase):
    async def test_live_engine_still_receives_the_decline(self) -> None:
        kernel = _Kernel(
            answer={"interaction_id": _INTERACTION, "answered": True},
            conversation_state="WAITING_FOR_INTERACTION",
        )

        with patch(_SETTLE, new=AsyncMock()) as settle:
            result = await kernel.supersede_pending_interaction(
                _USER, _SID, _INTERACTION
            )

        self.assertEqual(
            kernel.answer_calls, [(_SID, _INTERACTION, {"decline": True})]
        )
        self.assertTrue(result["answered"])
        # A live engine records the refusal as that tool's result; settling the
        # turn behind its back would strand the continuation it is about to run.
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
                    answer=_abandon_error(code),
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
            answer=APIError(
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

    async def test_a_turn_that_is_not_parked_is_never_settled(self) -> None:
        # ``settle_parked_turn`` appends its terminal event before the CAS that
        # rejects an unparked turn, so calling it on a running turn leaves a
        # ``turn.failed`` in the journal that nothing retracts.
        kernel = _Kernel(
            answer=_abandon_error("ENGINE_RUNTIME_UNAVAILABLE"),
            conversation_state="PROCESSING",
        )

        with patch(_SETTLE, new=AsyncMock()) as settle:
            result = await kernel.supersede_pending_interaction(
                _USER, _SID, _INTERACTION
            )

        settle.assert_not_awaited()
        self.assertTrue(result["abandoned"])
        # The projection still has to be cleared: the pending card is what
        # blocks the superseding message.
        kernel._sessions_repo.clear_pending_interaction.assert_awaited_once_with(
            _SID,
            interaction_id=_INTERACTION,
        )

    async def test_an_old_abandon_cannot_clear_a_newer_interaction(self) -> None:
        kernel = _Kernel(
            answer=_abandon_error("ENGINE_RUNTIME_UNAVAILABLE"),
            conversation_state="PROCESSING",
        )
        # The repository's compare-and-set reports that another interaction
        # replaced this id before cleanup reached the durable Session row.
        kernel._sessions_repo.clear_pending_interaction.return_value = False

        with patch(_SETTLE, new=AsyncMock()) as settle:
            result = await kernel.supersede_pending_interaction(
                _USER, _SID, _INTERACTION
            )

        self.assertTrue(result["abandoned"])
        settle.assert_not_awaited()
        kernel._sessions_repo.clear_pending_interaction.assert_awaited_once_with(
            _SID,
            interaction_id=_INTERACTION,
        )
        # The CAS's false result is the proof that the newer id was left alone;
        # there is deliberately no unconditional update fallback.
        kernel._sessions_repo.update_session.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
