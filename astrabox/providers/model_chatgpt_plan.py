"""ChatGPT plan usage: a conversation spends its owner's ChatGPT subscription.

OpenAI's "Sign in with ChatGPT" lets an application call the Responses API at
``https://api.openai.com/v1`` with an access token issued for a user's ChatGPT
account, and charges that user's plan. The application signs the user in and
keeps the tokens (:mod:`astrabox.core.service.model_accounts`); this provider
is the model endpoint a RuntimeProfile selects to run its conversations that
way. It has no credential of its own: each Session spends the access token of
the account :meth:`~astrabox.core.service.model_accounts.service.ModelAccountService.conversation_token`
chooses for it (its owner's, or with the team pool on, an account another
member shared), which the platform renews and the sandbox never holds.

The AstraBox server's own completions (conversation titles) are not the
user's requests, so they stay on the deployment's default model endpoint.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from astrabox.common.utils.errors import APIError
from astrabox.seams.model import (
    ModelEndpoint,
    ModelEndpointProvider,
    ModelRequestContext,
    default_model_endpoint_name,
    model_endpoint_for_name,
)

#: OpenAI's API, where an access token with plan usage is accepted.
CHATGPT_PLAN_BASE_URL = "https://api.openai.com/v1"


class ChatGPTPlanModelEndpointProvider(ModelEndpointProvider):
    """Send a conversation's model requests to OpenAI as its owner's ChatGPT account."""

    name = "chatgpt_plan"
    credential_owner: Literal["deployment", "session_owner", "sandbox"] = "session_owner"
    #: Plan usage serves the Responses API, which is Codex's wire.
    engine_kinds = frozenset({"codex"})

    def resolve(self, *, requested: ModelEndpoint, settings: Any = None) -> ModelEndpoint:
        _ = settings
        return ModelEndpoint(
            base_url=CHATGPT_PLAN_BASE_URL,
            api_key=None,
            model_name=requested.model_name,
            credential_kind="bearer",
        )

    def server_endpoint(self, *, settings: Any = None) -> ModelEndpoint:
        return model_endpoint_for_name(default_model_endpoint_name()).server_endpoint(
            settings=settings
        )

    async def _token(self, context: ModelRequestContext, *, keep: bool) -> Any:
        from astrabox.core.service.model_accounts.service import (
            VENDOR_CHATGPT_PLAN,
            get_model_account_service,
        )

        user_id = str(context.user_id or "").strip()
        if not user_id:
            raise APIError(
                code="MODEL_ACCOUNT_REQUIRED",
                message=(
                    "this conversation has no owner whose ChatGPT account it could use; "
                    "a ChatGPT plan RuntimeProfile runs conversations a signed-in user starts"
                ),
                status_code=409,
            )
        return await get_model_account_service().conversation_token(
            user_id=user_id,
            vendor=VENDOR_CHATGPT_PLAN,
            account_id=str(context.account_id or "").strip() or None,
            session_id=str(context.conversation_id or "").strip() or None,
            keep=keep,
        )

    async def ensure_session_credential(self, *, context: ModelRequestContext) -> str | None:
        return str((await self._token(context, keep=context.keep_account)).access_token)

    async def session_credential_expires_at(
        self, *, context: ModelRequestContext
    ) -> datetime | None:
        # The expiry of the token the conversation spends now, which the last
        # ensure_session_credential chose.
        expires_at: datetime | None = (await self._token(context, keep=True)).expires_at
        return expires_at
