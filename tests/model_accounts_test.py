"""ChatGPT model accounts on the PostgreSQL test database, against a stand-in for OpenAI.

The stand-in answers OpenAI's discovery document, signing keys, token endpoint
and revocation endpoint through an httpx ``MockTransport``, so every request the
service builds is encoded as it would be on the wire. It signs ID tokens with
its own RSA key and checks the PKCE verifier against the challenge the
authorization URL carried. Its refresh tokens rotate on every use and a spent
one is refused, as OpenAI's are.

The concurrency case races real transactions: each call takes its own pooled
connection, so the database, not the order of the coroutines, decides which
caller renews.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from sqlalchemy import update

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.model_accounts import service as service_module
from astrabox.core.service.model_accounts.service import (
    AUTHORIZE_ENDPOINT,
    DISCOVERY_URL,
    ISSUER,
    RESOURCE,
    TOKEN_ENDPOINT,
    ModelAccountService,
)
from astrabox.persistence.installation import load_installation_id
from astrabox.persistence.sql import execute
from astrabox.persistence.sql.models.model_accounts import ModelAccount
from astrabox.seams.secrets import secret_store_for_name

pytestmark = pytest.mark.usefixtures("database")

_USER = UserContext(user_id="plan-user")
_REDIRECT_URI = "http://127.0.0.1:1455/auth/callback"
_SCOPE = "chatgpt.tokens.use.direct email offline_access openid profile resource.invoke"
_JWKS_URI = "https://auth.openai.com/.well-known/jwks.json"
_REVOCATION_ENDPOINT = "https://auth.openai.com/oauth/revoke"

_RealAsyncClient = httpx.AsyncClient


def _form(request: httpx.Request) -> dict[str, str]:
    return dict(parse_qsl(request.content.decode()))


def _query(url: str) -> dict[str, str]:
    return {name: values[0] for name, values in parse_qs(urlsplit(url).query).items()}


class _OpenAI:
    """OpenAI's authorization server as the plan-usage guide describes it."""

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.jwk = {
            **json.loads(RSAAlgorithm.to_jwk(self.key.public_key())),
            "kid": "key-1", "alg": "RS256", "use": "sig",
        }
        self.issued_client_id = "oaiapp_first"
        self.subject = "chatgpt-user-1"
        self.email = "person@example.com"
        self.approved: dict[str, dict[str, str]] = {}
        self.code_exchanges: list[dict[str, str]] = []
        self.refreshes: list[str] = []
        self.refresh_token = "rt-1"
        self.refusal: dict[str, Any] | None = None
        self.id_tokens: list[str] = []

    def approve(self, authorize_url: str) -> dict[str, str]:
        """The query the browser brings back once the user approves the sign-in."""
        params = _query(authorize_url)
        code = f"code-{uuid.uuid4().hex}"
        self.approved[code] = params
        returned = {"code": code, "state": params["state"], "scope": _SCOPE}
        if params["client_id"] == "dynamic_agent_client":
            returned["client_id"] = self.issued_client_id
        return returned

    def _id_token(self, nonce: str, client_id: str) -> str:
        now = int(time.time())
        token = jwt.encode(
            {
                "iss": ISSUER, "aud": [client_id], "sub": self.subject, "email": self.email,
                "iat": now, "exp": now + 3600, "nonce": nonce,
            },
            self.key, algorithm="RS256", headers={"kid": "key-1"},
        )
        self.id_tokens.append(token)
        return token

    async def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == DISCOVERY_URL:
            return httpx.Response(200, json={
                "issuer": ISSUER, "jwks_uri": _JWKS_URI,
                "revocation_endpoint": _REVOCATION_ENDPOINT,
            })
        if url == _JWKS_URI:
            return httpx.Response(200, json={"keys": [self.jwk]})
        if url == _REVOCATION_ENDPOINT:
            return httpx.Response(200)
        if url != TOKEN_ENDPOINT:
            return httpx.Response(404)
        form = _form(request)
        if form["grant_type"] == "authorization_code":
            self.code_exchanges.append(form)
            params = self.approved.pop(form["code"])
            digest = hashlib.sha256(form["code_verifier"].encode()).digest()
            challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
            if challenge != params["code_challenge"] or form["redirect_uri"] != params["redirect_uri"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={
                "access_token": "at-signed-in", "refresh_token": self.refresh_token,
                "id_token": self._id_token(params["nonce"], form["client_id"]),
                "token_type": "Bearer", "expires_in": 3600, "scope": _SCOPE,
            })
        self.refreshes.append(form["refresh_token"])
        # The other callers arrive while this renewal is in flight.
        await asyncio.sleep(0.3)
        if self.refusal is not None:
            return httpx.Response(400, json=self.refusal)
        if form["refresh_token"] != self.refresh_token:
            return httpx.Response(400, json={"error": {"code": "refresh_token_reused"}})
        self.refresh_token = f"rt-{len(self.refreshes) + 1}"
        return httpx.Response(200, json={
            "access_token": f"at-renewed-{len(self.refreshes)}",
            "refresh_token": self.refresh_token, "expires_in": 3600, "scope": _SCOPE,
        })


