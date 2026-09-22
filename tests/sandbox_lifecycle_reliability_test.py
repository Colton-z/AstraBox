"""Sandbox lifecycle reliability at three failure boundaries.

Companion to ``tests/sandbox_death_convergence_test.py`` — same convergence
machinery, three new failure modes it did not cover:

* A probe of an exited / dead / stopped (or missing) container is
  classified terminal, so the dead-binding watcher CONVERGES the session (to
  READY with the binding cleared, never TERMINATED) instead of re-probing it
  forever after a host reboot. A transient probe failure (empty ``sandbox_state``)
  still never converges.
* A Hermes runtime-start failure after the container is started leaves a durable
  allocation for the platform transaction to release instead of hiding cleanup
  inside the adapter.
* A wedged unary control-plane call (probe / connect) hits the deadline
  and surfaces as a probe/connect failure instead of hanging; the docker client is
  built with a connect-level-only timeout so the exec stream stays unbounded.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from astrabox.common.utils.errors import APIError
from astrabox.core.model import SessionState
from astrabox.core.service.orchestrator import runtime_manager as rm
import astrabox.seams.sandbox as sandbox_seam
from astrabox.core.service.orchestrator.engine import hermes
from astrabox.core.service.orchestrator.engine import provisioning
from astrabox.core.service.orchestrator.engine.base import EngineStartupContext
from astrabox.core.service.orchestrator.expiration_watcher import ExpirationWatcher
from astrabox.core.service.orchestrator.runtime_manager import RemoteAgentRuntimeManager
from astrabox.core.service.orchestrator.sandbox_lifecycle import SandboxLifecycleService
from astrabox.seams.sandbox import (
    SANDBOX_LIFECYCLE_PROBE_FAILED,
    SANDBOX_LIFECYCLE_PROBE_NOT_FOUND,
    SANDBOX_LIFECYCLE_PROBE_OK,
    SandboxLifecycleProbeResult,
)
from astrabox.seams.model import ResolvedModelAccess


def _probe(status: str, state: str | None = None) -> SandboxLifecycleProbeResult:
    return SandboxLifecycleProbeResult(probe_status=status, sandbox_state=state)


# ── terminal-probe classification ────────────────────────────────────────────


class TerminalProbeClassificationTests(unittest.TestCase):
    """Exercise the runtime-manager terminal classifiers directly."""

    def setUp(self) -> None:
        self.mgr = RemoteAgentRuntimeManager()

    def test_stopped_container_states_are_terminal(self) -> None:
        # A present-but-stopped container surfaces as PROBE_FAILED carrying the
        # raw state. Treating it as transient would make a rebooted host re-probe
        # every affected session forever instead of converging.
        for state in ("exited", "dead", "stopped"):
            probe = _probe(SANDBOX_LIFECYCLE_PROBE_FAILED, state)
            self.assertTrue(self.mgr._is_terminal_sandbox_lifecycle_probe(probe), state)

    def test_missing_container_is_terminal(self) -> None:
        probe = _probe(SANDBOX_LIFECYCLE_PROBE_NOT_FOUND)
        self.assertTrue(self.mgr._is_terminal_sandbox_lifecycle_probe(probe))

    def test_transient_probe_failure_is_not_terminal(self) -> None:
        # A wedged / unreachable control plane returns PROBE_FAILED with NO state;
        # it must never converge a live binding.
        probe = _probe(SANDBOX_LIFECYCLE_PROBE_FAILED, None)
        self.assertFalse(self.mgr._is_terminal_sandbox_lifecycle_probe(probe))

    def test_running_box_is_not_terminal(self) -> None:
        probe = _probe(SANDBOX_LIFECYCLE_PROBE_OK, "running")
        self.assertFalse(self.mgr._is_terminal_sandbox_lifecycle_probe(probe))

    def test_turn_probe_distinguishes_parked_from_uncertain(self) -> None:
        self.assertTrue(
            self.mgr._is_terminal_turn_sandbox_lifecycle_probe(
                _probe(SANDBOX_LIFECYCLE_PROBE_NOT_FOUND)
            )
        )
        self.assertTrue(
            self.mgr._is_terminal_turn_sandbox_lifecycle_probe(
                _probe(SANDBOX_LIFECYCLE_PROBE_OK, "paused")
            )
        )
        self.assertFalse(
            self.mgr._is_terminal_turn_sandbox_lifecycle_probe(
                _probe(SANDBOX_LIFECYCLE_PROBE_FAILED)
            )
        )
        self.assertFalse(
            self.mgr._is_terminal_turn_sandbox_lifecycle_probe(
                _probe(SANDBOX_LIFECYCLE_PROBE_OK, "running")
            )
        )


class _FakeSessionsRepo:
    def __init__(self, candidates: list[dict[str, Any]]) -> None:
        self._candidates = list(candidates)
        self.update_calls: list[dict[str, Any]] = []

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        return None

    async def update_session(
        self, session_id: str, updates: dict[str, Any], *, touch_updated_at: bool = True
    ) -> bool:
        self.update_calls.append({"session_id": session_id, "updates": dict(updates)})
        return True

    async def compare_and_update_session(
        self,
        session_id: str,
        *,
        expected: dict[str, Any],
        updates: dict[str, Any],
        touch_updated_at: bool = True,
    ) -> bool:
        self.update_calls.append({"session_id": session_id, "updates": dict(updates)})
        return True

    async def list_sessions_by_sandbox_id(
        self, sandbox_id: str
    ) -> list[dict[str, Any]]:
        return [
            dict(item)
            for item in self._candidates
            if str(item.get("sandbox_id") or "") == str(sandbox_id)
        ]

    async def list_dead_binding_probe_candidates(
        self, *, now_iso: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        return [dict(item) for item in self._candidates]


class _NoAgentOwners:
    async def list_dead_binding_probe_candidates(
        self, *, now_iso: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        return []

    async def list_agents_by_sandbox_id(
        self, sandbox_id: str
    ) -> list[dict[str, Any]]:
        return []


class _NoAssistantWorkspaceOwners:
    async def list_dead_binding_probe_candidates(
        self, *, now_iso: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        return []

    async def list_workspaces_by_sandbox_id(
        self, sandbox_id: str
    ) -> list[dict[str, Any]]:
        return []


class _ExitedProbeRuntimeManager:
    """Returns a PROBE_FAILED+exited probe and classifies via the REAL manager."""

    def __init__(self) -> None:
        self._real = RemoteAgentRuntimeManager()
        self.evicted: list[str] = []

    async def get_sandbox_lifecycle_probe(self, sandbox_id: str) -> SandboxLifecycleProbeResult:
        return _probe(SANDBOX_LIFECYCLE_PROBE_FAILED, "exited")

    def _is_terminal_sandbox_lifecycle_probe(self, probe: Any) -> bool:
        return self._real._is_terminal_sandbox_lifecycle_probe(probe)

    async def get_sandbox_expires_at(self, sandbox_id: str) -> Any:
        return None

    async def evict_runtime(self, session_id: str) -> None:
        self.evicted.append(session_id)


def _fake_platform(sessions_repo: _FakeSessionsRepo, runtime_manager: Any) -> Any:
    projections: list[dict[str, Any]] = []

    async def _sync_projection(**kwargs: Any) -> None:
        projections.append(dict(kwargs))

    agent_service = SimpleNamespace(_agent_repo=object())
    platform = SimpleNamespace(
        _sessions_repo=sessions_repo,
        _agent_repo=_NoAgentOwners(),
        _assistant_workspace_service=_NoAssistantWorkspaceOwners(),
        _runtime_manager=runtime_manager,
        _sync_lifecycle_projection_from_session=_sync_projection,
        _agent_service_getter=lambda: agent_service,
        projections=projections,
    )
    platform.agent_service = agent_service
    platform._sandbox_lifecycle_service = SandboxLifecycleService(
        platform_service=platform
    )
    return platform


class ExitedContainerConvergesTests(unittest.IsolatedAsyncioTestCase):
    async def test_exited_container_converges_to_ready_not_terminated(self) -> None:
        sessions_repo = _FakeSessionsRepo(
            [{"session_id": "s-1", "sandbox_id": "sbx-1", "session_kind": "agent_chat"}]
        )
        runtime_manager = _ExitedProbeRuntimeManager()
        platform = _fake_platform(sessions_repo, runtime_manager)
        watcher = ExpirationWatcher(platform_service=platform)

        summary = await watcher.scan_once()

        # Converged through the single dead-binding owner — not stuck in the
        # alive/probe_failed re-probe loop.
        self.assertEqual(summary["converged"], 1)
        self.assertEqual(summary.get("alive", 0), 0)
        self.assertEqual(summary.get("probe_failed", 0), 0)
        self.assertEqual(runtime_manager.evicted, ["s-1"])
        # ...and the persisted convergence is READY with the binding cleared: sandbox
        # death converges the binding WITHOUT terminating the conversation.
        self.assertEqual(len(sessions_repo.update_calls), 1)
        updates = sessions_repo.update_calls[0]["updates"]
        self.assertEqual(updates["state"], SessionState.READY.value)
        self.assertNotEqual(updates["state"], "TERMINATED")
        self.assertIs(updates["runtime_unavailable"], True)
        self.assertIsNone(updates["sandbox_id"])


# ── Hermes create failure reaps the container ────────────────────────────────


class _FakeCommands:
    async def run(self, command: str, *, envs: Any = None, timeout_in_millis: Any = None) -> Any:
        # In-box command server never comes up -> the readiness gate times out.
        raise ConnectionError("in-box command server not up")


class _FakeSandbox:
    def __init__(self) -> None:
        self.id = "sbx-hermes-startup"
        self.commands = _FakeCommands()


class _FakeBackend:
    name = "fake-hermes"
    supports_correlated_create = True

    def __init__(self, sandbox: _FakeSandbox) -> None:
        self._sandbox = sandbox

    async def create_sandbox(self, spec: Any) -> Any:
        return self._sandbox

    async def find_sandbox_by_assignment(self, assignment_id: str) -> None:
        return None


class _FakeHermesManager:
    def __init__(self) -> None:
        self.allocations: list[tuple[str, Any]] = []

    async def resolve_runtime_sandbox_backend(
        self,
        session_id: str,
        *,
        workspace_plan: Any,
    ) -> str:
        return "fake-hermes"

    def resolve_sandbox_backend_secret(
        self,
        template: Any,
        *,
        backend: str | None = None,
    ) -> str:
        _ = backend
        return "sbx-key"

    def resolve_model_access(self, mc: dict[str, Any]) -> ResolvedModelAccess:
        return ResolvedModelAccess(
            configuration=dict(mc),
            base_url="https://gateway.test/v1",
            model_name="hermes-model",
            credential="sk-test",
            credential_kind="bearer",
            endpoint_provider="litellm",
        )

    async def record_startup_allocation(self, session_id: str, allocation: Any) -> None:
        self.allocations.append((session_id, allocation))


class HermesCreateFailureNamesAllocationTests(unittest.IsolatedAsyncioTestCase):
    async def test_readiness_failure_after_create_keeps_platform_cleanup_name(self) -> None:
        sandbox = _FakeSandbox()
        manager = _FakeHermesManager()
        backend = _FakeBackend(sandbox)
        template = SimpleNamespace(model_config={}, engine_kind="assistant", agent_id="")
        workspace_plan = SimpleNamespace(
            cwd="/w",
            subject_kind="assistant",
            user_id="u",
            assistant_id="a",
            engine_kind="assistant",
            conversation_session_id="s-1",
            materialize_default_repo=False,
            default_repo_target_cwd="",
        )

        with (
            patch.object(hermes, "_HERMES_INBOX_READY_TIMEOUT_S", 0),
            patch.object(
                provisioning,
                "resolve_model_credential_delivery",
                lambda *a, **k: ("model-key", None, None),
            ),
            patch.object(
                provisioning,
                "resolve_mcp_credential_plan",
                AsyncMock(return_value=None),
            ),
            patch.object(
                provisioning,
                "resolve_session_environment_credentials",
                AsyncMock(return_value=(None, {})),
            ),
            patch.object(
                provisioning, "resolve_runtime_template_name", lambda *a, **k: "img"
            ),
            patch.object(
                provisioning, "plan_conversation_identity", lambda **k: None
            ),
            patch.object(
                provisioning,
                "plan_workspace_mounts",
                AsyncMock(return_value=()),
            ),
            patch.object(
                provisioning,
                "workspace_is_ready",
                AsyncMock(return_value=None),
            ),
            patch.object(
                provisioning,
                "prepare_platform_workspace",
                AsyncMock(return_value=(None, "/w")),
            ),
            patch.object(sandbox_seam, "sandbox_for_name", lambda *a, **k: backend),
        ):
            adapter = hermes.HermesEngineAdapter()
            model_access = manager.resolve_model_access(template.model_config)
            provisioned = await provisioning.provision_engine_sandbox(
                manager,
                session_id="s-1",
                assignment_id="assignment-1",
                template=template,
                workspace_plan=workspace_plan,
                user_id="u",
                callback_url=None,
                request=adapter.sandbox_request(
                    template=template,
                    model_access=model_access,
                ),
            )
            with self.assertRaises(APIError):
                await adapter.activate_runtime(
                    EngineStartupContext(
                    session_id="s-1",
                    template=template,
                    workspace_plan=workspace_plan,
                    sandbox=provisioned.sandbox,
                    sandbox_id=provisioned.sandbox_id,
                    cwd=provisioned.cwd,
                    runtime_identity=provisioned.runtime_identity,
                    model_access=model_access,
                    model_credential=provisioned.model_credential,
                    resume_session_key=None,
                    user_id="u",
                    )
                )

        # The adapter closes only its local client. The surrounding platform
        # transaction owns rollback and can recover the exact resource from this
        # durable name after a process restart.
        self.assertEqual(len(manager.allocations), 1)
        session_id, allocation = manager.allocations[0]
        self.assertEqual(session_id, "s-1")
        self.assertEqual(allocation.sandbox_id, sandbox.id)
        self.assertEqual(allocation.scope, "sandbox")


# ── wedged control-plane call hits the deadline ──────────────────────────────


class _HangingProvider:
    """A provider whose control-plane ops never return (wedged dockerd)."""

    def __init__(self) -> None:
        self._hang = asyncio.Event()  # never set

    async def probe(self, sandbox_id: str) -> Any:
        await self._hang.wait()

    async def connect(self, sandbox_id: str) -> Any:
        await self._hang.wait()

    async def kill(self, sandbox_id: str) -> bool:
        await self._hang.wait()
        return True


class WedgedControlCallDeadlineTests(unittest.IsolatedAsyncioTestCase):
    def _manager(self) -> RemoteAgentRuntimeManager:
        manager = RemoteAgentRuntimeManager()

        async def _resolve(sandbox_id: str) -> str:
            return "open_sandbox"

        manager._resolve_sandbox_backend = _resolve  # type: ignore[method-assign]
        return manager

    async def test_wedged_probe_deadlines_to_transient_probe_failed(self) -> None:
        manager = self._manager()
        with (
            patch.dict(os.environ, {"ASTRABOX_SANDBOX_CONTROL_DEADLINE_S": "0.1"}),
            patch.object(rm, "sandbox_for_name", return_value=_HangingProvider()),
        ):
            probe = await manager.get_sandbox_lifecycle_probe("sbx-1")

        self.assertEqual(probe.probe_status, SANDBOX_LIFECYCLE_PROBE_FAILED)
        self.assertIn("timed out", str(probe.error_text or ""))
        # A timed-out probe is transient (empty state) -> never converges a binding.
        self.assertFalse(manager._is_terminal_sandbox_lifecycle_probe(probe))

    async def test_wedged_connect_deadlines_to_failure(self) -> None:
        manager = self._manager()
        with (
            patch.dict(os.environ, {"ASTRABOX_SANDBOX_CONTROL_DEADLINE_S": "0.1"}),
            patch.object(rm, "sandbox_for_name", return_value=_HangingProvider()),
        ):
            with self.assertRaises(TimeoutError):
                await manager.connect_sandbox_only("sbx-1")


if __name__ == "__main__":
    unittest.main()
