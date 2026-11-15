"""People's model accounts: ChatGPT accounts and Anthropic API keys.

A model account pays for its owner's model requests on a RuntimeProfile whose
model endpoint spends the conversation owner's account. Its ``vendor`` is the
kind: ``chatgpt_plan`` or ``anthropic_api_key``.

An Anthropic API key is stored as the user gives it: the server checks it
against Anthropic's model list (``GET /v1/models``) before keeping it in the
secret store under ``model-account/<account_id>``, and no view returns it.
Anthropic's terms ask each end user of a hosted Claude Code to authenticate
with their own key, which is what this account is.

OpenAI's plan usage for open-source clients
(https://developers.openai.com/siwc/token-sharing-open-source) lets an
application call the Responses API with an access token issued for a user's
ChatGPT account, and charges that user's plan. AstraBox is the application:

* **Linking.** A user signs in through a public-client OAuth authorization
  with PKCE (:meth:`ModelAccountService.start_authorization`, then
  :meth:`ModelAccountService.complete_authorization`). The redirect URI is an
  HTTP loopback address on the machine running the user's browser, so a client
  there (``astrabox model-account login chatgpt``) receives the redirect and
  hands its query to the server. The state, nonce and PKCE verifier stay on the
  server, which exchanges the code itself, validates the ID token, and requires
  the plan-usage scope.
* **Custody.** The access, refresh and ID tokens live in the secret store under
  ``model-account/<account_id>``, written in the same transaction as the
  account's row. No view returns them.
* **Renewal.** :meth:`ModelAccountService.access_token` renews an access token
  inside :data:`~astrabox.seams.egress_credentials.ACCESS_TOKEN_RENEWAL_WINDOW_S`
  of its expiry. OpenAI rotates the refresh token on every renewal and refuses
  a used one, so renewal runs under a compare-and-set claim on the account's
  row: one caller renews, the others wait for its result.
* **Accounts.** A user may keep several accounts of each vendor; one of each
  vendor is their default. Each ChatGPT registration keeps its own issued
  client id; the installation is one host (``ext_agent_host_id``, derived from
  the installation id).
* **Which account a conversation spends.**
  :meth:`ModelAccountService.conversation_token` decides it, when a
  conversation starts and before each of its responses, and records it on the
  session. It chooses only among accounts of the vendor the conversation's
  model endpoint spends. A conversation spends its owner's preferred account:
  the one its Agent names, or the owner's default of that vendor. With the
  team pool off that is all. With it on
  (``ASTRABOX_MODEL_ACCOUNT_POOL_ENABLED``), an account whose plan usage limit
  was reported (:meth:`ModelAccountService.record_usage_limit`) is passed over
  until the hold ends, and the conversation spends the owner's other accounts
  of that vendor, then the accounts of that vendor other members of the
  organization shared with the team. Only a ChatGPT account can be shared, and
  only a ChatGPT plan reports a usage limit, so an Anthropic API key is always
  its owner's preferred one. The response that met the limit stays failed;
  nothing replays it, and a response never changes accounts while it runs.
"""

from __future__ import annotations

import asyncio
import hmac
import re
import secrets
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode

import httpx
import jwt

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.common.utils.time_utils import parse_iso, utcnow
from astrabox.common.utils.user_context import UserContext, default_org_id
from astrabox.core.service.secret_changes import SecretStoreChange, prepare_secret_change
from astrabox.persistence.installation import load_installation_id
from astrabox.persistence.repository.model_account_repository import (
    STATUS_ACTIVE,
    ModelAccountRepository,
    new_account_id,
)
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.providers.identity_oidc import make_pkce_pair
from astrabox.seams.egress_credentials import ACCESS_TOKEN_RENEWAL_WINDOW_S
from astrabox.seams.secrets import SecretStore, secret_store_for_name

logger = get_logger(__name__)

#: OpenAI's endpoints for plan usage. The authorization and token endpoints are
#: the ones the plan-usage guide names; the signing keys and the revocation
#: endpoint come from the issuer's discovery document.
AUTHORIZE_ENDPOINT = "https://auth.openai.com/api/accounts/authorize"
TOKEN_ENDPOINT = "https://auth.openai.com/api/accounts/oauth/token"
ISSUER = "https://auth.openai.com"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
#: The API the access token is for, sent with every token request.
RESOURCE = "https://api.openai.com/v1"
#: The models the access token may call, in the order OpenAI offers them.
MODELS_ENDPOINT = f"{RESOURCE}/models"
#: The ``visibility`` of a model OpenAI means for a model picker.
_LISTED_MODEL = "list"
#: The account id that names the caller's default account.
DEFAULT_ACCOUNT = "default"
#: The scope that lets the access token spend the user's ChatGPT plan. A
#: valid ID token alone does not grant it.
PLAN_USAGE_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = f"openid profile email offline_access resource.invoke {PLAN_USAGE_SCOPE}"
#: The client id a first registration names; OpenAI issues the account's own
#: client id in the redirect, and every later request uses that one.
DYNAMIC_CLIENT_ID = "dynamic_agent_client"
#: The application name a first registration shows the user.
AGENT_NAME_HINT = "AstraBox"

VENDOR_CHATGPT_PLAN = "chatgpt_plan"
VENDOR_ANTHROPIC_API_KEY = "anthropic_api_key"

#: How each vendor's accounts are named to their owner.
_VENDOR_NOUNS = {
    VENDOR_CHATGPT_PLAN: "ChatGPT account",
    VENDOR_ANTHROPIC_API_KEY: "Anthropic API key",
}

#: What a conversation with no account of the vendor it needs is told.
_NO_DEFAULT = {
    VENDOR_CHATGPT_PLAN: (
        "no default ChatGPT account is linked for this user; link one with "
        "`astrabox model-account login chatgpt` or choose a default"
    ),
    VENDOR_ANTHROPIC_API_KEY: (
        "no default Anthropic API key is stored for this user; add one with "
        "`astrabox model-account add anthropic` or on the console's Model accounts "
        "page, or choose a default"
    ),
}

#: Anthropic's model list for an API key (https://platform.claude.com/docs/en/api/models/list).
ANTHROPIC_MODELS_ENDPOINT = "https://api.anthropic.com/v1/models"
#: The API version every request to Anthropic names.
ANTHROPIC_VERSION = "2026-10-21"
#: The most models Anthropic answers in one page.
_ANTHROPIC_PAGE_LIMIT = 1000
#: The most pages read for one list, a bound on a cursor that never ends.
_ANTHROPIC_MAX_PAGES = 20
#: Printable ASCII without spaces: what an HTTP header value can carry as the key.
_API_KEY_SHAPE = re.compile(r"[\x21-\x7e]+")
#: How many trailing characters of a key a default label shows, to tell keys apart.
_KEY_HINT_CHARS = 4

#: How long a started sign-in can be completed.
AUTHORIZATION_TTL_S = 600

#: The redirect URIs OpenAI accepts here: plain HTTP to ``127.0.0.1`` at
#: ``/auth/callback``, where only the port may vary.
_REDIRECT_URI = re.compile(r"http://127\.0\.0\.1:([0-9]{1,5})/auth/callback")

#: Refresh errors after which the refresh token is unusable and only a new
#: sign-in helps (the plan-usage guide's "Refresh errors").
_SIGN_IN_AGAIN_ERRORS = frozenset(
    {
        "invalid_grant",
        "invalid_refresh_token",
        "token_expired",
        "refresh_token_expired",
        "refresh_token_invalidated",
        "refresh_token_reused",
    }
)

