"""Session -> MATERIALIZING -> sandbox -> mark_ready -> READY, end to end.

The Assistant's sandbox is a resident workspace that outlives every turn.  A
conversation Session can materialize it through the ordinary lifecycle worker;
an explicit wake creates a hidden Session only because that entry point has no
conversation Session of its own.  Both paths use the same owner claim.

So this file wires the REAL collaborators — ``AssistantService``,
``SessionKernelService.create_session``, ``SessionLifecycleWorker`` (create +
startup commands), ``AssistantWorkspaceService`` — over in-memory repos, and
fakes only the runtime manager (the sandbox backend and the engine, which have
their own suites). No docker, no network, no mongo, no sleeps.
"""

from __future__ import annotations

import asyncio
import shlex
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.seams.sandbox_disposal import SandboxDestruction
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.assistant.assistant_service import (
    AssistantService,
)
from astrabox.core.service.orchestrator.assistant.assistant_workspace_service import (
    AssistantWorkspaceService,
    get_assistant_profile_ready_marker,
)
from astrabox.core.service.orchestrator.engine.base import EngineCapabilityManifest
from astrabox.core.service.orchestrator.session_kernel.service import (
    SessionKernelService,
)
from astrabox.core.service.orchestrator.session_service import SessionService
from astrabox.core.service.orchestrator.session_workspace_plan import (
    SessionWorkspacePlanner,
)
from astrabox.core.service.orchestrator.sandbox_lifecycle import (
    SandboxOwnerConvergence,
)
from astrabox.core.service.orchestrator.runtime_subject import (
    RuntimeSubjectCoordinator,
)


def _run_now(coroutine, *, name: str | None = None):
    """Run a spawned park inline, so a test observes its outcome deterministically.

    The production spawner detaches the commit from the request; a test that
    wants to see the mark cleared should not race a real task to do it.
    """
    import asyncio

    return asyncio.ensure_future(coroutine)

_USER = UserContext(user_id="owner-1")


# ── in-memory world ─────────────────────────────────────────────────────────


class _Journal:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.frames: list[dict[str, Any]] = []
        self._seq = 0

    async def get_command_event(self, session_id: str, *, command_id: str) -> dict[str, Any] | None:
        for event in self.events:
            if (
                str(event.get("session_id")) == session_id
                and str(event.get("event_type")) == "command.accepted"
                and str(event.get("causation_id")) == command_id
            ):
                return event
        return None

    async def append_event(self, doc: dict[str, Any]) -> dict[str, Any]:
        self._seq += 1
        record = {
            **doc,
            "event_seq": self._seq,
            "occurred_at": "2026-01-01T00:00:00+00:00",
        }
        self.events.append(record)
        return record

    async def list_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        event_types: Any = None,
        event_type: str | None = None,
        channel: str | None = None,
        turn_id: str | None = None,
        causation_id: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.events
            if row.get("session_id") == session_id
            and int(row.get("event_seq") or 0) > after_seq
            and (event_types is None or row.get("event_type") in event_types)
            and (event_type is None or row.get("event_type") == event_type)
            and (channel is None or row.get("channel") == channel)
            and (turn_id is None or row.get("turn_id") == turn_id)
            and (causation_id is None or row.get("causation_id") == causation_id)
        ][:limit]

    async def list_frames(
        self,
        session_id: str,
        *,
        scope: str,
        after_seq: int = -1,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.frames
            if row.get("session_id") == session_id
            and row.get("scope") == scope
            and int(row.get("frame_seq") or 0) > after_seq
        ][:limit]

    def error_texts(self) -> list[str]:
        return [
            str((event.get("payload") or {}).get("error_text") or "")
            for event in self.events
            if str(event.get("event_type")) == "session.startup_failed"
        ]


class _Sessions:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = self.rows.get(session_id)
        return dict(row) if row else None

    async def create_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.rows[str(payload["session_id"])] = dict(payload)
        return dict(payload)

    async def update_session(
        self,
        session_id: str,
        updates: dict[str, Any],
        touch_updated_at: bool = True,
    ) -> dict[str, Any]:
        self.rows.setdefault(session_id, {}).update(updates)
        return dict(self.rows[session_id])

    async def compare_and_update_session(
        self, session_id: str, *, expected: dict[str, Any], updates: dict[str, Any]
    ) -> bool:
        row = self.rows.get(session_id)
        if row is None or any(row.get(key) != value for key, value in expected.items()):
            return False
        row.update(updates)
        return True

    async def mark_interaction(self, *args: Any, **kwargs: Any) -> None:
        return None


class _Snapshots:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def apply_channel_update(
        self, session_id: str, *, channel: str, event_seq: int, updates: dict[str, Any]
    ) -> bool:
        self.rows.setdefault(session_id, {}).update(updates)
        return True

    async def get_snapshot(self, session_id: str) -> dict[str, Any]:
        return dict(self.rows.get(session_id) or {})

    async def force_update_fields(
        self,
        session_id: str,
        fields: dict[str, Any],
        *,
        extra_filter: dict[str, Any] | None = None,
    ) -> bool:
        row = self.rows.setdefault(session_id, {})
        if any(row.get(key) != value for key, value in (extra_filter or {}).items()):
            return False
        row.update(fields)
        return True


