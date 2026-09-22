"""Config-only SSO web-identity resolvers + the middleware's SSO extensions.

Covers the two resolvers in :mod:`astrabox.providers.identity_sso`
(TrustedHeader forwarded-header trust; VerifiedJwt real signature check) and the
:class:`~astrabox.web.identity_middleware.WebIdentityMiddleware` exempt-path +
admin-role gate. Config is read at ``__init__``, so every test sets env via
``monkeypatch.setenv`` and constructs the resolver FRESH.

Conventions (ASGI drive helpers, recording app) mirror
``tests/web_identity_middleware_test.py``.
"""

from __future__ import annotations

import time
from typing import Any

import jwt
import pytest

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import (
    UserContext,
    default_org_id,
    get_current_user_context,
)
from astrabox.providers.identity_sso import (
    TrustedHeaderWebIdentityResolver,
    VerifiedJwtWebIdentityResolver,
)
from astrabox.web.identity_middleware import WebIdentityMiddleware

# >= 32 bytes: silence PyJWT's InsecureKeyLengthWarning for HS256 in tests.
_JWT_SECRET = "unit-test-shared-secret-0123456789abcdef"
_OTHER_SECRET = "a-different-secret-0123456789abcdef-xyz"

# Every env knob the resolvers/middleware read — cleared before each test so a
# value set by the shell or a prior test can never leak into config-at-__init__.
_IDENTITY_ENV_VARS = (
    "ASTRABOX_TRUSTED_HEADER_USER",
    "ASTRABOX_TRUSTED_HEADER_EMAIL",
    "ASTRABOX_TRUSTED_HEADER_NAME",
    "ASTRABOX_TRUSTED_HEADER_GROUPS",
    "ASTRABOX_TRUSTED_HEADER_ORG",
    "ASTRABOX_TRUSTED_HEADER_GATEWAY_SECRET",
    "ASTRABOX_TRUSTED_HEADER_GATEWAY_SECRET_HEADER",
    "ASTRABOX_TRUSTED_HEADER_STRICT",
    "ASTRABOX_JWT_JWKS_URL",
    "ASTRABOX_JWT_ISSUER",
    "ASTRABOX_JWT_SECRET",
    "ASTRABOX_JWT_ALGORITHMS",
    "ASTRABOX_JWT_AUDIENCE",
    "ASTRABOX_JWT_USER_CLAIM",
    "ASTRABOX_JWT_EMAIL_CLAIM",
    "ASTRABOX_JWT_NAME_CLAIM",
    "ASTRABOX_JWT_GROUPS_CLAIM",
    "ASTRABOX_JWT_ORG_CLAIM",
    "ASTRABOX_JWT_STRICT",
    "ASTRABOX_ADMIN_GROUP",
    "ASTRABOX_AUTH_EXEMPT_PREFIXES",
)


@pytest.fixture(autouse=True)
def _clean_identity_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _IDENTITY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# ── ASGI drive helpers (mirror web_identity_middleware_test.py) ─────────────
class _RecordingApp:
    def __init__(self) -> None:
        self.seen_user_id: str | None = None
        self.seen_roles: list[str] | None = None
        self.called = False

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        self.called = True
        user = await get_current_user_context()
        self.seen_user_id = user.user_id
        self.seen_roles = list(user.roles)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


class _StaticResolver:
    def __init__(self, user: UserContext | None) -> None:
        self._user = user

    async def resolve(self, headers: Any) -> UserContext | None:
        return self._user


class _RejectingResolver:
    async def resolve(self, headers: Any) -> UserContext | None:
        raise APIError(code="UNAUTHORIZED", message="no credential", status_code=401)


def _scope(
    path: str, *, method: str = "GET", type_: str = "http"
) -> dict[str, Any]:
    return {"type": type_, "method": method, "path": path, "headers": []}


async def _drive(middleware: WebIdentityMiddleware, scope: dict[str, Any]) -> list[dict]:
    sent: list[dict] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await middleware(scope, receive, send)
    return sent


def _status(sent: list[dict]) -> int:
    start = next(m for m in sent if m["type"] == "http.response.start")
    return int(start["status"])


