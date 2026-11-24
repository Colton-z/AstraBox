"""A retired client pool's member that survived its destruction is reclaimed.

OpenSandbox's ``SandboxPoolManagerAsync.destroy`` takes each idle member's id
out of shared state before destroying the member and only logs a destruction
that fails, then marks the namespace destroyed; retiring the pool again
reaches no member. The scenario runs the SDK's own pool and destroy against a
real Redis (``ASTRABOX_TEST_REDIS_URL``) and a stand-in OpenSandbox control
plane, then the platform's retirement and its ownerless reap: the member whose
destruction failed must be destroyed by the reap, while the members sessions
took from the pool, including one whose owner is still being published, are
kept.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.core.service.orchestrator import runtime_manager as runtime_manager_module
from astrabox.core.service.orchestrator.agent import client_pool
from astrabox.core.service.orchestrator.agent.agent_service import AgentService
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.providers.open_sandbox.sandbox import (
    OpenSandboxHandle,
    OpenSandboxSandboxProvider,
)
from astrabox.seams.sandbox import SandboxClientPoolMember, SandboxClientPoolSpec
from tests._agent_ledger import create_agent, set_runtime
from tests.open_sandbox_provider_test import _BASE_URL, _FakeLifecycle, _create_spec

_REDIS_URL_ENV = "ASTRABOX_TEST_REDIS_URL"
_BACKEND = "open_sandbox"
_AGENT = "agent-1"


@pytest.fixture
def redis_url() -> str:
    """A Redis server the SDK's pool state store may write to."""
    url = os.environ.get(_REDIS_URL_ENV, "").strip()
    if not url:
        raise RuntimeError(
            f"{_REDIS_URL_ENV} is required for tests of the client pool's shared state: "
            "a redis:// URL, for example a throwaway redis:7 container"
        )
    return url


