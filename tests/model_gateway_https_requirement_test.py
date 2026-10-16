"""A deployment that requires HTTPS refuses a model gateway the vault cannot secure.

``ASTRABOX_MODEL_GATEWAY_REQUIRE_HTTPS`` says sandboxes reach the model gateway
only over HTTPS, where the egress sidecar can attach the credential without it
crossing the wire in clear. The composition root asks the selected model
provider to validate that before serving: the embedded HTTP gateway, or a
gateway the credential vault cannot bind to, is refused at startup with the
setting named.
"""

from __future__ import annotations

from typing import Any

import pytest

import astrabox.bootstrap as bootstrap_module
from astrabox.providers import model as _model_provider  # noqa: F401
from astrabox.bootstrap import BootstrapConfigError


@pytest.fixture(autouse=True)
def _clean_gateway_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRABOX_AUTH_SESSION_SECRET", "test-only-model-signing-secret")
    monkeypatch.delenv("ASTRABOX_LITELLM_API_KEY", raising=False)


# ── a team gateway can require an HTTPS sandbox-facing endpoint ─────────────


def _model_gateway_settings(*, require_https: bool, vault_enabled: bool = True) -> Any:
    return type(
        "_Settings",
        (),
        {
            "model_endpoint_provider": "litellm",
            "model_gateway_require_https": require_https,
            "sandbox_credential_vault_enabled": vault_enabled,
        },
    )()


def _patch_model_gateway_settings(
    monkeypatch: pytest.MonkeyPatch,
    *,
    require_https: bool,
    vault_enabled: bool = True,
) -> None:
    current = _model_gateway_settings(
        require_https=require_https,
        vault_enabled=vault_enabled,
    )
    monkeypatch.setattr(
        "astrabox.common.utils.settings.load_astrabox_settings",
        lambda: current,
    )


def test_https_requirement_refuses_the_embedded_http_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_model_gateway_settings(monkeypatch, require_https=True)
    monkeypatch.delenv("ASTRABOX_LITELLM_BASE_URL", raising=False)

    with pytest.raises(BootstrapConfigError) as caught:
        bootstrap_module._assert_model_gateway_https_requirement()

    message = str(caught.value)
    assert "ASTRABOX_LITELLM_BASE_URL" in message
    assert "https://" in message


@pytest.mark.parametrize(
    "base_url",
    [
        "http://gateway.example.com",
        "https://gateway.example.com:8443",
        "https://192.0.2.10",
        "https://gateway",
    ],
)
def test_https_requirement_refuses_a_gateway_the_vault_cannot_secure(
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
) -> None:
    _patch_model_gateway_settings(monkeypatch, require_https=True)
    monkeypatch.setenv("ASTRABOX_LITELLM_BASE_URL", base_url)

    with pytest.raises(BootstrapConfigError) as caught:
        bootstrap_module._assert_model_gateway_https_requirement()

    assert base_url in str(caught.value)
