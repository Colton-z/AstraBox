"""A message that could not be delivered has one outcome.

The request that accepts a StartTurn delivers it, and the turn's worker then
delivers the same command. When the first delivery cannot reach a runtime, the
refusal it returns has to be the message's outcome: the worker must not attach
or replace a runtime for it afterwards, and the same message sent again must be
answered with that refusal rather than a server error.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.engine.input_delivery import (
    InputAlreadySettled,
    InputDeliveryRefused,
    refusal_record,
    settled_input_outcome,
)
from astrabox.core.service.orchestrator.session_kernel.service_mixins.turn_dispatch import (
    TurnDispatchStreamingMixin,
)
from astrabox.core.service.orchestrator.turn_service import TurnService

SESSION_ID = "20000000-0000-0000-0000-000000000001"
COMMAND_ID = f"{SESSION_ID}:client-1"


class _Journal:
    def __init__(self, events: list[dict[str, Any]] | None = None) -> None:
        self.events = [deepcopy(event) for event in events or []]

    async def list_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        event_type: str | None = None,
        causation_id: str | None = None,
        limit: int = 500,
        **_filters: Any,
    ) -> list[dict[str, Any]]:
        rows = [
            event
            for event in self.events
            if event["session_id"] == session_id
            and int(event["event_seq"]) > after_seq
            and (event_type is None or event["event_type"] == event_type)
            and (causation_id is None or event.get("causation_id") == causation_id)
        ]
        return deepcopy(rows[:limit])

    def settle(self, payload: dict[str, Any]) -> None:
        self.events.append(
            {
                "session_id": SESSION_ID,
                "event_seq": len(self.events) + 1,
                "event_type": "turn.failed",
                "causation_id": COMMAND_ID,
                "payload": payload,
            }
        )


def _delivery_service(journal: _Journal) -> TurnService:
    service = object.__new__(TurnService)
    service._input_delivery_locks = {}
    service._session_events_repo = journal  # type: ignore[assignment]
    service._sessions_repo = SimpleNamespace(  # type: ignore[assignment]
        get_session=AsyncMock(
            return_value={"session_id": SESSION_ID, "session_kind": "agent_chat"}
        )
    )
    return service


async def test_the_worker_answers_the_refusal_instead_of_replacing_again() -> None:
    journal = _Journal()
    service = _delivery_service(journal)
    attempts: list[str] = []
    replacing = asyncio.Event()
    replacement_failed = asyncio.Event()

    async def ensure(_session: dict[str, Any], **kwargs: Any) -> Any:
        attempts.append(str(kwargs["command_id"]))
        replacing.set()
        await replacement_failed.wait()
        raise InputDeliveryRefused(
            code="SANDBOX_GONE",
            message="the sandbox this conversation was on is gone",
            status_code=409,
            data={"sandbox_gone": True},
        )

    service.ensure_runtime_for_input_delivery = ensure  # type: ignore[method-assign]

    async def settle(refused: InputDeliveryRefused) -> None:
        journal.settle(
            {
                "failure_phase": "pre_dispatch",
                "error_text": refused.message,
                "refusal": refusal_record(refused),
            }
        )

    def deliver(**extra: Any) -> Any:
        return service.deliver_pending_inputs(
            user=UserContext(user_id="owner"),
            session={"session_id": SESSION_ID},
            session_id=SESSION_ID,
            requested_command_id=COMMAND_ID,
            permission_mode=None,
            **extra,
        )

    accepting_request = asyncio.create_task(deliver(on_refused=settle))
    await replacing.wait()
    # The turn's worker reaches the lock while the replacement is under way.
    turn_worker = asyncio.create_task(deliver())
    await asyncio.sleep(0)
    replacement_failed.set()

    with pytest.raises(InputDeliveryRefused) as refused:
        await accepting_request
    with pytest.raises(InputAlreadySettled) as settled:
        await turn_worker

    assert attempts == [COMMAND_ID], "a refused message was given a second replacement"
    assert settled.value.code == refused.value.code == "SANDBOX_GONE"
    assert settled.value.message == refused.value.message
    assert settled.value.status_code == 409
    assert settled.value.data == {"sandbox_gone": True}


async def test_a_turn_settled_during_the_replacement_is_not_delivered() -> None:
    journal = _Journal()
    service = _delivery_service(journal)

    async def ensure(_session: dict[str, Any], **_kwargs: Any) -> Any:
        # The replacement succeeds, but another server settled the turn while
        # it ran, so the FIFO does not owe this input.
        journal.settle(
            {"failure_phase": "pre_dispatch", "error_text": "turn failed before dispatch"}
        )
        return SimpleNamespace(engine_client=object())

    service.ensure_runtime_for_input_delivery = ensure  # type: ignore[method-assign]

    with pytest.raises(InputAlreadySettled) as settled:
        await service.deliver_pending_inputs(
            user=UserContext(user_id="owner"),
            session={"session_id": SESSION_ID},
            session_id=SESSION_ID,
            requested_command_id=COMMAND_ID,
            permission_mode=None,
        )

    assert settled.value.code == "INPUT_NOT_DELIVERED"
    assert settled.value.retryable is False
    assert "turn failed before dispatch" in settled.value.message


async def test_only_a_pre_dispatch_failure_is_a_settled_input() -> None:
    journal = _Journal()
    journal.settle({"failure_phase": "post_dispatch", "error_text": "engine failed"})

    assert await settled_input_outcome(journal, SESSION_ID, COMMAND_ID) is None


async def test_the_same_message_sent_again_is_answered_before_any_recovery() -> None:
    journal = _Journal()
    journal.settle(
        {
            "failure_phase": "pre_dispatch",
            "error_text": "gone",
            "refusal": {
                "code": "SANDBOX_GONE",
                "message": "the sandbox this conversation was on is gone",
                "status_code": 409,
                "data": {"sandbox_gone": True},
            },
        }
    )
    service = TurnDispatchStreamingMixin()
    service._must_get_projection_backed_session = AsyncMock(  # type: ignore[attr-defined]
        return_value={"session_id": SESSION_ID, "runtime_unavailable": True}
    )
    service._session_events_repo = journal  # type: ignore[attr-defined]
    service._get_kernel_session_snapshot = AsyncMock()  # type: ignore[attr-defined]
    service.recover_session = AsyncMock()  # type: ignore[attr-defined]

    with pytest.raises(InputAlreadySettled) as settled:
        await service.dispatch_turn_input(
            UserContext(user_id="owner"),
            SESSION_ID,
            "What is the codeword?",
            client_message_id="client-1",
        )

    assert settled.value.code == "SANDBOX_GONE"
    assert settled.value.status_code == 409
    assert settled.value.to_error_envelope()["retryable"] is False
    service.recover_session.assert_not_awaited()
    service._get_kernel_session_snapshot.assert_not_awaited()
