"""Runtime-manager dispatch for backend-owned storage and final cleanup."""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import astrabox.core.service.orchestrator.runtime_manager as runtime_manager_module
import astrabox.core.service.orchestrator.terminal_service as terminal_service_module
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.base import EngineCapabilityManifest
from astrabox.core.service.orchestrator.runtime_manager import (
    RemoteAgentRuntimeManager,
    SessionRuntime,
)
from astrabox.core.service.orchestrator.runtime.pty_terminal import (
    ResolvedExecdEndpoint,
)
from astrabox.core.service.orchestrator.terminal_service import TerminalService
from astrabox.common.utils.user_context import UserContext
from astrabox.persistence.models.session_snapshot import owned_fields_for_channel
from astrabox.core.service.orchestrator.session_kernel.workers.lifecycle.commands import (
    _LifecycleCommandsMixin,
)


def test_storage_mount_capability_helper_binds_only_the_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling the helper through a manager must not pass ``self`` as a box.

    Assistant startup reaches this after its Hermes sandbox exists. Binding the
    helper as an instance method passes the manager as an extra argument, which
    destroys the new sandbox and leaves the workspace hibernating.
    """
    sandbox = object()
    monkeypatch.setattr(
        runtime_manager_module,
        "get_underlying_sandbox",
        lambda candidate: candidate,
    )
    monkeypatch.setattr(
        runtime_manager_module,
        "sandbox_for_sandbox",
        lambda candidate: SimpleNamespace(
            uses_create_oss_mounts=candidate is sandbox
        ),
    )
    manager = RemoteAgentRuntimeManager.__new__(RemoteAgentRuntimeManager)

    assert manager._sandbox_uses_create_storage_mounts(sandbox) is True


def test_terminal_channel_owns_the_persistent_pty_anchor() -> None:
    assert "terminal_pty_session_id" in owned_fields_for_channel("terminal")
    assert "terminal_pty_session_id" not in owned_fields_for_channel("conversation")
    assert "terminal_pty_session_id" not in owned_fields_for_channel("lifecycle")


@pytest.mark.asyncio
async def test_final_conversation_disposal_terminates_the_engine_process() -> None:
    """Final disposal is stronger than the reconnect-safe runtime eviction.

    ``close`` only releases the platform client's connection. A final
    delete/archive/end must use an engine's optional ``dispose`` operation so
    the resident OpenSandbox PTY is terminated too.
    """

    log: list[str] = []

    class _EngineClient:
        async def close(self) -> None:
            log.append("close")

        async def dispose(self) -> None:
            log.append("dispose")

    manager = RemoteAgentRuntimeManager()
    runtime = SessionRuntime(
        session_id="session-1",
        agent=None,
        sandbox_id="sandbox-1",
        engine_kind="assistant",
        engine_client=_EngineClient(),
    )
    manager._runtimes["session-1"] = runtime

    await manager.dispose_runtime_session("session-1")

    assert log == ["dispose"]
    assert "session-1" not in manager._runtimes


@pytest.mark.asyncio
async def test_conversation_disposal_can_recover_the_last_process_anchor() -> None:
    """A backend restart must not make a finished Hermes PTY unidentifiable."""

    engine_turn_id = "hermes-tui:v1:durable-anchor"
    runtime_manager = SimpleNamespace(
        dispose_runtime_session=AsyncMock(),
        dispose_terminal_session=AsyncMock(),
    )
    worker = SimpleNamespace(
        _runtime_manager=runtime_manager,
        _session_snapshots_repo=SimpleNamespace(
            get_snapshot=AsyncMock(
                return_value={
                    "last_turn_id": "turn-1",
                    "terminal_pty_session_id": "terminal-pty-1",
                }
            )
        ),
        _session_events_repo=SimpleNamespace(
            list_events=AsyncMock(
                return_value=[
                    {
                        "payload": {
                            "recovery_context": {
                                "engine_anchor": {
                                    "engine_kind": "assistant",
                                    "engine_turn_id": engine_turn_id,
                                }
                            }
                        }
                    }
                ]
            )
        ),
    )

    await _LifecycleCommandsMixin._dispose_conversation_runtime(
        worker,
        "session-1",
        sandbox_id="sandbox-1",
    )

    runtime_manager.dispose_runtime_session.assert_awaited_once_with(
        "session-1",
        sandbox_id="sandbox-1",
        engine_kind="assistant",
        engine_turn_id=engine_turn_id,
    )
    runtime_manager.dispose_terminal_session.assert_awaited_once_with(
        "session-1",
        sandbox_id="sandbox-1",
        pty_session_id="terminal-pty-1",
    )


@pytest.mark.asyncio
async def test_conversation_disposal_reports_retryable_process_cleanup_failure() -> None:
    """A failed PTY cleanup must not be reported as a successful deletion."""

    runtime_manager = SimpleNamespace(
        dispose_runtime_session=AsyncMock(
            side_effect=RuntimeError("engine PTY delete failed")
        ),
        dispose_terminal_session=AsyncMock(),
    )
    worker = SimpleNamespace(
        _runtime_manager=runtime_manager,
        _session_snapshots_repo=SimpleNamespace(
            get_snapshot=AsyncMock(return_value={"terminal_pty_session_id": "pty-1"})
        ),
        _session_events_repo=SimpleNamespace(list_events=AsyncMock(return_value=[])),
    )
    terminal_service_module._pty_sessions["session-cleanup-failed"] = "pty-1"

    with pytest.raises(APIError) as caught:
        await _LifecycleCommandsMixin._dispose_conversation_runtime(
            worker,
            "session-cleanup-failed",
            sandbox_id="sandbox-1",
        )

    assert caught.value.to_error_envelope() == {
        "code": "SESSION_PROCESS_CLEANUP_FAILED",
        "status_code": 502,
        "category": "runtime.cleanup",
        "retryable": True,
        "owner": "runtime",
        "user_message": (
            "Session processes could not be stopped; retry after checking the "
            "sandbox runtime."
        ),
    }
    assert caught.value.message == "one or more session processes could not be stopped"
    assert caught.value.data == {"failed_operations": ["stop agent process"]}

    # A retry still has the process-local terminal anchor. The durable snapshot
    # remains authoritative after a backend restart, but throwing away both
    # names after failed process disposal would make a transient failure harder to
    # repair in the same process.
    assert terminal_service_module._pty_sessions["session-cleanup-failed"] == "pty-1"
    runtime_manager.dispose_terminal_session.assert_awaited_once_with(
        "session-cleanup-failed",
        sandbox_id="sandbox-1",
        pty_session_id="pty-1",
    )
    terminal_service_module._pty_sessions.pop("session-cleanup-failed", None)


@pytest.mark.asyncio
async def test_final_disposal_dispatches_a_durable_anchor_to_its_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The no-runtime path remains engine-owned and transport-neutral."""

    calls: list[tuple[str, str]] = []

    class _Transport:
        async def dispose_process(
            self,
            *,
            sandbox_id: str,
            engine_turn_id: str,
        ) -> None:
            calls.append((sandbox_id, engine_turn_id))

    monkeypatch.setattr(
        runtime_manager_module,
        "get_engine_adapter",
        lambda engine_kind: SimpleNamespace(
            process_disposal=lambda: _Transport()
        ),
    )
    runtime_manager = RemoteAgentRuntimeManager()

    await runtime_manager.dispose_runtime_session(
        "session-after-restart",
        sandbox_id="sandbox-1",
        engine_kind="assistant",
        engine_turn_id="opaque-engine-anchor",
    )

    assert calls == [("sandbox-1", "opaque-engine-anchor")]


