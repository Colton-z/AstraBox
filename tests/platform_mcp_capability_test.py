"""Platform server URLs authorize one deployment and one server, never an id alone."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from astrabox.api.routes import platform_mcp
from astrabox.core.service.orchestrator.transcript_capability import (
    mint_platform_mcp_capability,
    mint_transcript_capability_token,
    verify_platform_mcp_capability,
)


@pytest.fixture(autouse=True)
def signing_key(monkeypatch):
    monkeypatch.setenv("ASTRABOX_TRANSCRIPT_SIGNING_KEY", "test-platform-mcp-signing-key")


def test_platform_capability_is_domain_deployment_and_server_scoped() -> None:
    token = mint_platform_mcp_capability("deployment", "artifacts")
    assert verify_platform_mcp_capability("deployment", "artifacts", token)
    assert not verify_platform_mcp_capability("other", "artifacts", token)
    assert not verify_platform_mcp_capability("deployment", "expose_port", token)
    assert not verify_platform_mcp_capability(
        "deployment", "artifacts", mint_transcript_capability_token("deployment")
    )
    assert not verify_platform_mcp_capability("deployment", "artifacts", "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "deployment,server,token_subject",
    [
        ("deployment", "artifacts", ("deployment", "expose_port")),
        ("deployment", "artifacts", ("other", "artifacts")),
        ("deployment", "artifacts", None),
    ],
)
async def test_bad_capability_gets_non_disclosing_jsonrpc_404(
    monkeypatch, deployment, server, token_subject
) -> None:
    service = SimpleNamespace(
        resolve_platform_mcp_server=AsyncMock(),
        platform_mcp=SimpleNamespace(handle_jsonrpc=AsyncMock()),
    )
    monkeypatch.setattr(platform_mcp, "get_platform_service", lambda: service)
    app = FastAPI()
    platform_mcp.register_platform_mcp_routes(app)
    token = mint_platform_mcp_capability(*token_subject) if token_subject else "bad"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/v1/platform-mcp/{deployment}/{server}/{token}/mcp",
            json={"id": 1, "method": "tools/list"},
        )
    assert response.status_code == 404
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32001, "message": "not found"},
    }
    service.resolve_platform_mcp_server.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_shape_is_removed_and_valid_shape_reaches_registry(monkeypatch) -> None:
    service = SimpleNamespace(
        resolve_platform_mcp_server=AsyncMock(return_value={"platform_server": "artifacts"}),
        platform_mcp=SimpleNamespace(
            handle_jsonrpc=AsyncMock(
                return_value={"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
            )
        ),
    )
    monkeypatch.setattr(platform_mcp, "get_platform_service", lambda: service)
    app = FastAPI()
    platform_mcp.register_platform_mcp_routes(app)
    token = mint_platform_mcp_capability("deployment", "artifacts")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        old = await client.post("/api/v1/platform-mcp/deployment/artifacts/mcp", json={"id": 1})
        valid = await client.post(
            f"/api/v1/platform-mcp/deployment/artifacts/{token}/mcp",
            json={"id": 1, "method": "tools/list"},
        )
    assert old.status_code == 404
    assert valid.status_code == 200 and valid.json()["result"] == {"tools": []}
    service.platform_mcp.handle_jsonrpc.assert_awaited_once()
