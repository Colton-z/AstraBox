"""A session's READY does not wait for the engine's optional init snapshot.

The startup runs against the real :class:`SessionStartup` with the sandbox
side and the store faked; the store keeps one row and applies the same
compare-and-set the startup and the observation write through.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

# The adapter registers the Claude manifest vocabulary on import.
from astrabox.core.service.orchestrator.engine import claude_code  # noqa: F401
from astrabox.core.service.orchestrator.engine.base import EngineCapabilityManifest
from astrabox.core.service.orchestrator.session_lifecycle import startup


class _Store:
    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row
        self.notices: list[str | None] = []

    async def get_session(self, _session_id: str) -> dict[str, Any]:
        return dict(self.row)

    async def compare_and_update_session(
        self, _session_id: str, *, expected: dict[str, Any], updates: dict[str, Any],
        notify: str | None = None, touch_updated_at: bool = True,
    ) -> bool:
        if any(self.row.get(key) != value for key, value in expected.items()):
            return False
        self.row.update(updates)
        self.notices.append(notify)
        return True


async def test_ready_is_published_while_the_snapshot_read_is_held_and_the_snapshot_lands_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store({
        "session_id": "s1", "user_id": "u1", "state": "CREATING",
        "runtime_recovery_owner": "owner-1", "sandbox_generation": "g1",
    })
    reading, release = asyncio.Event(), asyncio.Event()

    async def server_info() -> dict[str, Any]:
        reading.set()
        await release.wait()
        return {"subtype": "init", "slash_commands": ["compact"]}

    runtime = SimpleNamespace(
        sandbox_id="box-1",
        engine_kind="claude_code",
        engine_manifest=EngineCapabilityManifest(engine_kind="claude_code", supports_server_info=True),
        engine_client=SimpleNamespace(get_server_info=server_info),
    )
    target = SimpleNamespace(
        action="create_runtime",
        workspace_plan=SimpleNamespace(session_kind="agent_chat"),
        session=dict(store.row),
        renewal_ttl_seconds=None,
        persist_runtime_identity_on_session=False,
    )
    service = startup.SessionStartup(
        sessions_repo=store,
        agent_config=SimpleNamespace(resolve_session_harness=AsyncMock(return_value=object())),
        runtime_manager=SimpleNamespace(
            create_runtime=AsyncMock(return_value=runtime),
            get_sandbox_expires_at=AsyncMock(return_value=None),
            resolve_template_model_name=Mock(return_value="model-1"),
            forget_published_startup_allocation=Mock(),
        ),
        runtime_subjects=SimpleNamespace(
            acquire_startup=AsyncMock(return_value=target), publish_runtime_ready=AsyncMock(),
        ),
    )
    monkeypatch.setattr(startup, "require_session_user_prerequisites", AsyncMock())
    monkeypatch.setattr(startup, "build_sandbox_callback_url_from_record", lambda **_kwargs: None)

    # A snapshot read that never answers must not hold the session in CREATING:
    # plain text can be sent as soon as the runtime takes input.
    ready = await asyncio.wait_for(
        service.run(
            session_id="s1", owner="owner-1", check_lock=AsyncMock(), sandbox_generation="g1",
            resume_engine_session_key=None,
        ),
        timeout=2,
    )
    assert ready["state"] == "READY"
    assert ready["engine_capabilities"]["engine_kind"] == "claude_code"
    assert "slash_commands" not in ready
    assert store.notices[-1] == "reader"

    # The connection serving the session records the snapshot once it arrives,
    # and tells open pages to read the session again.
    observing = asyncio.create_task(
        startup.record_runtime_observation(store, "s1", runtime, sandbox_generation="g1")
    )
    await reading.wait()
    assert "slash_commands" not in store.row
    release.set()
    await observing
    assert store.row["slash_commands"] == ["compact"]
    assert store.row["runtime_versions"]["sandbox_id"] == "box-1"
    assert store.notices[-1] == "session"

    # A read that lands after the session moved to another generation is not
    # written over what that generation recorded.
    store.row.update(sandbox_generation="g2", slash_commands=["review"])
    await startup.record_runtime_observation(store, "s1", runtime, sandbox_generation="g1")
    assert store.row["slash_commands"] == ["review"]
