from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.engine.base import EngineCapabilityManifest
from astrabox.core.service.orchestrator.engine.emissions import (
    ChildResourceFact,
    PrivateDiagnostic,
)
from astrabox.core.service.orchestrator.engine.frame_scope import (
    session_scoped_engine_frame,
)
from astrabox.core.service.orchestrator.runtime_ensure import RuntimeEnsure
from astrabox.core.service.orchestrator.session_kernel.workers.turn.worker import (
    TurnWorker,
)
from astrabox.core.service.orchestrator.turn_service import TurnService


def _bound_runtime() -> SimpleNamespace:
    return SimpleNamespace(
        engine_client=object(),
        engine_kind="test_engine",
        engine_manifest=EngineCapabilityManifest(engine_kind="test_engine"),
        conversation_bound=True,
    )


def _controllable_runtime() -> SimpleNamespace:
    client = SimpleNamespace(
        stop_child_run=AsyncMock(),
        interrupt_active_turn=AsyncMock(return_value=True),
        set_permission_mode=AsyncMock(),
    )
    return SimpleNamespace(
        engine_client=client,
        engine_kind="test_engine",
        engine_manifest=EngineCapabilityManifest(
            engine_kind="test_engine",
            permission_modes=["strict", "danger"],
            supports_child_run_control=True,
        ),
        conversation_bound=True,
        interrupting=False,
        permission_mode=None,
        permission_mode_verified=False,
    )


@pytest.mark.asyncio
async def test_control_acquisition_reattaches_when_the_process_cache_is_empty() -> None:
    runtime = _bound_runtime()
    manager = SimpleNamespace(
        get_runtime=Mock(return_value=None),
        ensure_runtime_lightweight=AsyncMock(return_value=runtime),
    )
    ensure = RuntimeEnsure.__new__(RuntimeEnsure)
    ensure._runtime_manager = manager
    ensure._agent_config = SimpleNamespace(
        resolve_session_harness=AsyncMock(return_value=SimpleNamespace())
    )
    ensure._plan_runtime_attach_for_turn = Mock(return_value="adapter-plan")
    session = {
        "session_id": "session-1",
        "session_kind": "assistant_chat",
        "sandbox_id": "sandbox-1",
        "engine_session_key": "native-session-1",
        "permission_mode": "native-mode",
    }

    acquired = await ensure.acquire_engine_control_runtime(session)

    assert acquired is runtime
    manager.ensure_runtime_lightweight.assert_awaited_once_with(
        "session-1",
        ensure._agent_config.resolve_session_harness.return_value,
        sandbox_id="sandbox-1",
        engine_session_key="native-session-1",
        permission_mode="native-mode",
        session_kind="assistant_chat",
        workspace_plan="adapter-plan",
        runtime_identity=None,
    )


def _ensure_whose_attach_raises(exc: Exception) -> RuntimeEnsure:
    ensure = RuntimeEnsure.__new__(RuntimeEnsure)
    ensure._runtime_manager = SimpleNamespace(
        get_runtime=Mock(return_value=None),
        ensure_runtime_lightweight=AsyncMock(side_effect=exc),
    )
    ensure._agent_config = SimpleNamespace(
        resolve_session_harness=AsyncMock(return_value=SimpleNamespace())
    )
    ensure._plan_runtime_attach_for_turn = Mock(return_value="adapter-plan")
    ensure._sessions_repo = SimpleNamespace(update_session=AsyncMock())
    ensure._sandbox_lifecycle_service = SimpleNamespace(
        converge_dead_sandbox_owners=AsyncMock()
    )
    return ensure


def _gone_session() -> dict:
    return {
        "session_id": "session-1",
        "session_kind": "assistant_chat",
        "sandbox_id": "sandbox-gone",
    }


@pytest.mark.asyncio
async def test_control_acquisition_preserves_a_typed_adapter_attach_failure() -> None:
    ensure = _ensure_whose_attach_raises(
        APIError(code="SANDBOX_GONE", message="sandbox disappeared", status_code=409)
    )

    with pytest.raises(APIError) as exc_info:
        await ensure.acquire_engine_control_runtime(_gone_session())

    assert exc_info.value.code == "SANDBOX_GONE"


