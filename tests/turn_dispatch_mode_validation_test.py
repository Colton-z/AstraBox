"""A requested permission mode is validated BEFORE the command is journaled.

The journal write is durable and the engine FIFO delivers strictly in order,
so an input accepted first and refused later squats the FIFO head forever —
every subsequent valid message then fails the consumption-boundary identity
check. These tests pin the ordering: the capability refusal must fire before
any repository write, and a modeless request must pass the gate.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from astrabox.core.service.orchestrator.engine import (  # noqa: F401
    deepseek_harness as _dsh_engine,  # module import registers the adapter
    hermes as _assistant_engine,  # registers engine_kind="assistant"
)

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.session_kernel.service_mixins.turn_dispatch import (
    TurnDispatchStreamingMixin,
)

_DSH_SESSION = {
    "session_id": "3e9c2a54-30ff-4f2f-9f6a-0a5b6f9d2f11",
    "session_kind": "agent_chat",
    "engine_kind": "deepseek_harness",
    "engine_capabilities": {
        "engine_kind": "deepseek_harness",
        "input_content_types": ["text"],
        "permission_modes": [
            "read-only",
            "workspace-write",
            "danger-full-access",
        ],
    },
}

#: The engine that declares no permission-mode capability at all. Both
#: refusals reach the same gate by different routes, so both are pinned.
_MODELESS_SESSION = {
    "session_id": "5b1d0f77-2c4a-4c9e-a0b1-7e3c5d9a4f22",
    "session_kind": "assistant_chat",
    "engine_kind": "assistant",
    "engine_capabilities": {
        "engine_kind": "assistant",
        "input_content_types": ["text"],
        "permission_modes": [],
    },
}


class _JournalTouched(Exception):
    """Sentinel proving execution got past the validation gate."""


def _kernel() -> Any:
    kernel = MagicMock()
    kernel._session_events_repo.get_command_event = AsyncMock(
        side_effect=_JournalTouched
    )
    # Bind the real method under test onto the mock.
    kernel._dispatch_active_input_queue = (
        TurnDispatchStreamingMixin._dispatch_active_input_queue.__get__(kernel)
    )
    kernel._input_command_id = TurnDispatchStreamingMixin._input_command_id
    kernel._input_id = TurnDispatchStreamingMixin._input_id
    return kernel


@pytest.mark.asyncio
async def test_a_mode_the_engine_does_not_declare_is_refused_before_journaling() -> None:
    """The engine has modes; this is not one of them.

    ``default`` is a Claude mode. The harness's own presets are read-only,
    workspace-write and danger-full-access, so naming another engine's
    vocabulary must be refused — and refused before the journal is touched.
    """

    kernel = _kernel()
    with pytest.raises(APIError) as excinfo:
        await kernel._dispatch_active_input_queue(
            user=MagicMock(user_id="u1"),
            session=dict(_DSH_SESSION),
            session_id=_DSH_SESSION["session_id"],
            content="hello",
            content_blocks=None,
            permission_mode="default",
            client_message_id="9c1a7c1e-6a1f-4a37-9b6c-2f2f9d1c0aa1",
        )
    assert excinfo.value.code == "INVALID_REQUEST"
    kernel._session_events_repo.get_command_event.assert_not_awaited()
    kernel._session_events_repo.try_claim_event.assert_not_called()


@pytest.mark.asyncio
async def test_a_mode_for_an_engine_without_any_is_refused_before_journaling() -> None:
    """The other refusal route: the engine declares no mode capability."""

    kernel = _kernel()
    with pytest.raises(APIError) as excinfo:
        await kernel._dispatch_active_input_queue(
            user=MagicMock(user_id="u1"),
            session=dict(_MODELESS_SESSION),
            session_id=_MODELESS_SESSION["session_id"],
            content="hello",
            content_blocks=None,
            permission_mode="workspace-write",
            client_message_id="7d2b8e3f-1a4c-4b5d-8e6f-3a2b1c0d9e88",
        )
    assert excinfo.value.code == "ENGINE_CAPABILITY_UNAVAILABLE"
    kernel._session_events_repo.get_command_event.assert_not_awaited()
    kernel._session_events_repo.try_claim_event.assert_not_called()


@pytest.mark.asyncio
async def test_modeless_request_passes_the_gate() -> None:
    kernel = _kernel()
    with pytest.raises(_JournalTouched):
        await kernel._dispatch_active_input_queue(
            user=MagicMock(user_id="u1"),
            session=dict(_DSH_SESSION),
            session_id=_DSH_SESSION["session_id"],
            content="hello",
            content_blocks=None,
            permission_mode=None,
            client_message_id="9c1a7c1e-6a1f-4a37-9b6c-2f2f9d1c0aa1",
        )


@pytest.mark.asyncio
async def test_a_mode_added_by_the_live_engine_passes_before_journaling() -> None:
    session = dict(_DSH_SESSION)
    session["engine_capabilities"] = {
        "engine_kind": "deepseek_harness",
        "input_content_types": ["text"],
        "permission_modes": [
            "read-only",
            "workspace-write",
            "danger-full-access",
            "vendor-new-mode",
        ],
    }
    kernel = _kernel()

    with pytest.raises(_JournalTouched):
        await kernel._dispatch_active_input_queue(
            user=MagicMock(user_id="u1"),
            session=session,
            session_id=session["session_id"],
            content="hello",
            content_blocks=None,
            permission_mode="vendor-new-mode",
            client_message_id="c3987a20-c7fc-42da-97a1-21a9fd4c2427",
        )


@pytest.mark.asyncio
async def test_a_missing_verified_manifest_fails_before_journaling() -> None:
    session = dict(_DSH_SESSION)
    session.pop("engine_capabilities")
    kernel = _kernel()

    with pytest.raises(APIError) as excinfo:
        await kernel._dispatch_active_input_queue(
            user=MagicMock(user_id="u1"),
            session=session,
            session_id=session["session_id"],
            content="hello",
            content_blocks=None,
            permission_mode="workspace-write",
            client_message_id="93dd745d-e88f-41f2-80c7-daaf21e2a94b",
        )

    assert excinfo.value.code == "ENGINE_CAPABILITY_CONTRACT_VIOLATION"
    kernel._session_events_repo.get_command_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unsupported_image_is_refused_before_journaling() -> None:
    kernel = _kernel()

    with pytest.raises(APIError) as excinfo:
        await kernel._dispatch_active_input_queue(
            user=MagicMock(user_id="u1"),
            session=dict(_DSH_SESSION),
            session_id=_DSH_SESSION["session_id"],
            content="look",
            content_blocks=[
                {"type": "text", "text": "look"},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "iVBORw0KGgo=",
                    },
                },
            ],
            permission_mode=None,
            client_message_id="b735b351-4fa8-46b1-90d4-644d43f67e75",
        )

    assert excinfo.value.code == "ENGINE_CAPABILITY_UNAVAILABLE"
    kernel._session_events_repo.get_command_event.assert_not_awaited()
    kernel._session_events_repo.try_claim_event.assert_not_called()


@pytest.mark.asyncio
async def test_a_declared_image_passes_the_gate() -> None:
    session = dict(_DSH_SESSION)
    session["engine_capabilities"] = {
        **_DSH_SESSION["engine_capabilities"],
        "input_content_types": ["text", "image"],
    }
    kernel = _kernel()

    with pytest.raises(_JournalTouched):
        await kernel._dispatch_active_input_queue(
            user=MagicMock(user_id="u1"),
            session=session,
            session_id=session["session_id"],
            content="",
            content_blocks=[
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "iVBORw0KGgo=",
                    },
                }
            ],
            permission_mode=None,
            client_message_id="7f993348-7c98-4c36-a8b3-362e95ddd2f8",
        )
