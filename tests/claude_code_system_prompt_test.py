"""Claude Code runs on its own system prompt, from a cold box or a prepared slot.

The pinned SDK turns a missing ``system_prompt`` into ``--system-prompt ""``:
Claude Code then starts with an empty prompt and none of its own guidance,
while the prepared-slot path always named the vendor preset. These tests drive
both real entry points — ``activate_runtime`` for a cold start and
``prepare_runtime`` for a prepared slot — capture the options each sends to the
in-box runner, and hand them to the vendor SDK's own command builder, so what
is pinned is the Claude Code command line, for an Agent with and without
instructions.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from astrabox.common.utils.settings import DEFAULT_REMOTE_AGENT_MAX_BUFFER_SIZE_BYTES
from astrabox.core.service.orchestrator.engine import claude_code_runtime, runner_link
from astrabox.core.service.orchestrator.engine.base import (
    EnginePreparationContext,
    EngineStartupContext,
)

INSTRUCTIONS = "Answer as the research desk."
PRESET = {"type": "preset", "preset": "claude_code"}


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        remote_agent_include_partial_messages=True,
        remote_agent_max_buffer_size=DEFAULT_REMOTE_AGENT_MAX_BUFFER_SIZE_BYTES,
        mcp_proxy_base_url="",
    )


def _template(system: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        agent_id="agent-1",
        name="Claude Code",
        engine_options=None,
        system=system,
        plugin_repos=[],
        mcp_servers={},
        skills=[],
    )


def _model_access() -> SimpleNamespace:
    return SimpleNamespace(
        credential="model-key",
        credential_kind="api_key",
        base_url="http://gateway.internal",
        model_name="deepseek-flash",
        endpoint_provider="",
    )


class _SandboxFiles:
    async def write_file(self, _path: str, _body: bytes, **_kwargs: Any) -> None:
        pass


def _identity() -> dict[str, Any]:
    return {
        "linux_user": "slotuser",
        "home_dir": "/home/slotuser",
        "workspace_dir": "/home/slotuser/workspace",
        "config_dir": "/home/slotuser/.claude",
        "sandbox_tenancy": "conversation",
    }


async def _cold_start_options(monkeypatch: pytest.MonkeyPatch, system: str | None) -> dict[str, Any]:
    sent: dict[str, Any] = {}

    class _Client:
        engine_session_key = None

        def start_resident_observation(self) -> None:
            pass

        async def close(self) -> None:
            pass

    async def connect(_runner_uri: str, **kwargs: Any) -> _Client:
        sent.update(kwargs["sdk_options"])
        return _Client()

    async def initialize(*_args: Any, **_kwargs: Any) -> object:
        return object()

    monkeypatch.setattr(claude_code_runtime, "_connect_runner_engine_client", connect)
    monkeypatch.setattr(claude_code_runtime, "initialize_engine_client", initialize)
    await claude_code_runtime.activate_runtime(
        EngineStartupContext(
            session_id="session-1",
            template=_template(system),
            workspace_plan=None,
            sandbox=SimpleNamespace(files=_SandboxFiles()),
            sandbox_id="box-1",
            cwd="/workspace",
            runtime_identity=_identity(),
            model_access=_model_access(),
            model_credential="model-key",
            resume_session_key=None,
            runner_uri="ws://runner.internal",
            permission_mode="default",
            deployment_settings=_settings(),
            capability_scope="conversation",
        )
    )
    return sent


async def _prepared_slot_options(monkeypatch: pytest.MonkeyPatch, system: str | None) -> dict[str, Any]:
    sent: dict[str, Any] = {}

    class _Link:
        def __init__(self, _uri: str, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _Link:
            return self

        async def prepare(self, _slot_id: str, *, options: dict[str, Any]) -> None:
            sent.update(options)

        async def close(self) -> None:
            pass

    monkeypatch.setattr(runner_link, "RunnerLink", _Link)
    await claude_code_runtime.prepare_runtime(
        EnginePreparationContext(
            template=_template(system),
            slot_id="slot-1",
            placement="agent_box",
            sandbox=SimpleNamespace(files=_SandboxFiles()),
            sandbox_id="box-1",
            cwd="/home/slotuser/workspace",
            runtime_identity=_identity(),
            model_access=_model_access(),
            model_credential="slot-placeholder",
            runtime_env={},
            runner_uri="ws://runner.internal",
            preparation_fingerprint="fingerprint-1",
            deployment_settings=_settings(),
        )
    )
    return sent


def _claude_command(wire: dict[str, Any]) -> list[str]:
    """The command line the vendor SDK spawns Claude Code with for these options."""

    options = ClaudeAgentOptions(
        cli_path="/usr/local/bin/claude", system_prompt=wire["system_prompt"]
    )
    return SubprocessCLITransport(prompt="", options=options)._build_command()


@pytest.mark.parametrize("system", [None, "", "   "])
async def test_a_cold_start_without_instructions_runs_claude_codes_own_prompt(
    monkeypatch: pytest.MonkeyPatch, system: str | None
) -> None:
    wire = await _cold_start_options(monkeypatch, system)

    assert wire["system_prompt"] == PRESET
    command = _claude_command(wire)
    # `--system-prompt` of any value replaces Claude Code's own prompt.
    assert "--system-prompt" not in command
    assert "--append-system-prompt" not in command


async def test_a_cold_start_appends_the_agents_instructions_to_the_preset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = await _cold_start_options(monkeypatch, f"  {INSTRUCTIONS}\n")

    assert wire["system_prompt"] == {**PRESET, "append": INSTRUCTIONS}
    command = _claude_command(wire)
    assert "--system-prompt" not in command
    assert command[command.index("--append-system-prompt") + 1] == INSTRUCTIONS


@pytest.mark.parametrize(
    ("system", "expected"),
    [
        (None, {**PRESET, "exclude_dynamic_sections": True}),
        (INSTRUCTIONS, {**PRESET, "append": INSTRUCTIONS, "exclude_dynamic_sections": True}),
    ],
)
async def test_a_prepared_slot_runs_the_same_prompt_with_session_sections_deferred(
    monkeypatch: pytest.MonkeyPatch, system: str | None, expected: dict[str, Any]
) -> None:
    prepared = await _prepared_slot_options(monkeypatch, system)
    cold = await _cold_start_options(monkeypatch, system)

    assert prepared["system_prompt"] == expected
    # The only difference is where the per-Session sections go: a slot has no
    # Session when it spawns, so the vendor moves them to the first input.
    prepared_prompt = dict(prepared["system_prompt"])
    assert prepared_prompt.pop("exclude_dynamic_sections") is True
    assert prepared_prompt == cold["system_prompt"]
    assert "--system-prompt" not in _claude_command(prepared)
