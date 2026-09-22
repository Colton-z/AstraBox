"""Discover browser management surfaces this deployment can actually open."""

from __future__ import annotations

import os
from urllib.parse import urlsplit

CASDOOR_ADMIN_URL_ENV = "ASTRABOX_CASDOOR_ADMIN_URL"
CASDOOR_API_ACCESS_URL_ENV = "ASTRABOX_CASDOOR_API_ACCESS_URL"
LITELLM_ADMIN_URL_ENV = "ASTRABOX_LITELLM_ADMIN_URL"


def _optional_browser_url(name: str, raw_value: str | None) -> str | None:
    value = str(raw_value or "").strip()
    if not value:
        return None
    if value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"{name} is not a valid browser URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError(
            f"{name} must be an http(s) URL without embedded credentials "
            "or a same-origin path beginning with /"
        )
    return value


def configured_management_links() -> list[dict[str, str]]:
    """Return only integrated service consoles this deployment can open.

    The model inference endpoint is not necessarily a browser management
    endpoint: an external LiteLLM address may be cluster-only and may use an
    unrelated login. Likewise, OIDC does not imply Casdoor. Explicit browser
    URLs make those external capabilities visible; the maintained bundled
    services fill them automatically.
    """

    services: list[dict[str, str]] = []

    identity_provider = str(os.getenv("ASTRABOX_WEB_IDENTITY") or "local").strip().lower()
    casdoor_url = _optional_browser_url(
        CASDOOR_ADMIN_URL_ENV,
        os.getenv("ASTRABOX_CASDOOR_ADMIN_URL"),
    )
    if casdoor_url:
        if identity_provider != "oidc":
            raise ValueError(
                f"{CASDOOR_ADMIN_URL_ENV} requires ASTRABOX_WEB_IDENTITY=oidc"
            )
        services.append(
            {
                "id": "casdoor",
                "name": "Casdoor",
                "category": "identity",
                "admin_url": casdoor_url,
            }
        )

    casdoor_api_url = _optional_browser_url(
        CASDOOR_API_ACCESS_URL_ENV,
        os.getenv(CASDOOR_API_ACCESS_URL_ENV),
    )
    if casdoor_api_url:
        if identity_provider != "oidc":
            raise ValueError(
                f"{CASDOOR_API_ACCESS_URL_ENV} requires ASTRABOX_WEB_IDENTITY=oidc"
            )
        api_client_id = str(os.getenv("ASTRABOX_OIDC_API_CLIENT_ID") or "").strip()
        api_client_secret = str(
            os.getenv("ASTRABOX_OIDC_API_CLIENT_SECRET")
            or os.getenv("ASTRABOX_OIDC_API_CLIENT_SECRET_FILE")
            or ""
        ).strip()
        if not api_client_id or not api_client_secret:
            raise ValueError(
                f"{CASDOOR_API_ACCESS_URL_ENV} requires the OIDC API client"
            )
        services.append(
            {
                "id": "casdoor-api",
                "name": "AstraBox API",
                "category": "api_access",
                "admin_url": casdoor_api_url,
            }
        )

    model_provider = str(
        os.getenv("ASTRABOX_MODEL_ENDPOINT_PROVIDER") or "litellm"
    ).strip().lower()
    external_litellm = str(os.getenv("ASTRABOX_LITELLM_BASE_URL") or "").strip()
    litellm_admin_url = _optional_browser_url(
        LITELLM_ADMIN_URL_ENV,
        os.getenv("ASTRABOX_LITELLM_ADMIN_URL"),
    )
    from astrabox.deploy.onebox import needs_litellm_gateway

    if needs_litellm_gateway():
        if litellm_admin_url:
            raise ValueError(
                f"{LITELLM_ADMIN_URL_ENV} is only for an external LiteLLM; "
                "the bundled gateway is managed at /litellm"
            )
        services.append(
            {
                "id": "litellm",
                "name": "LiteLLM",
                "category": "model_gateway",
                "admin_url": "/litellm",
            }
        )
    elif model_provider == "litellm" and external_litellm:
        if litellm_admin_url:
            services.append(
                {
                    "id": "litellm",
                    "name": "LiteLLM",
                    "category": "model_gateway",
                    "admin_url": litellm_admin_url,
                }
            )
    elif litellm_admin_url:
        raise ValueError(
            f"{LITELLM_ADMIN_URL_ENV} requires "
            "ASTRABOX_MODEL_ENDPOINT_PROVIDER=litellm"
        )

    return services


__all__ = [
    "CASDOOR_ADMIN_URL_ENV",
    "CASDOOR_API_ACCESS_URL_ENV",
    "LITELLM_ADMIN_URL_ENV",
    "configured_management_links",
]