#: Clock skew allowed when checking the ID token's times.
_ID_TOKEN_LEEWAY_S = 5
_ID_TOKEN_ALGORITHMS = ["RS256", "ES256"]

#: Bound on one request to OpenAI, connection included.
_HTTP_TIMEOUT_S = 30.0

#: How long a refresh claim lasts. It exceeds :data:`_HTTP_TIMEOUT_S` with room
#: to record the result, so a claim held by a live renewal never lapses under
#: it; one held by a crashed replica lapses after this.
_REFRESH_CLAIM_S = 60.0

#: How often a caller waiting on another replica's renewal re-reads the account.
_REFRESH_POLL_S = 0.25

_LABEL_MAX_CHARS = 200

#: The error a response, or an input with no account left to spend, fails with
#: when the plan usage limit of the account it spends was reached.
PLAN_USAGE_LIMIT = "MODEL_PLAN_USAGE_LIMIT"

#: How long a reported usage limit keeps an account from being chosen. OpenAI's
#: plan-usage error names no reset time and its guide says not to infer one
#: (https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery),
#: so the account is offered again after this.
USAGE_LIMIT_HOLD_S = 3600

#: Secret-store keys under an account's scope.
_ACCESS_TOKEN = "access_token"
_REFRESH_TOKEN = "refresh_token"
_ID_TOKEN = "id_token"
_API_KEY = "api_key"


@dataclass(frozen=True)
class ModelAccountToken:
    """An account's current credential, and when it expires.

    ``access_token`` is a ChatGPT account's access token, or an Anthropic API
    key, which has no expiry (``expires_at`` ``None``).
    """

    account_id: str
    #: ``repr=False``: a logged token object must not print the token.
    access_token: str = field(repr=False)
    expires_at: datetime | None


@dataclass(frozen=True)
class _TokenSet:
    """One token-endpoint answer; an absent token is ``""``."""

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    id_token: str = field(repr=False)
    expires_at: str
    #: The granted scopes, space-separated; ``None`` when the answer named none.
    scopes: str | None


def _scope(account_id: str) -> str:
    return f"model-account/{account_id}"


def _not_found(account_id: str, vendor: str | None = None) -> APIError:
    """No such account of the caller's, or none of ``vendor`` when one is named."""
    message = f"model account {account_id!r} not found"
    if vendor:
        message += f" among your {_VENDOR_NOUNS[vendor]}s"
    return APIError(code="MODEL_ACCOUNT_NOT_FOUND", message=message, status_code=404)


def _api_key_rejected(message: str) -> APIError:
    return APIError(
        code="MODEL_ACCOUNT_API_KEY_REJECTED",
        message=f"{message}; store a key Anthropic accepts",
        status_code=409,
    )


def _invalid(message: str) -> APIError:
    return APIError(code="INVALID_REQUEST", message=message, status_code=400)


def _authorization_failed(message: str, *, status_code: int = 400) -> APIError:
    return APIError(
        code="MODEL_ACCOUNT_AUTHORIZATION_FAILED",
        message=f"{message}; start a new sign-in",
        status_code=status_code,
    )


def _sign_in_required(account: Mapping[str, Any], reason: str) -> APIError:
    return APIError(
        code="MODEL_ACCOUNT_SIGN_IN_REQUIRED",
        message=(
            f"ChatGPT account {account['label']!r} needs a new sign-in: {reason}. "
            "Run `astrabox model-account login chatgpt --account "
            f"{account['account_id']}`"
        ),
        status_code=409,
    )


def _refresh_failed(account_id: str, message: str, *, retryable: bool = True) -> APIError:
    return APIError(
        code="MODEL_ACCOUNT_REFRESH_FAILED",
        message=f"model account {account_id!r}: {message}",
        status_code=502,
        retryable=retryable,
    )


def _models_unavailable(account_id: str, message: str, *, retryable: bool = True) -> APIError:
    return APIError(
        code="MODEL_ACCOUNT_MODELS_UNAVAILABLE",
        message=f"model account {account_id!r}: {message}",
        status_code=502,
        retryable=retryable,
    )


def _usage_limit_reached(account: Mapping[str, Any]) -> APIError:
    return APIError(
        code=PLAN_USAGE_LIMIT,
        message=(
            f"ChatGPT account {account['label']!r} reached its plan usage limit and is used "
            f"again after {account['usage_limited_until']}; no other linked or shared account "
            "can be used now"
        ),
        status_code=429,
        evidence={
            "account_id": account["account_id"],
            "usage_limited_until": account["usage_limited_until"],
        },
    )


def _due(expires_at: str | None) -> bool:
    """Whether an access token expiring at ``expires_at`` needs renewing now."""
    if not expires_at:
        return True
    return parse_iso(expires_at) <= utcnow() + timedelta(seconds=ACCESS_TOKEN_RENEWAL_WINDOW_S)


def _usage_limited(account: Mapping[str, Any]) -> bool:
    """Whether a reported usage limit still holds the account back."""
    until = account["usage_limited_until"]
    return bool(until) and parse_iso(until) > utcnow()


def _pool_enabled() -> bool:
    return load_astrabox_settings().model_account_pool_enabled


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _error_code(payload: Mapping[str, Any]) -> str:
    """The machine-readable error of a token-endpoint refusal.

    OAuth names it in a string ``error``; OpenAI's API errors carry it as
    ``error.code``.
    """
    error = payload.get("error")
    if isinstance(error, Mapping):
        return str(error.get("code") or "").strip()
    return str(error or "").strip()


def _token_set(payload: Mapping[str, Any]) -> _TokenSet | None:
    """The tokens of a successful token-endpoint answer; ``None`` without an access token or lifetime."""
    access_token = str(payload.get("access_token") or "").strip()
    expires_in = payload.get("expires_in")
    if (
        not access_token
        or isinstance(expires_in, bool)
        or not isinstance(expires_in, (int, float))
        or expires_in <= 0
    ):
        return None
    scope = str(payload.get("scope") or "").strip()
    return _TokenSet(
        access_token=access_token,
        refresh_token=str(payload.get("refresh_token") or "").strip(),
        id_token=str(payload.get("id_token") or "").strip(),
        expires_at=(utcnow() + timedelta(seconds=int(expires_in))).isoformat(),
        scopes=scope or None,
    )


def _plan_usage_granted(scopes: str) -> bool:
    return PLAN_USAGE_SCOPE in scopes.split()


def _public_view(account: Mapping[str, Any]) -> dict[str, Any]:
    """What an account's owner reads about it; never a token."""
    return {
        "account_id": account["account_id"],
        "vendor": account["vendor"],
        "label": account["label"],
        "email": account["email"],
        "status": account["status"],
        "is_default": account["is_default"],
        "scopes": account["scopes"].split(),
        "access_expires_at": account["access_expires_at"],
        "last_error_code": account["last_error_code"],
        "last_error_at": account["last_error_at"],
        "shared": account["shared"],
        "usage_limited_until": account["usage_limited_until"] if _usage_limited(account) else None,
        "pool_last_used_at": account["pool_last_used_at"],
        "created_at": account["created_at"],
        "updated_at": account["updated_at"],
    }


