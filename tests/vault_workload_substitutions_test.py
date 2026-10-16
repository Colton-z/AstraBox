"""Only the model gateway binding carries workload bearers; an MCP binding never splices
one in.
"""

from __future__ import annotations

from types import SimpleNamespace

from astrabox.providers.open_sandbox.credential_vault import (
    build_vault_write,
    carry_workload_substitutions,
)
from astrabox.seams.egress_credentials import (
    workload_credential_name,
    workload_model_placeholder,
)

GATEWAY = "https://gateway.example.com"


def placeholders_of(binding: object) -> set[str]:
    auth = getattr(binding, "auth", None)
    return {
        str(getattr(item, "placeholder", ""))
        for item in (getattr(auth, "substitutions", None) or [])
    }


def test_only_the_model_gateway_binding_carries_workload_bearers() -> None:
    """An MCP binding must not splice a credential into a matching string."""
    _credentials, mcp_bindings = build_vault_write(
        credential="sk-mcp-gateway",
        credential_header="authorization",
        base_url=GATEWAY,
        request_paths=("/bright_data/mcp",),
        name="astrabox-mcp-gateway-litellm",
    )
    completed = carry_workload_substitutions(
        mcp_bindings,
        [SimpleNamespace(name=workload_credential_name("slot-aaa"))],
    )
    assert workload_model_placeholder("slot-aaa") not in placeholders_of(completed[0])
