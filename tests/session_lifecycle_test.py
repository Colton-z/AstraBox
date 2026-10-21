"""Races on a conversation's lifecycle through the real store, and who a create key answers.

Each case drives the lifecycle code against the real store with the sandbox
side faked: the runtime manager and the runtime subject stand in for the
provider. Reader hosts are represented by a real :class:`SessionFeed`, the
listener a host starts a session's reader from, so a case sees a notice only
if the statement that sent it committed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.runtime_subject import (
    RuntimeStartupCleanup,
    RuntimeStartupTarget,
)
from astrabox.core.service.orchestrator.session_lifecycle import startup as startup_module
from astrabox.core.service.orchestrator.session_lifecycle.service import (
    SessionLifecycleService,
    _idempotent_session_id,
)
from astrabox.core.service.orchestrator.session_runtime.feed import SessionFeed
from astrabox.core.service.orchestrator.session_service import SessionService
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.providers import register_builtin_providers
from tests._agent_ledger import create_agent


@pytest.fixture(autouse=True)
def _environment_membership(environment_member_accounts: set[str]) -> None:
    environment_member_accounts.update({'u1'})

pytestmark = pytest.mark.usefixtures("database")

_USER = UserContext(user_id="u1")
_REF = {"kind": "agent", "agent_id": "a1"}
_TEMPLATE = SimpleNamespace(
    name="Agent",
    engine_kind="claude_code",
    sandbox_backend="test-backend",
    credential_vault_ids=[],
)
_MANIFEST = {"engine_kind": "claude_code", "permission_modes": ["default", "plan", "bypassPermissions"]}


class _AgentConfig:
    async def resolve_session_harness(self, _session: dict[str, Any]) -> Any:
        return _TEMPLATE


class _Runtimes:
    """The runtime manager's sandbox side: a box is a name, a runtime a record of the ask."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def resolve_template_model_name(self, _template: Any) -> str:
        return "model-1"

    async def create_runtime(
        self, session_id: str, _template: Any, *, assignment_id: str, startup_guard: Any,
        workspace_plan: Any, **_kwargs: Any,
    ) -> Any:
        await startup_guard()
        self.created.append(
            {"session_id": session_id, "resume": workspace_plan.resume_engine_session_key}
        )
        return SimpleNamespace(
            sandbox_id=f"box-{len(self.created)}",
            engine_session_key=workspace_plan.resume_engine_session_key or "native-1",
            terminal_cwd="/workspace",
            runtime_identity=None,
        )

    async def get_sandbox_expires_at(self, _sandbox_id: str) -> None:
        return None

    def forget_published_startup_allocation(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def get_runtime(self, _session_id: str, sandbox_id: str | None = None) -> None:
        return None

    async def evict_runtime(self, _session_id: str) -> None:
        return None

    async def evict_runtime_if_current(self, *_args: Any) -> None:
        return None


class _Subjects:
    """An Agent conversation's runtime subject: every startup creates the session's own box."""

    def __init__(self, sessions: SessionRepository) -> None:
        self._sessions = sessions

    def recovery_action_for(self, _session: dict[str, Any]) -> str:
        return "recover_session_allocation"

    async def acquire_startup(
        self, *, session_id: str, sandbox_generation: str, resume_engine_session_key: str | None,
        **_kwargs: Any,
    ) -> RuntimeStartupTarget:
        session = await self._sessions.get_session(session_id)
        assert session is not None and session["sandbox_generation"] == sandbox_generation
        plan = SimpleNamespace(
            resume_engine_session_key=resume_engine_session_key,
            sandbox_id=None,
            cwd=None,
            session_kind="agent_chat",
            subject_kind="session",
        )
        return RuntimeStartupTarget(
            action="create_runtime",
            session=session,
            workspace_plan=plan,  # type: ignore[arg-type]
            persist_runtime_identity_on_session=True,
        )

    async def publish_runtime_ready(self, **_kwargs: Any) -> None:
        return None

    async def cleanup_failed_startup_runtime(self, **_kwargs: Any) -> RuntimeStartupCleanup:
        return RuntimeStartupCleanup(destruction=None, leaked_sandbox_id=None)

    async def converge_startup_failure(self, **_kwargs: Any) -> bool:
        return False


class _World:
    def __init__(self) -> None:
        self.sessions = SessionRepository()
        self.runtimes = _Runtimes()
        self.tasks: list[asyncio.Task[Any]] = []
        session_service = SessionService(
            sessions_repo=self.sessions,
            agent_config=_AgentConfig(),  # type: ignore[arg-type]
            runtime_manager=self.runtimes,  # type: ignore[arg-type]
            ttl_seconds=3600,
            spawn_background_task=self.spawn,
            vault_service=SimpleNamespace(),  # type: ignore[arg-type]
        )
        self.lifecycle = SessionLifecycleService(
            sessions_repo=self.sessions,
            session_service=session_service,
            agent_config=_AgentConfig(),
            runtime_manager=self.runtimes,
            runtime_subjects=_Subjects(self.sessions),
            assistant_workspace_service=None,
            spawn_background_task=self.spawn,
        )

    def spawn(self, coro: Any, *, name: str | None = None) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=name)
        self.tasks.append(task)
        return task

    async def settle(self) -> None:
        while self.tasks:
            pending, self.tasks = self.tasks, []
            await asyncio.gather(*pending)

    async def row(self, session_id: str) -> dict[str, Any]:
        row = await self.sessions.get_session_including_deleted(session_id)
        assert row is not None
        return row

    async def conversation(self, session_id: str = "s1", **columns: Any) -> str:
        await self.sessions.create_session(
            {
                "session_id": session_id,
                "user_id": _USER.user_id,
                "session_kind": "agent_chat",
                "engine_kind": "claude_code",
                "agent_id": "a1",
                "workspace_ref": _REF,
                "sandbox_backend": "test-backend",
                "sandbox_generation": f"{session_id}-g0",
                "sandbox_callback_token": "token",
                "engine_capabilities": _MANIFEST,
                "state": "READY",
                **columns,
            }
        )
        return session_id