@pytest.mark.asyncio
async def test_a_confirmed_dead_sandbox_is_recorded_before_the_attach_error_is_raised() -> None:
    """A box that did not answer must not be a fact only the log remembers.

    Nothing downstream re-derives it: the turn coordinator settles an
    unresolved turn on the session's durable ``runtime_unavailable`` flag or on
    a terminal lifecycle probe, and a pooled backend keeps reporting Ready for
    a sandbox whose Pod was deleted out-of-band. If the attach drops the
    verdict, that turn stays unresolved and every later input is answered
    SESSION_BUSY.
    """

    ensure = _ensure_whose_attach_raises(
        APIError(code="SANDBOX_GONE", message="sandbox disappeared", status_code=409)
    )
    session = _gone_session()

    with pytest.raises(APIError):
        await ensure.acquire_engine_control_runtime(session)

    ensure._sessions_repo.update_session.assert_awaited_once()
    recorded_session_id, written = ensure._sessions_repo.update_session.await_args.args
    assert recorded_session_id == "session-1"
    assert written["runtime_unavailable"] is True
    # The lapsed-lease gate reads expires_at straight off the row, and a
    # still-READY runtime_binding masks the flag from it.
    assert written["expires_at"]
    assert session["runtime_unavailable"] is True

    # Shared tenancy: the box that died was carrying other conversations, and
    # only the sandbox id connects them.
    ensure._sandbox_lifecycle_service.converge_dead_sandbox_owners.assert_awaited_once()
    converge = ensure._sandbox_lifecycle_service.converge_dead_sandbox_owners.await_args
    assert converge.args[0] == "sandbox-gone"


@pytest.mark.asyncio
async def test_an_attach_failure_that_is_not_sandbox_gone_leaves_the_session_alone() -> None:
    """Only a box that did not answer at all is evidence the box is gone.

    A transient attach failure marked unavailable here would settle a live
    turn against a box that is still serving it.
    """

    ensure = _ensure_whose_attach_raises(
        APIError(
            code="ENGINE_RUNTIME_UNAVAILABLE",
            message="engine control is momentarily busy",
            status_code=409,
        )
    )
    session = _gone_session()

    with pytest.raises(APIError):
        await ensure.acquire_engine_control_runtime(session)

    ensure._sessions_repo.update_session.assert_not_awaited()
    ensure._sandbox_lifecycle_service.converge_dead_sandbox_owners.assert_not_awaited()
    assert "runtime_unavailable" not in session


@pytest.mark.asyncio
async def test_the_recovery_probe_returns_none_but_the_dead_box_is_already_durable() -> None:
    """The optional probe is the only caller that reaches a wedged turn.

    It swallows the runtime by contract; what it must not swallow is the
    verdict, which the attach beneath it has already written down.
    """

    ensure = _ensure_whose_attach_raises(
        APIError(code="SANDBOX_GONE", message="sandbox disappeared", status_code=409)
    )

    assert await ensure._ensure_runtime_lightweight_for_session(_gone_session()) is None

    ensure._sessions_repo.update_session.assert_awaited_once()
    ensure._sandbox_lifecycle_service.converge_dead_sandbox_owners.assert_awaited_once()


@pytest.mark.asyncio
async def test_turn_service_rejects_an_unbound_control_runtime() -> None:
    service = TurnService.__new__(TurnService)
    service._runtime_ensure = SimpleNamespace(
        acquire_engine_control_runtime=AsyncMock(
            return_value=SimpleNamespace(
                engine_client=object(),
                engine_kind="test_engine",
                engine_manifest=EngineCapabilityManifest(engine_kind="test_engine"),
                conversation_bound=False,
            )
        )
    )

    with pytest.raises(APIError) as exc_info:
        await service.acquire_engine_control_runtime(
            {"session_id": "session-1"},
            operation="stop child run",
        )

    assert exc_info.value.code == "ENGINE_CAPABILITY_CONTRACT_VIOLATION"


@pytest.mark.asyncio
async def test_engine_controls_execute_on_the_one_acquired_runtime() -> None:
    runtime = _controllable_runtime()
    service = TurnService.__new__(TurnService)
    service._runtime_ensure = SimpleNamespace(
        acquire_engine_control_runtime=AsyncMock(return_value=runtime)
    )
    session = {"session_id": "session-1", "sandbox_id": "sandbox-1"}

    await service.stop_engine_child_run(session, "private-control")
    await service.interrupt_engine_turn(session)
    applied = await service.set_engine_permission_mode(session, "danger")

    assert applied is True
    assert service._runtime_ensure.acquire_engine_control_runtime.await_count == 3
    runtime.engine_client.stop_child_run.assert_awaited_once_with("private-control")
    runtime.engine_client.interrupt_active_turn.assert_awaited_once_with()
    runtime.engine_client.set_permission_mode.assert_awaited_once_with("danger")
    assert runtime.interrupting is True
    assert runtime.permission_mode == "danger"
    assert runtime.permission_mode_verified is True