@pytest.fixture(autouse=True)
def _master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fixed master key, so the local store needs no key file."""
    monkeypatch.delenv("ASTRABOX_SECRET_STORE", raising=False)
    monkeypatch.setenv(
        "ASTRABOX_VAULT_MASTER_KEY", base64.urlsafe_b64encode(b"k" * 32).decode()
    )


@pytest.fixture
def openai(monkeypatch: pytest.MonkeyPatch) -> _OpenAI:
    fake = _OpenAI()
    monkeypatch.setattr(
        service_module.httpx, "AsyncClient",
        functools.partial(_RealAsyncClient, transport=httpx.MockTransport(fake.handle)),
    )
    return fake


async def _link(service: ModelAccountService, openai: _OpenAI) -> dict[str, Any]:
    started = await service.start_authorization(_USER, redirect_uri=_REDIRECT_URI)
    returned = openai.approve(started["authorize_url"])
    return await service.complete_authorization(
        _USER, started["authorization_id"], code=returned["code"], state=returned["state"],
        client_id=returned.get("client_id"),
    )


async def _secret(account_id: str, key: str) -> str | None:
    return await secret_store_for_name(None).get(scope=f"model-account/{account_id}", key=key)


async def _expire(account_id: str) -> None:
    table = ModelAccount.__table__
    await execute(
        update(table)
        .where(table.c.account_id == account_id)
        .values(access_expires_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    )


async def test_authorize_urls_carry_exactly_the_contract_parameters(openai: _OpenAI) -> None:
    """A first registration and a reauthorization each send the guide's parameter set.

    A missing ``ext_agent_host_id`` or a reauthorization that still sends
    ``agent_name_hint`` (or the dynamic client id) is refused or registers a
    second client at OpenAI, which only shows on a real sign-in.
    """
    service = ModelAccountService()
    with pytest.raises(APIError) as refused:
        await service.start_authorization(
            _USER, redirect_uri="http://localhost:1455/auth/callback"
        )
    assert refused.value.code == "INVALID_REQUEST"

    started = await service.start_authorization(_USER, redirect_uri=_REDIRECT_URI)
    url = started["authorize_url"]
    assert url.startswith(f"{AUTHORIZE_ENDPOINT}?")
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A1455%2Fauth%2Fcallback" in url
    host_id = f"urn:uuid:{uuid.UUID(hex=await load_installation_id())}"
    first = _query(url)
    challenge = first.pop("code_challenge")
    state, nonce = first.pop("state"), first.pop("nonce")
    assert first == {
        "client_id": "dynamic_agent_client",
        "agent_name_hint": "AstraBox",
        "ext_agent_host_id": host_id,
        "response_type": "code",
        "redirect_uri": _REDIRECT_URI,
        "scope": "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct",
        "resource": RESOURCE,
        "code_challenge_method": "S256",
    }
    assert challenge and state and nonce and state != nonce

    returned = openai.approve(url)
    account = await service.complete_authorization(
        _USER, started["authorization_id"], code=returned["code"], state=returned["state"],
        client_id=returned["client_id"],
    )
    again = _query(
        (
            await service.start_authorization(
                _USER, redirect_uri="http://127.0.0.1:54321/auth/callback",
                account_id=account["account_id"],
            )
        )["authorize_url"]
    )
    assert {name: again[name] for name in ("client_id", "ext_agent_host_id", "id_token_hint", "login_hint")} == {
        "client_id": "oaiapp_first",
        "ext_agent_host_id": host_id,
        "id_token_hint": openai.id_tokens[-1],
        "login_hint": "person@example.com",
    }
    assert set(again) == {
        "client_id", "ext_agent_host_id", "id_token_hint", "login_hint", "response_type",
        "redirect_uri", "scope", "resource", "state", "nonce", "code_challenge_method",
        "code_challenge",
    }


async def test_code_exchange_stores_the_account_and_no_view_carries_a_token(
    openai: _OpenAI,
) -> None:
    """The code is exchanged as the issued client, and the tokens stay in the store.

    The exchange must name the issued client id (never ``dynamic_agent_client``)
    with the attempt's verifier, redirect URI and resource, or OpenAI refuses
    it. The user's first account becomes their default; a second registration
    does not take the default over.
    """
    service = ModelAccountService()
    account = await _link(service, openai)

    [exchange] = openai.code_exchanges
    assert {name: value for name, value in exchange.items() if name not in ("code", "code_verifier")} == {
        "grant_type": "authorization_code",
        "client_id": "oaiapp_first",
        "redirect_uri": _REDIRECT_URI,
        "resource": RESOURCE,
    }
    assert account["status"] == "active"
    assert account["is_default"] is True
    assert account["email"] == account["label"] == "person@example.com"
    assert await _secret(account["account_id"], "access_token") == "at-signed-in"
    assert await _secret(account["account_id"], "refresh_token") == "rt-1"
    assert await _secret(account["account_id"], "id_token") == openai.id_tokens[-1]

    openai.issued_client_id = "oaiapp_second"
    second = await _link(service, openai)
    assert second["is_default"] is False
    listed = await service.list_accounts(_USER)
    assert [row["account_id"] for row in listed] == [account["account_id"], second["account_id"]]
    rendered = json.dumps([account, second, listed])
    for token in ("at-signed-in", "rt-1", *openai.id_tokens):
        assert token not in rendered


async def test_concurrent_callers_renew_an_expired_token_once(openai: _OpenAI) -> None:
    """Five turns find the same access token expired at once.

    OpenAI rotates the refresh token on every renewal and refuses a spent one,
    so a second renewal would fail and could end the grant. Only one exchange
    may happen; the other callers wait for it, and the replacement refresh
    token is what the next renewal presents.
    """
    service = ModelAccountService()
    account = await _link(service, openai)
    await _expire(account["account_id"])

    tokens = await asyncio.gather(
        *(service.access_token(user_id=_USER.user_id) for _ in range(5))
    )

    assert openai.refreshes == ["rt-1"]
    assert {token.access_token for token in tokens} == {"at-renewed-1"}
    assert all(token.expires_at > datetime.now(timezone.utc) for token in tokens)
    assert await _secret(account["account_id"], "refresh_token") == "rt-2"
    assert "at-renewed-1" not in repr(tokens)


@pytest.mark.parametrize(
    "refusal, code",
    [
        ({"error": "invalid_grant", "error_description": "expired"}, "invalid_grant"),
        (
            {"error": {"code": "refresh_token_reused", "type": "invalid_request_error",
                       "message": "already used"}},
            "refresh_token_reused",
        ),
    ],
)
async def test_a_terminal_refresh_error_requires_a_new_sign_in(
    openai: _OpenAI, refusal: dict[str, Any], code: str
) -> None:
    """A refusal OpenAI lists under "sign in again" ends the account's tokens.

    Both the OAuth shape (``error`` string) and OpenAI's API shape
    (``error.code``) carry the verdict. Read wrongly, the account would stay
    active and every turn would retry a dead refresh token.
    """
    service = ModelAccountService()
    account = await _link(service, openai)
    await _expire(account["account_id"])
    openai.refusal = refusal

    with pytest.raises(APIError) as failure:
        await service.access_token(user_id=_USER.user_id)

    assert failure.value.code == "MODEL_ACCOUNT_SIGN_IN_REQUIRED"
    [listed] = await service.list_accounts(_USER)
    assert listed["status"] == "sign_in_required"
    assert listed["last_error_code"] == code
    assert await _secret(account["account_id"], "access_token") is None
    assert await _secret(account["account_id"], "refresh_token") is None
    # The ID token identifies the account to the next sign-in.
    assert await _secret(account["account_id"], "id_token") == openai.id_tokens[-1]

    with pytest.raises(APIError) as again:
        await service.access_token(user_id=_USER.user_id)
    assert again.value.code == "MODEL_ACCOUNT_SIGN_IN_REQUIRED"
    assert len(openai.refreshes) == 1
