"""Listing an MCP client's tokens never answers with a secret field.

The secret is in the mint's answer and in no later one, so the listing route's
response must not carry the field at all, not even empty.
"""

from __future__ import annotations

from typing import Any, Iterator

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from astrabox.api.routes import mcp_tokens as mcp_token_routes

_TOKEN: dict[str, Any] = {
    "token_id": "tok_1",
    "name": "ci-runner",
    "scope": "converse",
    "expires_at": None,
    "created_at": "2026-08-01T00:00:00Z",
    "last_used_at": None,
}


def _envelope(data: Any) -> dict[str, Any]:
    return {"code": "OK", "message": "success", "data": data}


# ── clients ─────────────────────────────────────────────────────────────────


class _TokenService:
    """The three calls the token routes make, with the service's own shapes."""

    async def issue(self, user: Any, **kwargs: Any) -> dict[str, Any]:
        return {**_TOKEN, "secret": "astrabox_mcp_abc"}

    async def list(self, user: Any) -> list[dict[str, Any]]:
        return [_TOKEN]

    async def revoke(self, user: Any, token_id: str) -> None:
        return None


@pytest.fixture
def mcp_token_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(mcp_token_routes, "_registered_on", None)
    app = FastAPI()
    mcp_token_routes.register_mcp_token_routes(app, service=_TokenService())
    with TestClient(app) as client:
        yield client


# ── the payload reaches the caller unchanged ────────────────────────────────


def test_listing_tokens_carries_no_secret_field(mcp_token_client: TestClient) -> None:
    response = mcp_token_client.get("/api/v1/mcp-tokens")

    assert response.status_code == 200
    assert response.json() == _envelope({"tokens": [_TOKEN]})
    assert "secret" not in response.json()["data"]["tokens"][0]
