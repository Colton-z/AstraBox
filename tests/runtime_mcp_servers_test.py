"""The runtime MCP server contract: which definitions the sandbox dials itself."""

from __future__ import annotations

from astrabox.core.service.orchestrator.runtime.mcp_servers import (
    is_platform_mcp_server,
    runtime_mcp_servers_for_binding,
    sandbox_mcp_egress_hosts,
    validate_direct_mcp_servers,
)


def test_a_platform_capability_terminates_on_this_server() -> None:
    """Only a named platform server routes back here.

    `expose_port` publishes a port of the box that is asking, so answering it
    means acting on that sandbox — something no upstream can do. The route
    carries the deployment id because the answer depends on who called.
    """
    config = {"platform_server": "html_preview"}

    assert is_platform_mcp_server(config)
    assert runtime_mcp_servers_for_binding(
        {"preview": config},
        proxy_base_url="http://172.17.0.1:8000",
        deployment_id="mcp_abc123",
    ) == {
        "preview": {
            "type": "http",
            "url": "http://172.17.0.1:8000/api/v1/platform-mcp/mcp_abc123/preview/mcp",
        }
    }


def test_a_server_without_a_platform_name_is_dialled_by_the_sandbox() -> None:
    """Everything else is someone else's server, whatever its address.

    A remote HTTPS endpoint and an in-box process differ in where they run, not
    in who connects: the engine's own MCP client does, and AstraBox writes the
    URL and opens the egress. There is no third kind for a definition to be.
    """
    remote = {"type": "http", "url": "https://mcp.example.test/mcp"}

    assert not is_platform_mcp_server(remote)
    assert runtime_mcp_servers_for_binding(
        {"remote": remote},
        proxy_base_url="http://astrabox.test",
        deployment_id="session-1",
    ) == {"remote": {"type": "http", "url": "https://mcp.example.test/mcp"}}


def test_every_remote_host_the_sandbox_dials_enters_the_allowlist() -> None:
    """A host the box must reach is a host egress must admit.

    Loopback is excluded because that server runs inside the box; nothing else
    is, since the sandbox now opens every one of these connections itself and a
    host missing from the allowlist is a call that cannot leave.
    """
    assert sandbox_mcp_egress_hosts(
        {
            "public": {"type": "http", "url": "https://MCP.KeyVex.com/mcp"},
            "gateway": {"type": "http", "url": "https://gateway.example.test/x/mcp"},
            "local": {"type": "http", "url": "http://127.0.0.1:9000/mcp"},
            "platform": {"platform_server": "html_preview"},
        }
    ) == ["mcp.keyvex.com", "gateway.example.test"]


def test_a_stdio_server_contributes_no_egress_host() -> None:
    """A process the engine starts in the box is not dialled over the network.

    Requiring a URL from every non-platform server refused the whole session:
    an agent carrying one stdio MCP server reached TERMINATED with "requires an
    http(s) URL with a host", which is a real conversation lost to a server that
    was never going to leave the sandbox.
    """
    assert sandbox_mcp_egress_hosts(
        {
            "files": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"],
            },
            "remote": {"type": "http", "url": "https://mcp.example.test/mcp"},
        }
    ) == ["mcp.example.test"]


def test_vendor_stdio_definition_stays_inside_the_sandbox() -> None:
    config = {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"],
        "env": {"LOG_LEVEL": "error"},
    }

    assert not is_platform_mcp_server(config)
    expected = {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"],
        "env": {"LOG_LEVEL": "error"},
    }
    assert runtime_mcp_servers_for_binding(
        {"filesystem": config},
        proxy_base_url="http://astrabox.test",
        deployment_id="session-1",
    ) == {"filesystem": expected}
    assert validate_direct_mcp_servers({"filesystem": config}) == {"filesystem": expected}


def test_direct_http_definition_preserves_vendor_headers() -> None:
    assert runtime_mcp_servers_for_binding(
        {
            "public": {
                "type": "http",
                "url": "https://mcp.example.test/mcp",
                "headers": {"X-Tenant": "public"},
            }
        },
        proxy_base_url="http://astrabox.test",
        deployment_id="session-1",
    ) == {
        "public": {
            "type": "http",
            "url": "https://mcp.example.test/mcp",
            "headers": {"X-Tenant": "public"},
        }
    }