class _Hosts:
    """What reader hosts hear: the sessions whose ``reader`` notice arrived."""

    def __init__(self, feed: SessionFeed) -> None:
        self.readers: list[str] = []
        feed.on_reader_needed(self.readers.append)

    async def until(self, predicate: Callable[[], bool]) -> None:
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.01)


@pytest.fixture(autouse=True)
async def _sandbox_side(database: str, monkeypatch: pytest.MonkeyPatch) -> None:
    register_builtin_providers()
    await create_agent("a1")
    monkeypatch.setattr(
        startup_module, "build_sandbox_callback_url_from_record", lambda **_kwargs: "http://box.test/callback"
    )

    async def metadata(_session_id: str, _runtime: Any) -> dict[str, Any]:
        return {"engine_capabilities": _MANIFEST}

    monkeypatch.setattr(startup_module, "runtime_initialization_metadata", metadata)


@pytest.fixture
async def hosts(database: str) -> AsyncIterator[_Hosts]:
    feed = SessionFeed()
    await feed.start()
    try:
        yield _Hosts(feed)
    finally:
        await feed.stop()


async def test_concurrent_creates_with_one_idempotency_key_start_one_runtime() -> None:
    world = _World()
    looked = 0
    both_looked = asyncio.Event()
    lookup = world.sessions.get_session_including_deleted

    async def look_together(session_id: str) -> dict[str, Any] | None:
        # Both creates find no conversation before either inserts one.
        nonlocal looked
        row = await lookup(session_id)
        looked += 1
        if looked == 2:
            both_looked.set()
        if looked <= 2:
            await both_looked.wait()
        return row

    world.sessions.get_session_including_deleted = look_together  # type: ignore[method-assign]
    first, second = await asyncio.gather(
        *(
            world.lifecycle.create_session(
                _USER, session_kind="agent_chat", workspace_ref=_REF, agent_id="a1", idempotency_key="k1"
            )
            for _ in range(2)
        )
    )
    await world.settle()

    assert first["session_id"] == second["session_id"]
    assert len(world.runtimes.created) == 1
    assert (await world.row(first["session_id"]))["state"] == "READY"


async def test_a_gone_runtime_is_replaced_once_and_resumes_its_engine_session(hosts: _Hosts) -> None:
    world = _World()
    # The runtime died while working; its last report does not hold the replacement back.
    session_id = await world.conversation(
        runtime_unavailable=True, sandbox_id=None, engine_session_key="native-7", runtime_status="working"
    )

    await asyncio.gather(*(world.lifecycle.replace_runtime(session_id) for _ in range(3)))
    await world.settle()
    await hosts.until(lambda: session_id in hosts.readers)

    assert world.runtimes.created == [{"session_id": session_id, "resume": "native-7"}]
    row = await world.row(session_id)
    assert row["state"] == "READY"
    assert row["sandbox_id"] == "box-1"
    assert row["engine_session_key"] == "native-7"
    assert row["runtime_unavailable"] is False


async def test_a_create_key_never_answers_with_another_user_s_conversation() -> None:
    world = _World()
    taken = _idempotent_session_id(_USER.user_id, "a1", "browser-1")
    await world.conversation(taken, user_id="someone-else")

    with pytest.raises(APIError) as conflict:
        await world.lifecycle.create_session(
            _USER, idempotency_key="browser-1", session_kind="agent_chat", workspace_ref=_REF, agent_id="a1"
        )

    assert (conflict.value.code, conflict.value.status_code) == ("IDEMPOTENCY_KEY_CONFLICT", 409)
    assert world.runtimes.created == []
