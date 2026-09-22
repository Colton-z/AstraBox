from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.settings import (
    DEFAULT_REMOTE_AGENT_MAX_BUFFER_SIZE_BYTES,
)
from astrabox.core.service.orchestrator.engine.claude_code_config import (
    build_claude_options_kwargs,
)
from astrabox.core.service.orchestrator.sandbox_runner import (
    HostLink,
    _real_session_factory,
)


def _settings(**overrides: Any) -> SimpleNamespace:
    values = {
        "remote_agent_include_partial_messages": True,
        "remote_agent_max_buffer_size": DEFAULT_REMOTE_AGENT_MAX_BUFFER_SIZE_BYTES,
        "mcp_proxy_base_url": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _template(*, system: str | None = None, **engine_options: Any) -> SimpleNamespace:
    return SimpleNamespace(
        name="buffer-template",
        engine_options={"sdk_options": engine_options},
        system=system,
        plugin_repos=[],
        mcp_servers={},
        skills=[],
    )


def _options(*, system: str | None = None, **engine_options: Any) -> Any:
    return build_claude_options_kwargs(
        _settings(),
        _template(system=system, **engine_options),
        cwd="/tmp",
        session_id="session-buffer",
        permission_mode="default",
    )


def test_stdout_buffer_defaults_to_32_mib_and_live_user_replay_is_mandatory() -> None:
    options = _options()

    assert options["max_buffer_size"] == 32 * 1024 * 1024
    assert options["extra_args"]["replay-user-messages"] is None


def test_template_positive_override_takes_effect_but_cannot_disable_replay() -> None:
    options = _options(
        max_buffer_size=64 * 1024 * 1024,
    )

    assert options["max_buffer_size"] == 64 * 1024 * 1024
    assert options["extra_args"]["replay-user-messages"] is None
    with pytest.raises(APIError) as raised:
        _options(extra_args={"replay-user-messages": "false"})
    assert raised.value.code == "CLAUDE_OPTIONS_INVALID"
    assert "replay-user-messages" in raised.value.message


def test_agent_system_instructions_append_to_the_native_claude_code_prompt() -> None:
    options = _options(system="Use the research workflow.")

    assert options["system_prompt"] == {
        "type": "preset",
        "preset": "claude_code",
        "append": "Use the research workflow.",
    }


@pytest.mark.parametrize("key", ["system_prompt", "unknown_option", "mcp_servers"])
def test_undeclared_engine_options_are_refused_at_the_write_gate(key: str) -> None:
    # The engine declares JSON block names, while platform-managed SDK fields
    # remain first-class Agent inputs rather than native overrides.
    from astrabox.providers import register_builtin_providers
    from astrabox.core.service.orchestrator.engine.capabilities import (
        capabilities_for_engine_kind,
    )
    from astrabox.core.service.orchestrator.schema_validation import (
        validate_declared_config_bag,
    )

    register_builtin_providers()
    schema = capabilities_for_engine_kind("claude_code").engine_options_schema
    with pytest.raises(APIError) as raised:
        validate_declared_config_bag(
            {key: "value"} if key == "unknown_option" else {"sdk_options": {key: "value"}},
            schema,
            bag_label="engine_options",
            owner_label="engine 'claude_code'",
        )
    assert raised.value.code == "INVALID_REQUEST"
    assert key in str(raised.value.message)


@pytest.mark.parametrize("value", [0, -1, True, "33554432"])
def test_non_positive_or_non_integer_template_buffer_is_rejected(value: Any) -> None:
    with pytest.raises(APIError) as raised:
        _options(max_buffer_size=value)

    assert raised.value.code == "CLAUDE_OPTIONS_INVALID"


class _Link(HostLink):
    def is_connected(self) -> bool:
        return True

    async def send(self, frame: dict[str, Any]) -> bool:
        _ = frame
        return True


class _Sdk:
    captured_options: list[Any] = []

    def __init__(self, *, options: Any) -> None:
        self.captured_options.append(options)

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def receive_messages(self):  # noqa: ANN201 - SDK async iterator
        while True:
            await __import__("asyncio").sleep(60)
            yield None


async def test_positive_override_crosses_the_runner_option_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    import claude_agent_sdk

    options = _options(max_buffer_size=64 * 1024 * 1024)
    _Sdk.captured_options.clear()
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", _Sdk)
    monkeypatch.setenv("ASTRABOX_RUNNER_SPOOL_DIR", str(tmp_path / "spool"))
    session = _real_session_factory(
        {
            "slot_id": "slot-buffer",
            "options": {"max_buffer_size": options["max_buffer_size"]},
        },
        _Link(),
    )
    try:
        # The factory built this session's transcript target deferred, the way
        # a prepared slot's is; activation is what binds it.
        await session.start(store={"base_url": "http://host/api"})
        assert len(_Sdk.captured_options) == 1
        assert _Sdk.captured_options[0].max_buffer_size == 64 * 1024 * 1024
        assert set(_Sdk.captured_options[0].hooks) == {
            "PreToolUse",
            "UserPromptSubmit",
        }, "input observation is added without replacing the permission gate"
    finally:
        await session.stop()
