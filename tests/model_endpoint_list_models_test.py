"""Model-enumeration seam: the console's per-environment model dropdown source.

The Agent's `model` is a first-class field, offered as a dropdown fed by the
Environment's gateway (``docs/domain-model.md``). LiteLLM enumerates via the
OpenAI-standard `GET /v1/models`; with no explicit address it targets the
EMBEDDED proxy's loopback. Enumeration is best-effort — a gateway error never
raises.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from astrabox.providers.model import (
    EMBEDDED_LITELLM_PORT,
    EMBEDDED_LITELLM_SANDBOX_HOST,
    LiteLLMModelEndpointProvider,
    LITELLM_BASE_URL_ENV,
    LITELLM_MASTER_KEY_ENV,
    LITELLM_SERVER_BASE_URL_ENV,
)
from astrabox.seams.model import ModelEndpoint


@pytest.fixture(autouse=True)
def _sandbox_inference_signing_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRABOX_AUTH_SESSION_SECRET", "test-only-model-signing-secret")
    monkeypatch.delenv("ASTRABOX_LITELLM_API_KEY", raising=False)


def test_the_bundled_gateway_uses_a_port_the_credential_vault_can_intercept() -> None:
    # OpenSandbox Credential Vault derives the destination port from the scheme
    # (HTTP=80, HTTPS=443). A bundled gateway on 4000 cannot receive a binding.
    assert EMBEDDED_LITELLM_PORT == 80


def test_bundled_wildcards_preserve_each_providers_full_model_id() -> None:
    config = yaml.safe_load((
        Path(__file__).resolve().parents[1] / "containers" / "litellm" / "config.yaml"
    ).read_text(encoding="utf-8"))
    routes = {
        item["model_name"]: item["litellm_params"]["model"]
        for item in config["model_list"]
    }
    assert routes["claude-*"] == "anthropic/claude-*"
    assert routes["anthropic/*"] == "anthropic/*"
    assert routes["gpt-*"] == "openai/gpt-*"
    assert routes["gemini-*"] == "gemini/gemini-*"


def test_protected_embedded_gateway_uses_the_private_fqdn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(LITELLM_BASE_URL_ENV, raising=False)
    endpoint = LiteLLMModelEndpointProvider().resolve(
        requested=ModelEndpoint(model_name="model-a"),
        settings=SimpleNamespace(
            sandbox_credential_vault_enabled=True,
            mcp_proxy_base_url="http://172.17.0.1:8000",
        ),
    )
    assert endpoint.base_url == f"http://{EMBEDDED_LITELLM_SANDBOX_HOST}"
    assert endpoint.model_name == "model-a"


def test_an_unregistered_process_default_cannot_override_the_agents_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_LITELLM_MODEL", "wrong-global-route")

    endpoint = LiteLLMModelEndpointProvider().resolve(
        requested=ModelEndpoint(model_name="agent-selected-route"),
        settings=SimpleNamespace(
            sandbox_credential_vault_enabled=True,
            mcp_proxy_base_url="http://172.17.0.1:8000",
        ),
    )

    assert endpoint.model_name == "agent-selected-route"


def test_unprotected_embedded_gateway_uses_the_direct_platform_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(LITELLM_BASE_URL_ENV, raising=False)
    endpoint = LiteLLMModelEndpointProvider().resolve(
        requested=ModelEndpoint(model_name="model-a"),
        settings=SimpleNamespace(
            sandbox_credential_vault_enabled=False,
            mcp_proxy_base_url="http://172.17.0.1:8000",
        ),
    )
    assert endpoint.base_url == "http://172.17.0.1:80"


def test_litellm_without_a_base_url_targets_the_embedded_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No provider_access base_url and no env → the embedded proxy's loopback
    # (same container / dev host), never a silent [] without asking anyone.
    monkeypatch.delenv(LITELLM_BASE_URL_ENV, raising=False)
    captured: dict[str, str] = {}

    def _fake_get(url: str, headers: dict | None = None, timeout: float | None = None):  # noqa: ANN001
        captured["url"] = url
        return httpx.Response(200, json={"data": []}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", _fake_get)
    assert LiteLLMModelEndpointProvider().list_models(provider_access={}) == []
    assert captured["url"] == f"http://127.0.0.1:{EMBEDDED_LITELLM_PORT}/v1/models"


def test_litellm_control_plane_does_not_resolve_the_sandbox_only_gateway_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The private DNS name belongs to OpenSandbox's egress path. The platform
    process shares the embedded gateway's host namespace and must enumerate it
    over loopback instead.
    """
    monkeypatch.setenv(
        LITELLM_BASE_URL_ENV,
        f"http://{EMBEDDED_LITELLM_SANDBOX_HOST}",
    )
    monkeypatch.setenv(
        LITELLM_SERVER_BASE_URL_ENV,
        "http://127.0.0.1:14000",
    )
    captured: dict[str, str] = {}

    def _fake_get(url: str, headers: dict | None = None, timeout: float | None = None):  # noqa: ANN001
        captured["url"] = url
        return httpx.Response(
            200,
            json={"data": [{"id": "deepseek-v4-flash"}]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "get", _fake_get)

    assert LiteLLMModelEndpointProvider().list_models(provider_access={}) == [
        "deepseek-v4-flash"
    ]
    assert captured["url"] == "http://127.0.0.1:14000/v1/models"


def test_litellm_queries_v1_models_and_returns_sorted_routing_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def _fake_get(url: str, headers: dict | None = None, timeout: float | None = None):  # noqa: ANN001
        seen["url"] = url
        seen["auth"] = (headers or {}).get("Authorization")
        return httpx.Response(
            200,
            json={"data": [{"id": "gpt-4o"}, {"id": "claude-sonnet-4-5"}, {"id": "gpt-4o"}]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "get", _fake_get)
    models = LiteLLMModelEndpointProvider().list_models(
        provider_access={"base_url": "http://litellm:4000/", "api_key": "sk-proxy"}
    )
    # base_url from provider_access, standard /v1/models path, bearer auth, sorted+deduped.
    assert seen["url"] == "http://litellm:4000/v1/models"
    assert seen["auth"] == "Bearer sk-proxy"
    assert models == ["claude-sonnet-4-5", "gpt-4o"]


def test_litellm_model_discovery_can_use_a_server_only_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ASTRABOX_LITELLM_API_KEY", raising=False)
    monkeypatch.delenv(LITELLM_MASTER_KEY_ENV, raising=False)
    monkeypatch.setenv(LITELLM_MASTER_KEY_ENV, "sk-bundled-master")
    captured: dict[str, str | None] = {}

    def _fake_get(url: str, headers: dict | None = None, timeout: float | None = None):  # noqa: ANN001
        captured["auth"] = (headers or {}).get("Authorization")
        return httpx.Response(
            200,
            json={"data": [{"id": "openai/gpt-4o-mini"}]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "get", _fake_get)
    assert LiteLLMModelEndpointProvider().list_models(provider_access={}) == [
        "openai/gpt-4o-mini"
    ]
    assert captured["auth"] == "Bearer sk-bundled-master"


def test_litellm_falls_back_to_env_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LITELLM_BASE_URL_ENV, "http://env-proxy:4000")
    captured: dict[str, str] = {}

    def _fake_get(url: str, headers: dict | None = None, timeout: float | None = None):  # noqa: ANN001
        captured["url"] = url
        return httpx.Response(200, json={"data": [{"id": "m1"}]}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", _fake_get)
    models = LiteLLMModelEndpointProvider().list_models(provider_access={})
    assert captured["url"] == "http://env-proxy:4000/v1/models"
    assert models == ["m1"]


def test_litellm_swallows_gateway_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(url: str, headers: dict | None = None, timeout: float | None = None):  # noqa: ANN001
        raise httpx.ConnectError("proxy down", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", _boom)
    # A dead proxy must not raise — the console just gets no suggestions.
    assert LiteLLMModelEndpointProvider().list_models(
        provider_access={"base_url": "http://down:4000"}
    ) == []
