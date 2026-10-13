from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.core.service.orchestrator.engine_turn import (
    iter_engine_client_events,
)
from astrabox.core.service.orchestrator.engine.base import EngineInputCommand
from astrabox.core.service.orchestrator.turn_service import TurnService
from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    build_turn_active_snapshot_updates,
)
from astrabox.seams import runtime_preparation as preparation_seam
from astrabox.seams import sandbox as sandbox_seam
from astrabox.seams.runtime_preparation import (
    PreparationUserActionRequired,
    RuntimeMaterials,
    RuntimePreparationProvider,
    TurnPreparationContext,
    register_runtime_preparation_provider,
)
from astrabox.seams.sandbox import register_sandbox


class _Backend:
    """The profile-exclusive backend the assistant session is bound to."""

    sandbox_is_profile_exclusive = True

    def __init__(self, name: str) -> None:
        self.name = name

    def owns_sandbox(self, sandbox: Any) -> bool:
        return getattr(sandbox, "sandbox_id", None) == "assistant-sandbox"


class _PreparingProvider(RuntimePreparationProvider):
    def __init__(
        self,
        name: str,
        trace: list[str],
        *,
        error: Exception | None = None,
    ) -> None:
        self.name = name
        self.trace = trace
        self.error = error
        self.contexts: list[TurnPreparationContext] = []
        self.runtime_lock: asyncio.Lock | None = None
        self.lock_states: list[bool] = []

    async def prepare_turn(self, *, context: TurnPreparationContext) -> RuntimeMaterials:
        self.contexts.append(context)
        if self.runtime_lock is not None:
            self.lock_states.append(self.runtime_lock.locked())
        self.trace.append("prepare")
        if self.error is not None:
            raise self.error
        return RuntimeMaterials()


class _RecordingEngineClient:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.begin_delivery_calls = 0

    @property
    def engine_session_key(self) -> str | None:
        return "assistant-native-session"

    async def begin_delivery(
        self,
        command: EngineInputCommand,
        *,
        consumption_confirmed: bool = False,
    ) -> Any:
        assert command.content == "hello assistant"
        assert not consumption_confirmed
        self.begin_delivery_calls += 1
        self.trace.append("begin_delivery")
        raise RuntimeError(f"stop after write: {command.content}")


class _SessionsRepo:
    def __init__(self) -> None:
        self.guard: dict[str, Any] | None = None

    async def get_session(self, session_id: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "user_id": "persisted-owner",
            "sandbox_id": "assistant-sandbox",
            "sandbox_backend": "assistant-preparation-order",
        }

    async def claim_turn_preparation_guard(
        self,
        session_id: str,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        if self.guard is not None:
            return None
        self.guard = {
            "state": "ACTIVE",
            "sandbox_id": kwargs["sandbox_id"],
            "attempt_id": kwargs["attempt_id"],
            "owner_token": kwargs["owner_token"],
        }
        return dict(self.guard)

    async def release_turn_preparation_guard(
        self,
        session_id: str,
        **kwargs: Any,
    ) -> bool:
        if (
            self.guard is None
            or self.guard.get("attempt_id") != kwargs["attempt_id"]
            or self.guard.get("owner_token") != kwargs["owner_token"]
            or self.guard.get("state") != kwargs["expected_state"]
        ):
            return False
        self.guard = None
        return True

    async def quarantine_turn_preparation_guard(
        self,
        session_id: str,
        **kwargs: Any,
    ) -> bool:
        if self.guard is None:
            return False
        self.guard["state"] = "QUARANTINED"
        return True


class _SnapshotsRepo:
    """The session's snapshot after the worker admitted ``assistant-turn``."""

    async def get_snapshot(self, session_id: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            **build_turn_active_snapshot_updates(
                conversation_state="PROCESSING",
                turn_id="assistant-turn",
            ),
        }


@pytest.fixture(autouse=True)
def _isolated_sandbox_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox_seam, "_BACKENDS", dict(sandbox_seam._BACKENDS))
    monkeypatch.setattr(preparation_seam, "_PROVIDER", None)