@pytest.mark.asyncio
async def test_child_reconcile_persists_adapter_facts_as_session_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fact = ChildResourceFact(
        session_scoped_engine_frame(
            {
                "type": "data-subagent",
                "id": "child-snapshot",
                "data": {
                    "kind": "lifecycle",
                    "engineKind": "test_engine",
                    "engineRef": "private-child",
                    "event": "opened",
                    "engineEvent": "catalog",
                    "operations": [],
                },
            }
        )
    )
    diagnostic = PrivateDiagnostic(
        {
            "type": "data-raw-event",
            "data": {
                "event_type": "test_engine.sdk",
                "subtype": "catalog.unavailable",
                "raw": {"private": "native-address"},
            },
        },
        event_type="test_engine.sdk",
        subtype="catalog.unavailable",
        raw={"private": "native-address"},
    )

    class _ReconcilingClient:
        async def reconcile_child_resources(
            self,
        ) -> list[ChildResourceFact | PrivateDiagnostic]:
            return [fact, diagnostic]

    runtime = SimpleNamespace(
        engine_client=_ReconcilingClient(),
        engine_kind="test_engine",
        engine_manifest=EngineCapabilityManifest(engine_kind="test_engine"),
        conversation_bound=True,
    )
    repo = SimpleNamespace(
        allocate_session_frame_seq=AsyncMock(return_value=41),
        append_frames=AsyncMock(),
        append_event=AsyncMock(),
    )
    service = TurnService.__new__(TurnService)
    service._runtime_ensure = SimpleNamespace(
        acquire_engine_control_runtime=AsyncMock(return_value=runtime)
    )
    service._session_events_repo = repo
    monkeypatch.setattr(
        "astrabox.core.service.orchestrator.turn_service.get_engine_adapter",
        lambda _kind: SimpleNamespace(engine_client_type=_ReconcilingClient),
    )

    reconciled = await service.reconcile_engine_child_resources(
        {
            "session_id": "session-1",
            "session_kind": "agent_chat",
            "engine_kind": "test_engine",
            "sandbox_id": "sandbox-1",
            "state": "READY",
        }
    )

    assert reconciled is True
    repo.append_frames.assert_awaited_once()
    [docs] = repo.append_frames.await_args.args
    assert isinstance(docs[0].pop("created_at"), str)
    assert docs == [
        {
            "session_id": "session-1",
            "turn_id": None,
            "scope": "session",
            "command_id": None,
            "source_kind": "engine_child_reconcile",
            "frame_seq": 41,
            "payload": {
                "type": "data-subagent",
                "id": "child-snapshot",
                "data": {
                    "kind": "lifecycle",
                    "engineKind": "test_engine",
                    "engineRef": "private-child",
                    "event": "opened",
                    "engineEvent": "catalog",
                    "operations": [],
                },
                "transient": True,
            },
            "engine_kind": "test_engine",
        }
    ]
    repo.append_event.assert_awaited_once()
    [diagnostic_doc] = repo.append_event.await_args.args
    assert diagnostic_doc["event_type"] == "engine.diagnostic"
    assert diagnostic_doc["payload"] == {
        "engine_kind": "test_engine",
        "event_type": "test_engine.sdk",
        "subtype": "catalog.unavailable",
        "raw": {"private": "native-address"},
    }


@pytest.mark.asyncio
async def test_child_reconcile_does_not_attach_an_engine_without_the_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _OrdinaryClient:
        pass

    service = TurnService.__new__(TurnService)
    service._runtime_ensure = SimpleNamespace(
        acquire_engine_control_runtime=AsyncMock()
    )
    monkeypatch.setattr(
        "astrabox.core.service.orchestrator.turn_service.get_engine_adapter",
        lambda _kind: SimpleNamespace(engine_client_type=_OrdinaryClient),
    )

    reconciled = await service.reconcile_engine_child_resources(
        {
            "session_id": "session-1",
            "session_kind": "agent_chat",
            "engine_kind": "ordinary",
            "sandbox_id": "sandbox-1",
            "state": "READY",
        }
    )

    assert reconciled is False
    service._runtime_ensure.acquire_engine_control_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_interrupt_acquires_the_engine_control_attachment_before_calling_it() -> None:
    order: list[str] = []

    async def interrupt(_session: dict) -> None:
        order.append("acquire")
        order.append("interrupt")

    worker = TurnWorker.__new__(TurnWorker)
    worker._session_snapshots_repo = SimpleNamespace(
        force_update_fields=AsyncMock(return_value=True),
        get_snapshot=AsyncMock(return_value={"current_turn_id": "turn-1"}),
    )
    worker._sessions_repo = SimpleNamespace(
        get_session=AsyncMock(
            return_value={
                "session_id": "session-1",
                "session_kind": "assistant_chat",
                "sandbox_id": "sandbox-1",
            }
        )
    )
    worker._turn_service = SimpleNamespace(
        interrupt_engine_turn=AsyncMock(side_effect=interrupt)
    )
    worker._session_events_repo = SimpleNamespace(
        append_event=AsyncMock(return_value={"event_seq": 17})
    )
    worker._settle_pre_first_token_interrupt = AsyncMock(return_value=None)

    _, result = await worker._run_interrupt_command(
        user=UserContext("user-1"),
        session_id="session-1",
        command_event={
            "event_seq": 12,
            "turn_id": "turn-1",
            "causation_id": "interrupt-1",
        },
    )

    assert order == ["acquire", "interrupt"]
    assert result == {"session_id": "session-1", "status": "accepted"}
