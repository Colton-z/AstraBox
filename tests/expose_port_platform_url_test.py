"""The expose-port control plane returns OpenSandbox's native browser URL."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.settings import AstraBoxRuntimeSettings
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator.expose_port_service import ExposePortService
from astrabox.seams.sandbox import SandboxBrowserEndpoint


class _Repo:
    def __init__(self, value: dict | None) -> None:
        self.value = value

    async def get_binding(self, _key: str) -> dict | None:
        return self.value

    async def get_session(self, _key: str) -> dict | None:
        return self.value


def _service(session: dict | None = None, binding: dict | None = None) -> ExposePortService:
    return ExposePortService(
        sessions_repo=_Repo(session),
        binding_repo=_Repo(binding),
    )


def test_runtime_settings_refuse_signed_urls_through_the_server_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_SANDBOX_SECURE_ACCESS", "true")
    monkeypatch.setenv("ASTRABOX_SANDBOX_ENDPOINT_VIA_SERVER_PROXY", "true")

    with pytest.raises(ValueError, match="cannot be combined"):
        AstraBoxRuntimeSettings()


def test_runtime_settings_normalize_the_native_endpoint_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_SANDBOX_ENDPOINT_SCHEME", "HTTPS://")

    assert AstraBoxRuntimeSettings().sandbox_endpoint_scheme == "https"


@pytest.mark.asyncio
async def test_local_docker_url_is_returned_without_an_astrabox_data_proxy() -> None:
    provider = SimpleNamespace(
        resolve_browser_endpoint=AsyncMock(
            return_value=SandboxBrowserEndpoint(endpoint="http://127.0.0.1:43123/proxy/5173")
        )
    )
    service = _service(
        session={
            "sandbox_id": "sb-1",
            "sandbox_backend": "open_sandbox",
            "user_id": "user-1",
        }
    )
    with (
        patch(
            "astrabox.core.service.orchestrator.expose_port_service.sandbox_for_name",
            return_value=provider,
        ),
        patch(
            "astrabox.core.service.orchestrator.expose_port_service.load_astrabox_settings",
            return_value=SimpleNamespace(sandbox_secure_access_enabled=False),
        ),
    ):
        result = await service.expose_port(deployment_id="session-1", port=5173)

    assert result["url"] == "http://127.0.0.1:43123/proxy/5173"
    assert "/api/v1/exposed-ports/" not in result["url"]
    assert result["access"] == "native"
    assert result["expires_at"] is None


@pytest.mark.asyncio
async def test_secure_access_requests_a_signed_url_with_the_configured_ttl() -> None:
    provider = SimpleNamespace(
        resolve_browser_endpoint=AsyncMock(
            return_value=SandboxBrowserEndpoint(
                endpoint="https://gateway.example/sb-1/5173/abc/signature",
                signed=True,
            )
        )
    )
    service = _service(session={"sandbox_id": "sb-1", "user_id": "user-1"})
    with (
        patch(
            "astrabox.core.service.orchestrator.expose_port_service.sandbox_for_name",
            return_value=provider,
        ),
        patch(
            "astrabox.core.service.orchestrator.expose_port_service.load_astrabox_settings",
            return_value=SimpleNamespace(
                sandbox_secure_access_enabled=True,
                sandbox_endpoint_url_ttl_seconds=900,
            ),
        ),
    ):
        result = await service.expose_port(deployment_id="session-1", port=5173)

    call = provider.resolve_browser_endpoint.await_args
    assert call.kwargs["expires_at"] is not None
    assert 850 <= (call.kwargs["expires_at"].timestamp() - time.time()) <= 900
    assert result["url"].startswith("https://gateway.example/")
    assert result["access"] == "signed"


@pytest.mark.asyncio
async def test_browser_endpoint_that_requires_headers_is_refused() -> None:
    provider = SimpleNamespace(
        resolve_browser_endpoint=AsyncMock(
            return_value=SandboxBrowserEndpoint(
                endpoint="https://gateway.example",
                headers={"OpenSandbox-Ingress-To": "secret-route"},
            )
        )
    )
    service = _service(session={"sandbox_id": "sb-1", "user_id": "user-1"})
    with (
        patch(
            "astrabox.core.service.orchestrator.expose_port_service.sandbox_for_name",
            return_value=provider,
        ),
        patch(
            "astrabox.core.service.orchestrator.expose_port_service.load_astrabox_settings",
            return_value=SimpleNamespace(sandbox_secure_access_enabled=False),
        ),
        pytest.raises(APIError) as caught,
    ):
        await service.expose_port(deployment_id="session-1", port=5173)

    assert caught.value.code == "SANDBOX_BROWSER_ENDPOINT_REQUIRES_HEADERS"
    assert "secret-route" not in caught.value.message


@pytest.mark.asyncio
async def test_refresh_does_not_mint_a_url_for_another_user() -> None:
    provider = SimpleNamespace(resolve_browser_endpoint=AsyncMock())
    service = _service(session={"sandbox_id": "sb-1", "user_id": "owner"})
    with (
        patch(
            "astrabox.core.service.orchestrator.expose_port_service.sandbox_for_name",
            return_value=provider,
        ),
        patch(
            "astrabox.core.service.orchestrator.expose_port_service.load_astrabox_settings",
            return_value=SimpleNamespace(sandbox_secure_access_enabled=False),
        ),
        pytest.raises(APIError) as caught,
    ):
        await service.refresh_port_url(
            deployment_id="session-1",
            port=5173,
            user=UserContext(user_id="someone-else"),
        )

    assert caught.value.status_code == 404
    provider.resolve_browser_endpoint.assert_not_awaited()
