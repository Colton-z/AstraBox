"""Which platform MCP servers a conversation is given.

Artifact tools bind only where the conversation's provenance and its Agent
allow them, and a channel conversation never acts with an Agent's connectors.
"""

from __future__ import annotations

import pytest

from astrabox.core.service.orchestrator.transcript_capability import mint_platform_mcp_capability
from astrabox.core.service.orchestrator.runtime.mcp_servers import (
    runtime_mcp_servers_for_binding,
    sandbox_mcp_egress_hosts,
)


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch) -> None:
    monkeypatch.setenv("ASTRABOX_TRANSCRIPT_SIGNING_KEY", "test-runtime-mcp-key")


def _artifact_server(base: str, deployment: str) -> dict:
    return {"type": "http", "url": f"{base}/api/v1/platform-mcp/{deployment}/artifacts/{mint_platform_mcp_capability(deployment, 'artifacts')}/mcp"}


@pytest.mark.parametrize("base", ["http://platform.test", ""])
def test_channel_session_never_synthesizes_or_aliases_artifacts(base) -> None:
    servers = runtime_mcp_servers_for_binding({
        "artifacts": {"platform_server": "artifacts"},
        "alias": {"platform_server": "artifacts"},
        "remote": {"type": "http", "url": "https://remote.example/mcp"},
    }, proxy_base_url=base, deployment_id="channel", session_source_type="channel",
        artifacts_enabled=True)
    assert servers == {"remote": {"type": "http", "url": "https://remote.example/mcp"}}


def test_a_channel_conversation_withholds_an_agents_connectors_but_not_an_assistants() -> None:
    """An Agent's connectors act as the caller; an Assistant's as its owner.

    They are withheld where the engine is configured, not from the resolved
    view, so the Agent's runtime generation, computed from the view, is the
    same for a channel conversation as for its other Sessions. The connections
    tools come with the connectors they describe.
    """
    declared = {
        "gmail": {"platform_server": "connector", "hub_server": "s1"},
        "remote": {"type": "http", "url": "https://remote.example/mcp"},
    }

    def engine_servers(**session) -> set[str]:
        return set(runtime_mcp_servers_for_binding(
            declared, proxy_base_url="http://platform.test", deployment_id="session-1",
            artifacts_enabled=True, **session,
        ))

    assert engine_servers(session_source_type="channel", agent_id="agent-1") == {"remote"}
    assert engine_servers(session_source_type="console", agent_id="agent-1") == {
        "gmail", "connections", "artifacts", "remote",
    }
    assert engine_servers(session_source_type="channel", agent_id=None) == {"gmail", "connections", "remote"}
    # The relay is the platform's own route; the sandbox dials no hub.
    assert sandbox_mcp_egress_hosts(declared) == ["remote.example"]


@pytest.mark.parametrize(
    ("source_type", "agent_switch", "bound"),
    [
        ("agent", True, True),
        ("agent", False, False),
        ("channel", True, False),
        ("channel", False, False),
    ],
)
def test_artifacts_bind_only_when_provenance_and_the_agent_switch_allow(
    source_type: str, agent_switch: bool, bound: bool,
) -> None:
    """An Agent whose owner turned Artifacts off gets no route to those tools.

    Every engine renders its platform servers through this function. Dropping
    only the synthesized server would leave an authored alias of it in place,
    which hands the engine the same tools under another name.
    """
    remote = {"type": "http", "url": "https://remote.example/mcp"}
    servers = runtime_mcp_servers_for_binding(
        {"alias": {"platform_server": "artifacts"}, "remote": remote},
        proxy_base_url="http://platform.test",
        deployment_id="session-1",
        session_source_type=source_type,
        artifacts_enabled=agent_switch,
    )
    alias_url = (
        "http://platform.test/api/v1/platform-mcp/session-1/alias/"
        f"{mint_platform_mcp_capability('session-1', 'alias')}/mcp"
    )
    assert servers == (
        {
            "alias": {"type": "http", "url": alias_url},
            "remote": remote,
            "artifacts": _artifact_server("http://platform.test", "session-1"),
        }
        if bound
        else {"remote": remote}
    )