async def _drive(
    provider: _PreparingProvider,
) -> tuple[list[dict[str, Any]], Any]:
    register_sandbox(_Backend(provider.name))  # type: ignore[arg-type]
    register_runtime_preparation_provider(provider)
    engine_client = _RecordingEngineClient(provider.trace)
    runtime_lock = asyncio.Lock()
    provider.runtime_lock = runtime_lock
    runtime = SimpleNamespace(
        lock=runtime_lock,
        current_task=None,
        engine_client=engine_client,
        engine_kind="assistant",
        sandbox_id="assistant-sandbox",
        sandbox=SimpleNamespace(sandbox_id="assistant-sandbox"),
        dataplane=object(),
        interrupting=False,
        conversation_bound=True,
    )
    session = {
        "session_id": "assistant-session",
        "user_id": "persisted-owner",
        "sandbox_id": "assistant-sandbox",
        "sandbox_backend": provider.name,
    }

    class _ProviderSessionsRepo(_SessionsRepo):
        async def get_session(self, session_id: str) -> dict[str, Any]:
            row = await super().get_session(session_id)
            row["sandbox_backend"] = provider.name
            return row

    sessions_repo = _ProviderSessionsRepo()
    turn_service = object.__new__(TurnService)
    turn_service._input_delivery_locks = {}
    turn_service._sessions_repo = sessions_repo  # type: ignore[assignment]
    turn_service._session_snapshots_repo = _SnapshotsRepo()
    events = [
        event
        async for event in iter_engine_client_events(
            session=session,
            session_id="assistant-session",
            effective_content="hello assistant",
            turn_id="assistant-turn",
            runtime=runtime,
            interaction_permission_mode=None,
            on_query_committed=None,
            emit_timing=lambda *args, **kwargs: None,
            client_message_id=None,
            sessions_repo=sessions_repo,  # type: ignore[arg-type]
            prepare_engine_write=turn_service.prepared_engine_write,
            broker=object(),  # type: ignore[arg-type]
            delivery_command={
                "command_id": "assistant-command",
                "session_id": "assistant-session",
                "sequence": 1,
                "input_id": "assistant-input",
                "content": "hello assistant",
                "client_message_id": None,
                "consumption_confirmed": False,
            },
        )
    ]
    return events, engine_client


async def test_assistant_prepares_inside_runtime_lock_before_input_delivery() -> None:
    trace: list[str] = []
    provider = _PreparingProvider("assistant-preparation-order", trace)

    events, engine_client = await _drive(provider)

    assert trace == ["prepare", "begin_delivery"]
    assert provider.lock_states == [True]
    assert engine_client.begin_delivery_calls == 1
    assert events[0]["code"] == "ENGINE_INPUT_DELIVERY_FAILED"
    context = provider.contexts[0]
    assert context.principal_id == "persisted-owner"
    assert context.engine_kind == "assistant"
    assert context.dispatch_attempt_id
    assert not hasattr(context, "dataplane")


async def test_assistant_preparation_failure_prevents_engine_write() -> None:
    trace: list[str] = []
    provider = _PreparingProvider(
        "assistant-preparation-failure",
        trace,
        error=RuntimeError("provider-only secret"),
    )

    events, engine_client = await _drive(provider)

    assert trace == ["prepare"]
    assert engine_client.begin_delivery_calls == 0
    assert events[0] == {
        "type": "error",
        "turn_id": "assistant-turn",
        "code": "TURN_PREPARATION_FAILED",
        "message": "sandbox turn preparation failed",
    }
    assert events[1]["state"] == "READY"


async def test_a_user_action_the_provider_asks_for_reaches_the_turn_stream() -> None:
    # The provider needs the user to authorize before this turn can run: the
    # stream must say so under its own code and message, not as a generic
    # delivery failure.
    trace: list[str] = []
    provider = _PreparingProvider(
        "assistant-preparation-user-action",
        trace,
        error=PreparationUserActionRequired(
            message="Authorize the agent to act for you",
            url="https://idp.example/confirm",
        ),
    )

    events, engine_client = await _drive(provider)

    assert trace == ["prepare"]
    assert engine_client.begin_delivery_calls == 0
    assert events[0] == {
        "type": "error",
        "turn_id": "assistant-turn",
        "code": "TURN_PREPARATION_USER_ACTION_REQUIRED",
        "message": "Authorize the agent to act for you",
        "refusal": {
            "code": "TURN_PREPARATION_USER_ACTION_REQUIRED",
            "message": "Authorize the agent to act for you",
            "user_message": "Authorize the agent to act for you",
            "status_code": 409,
            "data": None,
            "user_action": {
                "status": "user_action_required",
                "message": "Authorize the agent to act for you",
                "url": "https://idp.example/confirm",
            },
        },
    }
    assert events[1]["state"] == "READY"
