"""Pi platform endpoints reach native MCP registration without custom forwarding."""

import json
import os
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from astrabox.core.service.orchestrator.engine.pi import (
    PI_PLATFORM_MCP_PACKAGE_PATH,
    PI_SUBAGENT_PACKAGE_PATH,
    _settings,
    build_pi_rpc_command,
)
from astrabox.core.service.orchestrator.engine.platform_mcp import (
    platform_mcp_servers,
    startup_mcp_deployment_id,
)
from astrabox.core.service.orchestrator.runtime.mcp_servers import prepared_slot_mcp_deployment_id


@pytest.mark.parametrize("prepared", [False, True])
@pytest.mark.parametrize("shared", [False, True])
def test_pi_process_environment_preserves_the_binding_url(prepared: bool, shared: bool) -> None:
    context = SimpleNamespace(
        session_id="session-1", platform_mcp_deployment_id=None,
        prepared_manifest={"slot_id": "slot-1"} if prepared else None,
    )
    url = "http://platform.test/opaque-capability/mcp"
    home = "/home/conversations/conv_1" if shared else "/home/agent"
    template = SimpleNamespace(engine_options={"settings": {"defaultThinkingLevel": "high"}})
    with patch(
        "astrabox.core.service.orchestrator.engine.platform_mcp.runtime_mcp_servers_for_binding",
        return_value={"artifacts": {"type": "http", "url": url}},
    ) as render:
        command = build_pi_rpc_command(
            linux_user="conv_1" if shared else "agent", home=home,
            workspace="/workspace", model="model", isolated=shared,
            settings=_settings(template), platform_mcp_servers=platform_mcp_servers(
                template, deployment_settings=None, deployment_id=startup_mcp_deployment_id(context),
            ),
        )
    inner = shlex.split(command)[-1]
    assert f"PI_CODING_AGENT_DIR={home}/.pi/agent" in shlex.split(command)
    assert 'export ASTRABOX_PLATFORM_MCP_SERVERS=\'[{"name":"artifacts","url":"' + url + '"}]\'' in inner
    assert PI_PLATFORM_MCP_PACKAGE_PATH in inner
    assert render.call_args.kwargs["deployment_id"] == (
        prepared_slot_mcp_deployment_id("slot-1") if prepared else "session-1"
    )


def test_agent_packages_cannot_omit_the_image_platform_extension() -> None:
    native = {
        "packages": ["/user/package"],
        "extensions": ["+builtin:mcp"],
        "defaultTools": ["+codemode", "+tool_search"],
    }
    settings = _settings(SimpleNamespace(engine_options={"settings": native}))
    assert settings["packages"] == ["/user/package", PI_SUBAGENT_PACKAGE_PATH, PI_PLATFORM_MCP_PACKAGE_PATH]
    assert settings["extensions"] == native["extensions"]
    assert settings["defaultTools"] == native["defaultTools"]
    assert native["packages"] == ["/user/package"]


EXTENSION = (
    Path(__file__).resolve().parents[1]
    / "containers/sandbox-pi/astrabox-platform-mcp/index.js"
)
REGISTRATION_DRIVER = """
const { default: register } = await import(process.argv[1]);
const servers = [];
register({
  registerMcpServer(name, config) {
    if (name === 'vendor_rejected') throw new Error('vendor rejected config');
    servers.push({ name, config });
  },
});
process.stdout.write(JSON.stringify(servers));
"""


def _register(payload: str | None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("ASTRABOX_PLATFORM_MCP_SERVERS", None)
    if payload is not None:
        env["ASTRABOX_PLATFORM_MCP_SERVERS"] = payload
    return subprocess.run(
        ["node", "--input-type=module", "-e", REGISTRATION_DRIVER, EXTENSION.as_uri()],
        env=env, capture_output=True, text=True, check=False, timeout=10,
    )


@pytest.mark.parametrize("payload", [None, "[]", json.dumps([
    {"name": "artifacts", "url": "https://platform.test/capability/mcp"},
    {"name": "preview", "url": "https://platform.test/another/mcp"},
])])
def test_endpoints_register_with_native_direct_exposure(payload: str | None) -> None:
    result = _register(payload)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        {"name": entry["name"], "config": {"url": entry["url"], "exposure": "direct"}}
        for entry in json.loads(payload or "[]")
    ]


@pytest.mark.parametrize(("payload", "error"), [
    ("broken-json", "SyntaxError"),
    ("{}", "Platform MCP servers must be an array"),
    ('[null]', "Platform MCP server requires name and URL"),
    ('[{"name":"artifacts"}]', "Platform MCP server requires name and URL"),
    ('[{"name":"vendor_rejected","url":"invalid"}]', "vendor rejected config"),
])
def test_invalid_platform_registration_terminates_startup(payload: str, error: str) -> None:
    result = _register(payload)
    assert result.returncode == 1
    assert "astrabox-platform-mcp registration failed:" in result.stderr
    assert error in result.stderr
    assert result.stdout == ""
