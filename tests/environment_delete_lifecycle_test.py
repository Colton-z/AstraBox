"""Native metadata plus real lifecycle workers; only runtime allocation is fake.

The gates hold actual lifecycle boundaries, not a second implementation of
startup or deletion. These integration cases are not live sandbox E2E.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from tests.assistant_workspace_wake_chain_test import _USER, _World
from tests.environment_delete_test import ADMIN, service as service


@pytest.mark.parametrize("boundary", [
    "before-session-write", "after-harness-read", "after-allocation", "after-unconfirmed-allocation",
])
async def test_environment_deletion_during_assistant_wake_preserves_startup_cleanup(
    service: Any, monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    monkeypatch.setenv("ASTRABOX_MCP_PROXY_BASE_URL", "http://astrabox.test:8080")
    await service._environment_repo.upsert("env-1", {
        "enabled": True, "engine_kind": "assistant", "sandbox_backend": "open_sandbox",
    })
    world = _World(
        sessions=service._sessions_repo, workspace_repo=service._assistant_workspace_repo,
        catalog=service._assistant_repo, agent_config=service,
    )
    assistant_id = await world.create_assistant()
    reached, release = asyncio.Event(), asyncio.Event()
    session_ids: list[str] = []
    original_create = world.sessions.create_session

    async def create(payload: dict[str, Any]) -> dict[str, Any]:
        session_ids.append(str(payload["session_id"]))
        if boundary == "before-session-write":
            reached.set()
            await release.wait()
        return await original_create(payload)

    monkeypatch.setattr(world.sessions, "create_session", create)
    original_resolve = service.resolve_session_harness

    async def resolve(session: dict[str, Any], **kwargs: Any) -> Any:
        result = await original_resolve(session, **kwargs)
        if boundary == "after-harness-read" and session.get("session_id"):
            assert session["state"] == "CREATING"
            assert result is not None
            reached.set()
            await release.wait()
        return result

    monkeypatch.setattr(service, "resolve_session_harness", resolve)

    async def allocated(runtime: Any) -> None:
        reached.set()
        await release.wait()

    if boundary in {"after-allocation", "after-unconfirmed-allocation"}:
        world.runtime.after_create = allocated
        world.runtime.kill_confirms = boundary == "after-allocation"
    waking = asyncio.create_task(world.service.wake_workspace(_USER, assistant_id))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        assert await world.service.delete_assistant(_USER, assistant_id) == {
            "assistant_id": assistant_id, "deleted": True,
        }
        if boundary == "before-session-write":
            assert await world.sessions.get_session(session_ids[0]) is None
            assert await service.delete_environment_config(ADMIN, "env-1") == {
                "name": "env-1", "deleted": True,
            }
        else:
            assert (await world.sessions.get_session(session_ids[0]))["state"] == "CREATING"
            with pytest.raises(APIError) as refused:
                await service.delete_environment_config(ADMIN, "env-1")
            assert refused.value.code == "ENVIRONMENT_IN_USE"
            assert await service.get_environment("env-1") is not None
    finally:
        release.set()
        await asyncio.wait_for(waking, 5)
        await asyncio.wait_for(world.settle(), 5)

    assert len(session_ids) == 1
    row = await world.sessions.get_session(session_ids[0])
    assert row["state"] == "TERMINATED"
    if boundary == "after-unconfirmed-allocation":
        assert world.runtime.created == session_ids
        assert world.runtime.terminated == [{
            "runtime_key": session_ids[0], "fallback_sandbox_id": "sbx-1",
        }]
        workspace = await world.workspace_service.get_workspace(
            user_id=_USER.user_id, assistant_id=assistant_id,
        )
        assert (
            row.get("sandbox_id") == "sbx-1"
            or "sbx-1" in row.get("undestroyed_sandbox_ids", [])
            or workspace.get("current_sandbox_id") == "sbx-1"
        )
        with pytest.raises(APIError) as refused:
            await service.delete_environment_config(ADMIN, "env-1")
        assert refused.value.code == "ENVIRONMENT_IN_USE"
        assert await service.get_environment("env-1") is not None
        return
    assert not row.get("sandbox_id")
    assert not row.get("startup_allocation")
    assert not row.get("_retained_startup_allocations")
    assert not row.get("undestroyed_sandbox_ids")
    if boundary == "after-allocation":
        assert world.runtime.created == session_ids
        assert world.runtime.terminated == [{
            "runtime_key": session_ids[0], "fallback_sandbox_id": "sbx-1",
        }]
    else:
        assert world.runtime.created == []
        assert world.runtime.destroyed == []
    if boundary != "before-session-write":
        assert await service.delete_environment_config(ADMIN, "env-1") == {
            "name": "env-1", "deleted": True,
        }
    assert await service.get_environment("env-1") is None
    assert await world.sessions.get_session_including_deleted(session_ids[0]) is not None