def _hs256(payload: dict[str, Any], *, secret: str = _JWT_SECRET) -> str:
    """Sign a token, giving it a live ``exp`` unless the caller decides otherwise.

    The resolver requires ``exp``, so a fixture that left it out would exercise
    that requirement instead of whatever it meant to test — every audience,
    groups, and subject case below would pass or fail for the wrong reason.
    Pass ``exp=None`` to mint one without the claim.
    """
    claims = dict(payload)
    if "exp" not in claims:
        claims["exp"] = int(time.time()) + 3600
    elif claims["exp"] is None:
        del claims["exp"]
    return jwt.encode(claims, secret, algorithm="HS256")


def _bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


# ── TrustedHeaderWebIdentityResolver ───────────────────────────────────────
async def test_trusted_header_maps_all_fields_groups_admin_and_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_TRUSTED_HEADER_ORG", "x-forwarded-org")
    resolver = TrustedHeaderWebIdentityResolver()
    user = await resolver.resolve(
        {
            "x-forwarded-user": "alice@example.com",
            "x-forwarded-email": "alice@example.com",
            "x-forwarded-preferred-username": "Alice",
            "x-forwarded-groups": "dev, ops  astrabox-admin",
            "x-forwarded-org": "acme",
        }
    )
    assert user is not None
    assert user.user_id == "alice@example.com"
    assert user.email == "alice@example.com"
    assert user.display_name == "Alice"
    assert user.org_id == "acme"
    # Groups verbatim (comma+whitespace split) + the admin-group -> "admin" rule.
    assert user.roles == ["dev", "ops", "astrabox-admin", "admin"]


async def test_trusted_header_org_defaults_when_unset() -> None:
    resolver = TrustedHeaderWebIdentityResolver()
    user = await resolver.resolve({"x-forwarded-user": "bob"})
    assert user is not None
    assert user.org_id == default_org_id()
    assert user.roles == []


