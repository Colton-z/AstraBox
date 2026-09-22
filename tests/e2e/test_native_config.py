"""Authored JSON reaches each live engine's native configuration destination."""

import httpx
import pytest

from tests.e2e._sandbox_helpers import engine_kind
from tests.e2e.native_config_codex import verify_native_configuration
from tests.e2e.native_config_pi import verify_native_config

pytestmark = pytest.mark.e2e


def test_native_json_configuration_reaches_supplier(e2e_client: httpx.Client) -> None:
    """Read the current engine's actual configuration, not the Agent API echo."""

    from tests.e2e.native_config_claude_dsh import (
        verify_claude_native_configuration,
        verify_dsh_native_configuration,
    )

    checks = {
        "claude_code": verify_claude_native_configuration,
        "deepseek_harness": verify_dsh_native_configuration,
        "codex": verify_native_configuration,
        "pi": verify_native_config,
    }
    checks[engine_kind()](e2e_client)