@pytest.mark.asyncio
async def test_terminate_waits_for_inflight_runtime_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deletion must not step around the lock held by sandbox creation.

    The old five-second fallback popped the runtime map without owning the
    session lock. A slow create could then register its freshly created sandbox
    after deletion had already returned success.
    """

    manager = RemoteAgentRuntimeManager()
    session_lock = manager._get_session_lock("session-provisioning")
    await session_lock.acquire()
    destroy = AsyncMock(return_value=object())
    monkeypatch.setattr(manager, "_destroy_and_forget_pending", destroy)
    monkeypatch.setattr(
        manager,
        "_release_persisted_placement",
        AsyncMock(return_value=None),
    )

    task = asyncio.create_task(
        manager.terminate_runtime(
            "session-provisioning",
            fallback_sandbox_id="sandbox-created-inflight",
        )
    )
    try:
        await asyncio.sleep(0.05)
        assert not task.done(), "delete returned while runtime creation still owned the lock"
    finally:
        session_lock.release()

    await task
    destroy.assert_awaited_once_with(
        "session-provisioning", "sandbox-created-inflight"
    )


@pytest.mark.asyncio
async def test_terminal_reports_its_durable_pty_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lifecycle snapshot needs the shell id after a command has exited."""

    sandbox = object()
    runtime = SimpleNamespace(
        user_id="user-1",
        terminal_cwd="/workspace",
        sandbox=sandbox,
        agent=None,
        current_task=None,
        current_execution_id=None,
    )
    runtime_manager = SimpleNamespace(
        get_runtime=lambda session_id, sandbox_id=None: runtime,
        resolve_session_egress_credentials=AsyncMock(
            return_value=[
                SimpleNamespace(
                    secret_name="PRIVATE_API_TOKEN",
                    secret_value="must-never-enter-the-terminal",
                    placeholder="ASTRABOX-VAULT-CRED::credential-1::stable",
                )
            ]
        ),
    )
    opened: list[tuple[str | None, dict[str, str] | None]] = []

    class _Terminal:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def session_exists(self, pty_session_id: str) -> bool:
            return False

        async def open_session(
            self,
            *,
            cwd: str | None,
            envs: dict[str, str] | None,
        ) -> str:
            opened.append((cwd, envs))
            return "terminal-pty-1"

        async def run(self, pty_session_id: str, command: str):
            yield {"type": "__done__", "exit_code": 0, "cwd": "/workspace"}

    async def resolve_endpoint(candidate: object) -> ResolvedExecdEndpoint:
        assert candidate is sandbox
        return ResolvedExecdEndpoint(
            origin="http://sandbox.example.test",
            headers={},
        )

    terminal_service_module._pty_sessions.pop("session-1", None)
    monkeypatch.setattr(terminal_service_module, "PtyTerminal", _Terminal)
    monkeypatch.setattr(
        terminal_service_module,
        "resolve_execd_endpoint",
        resolve_endpoint,
    )
    monkeypatch.setattr(
        terminal_service_module,
        "get_underlying_sandbox",
        lambda candidate: candidate,
    )
    service = TerminalService(
        runtime_manager=runtime_manager,
        must_get_owned_session=AsyncMock(
            return_value={
                "session_id": "session-1",
                "user_id": "user-1",
                "state": "READY",
                "sandbox_id": "sandbox-1",
                "session_kind": "agent_chat",
                "runtime_identity": {
                    "linux_user": "agent",
                    "home_dir": "/home/agent",
                    "workspace_dir": "/workspace",
                    "workspace_source_dir": "/workspace",
                    "file_root_dir": "/workspace",
                    "config_dir": "/home/agent/.claude",
                    "config_env_var": "CLAUDE_CONFIG_DIR",
                    "sandbox_tenancy": "conversation",
                },
            }
        ),
    )
    on_started = AsyncMock()

    events = [
        event
        async for event in service.run_terminal_command(
            UserContext(user_id="user-1"),
            "session-1",
            "pwd",
            on_execution_started=on_started,
        )
    ]

    on_started.assert_awaited_once_with("terminal-pty-1")
    assert opened == [
        (
            "/workspace",
            {
                "PRIVATE_API_TOKEN": (
                    "ASTRABOX-VAULT-CRED::credential-1::stable"
                )
            },
        )
    ]
    assert "must-never-enter-the-terminal" not in repr(opened)
    assert events[-1] == {"type": "exit", "exit_code": 0}


