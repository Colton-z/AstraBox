"""Native JSON entry point and platform-owned Claude transport boundaries."""

from __future__ import annotations

import dataclasses
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions


CLAUDE_PLATFORM_OPTION_KEYS: frozenset[str] = frozenset({
    "cwd", "resume", "fork_session", "permission_mode", "system_prompt", "tools",
    "setting_sources", "include_partial_messages", "mcp_servers",
})

# The standalone runner owns these live handles and session identity fields.
CLAUDE_RUNNER_CONTROLLED_KEYS: frozenset[str] = frozenset({
    "can_use_tool", "cli_path", "continue_conversation", "debug_stderr", "hooks",
    "permission_prompt_tool_name", "session_id", "session_store",
    "session_store_flush", "stderr", "user",
})

CLAUDE_ENGINE_OPTIONS_SCHEMA: tuple[dict[str, Any], ...] = ({
    "key": "sdk_options",
    "type": "object",
    "label": "Claude SDK options",
    "protected_keys": sorted(CLAUDE_PLATFORM_OPTION_KEYS | CLAUDE_RUNNER_CONTROLLED_KEYS),
    "help": (
        "Overrides ClaudeAgentOptions keys directly. Nested settings is a JSON "
        "object passed to the CLI --settings node. Fields are defined by the "
        "installed Claude SDK, not AstraBox. Session identity, transport, "
        "workspace, permissions, system prompt and platform MCP wiring remain "
        "platform-managed. Platform environment and replay/debug CLI flags cannot "
        "be overridden. Native settings are retained; platform Plugin MCP "
        "restrictions are appended to settings.deniedMcpServers."
    ),
},)

CLAUDE_ENGINE_OPTION_KEYS: frozenset[str] = frozenset({"sdk_options"})

# Wire serialization follows the installed supplier type, not the editor schema.
CLAUDE_WIRE_OPTION_KEYS: frozenset[str] = frozenset(
    field.name for field in dataclasses.fields(ClaudeAgentOptions)
) - CLAUDE_RUNNER_CONTROLLED_KEYS
