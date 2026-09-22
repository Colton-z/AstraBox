"""Management for the API keys an MCP client authenticates with.

Three operations and no more: mint one, see the ones you have, delete one. The
secret is returned by the mint and never again, so there is nothing to read
back and no endpoint that offers to.

These authenticate as the console does — a signed-in user. A key cannot reach
them, because the facade's own credential must not be able to widen itself; the
only credential these accept is the one a person holds.

Design: `docs/maintainers/mcp-client-tokens.md`.
"""

from __future__ import annotations

from typing import Any

from fastapi import Body, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

from astrabox.api.routes.response_envelope import ApiEnvelope
from astrabox.common.utils.api_response import success_response
from astrabox.common.utils.user_context import get_current_user_context
from astrabox.core.service.orchestrator.mcp_client_token_service import (
    SCOPE_CONVERSE,
    MCPClientTokenService,
)

_registered_on: int | None = None


# ── response payloads ───────────────────────────────────────────────────────
#
# The payload half of ``ApiEnvelope[...]``; the obligations that come with
# declaring a response model are documented in
# :mod:`astrabox.api.routes.response_envelope`.
#
# The two answering routes return the ``success_response(...)`` dict rather than
# wrapping it in a ``JSONResponse``, because a handler that returns a Response
# object bypasses ``response_model`` — the declaration would document the shape
# without ever checking it. A non-default status code belongs on the decorator
# for the same reason: that is what puts it in the schema.


class MCPClientToken(BaseModel):
    """One key, as ``mcp_client_token_service.public_view`` renders it.

    There is no ``secret`` field: it exists only in the issuing answer below, so
    a reader cannot mistake an absent secret for an empty one.
    """

    model_config = ConfigDict(extra="allow")

    token_id: str
    name: str
    scope: str
    expires_at: str | None
    created_at: str | None
    last_used_at: str | None


class IssuedMCPClientToken(MCPClientToken):
    """The mint's answer — the only place the secret is ever returned."""

    secret: str


class MCPClientTokenList(BaseModel):
    """Every key the calling user holds."""

    model_config = ConfigDict(extra="allow")

    tokens: list[MCPClientToken]


def register_mcp_token_routes(app: Any, *, service: Any | None = None) -> None:
    global _registered_on
    if _registered_on == id(app):
        return
    _registered_on = id(app)

    token_service = service or MCPClientTokenService()

    @app.post(
        "/api/v1/mcp-tokens",
        status_code=201,
        response_model=ApiEnvelope[IssuedMCPClientToken],
        response_model_exclude_unset=True,
    )
    async def issue_mcp_token(
        request: Request,
        payload: dict[str, Any] = Body(default_factory=dict),
    ) -> dict[str, Any]:
        user = await get_current_user_context(request)
        raw_days = payload.get("expires_in_days")
        issued = await token_service.issue(
            user,
            name=str(payload.get("name") or ""),
            scope=str(payload.get("scope") or SCOPE_CONVERSE),
            expires_in_days=None if raw_days is None else int(raw_days),
        )
        return success_response(issued)

    @app.get(
        "/api/v1/mcp-tokens",
        response_model=ApiEnvelope[MCPClientTokenList],
        response_model_exclude_unset=True,
    )
    async def list_mcp_tokens(request: Request) -> dict[str, Any]:
        user = await get_current_user_context(request)
        return success_response({"tokens": await token_service.list(user)})

    # 204 is declared, not defaulted: the handler answers 204 with no body, and
    # the default 200 documents a body no caller ever receives.
    @app.delete("/api/v1/mcp-tokens/{token_id}", status_code=204)
    async def revoke_mcp_token(request: Request, token_id: str) -> Response:
        user = await get_current_user_context(request)
        await token_service.revoke(user, token_id)
        # Deletion, not disabling: a credential that can be re-enabled invites
        # re-enabling one that leaked.
        return Response(status_code=204)


__all__ = ["register_mcp_token_routes"]
