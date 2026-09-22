"""Sandbox-death convergence: one owner-wide entry, two delivery layers.

A session bound to a dead sandbox must converge the same way no matter how the
death is discovered: the provider's status callback (push) and the expiration
watcher's probe reconciler (pull) both end in
``SandboxLifecycleService.converge_dead_sandbox_owners``. Each owner keeps its
own state transition; Sessions use the canonical ``terminal_session_updates``
shape. These tests pin that contract:

* the canonical update shape per session kind (both clear busy locks and
  current-turn markers — a session without a sandbox cannot have a live turn);
* ``converge_dead_sandbox`` persists the shape and applies the terminal side
  effects (runtime eviction, MCP disconnect, kernel projection);
* the callback path still applies side effects only on terminal statuses;
* ``ExpirationWatcher.scan_once`` converges only control-plane-confirmed
  terminal probes — an alive answer or a probe failure never converges;
* the candidate query excludes lease-active, converged and terminal sessions.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from astrabox.core.model import SessionState
from astrabox.core.service.orchestrator.expiration_watcher import ExpirationWatcher
from astrabox.core.service.orchestrator.sandbox_lifecycle import (
    SandboxLifecycleService,
    terminal_session_updates,
)
from astrabox.seams.sandbox import SandboxCreateSpec


class _FakeSessionsRepo:
    def __init__(
        self,
        *,
        session: dict[str, Any] | None = None,
        candidates: list[dict[str, Any]] | None = None,
    ) -> None:
        self._session = session
        self._candidates = list(candidates or [])
        self.update_calls: list[dict[str, Any]] = []
        self.cas_calls: list[dict[str, Any]] = []
        self.candidate_calls: list[dict[str, Any]] = []
        self.events: list[str] | None = None

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        return dict(self._session) if self._session else None

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
        if self.events is not None:
            self.events.append(f"session:{session_id}")
        self.cas_calls.append(
            {"session_id": session_id, "expected": dict(expected), "updates": dict(updates)}
        )
        rows = [self._session] if self._session is not None else self._candidates
        row = next(
            ((item or {}) for item in rows if (item or {}).get("session_id") == session_id),
            None,
        )
        if row is None or any(row.get(key) != value for key, value in expected.items()):
            return False
        row.update(updates)
        return True

    async def list_dead_binding_probe_candidates(
        self, *, now_iso: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        self.candidate_calls.append({"now_iso": now_iso, "limit": limit})
        return [dict(item) for item in self._candidates]

    async def list_idle_reclaim_candidates(
        self, *, now_iso: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        _ = (now_iso, limit)
        return []

    async def list_sessions_by_sandbox_id(
        self, sandbox_id: str
    ) -> list[dict[str, Any]]:
        rows = [self._session] if self._session is not None else self._candidates
        return [
            dict(row)
            for row in rows
            if str((row or {}).get("sandbox_id") or "").strip() == sandbox_id
        ]


class _FakeAgentRepo:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = [dict(row) for row in rows or []]
        self.cas_calls: list[dict[str, Any]] = []
        self.events: list[str] | None = None

    async def list_agents_by_sandbox_id(
        self, sandbox_id: str
    ) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.rows
            if str(row.get("sandbox_id") or "").strip() == sandbox_id
        ]

    async def list_dead_binding_probe_candidates(
        self, *, now_iso: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        _ = now_iso
        return [
            dict(row)
            for row in self.rows[:limit]
            if str(row.get("sandbox_id") or "").strip()
        ]

    async def compare_and_update_agent(
        self,
        agent_id: str,
        *,
        expected: dict[str, Any],
        updates: dict[str, Any],
    ) -> bool:
        if self.events is not None:
            self.events.append(f"agent:{agent_id}")
        self.cas_calls.append(
            {
                "agent_id": agent_id,
                "expected": dict(expected),
                "updates": dict(updates),
            }
        )
        row = next((item for item in self.rows if item.get("agent_id") == agent_id), None)
        if row is None or any(row.get(key) != value for key, value in expected.items()):
            return False
        row.update(updates)
        return True


class _FakeAssistantWorkspaceService:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = [dict(row) for row in rows or []]
        self.converge_calls: list[dict[str, Any]] = []
        self.events: list[str] | None = None

    async def list_workspaces_by_sandbox_id(
        self, sandbox_id: str
    ) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.rows
            if str(row.get("current_sandbox_id") or "").strip() == sandbox_id
        ]

    async def list_dead_binding_probe_candidates(
        self, *, now_iso: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        _ = now_iso
        return [
            dict(row)
            for row in self.rows[:limit]
            if str(row.get("current_sandbox_id") or "").strip()
        ]

    async def realign_live_sandbox(
        self,
        *,
        workspace: dict[str, Any],
        sandbox_id: str,
        expires_at: str | None,
    ) -> bool:
        assistant_id = str(workspace.get("assistant_id") or "")
        row = next(
            (item for item in self.rows if item.get("assistant_id") == assistant_id),
            None,
        )
        if row is None or row.get("current_sandbox_id") != sandbox_id:
            return False
        if expires_at is not None:
            row["current_sandbox_expires_at"] = expires_at
        return True

    async def converge_dead_sandbox(
        self,
        *,
        workspace: dict[str, Any],
        sandbox_id: str,
        last_error: str,
    ) -> bool:
        if self.events is not None:
            self.events.append(
                f"workspace:{str(workspace.get('assistant_id') or '')}"
            )
        self.converge_calls.append(
            {
                "workspace": dict(workspace),
                "sandbox_id": sandbox_id,
                "last_error": last_error,
            }
        )
        assistant_id = str(workspace.get("assistant_id") or "")
        row = next(
            (item for item in self.rows if item.get("assistant_id") == assistant_id),
            None,
        )
        if row is None or row.get("current_sandbox_id") != sandbox_id:
            return False
        row.update(
            {
                "state": "RECOVERY_REQUIRED",
                "current_sandbox_id": None,
                "current_sandbox_expires_at": None,
                "hibernated_at": None,
                "runtime_identity": None,
                "assistant_profiles": {},
                "last_error": last_error,
            }
        )
        return True


class _FakeRuntimeManager:
    def __init__(self, probe_results: dict[str, Any] | None = None) -> None:
        self.evicted: list[str] = []
        self.probe_calls: list[str] = []
        self.probe_results = dict(probe_results or {})
        self.probe_error: Exception | None = None

    async def evict_runtime(self, session_id: str) -> None:
        self.evicted.append(session_id)

    async def reconcile_startup_allocations(self, **_kwargs: Any) -> dict[str, int]:
        return {}

    async def reap_abandoned_agent_boxes(self) -> dict[str, int]:
        return {}

    async def reap_ownerless_sandboxes(self) -> dict[str, int]:
        return {}

    async def keep_prewarmed_agents_ready(self) -> dict[str, int]:
        return {}

    async def get_sandbox_lifecycle_probe(self, sandbox_id: str) -> Any:
        self.probe_calls.append(sandbox_id)
        if self.probe_error is not None:
            raise self.probe_error
        return self.probe_results.get(
            sandbox_id,
            SimpleNamespace(probe_status="OK", sandbox_state="running", error_text=None),
        )

    async def get_sandbox_expires_at(self, sandbox_id: str) -> datetime:
        _ = sandbox_id
        return datetime(2099, 1, 1, tzinfo=timezone.utc)

    def _is_terminal_sandbox_lifecycle_probe(self, probe: Any) -> bool:
        if str(getattr(probe, "probe_status", "") or "").strip() == "NOT_FOUND":
            return True
        state = str(getattr(probe, "sandbox_state", "") or "").strip()
        return (
            str(getattr(probe, "probe_status", "") or "").strip() == "OK"
            and state in {"Terminated", "Failed"}
        )


def _fake_platform(
    *,
    sessions_repo: _FakeSessionsRepo,
    runtime_manager: _FakeRuntimeManager,
    agent_repo: _FakeAgentRepo | None = None,
    workspace_service: _FakeAssistantWorkspaceService | None = None,
) -> Any:
    projections: list[dict[str, Any]] = []

    async def _sync_projection(**kwargs: Any) -> None:
        projections.append(dict(kwargs))

    resolved_agent_repo = agent_repo or _FakeAgentRepo()
    resolved_workspace_service = workspace_service or _FakeAssistantWorkspaceService()
    fake_agent_service = SimpleNamespace(_agent_repo=resolved_agent_repo)
    platform = SimpleNamespace(
        _sessions_repo=sessions_repo,
        _agent_repo=resolved_agent_repo,
        _assistant_workspace_service=resolved_workspace_service,
        _runtime_manager=runtime_manager,
        _sync_lifecycle_projection_from_session=_sync_projection,
        _agent_service_getter=lambda: fake_agent_service,
        projections=projections,
    )
    platform.agent_service = fake_agent_service
    platform._sandbox_lifecycle_service = SandboxLifecycleService(
        platform_service=platform,
    )
    return platform


_GONE = SimpleNamespace(probe_status="NOT_FOUND", sandbox_state="", error_text=None)


class TerminalSessionUpdatesTests(unittest.TestCase):
    def test_agent_chat_returns_ready_with_cleared_binding(self) -> None:
        updates = terminal_session_updates(
            session={"session_kind": "agent_chat"}, last_error="sandbox terminated"
        )
        self.assertEqual(updates["state"], SessionState.READY.value)
        self.assertIs(updates["runtime_unavailable"], True)
        self.assertIsNone(updates["sandbox_id"])
        self.assertIsNone(updates["sandbox_endpoint"])
        self.assertIsNone(updates["current_turn_id"])

    def test_every_kind_stays_ready_and_clears_turn_markers(self) -> None:
        # Product invariant: sandbox death never terminates a conversation —
        # TERMINATED is reserved for explicit end/delete. Every kind converges
        # to READY with the binding gone and no surviving turn markers.
        updates = terminal_session_updates(session={}, last_error="sandbox terminated")
        self.assertEqual(updates["state"], SessionState.READY.value)
        self.assertIs(updates["runtime_unavailable"], True)
        self.assertIsNone(updates["sandbox_id"])
        self.assertIsNone(updates["sandbox_endpoint"])
        self.assertIsNone(updates["current_turn_id"])
        self.assertIsNone(updates["current_turn_partial"])


class ConvergeDeadSandboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_persists_canonical_shape_and_applies_side_effects(self) -> None:
        session = {
            "session_id": "s-1",
            "session_kind": "agent_chat",
            "sandbox_id": "sbx-1",
        }
        sessions_repo = _FakeSessionsRepo(session=session)
        runtime_manager = _FakeRuntimeManager()
        platform = _fake_platform(sessions_repo=sessions_repo, runtime_manager=runtime_manager)
        service = SandboxLifecycleService(platform_service=platform)

        converged = await service.converge_dead_sandbox(
            session, last_error="sandbox terminated", reason="dead_binding_reconcile:NOT_FOUND"
        )

        self.assertTrue(converged)
        self.assertEqual(len(sessions_repo.cas_calls), 1)
        update = sessions_repo.cas_calls[0]
        self.assertEqual(update["session_id"], "s-1")
        self.assertEqual(update["expected"], {"sandbox_id": "sbx-1"})
        self.assertEqual(
            update["updates"],
            terminal_session_updates(session=session, last_error="sandbox terminated"),
        )
        self.assertEqual(runtime_manager.evicted, ["s-1"])
        self.assertEqual(len(platform.projections), 1)
        self.assertEqual(
            platform.projections[0]["reason"], "dead_binding_reconcile:NOT_FOUND"
        )

    async def test_missing_session_id_is_a_noop(self) -> None:
        sessions_repo = _FakeSessionsRepo()
        runtime_manager = _FakeRuntimeManager()
        platform = _fake_platform(sessions_repo=sessions_repo, runtime_manager=runtime_manager)
        service = SandboxLifecycleService(platform_service=platform)

        self.assertFalse(
            await service.converge_dead_sandbox({}, last_error="x", reason="r")
        )
        self.assertEqual(sessions_repo.update_calls, [])
        self.assertEqual(runtime_manager.evicted, [])

    async def test_one_terminal_fact_converges_every_owner_of_the_box(self) -> None:
        sessions_repo = _FakeSessionsRepo(
            candidates=[
                {
                    "session_id": "s-1",
                    "session_kind": "agent_chat",
                    "sandbox_id": "sbx-1",
                },
                {
                    "session_id": "s-2",
                    "session_kind": "assistant_chat",
                    "sandbox_id": "sbx-1",
                },
            ]
        )
        agent_repo = _FakeAgentRepo(
            [
                {
                    "agent_id": "agent-1",
                    "sandbox_id": "sbx-1",
                    "sandbox_backend": "open_sandbox",
                    "_resident_sandbox_generation": "sbx-1",
                }
            ]
        )
        workspace_service = _FakeAssistantWorkspaceService(
            [
                {
                    "assistant_id": "assistant-1",
                    "state": "READY",
                    "current_sandbox_id": "sbx-1",
                    "current_sandbox_expires_at": "2099-01-01T00:00:00+00:00",
                    "runtime_identity": {"sandbox_id": "sbx-1"},
                    "assistant_profiles": {"owner": {"sandbox_id": "sbx-1"}},
                }
            ]
        )
        runtime_manager = _FakeRuntimeManager()
        platform = _fake_platform(
            sessions_repo=sessions_repo,
            runtime_manager=runtime_manager,
            agent_repo=agent_repo,
            workspace_service=workspace_service,
        )
        service = SandboxLifecycleService(platform_service=platform)

        result = await service.converge_dead_sandbox_owners(
            "sbx-1",
            last_error="sandbox terminated",
            reason="control_plane_not_found",
        )

        self.assertEqual(result.converged_sessions, ("s-1", "s-2"))
        self.assertEqual(result.converged_agents, ("agent-1",))
        self.assertEqual(result.converged_assistant_workspaces, ("assistant-1",))
        self.assertEqual(runtime_manager.evicted, ["s-1", "s-2"])
        self.assertEqual(
            agent_repo.cas_calls,
            [
                {
                    "agent_id": "agent-1",
                    "expected": {"sandbox_id": "sbx-1"},
                    "updates": {
                        "sandbox_id": None,
                        "sandbox_backend": None,
                        "_resident_sandbox_generation": None,
                        "expires_at": None,
                    },
                }
            ],
        )
        self.assertEqual(workspace_service.rows[0]["state"], "RECOVERY_REQUIRED")
        self.assertIsNone(workspace_service.rows[0]["current_sandbox_id"])
        self.assertIsNone(workspace_service.rows[0]["runtime_identity"])
        self.assertEqual(workspace_service.rows[0]["assistant_profiles"], {})

    async def test_box_notice_preserves_planned_session_park_and_workspace_release(self) -> None:
        sessions_repo = _FakeSessionsRepo(
            candidates=[
                {
                    "session_id": "parked-session",
                    "sandbox_id": "sbx-1",
                    "agent_id": "agent-1",
                    "sandbox_parked_at": "2026-08-16T00:00:00+00:00",
                },
                {
                    "session_id": "assistant-session",
                    "sandbox_id": "sbx-1",
                    "workspace_ref": {
                        "kind": "assistant",
                        "assistant_id": "assistant-1",
                    },
                },
                {
                    "session_id": "unrelated-session",
                    "sandbox_id": "sbx-1",
                }
            ]
        )
        agent_repo = _FakeAgentRepo(
            [{"agent_id": "agent-1", "sandbox_id": "sbx-1"}]
        )
        workspace_service = _FakeAssistantWorkspaceService(
            [
                {
                    "assistant_id": "assistant-1",
                    "state": "RECOVERY_REQUIRED",
                    "current_sandbox_id": "sbx-1",
                    "hibernated_at": "2026-08-16T00:00:00+00:00",
                }
            ]
        )
        runtime_manager = _FakeRuntimeManager()
        platform = _fake_platform(
            sessions_repo=sessions_repo,
            runtime_manager=runtime_manager,
            agent_repo=agent_repo,
            workspace_service=workspace_service,
        )
        service = SandboxLifecycleService(platform_service=platform)

        result = await service.converge_dead_sandbox_owners(
            "sbx-1",
            last_error="sandbox terminated",
            reason="box_terminating_notice",
            preserve_planned_teardowns=True,
        )

        self.assertEqual(result.converged_sessions, ("unrelated-session",))
        self.assertEqual(
            result.ignored_sessions,
            {
                "parked-session": "session_parked",
                "assistant-session": "workspace_release_pending",
            },
        )
        self.assertEqual(result.converged_agents, ())
        self.assertEqual(result.ignored_agents, {"agent-1": "agent_box_parked"})
        self.assertEqual(result.converged_assistant_workspaces, ())
        self.assertEqual(
            result.ignored_assistant_workspaces,
            {"assistant-1": "workspace_release_pending"},
        )
        self.assertEqual(len(sessions_repo.cas_calls), 1)
        self.assertEqual(
            sessions_repo.cas_calls[0]["session_id"], "unrelated-session"
        )
        self.assertEqual(agent_repo.cas_calls, [])
        self.assertEqual(workspace_service.converge_calls, [])

    async def test_box_notice_does_not_preserve_a_pre_commit_hibernate(self) -> None:
        """HIBERNATING is intent to read files, not proof they reached storage."""
        sessions_repo = _FakeSessionsRepo(
            candidates=[
                {
                    "session_id": "assistant-session",
                    "sandbox_id": "sbx-1",
                    "workspace_ref": {
                        "kind": "assistant",
                        "assistant_id": "assistant-1",
                    },
                }
            ]
        )
        workspace_service = _FakeAssistantWorkspaceService(
            [
                {
                    "assistant_id": "assistant-1",
                    "state": "HIBERNATING",
                    "current_sandbox_id": "sbx-1",
                    "hibernated_at": "2026-08-16T00:00:00+00:00",
                }
            ]
        )
        platform = _fake_platform(
            sessions_repo=sessions_repo,
            runtime_manager=_FakeRuntimeManager(),
            workspace_service=workspace_service,
        )
        service = SandboxLifecycleService(platform_service=platform)

        result = await service.converge_dead_sandbox_owners(
            "sbx-1",
            last_error="sandbox terminated before workspace commit",
            reason="box_terminating_notice",
            preserve_planned_teardowns=True,
        )

        self.assertEqual(result.converged_assistant_workspaces, ("assistant-1",))
        self.assertEqual(result.converged_sessions, ("assistant-session",))
        self.assertEqual(result.ignored_assistant_workspaces, {})
        self.assertEqual(result.ignored_sessions, {})

    async def test_late_terminal_fact_cannot_clear_a_replacement_sandbox(self) -> None:
        sessions_repo = _FakeSessionsRepo(
            candidates=[{"session_id": "s-1", "sandbox_id": "sbx-old"}]
        )
        runtime_manager = _FakeRuntimeManager()
        platform = _fake_platform(
            sessions_repo=sessions_repo,
            runtime_manager=runtime_manager,
        )
        service = SandboxLifecycleService(platform_service=platform)
        stale = {"session_id": "s-1", "sandbox_id": "sbx-old"}
        sessions_repo._candidates[0]["sandbox_id"] = "sbx-new"

        converged = await service.converge_dead_sandbox(
            stale,
            last_error="sandbox terminated",
            reason="late_notice",
        )

        self.assertFalse(converged)
        self.assertEqual(sessions_repo.update_calls, [])
        self.assertEqual(runtime_manager.evicted, [])

    async def test_parent_owner_fences_precede_session_projection_clear(self) -> None:
        sessions_repo = _FakeSessionsRepo(
            candidates=[{"session_id": "s-1", "sandbox_id": "sbx-1"}]
        )
        agent_repo = _FakeAgentRepo(
            [{"agent_id": "agent-1", "sandbox_id": "sbx-1"}]
        )
        workspace_service = _FakeAssistantWorkspaceService(
            [
                {
                    "assistant_id": "assistant-1",
                    "state": "READY",
                    "current_sandbox_id": "sbx-1",
                }
            ]
        )
        events: list[str] = []
        sessions_repo.events = events
        agent_repo.events = events
        workspace_service.events = events
        platform = _fake_platform(
            sessions_repo=sessions_repo,
            runtime_manager=_FakeRuntimeManager(),
            agent_repo=agent_repo,
            workspace_service=workspace_service,
        )

        await platform._sandbox_lifecycle_service.converge_dead_sandbox_owners(
            "sbx-1",
            last_error="sandbox terminated",
            reason="control_plane_not_found",
        )

        self.assertEqual(
            events,
            ["agent:agent-1", "workspace:assistant-1", "session:s-1"],
        )


class CallbackTerminalPathTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, session: dict[str, Any]) -> tuple[SandboxLifecycleService, Any]:
        sessions_repo = _FakeSessionsRepo(session=session)
        runtime_manager = _FakeRuntimeManager()
        platform = _fake_platform(sessions_repo=sessions_repo, runtime_manager=runtime_manager)
        service = SandboxLifecycleService(platform_service=platform)
        return service, platform

    async def test_terminal_callback_applies_side_effects(self) -> None:
        session = {
            "session_id": "s-cb",
            "session_kind": "agent_chat",
            "sandbox_id": "sbx-cb",
            "sandbox_generation": "gen-1",
            "sandbox_callback_token": "tok-1",
        }
        service, platform = self._service(session)

        result = await service.handle_callback(
            subject_type="session",
            subject_id="s-cb",
            generation="gen-1",
            token="tok-1",
            payload={"status": "terminated"},
        )

        self.assertTrue(result["handled"])
        self.assertEqual(platform._runtime_manager.evicted, ["s-cb"])
        self.assertEqual(len(platform.projections), 1)

    async def test_non_terminal_callback_does_not_evict(self) -> None:
        session = {
            "session_id": "s-cb",
            "session_kind": "agent_chat",
            "sandbox_generation": "gen-1",
            "sandbox_callback_token": "tok-1",
            "expires_at": None,
        }
        service, platform = self._service(session)

        result = await service.handle_callback(
            subject_type="session",
            subject_id="s-cb",
            generation="gen-1",
            token="tok-1",
            payload={"status": "running", "expireTime": "2099-01-01T00:00:00+00:00"},
        )

        self.assertTrue(result["handled"])
        self.assertEqual(platform._runtime_manager.evicted, [])
        # the expires_at-only update still projects
        self.assertEqual(len(platform.projections), 1)


class ScanOnceTests(unittest.IsolatedAsyncioTestCase):
    def _watcher(
        self,
        candidates: list[dict[str, Any]],
        probe_results: dict[str, Any] | None = None,
        *,
        agents: list[dict[str, Any]] | None = None,
        workspaces: list[dict[str, Any]] | None = None,
    ) -> tuple[ExpirationWatcher, _FakeSessionsRepo, _FakeRuntimeManager]:
        sessions_repo = _FakeSessionsRepo(candidates=candidates)
        runtime_manager = _FakeRuntimeManager(probe_results)
        platform = _fake_platform(
            sessions_repo=sessions_repo,
            runtime_manager=runtime_manager,
            agent_repo=_FakeAgentRepo(agents),
            workspace_service=_FakeAssistantWorkspaceService(workspaces),
        )
        return ExpirationWatcher(platform_service=platform), sessions_repo, runtime_manager

    async def test_confirmed_gone_candidate_converges(self) -> None:
        watcher, sessions_repo, runtime_manager = self._watcher(
            [{"session_id": "s-1", "sandbox_id": "sbx-1", "session_kind": "agent_chat"}],
            probe_results={"sbx-1": _GONE},
        )
        summary = await watcher.scan_once()
        self.assertEqual(summary["candidates"], 1)
        self.assertEqual(summary["converged"], 1)
        self.assertEqual(runtime_manager.probe_calls, ["sbx-1"])
        self.assertEqual(len(sessions_repo.cas_calls), 1)
        self.assertIs(
            sessions_repo.cas_calls[0]["updates"]["runtime_unavailable"], True
        )

    async def test_alive_probe_never_converges(self) -> None:
        watcher, sessions_repo, _runtime_manager = self._watcher(
            [{"session_id": "s-1", "sandbox_id": "sbx-1"}],
        )
        summary = await watcher.scan_once()
        self.assertEqual(summary["alive"], 1)
        self.assertEqual(summary["converged"], 0)
        # Nothing that CONVERGES. The bookkeeping that takes a live session out
        # of the candidate set is not convergence and has to be allowed, or a
        # box that answered RUNNING is re-probed on every tick forever — the
        # sweep already realigns `expires_at` here for the same reason. The
        # assertion is on the dead-binding fields, not on writing at all.
        for call in sessions_repo.update_calls:
            for field in ("runtime_unavailable", "sandbox_id", "state", "last_error"):
                self.assertNotIn(
                    field,
                    call["updates"],
                    f"an alive probe must not write {field}",
                )

    async def test_probe_error_never_converges(self) -> None:
        watcher, sessions_repo, runtime_manager = self._watcher(
            [{"session_id": "s-1", "sandbox_id": "sbx-1"}],
        )
        runtime_manager.probe_error = RuntimeError("control plane down")
        summary = await watcher.scan_once()
        self.assertEqual(summary["probe_failed"], 1)
        self.assertEqual(summary["converged"], 0)
        self.assertEqual(sessions_repo.update_calls, [])

    async def test_shared_alive_box_is_probed_once_and_repairs_every_candidate(self) -> None:
        watcher, sessions_repo, runtime_manager = self._watcher(
            [
                {"session_id": "s-1", "sandbox_id": "sbx-shared"},
                {"session_id": "s-2", "sandbox_id": "sbx-shared"},
            ],
        )

        summary = await watcher.scan_once()

        self.assertEqual(summary["alive"], 2)
        self.assertEqual(runtime_manager.probe_calls, ["sbx-shared"])
        self.assertEqual(
            [call["session_id"] for call in sessions_repo.cas_calls],
            ["s-1", "s-2"],
        )
        self.assertTrue(
            all(
                call["expected"] == {"sandbox_id": "sbx-shared"}
                for call in sessions_repo.cas_calls
            )
        )

    async def test_alive_fact_realigns_session_agent_and_workspace_owners(self) -> None:
        agent = {"agent_id": "agent-1", "sandbox_id": "sbx-shared"}
        workspace = {
            "assistant_id": "assistant-1",
            "state": "READY",
            "current_sandbox_id": "sbx-shared",
        }
        watcher, sessions_repo, runtime_manager = self._watcher(
            [{"session_id": "s-1", "sandbox_id": "sbx-shared"}],
            agents=[agent],
            workspaces=[workspace],
        )
        platform = watcher._platform

        summary = await watcher.scan_once()

        self.assertEqual(runtime_manager.probe_calls, ["sbx-shared"])
        self.assertEqual(summary["alive"], 3)
        self.assertEqual(
            sessions_repo.cas_calls[0]["updates"]["expires_at"],
            "2099-01-01T00:00:00+00:00",
        )
        self.assertEqual(
            platform._agent_repo.rows[0]["expires_at"],
            "2099-01-01T00:00:00+00:00",
        )
        self.assertEqual(
            platform._assistant_workspace_service.rows[0][
                "current_sandbox_expires_at"
            ],
            "2099-01-01T00:00:00+00:00",
        )

    async def test_agent_owner_without_a_live_session_is_still_probed(self) -> None:
        watcher, _sessions_repo, runtime_manager = self._watcher(
            [],
            probe_results={"sbx-agent": _GONE},
            agents=[{"agent_id": "agent-1", "sandbox_id": "sbx-agent"}],
        )

        summary = await watcher.scan_once()

        self.assertEqual(runtime_manager.probe_calls, ["sbx-agent"])
        self.assertEqual(summary["candidates"], 1)
        self.assertEqual(summary["converged"], 1)

    async def test_assistant_owner_without_a_live_session_is_still_probed(self) -> None:
        watcher, _sessions_repo, runtime_manager = self._watcher(
            [],
            probe_results={"sbx-assistant": _GONE},
            workspaces=[
                {
                    "assistant_id": "assistant-1",
                    "state": "READY",
                    "current_sandbox_id": "sbx-assistant",
                }
            ],
        )

        summary = await watcher.scan_once()

        self.assertEqual(runtime_manager.probe_calls, ["sbx-assistant"])
        self.assertEqual(summary["candidates"], 1)
        self.assertEqual(summary["converged"], 1)

    async def test_no_candidates_is_quiet(self) -> None:
        watcher, _sessions_repo, runtime_manager = self._watcher([])
        with (
            patch(
                "astrabox.core.service.orchestrator.runtime.storage.mounts.reconcile_workspace_mounts",
                new=AsyncMock(return_value={}),
            ),
            patch(
                "astrabox.core.service.orchestrator.expiration_watcher.logger.exception"
            ) as log_exception,
        ):
            self.assertEqual(await watcher.scan_once(), {})
        self.assertEqual(runtime_manager.probe_calls, [])
        log_exception.assert_not_called()


class WatcherShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_translated_dead_sweep_cancellation_does_not_start_idle_work(
        self,
    ) -> None:
        watcher = ExpirationWatcher(
            platform_service=SimpleNamespace(
                _runtime_manager=SimpleNamespace(
                    reconcile_startup_allocations=AsyncMock(return_value={}),
                    reap_abandoned_agent_boxes=AsyncMock(return_value={}),
                    reap_ownerless_sandboxes=AsyncMock(return_value={}),
                    keep_prewarmed_agents_ready=AsyncMock(return_value={}),
                )
            )
        )
        dead_sweep_started = asyncio.Event()

        async def _dead_sweep() -> dict[str, int]:
            dead_sweep_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError as exc:
                raise RuntimeError("database driver translated cancellation") from exc

        idle_sweep = AsyncMock(return_value={})
        with (
            patch.object(watcher, "_sweep_dead_bindings", new=_dead_sweep),
            patch.object(watcher, "_sweep_idle_bindings", new=idle_sweep),
            patch(
                "astrabox.core.service.orchestrator.runtime.storage.mounts.reconcile_workspace_mounts",
                new=AsyncMock(return_value={}),
            ),
            patch.object(
                watcher,
                "_settings",
                return_value=SimpleNamespace(expiration_watcher_interval_seconds=300),
            ),
            patch(
                "astrabox.core.service.orchestrator.expiration_watcher.logger.exception"
            ) as log_exception,
        ):
            task = asyncio.create_task(watcher._loop())
            watcher._task = task
            await dead_sweep_started.wait()

            watcher.quiesce()

            await task
            idle_sweep.assert_not_awaited()
            log_exception.assert_not_called()

    async def test_scan_error_after_quiesce_does_not_enter_poll_sleep(self) -> None:
        watcher = ExpirationWatcher(platform_service=SimpleNamespace())
        scan_started = asyncio.Event()

        async def _scan_with_translated_cancellation() -> dict[str, int]:
            scan_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError as exc:
                raise RuntimeError("database driver translated cancellation") from exc

        with (
            patch.object(watcher, "scan_once", new=_scan_with_translated_cancellation),
            patch.object(
                watcher,
                "_settings",
                return_value=SimpleNamespace(expiration_watcher_interval_seconds=300),
            ),
            patch(
                "astrabox.core.service.orchestrator.expiration_watcher.logger.exception"
            ) as log_exception,
            patch(
                "astrabox.core.service.orchestrator.expiration_watcher.asyncio.sleep",
                new_callable=AsyncMock,
            ) as poll_sleep,
        ):
            task = asyncio.create_task(watcher._loop())
            watcher._task = task
            await scan_started.wait()

            watcher.quiesce()

            await task
            self.assertTrue(task.done())
            log_exception.assert_not_called()
            poll_sleep.assert_not_awaited()


class DeadBindingCandidatesQueryTests(unittest.IsolatedAsyncioTestCase):
    """The candidate query IS the pull layer's scope contract; lock its shape."""

    async def test_query_shape(self) -> None:
        captured: dict[str, Any] = {}

        class _FakeCursor:
            def sort(self, field: str, direction: int) -> "_FakeCursor":
                captured["sort"] = (field, direction)
                return self

            def limit(self, n: int) -> "_FakeCursor":
                captured["limit"] = n
                return self

            def __aiter__(self):
                async def _gen():
                    if False:  # pragma: no cover — empty async iterator
                        yield None

                return _gen()

        class _FakeCollection:
            def find(self, query: dict) -> _FakeCursor:
                captured["query"] = dict(query)
                return _FakeCursor()

        async def _get_collection(_name: str) -> _FakeCollection:
            return _FakeCollection()

        async def _run(_label: str, op, **_kwargs):
            return await op()

        with (
            patch(
                "astrabox.persistence.repository.session_repository.load_astrabox_settings",
                return_value=SimpleNamespace(sessions_collection="sessions"),
            ),
            patch(
                "astrabox.persistence.repository.session_repository.get_async_collection",
                new=_get_collection,
            ),
            patch(
                "astrabox.persistence.repository.session_repository.run_mongo_with_retry",
                new=_run,
            ),
        ):
            from astrabox.persistence.repository.session_repository import SessionRepository

            repo = SessionRepository()
            rows = await repo.list_dead_binding_probe_candidates(
                now_iso="2026-01-01T00:00:00+00:00", limit=50
            )

        self.assertEqual(rows, [])
        query = captured["query"]
        self.assertEqual(query["runtime_unavailable"], {"$ne": True})
        self.assertEqual(query["deleted"], {"$ne": True})
        self.assertEqual(query["sandbox_id"], {"$gt": ""})
        self.assertEqual(query["state"], {"$nin": ["TERMINATED", "DELETED"]})
        self.assertIn(
            {"expires_at": {"$lte": "2026-01-01T00:00:00+00:00"}}, query["$or"]
        )
        self.assertIn({"expires_at": None}, query["$or"])
        self.assertEqual(captured["limit"], 50)

    async def test_agent_query_selects_shared_owner_bindings_without_sessions(
        self,
    ) -> None:
        captured: dict[str, Any] = {}

        class _FakeCursor:
            def sort(self, field: str, direction: int) -> "_FakeCursor":
                captured["sort"] = (field, direction)
                return self

            def limit(self, n: int) -> "_FakeCursor":
                captured["limit"] = n
                return self

            def __aiter__(self):
                async def _gen():
                    if False:  # pragma: no cover - empty async iterator
                        yield None

                return _gen()

        class _FakeCollection:
            def find(self, query: dict[str, Any]) -> _FakeCursor:
                captured["query"] = dict(query)
                return _FakeCursor()

        async def _get_collection(_name: str) -> _FakeCollection:
            return _FakeCollection()

        async def _run(_label: str, op, **_kwargs):
            return await op()

        with (
            patch(
                "astrabox.persistence.repository.agent_repository.load_astrabox_settings",
                return_value=SimpleNamespace(agents_collection="agents"),
            ),
            patch(
                "astrabox.persistence.repository.agent_repository.get_async_collection",
                new=_get_collection,
            ),
            patch(
                "astrabox.persistence.repository.agent_repository.run_mongo_with_retry",
                new=_run,
            ),
        ):
            from astrabox.persistence.repository.agent_repository import AgentRepository

            rows = await AgentRepository().list_dead_binding_probe_candidates(
                now_iso="2026-01-01T00:00:00+00:00",
                limit=25,
            )

        self.assertEqual(rows, [])
        self.assertEqual(captured["query"]["deleted"], {"$ne": True})
        self.assertEqual(captured["query"]["sandbox_id"], {"$gt": ""})
        self.assertIn(
            {"expires_at": {"$lte": "2026-01-01T00:00:00+00:00"}},
            captured["query"]["$or"],
        )
        self.assertEqual(captured["sort"], ("expires_at", 1))
        self.assertEqual(captured["limit"], 25)

    async def test_workspace_query_selects_shared_owner_bindings_without_sessions(
        self,
    ) -> None:
        captured: dict[str, Any] = {}

        class _FakeCursor:
            def sort(self, field: str, direction: int) -> "_FakeCursor":
                captured["sort"] = (field, direction)
                return self

            def limit(self, n: int) -> "_FakeCursor":
                captured["limit"] = n
                return self

            def __aiter__(self):
                async def _gen():
                    if False:  # pragma: no cover - empty async iterator
                        yield None

                return _gen()

        class _FakeCollection:
            def find(self, query: dict[str, Any]) -> _FakeCursor:
                captured["query"] = dict(query)
                return _FakeCursor()

        async def _get_collection(_name: str) -> _FakeCollection:
            return _FakeCollection()

        async def _run(_label: str, op, **_kwargs):
            return await op()

        with (
            patch(
                "astrabox.persistence.repository.assistant_workspace_repository."
                "load_astrabox_settings",
                return_value=SimpleNamespace(
                    assistant_workspace_collection="assistant_workspaces"
                ),
            ),
            patch(
                "astrabox.persistence.repository.assistant_workspace_repository."
                "get_async_collection",
                new=_get_collection,
            ),
            patch(
                "astrabox.persistence.repository.assistant_workspace_repository."
                "run_mongo_with_retry",
                new=_run,
            ),
        ):
            from astrabox.persistence.repository.assistant_workspace_repository import (
                AssistantWorkspaceRepository,
            )

            rows = (
                await AssistantWorkspaceRepository().list_dead_binding_probe_candidates(
                    now_iso="2026-01-01T00:00:00+00:00",
                    limit=25,
                )
            )

        self.assertEqual(rows, [])
        self.assertEqual(captured["query"]["deleted"], {"$ne": True})
        self.assertEqual(
            captured["query"]["current_sandbox_id"],
            {"$gt": ""},
        )
        self.assertIn(
            {
                "current_sandbox_expires_at": {
                    "$lte": "2026-01-01T00:00:00+00:00"
                }
            },
            captured["query"]["$or"],
        )
        self.assertEqual(captured["sort"], ("current_sandbox_expires_at", 1))
        self.assertEqual(captured["limit"], 25)


