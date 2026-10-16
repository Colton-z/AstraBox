"""An Agent's Codex options cannot redirect the platform's MCP route."""

from types import SimpleNamespace

import pytest

from astrabox.core.service.orchestrator.engine.codex import _engine_option


@pytest.mark.parametrize("key", ["mcp_servers", "mcp_servers.artifacts.url"])
def test_user_cannot_override_platform_mcp_config(key: str) -> None:
    with pytest.raises(ValueError, match="managed by AstraBox"):
        _engine_option(SimpleNamespace(engine_options={"config": {key: {}}}), "config")
