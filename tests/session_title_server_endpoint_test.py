"""Platform labels reach the model gateway the way the AstraBox server does.

Titles and process summaries are completions the server process makes. A
default installation protects the embedded gateway with the credential vault,
which gives sandboxes ``gateway.astrabox.test``: a name only their egress DNS
resolves. The server must use its provider's server-side address and its own
credential instead, and must say so when it has neither. That credential goes
only to the gateway itself: a title base URL anywhere else needs its own key,
and startup refuses one that has none.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.core.service.orchestrator.session_title_service import (
    ConversationTitleModel,
    SessionTitleGenerationError,
    validate_title_model_settings,
)
from astrabox.providers.model import (
    LITELLM_API_KEY_ENV,
    LITELLM_BASE_URL_ENV,
    LITELLM_MASTER_KEY_ENV,
    LITELLM_SERVER_BASE_URL_ENV,
)
from astrabox.seams.model import (
    ModelEndpoint,
    ModelEndpointProvider,
    register_model_endpoint,
)

_SIGNING_SECRET = "test-only-title-signing-secret"
_SERVER_CREDENTIAL = "sk-test-only-server-credential"
#: What ``scripts/install.sh`` configures for DeepSeek, after the entry point
#: normalizes ``ANTHROPIC_MODEL`` to the bundled gateway's ``anthropic/*`` route.
_DEFAULT_MODEL = "anthropic/deepseek-flash"


class _SandboxOnlyProvider(ModelEndpointProvider):
    """A gateway that answers for sandboxes and declares no server endpoint."""

    name = "title-sandbox-only-test"

    def resolve(self, *, requested: ModelEndpoint, settings: Any = None) -> ModelEndpoint:
        _ = settings
        return ModelEndpoint(
            base_url="http://sandbox-only.test",
            api_key="sandbox-only-credential",
            model_name=requested.model_name,
        )


register_model_endpoint(_SandboxOnlyProvider())


def _settings(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "model_api_key": "",
        "model_api_key_secret_name": "",
        "model_base_url": "https://api.deepseek.com/anthropic",
        "model_name": _DEFAULT_MODEL,
        "model_endpoint_provider": "litellm",
        "mcp_proxy_base_url": "http://172.17.0.1:8000",
        "sandbox_credential_vault_enabled": True,
        "title_model_enabled": True,
        "title_model_base_url": "",
        "title_model_name": "",
        "title_model_api_key": "",
        "title_model_api_key_secret_name": "",
        "title_model_max_tokens": 256,
        "title_model_request_timeout_seconds": 60.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture(autouse=True)
def _installation_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRABOX_AUTH_SESSION_SECRET", _SIGNING_SECRET)
    for name in (
        LITELLM_BASE_URL_ENV,
        LITELLM_SERVER_BASE_URL_ENV,
        LITELLM_API_KEY_ENV,
        LITELLM_MASTER_KEY_ENV,
        "ASTRABOX_MODEL_BASE_URL",
        "ASTRABOX_MODEL_NAME",
        "ASTRABOX_MODEL_API_KEY",
        "ASTRABOX_MODEL_API_KEY_SECRET_NAME",
    ):
        monkeypatch.delenv(name, raising=False)


_GATEWAY = "http://litellm.platform:4000"


@pytest.fixture
def _external_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LITELLM_BASE_URL_ENV, "https://gateway.sandbox-dns.internal")
    monkeypatch.setenv(LITELLM_SERVER_BASE_URL_ENV, _GATEWAY)
    monkeypatch.setenv(LITELLM_MASTER_KEY_ENV, _SERVER_CREDENTIAL)


@pytest.mark.parametrize(
    "title_base_url",
    [
        "https://labels.example.test/v1",
        "http://litellm.platform:4001",
        "https://litellm.platform:4000",
        "http://litellm.platform:4000/other-service",
        "http://litellm.platform:not-a-port",
    ],
)
def test_a_title_url_elsewhere_never_receives_the_gateway_credential(
    _external_gateway: None, title_base_url: str
) -> None:
    settings = _settings(title_model_base_url=title_base_url)

    with pytest.raises(SessionTitleGenerationError) as at_request:
        ConversationTitleModel(settings=settings).resolve_request_config()
    with pytest.raises(SessionTitleGenerationError) as at_startup:
        validate_title_model_settings(settings)

    for caught in (at_request, at_startup):
        message = str(caught.value)
        assert "ASTRABOX_TITLE_MODEL_BASE_URL" in message
        assert "ASTRABOX_TITLE_MODEL_API_KEY" in message
        assert _SERVER_CREDENTIAL not in message