async def test_trusted_header_gateway_secret_match(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRABOX_TRUSTED_HEADER_GATEWAY_SECRET", "hop-proof")
    resolver = TrustedHeaderWebIdentityResolver()
    user = await resolver.resolve(
        {"x-forwarded-user": "carol", "x-astrabox-gateway-secret": "hop-proof"}
    )
    assert user is not None
    assert user.user_id == "carol"


async def test_trusted_header_gateway_secret_mismatch_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_TRUSTED_HEADER_GATEWAY_SECRET", "hop-proof")
    resolver = TrustedHeaderWebIdentityResolver()
    with pytest.raises(APIError) as excinfo:
        await resolver.resolve(
            {"x-forwarded-user": "carol", "x-astrabox-gateway-secret": "WRONG"}
        )
    assert excinfo.value.status_code == 401


async def test_trusted_header_gateway_secret_missing_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_TRUSTED_HEADER_GATEWAY_SECRET", "hop-proof")
    resolver = TrustedHeaderWebIdentityResolver()
    with pytest.raises(APIError) as excinfo:
        await resolver.resolve({"x-forwarded-user": "carol"})  # no secret header at all
    assert excinfo.value.status_code == 401


async def test_trusted_header_strict_missing_user_raises_401() -> None:
    resolver = TrustedHeaderWebIdentityResolver()  # strict is the default
    with pytest.raises(APIError) as excinfo:
        await resolver.resolve({"x-forwarded-email": "nobody@example.com"})
    assert excinfo.value.code == "UNAUTHORIZED"
    assert excinfo.value.status_code == 401


async def test_trusted_header_lenient_missing_user_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_TRUSTED_HEADER_STRICT", "0")
    resolver = TrustedHeaderWebIdentityResolver()
    assert await resolver.resolve({}) is None


# ── VerifiedJwtWebIdentityResolver ─────────────────────────────────────────
async def test_jwt_hs256_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRABOX_JWT_SECRET", _JWT_SECRET)
    resolver = VerifiedJwtWebIdentityResolver()
    token = _hs256({"sub": "dave", "email": "dave@example.com", "name": "Dave"})
    user = await resolver.resolve(_bearer(token))
    assert user is not None
    assert user.user_id == "dave"
    assert user.email == "dave@example.com"
    assert user.display_name == "Dave"


async def test_jwt_wrong_secret_401(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRABOX_JWT_SECRET", _JWT_SECRET)
    resolver = VerifiedJwtWebIdentityResolver()
    token = _hs256({"sub": "dave"}, secret=_OTHER_SECRET)
    with pytest.raises(APIError) as excinfo:
        await resolver.resolve(_bearer(token))
    assert excinfo.value.status_code == 401


async def test_jwt_expired_401(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRABOX_JWT_SECRET", _JWT_SECRET)
    resolver = VerifiedJwtWebIdentityResolver()
    token = _hs256({"sub": "dave", "exp": int(time.time()) - 3600})
    with pytest.raises(APIError) as excinfo:
        await resolver.resolve(_bearer(token))
    assert excinfo.value.status_code == 401


async def test_jwt_audience_mismatch_401(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRABOX_JWT_SECRET", _JWT_SECRET)
    monkeypatch.setenv("ASTRABOX_JWT_AUDIENCE", "astrabox")
    resolver = VerifiedJwtWebIdentityResolver()
    token = _hs256({"sub": "dave", "aud": "some-other-app"})
    with pytest.raises(APIError) as excinfo:
        await resolver.resolve(_bearer(token))
    assert excinfo.value.status_code == 401


async def test_jwt_audience_match_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRABOX_JWT_SECRET", _JWT_SECRET)
    monkeypatch.setenv("ASTRABOX_JWT_AUDIENCE", "astrabox")
    resolver = VerifiedJwtWebIdentityResolver()
    token = _hs256({"sub": "dave", "aud": "astrabox"})
    user = await resolver.resolve(_bearer(token))
    assert user is not None and user.user_id == "dave"


async def test_jwt_groups_claim_as_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRABOX_JWT_SECRET", _JWT_SECRET)
    resolver = VerifiedJwtWebIdentityResolver()
    token = _hs256({"sub": "erin", "groups": ["dev", "astrabox-admin"]})
    user = await resolver.resolve(_bearer(token))
    assert user is not None
    assert user.roles == ["dev", "astrabox-admin", "admin"]


async def test_jwt_groups_claim_as_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRABOX_JWT_SECRET", _JWT_SECRET)
    resolver = VerifiedJwtWebIdentityResolver()
    token = _hs256({"sub": "erin", "groups": "dev, astrabox-admin"})
    user = await resolver.resolve(_bearer(token))
    assert user is not None
    assert user.roles == ["dev", "astrabox-admin", "admin"]


async def test_jwt_x_astrabox_token_header_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRABOX_JWT_SECRET", _JWT_SECRET)
    resolver = VerifiedJwtWebIdentityResolver()
    token = _hs256({"sub": "frank"})
    user = await resolver.resolve({"x-astrabox-token": token})
    assert user is not None and user.user_id == "frank"


async def test_jwt_missing_user_claim_401(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRABOX_JWT_SECRET", _JWT_SECRET)
    resolver = VerifiedJwtWebIdentityResolver()
    token = _hs256({"email": "no-sub@example.com"})  # validly signed, but no subject
    with pytest.raises(APIError) as excinfo:
        await resolver.resolve(_bearer(token))
    assert excinfo.value.status_code == 401


async def test_jwt_strict_without_token_401(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRABOX_JWT_SECRET", _JWT_SECRET)
    resolver = VerifiedJwtWebIdentityResolver()  # strict default
    with pytest.raises(APIError) as excinfo:
        await resolver.resolve({})
    assert excinfo.value.status_code == 401


async def test_jwt_lenient_without_token_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRABOX_JWT_SECRET", _JWT_SECRET)
    monkeypatch.setenv("ASTRABOX_JWT_STRICT", "0")
    resolver = VerifiedJwtWebIdentityResolver()
    assert await resolver.resolve({}) is None


async def test_jwt_lenient_present_but_invalid_still_401(monkeypatch: pytest.MonkeyPatch) -> None:
    # Lenient governs only the ABSENT-credential case; a present bad token is 401.
    monkeypatch.setenv("ASTRABOX_JWT_SECRET", _JWT_SECRET)
    monkeypatch.setenv("ASTRABOX_JWT_STRICT", "0")
    resolver = VerifiedJwtWebIdentityResolver()
    token = _hs256({"sub": "dave"}, secret=_OTHER_SECRET)
    with pytest.raises(APIError) as excinfo:
        await resolver.resolve(_bearer(token))
    assert excinfo.value.status_code == 401


async def test_jwt_refusals_separate_expiry_from_everything_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One refusal a client can act on; the rest deliberately share a code.

    Expiry has its own recovery — get a new token and reconnect — so a client
    has to be able to tell it apart. A wrong signature and a string that is not
    a token do not: both mean stop and fix the credential, and splitting them
    would only tell a caller probing the endpoint which one it holds. So the
    assertion is two-sided, and the second half is as load-bearing as the first.
    """

    monkeypatch.setenv("ASTRABOX_JWT_SECRET", _JWT_SECRET)
    resolver = VerifiedJwtWebIdentityResolver()

    async def refusal(token: str) -> APIError:
        with pytest.raises(APIError) as excinfo:
            await resolver.resolve(_bearer(token))
        return excinfo.value

    expired = await refusal(_hs256({"sub": "dave", "exp": int(time.time()) - 3600}))
    wrong_key = await refusal(_hs256({"sub": "dave"}, secret=_OTHER_SECRET))
    garbage = await refusal("not.a.jwt")

    assert expired.code == "TOKEN_EXPIRED"
    assert "obtain a new one from your token issuer" in expired.message
    assert expired.status_code == 401

    # Distinguishable from the rest…
    assert expired.code not in {wrong_key.code, garbage.code}
    # …and the rest still indistinguishable from each other.
    assert wrong_key.code == garbage.code == "UNAUTHORIZED"
    assert wrong_key.message == garbage.message
    assert wrong_key.status_code == garbage.status_code == 401


async def test_jwt_without_an_exp_claim_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token that never expires is a permanent credential.

    Verifying ``exp`` only when it happens to be present makes expiry the
    issuer's option rather than the deployment's rule: whoever mints tokens can
    opt out of it by omitting one claim, and nothing on this side would say so.
    In the shared-secret mode there is no issuer session to revoke either, so a
    leaked unbounded token is good until the secret is rotated.

    The message names the claim because that is fixed where the token is
    minted, not by the caller presenting it.
    """

    monkeypatch.setenv("ASTRABOX_JWT_SECRET", _JWT_SECRET)
    resolver = VerifiedJwtWebIdentityResolver()
    token = _hs256({"sub": "dave", "exp": None})

    with pytest.raises(APIError) as excinfo:
        await resolver.resolve(_bearer(token))

    assert excinfo.value.status_code == 401
    assert "exp" in excinfo.value.message
    # Not its own code: the caller stops either way, exactly as for a bad key.
    assert excinfo.value.code == "UNAUTHORIZED"


def test_jwt_no_key_material_fails_at_construction() -> None:
    # Loader runs at create_app; a JWT resolver with no key can never verify.
    with pytest.raises(RuntimeError):
        VerifiedJwtWebIdentityResolver()


# ── Middleware: exempt paths + admin gate ──────────────────────────────────
async def test_middleware_strict_resolver_healthz_passes() -> None:
    app = _RecordingApp()
    mw = WebIdentityMiddleware(app, _RejectingResolver())
    sent = await _drive(mw, _scope("/healthz"))
    assert app.called  # exempt: proceeds unauthenticated despite the rejection
    assert _status(sent) == 200


async def test_middleware_api_sessions_without_identity_401() -> None:
    app = _RecordingApp()
    mw = WebIdentityMiddleware(app, _RejectingResolver())
    sent = await _drive(mw, _scope("/api/v1/sessions"))
    assert not app.called
    assert _status(sent) == 401


async def test_middleware_share_prefix_passes() -> None:
    app = _RecordingApp()
    mw = WebIdentityMiddleware(app, _RejectingResolver())
    sent = await _drive(mw, _scope("/api/v1/share/abc123"))
    assert app.called  # capability-token surface: its own auth, exempt
    assert _status(sent) == 200


async def test_middleware_admin_path_non_admin_403() -> None:
    app = _RecordingApp()
    mw = WebIdentityMiddleware(app, _StaticResolver(UserContext(user_id="u", roles=["dev"])))
    sent = await _drive(mw, _scope("/api/v1/admin/users"))
    assert not app.called
    assert _status(sent) == 403


async def test_middleware_admin_path_admin_role_passes() -> None:
    app = _RecordingApp()
    mw = WebIdentityMiddleware(app, _StaticResolver(UserContext(user_id="u", roles=["admin"])))
    sent = await _drive(mw, _scope("/api/v1/admin/users"))
    assert app.called
    assert _status(sent) == 200
    assert app.seen_user_id == "u"


@pytest.mark.parametrize(
    ("method", "scopes", "expected_status"),
    [
        ("GET", ["astrabox:read"], 200),
        ("GET", ["astrabox:write"], 403),
        ("POST", ["astrabox:write"], 200),
        ("POST", ["astrabox:read"], 403),
    ],
)
async def test_middleware_enforces_scopes_only_on_machine_tokens(
    method: str, scopes: list[str], expected_status: int
) -> None:
    app = _RecordingApp()
    user = UserContext(user_id="api-client", api_scopes=scopes)
    sent = await _drive(
        WebIdentityMiddleware(app, _StaticResolver(user)),
        _scope("/api/v1/sessions", method=method),
    )
    assert app.called is (expected_status == 200)
    assert _status(sent) == expected_status


async def test_browser_identity_is_not_subject_to_machine_token_scopes() -> None:
    app = _RecordingApp()
    user = UserContext(user_id="browser-user")
    sent = await _drive(
        WebIdentityMiddleware(app, _StaticResolver(user)),
        _scope("/api/v1/sessions", method="POST"),
    )
    assert app.called
    assert _status(sent) == 200


async def test_admin_api_requires_admin_scope_even_with_admin_role() -> None:
    import json

    app = _RecordingApp()
    user = UserContext(
        user_id="api-client",
        roles=["admin"],
        api_scopes=["astrabox:read", "astrabox:write"],
    )
    sent = await _drive(
        WebIdentityMiddleware(app, _StaticResolver(user)),
        _scope("/api/v1/admin/templates"),
    )
    assert not app.called
    assert _status(sent) == 403
    body = json.loads(
        next(message for message in sent if message["type"] == "http.response.body")[
            "body"
        ]
    )
    assert body["code"] == "API_TOKEN_SCOPE_INSUFFICIENT"
    assert body["data"] == {"required_scope": "astrabox:admin"}


async def test_scoped_token_does_not_override_self_authorizing_route() -> None:
    app = _RecordingApp()
    user = UserContext(user_id="api-client", api_scopes=[])
    sent = await _drive(
        WebIdentityMiddleware(app, _StaticResolver(user)),
        _scope("/api/v1/sbxcap/capability/transcript", method="POST"),
    )
    assert app.called
    assert _status(sent) == 200


async def test_websocket_requires_write_scope() -> None:
    app = _RecordingApp()
    user = UserContext(user_id="api-client", api_scopes=["astrabox:read"])
    sent = await _drive(
        WebIdentityMiddleware(app, _StaticResolver(user)),
        _scope("/api/v1/terminal/ws", type_="websocket"),
    )
    assert not app.called
    assert sent == [{"type": "websocket.close", "code": 1008}]


async def test_middleware_admin_path_no_identity_passes() -> None:
    # The no-auth DEFAULT resolver (marked is_no_auth_default) keeps the
    # single-user admin console open.
    app = _RecordingApp()
    resolver = _StaticResolver(None)
    resolver.is_no_auth_default = True  # type: ignore[attr-defined]
    mw = WebIdentityMiddleware(app, resolver)
    sent = await _drive(mw, _scope("/api/v1/admin/users"))
    assert app.called
    assert _status(sent) == 200


async def test_middleware_admin_path_anonymous_strict_resolver_is_rejected() -> None:
    # A REAL resolver that yields no identity must not reopen the admin
    # console: anonymous never passes /api/v1/admin* once auth is configured.
    app = _RecordingApp()
    mw = WebIdentityMiddleware(app, _StaticResolver(None))
    sent = await _drive(mw, _scope("/api/v1/admin/users"))
    assert not app.called
    assert _status(sent) == 401


async def test_middleware_admin_api_dashed_prefix_gated() -> None:
    # /api/v1/admin (no trailing slash) also covers /api/v1/admin-api/*.
    app = _RecordingApp()
    mw = WebIdentityMiddleware(app, _StaticResolver(UserContext(user_id="u", roles=["dev"])))
    sent = await _drive(mw, _scope("/api/v1/admin-api/flags"))
    assert not app.called
    assert _status(sent) == 403


async def test_middleware_exempt_prefix_override_replaces_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Override REPLACES the default prefixes: with the override set, /api/v1/share/ is not exempt...
    monkeypatch.setenv("ASTRABOX_AUTH_EXEMPT_PREFIXES", "/api/v1/custom/")
    app = _RecordingApp()
    mw = WebIdentityMiddleware(app, _RejectingResolver())
    sent = await _drive(mw, _scope("/api/v1/share/abc"))
    assert not app.called
    assert _status(sent) == 401

    # ...but the newly-named prefix is, and /healthz always holds.
    app2 = _RecordingApp()
    mw2 = WebIdentityMiddleware(app2, _RejectingResolver())
    assert _status(await _drive(mw2, _scope("/api/v1/custom/thing"))) == 200
    app3 = _RecordingApp()
    mw3 = WebIdentityMiddleware(app3, _RejectingResolver())
    assert _status(await _drive(mw3, _scope("/healthz"))) == 200


# ── the reject response is the standard envelope, data included ────────────


class _RedirectingResolver:
    async def resolve(self, headers: Any) -> UserContext | None:
        raise APIError(
            code="UNAUTHORIZED",
            message="session cookie expired",
            status_code=401,
            data={"login_url": "https://idp.example/login?next=%2F"},
        )


async def test_middleware_reject_emits_the_platform_envelope_with_data() -> None:
    """A cookie/SSO resolver's login redirect payload must survive the wire:
    standard envelope (top-level code/message/data + error), APIError.data
    passed through — a frontend keying on code + data.login_url can bounce
    to login instead of a dead error screen."""
    import json

    app = _RecordingApp()
    mw = WebIdentityMiddleware(app, _RedirectingResolver())
    sent = await _drive(mw, _scope("/api/v1/sessions"))
    assert not app.called
    assert _status(sent) == 401
    body = json.loads(
        next(m for m in sent if m["type"] == "http.response.body")["body"]
    )
    assert body["code"] == "UNAUTHORIZED"
    assert body["message"] == "session cookie expired"
    assert body["data"] == {"login_url": "https://idp.example/login?next=%2F"}
    assert body["error"]["code"] == "UNAUTHORIZED", "route-error envelope parity"


# ── root_path deployments must not silently disable API auth ────────────────


def _mounted_scope(route_path: str, *, root_path: str = "/astrabox") -> dict[str, Any]:
    # ASGI spec: scope["path"] INCLUDES root_path under a sub-mount /
    # `uvicorn --root-path` / prefixing reverse proxy.
    return {
        "type": "http",
        "method": "GET",
        "path": f"{root_path}{route_path}",
        "root_path": root_path,
        "headers": [],
    }


async def test_mounted_api_path_still_rejects_with_a_strict_resolver() -> None:
    """The root_path failure mode: raw-path classification made every mounted API
    path look like a public SPA asset, downgrading a strict resolver's 401 to
    anonymous — auth silently OFF. Classification now follows the route path."""
    app = _RecordingApp()
    mw = WebIdentityMiddleware(app, _RejectingResolver())
    sent = await _drive(mw, _mounted_scope("/api/v1/sessions"))
    assert not app.called, "a mounted API route must never fall through anonymous"
    assert _status(sent) == 401


async def test_mounted_exempt_path_still_passes() -> None:
    app = _RecordingApp()
    mw = WebIdentityMiddleware(app, _RejectingResolver())
    sent = await _drive(mw, _mounted_scope("/healthz"))
    assert app.called
    assert _status(sent) == 200


async def test_mounted_admin_surface_still_hard_gates() -> None:
    # A REAL resolver yielding no identity: the mounted admin surface must
    # hard-gate exactly like the unmounted one.
    app = _RecordingApp()
    mw = WebIdentityMiddleware(app, _StaticResolver(None))
    sent = await _drive(mw, _mounted_scope("/api/v1/admin/templates"))
    assert not app.called
    assert _status(sent) == 401
