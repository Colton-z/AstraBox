"""A signed-in user's model accounts: link a ChatGPT account, list, change, remove.

Linking is OpenAI's "Sign in with ChatGPT". ``POST
/api/v1/me/model-accounts/chatgpt/authorizations`` starts a sign-in for the
loopback redirect URI the user's client listens on and answers the URL to open
in the browser; the client posts the query OpenAI redirected the browser to
``…/authorizations/{authorization_id}/callback``, and the server completes the
sign-in. ``astrabox model-account login chatgpt`` is that client.

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
    created_at: str
    updated_at: str


class DeletedModelAccount(BaseModel):
    """A removed account, and whether OpenAI confirmed its session ended."""

    model_config = ConfigDict(extra="allow")

    account_id: str
    #: ``null`` when the account held no refresh token to revoke.
    revocation_confirmed: bool | None


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
                user, account_id, label=body.label, is_default=body.is_default
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
