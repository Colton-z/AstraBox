"""Permission modes belong to the engine, and the engine takes them from its vendor.

Two failure modes this pins, one on each side of the seam.

Outward: the platform must not own a permission-mode whitelist. The modes are
the engine's vocabulary — claude_code's come from the agent SDK it wraps, and an
assistant engine has no reason to share those names. A platform-wide list would
make every engine added speak the first one's language, which is how a seam
that was only ever shaped for one vendor gets discovered by the second.

Inward: the claude_code engine must READ its set from the SDK rather than keep a
copy. A transcribed list drifts the first time the vendor adds a mode, silently
and in the engine's favour — it would go on rejecting a mode the CLI accepts.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import get_args
from unittest.mock import AsyncMock

import pytest
from claude_agent_sdk.types import PermissionMode

from astrabox.core.service.orchestrator.engine.base import EngineCapabilityManifest
from astrabox.core.service.orchestrator.engine import (  # noqa: F401
    claude_code as _claude_code_engine,
)
from astrabox.core.service.orchestrator.engine.capabilities import (
    capabilities_for_engine_kind,
)
from astrabox.core.service.orchestrator.engine.claude_code_client import (
    ClaudeCodeEngineClient,
)
from astrabox.core.service.orchestrator.session_kernel.permission_lifecycle import (
    PermissionLifecycle,
)


class ClaudeCodeDeclaresTheSdkModesTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_declared_set_is_the_sdks_own(self) -> None:
        client = ClaudeCodeEngineClient(
            link=object(),  # type: ignore[arg-type]
            session_id="s-1",
            transcript_store=AsyncMock(),
            workspace_dir="/workspace",
        )
        manifest = await client.get_capabilities()
        self.assertEqual(
            tuple(manifest.permission_modes),
            get_args(PermissionMode),
            "claude_code must read its modes from the pinned SDK, not transcribe them",
        )

    async def test_static_and_live_declarations_share_the_vendor_vocabulary(self) -> None:
        client = ClaudeCodeEngineClient(
            link=object(),  # type: ignore[arg-type]
            session_id="s-1",
            transcript_store=AsyncMock(),
            workspace_dir="/workspace",
        )
        manifest = await client.get_capabilities()
        static = capabilities_for_engine_kind("claude_code")

        self.assertEqual(static.permission_modes, get_args(PermissionMode))
        self.assertEqual(tuple(manifest.permission_modes), static.permission_modes)


class EngineSeamOwnsTheVocabularyTests(unittest.TestCase):
    def test_an_engine_declares_no_modes_by_default(self) -> None:
        # Empty means "no permission-mode concept". It must NOT inherit another
        # engine's modes just because the platform happens to know them.
        manifest = EngineCapabilityManifest(engine_kind="assistant")
        self.assertEqual(manifest.permission_modes, [])


@pytest.mark.asyncio
async def test_turn_validation_uses_the_verified_runtime_vocabulary() -> None:
    static_modes = list(capabilities_for_engine_kind("claude_code").permission_modes)
    live_mode = "vendor-added-mode"

    class LiveClient:
        async def set_permission_mode(self, mode: str) -> None:
            _ = mode

    runtime = SimpleNamespace(
        sandbox_id="sandbox-1",
        engine_kind="claude_code",
        engine_client=LiveClient(),
        engine_manifest=EngineCapabilityManifest(
            engine_kind="claude_code",
            permission_modes=[*static_modes, live_mode],
        ),
        conversation_bound=True,
        permission_mode=live_mode,
        permission_mode_verified=True,
    )
    session = {
        "session_id": "session-1",
        "session_kind": "agent_chat",
        "engine_kind": "claude_code",
        "sandbox_id": "sandbox-1",
        "permission_mode": live_mode,
        "engine_capabilities": {
            "engine_kind": "claude_code",
            "permission_modes": [*static_modes, live_mode],
        },
    }
    lifecycle = PermissionLifecycle(
        sessions_repo=AsyncMock(),
        apply_engine_permission_mode=AsyncMock(),
        session_events_repo=AsyncMock(),
        session_snapshots_repo=AsyncMock(),
    )

    result = await lifecycle.ensure_before_turn_dispatch(
        session_id="session-1",
        session=session,
        turn_id="turn-1",
        command_id="command-1",
        requested_mode=live_mode,
        runtime=runtime,
    )

    assert result.permission_mode == live_mode
    assert result.changed is False
    lifecycle._apply_engine_permission_mode.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()


@pytest.mark.asyncio
async def test_an_engine_with_no_permission_modes_is_refused_by_its_own_code() -> None:
    """The refusal must survive being recorded.

    An engine may declare no permission-mode concept — pi ships no permission
    system and says so — and setting one on such a session is a client error the
    platform already names: `ENGINE_CAPABILITY_UNAVAILABLE`. Recording that
    failure must not raise on the way out. It did: the failure path recomputed
    the session's current mode with a second, incomplete call, and for the one
    engine whose current mode is None the resulting TypeError replaced a 400 the
    client can act on with a 500 it cannot.
    """

    from astrabox.common.utils.errors import APIError
    from astrabox.core.service.orchestrator.engine import pi as _pi_engine  # noqa: F401

    events = AsyncMock()
    events.append_event = AsyncMock(return_value={"event_seq": 7})
    lifecycle = PermissionLifecycle(
        sessions_repo=AsyncMock(),
        apply_engine_permission_mode=AsyncMock(),
        session_events_repo=events,
        session_snapshots_repo=AsyncMock(),
    )
    session = {
        "session_id": "session-1",
        "session_kind": "agent_chat",
        "engine_kind": "pi",
        "sandbox_id": "sandbox-1",
        "engine_capabilities": {"engine_kind": "pi", "permission_modes": []},
    }

    with pytest.raises(APIError) as caught:
        await lifecycle.apply_explicit_update(
            session_id="session-1",
            session=session,
            command_id="command-1",
            requested_mode="bypassPermissions",
        )

    assert caught.value.code == "ENGINE_CAPABILITY_UNAVAILABLE"
    assert caught.value.status_code == 400
