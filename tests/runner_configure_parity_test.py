"""Configure parity — declared SDK options cross the runner wire.

The adapter selects the fields the host serializes from ``ClaudeAgentOptions``.
The standalone runner checks those names against the SDK installed in its own
image and refuses unknown or runner-controlled fields loudly.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.claude_code_options import (
    CLAUDE_ENGINE_OPTION_KEYS,
    CLAUDE_WIRE_OPTION_KEYS,
)
from astrabox.core.service.orchestrator.engine.claude_code_runtime import (
    _disable_plugin_mcp_autostart,
    _runner_configure_options,
)
from astrabox.core.service.orchestrator.engine.claude_code_config import (
    apply_claude_engine_options,
    build_claude_options_kwargs,
)
from astrabox.core.service.orchestrator.sandbox_runner import (
    RunnerProtocolError,
    _DeferredHttpStoreTarget,
    _HttpStoreTarget,
    _real_session_factory,
)


def test_native_sdk_json_reaches_supplier_and_runner_without_field_schema() -> None:
    native = {
        "max_turns": 7,
        "output_format": {"type": "json_schema", "schema": {"type": "object"}},
        "settings": {"futureVendorSetting": {"nested": [1, True, "value"]}},
    }
    kwargs: dict[str, Any] = {}
    apply_claude_engine_options(kwargs, SimpleNamespace(engine_options={"sdk_options": native}))
    options = ClaudeAgentOptions(**kwargs)
    options = _disable_plugin_mcp_autostart(options, ["plugin:search"])
    wire = _runner_configure_options(options)
    assert wire["max_turns"] == 7
    assert wire["output_format"] == native["output_format"]
    settings = json.loads(wire["settings"])
    assert settings["futureVendorSetting"] == native["settings"]["futureVendorSetting"]
    assert settings["deniedMcpServers"] == [{"serverName": "plugin:search"}]


def test_unknown_native_sdk_option_is_rejected_by_supplier_constructor() -> None:
    kwargs: dict[str, Any] = {}
    apply_claude_engine_options(
        kwargs, SimpleNamespace(engine_options={"sdk_options": {"not_a_vendor_option": 1}})
    )
    assert kwargs["not_a_vendor_option"] == 1
    with pytest.raises(TypeError, match="not_a_vendor_option"):
        ClaudeAgentOptions(**kwargs)


def test_native_json_cannot_replace_platform_session_identity() -> None:
    with pytest.raises(APIError, match="platform-managed"):
        apply_claude_engine_options(
            {}, SimpleNamespace(engine_options={"sdk_options": {"session_id": "other"}})
        )


class _FakeLink:
    def is_connected(self) -> bool:
        return True

    async def send(self, frame: dict[str, Any]) -> bool:
        return True


def _opening(options: dict[str, Any]) -> dict[str, Any]:
    return {
        "slot_id": "slot-1",
        "options": options,
    }


def test_the_host_never_offers_a_field_the_runner_controls() -> None:
    """The two sides agree by test, because they cannot agree by import.

    The runner ships standalone into the sandbox image and may import nothing
    from the host package graph, so its list of self-composed SDK fields and
    the adapter's list of configurable ones are two declarations of one
    boundary. Nothing but this holds them together: widening the adapter's
    declaration to derive from the SDK offered `continue_conversation`, `user`
    and the approval bridge — every one of them a field the runner rejects the
    configure frame for, so the Agent saved cleanly and died at the first turn.
    """
    from astrabox.core.service.orchestrator.engine.claude_code_options import (
        CLAUDE_RUNNER_CONTROLLED_KEYS,
    )
    from astrabox.core.service.orchestrator.sandbox_runner import (
        _RUNNER_CONTROLLED_SDK_OPTION_KEYS,
    )
    assert CLAUDE_RUNNER_CONTROLLED_KEYS == _RUNNER_CONTROLLED_SDK_OPTION_KEYS
    # And the declaration honours it: neither the Agent form nor the wire may
    # carry one.
    assert not (CLAUDE_ENGINE_OPTION_KEYS & _RUNNER_CONTROLLED_SDK_OPTION_KEYS)
    assert not (CLAUDE_WIRE_OPTION_KEYS & _RUNNER_CONTROLLED_SDK_OPTION_KEYS)


def test_host_serializer_carries_sdk_fields_and_drops_host_only() -> None:
    options = SimpleNamespace(
        cwd="/w",
        system_prompt="be terse",
        allowed_tools=["Read", "Bash"],
        max_turns=7,
        strict_mcp_config=True,
        settings='{"disabledMcpjsonServers":["vendor-a"]}',
        setting_sources=["project", "user"],
        env={"X": "1"},
        permission_mode="default",
        # host-only concerns that must never reach the wire:
        hooks={"PreToolUse": [object()]},
        can_use_tool=object(),
        permission_prompt_tool_name="host-stdio",
        session_store=object(),
    )
    wire = _runner_configure_options(options)
    assert wire["system_prompt"] == "be terse"
    assert wire["allowed_tools"] == ["Read", "Bash"]
    assert wire["max_turns"] == 7
    assert wire["strict_mcp_config"] is True
    assert wire["settings"] == '{"disabledMcpjsonServers":["vendor-a"]}'
    assert wire["env"] == {"X": "1"}
    assert "hooks" not in wire
    assert "can_use_tool" not in wire
    assert "permission_prompt_tool_name" not in wire
    assert "session_store" not in wire


def test_host_serializer_refuses_unserializable_values() -> None:
    options = SimpleNamespace(system_prompt=object())
    with pytest.raises(APIError, match="not wire-serializable"):
        _runner_configure_options(options)


def test_runner_applies_allowlisted_options_onto_the_sdk(
    monkeypatch: Any, tmp_path: Any
) -> None:
    captured: dict[str, Any] = {}

    class _CapturingClient:
        def __init__(self, *, options: Any) -> None:
            captured["options"] = options

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", _CapturingClient)
    # A real, creatable path: building the client also prepares the working
    # directory (the runner owns the spawn, so it owns the cwd's existence).
    cwd = str(tmp_path / "w")
    session = _real_session_factory(
        _opening({
            "cwd": cwd,
            "system_prompt": "be terse",
            "allowed_tools": ["Read"],
            "max_turns": 3,
            "strict_mcp_config": True,
            "settings": '{"disabledMcpjsonServers":["vendor-a"]}',
            "interaction_wait_s": 9,
            "env": {
                "E2E_BOUND_TOKEN": "ASTRABOX-VAULT-CRED::vcr-test::nonce"
            },
        }),
        _FakeLink(),
    )
    session._client_factory(session.broker)  # build the SDK client
    options = captured["options"]
    assert options.cli_path == "/usr/local/bin/claude"
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
    transport = SubprocessCLITransport(prompt="", options=options)
    assert transport._cli_path == "/usr/local/bin/claude"
    assert options.cwd == cwd
    assert options.system_prompt == "be terse"
    assert options.allowed_tools == ["Read"]
    assert options.max_turns == 3
    assert options.strict_mcp_config is True
    assert options.settings == '{"disabledMcpjsonServers":["vendor-a"]}'
    assert options.env == {
        "E2E_BOUND_TOKEN": "ASTRABOX-VAULT-CRED::vcr-test::nonce"
    }
    assert options.permission_mode == "default"  # defaulted, not dropped
    assert options.session_store is not None  # runner-owned, always attached
    assert options.session_store_flush == "eager"  # pre-Result history is durable
    assert options.hooks  # the broker's PreToolUse gate, runner-owned


async def test_resume_store_is_readable_when_the_sdk_connects(
    monkeypatch: Any,
    tmp_path: Any,
) -> None:
    captured: dict[str, Any] = {}

    async def post(
        _target: _HttpStoreTarget,
        path: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        captured["path"] = path
        captured["key"] = payload["key"]
        return {
            "data": {
                "entries": [{"type": "user", "uuid": "durable-entry"}],
                "store_sequence": 7,
            }
        }

    class _ResumeReadingClient:
        def __init__(self, *, options: Any) -> None:
            self.options = options

        async def connect(self) -> None:
            key = {"project_key": "-work", "session_id": "sdk-session"}
            captured["loaded"] = await self.options.session_store.load(key)

        async def disconnect(self) -> None:
            pass

    import claude_agent_sdk

    monkeypatch.setattr(_HttpStoreTarget, "_post", post)
    monkeypatch.setattr(
        claude_agent_sdk,
        "ClaudeSDKClient",
        _ResumeReadingClient,
    )
    monkeypatch.setenv("ASTRABOX_RUNNER_SPOOL_DIR", str(tmp_path / "spool"))
    session = _real_session_factory(
        _opening({
            "cwd": str(tmp_path / "work"),
            "resume": "sdk-session",
            "resume_transcript": {
                "platform_session_id": "platform-session",
                "store": {"base_url": "http://platform.internal"},
            },
        }),
        _FakeLink(),
    )

    try:
        await session.prepare()
    finally:
        await session.stop()

    assert captured["loaded"] == [
        {"type": "user", "uuid": "durable-entry"}
    ]
    assert captured["path"] == "/api/v1/transcript/platform-session/load"
    assert captured["key"]["session_id"] == "sdk-session"


def test_runner_refuses_resume_without_a_platform_transcript_source() -> None:
    with pytest.raises(
        RunnerProtocolError,
        match="resume requires resume_transcript",
    ):
        _real_session_factory(
            _opening({"cwd": "/work", "resume": "sdk-session"}),
            _FakeLink(),
        )


async def test_resume_store_refuses_writes_until_matching_activation(
    monkeypatch: Any,
) -> None:
    requests: list[str] = []

    async def post(
        _target: _HttpStoreTarget,
        path: str,
        _payload: dict[str, Any],
    ) -> dict[str, Any]:
        requests.append(path)
        if path.endswith("/load"):
            return {
                "data": {
                    "entries": [{"type": "user", "uuid": "durable-entry"}],
                    "store_sequence": 7,
                }
            }
        return {"data": {"store_sequence": 8}}

    monkeypatch.setattr(_HttpStoreTarget, "_post", post)
    target = _DeferredHttpStoreTarget()
    store = {"base_url": "http://platform.internal"}
    key = {"project_key": "-work", "session_id": "sdk-session"}
    target.bind_resume_source("platform-session", store)

    assert await target.load(key) == [
        {"type": "user", "uuid": "durable-entry"}
    ]
    with pytest.raises(
        RunnerProtocolError,
        match="cannot flush before Session activation",
    ):
        await target.flush(key, [{"type": "assistant"}], "append-1")
    with pytest.raises(
        RunnerProtocolError,
        match="does not match resume source",
    ):
        target.activate("different-session", store)

    target.activate("platform-session", store)
    await target.flush(key, [{"type": "assistant"}], "append-1")

    assert requests == [
        "/api/v1/transcript/platform-session/load",
        "/api/v1/transcript/platform-session/append",
    ]


def test_runner_accepts_a_field_declared_by_its_installed_sdk(
    monkeypatch: Any, tmp_path: Any
) -> None:
    captured: dict[str, Any] = {}

    class _CapturingClient:
        def __init__(self, *, options: Any) -> None:
            captured["options"] = options

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", _CapturingClient)
    session = _real_session_factory(
        _opening({
            "cwd": str(tmp_path / "w"),
            "max_thinking_tokens": 2048,
        }),
        _FakeLink(),
    )
    session._client_factory(session.broker)

    assert captured["options"].max_thinking_tokens == 2048


def test_json_sdk_structures_cross_the_wire_and_recover_vendor_dataclasses(
    monkeypatch: Any, tmp_path: Any
) -> None:
    from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions

    captured: dict[str, Any] = {}

    class _CapturingClient:
        def __init__(self, *, options: Any) -> None:
            captured["options"] = options

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", _CapturingClient)
    output_format = {
        "type": "json_schema",
        "schema": {"type": "object", "properties": {"answer": {"type": "string"}}},
    }
    host_options = ClaudeAgentOptions(
        cwd=str(tmp_path / "w"),
        output_format=output_format,
        agents={
            "reviewer": AgentDefinition(
                description="Reviews the proposed change",
                prompt="Review the change for correctness.",
                maxTurns=3,
            )
        },
    )

    wire = _runner_configure_options(host_options)

    assert wire["output_format"] == output_format
    reviewer_wire = wire["agents"]["reviewer"]
    assert reviewer_wire["description"] == "Reviews the proposed change"
    assert reviewer_wire["prompt"] == "Review the change for correctness."
    assert reviewer_wire["maxTurns"] == 3

    session = _real_session_factory(_opening(wire), _FakeLink())
    session._client_factory(session.broker)
    options = captured["options"]
    assert options.output_format == output_format
    assert isinstance(options.agents["reviewer"], AgentDefinition)
    assert options.agents["reviewer"].maxTurns == 3


def test_runner_rejects_a_json_structure_that_cannot_form_the_sdk_dataclass(
    monkeypatch: Any, tmp_path: Any
) -> None:
    class _CapturingClient:
        def __init__(self, *, options: Any) -> None:
            self.options = options

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", _CapturingClient)
    session = _real_session_factory(
        _opening(
            {
                "cwd": str(tmp_path / "w"),
                "agents": {"reviewer": {"description": "Missing prompt"}},
            }
        ),
        _FakeLink(),
    )

    with pytest.raises(RunnerProtocolError, match=r"options\.agents\.reviewer"):
        session._client_factory(session.broker)


def test_default_claude_code_tools_preset_crosses_the_runner_wire(
    monkeypatch: Any, tmp_path: Any
) -> None:
    captured: dict[str, Any] = {}

    class _CapturingClient:
        def __init__(self, *, options: Any) -> None:
            captured["options"] = options

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", _CapturingClient)
    settings = SimpleNamespace(
        remote_agent_include_partial_messages=True,
        remote_agent_max_buffer_size=32 * 1024 * 1024,
        mcp_proxy_base_url="",
    )
    template = SimpleNamespace(
        name="default-tools",
        engine_options={"sdk_options": {"strict_mcp_config": True}},
        plugin_repos=[],
        mcp_servers={},
        skills=[],
    )
    from claude_agent_sdk import ClaudeAgentOptions

    host_options = ClaudeAgentOptions(
        **build_claude_options_kwargs(
            settings,
            template,
            cwd=str(tmp_path / "w"),
            permission_mode="default",
        )
    )

    wire = _runner_configure_options(host_options)
    assert wire["tools"] == {"type": "preset", "preset": "claude_code"}
    assert wire["strict_mcp_config"] is True

    session = _real_session_factory(_opening(wire), _FakeLink())
    session._client_factory(session.broker)
    assert captured["options"].tools == {
        "type": "preset",
        "preset": "claude_code",
    }
    assert captured["options"].strict_mcp_config is True


def test_workload_identity_is_process_environment_not_an_sdk_user_switch() -> None:
    settings = SimpleNamespace(
        remote_agent_include_partial_messages=True,
        remote_agent_max_buffer_size=32 * 1024 * 1024,
        mcp_proxy_base_url="",
    )
    template = SimpleNamespace(
        name="identity-probe",
        engine_options={},
        plugin_repos=[],
        mcp_servers={},
        skills=[],
    )
    identity = {
        "linux_user": "agent",
        "home_dir": "/home/agent",
        "workspace_dir": "/workspace",
        "workspace_source_dir": "/workspace",
        "config_dir": "/home/agent/.claude",
        "sandbox_tenancy": "conversation",
    }

    kwargs = build_claude_options_kwargs(
        settings,
        template,
        cwd="/workspace",
        session_id="sess-1",
        permission_mode="default",
        runtime_identity=identity,
    )

    assert "user" not in kwargs, (
        "the runner already owns the workload uid; a child-only SDK switch "
        "would split the session store from the CLI"
    )
    assert kwargs["env"]["USER"] == "agent"
    assert kwargs["env"]["HOME"] == "/home/agent"


async def test_runner_callback_materializes_the_vendor_permission_prompt_argv(
    monkeypatch: Any, tmp_path: Any
) -> None:
    """The real pinned SDK must turn the runner callback into the CLI channel."""
    from claude_agent_sdk import ClaudeSDKClient
    from claude_agent_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport,
    )

    class _ArgvCaptured(Exception):
        pass

    captured: dict[str, Any] = {}

    async def capture_connect(transport: Any) -> None:
        # Stop immediately before spawn, but use the SDK transport's real argv
        # builder after ClaudeSDKClient has materialized can_use_tool as stdio.
        transport._cli_path = "/vendor/claude"
        captured["argv"] = transport._build_command()
        raise _ArgvCaptured

    monkeypatch.setattr(SubprocessCLITransport, "connect", capture_connect)
    session = _real_session_factory(
        _opening({
            "cwd": str(tmp_path / "work"),
            "tools": {"type": "preset", "preset": "claude_code"},
            "strict_mcp_config": True,
            "permission_mode": "default",
        }),
        _FakeLink(),
    )
    client = session._client_factory(session.broker)
    assert isinstance(client, ClaudeSDKClient)
    assert client.options.can_use_tool == session.broker.can_use_tool
    assert client.options.permission_prompt_tool_name is None, (
        "the SDK owns stdio materialization; setting both options is invalid"
    )

    with pytest.raises(_ArgvCaptured):
        await client.connect()

    argv = captured["argv"]
    assert argv.count("--tools") == 1
    assert argv[argv.index("--tools") + 1] == "default"
    assert argv.count("--permission-prompt-tool") == 1
    assert argv[argv.index("--permission-prompt-tool") + 1] == "stdio"
    assert argv.count("--strict-mcp-config") == 1


async def test_plugin_and_agent_mcp_configuration_cross_the_vendor_argv_unchanged(
    monkeypatch: Any, tmp_path: Any
) -> None:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    from claude_agent_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport,
    )

    class _ArgvCaptured(Exception):
        pass

    captured: dict[str, Any] = {}

    async def capture_connect(transport: Any) -> None:
        transport._cli_path = "/vendor/claude"
        captured["argv"] = transport._build_command()
        raise _ArgvCaptured

    monkeypatch.setattr(SubprocessCLITransport, "connect", capture_connect)
    host_options = ClaudeAgentOptions(
        cwd=str(tmp_path / "work"),
        plugins=[{"type": "local", "path": "/plugins/financial-analysis"}],
        mcp_servers={
            "keyvex": {"type": "http", "url": "https://mcp.keyvex.com"}
        },
        strict_mcp_config=True,
    )
    wire = _runner_configure_options(host_options)
    session = _real_session_factory(_opening(wire), _FakeLink())
    client = session._client_factory(session.broker)
    assert isinstance(client, ClaudeSDKClient)

    with pytest.raises(_ArgvCaptured):
        await client.connect()

    argv = captured["argv"]
    assert "--settings" not in argv
    assert argv[argv.index("--mcp-config") + 1] == (
        '{"mcpServers": {"keyvex": {"type": "http", '
        '"url": "https://mcp.keyvex.com"}}}'
    )
    assert argv[argv.index("--plugin-dir") + 1] == "/plugins/financial-analysis"


async def test_background_terminal_mirrors_without_waiting_for_followup_result(
    monkeypatch: Any, tmp_path: Any
) -> None:
    """A post-Result task terminal reaches the spool as soon as the CLI mirrors it."""
    captured: dict[str, Any] = {}

    class _CapturingClient:
        def __init__(self, *, options: Any) -> None:
            captured["options"] = options

    import claude_agent_sdk
    from claude_agent_sdk._internal.session_resume import build_mirror_batcher

    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", _CapturingClient)
    monkeypatch.setenv("ASTRABOX_RUNNER_SPOOL_DIR", str(tmp_path / "spool"))
    config_dir = tmp_path / "claude"
    session = _real_session_factory(
        _opening({
            "cwd": str(tmp_path / "work"),
            "env": {"CLAUDE_CONFIG_DIR": str(config_dir)},
        }),
        _FakeLink(),
    )
    session._client_factory(session.broker)
    options = captured["options"]
    store = options.session_store
    appended = asyncio.Event()
    observed: list[dict[str, Any]] = []
    original_append = store.append

    async def record_append(
        key: dict[str, Any], entries: list[dict[str, Any]]
    ) -> None:
        await original_append(key, entries)
        observed.extend(entries)
        appended.set()

    monkeypatch.setattr(store, "append", record_append)
    mirror_errors: list[str] = []

    async def record_mirror_error(_key: Any, error: str) -> None:
        mirror_errors.append(error)

    batcher = build_mirror_batcher(
        store=store,
        materialized=None,
        env=options.env,
        on_error=record_mirror_error,
        flush_mode=options.session_store_flush,
    )
    terminal_entry = {
        "type": "user",
        "origin": {"kind": "task-notification"},
        "message": {
            "role": "user",
            "content": (
                "<task-notification><task-id>agent-1</task-id>"
                "<status>completed</status></task-notification>"
            ),
        },
    }
    transcript_path = config_dir / "projects" / "-work" / "sess-1.jsonl"

    batcher.enqueue(str(transcript_path), [terminal_entry])
    try:
        await asyncio.wait_for(appended.wait(), timeout=0.5)
    finally:
        await batcher.close()

    assert mirror_errors == []
    assert observed == [terminal_entry], "the vendor terminal must be spooled verbatim"
    assert store.pending_batch_count() == 1, (
        "background settlement cannot wait for a later ResultMessage"
    )


def test_runner_refuses_unknown_option_keys() -> None:
    with pytest.raises(RunnerProtocolError, match="unknown keys"):
        _real_session_factory(
            _opening({"cwd": "/w", "tool_permission_handler": "nope"}),
            _FakeLink(),
        )


@pytest.mark.parametrize(
    "key",
    [
        # Exact durable resume is the platform contract. Letting a host ask
        # Claude for whichever conversation is "most recent" can bind the
        # wrong user's context after a runtime replacement.
        "continue_conversation",
        "hooks",
        "session_store",
        "user",
    ],
)
def test_runner_refuses_runner_controlled_option_keys(key: str) -> None:
    with pytest.raises(RunnerProtocolError, match="runner-controlled keys"):
        _real_session_factory(
            _opening({"cwd": "/w", key: "nope"}),
            _FakeLink(),
        )
