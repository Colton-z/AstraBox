"""A confirmed-dead sandbox is replaced, never reincarnated under its old id.

The replacement turn keeps the conversation's durable Claude session handle,
but the sandbox is a different lifecycle object.  These tests exercise the
runtime-ensure boundary where those two identities deliberately diverge.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from astrabox.core.service.orchestrator.engine.base import EngineCapabilityManifest
from astrabox.core.service.orchestrator.runtime.models import SessionRuntime
from astrabox.core.service.orchestrator.runtime_ensure import RuntimeEnsure
from astrabox.core.service.orchestrator.runtime_manager import RemoteAgentRuntimeManager
from astrabox.core.service.orchestrator.session_workspace_plan import RuntimeWorkspacePlan
from astrabox.core.service.orchestrator.stream_errors import (
    RUNTIME_ENSURE_ATTACHED,
    RUNTIME_ENSURE_ATTACH_FAILED,
)


class _EngineClient:
    is_live = True

    def __init__(self) -> None:
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


class _SessionsRepo:
    def __init__(self) -> None:
        self.updates: list[tuple[str, dict[str, Any]]] = []

    async def update_session(
        self,
        session_id: str,
        updates: dict[str, Any],
        **_kwargs: Any,
    ) -> None:
        self.updates.append((session_id, dict(updates)))


def _start_plan(session_id: str, engine_session_key: str) -> RuntimeWorkspacePlan:
    return RuntimeWorkspacePlan(
        subject_kind="deployment_conversation",
        session_kind="agent_chat",
        operation="runtime_start",
        runtime_key=session_id,
        conversation_session_id=session_id,
        cwd=f"/home/agents/agent-1/conversations/{session_id}",
        resume_engine_session_key=engine_session_key,
        sandbox_id=None,
        materialize_default_repo=False,
        default_repo_target_cwd=None,
        engine_kind="claude_code",
        agent_id="agent-1",
    )


def _runtime(
    session_id: str,
    sandbox_id: str,
    engine_session_key: str,
) -> tuple[SessionRuntime, _EngineClient]:
    engine_client = _EngineClient()
    runtime = SessionRuntime(
        session_id=session_id,
        agent=None,
        engine_kind="claude_code",
        sandbox_id=sandbox_id,
        engine_session_key=engine_session_key,
        terminal_cwd=f"/home/agents/agent-1/conversations/{session_id}",
        engine_client=engine_client,
        engine_manifest=EngineCapabilityManifest(engine_kind="claude_code"),
        conversation_bound=True,
        runtime_identity={
            "sandbox_id": sandbox_id,
            "linux_user": "agent",
            "home_dir": f"/home/agents/agent-1/conversations/{session_id}",
            "workspace_dir": f"/home/agents/agent-1/conversations/{session_id}/workspace",
            "sandbox_tenancy": "conversation",
        },
    )
    return runtime, engine_client


def _ensure(
    manager: RemoteAgentRuntimeManager,
    sessions_repo: _SessionsRepo,
    template: Any,
) -> RuntimeEnsure:
    lifecycle = SimpleNamespace(
        converge_dead_sandbox_owners=AsyncMock(),
        project_session_runtime_ready=AsyncMock(),
    )
    return RuntimeEnsure(
        runtime_manager=manager,
        sessions_repo=sessions_repo,  # type: ignore[arg-type]
        agent_repo=SimpleNamespace(),  # type: ignore[arg-type]
        agent_config=SimpleNamespace(
            resolve_session_harness=AsyncMock(return_value=template)
        ),
        permission_lifecycle=SimpleNamespace(
            ensure_before_turn_dispatch=AsyncMock()
        ),
        sandbox_lifecycle_service=lifecycle,
        has_turn_dispatch_permission_context=lambda **_kwargs: False,
    )


@pytest.mark.asyncio
async def test_empty_durable_binding_evicts_stale_resident_before_create() -> None:
    session_id = "session-cross-process-clear"
    engine_session_key = "claude-thread-1"
    stale_runtime, stale_client = _runtime(
        session_id, "sandbox-stale", engine_session_key
    )
    replacement, _replacement_client = _runtime(
        session_id, "sandbox-replacement", engine_session_key
    )
    manager = RemoteAgentRuntimeManager()
    manager._runtimes[session_id] = stale_runtime
    manager._start_runtime = AsyncMock(return_value=replacement)  # type: ignore[method-assign]
    manager.get_sandbox_expires_at = AsyncMock(  # type: ignore[method-assign]
        return_value=datetime(2099, 1, 1, tzinfo=timezone.utc)
    )
    manager.plan_agent_chat_runtime_start = Mock(  # type: ignore[method-assign]
        return_value=_start_plan(session_id, engine_session_key)
    )
    sessions_repo = _SessionsRepo()
    ensure = _ensure(manager, sessions_repo, SimpleNamespace(name="agent-template"))
    session = {
        "session_id": session_id,
        "session_kind": "agent_chat",
        "engine_kind": "claude_code",
        "agent_id": "agent-1",
        "sandbox_id": None,
        "engine_session_key": engine_session_key,
        "runtime_identity": stale_runtime.runtime_identity,
    }

    assert manager.get_runtime(session_id) is stale_runtime
    assert manager.get_runtime(session_id, sandbox_id=None) is None

    result = await ensure._prepare_agent_chat_turn_runtime(
        session,
        command_id="command-cross-process-clear",
    )

    assert result.status == RUNTIME_ENSURE_ATTACHED
    assert result.runtime is replacement
    assert manager._runtimes[session_id] is replacement
    assert stale_client.closed == 1
    assert (
        manager._start_runtime.await_args.kwargs["assignment_id"]  # type: ignore[attr-defined]
        == "command-cross-process-clear"
    )
    assert session["sandbox_id"] == "sandbox-replacement"
    assert sessions_repo.updates[-1][1]["sandbox_id"] == "sandbox-replacement"


@pytest.mark.asyncio
async def test_empty_durable_binding_requires_the_turn_assignment_before_create() -> None:
    session_id = "session-missing-assignment"
    engine_session_key = "claude-thread-missing-assignment"
    runtime, _client = _runtime(session_id, "sandbox-unused", engine_session_key)
    manager = RemoteAgentRuntimeManager()
    manager._start_runtime = AsyncMock(return_value=runtime)  # type: ignore[method-assign]
    manager.plan_agent_chat_runtime_start = Mock(  # type: ignore[method-assign]
        return_value=_start_plan(session_id, engine_session_key)
    )
    sessions_repo = _SessionsRepo()
    ensure = _ensure(manager, sessions_repo, SimpleNamespace(name="agent-template"))
    session = {
        "session_id": session_id,
        "session_kind": "agent_chat",
        "engine_kind": "claude_code",
        "agent_id": "agent-1",
        "sandbox_id": None,
        "engine_session_key": engine_session_key,
        "runtime_identity": runtime.runtime_identity,
    }

    result = await ensure._prepare_agent_chat_turn_runtime(session)

    assert result.status == RUNTIME_ENSURE_ATTACH_FAILED
    assert result.sandbox_gone is True
    assert "requires a durable assignment_id" in str(result.error_text)
    manager._start_runtime.assert_not_awaited()  # type: ignore[attr-defined]
    assert sessions_repo.updates[-1][1]["runtime_unavailable"] is True


@pytest.mark.asyncio
async def test_confirmed_dead_sandbox_replacement_mints_a_new_id_and_resumes_thread() -> None:
    session_id = "session-1"
    engine_session_key = "claude-thread-1"
    old_runtime, old_client = _runtime(
        session_id, "sandbox-reclaimed", engine_session_key
    )
    new_runtime, _new_client = _runtime(
        session_id, "sandbox-replacement", engine_session_key
    )
    manager = RemoteAgentRuntimeManager()
    manager._runtimes[session_id] = old_runtime
    manager._start_runtime = AsyncMock(return_value=new_runtime)  # type: ignore[method-assign]
    manager.get_sandbox_expires_at = AsyncMock(  # type: ignore[method-assign]
        return_value=datetime(2099, 1, 1, tzinfo=timezone.utc)
    )
    start_plan = _start_plan(session_id, engine_session_key)
    plan_runtime_start = Mock(return_value=start_plan)
    manager.plan_agent_chat_runtime_start = plan_runtime_start  # type: ignore[method-assign]
    sessions_repo = _SessionsRepo()
    template = SimpleNamespace(name="agent-template")
    ensure = _ensure(manager, sessions_repo, template)
    session = {
        "session_id": session_id,
        "session_kind": "agent_chat",
        "engine_kind": "claude_code",
        "sandbox_id": old_runtime.sandbox_id,
        "engine_session_key": engine_session_key,
        "terminal_cwd": old_runtime.terminal_cwd,
    }

    result = await ensure._reborrow_agent_chat_runtime_for_turn(
        session=session,
        agent_id="agent-1",
        engine_session_key=engine_session_key,
        runtime_identity=old_runtime.runtime_identity,
        turn_id="turn-1",
        command_id="command-1",
        requested_permission_mode=None,
    )

    assert result.status == RUNTIME_ENSURE_ATTACHED
    assert result.runtime is new_runtime
    assert manager._runtimes[session_id] is new_runtime
    assert old_client.closed == 1, "the reclaimed box's resident handle must be retired"
    assert session["sandbox_id"] == "sandbox-replacement"
    assert session["sandbox_id"] != old_runtime.sandbox_id
    assert session["engine_session_key"] == engine_session_key
    plan_runtime_start.assert_called_once_with(
        session_id=session_id,
        agent_id="agent-1",
        template=template,
        resume_engine_session_key=engine_session_key,
        existing_terminal_cwd=old_runtime.terminal_cwd,
        # Exact by construction, so a new keyword is a failure here whether or
        # not its value is interesting. Naming it keeps that exactness; the
        # alternative would stop this noticing the next argument.
        runtime_identity=None,
    )
    manager._start_runtime.assert_awaited_once()  # type: ignore[attr-defined]
    assert (
        manager._start_runtime.await_args.kwargs["workspace_plan"]  # type: ignore[attr-defined]
        is start_plan
    )
    assert (
        manager._start_runtime.await_args.kwargs["assignment_id"]  # type: ignore[attr-defined]
        == "command-1"
    )
    assert sessions_repo.updates[-1][1]["engine_session_key"] == engine_session_key
    ensure._sandbox_lifecycle_service.project_session_runtime_ready.assert_awaited_once_with(
        session,
        reason="agent_chat_reborrow_ready:sandbox-replacement",
    )
    assert session["runtime_unavailable"] is False
    assert session["last_error"] is None


@pytest.mark.asyncio
async def test_replacement_refuses_to_publish_a_reused_sandbox_id() -> None:
    session_id = "session-1"
    engine_session_key = "claude-thread-1"
    old_runtime, old_client = _runtime(
        session_id, "sandbox-reclaimed", engine_session_key
    )
    reused_runtime, reused_client = _runtime(
        session_id, "sandbox-reclaimed", engine_session_key
    )
    manager = RemoteAgentRuntimeManager()
    manager._runtimes[session_id] = old_runtime
    manager._start_runtime = AsyncMock(return_value=reused_runtime)  # type: ignore[method-assign]
    manager.plan_agent_chat_runtime_start = Mock(  # type: ignore[method-assign]
        return_value=_start_plan(session_id, engine_session_key)
    )
    sessions_repo = _SessionsRepo()
    ensure = _ensure(manager, sessions_repo, SimpleNamespace(name="agent-template"))
    session = {
        "session_id": session_id,
        "session_kind": "agent_chat",
        "engine_kind": "claude_code",
        "sandbox_id": old_runtime.sandbox_id,
        "engine_session_key": engine_session_key,
    }

    result = await ensure._reborrow_agent_chat_runtime_for_turn(
        session=session,
        agent_id="agent-1",
        engine_session_key=engine_session_key,
        runtime_identity=old_runtime.runtime_identity,
        turn_id="turn-1",
        command_id="command-1",
        requested_permission_mode=None,
    )

    assert result.status == RUNTIME_ENSURE_ATTACH_FAILED
    assert result.sandbox_gone is True
    assert session["sandbox_id"] == "sandbox-reclaimed"
    assert session["runtime_unavailable"] is True
    assert "must differ" in str(result.error_text)
    assert session_id not in manager._runtimes
    assert old_client.closed == 1
    assert reused_client.closed == 1
    assert all("sandbox_id" not in update for _session_id, update in sessions_repo.updates)


@pytest.mark.asyncio
async def test_replacement_asks_for_the_owner_the_conversation_already_has() -> None:
    """A rebuilt box gets a new sandbox id and the same POSIX owner.

    The account exists on the box from the first placement and the bootstrap
    validates what it finds against the uid it is given, so a fresh number is
    not a new conversation's answer to the same question — it is a failure.
    """
    session_id = "session-owner-carried"
    engine_session_key = "claude-thread-owner"
    old_runtime, _old_client = _runtime(session_id, "sandbox-dead", engine_session_key)
    new_runtime, _new_client = _runtime(
        session_id, "sandbox-replacement", engine_session_key
    )
    manager = RemoteAgentRuntimeManager()
    manager._runtimes[session_id] = old_runtime
    manager._start_runtime = AsyncMock(return_value=new_runtime)  # type: ignore[method-assign]
    manager.get_sandbox_expires_at = AsyncMock(  # type: ignore[method-assign]
        return_value=datetime(2099, 1, 1, tzinfo=timezone.utc)
    )
    plan_runtime_start = Mock(return_value=_start_plan(session_id, engine_session_key))
    manager.plan_agent_chat_runtime_start = plan_runtime_start  # type: ignore[method-assign]
    sessions_repo = _SessionsRepo()
    ensure = _ensure(manager, sessions_repo, SimpleNamespace(name="agent-template"))
    recorded = {
        **old_runtime.runtime_identity,
        "uid": 2007,
        "gid": 2007,
        "linux_user": "conv_owner",
    }
    session = {
        "session_id": session_id,
        "session_kind": "agent_chat",
        "engine_kind": "claude_code",
        "agent_id": "agent-1",
        "sandbox_id": None,
        "engine_session_key": engine_session_key,
        "runtime_identity": recorded,
    }

    await ensure._prepare_agent_chat_turn_runtime(
        session,
        command_id="command-owner-carried",
    )

    carried = plan_runtime_start.call_args.kwargs["runtime_identity"]
    assert carried is not None
    assert carried["uid"] == 2007
    assert carried["gid"] == 2007


@pytest.mark.asyncio
async def test_a_rebuild_invents_no_owner_the_conversation_never_had() -> None:
    """The control: same rebuild, an identity that carries no owner.

    Without it the assertion above would also pass against a plan handed some
    unconditional default. Varying only what the recorded identity carries, on
    the branch that plans a start, is what makes this a control — a session with
    no durable binding at all takes the attach path and plans nothing.
    """
    session_id = "session-owner-absent"
    engine_session_key = "claude-thread-absent"
    old_runtime, _old_client = _runtime(session_id, "sandbox-dead", engine_session_key)
    new_runtime, _new_client = _runtime(
        session_id, "sandbox-replacement", engine_session_key
    )
    manager = RemoteAgentRuntimeManager()
    manager._runtimes[session_id] = old_runtime
    manager._start_runtime = AsyncMock(return_value=new_runtime)  # type: ignore[method-assign]
    manager.get_sandbox_expires_at = AsyncMock(  # type: ignore[method-assign]
        return_value=datetime(2099, 1, 1, tzinfo=timezone.utc)
    )
    plan_runtime_start = Mock(return_value=_start_plan(session_id, engine_session_key))
    manager.plan_agent_chat_runtime_start = plan_runtime_start  # type: ignore[method-assign]
    sessions_repo = _SessionsRepo()
    ensure = _ensure(manager, sessions_repo, SimpleNamespace(name="agent-template"))
    ownerless = dict(old_runtime.runtime_identity)
    assert "uid" not in ownerless
    session = {
        "session_id": session_id,
        "session_kind": "agent_chat",
        "engine_kind": "claude_code",
        "agent_id": "agent-1",
        "sandbox_id": None,
        "engine_session_key": engine_session_key,
        "runtime_identity": ownerless,
    }

    await ensure._prepare_agent_chat_turn_runtime(
        session,
        command_id="command-owner-absent",
    )

    carried = plan_runtime_start.call_args.kwargs["runtime_identity"]
    assert carried is not None
    assert "uid" not in carried
    assert "gid" not in carried