class CreateSpecDeathCallbackFieldTests(unittest.TestCase):
    def test_defaults_to_none_and_is_settable(self) -> None:
        spec = SandboxCreateSpec(
            session_id="s-1",
            assignment_id="assignment-1",
            resource_limits={"cpu": "4", "memory": "4Gi"},
            resource_requests={"cpu": "200m", "memory": "768Mi"},
        )
        self.assertIsNone(spec.death_callback_url)
        wired = SandboxCreateSpec(
            session_id="s-1",
            assignment_id="assignment-2",
            resource_limits={"cpu": "4", "memory": "4Gi"},
            resource_requests={"cpu": "200m", "memory": "768Mi"},
            death_callback_url="http://host/api/v1/sandbox-callback/session/s-1/g/t",
        )
        self.assertTrue(wired.death_callback_url.endswith("/g/t"))


if __name__ == "__main__":
    unittest.main()


class SuspectMarkTests(unittest.IsolatedAsyncioTestCase):
    """A detached engine stream is a reason to LOOK, never a conclusion.

    Death convergence takes control-plane-confirmed evidence and happens once,
    at one entry. A closed link is not that evidence — it closes on a network
    blip too. But it was not even a reason to ask: the only trigger for the
    dead-binding sweep was a lapsed lease, and an out-of-band kill leaves the
    lease nominally valid for hours, so the session kept a binding to a box
    that was gone, and every turn failed on it.
    """

    def test_a_suspect_binding_is_a_probe_candidate(self) -> None:
        from astrabox.persistence.repository.session_repository import (
            SessionRepository,
        )
        import inspect

        source = inspect.getsource(SessionRepository.list_dead_binding_probe_candidates)
        self.assertIn(
            "sandbox_liveness_suspect_at",
            source,
            "a detached engine stream must give the sweep a reason to probe",
        )
        # Still an OR arm beside the lease, not a replacement: a lapsed lease
        # remains a candidate on its own.
        self.assertIn("expires_at", source)