async def _post_form(url: str, data: Mapping[str, str]) -> httpx.Response:
    async with asyncio.timeout(_HTTP_TIMEOUT_S):
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_HTTP_TIMEOUT_S, connect=10.0), follow_redirects=False
        ) as client:
            return await client.post(url, data=dict(data), headers={"Accept": "application/json"})


async def _get_with_token(url: str, access_token: str) -> httpx.Response:
    async with asyncio.timeout(_HTTP_TIMEOUT_S):
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_HTTP_TIMEOUT_S, connect=10.0), follow_redirects=False
        ) as client:
            return await client.get(
                url,
                headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
            )


async def _get_with_api_key(url: str, api_key: str, params: Mapping[str, str]) -> httpx.Response:
    # The key rides in ``x-api-key``, the header Claude Code sends it in, so a
    # key this request accepts is accepted from the sandbox too.
    async with asyncio.timeout(_HTTP_TIMEOUT_S):
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_HTTP_TIMEOUT_S, connect=10.0), follow_redirects=False
        ) as client:
            return await client.get(
                url,
                params=dict(params),
                headers={
                    "Accept": "application/json",
                    "x-api-key": api_key,
                    "anthropic-version": ANTHROPIC_VERSION,
                },
            )


def _anthropic_error(response: httpx.Response) -> str:
    """The error type of an Anthropic refusal (``{"error": {"type": ...}}``), or ``unnamed``."""
    error = _json_object(response).get("error")
    kind = str(error.get("type") or "").strip() if isinstance(error, Mapping) else ""
    return kind or "unnamed"


async def _anthropic_models(api_key: str, *, account: str, limit: int | None = None) -> list[dict[str, str]]:
    """The models an Anthropic API key can use, as ``{id, display_name}`` in Anthropic's order.

    Anthropic lists the newest first and pages with ``has_more`` and
    ``last_id``; with ``limit`` only the first page of that many is read.
    ``account`` names the key in errors. A key Anthropic refuses (401 or 403)
    raises ``MODEL_ACCOUNT_API_KEY_REJECTED``; an unreachable API, another
    status or an answer without a ``data`` array raises
    ``MODEL_ACCOUNT_MODELS_UNAVAILABLE``.
    """
    models: list[dict[str, str]] = []
    after_id: str | None = None
    for _ in range(_ANTHROPIC_MAX_PAGES):
        params = {"limit": str(limit or _ANTHROPIC_PAGE_LIMIT)}
        if after_id:
            params["after_id"] = after_id
        try:
            response = await _get_with_api_key(ANTHROPIC_MODELS_ENDPOINT, api_key, params)
        except (httpx.HTTPError, TimeoutError) as exc:
            raise _models_unavailable(
                account, f"Anthropic's model list could not be reached ({type(exc).__name__})"
            ) from exc
        if response.status_code in (401, 403):
            raise _api_key_rejected(
                f"Anthropic refused the API key (status {response.status_code}, "
                f"{_anthropic_error(response)})"
            )
        if response.status_code != 200:
            raise _models_unavailable(
                account,
                f"Anthropic's model list answered {response.status_code} "
                f"({_anthropic_error(response)})",
                retryable=response.status_code >= 500 or response.status_code == 429,
            )
        payload = _json_object(response)
        entries = payload.get("data")
        if not isinstance(entries, list):
            raise _models_unavailable(
                account, "Anthropic's model list answer has no data array", retryable=False
            )
        for entry in entries:
            model_id = str(entry.get("id") or "").strip() if isinstance(entry, Mapping) else ""
            if not model_id:
                raise _models_unavailable(
                    account, "Anthropic's model list names a model without an id", retryable=False
                )
            models.append(
                {"id": model_id, "display_name": str(entry.get("display_name") or "").strip() or model_id}
            )
        after_id = str(payload.get("last_id") or "").strip() or None
        if limit is not None or payload.get("has_more") is not True or not after_id:
            return models
    raise _models_unavailable(
        account,
        f"Anthropic's model list did not end within {_ANTHROPIC_MAX_PAGES} pages",
        retryable=False,
    )


def _checked_api_key(api_key: str) -> str:
    key = str(api_key or "").strip()
    if not key or _API_KEY_SHAPE.fullmatch(key) is None:
        raise _invalid("api_key must be a non-empty Anthropic API key without spaces")
    return key


async def _get_json(url: str) -> dict[str, Any]:
    async with asyncio.timeout(_HTTP_TIMEOUT_S):
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_HTTP_TIMEOUT_S, connect=10.0), follow_redirects=False
        ) as client:
            response = await client.get(url, headers={"Accept": "application/json"})
    if response.status_code != 200:
        raise ValueError(f"{url} answered {response.status_code}")
    document = _json_object(response)
    if not document:
        raise ValueError(f"{url} answered no JSON object")
    return document


async def _discovery() -> dict[str, Any]:
    """OpenAI's OpenID configuration, checked to be the issuer's own."""
    document = await _get_json(DISCOVERY_URL)
    if document.get("issuer") != ISSUER:
        raise ValueError(f"the discovery document names issuer {document.get('issuer')!r}")
    return document


def _discovered_https(document: Mapping[str, Any], name: str) -> str:
    url = str(document.get(name) or "").strip()
    if not url.startswith("https://"):
        raise ValueError(f"the discovery document has no HTTPS {name}")
    return url


async def ext_agent_host_id() -> str:
    """This installation's ``ext_agent_host_id``: its installation id as a URN.

    The installation id is a random UUID generated once per database and shared
    by every replica, which together are the host that keeps and renews the
    tokens. It exists before the first sign-in because reading it creates it.
    """
    return f"urn:uuid:{uuid.UUID(hex=await load_installation_id())}"


