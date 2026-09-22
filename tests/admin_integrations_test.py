from __future__ import annotations

import pytest

from astrabox.deploy.admin_integrations import (
    CASDOOR_ADMIN_URL_ENV,
    CASDOOR_API_ACCESS_URL_ENV,
    LITELLM_ADMIN_URL_ENV,
    configured_management_links,
)


@pytest.fixture(autouse=True)
def _clear_integration_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ASTRABOX_WEB_IDENTITY",
        "ASTRABOX_MODEL_ENDPOINT_PROVIDER",
        "ASTRABOX_LITELLM_BASE_URL",
        CASDOOR_ADMIN_URL_ENV,
        CASDOOR_API_ACCESS_URL_ENV,
        "ASTRABOX_OIDC_API_CLIENT_ID",
        "ASTRABOX_OIDC_API_CLIENT_SECRET",
        "ASTRABOX_OIDC_API_CLIENT_SECRET_FILE",
        LITELLM_ADMIN_URL_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


def test_bundled_litellm_has_the_protected_same_origin_console() -> None:
    assert configured_management_links() == [
        {
            "id": "litellm",
            "name": "LiteLLM",
            "category": "model_gateway",
            "admin_url": "/litellm",
        }
    ]


def test_unconfigured_or_non_litellm_services_have_no_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_MODEL_ENDPOINT_PROVIDER", "company_gateway")
    assert configured_management_links() == []

    monkeypatch.setenv("ASTRABOX_MODEL_ENDPOINT_PROVIDER", "litellm")
    monkeypatch.setenv("ASTRABOX_LITELLM_BASE_URL", "https://llm.cluster.internal")
    assert configured_management_links() == []


def test_external_service_entries_require_explicit_browser_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_WEB_IDENTITY", "oidc")
    monkeypatch.setenv(CASDOOR_ADMIN_URL_ENV, "https://identity.example.com")
    monkeypatch.setenv("ASTRABOX_LITELLM_BASE_URL", "https://llm.cluster.internal")
    monkeypatch.setenv(LITELLM_ADMIN_URL_ENV, "https://llm.example.com/ui/")

    assert configured_management_links() == [
        {
            "id": "casdoor",
            "name": "Casdoor",
            "category": "identity",
            "admin_url": "https://identity.example.com",
        },
        {
            "id": "litellm",
            "name": "LiteLLM",
            "category": "model_gateway",
            "admin_url": "https://llm.example.com/ui/",
        },
    ]


def test_casdoor_entry_cannot_claim_a_non_oidc_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CASDOOR_ADMIN_URL_ENV, "https://identity.example.com")
    with pytest.raises(ValueError, match="requires ASTRABOX_WEB_IDENTITY=oidc"):
        configured_management_links()


def test_api_access_entry_requires_and_links_the_configured_casdoor_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_WEB_IDENTITY", "oidc")
    monkeypatch.setenv("ASTRABOX_OIDC_API_CLIENT_ID", "astrabox-api")
    monkeypatch.setenv("ASTRABOX_OIDC_API_CLIENT_SECRET_FILE", "/run/secrets/api")
    monkeypatch.setenv(
        CASDOOR_API_ACCESS_URL_ENV,
        "https://identity.example.com/applications/astrabox/astrabox-api",
    )

    assert configured_management_links() == [
        {
            "id": "casdoor-api",
            "name": "AstraBox API",
            "category": "api_access",
            "admin_url": (
                "https://identity.example.com/applications/astrabox/astrabox-api"
            ),
        },
        {
            "id": "litellm",
            "name": "LiteLLM",
            "category": "model_gateway",
            "admin_url": "/litellm",
        },
    ]


def test_api_access_entry_refuses_an_inert_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_WEB_IDENTITY", "oidc")
    monkeypatch.setenv(
        CASDOOR_API_ACCESS_URL_ENV,
        "https://identity.example.com/applications/astrabox/astrabox-api",
    )

    with pytest.raises(ValueError, match="requires the OIDC API client"):
        configured_management_links()


def test_invalid_browser_url_refuses_application_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_ENV_FILE", "/does/not/exist")
    monkeypatch.setenv(CASDOOR_ADMIN_URL_ENV, "javascript:alert(1)")

    from astrabox.api.app import create_app

    with pytest.raises(ValueError, match=CASDOOR_ADMIN_URL_ENV):
        create_app()


def test_bundled_litellm_rejects_a_competing_admin_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LITELLM_ADMIN_URL_ENV, "https://llm.example.com/ui/")
    with pytest.raises(ValueError, match="bundled gateway is managed at /litellm"):
        configured_management_links()
