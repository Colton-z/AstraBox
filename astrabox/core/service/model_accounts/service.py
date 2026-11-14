"""People's ChatGPT accounts, linked through OpenAI's "Sign in with ChatGPT".

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
* **Accounts.** A user may link several accounts; one is their default, and
  nothing switches between them on its own. Each registration keeps its own
  issued client id; the installation is one host
  (``ext_agent_host_id``, derived from the installation id).
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
from astrabox.common.utils.time_utils import parse_iso, utcnow
from astrabox.common.utils.user_context import UserContext, default_org_id
from astrabox.core.service.secret_changes import SecretStoreChange, prepare_secret_change
from astrabox.persistence.installation import load_installation_id
from astrabox.persistence.repository.model_account_repository import (
    STATUS_ACTIVE,
    ModelAccountRepository,
    new_account_id,
)
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

#: Bound on one request to OpenAI's authorization server, connection included.
_HTTP_TIMEOUT_S = 30.0

#: How long a refresh claim lasts. It exceeds :data:`_HTTP_TIMEOUT_S` with room
#: to record the result, so a claim held by a live renewal never lapses under
#: it; one held by a crashed replica lapses after this.
_REFRESH_CLAIM_S = 60.0

#: How often a caller waiting on another replica's renewal re-reads the account.
_REFRESH_POLL_S = 0.25

_LABEL_MAX_CHARS = 200

#: Secret-store keys under an account's scope.
_ACCESS_TOKEN = "access_token"
_REFRESH_TOKEN = "refresh_token"
_ID_TOKEN = "id_token"


@dataclass(frozen=True)
class ModelAccountToken:
    """An account's current access token, and when it expires."""

    account_id: str
    #: ``repr=False``: a logged token object must not print the token.
    access_token: str = field(repr=False)
    expires_at: datetime


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


def _not_found(account_id: str) -> APIError:
    return APIError(
        code="MODEL_ACCOUNT_NOT_FOUND",
        message=f"model account {account_id!r} not found",
        status_code=404,
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


def _due(expires_at: str | None) -> bool:
    """Whether an access token expiring at ``expires_at`` needs renewing now."""
    if not expires_at:
        return True
    return parse_iso(expires_at) <= utcnow() + timedelta(seconds=ACCESS_TOKEN_RENEWAL_WINDOW_S)


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
        "created_at": account["created_at"],
        "updated_at": account["updated_at"],
    }


async def _post_form(url: str, data: Mapping[str, str]) -> httpx.Response:
    async with asyncio.timeout(_HTTP_TIMEOUT_S):
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_HTTP_TIMEOUT_S, connect=10.0), follow_redirects=False
        ) as client:
            return await client.post(url, data=dict(data), headers={"Accept": "application/json"})


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

    async def _owned(self, user_id: str, account_id: str) -> dict[str, Any]:
        account = await self._repo.get_account(account_id)
        if account is None or account["owner_user_id"] != user_id:
            raise _not_found(account_id)
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
        account = await self._owned(user.user_id, account_id) if account_id else None
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
            account = await self._owned(user.user_id, pending["account_id"])
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

    async def update_account(
        self,
        user: UserContext,
        account_id: str,
        *,
        label: str | None = None,
        is_default: bool | None = None,
    ) -> dict[str, Any]:
        """Rename an account, or make it (or stop it being) the user's default."""
        if label is not None:
            label = label.strip()
            if not label or len(label) > _LABEL_MAX_CHARS:
                raise _invalid(f"label must be 1 to {_LABEL_MAX_CHARS} characters")
        account = await self._repo.update_account(
            account_id, owner_user_id=user.user_id, label=label, is_default=is_default
        )
        if account is None:
            raise _not_found(account_id)
        return _public_view(account)

    async def delete_account(self, user: UserContext, account_id: str) -> dict[str, Any]:
        """Sign the account out at OpenAI, then delete it and its tokens.

        The refresh token is revoked at OpenAI's revocation endpoint first.
        A failed revocation is logged and does not stop the deletion; the
        answer's ``revocation_confirmed`` is then false, and the user can end
        the session by disconnecting AstraBox in their ChatGPT settings. It is
        ``None`` when the account held no refresh token to revoke. Deleting the
        default account leaves the user without one.
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

    # ── access tokens ───────────────────────────────────────────────────────

    async def access_token(
        self, *, user_id: str, account_id: str | None = None
    ) -> ModelAccountToken:
        """The current access token of ``account_id``, or of the user's default account.

        ``account_id`` must belong to ``user_id``. A token inside
        :data:`~astrabox.seams.egress_credentials.ACCESS_TOKEN_RENEWAL_WINDOW_S`
        of its expiry is renewed first, once across every replica. Raises
        ``MODEL_ACCOUNT_REQUIRED`` when the user has no default account,
        ``MODEL_ACCOUNT_NOT_FOUND`` for an account that is not theirs,
        ``MODEL_ACCOUNT_SIGN_IN_REQUIRED`` when OpenAI refused renewal for good,
        ``MODEL_ACCOUNT_PLAN_USAGE_NOT_ENABLED`` when the grant lacks plan
        usage, and ``MODEL_ACCOUNT_REFRESH_FAILED`` when renewal failed
        otherwise.
        """
        if account_id:
            account = await self._owned(user_id, account_id)
        else:
            found = await self._repo.get_default_account(user_id)
            if found is None:
                raise APIError(
                    code="MODEL_ACCOUNT_REQUIRED",
                    message=(
                        "no default ChatGPT account is linked for this user; link one with "
                        "`astrabox model-account login chatgpt` or choose a default"
                    ),
                    status_code=409,
                )
            account = found
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
    "AUTHORIZE_ENDPOINT",
    "ModelAccountService",
    "ext_agent_host_id",
    "ModelAccountToken",
    "get_model_account_service",
]