class ModelAccountService:
    """Linking, listing and removing a user's model accounts, and their access tokens."""

    def __init__(
        self,
        *,
        repository: ModelAccountRepository | None = None,
        secret_store_name: str | None = None,
    ) -> None:
        self._repo = repository or ModelAccountRepository()
        self._secret_store_name = secret_store_name

    # The store is resolved per use, as VaultService resolves it, so a provider
    # registered after construction still wins by name.
    def _store(self) -> SecretStore:
        return secret_store_for_name(self._secret_store_name)

    async def _secret_change(
        self,
        account_id: str,
        *,
        put: Mapping[str, str] | None = None,
        remove: Sequence[str] = (),
        purge: bool = False,
    ) -> SecretStoreChange:
        return await prepare_secret_change(
            self._store(), _scope(account_id), put=put, remove=remove, purge=purge
        )

    async def _owned(
        self, user_id: str, account_id: str, *, vendor: str | None = None
    ) -> dict[str, Any]:
        """The user's account ``account_id``, of ``vendor`` when one is named."""
        account = await self._repo.get_account(account_id)
        if (
            account is None
            or account["owner_user_id"] != user_id
            or (vendor is not None and account["vendor"] != vendor)
        ):
            raise _not_found(account_id, vendor)
        return account

    # ── linking ─────────────────────────────────────────────────────────────

    async def start_authorization(
        self, user: UserContext, *, redirect_uri: str, account_id: str | None = None
    ) -> dict[str, Any]:
        """Begin a sign-in; the user's browser opens the returned ``authorize_url``.

        ``redirect_uri`` is the loopback address the user's client listens on,
        ``http://127.0.0.1:<port>/auth/callback``. With ``account_id`` the
        sign-in is a reauthorization of that account: it names the account's
        issued client id and the hints that select the same ChatGPT account.
        The sign-in can be completed once, within
        :data:`AUTHORIZATION_TTL_S` seconds.
        """
        match = _REDIRECT_URI.fullmatch(str(redirect_uri or ""))
        if match is None or not 1 <= int(match.group(1)) <= 65535:
            raise _invalid(
                "redirect_uri must be http://127.0.0.1:<port>/auth/callback, the loopback "
                "address the signing-in client listens on"
            )
        account = (
            await self._owned(user.user_id, account_id, vendor=VENDOR_CHATGPT_PLAN)
            if account_id
            else None
        )
        params: dict[str, str] = {}
        if account is None:
            params["client_id"] = DYNAMIC_CLIENT_ID
            params["agent_name_hint"] = AGENT_NAME_HINT
        else:
            params["client_id"] = account["issued_client_id"]
        params["ext_agent_host_id"] = await ext_agent_host_id()
        if account is not None:
            id_token = await self._store().get(scope=_scope(account["account_id"]), key=_ID_TOKEN)
            if id_token:
                params["id_token_hint"] = id_token
            if account["email"]:
                params["login_hint"] = account["email"]
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier, challenge = make_pkce_pair()
        params.update(
            response_type="code",
            redirect_uri=redirect_uri,
            scope=SCOPES,
            resource=RESOURCE,
            state=state,
            nonce=nonce,
            code_challenge_method="S256",
            code_challenge=challenge,
        )
        pending = await self._repo.create_authorization(
            {
                "authorization_id": f"maz_{uuid.uuid4().hex}",
                "owner_user_id": user.user_id,
                "account_id": account["account_id"] if account is not None else None,
                "client_id": params["client_id"],
                "redirect_uri": redirect_uri,
                "state": state,
                "nonce": nonce,
                "code_verifier": verifier,
            },
            ttl_s=AUTHORIZATION_TTL_S,
        )
        logger.info(
            "model account sign-in started: authorization=%s user=%s account=%s",
            pending["authorization_id"], user.user_id, pending["account_id"],
        )
        return {
            "authorization_id": pending["authorization_id"],
            "authorize_url": f"{AUTHORIZE_ENDPOINT}?{urlencode(params, quote_via=quote)}",
            "expires_at": pending["expires_at"],
        }

    async def complete_authorization(
        self,
        user: UserContext,
        authorization_id: str,
        *,
        code: str | None,
        state: str,
        client_id: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        """Finish a sign-in with the query OpenAI redirected the browser to.

        The sign-in is used up by this call whatever its outcome. The code is
        exchanged at OpenAI with the sign-in's verifier, the ID token is
        validated against OpenAI's signing keys, and the granted scopes must
        include plan usage. A reauthorization must sign in to the same ChatGPT
        account (the same ``sub``). A new registration's account becomes the
        user's default when they have none. Returns the account's view.
        """
        pending = await self._repo.take_authorization(
            authorization_id, owner_user_id=user.user_id
        )
        if pending is None:
            raise APIError(
                code="MODEL_ACCOUNT_AUTHORIZATION_NOT_FOUND",
                message=(
                    f"sign-in {authorization_id!r} is not open: it was completed, expired "
                    f"after {AUTHORIZATION_TTL_S // 60} minutes, or was never started by "
                    "this user"
                ),
                status_code=404,
            )
        if not hmac.compare_digest(str(state or ""), pending["state"]):
            raise _authorization_failed("the callback's state does not match the sign-in")
        if error:
            if error == "access_denied":
                raise APIError(
                    code="MODEL_ACCOUNT_AUTHORIZATION_DECLINED",
                    message="the ChatGPT sign-in was declined, so no account was linked",
                    status_code=409,
                )
            raise _authorization_failed(f"OpenAI ended the sign-in with error {error!r}")
        if not code:
            raise _authorization_failed("the callback carries no authorization code")
        issued_client_id = self._issued_client_id(pending, client_id)
        tokens = await self._exchange_code(
            code=code,
            verifier=pending["code_verifier"],
            redirect_uri=pending["redirect_uri"],
            client_id=issued_client_id,
        )
        try:
            claims = await self._verified_claims(
                tokens.id_token, client_id=issued_client_id, nonce=pending["nonce"]
            )
            scopes = tokens.scopes or ""
            if not _plan_usage_granted(scopes):
                raise APIError(
                    code="MODEL_ACCOUNT_PLAN_USAGE_NOT_ENABLED",
                    message=(
                        "the ChatGPT sign-in did not allow AstraBox to use the account's plan "
                        f"(scope {PLAN_USAGE_SCOPE}); sign in again and allow plan usage"
                    ),
                    status_code=409,
                )
            account = await self._save_sign_in(
                user, pending, tokens, claims=claims, issued_client_id=issued_client_id
            )
        except Exception:
            # The grant is discarded, so it is ended at OpenAI rather than left
            # open under the user's connected apps.
            await self._revoke(
                None, refresh_token=tokens.refresh_token, client_id=issued_client_id
            )
            raise
        logger.info(
            "model account signed in: account=%s user=%s reauthorization=%s",
            account["account_id"], user.user_id, pending["account_id"] is not None,
        )
        return _public_view(account)

    @staticmethod
    def _issued_client_id(pending: Mapping[str, Any], client_id: str | None) -> str:
        """The client id the code is exchanged as.

        A new registration's callback carries the client id OpenAI issued;
        without it the registration is incomplete. A reauthorization keeps the
        account's issued id, and a callback naming another one is refused.
        """
        returned = str(client_id or "").strip()
        if pending["client_id"] == DYNAMIC_CLIENT_ID:
            if not returned or returned == DYNAMIC_CLIENT_ID:
                raise _authorization_failed(
                    "the callback of a new registration carries no issued client_id"
                )
            return returned
        if returned and returned != pending["client_id"]:
            raise _authorization_failed(
                "the callback names a different client_id than the account's registration"
            )
        return str(pending["client_id"])

    async def _exchange_code(
        self, *, code: str, verifier: str, redirect_uri: str, client_id: str
    ) -> _TokenSet:
        try:
            response = await _post_form(
                TOKEN_ENDPOINT,
                {
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "code": code,
                    "code_verifier": verifier,
                    "redirect_uri": redirect_uri,
                    "resource": RESOURCE,
                },
            )
        except (httpx.HTTPError, TimeoutError) as exc:
            raise _authorization_failed(
                f"OpenAI's token endpoint could not be reached ({type(exc).__name__})",
                status_code=502,
            ) from exc
        payload = _json_object(response)
        if response.status_code != 200:
            raise _authorization_failed(
                f"OpenAI refused the code exchange (status {response.status_code}, "
                f"error {_error_code(payload) or 'unnamed'})",
                status_code=502,
            )
        tokens = _token_set(payload)
        if tokens is None or not tokens.refresh_token or not tokens.id_token:
            raise _authorization_failed(
                "OpenAI's token answer lacks an access token, refresh token, ID token or "
                "lifetime",
                status_code=502,
            )
        return tokens

    async def _verified_claims(
        self, id_token: str, *, client_id: str, nonce: str
    ) -> dict[str, Any]:
        """The ID token's claims, after checking it was issued by OpenAI to this sign-in.

        The signature is checked against the keys OpenAI publishes, the issuer
        against :data:`ISSUER`, the audience against the issued client id, the
        times with :data:`_ID_TOKEN_LEEWAY_S` of skew, and the nonce against the
        sign-in's.
        """
        try:
            jwks = await _get_json(_discovered_https(await _discovery(), "jwks_uri"))
        except (httpx.HTTPError, TimeoutError, ValueError) as exc:
            raise _authorization_failed(
                f"OpenAI's signing keys could not be read ({exc})", status_code=502
            ) from exc
        try:
            key_id = jwt.get_unverified_header(id_token).get("kid")
            keys = [key for key in jwt.PyJWKSet.from_dict(jwks).keys if key.key_id == key_id]
            if len(keys) != 1:
                raise jwt.InvalidKeyError(f"OpenAI publishes no single signing key {key_id!r}")
            claims: dict[str, Any] = jwt.decode(
                id_token,
                keys[0].key,
                algorithms=_ID_TOKEN_ALGORITHMS,
                audience=client_id,
                issuer=ISSUER,
                leeway=_ID_TOKEN_LEEWAY_S,
                options={"require": ["sub", "exp", "iat", "nonce"]},
            )
        except jwt.PyJWTError as exc:
            raise _authorization_failed(
                f"OpenAI's ID token did not validate ({exc})", status_code=502
            ) from exc
        if not hmac.compare_digest(str(claims["nonce"]), nonce):
            raise _authorization_failed("the ID token was not issued to this sign-in (nonce)")
        if not str(claims["sub"]).strip():
            raise _authorization_failed("the ID token names no subject", status_code=502)
        return claims

    async def _save_sign_in(
        self,
        user: UserContext,
        pending: Mapping[str, Any],
        tokens: _TokenSet,
        *,
        claims: Mapping[str, Any],
        issued_client_id: str,
    ) -> dict[str, Any]:
        subject = str(claims["sub"]).strip()
        email = str(claims.get("email") or "").strip() or None
        scopes = tokens.scopes or ""
        if pending["account_id"] is not None:
            account = await self._owned(
                user.user_id, pending["account_id"], vendor=VENDOR_CHATGPT_PLAN
            )
            if account["subject"] != subject:
                raise _authorization_failed(
                    f"this sign-in is for another ChatGPT account than {account['label']!r}",
                    status_code=409,
                )
        else:
            account = await self._repo.find_registration(
                owner_user_id=user.user_id, subject=subject, issued_client_id=issued_client_id
            )
        if account is not None:
            change = await self._secret_change(
                account["account_id"],
                put={
                    _ACCESS_TOKEN: tokens.access_token,
                    _REFRESH_TOKEN: tokens.refresh_token,
                    _ID_TOKEN: tokens.id_token,
                },
            )
            saved = await self._repo.record_sign_in(
                account["account_id"],
                owner_user_id=user.user_id,
                email=email,
                scopes=scopes,
                access_expires_at=tokens.expires_at,
                secrets=change.apply,
            )
            if saved is None:
                raise _not_found(account["account_id"])
            return saved
        account_id = new_account_id()
        change = await self._secret_change(
            account_id,
            put={
                _ACCESS_TOKEN: tokens.access_token,
                _REFRESH_TOKEN: tokens.refresh_token,
                _ID_TOKEN: tokens.id_token,
            },
        )
        try:
            return await self._repo.create_account(
                {
                    "account_id": account_id,
                    "org_id": getattr(user, "org_id", None) or default_org_id(),
                    "owner_user_id": user.user_id,
                    "vendor": VENDOR_CHATGPT_PLAN,
                    "label": email or "ChatGPT account",
                    "email": email,
                    "subject": subject,
                    "issued_client_id": issued_client_id,
                    "scopes": scopes,
                },
                access_expires_at=tokens.expires_at,
                secrets=change.apply,
            )
        except BaseException:
            # Any failure may come after the store wrote the values, when the
            # commit did not happen.
            await change.discard()
            raise

    # ── the owner's accounts ────────────────────────────────────────────────

    async def list_accounts(self, user: UserContext) -> list[dict[str, Any]]:
        """The user's accounts, oldest first."""
        return [_public_view(account) for account in await self._repo.list_accounts(user.user_id)]

    async def add_api_key(
        self, user: UserContext, *, api_key: str, label: str | None = None
    ) -> dict[str, Any]:
        """Keep an Anthropic API key as one of the user's accounts; the account's view.

        Anthropic must accept the key: it is checked by reading one page of
        Anthropic's model list with it before anything is stored. Without a
        ``label`` the account is named after the key's last characters. The
        user's first key becomes their default Anthropic API key.
        """
        key = _checked_api_key(api_key)
        name = self._label(label) if label is not None else (
            f"Anthropic API key …{key[-_KEY_HINT_CHARS:]}"
        )
        await _anthropic_models(key, account=name, limit=1)
        account_id = new_account_id()
        change = await self._secret_change(account_id, put={_API_KEY: key})
        try:
            account = await self._repo.create_account(
                {
                    "account_id": account_id,
                    "org_id": getattr(user, "org_id", None) or default_org_id(),
                    "owner_user_id": user.user_id,
                    "vendor": VENDOR_ANTHROPIC_API_KEY,
                    "label": name,
                    "email": None,
                    "subject": None,
                    "issued_client_id": None,
                    "scopes": "",
                },
                access_expires_at=None,
                secrets=change.apply,
            )
        except BaseException:
            await change.discard()
            raise
        logger.info(
            "model account added: account=%s user=%s vendor=%s",
            account_id, user.user_id, VENDOR_ANTHROPIC_API_KEY,
        )
        return _public_view(account)

    @staticmethod
    def _label(label: str) -> str:
        label = label.strip()
        if not label or len(label) > _LABEL_MAX_CHARS:
            raise _invalid(f"label must be 1 to {_LABEL_MAX_CHARS} characters")
        return label

    async def update_account(
        self,
        user: UserContext,
        account_id: str,
        *,
        label: str | None = None,
        is_default: bool | None = None,
        shared: bool | None = None,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        """Rename an account, make it (or stop it being) the user's default, share it, or replace its key.

        ``is_default`` is about the account's vendor: making an account the
        default stops the user's other account of that vendor being it.

        A shared account may be spent by other members' conversations whose
        own accounts reached their plan usage limit. Only a ChatGPT account can
        be shared: the team pool is a ChatGPT-plan arrangement, and sharing an
        Anthropic API key is refused with ``MODEL_ACCOUNT_NOT_SHAREABLE``.
        Sharing is refused with ``MODEL_ACCOUNT_POOL_DISABLED`` while the
        deployment's team pool is off; stopping sharing is always accepted, and
        takes effect from the next response of each conversation spending the
        account.

        ``api_key`` replaces an Anthropic API key account's key, after
        Anthropic accepts the new one, as :meth:`add_api_key` checks it; a
        running conversation sends the new key from its next response.
        """
        if label is not None:
            label = self._label(label)
        current = await self._owned(user.user_id, account_id) if shared or api_key is not None else None
        if shared and current is not None and current["vendor"] != VENDOR_CHATGPT_PLAN:
            raise APIError(
                code="MODEL_ACCOUNT_NOT_SHAREABLE",
                message=(
                    f"model account {account_id!r} is a {_VENDOR_NOUNS[current['vendor']]}; "
                    "only a ChatGPT account can be shared with the team pool"
                ),
                status_code=409,
            )
        if shared and not _pool_enabled():
            raise APIError(
                code="MODEL_ACCOUNT_POOL_DISABLED",
                message=(
                    "this deployment's team pool of model accounts is off, so no account can "
                    "be shared; an administrator turns it on with "
                    "ASTRABOX_MODEL_ACCOUNT_POOL_ENABLED"
                ),
                status_code=409,
            )
        if api_key is not None and current is not None:
            key = _checked_api_key(api_key)
            if current["vendor"] != VENDOR_ANTHROPIC_API_KEY:
                raise _invalid(
                    f"model account {account_id!r} is a {_VENDOR_NOUNS[current['vendor']]}, "
                    "which has no API key to replace"
                )
            await _anthropic_models(key, account=account_id, limit=1)
            change = await self._secret_change(account_id, put={_API_KEY: key})
            if await self._repo.replace_secret(
                account_id, owner_user_id=user.user_id, secrets=change.apply
            ) is None:
                raise _not_found(account_id)
            logger.info("model account key replaced: account=%s user=%s", account_id, user.user_id)
        account = await self._repo.update_account(
            account_id,
            owner_user_id=user.user_id,
            label=label,
            is_default=is_default,
            shared=shared,
        )
        if account is None:
            raise _not_found(account_id)
        if shared is not None:
            logger.info(
                "model account sharing changed: account=%s user=%s shared=%s",
                account_id, user.user_id, shared,
            )
        return _public_view(account)

    async def team_pool(self, user: UserContext) -> dict[str, Any]:
        """Whether this deployment's team pool is on, so the user's accounts can be shared."""
        _ = user
        return {"enabled": _pool_enabled()}

    async def delete_account(self, user: UserContext, account_id: str) -> dict[str, Any]:
        """Delete an account and its secrets; a ChatGPT account is signed out at OpenAI first.

        A ChatGPT account's refresh token is revoked at OpenAI's revocation
        endpoint. A failed revocation is logged and does not stop the deletion;
        the answer's ``revocation_confirmed`` is then false, and the user can
        end the session by disconnecting AstraBox in their ChatGPT settings. It
        is ``None`` when the account held no refresh token to revoke, which is
        always so for an Anthropic API key: AstraBox cannot revoke a key, and
        its owner revokes it in the Claude Console. Deleting a default account
        leaves the user without one of that vendor.
        """
        account = await self._owned(user.user_id, account_id)
        refresh_token = await self._store().get(scope=_scope(account_id), key=_REFRESH_TOKEN)
        revoked: bool | None = None
        if refresh_token:
            revoked = await self._revoke(
                account_id, refresh_token=refresh_token, client_id=account["issued_client_id"]
            )
        change = await self._secret_change(account_id, purge=True)
        if not await self._repo.delete_account(
            account_id, owner_user_id=user.user_id, secrets=change.apply
        ):
            raise _not_found(account_id)
        logger.info(
            "model account deleted: account=%s user=%s revocation_confirmed=%s",
            account_id, user.user_id, revoked,
        )
        return {"account_id": account_id, "revocation_confirmed": revoked}

    async def list_models(
        self, user: UserContext, account_id: str, *, vendor: str | None = None
    ) -> list[dict[str, str]]:
        """The models an account can use, as ``{id, display_name}`` in its vendor's order.

        ``account_id`` is one of the user's accounts, or :data:`DEFAULT_ACCOUNT`
        for their default account of ``vendor``, which then has to be named.
        ``id`` is what an Agent names as its model.

        For a ChatGPT account, OpenAI's ``GET /v1/models`` answers the models
        the account's plan offers; only those whose ``visibility`` is ``list``
        are meant for a model picker, and ``id`` is the ``slug``. This raises
        what :meth:`access_token` raises. For an Anthropic API key,
        Anthropic's ``GET /v1/models`` answers every model the key can call,
        newest first. Either raises ``MODEL_ACCOUNT_MODELS_UNAVAILABLE`` when
        the list could not be read, and an Anthropic key the API refuses
        raises ``MODEL_ACCOUNT_API_KEY_REJECTED``.
        """
        if account_id == DEFAULT_ACCOUNT:
            if vendor not in _VENDOR_NOUNS:
                raise _invalid(
                    "the default account is one per vendor; name vendor as one of "
                    f"{sorted(_VENDOR_NOUNS)}"
                )
            account_vendor = str(vendor)
            named: str | None = None
        else:
            account_vendor = (await self._owned(user.user_id, account_id, vendor=vendor))["vendor"]
            named = account_id
        if account_vendor == VENDOR_ANTHROPIC_API_KEY:
            key = await self.api_key(user_id=user.user_id, account_id=named)
            return await _anthropic_models(key, account=account_id)
        token = await self.access_token(user_id=user.user_id, account_id=named)
        try:
            response = await _get_with_token(MODELS_ENDPOINT, token.access_token)
        except (httpx.HTTPError, TimeoutError) as exc:
            raise _models_unavailable(
                token.account_id, f"OpenAI's model list could not be reached ({type(exc).__name__})"
            ) from exc
        if response.status_code != 200:
            raise _models_unavailable(
                token.account_id,
                f"OpenAI's model list answered {response.status_code}",
                retryable=response.status_code >= 500,
            )
        entries = _json_object(response).get("models")
        if not isinstance(entries, list):
            raise _models_unavailable(
                token.account_id, "OpenAI's model list answer has no models array", retryable=False
            )
        models: list[dict[str, str]] = []
        for entry in entries:
            if not isinstance(entry, Mapping) or entry.get("visibility") != _LISTED_MODEL:
                continue
            slug = str(entry.get("slug") or "").strip()
            if not slug:
                raise _models_unavailable(
                    token.account_id,
                    "OpenAI's model list names a listed model without a slug",
                    retryable=False,
                )
            models.append(
                {"id": slug, "display_name": str(entry.get("display_name") or "").strip() or slug}
            )
        return models

    async def _revoke(self, account_id: str | None, *, refresh_token: str, client_id: str) -> bool:
        """End a grant's renewable session at OpenAI; whether OpenAI confirmed it.

        Best effort: a failure is logged and answered ``False``.
        """
        try:
            endpoint = _discovered_https(await _discovery(), "revocation_endpoint")
            response = await _post_form(
                endpoint,
                {
                    "token": refresh_token,
                    "token_type_hint": "refresh_token",
                    "client_id": client_id,
                },
            )
        except (httpx.HTTPError, TimeoutError, ValueError) as exc:
            logger.warning(
                "model account revocation not confirmed: account=%s error=%s",
                account_id, type(exc).__name__,
            )
            return False
        if response.status_code != 200:
            logger.warning(
                "model account revocation not confirmed: account=%s status=%s",
                account_id, response.status_code,
            )
            return False
        return True

    # ── credentials ─────────────────────────────────────────────────────────

    async def api_key(self, *, user_id: str, account_id: str | None = None) -> str:
        """The Anthropic API key of ``account_id``, or of the user's default Anthropic API key.

        ``account_id`` must be one of ``user_id``'s Anthropic API key accounts.
        Raises ``MODEL_ACCOUNT_REQUIRED`` when the user has no default one and
        ``MODEL_ACCOUNT_NOT_FOUND`` for an account that is not theirs or not
        an Anthropic API key.
        """
        account = await self._preferred(user_id, account_id, vendor=VENDOR_ANTHROPIC_API_KEY)
        return (await self._account_token(account)).access_token

    async def access_token(
        self, *, user_id: str, account_id: str | None = None
    ) -> ModelAccountToken:
        """The current access token of ``account_id``, or of the user's default account.

        ``account_id`` must be one of ``user_id``'s ChatGPT accounts. A token inside
        :data:`~astrabox.seams.egress_credentials.ACCESS_TOKEN_RENEWAL_WINDOW_S`
        of its expiry is renewed first, once across every replica. Raises
        ``MODEL_ACCOUNT_REQUIRED`` when the user has no default ChatGPT account,
        ``MODEL_ACCOUNT_NOT_FOUND`` for an account that is not theirs or not a
        ChatGPT account,
        ``MODEL_ACCOUNT_SIGN_IN_REQUIRED`` when OpenAI refused renewal for good,
        ``MODEL_ACCOUNT_PLAN_USAGE_NOT_ENABLED`` when the grant lacks plan
        usage, and ``MODEL_ACCOUNT_REFRESH_FAILED`` when renewal failed
        otherwise.
        """
        return await self._account_token(
            await self._preferred(user_id, account_id, vendor=VENDOR_CHATGPT_PLAN)
        )

    async def release_session_account(self, session_id: str) -> None:
        """Record that ``session_id`` spends no model account.

        A Session on a model endpoint the deployment pays for names no account:
        an account named there would be held back for a usage limit the
        deployment's gateway reported, and shown as the account in use.
        """
        if not session_id:
            return
        sessions = SessionRepository()
        session = await sessions.get_session(session_id)
        if str((session or {}).get("model_account_id") or "").strip():
            await sessions.update_session(
                session_id, {"model_account_id": None}, touch_updated_at=False
            )

    async def conversation_token(
        self,
        *,
        user_id: str,
        vendor: str,
        account_id: str | None = None,
        session_id: str | None = None,
        keep: bool = False,
    ) -> ModelAccountToken:
        """The credential a conversation of ``user_id`` spends now, from an account of ``vendor``.

        ``vendor`` is the kind of account the conversation's model endpoint
        spends; no account of another vendor is ever chosen. ``account_id`` is
        one of the user's accounts of that vendor the conversation's Agent
        names; without it the user's default account of that vendor is
        preferred. The account chosen is recorded on the session
        ``session_id`` names (``model_account_id``):

        * With the team pool off (``ASTRABOX_MODEL_ACCOUNT_POOL_ENABLED``),
          the preferred account.
        * With it on, the preferred account unless a reported usage limit
          holds it back (:meth:`record_usage_limit`). Then the user's other
          active accounts of the vendor, the default first, and after them the
          active accounts of the vendor other members of the session's
          organization shared, oldest first; an account held back is skipped,
          and one whose credential cannot be obtained is logged and skipped.
          ``MODEL_PLAN_USAGE_LIMIT`` (429) when none is left, with the
          preferred account in its evidence. Only a ChatGPT plan reports a
          usage limit and only a ChatGPT account can be shared, so for an
          Anthropic API key this is always the preferred key.

        ``keep`` is for a response in progress: it renews the credential of the
        account the session already spends instead of choosing, so a running
        response never changes accounts, even when a limit reported elsewhere
        holds that account back. ``MODEL_ACCOUNT_NOT_FOUND`` when that account
        was deleted, or its owner stopped sharing it. An account of another
        vendor recorded on the session was spent at another model endpoint and
        is not kept: the account is chosen as without ``keep``. Otherwise raises
        what :meth:`access_token` or :meth:`api_key` raises.
        """
        sessions = SessionRepository()
        session = await sessions.get_session(session_id) if session_id else None
        current = str((session or {}).get("model_account_id") or "").strip()
        org_id = str((session or {}).get("org_id") or "").strip() or default_org_id()
        pool = _pool_enabled()
        kept = await self._repo.get_account(current) if keep and current else None
        if keep and current and (kept is None or kept["vendor"] == vendor):
            if kept is None or not (
                kept["owner_user_id"] == user_id
                or (pool and kept["shared"] and kept["org_id"] == org_id)
            ):
                raise _not_found(current)
            try:
                return await self._account_token(kept)
            except APIError as exc:
                if kept["owner_user_id"] == user_id:
                    raise
                # The errors name the account by its label, which is its
                # owner's, and tell its owner how to sign in again.
                raise APIError(
                    code=exc.code,
                    message=f"the shared ChatGPT account {current!r} cannot be used now ({exc.code})",
                    status_code=exc.status_code,
                    retryable=exc.retryable,
                ) from exc
        preferred = await self._preferred(user_id, account_id, vendor=vendor)
        if pool and _usage_limited(preferred):
            token = await self._pooled_token(preferred, user_id=user_id, org_id=org_id)
        else:
            token = await self._account_token(preferred)
        if session_id and token.account_id != current:
            await sessions.update_session(
                session_id, {"model_account_id": token.account_id}, touch_updated_at=False
            )
            logger.info(
                "conversation model account chosen: session=%s account=%s previous=%s",
                session_id, token.account_id, current or None,
            )
        return token

    async def _pooled_token(
        self, preferred: Mapping[str, Any], *, user_id: str, org_id: str
    ) -> ModelAccountToken:
        """The credential of the first account of ``preferred``'s vendor the pool offers in its place."""
        vendor = str(preferred["vendor"])
        # sorted() keeps the repository's oldest-first order among equals.
        own = sorted(
            (
                account
                for account in await self._repo.list_accounts(user_id)
                if account["account_id"] != preferred["account_id"]
                and account["vendor"] == vendor
                and account["status"] == STATUS_ACTIVE
            ),
            key=lambda account: not account["is_default"],
        )
        shared = await self._repo.list_pool_accounts(
            org_id=org_id, excluding_owner=user_id, vendor=vendor
        )
        for account in [*own, *shared]:
            if _usage_limited(account):
                continue
            try:
                token = await self._account_token(account)
            except APIError as exc:
                logger.warning(
                    "model account passed over by the pool: account=%s code=%s",
                    account["account_id"], exc.code,
                )
                continue
            if account["owner_user_id"] != user_id:
                await self._repo.record_pool_use(account["account_id"])
            return token
        raise _usage_limit_reached(preferred)

    async def record_usage_limit(self, account_id: str) -> dict[str, Any]:
        """Hold the account back from conversations after its vendor reported its usage limit.

        The hold lasts :data:`USAGE_LIMIT_HOLD_S`. Returns what a failed
        response reports about it: the stable ``code``, the ``account_id``,
        and ``resets_at``, which is ``None`` because OpenAI's plan-usage error
        names no reset time.
        """
        until = (utcnow() + timedelta(seconds=USAGE_LIMIT_HOLD_S)).isoformat()
        await self._repo.limit_usage(account_id, until=until)
        logger.warning(
            "model account reached its plan usage limit: account=%s held_until=%s",
            account_id, until,
        )
        return {"code": PLAN_USAGE_LIMIT, "account_id": account_id, "resets_at": None}

    async def _preferred(
        self, user_id: str, account_id: str | None, *, vendor: str
    ) -> dict[str, Any]:
        """``account_id``, which must be the user's and of ``vendor``, or the user's default of ``vendor``."""
        if account_id:
            return await self._owned(user_id, account_id, vendor=vendor)
        found = await self._repo.get_default_account(user_id, vendor=vendor)
        if found is None:
            raise APIError(
                code="MODEL_ACCOUNT_REQUIRED", message=_NO_DEFAULT[vendor], status_code=409
            )
        return found

    async def _account_token(self, account: Mapping[str, Any]) -> ModelAccountToken:
        """The account's current credential: an Anthropic API key as stored, or a ChatGPT
        access token, renewed first when it is due."""
        if account["vendor"] == VENDOR_ANTHROPIC_API_KEY:
            key = await self._store().get(scope=_scope(account["account_id"]), key=_API_KEY)
            if not key:
                raise _not_found(account["account_id"], VENDOR_ANTHROPIC_API_KEY)
            return ModelAccountToken(account_id=account["account_id"], access_token=key, expires_at=None)
        if account["status"] != STATUS_ACTIVE:
            raise _sign_in_required(account, f"OpenAI refused renewal ({account['last_error_code']})")
        if _due(account["access_expires_at"]):
            account = await self._refresh_once(account)
        if not _plan_usage_granted(account["scopes"]):
            raise APIError(
                code="MODEL_ACCOUNT_PLAN_USAGE_NOT_ENABLED",
                message=(
                    f"ChatGPT account {account['label']!r} no longer grants plan usage "
                    f"(scope {PLAN_USAGE_SCOPE}); sign in again and allow it"
                ),
                status_code=409,
            )
        token = await self._store().get(scope=_scope(account["account_id"]), key=_ACCESS_TOKEN)
        if not token or not account["access_expires_at"]:
            raise _sign_in_required(account, "AstraBox holds no access token for it")
        return ModelAccountToken(
            account_id=account["account_id"],
            access_token=token,
            expires_at=parse_iso(account["access_expires_at"]),
        )

    async def _refresh_once(self, account: Mapping[str, Any]) -> dict[str, Any]:
        """Renew the account's access token once across every replica; the account after it.

        OpenAI rotates the refresh token on every renewal and refuses a used
        one, so two replicas must never renew the same token. The renewal runs
        only under a claim taken by compare-and-set on the account's row
        (:meth:`~astrabox.persistence.repository.model_account_repository.ModelAccountRepository.claim_refresh`):
        it succeeds only while the row still carries the expiry this caller
        judged due and no live claim exists. A caller that does not get the
        claim waits for the holder's result, re-reading the row until its
        expiry moves; if the holder gives the claim up without a result, the
        waiting caller takes it and tries in turn. Waiting ends with an error
        after :data:`_REFRESH_CLAIM_S`.
        """
        account_id = str(account["account_id"])
        observed = account["access_expires_at"]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _REFRESH_CLAIM_S
        while True:
            claimed = await self._repo.claim_refresh(
                account_id, expires_at=observed, lease_s=_REFRESH_CLAIM_S
            )
            if claimed is not None:
                return await self._refresh_claimed(claimed)
            current = await self._repo.get_account(account_id)
            if current is None:
                raise _not_found(account_id)
            if current["status"] != STATUS_ACTIVE:
                raise _sign_in_required(
                    current, f"OpenAI refused renewal ({current['last_error_code']})"
                )
            if current["access_expires_at"] != observed:
                return current
            if loop.time() >= deadline:
                raise _refresh_failed(
                    account_id,
                    "another request is renewing the access token and did not finish within "
                    f"{_REFRESH_CLAIM_S:.0f}s",
                )
            await asyncio.sleep(_REFRESH_POLL_S)

    async def _refresh_claimed(self, account: Mapping[str, Any]) -> dict[str, Any]:
        """Renew under the claim ``account`` carries, and record the result.

        The new tokens and the new expiry commit together, and committing
        releases the claim. A refusal in :data:`_SIGN_IN_AGAIN_ERRORS` removes
        the access and refresh tokens and marks the account
        ``sign_in_required``; the ID token stays, to identify the account in
        the next sign-in. Any other failure gives the claim up so the next
        caller can try at once instead of after the claim lapses.
        """
        account_id = str(account["account_id"])
        claim = str(account["refresh_claimed_until"])
        try:
            refresh_token = await self._store().get(scope=_scope(account_id), key=_REFRESH_TOKEN)
            if not refresh_token:
                raise _sign_in_required(account, "AstraBox holds no refresh token for it")
            try:
                response = await _post_form(
                    TOKEN_ENDPOINT,
                    {
                        "grant_type": "refresh_token",
                        "client_id": str(account["issued_client_id"]),
                        "refresh_token": refresh_token,
                        "resource": RESOURCE,
                    },
                )
            except (httpx.HTTPError, TimeoutError) as exc:
                raise _refresh_failed(
                    account_id, f"OpenAI's token endpoint could not be reached ({type(exc).__name__})"
                ) from exc
            payload = _json_object(response)
            if response.status_code != 200:
                error_code = _error_code(payload)
                if error_code in _SIGN_IN_AGAIN_ERRORS:
                    removal = await self._secret_change(
                        account_id, remove=(_ACCESS_TOKEN, _REFRESH_TOKEN)
                    )
                    await self._repo.require_sign_in(
                        account_id, claim=claim, error_code=error_code, secrets=removal.apply
                    )
                    logger.warning(
                        "model account needs a new sign-in: account=%s error=%s",
                        account_id, error_code,
                    )
                    raise _sign_in_required(account, f"OpenAI refused renewal ({error_code})")
                raise _refresh_failed(
                    account_id,
                    f"OpenAI refused renewal (status {response.status_code}, "
                    f"error {error_code or 'unnamed'})",
                    retryable=response.status_code >= 500,
                )
            tokens = _token_set(payload)
            if tokens is None:
                raise _refresh_failed(
                    account_id, "OpenAI's renewal answer lacks an access token or lifetime"
                )
            put = {_ACCESS_TOKEN: tokens.access_token}
            # A renewal answer without a refresh or ID token leaves the stored one current.
            if tokens.refresh_token:
                put[_REFRESH_TOKEN] = tokens.refresh_token
            if tokens.id_token:
                put[_ID_TOKEN] = tokens.id_token
            change = await self._secret_change(account_id, put=put)
            refreshed = await self._repo.complete_refresh(
                account_id,
                claim=claim,
                access_expires_at=tokens.expires_at,
                scopes=tokens.scopes or str(account["scopes"]),
                secrets=change.apply,
            )
        except BaseException:
            try:
                await self._repo.release_refresh(account_id, claim=claim)
            except Exception:
                logger.warning(
                    "model account refresh claim not released; it lapses after %.0fs: "
                    "account=%s",
                    _REFRESH_CLAIM_S,
                    account_id,
                    exc_info=True,
                )
            raise
        if refreshed is None:
            if await self._repo.get_account(account_id) is None:
                raise _not_found(account_id)
            raise _refresh_failed(
                account_id,
                "the account changed while its token was renewed: it was signed in again, "
                "or the renewal outlasted its claim",
            )
        logger.info(
            "model account refreshed: account=%s expires_at=%s", account_id, tokens.expires_at
        )
        return refreshed


_service: ModelAccountService | None = None


def get_model_account_service() -> ModelAccountService:
    """The process's model-account service."""
    global _service
    if _service is None:
        _service = ModelAccountService()
    return _service


__all__ = [
    "ANTHROPIC_MODELS_ENDPOINT",
    "ANTHROPIC_VERSION",
    "AUTHORIZE_ENDPOINT",
    "DEFAULT_ACCOUNT",
    "MODELS_ENDPOINT",
    "ModelAccountService",
    "ext_agent_host_id",
    "ModelAccountToken",
    "VENDOR_ANTHROPIC_API_KEY",
    "VENDOR_CHATGPT_PLAN",
    "get_model_account_service",
]