class _ControlPlane(_FakeLifecycle):
    """The lifecycle server with its own clock, renewals, prepared seats and a failing kill.

    Each box records when it was created; a renewal sets the expiry the SDK
    asks for; every box answers one active isolated session, as a box
    carrying prepared seats does; and one named box's next destruction fails
    as a transient server error.
    """

    def __init__(self) -> None:
        super().__init__()
        self.failing_kill: str | None = None
        self.kills: list[str] = []

    def add_sandbox(self, sandbox_id: str, **kwargs: Any) -> None:
        super().add_sandbox(sandbox_id, **kwargs)
        self.sandboxes[sandbox_id]["created_at"] = datetime.now(timezone.utc)

    def elapse(self, interval: timedelta) -> None:
        """Move every existing box's creation and expiry back, as if ``interval`` had passed."""
        for row in self.sandboxes.values():
            row["created_at"] -= interval
            if row["expires_at"] is not None:
                row["expires_at"] -= interval

    def _sandbox_json(self, sandbox_id: str) -> dict[str, Any]:
        body = super()._sandbox_json(sandbox_id)
        body["createdAt"] = self.sandboxes[sandbox_id]["created_at"].isoformat()
        return body

    def _handle_lifecycle(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.strip("/").split("/")
        if len(parts) >= 3 and parts[:2] == ["v1", "sandboxes"] and parts[2] in self.sandboxes:
            sandbox_id, tail = parts[2], parts[3:]
            if tail == ["renew-expiration"] and request.method == "POST":
                expires_at = datetime.fromisoformat(json.loads(request.content)["expiresAt"])
                self.sandboxes[sandbox_id]["expires_at"] = expires_at
                return httpx.Response(200, json={"expiresAt": expires_at.isoformat()})
            if not tail and request.method == "DELETE":
                self.kills.append(sandbox_id)
                if sandbox_id == self.failing_kill:
                    self.failing_kill = None
                    return self._error(503, "UNAVAILABLE")
        return super()._handle_lifecycle(request)

    def _handle_execd(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/isolated/sessions" and request.method == "GET":
            now = datetime.now(timezone.utc).isoformat()
            seat = {"session_id": "seat-1", "status": "active", "created_at": now, "last_run_at": now}
            return httpx.Response(200, json={"sessions": [seat]})
        return super()._handle_execd(request)


async def _eventually(check: Callable[[], Any], what: str) -> None:
    try:
        async with asyncio.timeout(30):
            while not await check():
                await asyncio.sleep(0.1)
    except TimeoutError:
        raise AssertionError(f"timed out waiting until {what}") from None


async def test_retired_pool_member_whose_destruction_failed_is_reclaimed(
    database: str, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTRABOX_SANDBOX_OPENAPI_BASE_URL", _BASE_URL)
    monkeypatch.setenv("OPENSANDBOX_DISABLE_METRICS", "1")
    monkeypatch.setenv("ASTRABOX_AGENT_PREWARM_REDIS_URL", redis_url)
    lease = int(load_astrabox_settings().sandbox_lease_seconds)
    control_plane = _ControlPlane()
    # Two server replicas sharing the control plane and Redis.
    replica = OpenSandboxSandboxProvider(transport=control_plane.transport())
    other_replica = OpenSandboxSandboxProvider(transport=control_plane.transport())
    pool_name = f"astrabox-agent-{_AGENT}-{uuid.uuid4().hex[:12]}"
    spec = SandboxClientPoolSpec(
        pool_name=pool_name,
        creation_image="astrabox/sandbox-claude-code:test",
        max_idle=2,
        idle_timeout_seconds=lease,
        preparation_timeout_seconds=600,
    )

    async def create(member: SandboxClientPoolMember) -> OpenSandboxHandle:
        return await replica.create_sandbox(
            _create_spec(session_id=member.session_id, assignment_id=member.assignment_id)
        )

    async def prepare(_handle: Any) -> None:
        return None

    async def idle() -> tuple[str, ...]:
        status = await replica.describe_client_pool(pool_name)
        return status.idle_sandbox_ids if status.idle_count == 2 else ()

    adopted: OpenSandboxHandle | None = None
    handing_out: OpenSandboxHandle | None = None
    repo = await create_agent(_AGENT)
    await set_runtime(_AGENT, client_pool_epoch="first")
    assert await repo.record_client_pool(
        _AGENT, client_pool_epoch="first", name=pool_name, backend=_BACKEND
    ) == "recorded"
    try:
        await replica.ensure_client_pool(spec, creator=create, preparer=prepare)
        await _eventually(idle, "the pool fills")

        # A session takes a member and publishes itself as its owner.
        adopted = await replica.acquire_client_pool(spec)
        assert adopted is not None
        await SessionRepository().create_session({
            "session_id": "session-adopted", "user_id": "someone", "agent_id": _AGENT,
            "session_kind": "agent_chat", "engine_kind": "claude_code", "state": "READY",
            "sandbox_id": adopted.sandbox_id, "sandbox_backend": _BACKEND,
        })
        await replica.adopt_sandbox_identity(
            adopted, session_id="session-adopted", assignment_id=str(uuid.uuid4())
        )
        await _eventually(idle, "the pool refills")

        # An hour passes for every box that exists now.
        control_plane.elapse(timedelta(hours=1))

        # Another session takes a member and has not yet published its owner
        # when the pool is retired.
        handing_out = await replica.acquire_client_pool(spec)
        assert handing_out is not None
        await _eventually(idle, "the pool refills again")
        status = await replica.describe_client_pool(pool_name)
        (survivor,) = [
            sandbox_id
            for sandbox_id in status.idle_sandbox_ids
            if control_plane.sandboxes[sandbox_id]["created_at"]
            < datetime.now(timezone.utc) - timedelta(minutes=30)
        ]
        control_plane.failing_kill = survivor

        # An Agent edit displaces the pool, and the platform retires it.
        await set_runtime(_AGENT, client_pool_epoch="second")
        assert await repo.record_client_pool(
            _AGENT, client_pool_epoch="second", name=f"{pool_name}-next", backend=_BACKEND
        ) == "recorded"
        monkeypatch.setattr(client_pool, "sandbox_for_name", lambda _name: replica)
        service = AgentService.__new__(AgentService)
        service._agent_repo = repo  # type: ignore[attr-defined]
        await service._retire_obsolete_pools(_AGENT)
        assert await repo.list_retiring_client_pools(_AGENT) == []
        assert survivor in control_plane.sandboxes, "the control plane failed this destruction"

        # Retiring again from the other replica destroys nothing more.
        kills = list(control_plane.kills)
        await other_replica.retire_client_pool(pool_name)
        assert control_plane.kills == kills

        monkeypatch.setattr(runtime_manager_module, "sandbox_for_name", lambda _name: other_replica)
        manager = runtime_manager_module.RemoteAgentRuntimeManager()
        await manager.reap_ownerless_sandboxes(backend=_BACKEND)

        assert survivor not in control_plane.sandboxes, (
            "a member of the retired pool that no pool hands out must be reclaimed"
        )
        assert adopted.sandbox_id in control_plane.sandboxes
        assert handing_out.sandbox_id in control_plane.sandboxes, (
            "a member handed out just before retirement must outlive its owner's publication"
        )

        # A later reap destroys nothing else.
        kills = list(control_plane.kills)
        await manager.reap_ownerless_sandboxes(backend=_BACKEND)
        assert control_plane.kills == kills
        assert {adopted.sandbox_id, handing_out.sandbox_id} <= set(control_plane.sandboxes)
    finally:
        for handle in (adopted, handing_out):
            if handle is not None:
                await handle.close()
        await replica.shutdown_current_loop_resources()
        await other_replica.shutdown_current_loop_resources()
