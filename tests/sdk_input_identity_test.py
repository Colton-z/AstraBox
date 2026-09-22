from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from claude_agent_sdk import ToolResultBlock, UserMessage

from astrabox.core.service.orchestrator.sandbox_runner import (
    DeliveryCommand,
    RunnerProtocolError,
    RunnerSession,
)


class _Link:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    def is_connected(self) -> bool:
        return True

    async def send(self, frame: dict[str, Any]) -> bool:
        self.frames.append(dict(frame))
        return True


class _Sdk:
    def __init__(self) -> None:
        self.inputs: list[dict[str, Any]] = []
        self.messages: asyncio.Queue[Any] = asyncio.Queue()

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def query(self, prompt: Any, session_id: str = "default") -> None:
        _ = session_id
        self.inputs.extend([item async for item in prompt])

    async def receive_messages(self):  # noqa: ANN201 - SDK async iterator
        while True:
            yield await self.messages.get()

    async def interrupt(self) -> None:
        return None

    async def stop_task(self, task_id: str) -> None:
        _ = task_id

    async def set_permission_mode(self, mode: str) -> None:
        _ = mode

    async def get_server_info(self) -> dict[str, Any]:
        return {}


async def _running_session() -> tuple[RunnerSession, _Sdk, _Link]:
    sdk = _Sdk()
    link = _Link()
    session = RunnerSession(
        session_id="session-identity",
        link=link,
        client_factory=lambda _broker: sdk,
    )
    await session.start()
    await session.sender.replay_after(0)
    link.frames.clear()
    return session, sdk, link


def _delivery(
    command_id: str,
    input_id: str,
    content: str,
    *,
    sequence: int = 1,
) -> DeliveryCommand:
    return DeliveryCommand(
        command_id=command_id,
        session_id="session-identity",
        sequence=sequence,
        sdk_input={
            "type": "user",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
            "session_id": "session-identity",
            "uuid": input_id,
        },
    )


async def test_internal_prompt_cannot_consume_the_external_input_identity() -> None:
    session, sdk, link = await _running_session()
    input_id = "00000000-0000-0000-0000-000000000101"
    try:
        await session.submit(
            _delivery("command-101", input_id, "the platform prompt")
        )

        await session.on_user_prompt_submit(
            {
                "hook_event_name": "UserPromptSubmit",
                "prompt": "<task-notification>child completed</task-notification>",
            },
            None,
            None,
        )
        assert not [
            frame
            for frame in link.frames
            if frame.get("message_type") == "UserMessage"
        ], "an SDK-owned prompt must not become a platform user input"

        await session.on_user_prompt_submit(
            {
                "hook_event_name": "UserPromptSubmit",
                "prompt": "the platform prompt",
            },
            None,
            None,
        )
        observed = [
            frame
            for frame in link.frames
            if frame.get("message_type") == "UserMessage"
        ]
        assert len(observed) == 1
        assert observed[0]["message"]["uuid"] == input_id
        # The vendor sees its own message id; the host frame above carries the
        # platform input id back.
        assert [
            {k: v for k, v in item.items() if k != "uuid"} for item in sdk.inputs
        ] == [
            {
                "type": "user",
                "message": {"role": "user", "content": "the platform prompt"},
                "parent_tool_use_id": None,
                "session_id": "session-identity",
            }
        ]
        assert sdk.inputs[0]["uuid"] != input_id
    finally:
        await session.stop()


async def test_sdk_owned_root_message_cannot_cross_external_input_boundary() -> None:
    session, sdk, link = await _running_session()
    input_id = "00000000-0000-0000-0000-000000000103"
    try:
        await session.submit(_delivery("command-103", input_id, "the platform prompt"))
        await sdk.messages.put(
            UserMessage(
                content="<task-notification>child completed</task-notification>",
                uuid="00000000-0000-0000-0000-000000000999",
                parent_tool_use_id=None,
            )
        )
        await asyncio.sleep(0.01)
        assert not [
            frame
            for frame in link.frames
            if frame.get("message_type") == "UserMessage"
        ], "an SDK-owned root prompt must not become platform input consumption"

        await session.on_user_prompt_submit(
            {
                "hook_event_name": "UserPromptSubmit",
                "prompt": "the platform prompt",
            },
            None,
            None,
        )
        observed = [
            frame
            for frame in link.frames
            if frame.get("message_type") == "UserMessage"
        ]
        assert len(observed) == 1
        assert observed[0]["message"]["uuid"] == input_id
    finally:
        await session.stop()


async def test_sdk_tool_result_user_message_still_crosses_the_engine_stream() -> None:
    session, sdk, link = await _running_session()
    try:
        await sdk.messages.put(
            UserMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id="call-1",
                        content="done",
                        is_error=False,
                    )
                ],
                uuid="tool-result-uuid",
                parent_tool_use_id=None,
            )
        )
        await asyncio.sleep(0.01)
        observed = [
            frame
            for frame in link.frames
            if frame.get("message_type") == "UserMessage"
        ]
        assert len(observed) == 1
        assert observed[0]["message"]["content"][0]["__sdk_type"] == "ToolResultBlock"
    finally:
        await session.stop()


async def test_one_external_identity_is_observed_once_when_the_sdk_echoes_it() -> None:
    session, sdk, link = await _running_session()
    input_id = "00000000-0000-0000-0000-000000000102"
    try:
        command = _delivery("command-102", input_id, "once")
        await session.submit(command)
        await session.on_user_prompt_submit(
            {"hook_event_name": "UserPromptSubmit", "prompt": "once"},
            None,
            None,
        )
        await sdk.messages.put(
            UserMessage(content="once", uuid=input_id, parent_tool_use_id=None)
        )
        await asyncio.sleep(0.01)
        assert len(
            [frame for frame in link.frames if frame.get("message_type") == "UserMessage"]
        ) == 1, "the typed SDK echo must not duplicate hook consumption"

        result = await session.submit(command)
        assert result == "duplicate"
        assert len(sdk.inputs) == 1
    finally:
        await session.stop()


async def test_input_without_a_canonical_platform_identity_fails_loudly() -> None:
    session, _sdk, _link = await _running_session()
    try:
        with pytest.raises(RunnerProtocolError, match="uuid"):
            await session.submit(_delivery("command-empty", "", "unidentified"))
    finally:
        await session.stop()


async def test_opaque_platform_command_id_has_one_stable_sdk_uuid() -> None:
    session, sdk, _link = await _running_session()
    try:
        input_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "command-opaque"))
        command = _delivery("command-opaque", input_id, "identified")
        await session.submit(command)
        first_uuid = sdk.inputs[0]["uuid"]
        assert str(uuid.UUID(first_uuid)) == first_uuid
        assert await session.submit(command) == "duplicate"
        assert len(sdk.inputs) == 1
    finally:
        await session.stop()
