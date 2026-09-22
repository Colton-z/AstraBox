from __future__ import annotations

from types import SimpleNamespace

import pytest

from astrabox.core.service.orchestrator.credential_delivery import (
    credential_delivery_overview,
)


@pytest.mark.parametrize(
    ("local_mode", "identity", "expected"),
    [
        ("1", "local", "local_development"),
        ("", "local", "trusted_private"),
        ("", "oidc", "team"),
        ("1", "jwt", "team"),
    ],
)
def test_delivery_overview_reports_the_audience_without_changing_policy(
    monkeypatch: pytest.MonkeyPatch,
    local_mode: str,
    identity: str,
    expected: str,
) -> None:
    monkeypatch.setenv("ASTRABOX_LOCAL_MODE", local_mode)
    monkeypatch.setenv("ASTRABOX_WEB_IDENTITY", identity)

    overview = credential_delivery_overview(
        settings=SimpleNamespace(sandbox_credential_vault_enabled=True)
    )

    assert overview == {
        "deployment_mode": expected,
        "model_credentials": "egress_placeholder",
        "mcp_credentials": "egress_injection",
        "environment_credentials": "egress_placeholder",
    }


def test_delivery_overview_reports_an_explicitly_disabled_vault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_LOCAL_MODE", "1")
    monkeypatch.setenv("ASTRABOX_WEB_IDENTITY", "local")

    overview = credential_delivery_overview(
        settings=SimpleNamespace(sandbox_credential_vault_enabled=False)
    )

    assert overview["model_credentials"] == "sandbox_environment"
    assert overview["mcp_credentials"] == "unavailable"
    assert overview["environment_credentials"] == "unavailable"
