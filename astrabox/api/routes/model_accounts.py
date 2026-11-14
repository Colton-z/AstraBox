"""A signed-in user's model accounts: link a ChatGPT account, list, change, remove.

Linking is OpenAI's "Sign in with ChatGPT". ``POST
/api/v1/me/model-accounts/chatgpt/authorizations`` starts a sign-in for the
loopback redirect URI the user's client listens on and answers the URL to open
in the browser; the client posts the query OpenAI redirected the browser to
``…/authorizations/{authorization_id}/callback``, and the server completes the
sign-in. ``astrabox model-account login chatgpt`` is that client.

``GET /api/v1/me/model-accounts/{account_id}/models`` lists the models an
account can use (``default`` names the caller's default account), for choosing
an Agent's model on a ChatGPT plan RuntimeProfile.

``PATCH …/{account_id}`` with ``shared`` shares an account with the team pool,
or stops sharing it; ``GET /api/v1/me/model-accounts/team-pool`` answers
whether the deployment's pool is on, without which nothing can be shared.

Each route acts on the caller's own accounts only. No answer carries a token:
the tokens stay in the server's secret store
(:mod:`astrabox.core.service.model_accounts.service`).
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from pydantic import BaseModel, ConfigDict

from astrabox.api.routes.response_envelope import ApiEnvelope
from astrabox.common.utils.api_response import success_response
from astrabox.common.utils.user_context import get_current_user_context
from astrabox.core.service.model_accounts.service import get_model_account_service

_registered_on: int | None = None


class StartAuthorizationRequest(BaseModel):
    """Body for starting a ChatGPT sign-in."""

    model_config = ConfigDict(extra="forbid")

    #: ``http://127.0.0.1:<port>/auth/callback``, where the client listens.
    redirect_uri: str
    #: Sign in to this linked account again instead of linking a new one.
    account_id: str | None = None


class CompleteAuthorizationRequest(BaseModel):
    """The query OpenAI redirected the browser to, as the client received it."""

    model_config = ConfigDict(extra="forbid")

    code: str | None = None
    state: str
    client_id: str | None = None
    error: str | None = None


class UpdateModelAccountRequest(BaseModel):
    """Body for changing an account; an absent or null field is left as it is."""

    model_config = ConfigDict(extra="forbid")

    label: str | None = None
    is_default: bool | None = None
    #: Share the account with the team pool (true) or stop sharing it (false).
    shared: bool | None = None


# ── response payloads ───────────────────────────────────────────────────────
#
# The payload half of ``ApiEnvelope[...]``; the obligations that come with
# declaring a response model are documented in
# :mod:`astrabox.api.routes.response_envelope`.


class ModelAccountAuthorization(BaseModel):
    """A started sign-in: where to send the browser, and until when it can complete."""

    model_config = ConfigDict(extra="allow")

    authorization_id: str
    authorize_url: str
    expires_at: str


class ModelAccount(BaseModel):
    """One linked account, without its tokens."""

    model_config = ConfigDict(extra="allow")

    account_id: str
    vendor: str
    label: str
    email: str | None
    #: ``active``, or ``sign_in_required`` once OpenAI refused renewal for good.
    status: str
    is_default: bool
    scopes: list[str]
    access_expires_at: str | None
    last_error_code: str | None
    last_error_at: str | None
    #: Whether the owner shared the account with the team pool.
    shared: bool
    #: Until when the plan usage limit OpenAI reported keeps the account from
    #: being chosen for a conversation; ``null`` when no limit holds it back.
    usage_limited_until: str | None
    #: When the pool last gave the account to another member's conversation.
    pool_last_used_at: str | None
    created_at: str
    updated_at: str


class TeamPool(BaseModel):
    """Whether the deployment's team pool of model accounts is on."""

    model_config = ConfigDict(extra="allow")

    enabled: bool


class DeletedModelAccount(BaseModel):
    """A removed account, and whether OpenAI confirmed its session ended."""

    model_config = ConfigDict(extra="allow")

    account_id: str
    #: ``null`` when the account held no refresh token to revoke.
    revocation_confirmed: bool | None


class ModelAccountModel(BaseModel):
    """One model an account can use."""

    model_config = ConfigDict(extra="allow")

    #: OpenAI's model slug, the value an Agent names as its ``model``.
    id: str
    display_name: str


def register_model_account_routes(app: Any) -> None:
    global _registered_on
    if _registered_on == id(app):
        return
    _registered_on = id(app)

    @app.post(
        "/api/v1/me/model-accounts/chatgpt/authorizations",
        response_model=ApiEnvelope[ModelAccountAuthorization],
        response_model_exclude_unset=True,
    )
    async def start_chatgpt_authorization(
        body: StartAuthorizationRequest, request: Request
    ) -> dict[str, Any]:
        user = await get_current_user_context(request)
        return success_response(
            await get_model_account_service().start_authorization(
                user, redirect_uri=body.redirect_uri, account_id=body.account_id
            )
        )

    @app.post(
        "/api/v1/me/model-accounts/chatgpt/authorizations/{authorization_id}/callback",
        response_model=ApiEnvelope[ModelAccount],
        response_model_exclude_unset=True,
    )
    async def complete_chatgpt_authorization(
        authorization_id: str, body: CompleteAuthorizationRequest, request: Request
    ) -> dict[str, Any]:
        user = await get_current_user_context(request)
        return success_response(
            await get_model_account_service().complete_authorization(
                user,
                authorization_id,
                code=body.code,
                state=body.state,
                client_id=body.client_id,
                error=body.error,
            )
        )

    @app.get(
        "/api/v1/me/model-accounts",
        response_model=ApiEnvelope[list[ModelAccount]],
        response_model_exclude_unset=True,
    )
    async def list_model_accounts(request: Request) -> dict[str, Any]:
        user = await get_current_user_context(request)
        return success_response(await get_model_account_service().list_accounts(user))

    @app.get(
        "/api/v1/me/model-accounts/team-pool",
        response_model=ApiEnvelope[TeamPool],
        response_model_exclude_unset=True,
    )
    async def get_model_account_team_pool(request: Request) -> dict[str, Any]:
        user = await get_current_user_context(request)
        return success_response(await get_model_account_service().team_pool(user))

    @app.get(
        "/api/v1/me/model-accounts/{account_id}/models",
        response_model=ApiEnvelope[list[ModelAccountModel]],
        response_model_exclude_unset=True,
    )
    async def list_model_account_models(account_id: str, request: Request) -> dict[str, Any]:
        user = await get_current_user_context(request)
        return success_response(
            await get_model_account_service().list_models(user, account_id)
        )

    @app.patch(
        "/api/v1/me/model-accounts/{account_id}",
        response_model=ApiEnvelope[ModelAccount],
        response_model_exclude_unset=True,
    )
    async def update_model_account(
        account_id: str, body: UpdateModelAccountRequest, request: Request
    ) -> dict[str, Any]:
        user = await get_current_user_context(request)
        return success_response(
            await get_model_account_service().update_account(
                user, account_id, label=body.label, is_default=body.is_default, shared=body.shared
            )
        )

    @app.delete(
        "/api/v1/me/model-accounts/{account_id}",
        response_model=ApiEnvelope[DeletedModelAccount],
        response_model_exclude_unset=True,
    )
    async def delete_model_account(account_id: str, request: Request) -> dict[str, Any]:
        user = await get_current_user_context(request)
        return success_response(
            await get_model_account_service().delete_account(user, account_id)
        )


__all__ = ["register_model_account_routes"]
