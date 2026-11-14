"""Claude Code conversations paid by the person who has them, not the deployment.

Anthropic's terms let each end user of a hosted Claude Code authenticate with
their own Anthropic API key or their own Claude subscription, and forbid a
third party to store or relay Claude.ai subscription credentials. Two model
endpoints follow from that, both at Anthropic's own API, where Claude Code
talks by default:

* ``anthropic_api_key``: each conversation spends its owner's Anthropic API
  key, kept as one of their model accounts and chosen, like a ChatGPT
  account, by
  :meth:`~astrabox.core.service.model_accounts.service.ModelAccountService.conversation_token`
  among the owner's keys only. The sandbox holds a placeholder and the egress
  boundary attaches the key to requests for ``api.anthropic.com``.
* ``claude_login``: the platform delivers no credential. The conversation's
  user signs Claude Code in to their own Claude account from the
  conversation's terminal; the login stays in that sandbox and goes with it.

The AstraBox server's own completions (conversation titles) are not the
user's requests, so they stay on the deployment's default model endpoint.
"""

from __future__ import annotations

from typing import Any, Literal

from astrabox.common.utils.errors import APIError
from astrabox.seams.model import (
    ModelEndpoint,
    ModelEndpointProvider,
    ModelRequestContext,
    default_model_endpoint_name,
    model_endpoint_for_name,
)

#: Anthropic's API, which Claude Code calls when no other endpoint is set.
ANTHROPIC_API_BASE_URL = "https://api.anthropic.com"

#: The hosts Claude Code's claude.ai login uses from inside the sandbox, besides
#: the API: account authentication, and the OAuth token exchange, refresh and
#: revocation (https://code.claude.com/docs/en/network-config, "Network access
#: requirements"). The sign-in page itself opens in the user's own browser.
CLAUDE_LOGIN_HOSTS = ("claude.ai", "platform.claude.com")


def _server_endpoint(settings: Any) -> ModelEndpoint:
    return model_endpoint_for_name(default_model_endpoint_name()).server_endpoint(
        settings=settings
    )


class AnthropicAPIKeyModelEndpointProvider(ModelEndpointProvider):
    """Send a conversation's model requests to Anthropic with its owner's API key."""

    name = "anthropic_api_key"
    credential_owner: Literal["deployment", "session_owner", "sandbox"] = "session_owner"
    engine_kinds = frozenset({"claude_code"})

    def resolve(self, *, requested: ModelEndpoint, settings: Any = None) -> ModelEndpoint:
        _ = settings
        return ModelEndpoint(
            base_url=ANTHROPIC_API_BASE_URL,
            api_key=None,
            model_name=requested.model_name,
            credential_kind="api_key",
        )

    def server_endpoint(self, *, settings: Any = None) -> ModelEndpoint:
        return _server_endpoint(settings)

    async def ensure_session_credential(self, *, context: ModelRequestContext) -> str | None:
        from astrabox.core.service.model_accounts.service import (
            VENDOR_ANTHROPIC_API_KEY,
            get_model_account_service,
        )

        user_id = str(context.user_id or "").strip()
        if not user_id:
            raise APIError(
                code="MODEL_ACCOUNT_REQUIRED",
                message=(
                    "this conversation has no owner whose Anthropic API key it could use; "
                    "an Anthropic API key RuntimeProfile runs conversations a signed-in user starts"
                ),
                status_code=409,
            )
        token = await get_model_account_service().conversation_token(
            user_id=user_id,
            vendor=VENDOR_ANTHROPIC_API_KEY,
            account_id=str(context.account_id or "").strip() or None,
            session_id=str(context.conversation_id or "").strip() or None,
            keep=context.keep_account,
        )
        return str(token.access_token)


class ClaudeLoginModelEndpointProvider(ModelEndpointProvider):
    """Let Claude Code use the login its user makes inside the conversation's sandbox."""

    name = "claude_login"
    credential_owner: Literal["deployment", "session_owner", "sandbox"] = "sandbox"
    engine_kinds = frozenset({"claude_code"})
    sandbox_hosts = CLAUDE_LOGIN_HOSTS

    def resolve(self, *, requested: ModelEndpoint, settings: Any = None) -> ModelEndpoint:
        # The address admits the API host in the sandbox's network policy; no
        # credential travels with it.
        _ = settings
        return ModelEndpoint(
            base_url=ANTHROPIC_API_BASE_URL,
            api_key=None,
            model_name=requested.model_name,
            credential_kind="bearer",
        )

    def server_endpoint(self, *, settings: Any = None) -> ModelEndpoint:
        return _server_endpoint(settings)