@pytest.mark.asyncio
async def test_shared_terminal_uses_the_persisted_isolated_session_after_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shared Agent's terminal must never fall back to the box-level root PTY."""

    calls: list[tuple[str, str, str, dict[str, str] | None, float | None]] = []
    bindings: dict[str, asyncio.Task] = {}

    class _Provider:
        async def stream_in_isolated_session(
            self,
            sandbox_id: str,
            isolated_session_id: str,
            *,
            code: str,
            envs: dict[str, str] | None,
            timeout_s: float | None,
        ):
            calls.append((sandbox_id, isolated_session_id, code, envs, timeout_s))
            match = re.search(r"(__ASTRABOX_ISOLATED_TERMINAL_[0-9a-f]+__)", code)
            assert match is not None
            marker = match.group(1)
            yield {"type": "stdout", "text": "2000\n"}
            yield {
                "type": "stdout",
                "text": f"{marker}:0:/workspace/subdir\n",
            }
            yield {"type": "__done__", "exit_code": 0}

    class _RuntimeManager:
        def get_runtime(self, *_args, **_kwargs):
            return None  # process restart: only the durable row remains

        async def connect_sandbox_only(self, _sandbox_id: str):
            raise AssertionError("isolated terminal must not connect to the box-level PTY")

        def resolve_session_terminal_cwd(self, *_args, **_kwargs):
            raise AssertionError("the immutable runtime identity owns the cwd")

        async def resolve_session_egress_credentials(self, session_id: str):
            assert session_id == "session-1"
            return [
                SimpleNamespace(
                    secret_name="PRIVATE_API_TOKEN",
                    placeholder="ASTRABOX-VAULT-CRED::credential-1::stable",
                )
            ]

        def register_terminal_execution(self, execution_id: str, task: asyncio.Task):
            bindings[execution_id] = task

        def unregister_terminal_execution(self, execution_id: str, task: asyncio.Task):
            assert bindings[execution_id] is task
            bindings.pop(execution_id)

    monkeypatch.setattr(terminal_service_module, "sandbox_for_name", lambda name: _Provider())
    monkeypatch.setattr(
        terminal_service_module,
        "resolve_execd_endpoint",
        AsyncMock(side_effect=AssertionError("box-level execd PTY must not be resolved")),
    )
    service = TerminalService(
        runtime_manager=_RuntimeManager(),
        must_get_owned_session=AsyncMock(
            return_value={
                "session_id": "session-1",
                "user_id": "user-1",
                "state": "READY",
                "sandbox_id": "box-1",
                "sandbox_backend": "open_sandbox",
                "session_kind": "agent_chat",
                "runtime_identity": {
                    "linux_user": "conv_1",
                    "home_dir": "/home/conversations/conv_1",
                    "workspace_dir": "/workspace",
                    "workspace_source_dir": "/home/conversations/conv_1/workspace",
                    "config_dir": "/home/conversations/conv_1/.claude",
                    "config_env_var": "CLAUDE_CONFIG_DIR",
                    "temp_dir": "/home/conversations/conv_1/tmp",
                    "sandbox_tenancy": "agent",
                    "uid": 2000,
                    "gid": 2000,
                    "isolated_session_id": "iso-1",
                    "terminal_isolated_session_id": "iso-terminal-1",
                },
            }
        ),
    )
    on_started = AsyncMock()

    events = [
        event
        async for event in service.run_terminal_command(
            UserContext(user_id="user-1"),
            "session-1",
            "id -u; mkdir -p subdir; cd subdir",
            on_execution_started=on_started,
        )
    ]

    assert [(item["type"], item.get("text")) for item in events if item["type"] == "stdout"] == [
        ("stdout", "2000\n")
    ]
    assert events[-2:] == [
        {"type": "__cwd__", "path": "/workspace/subdir"},
        {"type": "exit", "exit_code": 0},
    ]
    assert calls[0][0:2] == ("box-1", "iso-terminal-1")
    assert calls[0][3] == {
        "PRIVATE_API_TOKEN": "ASTRABOX-VAULT-CRED::credential-1::stable"
    }
    assert calls[0][4] is None, "the terminal must not kill long commands on a fixed timer"
    rendered = calls[0][2]
    assert "export HOME=/home/conversations/conv_1" in rendered
    assert "export USER=conv_1 LOGNAME=conv_1" in rendered
    assert "export CLAUDE_CONFIG_DIR=/home/conversations/conv_1/.claude" in rendered
    assert "runuser" not in rendered
    assert bindings == {}
    bound_id = on_started.await_args.args[0]
    assert bound_id.startswith("isolated-run:session-1:")


