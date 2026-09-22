"""Built-in OIDC login: session/flow tokens, the resolver, the routes, the guard.

Conventions mirror ``tests/identity_sso_test.py``: config is read at
construction/registration, so every test sets env via ``monkeypatch.setenv``
and builds fresh; the module-level session-secret cache is reset per test.

The callback happy path runs the REAL verification code — an RSA key pair is
generated in-test, the stub IdP signs an id_token with it, ``PyJWKClient`` is
stubbed to hand back the public key, and the token exchange goes through an
``httpx.MockTransport`` — so audience/issuer/signature checking is exercised,
not mocked away.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import astrabox.api.routes.auth as auth_routes
import astrabox.identity.oidc as identity_oidc
import astrabox.providers.identity_oidc as oidc
from astrabox.common.utils.errors import APIError
from astrabox.identity.oidc import IdentityPrincipal

_SESSION_SECRET = "unit-test-session-secret-0123456789abcdef"
_REAL_ASYNC_CLIENT = httpx.AsyncClient

_OIDC_ENV_VARS = (
    "ASTRABOX_OIDC_ISSUER",
    "ASTRABOX_OIDC_INTERNAL_ISSUER",
    "ASTRABOX_OIDC_CLIENT_ID",
    "ASTRABOX_OIDC_CLIENT_SECRET",
    "ASTRABOX_OIDC_CLIENT_SECRET_FILE",
    "ASTRABOX_OIDC_API_CLIENT_ID",
    "ASTRABOX_OIDC_API_CLIENT_SECRET",
    "ASTRABOX_OIDC_API_CLIENT_SECRET_FILE",
    "ASTRABOX_OIDC_SCOPES",
    "ASTRABOX_OIDC_GROUPS_CLAIM",
    "ASTRABOX_OIDC_ADMIN_GROUP",
    "ASTRABOX_OIDC_STRICT",
    "ASTRABOX_OIDC_REDIRECT_URL",
    "ASTRABOX_AUTH_SESSION_SECRET",
    "ASTRABOX_AUTH_SESSION_TTL_SECONDS",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    for name in _OIDC_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ASTRABOX_AUTH_SESSION_SECRET", _SESSION_SECRET)
    oidc._cached_session_secret = None
    oidc._discovery_cache.clear()
    yield
    oidc._cached_session_secret = None
    oidc._discovery_cache.clear()


def _set_oidc_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRABOX_OIDC_ISSUER", "https://idp.example.com")
    monkeypatch.setenv("ASTRABOX_OIDC_CLIENT_ID", "astrabox-console")
    monkeypatch.setenv("ASTRABOX_OIDC_CLIENT_SECRET", "s3cret")


def test_oidc_client_secret_can_come_from_a_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret = tmp_path / "oidc-client-secret"
    secret.write_text("file-secret\n", encoding="utf-8")
    monkeypatch.setenv("ASTRABOX_OIDC_ISSUER", "https://idp.example.com")
    monkeypatch.setenv("ASTRABOX_OIDC_CLIENT_ID", "astrabox-console")
    monkeypatch.setenv("ASTRABOX_OIDC_CLIENT_SECRET_FILE", str(secret))

    assert oidc.OidcProviderConfig.load().client_secret == "file-secret"


def test_oidc_client_secret_refuses_ambiguous_or_empty_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ASTRABOX_OIDC_ISSUER", "https://idp.example.com")
    monkeypatch.setenv("ASTRABOX_OIDC_CLIENT_ID", "astrabox-console")
    secret = tmp_path / "oidc-client-secret"
    secret.write_text("\n", encoding="utf-8")
    monkeypatch.setenv("ASTRABOX_OIDC_CLIENT_SECRET_FILE", str(secret))
    with pytest.raises(RuntimeError, match="is empty"):
        oidc.OidcProviderConfig.load()

    secret.write_text("file-secret\n", encoding="utf-8")
    monkeypatch.setenv("ASTRABOX_OIDC_CLIENT_SECRET", "inline-secret")
    with pytest.raises(RuntimeError, match="set only one"):
        oidc.OidcProviderConfig.load()


def test_oidc_api_client_requires_a_complete_long_lived_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_oidc_env(monkeypatch)
    monkeypatch.setenv("ASTRABOX_OIDC_API_CLIENT_ID", "astrabox-api")
    with pytest.raises(RuntimeError, match="requires both"):
        oidc.OidcProviderConfig.load()

    secret = tmp_path / "oidc-api-client-secret"
    secret.write_text("api-file-secret\n", encoding="utf-8")
    monkeypatch.setenv("ASTRABOX_OIDC_API_CLIENT_SECRET_FILE", str(secret))
    config = oidc.OidcProviderConfig.load()
    assert config.api_client_id == "astrabox-api"
    assert config.api_client_secret == "api-file-secret"


# ── session / flow tokens ──────────────────────────────────────────────────


def test_session_token_roundtrip():
    token = oidc.mint_session_token(
        user_id="u-1", email="a@b.c", display_name="Ada", roles=["admin"]
    )
    claims = oidc.verify_session_token(token)
    assert claims["sub"] == "u-1"
    assert claims["email"] == "a@b.c"
    assert claims["roles"] == ["admin"]


def test_session_token_rejects_tamper_and_expiry():
    with pytest.raises(jwt.PyJWTError):
        oidc.verify_session_token(
            jwt.encode({"use": "session", "sub": "u"}, "wrong-secret-0123456789abcdef-pad", algorithm="HS256")
        )
    expired = jwt.encode(
        {"use": "session", "sub": "u", "exp": int(time.time()) - 10},
        _SESSION_SECRET,
        algorithm="HS256",
    )
    with pytest.raises(jwt.PyJWTError):
        oidc.verify_session_token(expired)


def test_flow_and_session_tokens_are_not_interchangeable():
    flow = oidc.mint_flow_token(state="s", code_verifier="v", next_url="/x")
    with pytest.raises(jwt.PyJWTError):
        oidc.verify_session_token(flow)
    session = oidc.mint_session_token(user_id="u", email=None, display_name=None, roles=[])
    with pytest.raises(jwt.PyJWTError):
        oidc.verify_flow_token(session)


def test_roles_map_org_prefixed_groups(monkeypatch: pytest.MonkeyPatch):
    """IdPs prefix groups with a container path — Casdoor ``org/group``,
    Keycloak ``/group`` — and membership must still map (verified against a
    real Casdoor 3.128 id_token, which emits ``astrabox/astrabox-admin``)."""
    _set_oidc_env(monkeypatch)
    config = oidc.OidcProviderConfig.load()
    assert oidc.roles_from_claims({"groups": ["astrabox/astrabox-admin"]}, config) == ["admin"]
    assert oidc.roles_from_claims({"groups": ["/astrabox-admin"]}, config) == ["admin"]
    assert oidc.roles_from_claims({"groups": ["astrabox-admin"]}, config) == ["admin"]
    assert oidc.roles_from_claims({"groups": ["astrabox/other"]}, config) == []
    assert oidc.roles_from_claims({}, config) == []


def test_pkce_pair_is_s256():
    import base64
    import hashlib

    verifier, challenge = oidc.make_pkce_pair()
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    assert challenge == base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


# ── the resolver ───────────────────────────────────────────────────────────


async def test_resolver_absent_credential_strict_401(monkeypatch: pytest.MonkeyPatch):
    _set_oidc_env(monkeypatch)
    resolver = oidc.OidcSessionWebIdentityResolver()
    with pytest.raises(APIError) as excinfo:
        await resolver.resolve({})
    assert excinfo.value.status_code == 401
    assert excinfo.value.code == "AUTH_REQUIRED"
    # The console branches on this refusal to send someone to sign in, so the
    # envelope has to say the caller owns the next move. An unregistered code
    # would answer `unregistered`/`platform` and describe a broken deployment.
    envelope = excinfo.value.to_error_envelope()
    assert envelope["category"] == "auth"
    assert envelope["owner"] == "client"
    assert envelope["retryable"] is False


async def test_resolver_absent_credential_lenient_none(monkeypatch: pytest.MonkeyPatch):
    _set_oidc_env(monkeypatch)
    monkeypatch.setenv("ASTRABOX_OIDC_STRICT", "false")
    resolver = oidc.OidcSessionWebIdentityResolver()
    assert await resolver.resolve({}) is None


async def test_resolver_reads_cookie_and_oidc_bearer(
    monkeypatch: pytest.MonkeyPatch,
):
    _set_oidc_env(monkeypatch)

    async def _principal_from_access_token(
        config: oidc.OidcProviderConfig,
        access_token: str,
    ) -> IdentityPrincipal:
        assert config.issuer == "https://idp.example.com"
        assert access_token == "oidc-access-token"
        return IdentityPrincipal(
            user_id="u-8",
            display_name="Ari",
            roles=("admin",),
        )

    monkeypatch.setattr(
        oidc,
        "principal_from_access_token",
        _principal_from_access_token,
    )
    resolver = oidc.OidcSessionWebIdentityResolver()
    token = oidc.mint_session_token(
        user_id="u-7", email=None, display_name="Nia", roles=["admin"]
    )
    via_cookie = await resolver.resolve({"cookie": f"other=1; {oidc.SESSION_COOKIE}={token}"})
    assert via_cookie is not None and via_cookie.user_id == "u-7"
    assert "admin" in via_cookie.roles
    via_bearer = await resolver.resolve(
        {"authorization": "Bearer oidc-access-token"}
    )
    assert via_bearer is not None and via_bearer.user_id == "u-8"
    assert resolver.login_url("/litellm") == (
        "/api/v1/auth/login?next=%2Flitellm"
    )


async def test_resolver_invalid_session_is_hard_401_even_lenient(
    monkeypatch: pytest.MonkeyPatch,
):
    _set_oidc_env(monkeypatch)
    monkeypatch.setenv("ASTRABOX_OIDC_STRICT", "false")
    resolver = oidc.OidcSessionWebIdentityResolver()
    with pytest.raises(APIError):
        await resolver.resolve({"cookie": f"{oidc.SESSION_COOKIE}=not-a-jwt"})


async def test_api_client_configuration_does_not_scope_browser_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_oidc_env(monkeypatch)
    monkeypatch.setenv("ASTRABOX_OIDC_API_CLIENT_ID", "astrabox-api")
    monkeypatch.setenv("ASTRABOX_OIDC_API_CLIENT_SECRET", "api-secret")
    resolver = oidc.OidcSessionWebIdentityResolver()
    token = oidc.mint_session_token(
        user_id="browser-admin",
        email=None,
        display_name="Browser Admin",
        roles=["admin"],
    )

    context = await resolver.resolve({"cookie": f"{oidc.SESSION_COOKIE}={token}"})

    assert context is not None
    assert context.user_id == "browser-admin"
    assert context.roles == ["admin"]
    assert context.api_scopes is None


def test_resolver_fails_loud_unconfigured():
    with pytest.raises(RuntimeError):
        oidc.OidcSessionWebIdentityResolver()


# ── what a UserInfo answer means ───────────────────────────────────────────


def _userinfo_resolver(
    monkeypatch: pytest.MonkeyPatch, handler: Any
) -> oidc.OidcSessionWebIdentityResolver:
    """A resolver whose UserInfo hop is the only thing stubbed.

    Seeds the discovery cache and swaps the transport, so `handler` answers the
    real `principal_from_access_token` and the branch under test is the one
    that ships. Stubbing `principal_from_access_token` itself, as the resolver
    tests above do, would decide the outcome in the test instead of reading it.
    """
    _set_oidc_env(monkeypatch)
    oidc._discovery_cache["https://idp.example.com"] = {
        **_DISCOVERY,
        "userinfo_endpoint": "https://idp.example.com/userinfo",
    }
    transport = httpx.MockTransport(handler)

    def _mock_async_client(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        # `_REAL_ASYNC_CLIENT`, not `httpx.AsyncClient`: patching the attribute
        # patches the shared module, so reading it here inside a loop would
        # capture the previous iteration's mock and keep its transport.
        return _REAL_ASYNC_CLIENT(**kwargs)

    monkeypatch.setattr(identity_oidc.httpx, "AsyncClient", _mock_async_client)
    return oidc.OidcSessionWebIdentityResolver()


async def test_a_userinfo_identity_becomes_the_user(monkeypatch: pytest.MonkeyPatch):
    resolver = _userinfo_resolver(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"sub": "u-11", "email": "ari@example.com", "name": "Ari"}
        ),
    )

    context = await resolver.resolve({"authorization": "Bearer good-token"})

    assert context is not None
    assert context.user_id == "u-11"


async def test_a_rejected_token_asks_the_caller_for_a_new_one(
    monkeypatch: pytest.MonkeyPatch,
):
    resolver = _userinfo_resolver(
        monkeypatch, lambda request: httpx.Response(401, json={"error": "invalid_token"})
    )

    with pytest.raises(APIError) as caught:
        await resolver.resolve({"authorization": "Bearer stale-token"})

    envelope = caught.value.to_error_envelope()
    assert envelope["code"] == "AUTH_REQUIRED"
    assert envelope["status_code"] == 401
    assert envelope["owner"] == "client"


async def test_an_identity_without_a_subject_sends_the_reader_to_the_provider(
    monkeypatch: pytest.MonkeyPatch,
):
    """The third state, which the other two cannot express.

    UserInfo answered 200 with a document — the token is good and the provider
    is reachable — but the document carries no `sub`, which means the provider
    granted no `openid` scope or maps no subject claim. Answering `AUTH_REQUIRED`
    here sends an operator to reissue tokens and a user to sign in again, and
    neither ever succeeds. The message has to name the mapping, because the code
    alone does not say which claim is missing.
    """
    resolver = _userinfo_resolver(
        monkeypatch,
        lambda request: httpx.Response(200, json={"email": "ari@example.com", "name": "Ari"}),
    )

    with pytest.raises(APIError) as caught:
        await resolver.resolve({"authorization": "Bearer good-token"})

    envelope = caught.value.to_error_envelope()
    assert envelope["code"] == "IDENTITY_PROVIDER_MISCONFIGURED"
    assert envelope["status_code"] == 502
    assert envelope["owner"] == "platform"
    assert envelope["retryable"] is False
    # The row's only value the fallback does not also produce: an unregistered
    # code answers `platform`, `False` and the raise site's own status, so
    # `category` is the whole of what proves this code is registered.
    assert envelope["category"] == "auth"
    assert "sub" in envelope["user_message"] and "scope" in envelope["user_message"]


async def test_the_three_userinfo_answers_are_three_codes(
    monkeypatch: pytest.MonkeyPatch,
):
    """Read together, because the defect this replaces was two of them colliding.

    A missing subject and a rejected token reached the client as one code, so an
    operator could not tell "reissue the token" from "fix the claim mapping".
    """
    answers = {
        "accepted": httpx.Response(200, json={"sub": "u-11"}),
        "rejected": httpx.Response(401, json={"error": "invalid_token"}),
        "no-subject": httpx.Response(200, json={"email": "ari@example.com"}),
    }
    seen: dict[str, str] = {}
    for name, response in answers.items():
        resolver = _userinfo_resolver(monkeypatch, lambda request, r=response: r)
        try:
            context = await resolver.resolve({"authorization": "Bearer t"})
            seen[name] = f"OK:{context.user_id if context else None}"
        except APIError as exc:
            seen[name] = exc.code

    assert len(set(seen.values())) == 3, seen
    assert seen["accepted"] == "OK:u-11"
    assert seen["rejected"] != seen["no-subject"]


# ── what an RFC 7662 introspection answer means ───────────────────────────


def _introspection_resolver(
    monkeypatch: pytest.MonkeyPatch, handler: Any
) -> oidc.OidcSessionWebIdentityResolver:
    _set_oidc_env(monkeypatch)
    monkeypatch.setenv("ASTRABOX_OIDC_API_CLIENT_ID", "astrabox-api")
    monkeypatch.setenv("ASTRABOX_OIDC_API_CLIENT_SECRET", "api-secret")
    oidc._discovery_cache["https://idp.example.com"] = {
        **_DISCOVERY,
        "introspection_endpoint": "https://idp.example.com/oauth/introspect",
    }
    transport = httpx.MockTransport(handler)

    def _mock_async_client(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return _REAL_ASYNC_CLIENT(**kwargs)

    monkeypatch.setattr(identity_oidc.httpx, "AsyncClient", _mock_async_client)
    return oidc.OidcSessionWebIdentityResolver()


async def test_active_api_token_preserves_scopes_and_maps_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == "https://idp.example.com/oauth/introspect"
        assert request.headers["authorization"] == "Basic YXN0cmFib3gtYXBpOmFwaS1zZWNyZXQ="
        assert parse_qs(request.content.decode()) == {
            "token": ["short-lived-token"],
            "token_type_hint": ["access_token"],
        }
        return httpx.Response(
            200,
            json={
                "active": True,
                "sub": "admin/astrabox-api",
                "username": "astrabox-api",
                "aud": ["astrabox-api"],
                "scope": "astrabox:read astrabox:admin astrabox:read",
            },
        )

    resolver = _introspection_resolver(monkeypatch, answer)

    context = await resolver.resolve(
        {"authorization": "Bearer short-lived-token"}
    )

    assert context is not None
    assert context.user_id == "admin/astrabox-api"
    assert context.display_name == "astrabox-api"
    assert context.api_scopes == ["astrabox:read", "astrabox:admin"]
    assert context.roles == ["admin"]


async def test_inactive_api_token_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = _introspection_resolver(
        monkeypatch, lambda request: httpx.Response(200, json={"active": False})
    )

    with pytest.raises(APIError) as caught:
        await resolver.resolve({"authorization": "Bearer rejected-token"})

    assert caught.value.code == "AUTH_REQUIRED"
    assert caught.value.status_code == 401


async def test_client_id_is_the_stable_identity_when_introspection_omits_sub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = _introspection_resolver(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={
                "active": True,
                "client_id": "nightly-backup",
                "aud": ["nightly-backup"],
                "scope": "astrabox:read",
            },
        ),
    )

    context = await resolver.resolve({"authorization": "Bearer client-token"})

    assert context is not None
    assert context.user_id == "oauth-client:nightly-backup"
    assert context.display_name == "nightly-backup"
    assert context.api_scopes == ["astrabox:read"]


async def test_bad_introspection_client_is_a_deployment_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = _introspection_resolver(
        monkeypatch,
        lambda request: httpx.Response(401, json={"error": "invalid_client"}),
    )

    with pytest.raises(APIError) as caught:
        await resolver.resolve({"authorization": "Bearer otherwise-valid-token"})

    assert caught.value.code == "IDENTITY_PROVIDER_MISCONFIGURED"
    assert caught.value.status_code == 502
    assert "API client" in caught.value.message


async def test_api_client_requires_discovered_introspection_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_oidc_env(monkeypatch)
    monkeypatch.setenv("ASTRABOX_OIDC_API_CLIENT_ID", "astrabox-api")
    monkeypatch.setenv("ASTRABOX_OIDC_API_CLIENT_SECRET", "api-secret")
    oidc._discovery_cache["https://idp.example.com"] = dict(_DISCOVERY)
    resolver = oidc.OidcSessionWebIdentityResolver()

    with pytest.raises(APIError) as caught:
        await resolver.resolve({"authorization": "Bearer token"})

    assert caught.value.code == "IDENTITY_PROVIDER_MISCONFIGURED"
    assert caught.value.status_code == 502
    assert "introspection_endpoint" in caught.value.message


# ── routes ─────────────────────────────────────────────────────────────────


_DISCOVERY = {
    "authorization_endpoint": "https://idp.example.com/oauth/authorize",
    "token_endpoint": "https://idp.example.com/oauth/token",
    "jwks_uri": "https://idp.example.com/jwks",
}


def _client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    _set_oidc_env(monkeypatch)

    async def _fake_discover(config: Any) -> dict[str, Any]:
        return dict(_DISCOVERY)

    monkeypatch.setattr(auth_routes, "discover", _fake_discover)
    auth_routes._registered_on = None
    app = FastAPI()
    auth_routes.register_auth_routes(app)
    return TestClient(app, base_url="http://console.local")


def test_login_redirects_with_pkce_and_flow_cookie(monkeypatch: pytest.MonkeyPatch):
    client = _client(monkeypatch)
    response = client.get(
        "/api/v1/auth/login", params={"next": "/sessions"}, follow_redirects=False
    )
    assert response.status_code == 302
    location = urlparse(response.headers["location"])
    params = parse_qs(location.query)
    assert location.netloc == "idp.example.com"
    assert params["response_type"] == ["code"]
    assert params["client_id"] == ["astrabox-console"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["redirect_uri"] == ["http://console.local/api/v1/auth/callback"]
    flow_cookie = response.cookies.get(oidc.FLOW_COOKIE)
    assert flow_cookie
    flow = oidc.verify_flow_token(flow_cookie)
    assert flow["state"] == params["state"][0]
    assert flow["next"] == "/sessions"


def test_https_redirect_override_marks_the_flow_cookie_secure_behind_http_proxy(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "ASTRABOX_OIDC_REDIRECT_URL",
        "https://console.example.com/api/v1/auth/callback",
    )
    client = _client(monkeypatch)

    response = client.get("/api/v1/auth/login", follow_redirects=False)

    assert response.status_code == 302
    assert "; secure" in response.headers["set-cookie"].lower()
    params = parse_qs(urlparse(response.headers["location"]).query)
    assert params["redirect_uri"] == [
        "https://console.example.com/api/v1/auth/callback"
    ]


def test_login_rejects_offsite_next(monkeypatch: pytest.MonkeyPatch):
    client = _client(monkeypatch)
    response = client.get(
        "/api/v1/auth/login",
        params={"next": "https://evil.example.com/"},
        follow_redirects=False,
    )
    flow = oidc.verify_flow_token(response.cookies[oidc.FLOW_COOKIE])
    assert flow["next"] == "/"


def test_callback_rejects_state_mismatch(monkeypatch: pytest.MonkeyPatch):
    client = _client(monkeypatch)
    flow = oidc.mint_flow_token(state="expected", code_verifier="v", next_url="/")
    client.cookies.set(oidc.FLOW_COOKIE, flow)
    response = client.get(
        "/api/v1/auth/callback",
        params={"code": "c", "state": "different"},
        follow_redirects=False,
    )
    assert response.status_code == 401


def test_callback_missing_flow_cookie_400(monkeypatch: pytest.MonkeyPatch):
    client = _client(monkeypatch)
    response = client.get(
        "/api/v1/auth/callback", params={"code": "c", "state": "s"}, follow_redirects=False
    )
    assert response.status_code == 400


def test_callback_happy_path_sets_session_cookie(monkeypatch: pytest.MonkeyPatch):
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    id_token = jwt.encode(
        {
            "iss": "https://idp.example.com",
            "aud": "astrabox-console",
            "sub": "cas-user-1",
            "name": "Ada",
            "email": "ada@example.com",
            "groups": ["astrabox-admin"],
            "exp": int(time.time()) + 300,
        },
        private_key,
        algorithm="RS256",
    )

    class _StubSigningKey:
        key = private_key.public_key()

    class _StubJwkClient:
        def __init__(self, url: str):
            assert url == "https://idp.example.com/jwks"

        def get_signing_key_from_jwt(self, token: str) -> Any:
            return _StubSigningKey()

    def _token_endpoint(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        assert "grant_type=authorization_code" in body
        assert "code_verifier=" in body
        return httpx.Response(200, json={"id_token": id_token})

    transport = httpx.MockTransport(_token_endpoint)
    real_async_client = httpx.AsyncClient

    def _mock_async_client(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(**kwargs)

    client = _client(monkeypatch)
    monkeypatch.setattr(auth_routes, "PyJWKClient", _StubJwkClient)
    monkeypatch.setattr(auth_routes.httpx, "AsyncClient", _mock_async_client)

    flow = oidc.mint_flow_token(state="st-1", code_verifier="ver-1", next_url="/sessions")
    client.cookies.set(oidc.FLOW_COOKIE, flow)
    response = client.get(
        "/api/v1/auth/callback",
        params={"code": "the-code", "state": "st-1"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/sessions"
    session_cookie = response.cookies.get(oidc.SESSION_COOKIE)
    assert session_cookie
    claims = oidc.verify_session_token(session_cookie)
    assert claims["sub"] == "cas-user-1"
    assert claims["roles"] == ["admin"]

    # The probe endpoint reflects the session.
    client.cookies.set(oidc.SESSION_COOKIE, session_cookie)
    probe = client.get("/api/v1/auth/session")
    assert probe.json()["authenticated"] is True
    assert probe.json()["user"]["user_id"] == "cas-user-1"

    # Logout clears it.
    out = client.post("/api/v1/auth/logout")
    assert out.status_code == 204


# ── the serve guard ────────────────────────────────────────────────────────


def test_guard_refuses_public_bind_without_auth(monkeypatch: pytest.MonkeyPatch):
    from astrabox.cli.serve import _guard_unauthenticated_bind

    monkeypatch.delenv("ASTRABOX_WEB_IDENTITY", raising=False)
    monkeypatch.delenv("ASTRABOX_ALLOW_UNAUTHENTICATED_BIND", raising=False)
    monkeypatch.setattr("os.path.exists", lambda path: False)
    with pytest.raises(SystemExit):
        _guard_unauthenticated_bind("0.0.0.0")


def test_guard_allows_loopback_auth_override_and_container(
    monkeypatch: pytest.MonkeyPatch,
):
    from astrabox.cli.serve import _guard_unauthenticated_bind

    monkeypatch.delenv("ASTRABOX_WEB_IDENTITY", raising=False)
    monkeypatch.delenv("ASTRABOX_ALLOW_UNAUTHENTICATED_BIND", raising=False)
    _guard_unauthenticated_bind("127.0.0.1")  # loopback: fine

    monkeypatch.setenv("ASTRABOX_WEB_IDENTITY", "oidc")
    _guard_unauthenticated_bind("0.0.0.0")  # authenticated: fine
    monkeypatch.delenv("ASTRABOX_WEB_IDENTITY")

    monkeypatch.setenv("ASTRABOX_ALLOW_UNAUTHENTICATED_BIND", "1")
    _guard_unauthenticated_bind("0.0.0.0")  # explicit override: fine
    monkeypatch.delenv("ASTRABOX_ALLOW_UNAUTHENTICATED_BIND")

    monkeypatch.setattr("os.path.exists", lambda path: path == "/.dockerenv")
    _guard_unauthenticated_bind("0.0.0.0")  # in-container: warns, not refuses