class _WorkspaceRepo:
    """The assistant_workspace row, with the real compare-and-set semantics."""

    def __init__(self) -> None:
        self.row: dict[str, Any] | None = None

    async def get_workspace(
        self, user_id: str, assistant_id: str
    ) -> dict[str, Any] | None:
        return dict(self.row) if self.row else None

    async def has_assistant_workspace(self, assistant_id: str) -> bool:
        return self.row is not None

    async def list_workspaces_by_sandbox_id(
        self, sandbox_id: str
    ) -> list[dict[str, Any]]:
        if self.row is None or self.row.get("current_sandbox_id") != sandbox_id:
            return []
        return [dict(self.row)]

    async def materialize_workspace(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.row = dict(payload)
        return dict(self.row)

    async def update_workspace(
        self, user_id: str, assistant_id: str, updates: dict[str, Any]
    ) -> bool:
        if self.row is None:
            return False
        self.row.update(updates)
        return True

    async def compare_and_update_workspace(
        self,
        user_id: str,
        assistant_id: str,
        *,
        expected: dict[str, Any],
        updates: dict[str, Any],
    ) -> bool:
        row = self.row
        if row is None:
            return False
        # Mongo semantics: a `None` predicate matches null and missing alike.
        if any(row.get(key) != value for key, value in expected.items()):
            return False
        row.update(updates)
        return True

    async def mark_ready(
        self,
        user_id: str,
        assistant_id: str,
        *,
        provisioning_session_id: str,
        provisioning_sandbox_generation: str | None,
        sandbox_id: str,
        expires_at: str | None,
        runtime_identity: dict[str, Any] | None,
        profile_marker_key: str,
        profile_marker: dict[str, Any],
    ) -> bool:
        row = self.row
        if (
            row is None
            or str(row.get("state") or "") != "MATERIALIZING"
            or str(row.get("provisioning_session_id") or "") != provisioning_session_id
            or row.get("provisioning_sandbox_generation") != provisioning_sandbox_generation
        ):
            return False
        row.update(
            {
                "state": "READY",
                "current_sandbox_id": sandbox_id,
                "current_sandbox_expires_at": expires_at,
                "runtime_identity": runtime_identity,
                "provisioning_session_id": None,
                "provisioning_sandbox_generation": None,
                "last_error": None,
            }
        )
        row.setdefault("assistant_profiles", {})[profile_marker_key] = dict(
            profile_marker
        )
        return True

    async def mark_post_commit_failure(self, *args: Any, **kwargs: Any) -> bool:
        return True


class _CatalogRepo:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def create_assistant(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.rows[str(payload["assistant_id"])] = dict(payload)
        return dict(payload)

    async def get_assistant(self, assistant_id: str) -> dict[str, Any] | None:
        row = self.rows.get(assistant_id)
        return dict(row) if row else None


class _Template:
    name = "env-1"
    sandbox_backend = "open_sandbox"
    model_config: dict[str, Any] = {}


class _AgentConfig:
    async def resolve_session_harness(self, session: Any) -> Any:
        return _Template()

    async def get_environment(self, name: str) -> dict[str, Any]:
        return {"name": name, "enabled": True, "engine_kind": "assistant"}


class _StartedRuntime:
    def __init__(self, sandbox_id: str) -> None:
        self.sandbox_id = sandbox_id
        self.engine_kind = "assistant"
        self.runtime_identity = {
            "sandbox_id": sandbox_id,
            "linux_user": "asst_owner",
            "home_dir": "/home/conversations/owner-1/a",
            "config_dir": "/home/conversations/owner-1/a/.hermes",
            "workspace_dir": "/home/conversations/owner-1/a/workspace",
            "workspace_source_dir": "/home/conversations/owner-1/a/workspace",
            "sandbox_tenancy": "agent",
        }
        self.terminal_cwd = "/home/conversations/owner-1/a/workspace"
        self.engine_session_key = None
        self.engine_client = SimpleNamespace(
            get_server_info=AsyncMock(return_value={})
        )
        self.engine_manifest = EngineCapabilityManifest(
            engine_kind="assistant",
            supports_server_info=True,
        )
        self.conversation_bound = True


class _Runtime:
    """Everything below the seam: the backend created a box and the engine started."""

    def __init__(self) -> None:
        self.created: list[str] = []
        self.terminated: list[dict[str, Any]] = []
        self.destroyed: list[str] = []
        self.create_error: Exception | None = None
        self.after_create: Any | None = None
        self.kill_confirms = True
        self.gone: set[str] = set()
        self.runtimes: dict[str, _StartedRuntime] = {}
        self.native_save_fails = False
        self.sandbox_commands: list[str] = []
        self.save_operations: list[str] = []
        self.stopped_programs: set[str] = set()

    # workspace planning is real
    def plan_assistant_runtime_start(self, **kwargs: Any) -> Any:
        return SessionWorkspacePlanner(
            base_cwd="/home/user"
        ).plan_assistant_runtime_start(**kwargs)

    def plan_assistant_runtime_attach(self, **kwargs: Any) -> Any:
        return SessionWorkspacePlanner(
            base_cwd="/home/user"
        ).plan_assistant_runtime_attach(**kwargs)

    async def create_runtime(
        self, session_id: str, template: Any, **kwargs: Any
    ) -> Any:
        if self.create_error is not None:
            raise self.create_error
        self.created.append(session_id)
        runtime = _StartedRuntime(f"sbx-{len(self.created)}")
        self.runtimes[session_id] = runtime
        if self.after_create is not None:
            await self.after_create(runtime)
        return runtime

    async def ensure_runtime(self, *args: Any, **kwargs: Any) -> Any:
        session_id = str(args[0])
        sandbox_id = str(kwargs.get("sandbox_id") or "").strip()
        assert sandbox_id
        runtime = _StartedRuntime(sandbox_id)
        self.runtimes[session_id] = runtime
        return runtime

    async def get_sandbox_lifecycle_probe(self, sandbox_id: str) -> Any:
        if sandbox_id in self.gone:
            return SimpleNamespace(
                probe_status="NOT_FOUND", sandbox_state="", error_text=None
            )
        return SimpleNamespace(
            probe_status="OK", sandbox_state="running", error_text=None
        )

    async def connect_sandbox_only(self, sandbox_id: str) -> Any:
        assert sandbox_id not in self.gone

        async def run(command: str) -> Any:
            self.sandbox_commands.append(command)
            argv = shlex.split(command)
            programs = {"astrabox-hermes-state-mirror", "astrabox-hermes"}
            if argv[:2] == ["supervisorctl", "stop"] and argv[2] in programs:
                self.stopped_programs.add(argv[2])
                self.save_operations.append(f"stop:{argv[2]}")
                return SimpleNamespace(exit_code=0, stdout="", error=None)
            if argv[:2] == ["bash", "-lc"]:
                program = shlex.split(argv[2])[3].rstrip(";")
                assert program in programs
                self.save_operations.append(f"status:{program}")
                state = "STOPPED" if program in self.stopped_programs else "RUNNING"
                return SimpleNamespace(exit_code=0, stdout=f"{program} {state}", error=None)
            if argv[:1] == ["runuser"] and "astrabox-hermes-state-save" in argv:
                assert self.stopped_programs == programs
                failed = self.native_save_fails
                self.save_operations.append("save_failed" if failed else "save_complete")
                return SimpleNamespace(
                    exit_code=1 if failed else 0,
                    stdout="" if failed else "HERMES_STATE_SAVE_COMPLETE snapshot_id=wake-snapshot",
                    error=None,
                )
            raise AssertionError(f"unexpected sandbox command: {command}")

        async def close() -> None:
            self.save_operations.append("close")

        return SimpleNamespace(commands=SimpleNamespace(run=run), close=close)

    async def destroy_sandbox_by_id(self, sandbox_id: str) -> SandboxDestruction:
        self.save_operations.append(f"destroy:{sandbox_id}")
        self.destroyed.append(sandbox_id)
        if not self.kill_confirms:
            return SandboxDestruction.unconfirmed(
                sandbox_id, detail="the confirming probe could not be answered"
            )
        self.gone.add(sandbox_id)
        return SandboxDestruction.confirmed_gone(
            sandbox_id, detail="the fake control plane confirmed destruction"
        )

    async def renew_sandbox_by_id(
        self, sandbox_id: str, *, ttl_seconds: int
    ) -> None:
        return None

    async def get_sandbox_expires_at(self, sandbox_id: str) -> None:
        return None

    @staticmethod
    def _is_terminal_sandbox_lifecycle_probe(probe: Any) -> bool:
        return str(getattr(probe, "probe_status", "") or "") == "NOT_FOUND"

    async def terminate_runtime(
        self, runtime_key: str, *, fallback_sandbox_id: str | None = None
    ) -> SandboxDestruction:
        self.terminated.append(
            {"runtime_key": runtime_key, "fallback_sandbox_id": fallback_sandbox_id}
        )
        target = str(fallback_sandbox_id or "").strip()
        if not target:
            # Nothing was named and this fake tracks no in-process boxes, so
            # this destroy had nothing to act on. That is not a success — it
            # only says the call had no subject.
            return SandboxDestruction.nothing_named(
                detail=f"nothing named a sandbox for {runtime_key}"
            )
        if self.kill_confirms:
            return SandboxDestruction.confirmed_gone(
                target, detail="the control plane no longer knows it"
            )
        return SandboxDestruction.unconfirmed(
            target, detail="the confirming probe could not be answered"
        )

    async def cleanup_startup_allocation(
        self, session_id: str, *, fallback_sandbox_id: str | None = None
    ) -> Any:
        destruction = await self.terminate_runtime(
            session_id,
            fallback_sandbox_id=fallback_sandbox_id,
        )
        return SimpleNamespace(
            destruction=destruction,
            leaked_sandbox_id=destruction.leaked_sandbox_id,
        )

    def forget_published_startup_allocation(
        self, session_id: str, *, sandbox_id: str
    ) -> None:
        return None

    async def _send_sandbox_http_json(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"initialization": {}}

    def resolve_template_model_name(self, template: Any) -> str:
        return "model-1"

    @staticmethod
    def normalize_permission_mode(mode: str | None) -> str:
        return str(mode or "default")

    def get_runtime(self, *args: Any, **kwargs: Any) -> Any:
        return self.runtimes.get(str(args[0]))

    def resolve_session_terminal_cwd(self, *args: Any, **kwargs: Any) -> str:
        return "/home/user"


class _Lifecycle:
    def __init__(self, workspace_service: AssistantWorkspaceService) -> None:
        self._workspace_service = workspace_service
        self.calls: list[dict[str, Any]] = []

    async def converge_dead_sandbox_owners(
        self,
        sandbox_id: str,
        *,
        last_error: str,
        reason: str,
        preserve_planned_teardowns: bool = False,
    ) -> SandboxOwnerConvergence:
        self.calls.append(
            {
                "sandbox_id": sandbox_id,
                "last_error": last_error,
                "reason": reason,
                "preserve_planned_teardowns": preserve_planned_teardowns,
            }
        )
        converged: list[str] = []
        for workspace in await self._workspace_service.list_workspaces_by_sandbox_id(
            sandbox_id
        ):
            if await self._workspace_service.converge_dead_sandbox(
                workspace=workspace,
                sandbox_id=sandbox_id,
                last_error=last_error,
            ):
                converged.append(str(workspace["assistant_id"]))
        return SandboxOwnerConvergence(
            sandbox_id=sandbox_id,
            converged_assistant_workspaces=tuple(converged),
        )


class _World:
    """One assistant, its workspace and the kernel that boots it."""

    def __init__(self) -> None:
        self.sessions = _Sessions()
        self.journal = _Journal()
        self.snapshots = _Snapshots()
        self.runtime = _Runtime()
        self.workspace_repo = _WorkspaceRepo()
        self.catalog = _CatalogRepo()
        self.tasks: list[asyncio.Task[Any]] = []

        agent_config = _AgentConfig()
        workspace_service = AssistantWorkspaceService(
            workspace_repo=self.workspace_repo,  # type: ignore[arg-type]
        )
        self.lifecycle = _Lifecycle(workspace_service)
        broker = AsyncMock()
        session_service = SessionService(
            sessions_repo=self.sessions,  # type: ignore[arg-type]
            messages_repo=AsyncMock(),
            agent_config=agent_config,  # type: ignore[arg-type]
            runtime_manager=self.runtime,  # type: ignore[arg-type]
            broker=broker,
            ttl_seconds=3600,
            spawn_background_task=self._spawn,
            assistant_workspace_service=workspace_service,
            vault_service=AsyncMock(),
        )
        runtime_subjects = RuntimeSubjectCoordinator(
            runtime_manager=self.runtime,
            sessions_repo=self.sessions,
            assistant_workspace_service=workspace_service,
            assistant_lifecycle_getter=lambda: self.service,
            ready_timeout_seconds=2,
            ready_poll_seconds=0.001,
        )
        # The real constructor owns child-run views, task tracking and worker
        # state. A hand-written subset silently omits newly required seams.
        kernel = SessionKernelService(
            sessions_repo=self.sessions,
            session_service=session_service,
            runtime_manager=self.runtime,
            broker=broker,
            session_events_repo=self.journal,
            session_snapshots_repo=self.snapshots,
            interaction_snapshots_repo=AsyncMock(
                get_active_interaction=AsyncMock(return_value=None),
            ),
            message_view=AsyncMock(),
            artifacts_repo=AsyncMock(),
            transcript_entries_repo=AsyncMock(),
            agent_repo=AsyncMock(),
            assistant_workspace_service=workspace_service,
            runtime_subjects=runtime_subjects,
            terminal_service=AsyncMock(),
            turn_service=AsyncMock(pending_input_rows=AsyncMock(return_value=[])),
            spawn_background_task=self._spawn,
            must_get_owned_session=self._owned_session,
        )

        self.workspace_service = workspace_service
        self.kernel = kernel
        self.service = AssistantService(
            agent_config=agent_config,
            session_kernel=kernel,
            runtime_manager=self.runtime,
            catalog_repo=self.catalog,  # type: ignore[arg-type]
            workspace_service=workspace_service,
            sessions_repo=self.sessions,  # type: ignore[arg-type]
            sandbox_lifecycle_service=self.lifecycle,
            spawn_background_task=_run_now,
        )

    def _spawn(self, coro: Any, *, name: str | None = None) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task

    async def _owned_session(self, user: Any, session_id: str) -> dict[str, Any]:
        row = await self.sessions.get_session(session_id)
        assert row is not None
        return row

    async def settle(self) -> None:
        """Let every spawned worker finish, then surface any crash."""
        while self.tasks:
            pending = list(self.tasks)
            self.tasks.clear()
            await asyncio.gather(*pending)

    async def create_assistant(self) -> str:
        created = await self.service.create_assistant(
            _USER,
            {
                "display_name": "Jarvis",
                "environment_name": "env-1",
                "engine_kind": "assistant",
            },
        )
        return str(created["assistant_id"])

    def workspace(self) -> dict[str, Any]:
        assert self.workspace_repo.row is not None
        return dict(self.workspace_repo.row)

    def bootstrap_sessions(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.sessions.rows.values()
            if str(row.get("owner_type") or "") == "assistant_workspace"
        ]


@pytest.fixture(autouse=True)
def _callback_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    # The startup path builds the sandbox death-callback URL from this; the
    # settings loader is uncached, so setting it here is enough.
    monkeypatch.setenv("ASTRABOX_MCP_PROXY_BASE_URL", "http://astrabox.test:8080")


# ── the chain ───────────────────────────────────────────────────────────────


async def test_wake_drives_the_hidden_bootstrap_all_the_way_to_ready() -> None:
    world = _World()
    assistant_id = await world.create_assistant()

    async def _assert_materializer_generation(runtime: _StartedRuntime) -> None:
        (bootstrap,) = world.bootstrap_sessions()
        workspace = world.workspace()
        assert workspace["state"] == "MATERIALIZING"
        assert workspace["provisioning_session_id"] == bootstrap["session_id"]
        assert bootstrap["sandbox_generation"]
        assert workspace["provisioning_sandbox_generation"] == bootstrap["sandbox_generation"]

    world.runtime.after_create = _assert_materializer_generation
    woken = await world.service.wake_workspace(_USER, assistant_id)
    assert woken["state"] == "MATERIALIZING"
    provisioning_session_id = str(woken["provisioning_session_id"])

    await world.settle()

    # The hidden bootstrap session built a sandbox and reached READY…
    (bootstrap,) = world.bootstrap_sessions()
    assert bootstrap["session_id"] == provisioning_session_id
    assert bootstrap["hidden"] is True
    assert bootstrap["session_kind"] == "assistant_chat"
    assert bootstrap["state"] == "READY", {
        "bootstrap": bootstrap,
        "startup_errors": world.journal.error_texts(),
    }
    assert bootstrap["sandbox_id"] == "sbx-1"
    assert world.runtime.created == [provisioning_session_id]
    # …and published that sandbox onto the workspace, which is the only thing
    # that makes the resident workspace usable.
    workspace = world.workspace()
    assert workspace["state"] == "READY"
    assert workspace["current_sandbox_id"] == "sbx-1"
    assert workspace["provisioning_sandbox_generation"] is None

    # A conversation now attaches to that resident box instead of building one.
    conversation = await world.service.start_conversation(_USER, assistant_id)
    assert conversation["session_id"] != provisioning_session_id
    await world.settle()
    assert world.runtime.created == [provisioning_session_id]


async def test_conversation_creation_materializes_its_runtime_subject_in_background() -> None:
    world = _World()
    assistant_id = await world.create_assistant()

    conversation = await world.service.start_conversation(_USER, assistant_id)

    assert conversation["state"] == "CREATING"
    await world.settle()
    workspace = world.workspace()
    assert workspace["state"] == "READY"
    assert workspace["current_sandbox_id"] == "sbx-1"
    ready = await world.sessions.get_session(str(conversation["session_id"]))
    assert ready is not None
    assert ready["state"] == "READY"
    assert ready["sandbox_id"] == "sbx-1"
    assert ready.get("runtime_identity") is None
    assert workspace["runtime_identity"]["sandbox_id"] == "sbx-1"
    assert get_assistant_profile_ready_marker(
        workspace,
        user_id=_USER.user_id,
        assistant_id=assistant_id,
        sandbox_id="sbx-1",
    ) is not None
    assert world.bootstrap_sessions() == []
    assert world.runtime.created == [conversation["session_id"]]


async def test_concurrent_conversations_share_one_materialization() -> None:
    world = _World()
    assistant_id = await world.create_assistant()

    first, second = await asyncio.gather(
        world.service.start_conversation(_USER, assistant_id),
        world.service.start_conversation(_USER, assistant_id),
    )
    await world.settle()

    assert world.bootstrap_sessions() == []
    assert len(world.runtime.created) == 1
    assert world.runtime.created[0] in {
        str(first["session_id"]),
        str(second["session_id"]),
    }
    for conversation in (first, second):
        ready = await world.sessions.get_session(str(conversation["session_id"]))
        assert ready is not None
        assert ready["state"] == "READY"
        assert ready["sandbox_id"] == "sbx-1"


async def test_materializer_that_loses_its_claim_cleans_up_instead_of_publishing_ready() -> None:
    world = _World()
    assistant_id = await world.create_assistant()

    async def _steal_claim(runtime: _StartedRuntime) -> None:
        assert runtime.sandbox_id == "sbx-1"
        assert world.workspace_repo.row is not None
        world.workspace_repo.row.update(
            {
                "state": "MATERIALIZING",
                "provisioning_session_id": "new-materializer",
                "current_sandbox_id": None,
            }
        )

    world.runtime.after_create = _steal_claim
    conversation = await world.service.start_conversation(_USER, assistant_id)
    await world.settle()

    failed = await world.sessions.get_session(str(conversation["session_id"]))
    assert failed is not None
    assert failed["state"] == "TERMINATED"
    assert world.runtime.terminated[-1] == {
        "runtime_key": conversation["session_id"],
        "fallback_sandbox_id": "sbx-1",
    }
    workspace = world.workspace()
    assert workspace["state"] == "MATERIALIZING"
    assert workspace["provisioning_session_id"] == "new-materializer"
    assert workspace["current_sandbox_id"] is None


async def test_a_second_wake_on_a_ready_workspace_builds_nothing() -> None:
    world = _World()
    assistant_id = await world.create_assistant()
    await world.service.wake_workspace(_USER, assistant_id)
    await world.settle()

    again = await world.service.wake_workspace(_USER, assistant_id)

    assert again["state"] == "READY"
    assert again["current_sandbox_id"] == "sbx-1"
    assert len(world.bootstrap_sessions()) == 1


async def test_wake_converges_a_ready_workspace_whose_box_is_gone() -> None:
    world = _World()
    assistant_id = await world.create_assistant()
    await world.service.wake_workspace(_USER, assistant_id)
    await world.settle()
    assert world.workspace()["current_sandbox_id"] == "sbx-1"
    world.runtime.gone.add("sbx-1")

    woken = await world.service.wake_workspace(_USER, assistant_id)

    assert woken["state"] == "MATERIALIZING"
    assert world.lifecycle.calls == [
        {
            "sandbox_id": "sbx-1",
            "last_error": "sandbox terminated",
            "reason": "assistant_workspace_wake:NOT_FOUND",
            "preserve_planned_teardowns": False,
        }
    ]
    recovering = world.workspace()
    assert recovering["state"] == "MATERIALIZING"
    assert recovering["current_sandbox_id"] is None
    assert recovering["runtime_identity"] is None
    assert recovering["assistant_profiles"] == {}

    await world.settle()
    assert world.workspace()["state"] == "READY"
    assert world.workspace()["current_sandbox_id"] == "sbx-2"


async def test_start_conversation_triggers_recovery_instead_of_reusing_a_dead_box() -> None:
    world = _World()
    assistant_id = await world.create_assistant()
    await world.service.wake_workspace(_USER, assistant_id)
    await world.settle()
    world.runtime.gone.add("sbx-1")

    conversation = await world.service.start_conversation(_USER, assistant_id)

    assert conversation["state"] == "CREATING"
    await world.settle()
    assert world.lifecycle.calls[0]["sandbox_id"] == "sbx-1"
    assert world.workspace()["current_sandbox_id"] == "sbx-2"
    ready = await world.sessions.get_session(str(conversation["session_id"]))
    assert ready is not None
    assert ready["state"] == "READY"
    assert ready["sandbox_id"] == "sbx-2"
    assert len(world.bootstrap_sessions()) == 1
    assert world.runtime.created[-1] == conversation["session_id"]


async def test_hibernated_workspace_rebuilds_from_storage_on_a_new_box() -> None:
    world = _World()
    assistant_id = await world.create_assistant()
    await world.service.wake_workspace(_USER, assistant_id)
    await world.settle()
    world.runtime.native_save_fails = True
    with pytest.raises(APIError) as failed_save:
        await world.service.hibernate_workspace(_USER, assistant_id)
    assert failed_save.value.code == "ASSISTANT_WORKSPACE_CONVERGENCE_FAILED"
    assert failed_save.value.status_code == 503
    assert failed_save.value.data == {
        "sandbox_id": "sbx-1",
        "retryable": True,
        "phase": "hermes_native_state_save",
    }
    assert world.workspace()["state"] == "HIBERNATING"
    assert world.workspace()["current_sandbox_id"] == "sbx-1"
    assert world.runtime.destroyed == []
    assert "sbx-1" not in world.runtime.gone

    world.runtime.native_save_fails = False
    hibernated = await world.service.hibernate_workspace(_USER, assistant_id)

    conversation = await world.service.start_conversation(_USER, assistant_id)
    await world.settle()

    assert hibernated["hibernated"] is True
    assert hibernated["released"] is True
    assert hibernated["previous_sandbox_id"] == "sbx-1"
    assert hibernated["sandbox_id"] is None
    stop_and_verify = [
        "stop:astrabox-hermes-state-mirror",
        "status:astrabox-hermes-state-mirror",
        "stop:astrabox-hermes",
        "status:astrabox-hermes",
    ]
    assert world.runtime.save_operations == [
        *stop_and_verify, "save_failed", "close",
        *stop_and_verify, "save_complete", "close", "destroy:sbx-1",
    ]
    save_commands = [
        shlex.split(command) for command in world.runtime.sandbox_commands
        if command.startswith("runuser ")
    ]
    assert len(save_commands) == 2
    assert save_commands[0] == save_commands[1]
    save = save_commands[1]
    assert save[:10] == [
        "runuser", "-u", "asst_owner", "--", "env",
        "HOME=/home/conversations/owner-1/a", "USER=asst_owner", "LOGNAME=asst_owner",
        "bash", "--noprofile",
    ]
    assert save[-4:] == [
        "astrabox-hermes-state-save",
        "/home/conversations/owner-1/a/.hermes/astrabox-hermes.env",
        "asst_owner", "/home/conversations/owner-1/a",
    ]
    assert '/opt/astrabox/hermes/hermes_state_mirror.py save --profile-env "$1"' in save[12]
    assert "sbx-1" in world.runtime.gone
    assert world.lifecycle.calls == []
    assert world.workspace()["current_sandbox_id"] == "sbx-2"
    ready = await world.sessions.get_session(str(conversation["session_id"]))
    assert ready is not None
    assert ready["state"] == "READY"
    assert ready["sandbox_id"] == "sbx-2"


# ── bootstrap failures that wedge the workspace without a signal ────────────


async def test_conversation_materialization_failure_releases_the_owner_claim() -> None:
    world = _World()
    assistant_id = await world.create_assistant()
    world.runtime.create_error = APIError(
        code="AGENT_RUNTIME_ERROR",
        message="engine=assistant runtime failed: boom",
        status_code=502,
    )

    conversation = await world.service.start_conversation(_USER, assistant_id)
    await world.settle()

    assert world.bootstrap_sessions() == []
    failed = await world.sessions.get_session(str(conversation["session_id"]))
    assert failed is not None
    assert failed["state"] == "TERMINATED"
    workspace = world.workspace()
    assert workspace["state"] == "HIBERNATING"
    assert workspace["provisioning_session_id"] is None

    world.runtime.create_error = None
    retry = await world.service.start_conversation(_USER, assistant_id)
    await world.settle()

    assert world.bootstrap_sessions() == []
    ready = await world.sessions.get_session(str(retry["session_id"]))
    assert ready is not None
    assert ready["state"] == "READY"
    assert ready["sandbox_id"] == "sbx-1"


async def test_a_bootstrap_failure_leaves_the_workspace_wakeable_not_wedged() -> None:
    """A bootstrap that dies before mark_ready must publish that on the row.

    ``mark_ready`` is the only writer of ``current_sandbox_id``, so before it
    runs the pointer is empty and the post-commit convergence has nothing to
    fence. If that is the whole failure handling, the row keeps asserting
    MATERIALIZING with a provisioning session that is now TERMINATED — and the
    assistant is dead for ever, silently, because the error only ever reaches
    the session row and the journal.
    """
    world = _World()
    assistant_id = await world.create_assistant()
    world.runtime.create_error = APIError(
        code="AGENT_RUNTIME_ERROR",
        message="engine=assistant runtime failed: boom",
        status_code=502,
    )

    await world.service.wake_workspace(_USER, assistant_id)
    await world.settle()

    (bootstrap,) = world.bootstrap_sessions()
    assert bootstrap["state"] == "TERMINATED"
    assert bootstrap["sandbox_id"] is None
    # The failure is journalled, never logged — which is why nobody saw this.
    assert any("boom" in text for text in world.journal.error_texts())
    # The workspace says what happened instead of claiming to be provisioning.
    workspace = world.workspace()
    assert workspace["state"] == "HIBERNATING"
    assert workspace["current_sandbox_id"] is None
    assert workspace["provisioning_session_id"] is None
    assert "runtime_start_failed" in str(workspace["last_error"])

    # …and the next wake rebuilds, rather than reading a stale claim.
    world.runtime.create_error = None
    again = await world.service.wake_workspace(_USER, assistant_id)
    assert again["state"] == "MATERIALIZING"
    await world.settle()
    assert world.workspace()["state"] == "READY"
    assert world.workspace()["current_sandbox_id"] == "sbx-1"


async def test_a_bootstrap_box_that_survived_its_cleanup_blocks_hibernation() -> None:
    """The blocker this batch closed, end to end.

    A bootstrap fails between the sandbox's create and ``mark_ready``. The
    pointer is empty — it is written by ``mark_ready`` and by nothing else —
    and the box is running. Publishing HIBERNATING there declares an absence
    that is not true AND clears ``provisioning_session_id``, the last thing
    naming the bootstrap that built it: the box keeps running, keeps costing,
    and nothing in the system can say its name.

    So an unconfirmed destruction is ADOPTED onto the pointer instead, which
    puts the workspace in the one state wake already knows how to retry.
    """
    world = _World()
    assistant_id = await world.create_assistant()
    world.runtime.create_error = APIError(
        code="AGENT_RUNTIME_ERROR",
        message="engine=assistant runtime failed: boom",
        status_code=502,
        data={"sandbox_id": "sbx-orphan", "leaked_sandbox_id": "sbx-orphan"},
    )
    world.runtime.kill_confirms = False  # the destroy runs and proves nothing

    await world.service.wake_workspace(_USER, assistant_id)
    await world.settle()

    workspace = world.workspace()
    assert workspace["state"] != "HIBERNATING", (
        "a running box must never be published as an absence"
    )
    assert workspace["state"] == "RECOVERY_REQUIRED"
    assert workspace["current_sandbox_id"] == "sbx-orphan", (
        "the orphan's id takes the pointer, which is the one place a later wake looks"
    )
    assert workspace["post_commit_cleanup_pending"] is True

    # …and the next wake IS the retry: once the destruction is confirmed the
    # pointer is released and the rebuild goes ahead.
    world.runtime.create_error = None
    world.runtime.kill_confirms = True
    again = await world.service.wake_workspace(_USER, assistant_id)
    assert again["state"] == "MATERIALIZING"
    assert world.runtime.destroyed == ["sbx-orphan"]
    assert any(call["fallback_sandbox_id"] == "sbx-orphan" for call in world.runtime.terminated), (
        "wake retries the destruction of exactly the box the pointer named"
    )
    await world.settle()
    assert world.workspace()["state"] == "READY"


# ── the owner died without writing anything (process crash) ─────────────────


async def _wedged_workspace(world: _World, *, bootstrap_state: str) -> str:
    """A workspace left MATERIALIZING by a bootstrap that never settled."""
    assistant_id = await world.create_assistant()
    world.runtime.create_error = APIError(
        code="AGENT_RUNTIME_ERROR", message="boom", status_code=502
    )
    woken = await world.service.wake_workspace(_USER, assistant_id)
    await world.settle()
    provisioning_session_id = str(woken["provisioning_session_id"])
    world.runtime.create_error = None
    # Rewind the row to what a crash mid-startup leaves behind: the claim is
    # still there because nothing ran the failure tail.
    assert world.workspace_repo.row is not None
    world.workspace_repo.row.update(
        {
            "state": "MATERIALIZING",
            "current_sandbox_id": None,
            "provisioning_session_id": provisioning_session_id,
            "last_error": None,
        }
    )
    world.sessions.rows[provisioning_session_id]["state"] = bootstrap_state
    return assistant_id


async def test_wake_redrives_a_materialization_whose_bootstrap_session_died() -> None:
    world = _World()
    assistant_id = await _wedged_workspace(world, bootstrap_state="TERMINATED")

    woken = await world.service.wake_workspace(_USER, assistant_id)

    assert woken["state"] == "MATERIALIZING"
    await world.settle()
    assert world.workspace()["state"] == "READY"
    assert world.workspace()["current_sandbox_id"] == "sbx-1"
    assert len(world.bootstrap_sessions()) == 2


async def test_wake_redrives_when_the_bootstrap_session_is_gone_entirely() -> None:
    world = _World()
    assistant_id = await _wedged_workspace(world, bootstrap_state="TERMINATED")
    assert world.workspace_repo.row is not None
    world.sessions.rows.pop(str(world.workspace_repo.row["provisioning_session_id"]))
    world.workspace_repo.row["updated_at"] = "2000-01-01T00:00:00+00:00"

    await world.service.wake_workspace(_USER, assistant_id)
    await world.settle()

    assert world.workspace()["state"] == "READY"


async def test_wake_leaves_a_bootstrap_that_is_still_running_alone() -> None:
    """The liveness check is not a timeout: a bootstrap still in flight owns
    the workspace, and a second wake must not build a competing sandbox."""
    world = _World()
    assistant_id = await _wedged_workspace(world, bootstrap_state="CREATING")

    woken = await world.service.wake_workspace(_USER, assistant_id)

    assert woken["state"] == "MATERIALIZING"
    assert "retryable" not in woken
    assert len(world.bootstrap_sessions()) == 1
    assert world.workspace()["state"] == "MATERIALIZING"


async def test_wake_will_not_rebuild_over_a_sandbox_the_dead_bootstrap_left() -> None:
    """The dead session's row is the last record of that sandbox's id.

    The workspace pointer is empty here (mark_ready never ran), so rebuilding
    without a confirmed kill would strand a running box nothing can name.
    """
    world = _World()
    assistant_id = await _wedged_workspace(world, bootstrap_state="TERMINATED")
    assert world.workspace_repo.row is not None
    provisioning_session_id = str(world.workspace_repo.row["provisioning_session_id"])
    world.sessions.rows[provisioning_session_id]["sandbox_id"] = "sbx-leaked"
    world.runtime.kill_confirms = False

    woken = await world.service.wake_workspace(_USER, assistant_id)

    assert woken["state"] == "RECOVERY_REQUIRED"
    assert woken["retryable"] is True
    assert woken["recovery_pending_sandbox_id"] == "sbx-leaked"
    assert len(world.bootstrap_sessions()) == 1
    assert world.workspace()["provisioning_session_id"] == provisioning_session_id

    # Once the kill IS confirmed, the same call rebuilds.
    world.runtime.kill_confirms = True
    again = await world.service.wake_workspace(_USER, assistant_id)
    assert again["state"] == "MATERIALIZING"
    assert world.runtime.destroyed[-1] == "sbx-leaked"
    assert world.workspace()["current_sandbox_id"] is None
    await world.settle()
    assert world.workspace()["state"] == "READY"