@pytest.mark.asyncio
async def test_isolated_terminal_interrupt_cancels_its_sse_owner_task() -> None:
    manager = RemoteAgentRuntimeManager.__new__(RemoteAgentRuntimeManager)
    manager._terminal_execution_tasks = {}
    manager._runtimes = {}
    started = asyncio.Event()

    async def _stream_owner() -> None:
        started.set()
        await asyncio.Future()

    task = asyncio.create_task(_stream_owner())
    await started.wait()
    execution_id = "isolated-run:session-1:abc"
    manager.register_terminal_execution(execution_id, task)

    await manager.interrupt_terminal_execution(
        "session-1",
        execution_id=execution_id,
        sandbox_id="box-1",
    )

    with pytest.raises(asyncio.CancelledError):
        await task
    manager.unregister_terminal_execution(execution_id, task)
    assert manager._terminal_execution_tasks == {}


@pytest.mark.asyncio
async def test_shared_terminal_interrupt_replaces_only_its_disposable_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interrupt rotates the terminal shell without touching the Agent runner."""

    stream_entered = asyncio.Event()
    streamed_sessions: list[str] = []
    closed_sessions: list[tuple[str, str]] = []

    class _Provider:
        async def stream_in_isolated_session(
            self,
            sandbox_id: str,
            isolated_session_id: str,
            *,
            code: str,
            timeout_s: float | None,
        ):
            assert sandbox_id == "box-1"
            assert timeout_s is None
            streamed_sessions.append(isolated_session_id)
            if len(streamed_sessions) == 1:
                stream_entered.set()
                await asyncio.Future()
                return

            match = re.search(r"(__ASTRABOX_ISOLATED_TERMINAL_[0-9a-f]+__)", code)
            assert match is not None
            marker = match.group(1)
            yield {"type": "stdout", "text": "replacement-ready\n"}
            yield {
                "type": "stdout",
                "text": f"{marker}:0:/workspace\n",
            }
            yield {"type": "__done__", "exit_code": 0}

        async def close_isolated_session(
            self,
            sandbox_id: str,
            isolated_session_id: str,
        ) -> None:
            closed_sessions.append((sandbox_id, isolated_session_id))

        async def open_isolated_session(self, sandbox_id: str, **kwargs):
            assert sandbox_id == "box-1"
            assert kwargs == {
                "workspace_dir": "/workspace",
                "workspace_source_dir": "/home/conversations/conv_1/workspace",
                "uid": 2000,
                "gid": 2000,
                "share_net": True,
                "extra_writable": ["/home/conversations/conv_1"],
            }
            return SimpleNamespace(session_id="iso-terminal-2")

    provider = _Provider()
    monkeypatch.setattr(
        terminal_service_module,
        "sandbox_for_name",
        lambda _name: provider,
    )
    identity = {
        "linux_user": "conv_1",
        "home_dir": "/home/conversations/conv_1",
        "workspace_dir": "/workspace",
        "workspace_source_dir": "/home/conversations/conv_1/workspace",
        "config_dir": "/home/conversations/conv_1/.claude",
        "config_env_var": "CLAUDE_CONFIG_DIR",
        "temp_dir": "/home/conversations/conv_1/tmp",
        "sandbox_tenancy": "agent",
        "uid": 2000,
        "gid": 2000,
        "isolated_session_id": "iso-agent-1",
        "terminal_isolated_session_id": "iso-terminal-1",
    }
    session = {
        "session_id": "session-1",
        "user_id": "user-1",
        "state": "READY",
        "sandbox_id": "box-1",
        "sandbox_backend": "open_sandbox",
        "session_kind": "agent_chat",
        "runtime_identity": identity,
    }
    runtime = SessionRuntime(
        session_id="session-1",
        agent=None,
        engine_kind="claude_code",
        user_id="user-1",
        sandbox=object(),
        sandbox_id="box-1",
        engine_client=SimpleNamespace(is_live=True),
        engine_manifest=EngineCapabilityManifest(engine_kind="claude_code"),
        conversation_bound=True,
        runtime_identity=identity,
        isolated_session_id="iso-agent-1",
    )
    manager = RemoteAgentRuntimeManager()
    manager._runtimes["session-1"] = runtime
    sessions_repo = SimpleNamespace(update_session=AsyncMock())
    service = TerminalService(
        runtime_manager=manager,
        must_get_owned_session=AsyncMock(return_value=session),
        sessions_repo=sessions_repo,
    )
    execution_id: str | None = None

    async def _remember_execution(value: str) -> None:
        nonlocal execution_id
        execution_id = value

    async def _collect_blocked_command() -> None:
        async for _event in service.run_terminal_command(
            UserContext(user_id="user-1"),
            "session-1",
            "sleep 600",
            on_execution_started=_remember_execution,
        ):
            pass

    owner = asyncio.create_task(_collect_blocked_command())
    await asyncio.wait_for(stream_entered.wait(), timeout=1)
    assert execution_id is not None

    await manager.interrupt_terminal_execution(
        "session-1",
        execution_id=execution_id,
        sandbox_id="box-1",
    )
    with pytest.raises(asyncio.CancelledError):
        await owner

    assert closed_sessions == [("box-1", "iso-terminal-1")]
    assert ("box-1", "iso-agent-1") not in closed_sessions
    sessions_repo.update_session.assert_awaited_once()
    persisted = sessions_repo.update_session.await_args.args[1]["runtime_identity"]
    assert persisted["terminal_isolated_session_id"] == "iso-terminal-2"
    assert session["runtime_identity"] == persisted
    assert runtime.runtime_identity == persisted
    assert manager._terminal_execution_tasks == {}

    recovered_events = [
        event
        async for event in service.run_terminal_command(
            UserContext(user_id="user-1"),
            "session-1",
            "printf replacement-ready",
        )
    ]
    assert streamed_sessions == ["iso-terminal-1", "iso-terminal-2"]
    assert {"type": "stdout", "text": "replacement-ready\n"} in recovered_events
    assert recovered_events[-1] == {"type": "exit", "exit_code": 0}
