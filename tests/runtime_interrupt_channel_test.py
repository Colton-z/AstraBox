"""Turn interruption uses the conversation-bound engine control attachment."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.base import EngineCapabilityManifest
from astrabox.core.service.orchestrator.turn_service import TurnService


def _service_with_acquisition(result: object) -> TurnService:
    service = TurnService.__new__(TurnService)
    service._runtime_ensure = SimpleNamespace(
        acquire_engine_control_runtime=AsyncMock(return_value=result)
    )
    return service


@pytest.mark.asyncio
async def test_turn_interrupt_targets_the_engine_not_the_terminal_register() -> None:
    engine_client = SimpleNamespace(interrupt_active_turn=AsyncMock(return_value=True))
    runtime = SimpleNamespace(
        interrupting=False,
        current_execution_id="exec-of-an-unrelated-terminal-command",
        engine_client=engine_client,
        engine_kind="test_engine",
        engine_manifest=EngineCapabilityManifest(engine_kind="test_engine"),
        conversation_bound=True,
    )
    service = _service_with_acquisition(runtime)
    session = {"session_id": "session-1", "sandbox_id": "sandbox-1"}

    await service.interrupt_engine_turn(session)

    service._runtime_ensure.acquire_engine_control_runtime.assert_awaited_once_with(session)
    engine_client.interrupt_active_turn.assert_awaited_once_with()
    assert runtime.current_execution_id == "exec-of-an-unrelated-terminal-command"
    assert runtime.interrupting is True


@pytest.mark.asyncio
async def test_turn_interrupt_preserves_a_typed_acquisition_failure() -> None:
    service = TurnService.__new__(TurnService)
    service._runtime_ensure = SimpleNamespace(
        acquire_engine_control_runtime=AsyncMock(
            side_effect=APIError(
                code="SANDBOX_GONE",
                message="sandbox disappeared",
                status_code=409,
            )
        )
    )

    with pytest.raises(APIError) as caught:
        await service.interrupt_engine_turn(
            {"session_id": "session-1", "sandbox_id": "sandbox-gone"}
        )

    assert caught.value.code == "SANDBOX_GONE"
    assert caught.value.status_code == 409
