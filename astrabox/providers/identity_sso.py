"""Config-only SSO web-identity resolvers (``astrabox.web.identity``).

Two production, auth-backed resolvers for the web/console HTTP+WS surface, each
selected **purely by configuration** (an ``ASTRABOX_WEB_IDENTITY`` entry-point
name + env vars) with no code change to the ~50 handler call sites. They satisfy
:class:`~astrabox.seams.identity.WebIdentityResolver` structurally; the ASGI
:class:`~astrabox.web.identity_middleware.WebIdentityMiddleware` runs the
configured resolver once per request and binds its :class:`UserContext`.

* :class:`TrustedHeaderWebIdentityResolver` — the standard self-hosted SSO
  pattern: an authenticating reverse proxy (oauth2-proxy / Authelia / Authentik
  / Cloudflare Access) terminates login in front of AstraBox and injects the
  signed-in user into request headers, which AstraBox then trusts. Forwarded
  headers are client-forgeable unless the proxy is the *only* ingress, so an
  optional shared gateway secret proves the request actually transited the proxy.

* :class:`VerifiedJwtWebIdentityResolver` — real bearer-token verification: the
  JWT signature is checked against JWKS (a configured URL, or one discovered via
  OIDC) or an HS256 shared secret. This is the hard-auth contrast to the
  *unverified* convenience decode in :mod:`astrabox.providers.identity`, which
  never checks a signature.

Shared invariants with the rest of the identity seam:

* **Fail loud on misconfiguration.** :class:`VerifiedJwtWebIdentityResolver`
  raises at construction when no key material is configured — the loader runs at
  ``create_app``, so a broken auth config breaks boot, not individual requests.
* **Never silently degrade a *present* credential.** A supplied-but-invalid
  token / gateway secret is always a hard reject; the strict/lenient switch only
  governs the *absent*-credential case (strict → 401; lenient → ``None`` = fall
  through to the anonymous/local no-auth default).
* **Config is read once, at ``__init__``.** Header names, claim names, key
  material and the strict flag are captured when the resolver is constructed (at
  ``create_app``); per-request code only reads request data.

Imports only core dependencies (``pyjwt``, ``httpx``) — no new deps.
"""

from __future__ import annotations

import asyncio
import hmac
import os
from typing import Any, Mapping, Sequence

import httpx
import jwt
from jwt import PyJWKClient

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import PLATFORM_ADMIN_ROLE, UserContext

logger = get_logger(__name__)

__all__ = [
    "TrustedHeaderWebIdentityResolver",
    "VerifiedJwtWebIdentityResolver",
]

# ── TrustedHeader defaults (oauth2-proxy's forwarded-header names) ──────────
_DEFAULT_USER_HEADER = "x-forwarded-user"
_DEFAULT_EMAIL_HEADER = "x-forwarded-email"
_DEFAULT_NAME_HEADER = "x-forwarded-preferred-username"
_DEFAULT_GROUPS_HEADER = "x-forwarded-groups"
_DEFAULT_GATEWAY_SECRET_HEADER = "x-astrabox-gateway-secret"

# ── shared ─────────────────────────────────────────────────────────────────
#: Group whose membership grants the ``admin`` role (both resolvers honour it).
_DEFAULT_ADMIN_GROUP = "astrabox-admin"

# ── VerifiedJwt claim defaults ─────────────────────────────────────────────
_DEFAULT_USER_CLAIM = "sub"
_DEFAULT_EMAIL_CLAIM = "email"
_DEFAULT_NAME_CLAIM = "name"
_DEFAULT_GROUPS_CLAIM = "groups"

#: Default signature algorithms per key-material path. Asymmetric for JWKS/OIDC
#: (the IdP signs, the verifier holds only the public key); HMAC for a shared secret.
_DEFAULT_JWKS_ALGORITHMS = ("RS256", "ES256")
_DEFAULT_SECRET_ALGORITHMS = ("HS256",)

#: Only an explicit falsey value flips a default-ON strict flag to lenient.
_FALSEY = {"0", "false", "no", "off"}


def _env(name: str, default: str = "") -> str:
    """Trimmed env value (empty string when unset) — config read helper."""
    return str(os.getenv(name, default) or "").strip()


def _strict_flag(name: str) -> bool:
    """A boolean env flag defaulting to ON; only an explicit falsey value disables it.

    Strict is the safe default for an auth-backed resolver: an absent credential
    rejects rather than silently proceeding anonymous. Set ``0``/``false`` to opt
    into lenient fall-through.
    """
    return str(os.getenv(name, "")).strip().lower() not in _FALSEY


def _bearer_token(headers: Mapping[str, str]) -> str:
    """Bearer token from ``x-astrabox-token`` then ``Authorization: Bearer``.

    The custom header remains available for trusted gateways; standard clients
    use the Authorization header.
    """
    header_token = str(headers.get("x-astrabox-token", "") or "").strip()
    if header_token:
        return header_token
    auth_header = str(headers.get("authorization", "") or "").strip()
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return ""


def _coerce_groups(value: Any) -> list[str]:
    """Groups from a header/claim value: a list/tuple, or a comma/whitespace string.

    A JWT ``groups`` claim is commonly a JSON array; a forwarded header is a
    single string. Both normalise to a trimmed, empty-free list of group names.
    """
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    # Split on commas first, then any whitespace; drops empties.
    return text.replace(",", " ").split()


def _roles_from_groups(groups: Sequence[str], admin_group: str) -> list[str]:
    """Groups become roles verbatim; the configured admin group adds ``admin``."""
    roles = [str(group).strip() for group in groups if str(group).strip()]
    if admin_group and admin_group in roles and PLATFORM_ADMIN_ROLE not in roles:
        roles.append(PLATFORM_ADMIN_ROLE)
    return roles


def _sso_user_context(
    *,
    user_id: str,
    email: Any = "",
    display_name: Any = "",
    groups: Sequence[str] = (),
    org: Any = "",
    admin_group: str,
) -> UserContext:
    """Build the resolved :class:`UserContext` (caller guarantees a non-empty id).

    ``org`` of ``""`` lets :class:`UserContext` apply ``default_org_id()``; the
    ``groups`` → ``roles`` mapping (incl. the admin-group rule) is centralised so
    both resolvers agree.
    """
    return UserContext(
        user_id=user_id,
        display_name=str(display_name or "").strip() or None,
        email=str(email or "").strip() or None,
        org_id=str(org or "").strip() or None,
        roles=_roles_from_groups(groups, admin_group),
    )


class TrustedHeaderWebIdentityResolver:
    """Trust an authenticating gateway's forwarded identity headers.

    The self-hosted SSO norm: oauth2-proxy / Authelia / Authentik / Cloudflare
    Access terminates the login and forwards the signed-in user as request
    headers; AstraBox trusts them. Structurally satisfies
    :class:`~astrabox.seams.identity.WebIdentityResolver`.

    **Threat model / the gateway secret.** Forwarded headers are trivially
    forgeable by anything that can reach the app socket directly (bypassing the
    proxy). When ``ASTRABOX_TRUSTED_HEADER_GATEWAY_SECRET`` is configured, every
    request MUST carry it (in ``ASTRABOX_TRUSTED_HEADER_GATEWAY_SECRET_HEADER``,
    compared with :func:`hmac.compare_digest`); a mismatched/absent secret is an
    unconditional 401 — this is a transport-integrity gate (proves the request
    came through the proxy), enforced independently of strict/lenient.

    **Strictness.** Default strict: an absent user header is a 401. Lenient
    (``ASTRABOX_TRUSTED_HEADER_STRICT=0``) returns ``None`` — no identity
    asserted — so the request falls through to the anonymous/local default.

    **Roles.** The groups header (comma/whitespace-delimited) becomes
    ``roles`` verbatim; membership in ``ASTRABOX_ADMIN_GROUP`` additionally adds
    the ``admin`` role (what the admin-surface gate checks).

    Header *names* are configurable and lower-cased at construction to match the
    middleware's lower-cased header map.
    """

    def __init__(self) -> None:
        self._user_header = (_env("ASTRABOX_TRUSTED_HEADER_USER") or _DEFAULT_USER_HEADER).lower()
        self._email_header = (
            _env("ASTRABOX_TRUSTED_HEADER_EMAIL") or _DEFAULT_EMAIL_HEADER
        ).lower()
        self._name_header = (_env("ASTRABOX_TRUSTED_HEADER_NAME") or _DEFAULT_NAME_HEADER).lower()
        self._groups_header = (
            _env("ASTRABOX_TRUSTED_HEADER_GROUPS") or _DEFAULT_GROUPS_HEADER
        ).lower()
        # "" = unset -> org falls back to the deployment default in UserContext.
        self._org_header = _env("ASTRABOX_TRUSTED_HEADER_ORG").lower()
        self._admin_group = _env("ASTRABOX_ADMIN_GROUP") or _DEFAULT_ADMIN_GROUP
        # "" = disabled: no transport-integrity check.
        self._gateway_secret = _env("ASTRABOX_TRUSTED_HEADER_GATEWAY_SECRET")
        self._gateway_secret_header = (
            _env("ASTRABOX_TRUSTED_HEADER_GATEWAY_SECRET_HEADER") or _DEFAULT_GATEWAY_SECRET_HEADER
        ).lower()
        self._strict = _strict_flag("ASTRABOX_TRUSTED_HEADER_STRICT")

    @staticmethod
    def login_url(next_url: str = "/") -> None:
        """The authenticating ingress owns browser login and redirection."""

        _ = next_url
        return None

    def _verify_gateway_secret(self, headers: Mapping[str, str]) -> None:
        """Enforce the shared gateway secret when configured (unconditional 401).

        Independent of strict/lenient: a configured secret means "all traffic
        transits the proxy", so a request without the matching secret could not
        have come through it and is rejected — the forgeable identity headers are
        only trustworthy behind this proof.
        """
        if not self._gateway_secret:
            return
        presented = str(headers.get(self._gateway_secret_header, "") or "")
        if not hmac.compare_digest(presented, self._gateway_secret):
            raise APIError(
                code="UNAUTHORIZED",
                message="trusted-header gateway secret missing or mismatched",
                status_code=401,
            )

    async def resolve(self, headers: Mapping[str, str]) -> UserContext | None:
        self._verify_gateway_secret(headers)

        user_id = str(headers.get(self._user_header, "") or "").strip()
        if not user_id:
            if self._strict:
                raise APIError(
                    code="UNAUTHORIZED",
                    message="trusted identity header missing",
                    status_code=401,
                )
            # Lenient: no identity asserted -> anonymous/local fall-through.
            return None

        org = str(headers.get(self._org_header, "") or "").strip() if self._org_header else ""
        return _sso_user_context(
            user_id=user_id,
            email=headers.get(self._email_header, ""),
            display_name=headers.get(self._name_header, ""),
            groups=_coerce_groups(headers.get(self._groups_header)),
            org=org,
            admin_group=self._admin_group,
        )


class VerifiedJwtWebIdentityResolver:
    """Verify a bearer JWT's signature and map its claims to a :class:`UserContext`.

    Real cryptographic verification — the hard-auth contrast to the *unverified*
    convenience decode in :mod:`astrabox.providers.identity`. Structurally
    satisfies :class:`~astrabox.seams.identity.WebIdentityResolver`.

    **Key material** (precedence, read at ``__init__``; construction fails loud
    if none is configured, because the loader runs at ``create_app`` and a JWT
    resolver with no key can never verify a token):

    1. ``ASTRABOX_JWT_JWKS_URL`` — a PyJWKClient built once against the JWKS URL;
       key lookup does synchronous HTTP, dispatched via :func:`asyncio.to_thread`.
    2. ``ASTRABOX_JWT_ISSUER`` (no JWKS URL) — OIDC discovery: an async
       ``GET {issuer}/.well-known/openid-configuration`` yields ``jwks_uri``, from
       which a PyJWKClient is built and cached after the first success. Discovery
       *failure* is a retryable 503 per request and is NOT cached.
    3. ``ASTRABOX_JWT_SECRET`` — an HS256 shared secret.

    **Verification.** Algorithms from ``ASTRABOX_JWT_ALGORITHMS`` (default
    ``RS256,ES256`` for the JWKS/OIDC paths, ``HS256`` for a secret); audience
    checked iff ``ASTRABOX_JWT_AUDIENCE`` is set; issuer checked iff
    ``ASTRABOX_JWT_ISSUER`` is set (even alongside a JWKS URL); ``exp``/``iat``
    per PyJWT defaults.

    **Strictness.** Default strict: no token → 401. Lenient
    (``ASTRABOX_JWT_STRICT=0``): no token → ``None`` (anonymous fall-through). A
    token that IS present but fails verification (bad signature, expired, wrong
    audience, missing subject) is ALWAYS a hard 401 — a present credential is
    never silently degraded.
    """

    @staticmethod
    def login_url(next_url: str = "/") -> None:
        """Bearer JWT deployments do not expose a browser login route."""

        _ = next_url
        return None

    def __init__(self) -> None:
        self._user_claim = _env("ASTRABOX_JWT_USER_CLAIM") or _DEFAULT_USER_CLAIM
        self._email_claim = _env("ASTRABOX_JWT_EMAIL_CLAIM") or _DEFAULT_EMAIL_CLAIM
        self._name_claim = _env("ASTRABOX_JWT_NAME_CLAIM") or _DEFAULT_NAME_CLAIM
        self._groups_claim = _env("ASTRABOX_JWT_GROUPS_CLAIM") or _DEFAULT_GROUPS_CLAIM
        # "" = unset -> org falls back to the deployment default.
        self._org_claim = _env("ASTRABOX_JWT_ORG_CLAIM")
        self._admin_group = _env("ASTRABOX_ADMIN_GROUP") or _DEFAULT_ADMIN_GROUP
        self._strict = _strict_flag("ASTRABOX_JWT_STRICT")

        # Audience/issuer verification is independent of the key-material choice:
        # a JWKS-URL deployment can still pin the issuer for defence in depth.
        self._audience = _env("ASTRABOX_JWT_AUDIENCE") or None
        self._verify_issuer = _env("ASTRABOX_JWT_ISSUER") or None

        jwks_url = _env("ASTRABOX_JWT_JWKS_URL")
        issuer = _env("ASTRABOX_JWT_ISSUER")
        secret = _env("ASTRABOX_JWT_SECRET")

        self._secret: str | None = None
        self._jwk_client: PyJWKClient | None = None
        self._oidc_issuer: str | None = None
        self._discovery_lock = asyncio.Lock()

        if jwks_url:
            # Construct the JWKS client ONCE; it caches signing keys internally.
            self._mode = "jwks"
            self._jwk_client = PyJWKClient(jwks_url)
            self._algorithms = _algorithms(_DEFAULT_JWKS_ALGORITHMS)
        elif issuer:
            # Discovery deferred to the first request (async httpx); see _oidc_client.
            self._mode = "oidc"
            self._oidc_issuer = issuer
            self._algorithms = _algorithms(_DEFAULT_JWKS_ALGORITHMS)
        elif secret:
            self._mode = "secret"
            self._secret = secret
            self._algorithms = _algorithms(_DEFAULT_SECRET_ALGORITHMS)
        else:
            # Fails loud at construction (loader runs at create_app): no key material
            # means no possible verification — misconfig must break boot, not requests.
            raise RuntimeError(
                "VerifiedJwtWebIdentityResolver requires verification key material: set "
                "one of ASTRABOX_JWT_JWKS_URL, ASTRABOX_JWT_ISSUER (OIDC discovery), or "
                "ASTRABOX_JWT_SECRET (HS256)."
            )

    async def resolve(self, headers: Mapping[str, str]) -> UserContext | None:
        token = _bearer_token(headers)
        if not token:
            if self._strict:
                raise APIError(
                    code="UNAUTHORIZED",
                    message="bearer token required",
                    status_code=401,
                )
            # Lenient: no credential -> anonymous/local fall-through.
            return None

        # A present token is verified and fails closed: any verification/key error
        # is a hard 401, never a silent degrade. The one exception is OIDC discovery
        # unavailability, which _signing_key raises as a retryable 503 (an APIError
        # re-raised as-is here) — the token may be fine, the resolver just can't reach the IdP.
        try:
            key = await self._signing_key(token)
            payload = jwt.decode(token, key, **self._decode_kwargs())
        except APIError:
            raise
        except jwt.ExpiredSignatureError as exc:
            # Expiry is the one verification failure with its own correct client
            # action — acquire a new token and reconnect — so it needs a code a
            # client can branch on. Everything below keeps a single code on
            # purpose: a wrong signature and a string that is not a token call
            # for the same response, and separating them would only tell a
            # caller probing the endpoint which of the two it holds.
            raise APIError(
                code="TOKEN_EXPIRED",
                message="bearer token expired; obtain a new one from your token issuer",
                status_code=401,
            ) from exc
        except jwt.MissingRequiredClaimError as exc:
            # Named rather than merged into the sentence below, because this one
            # is fixed where the token is minted, not where it is presented. The
            # code stays shared: the caller stops either way.
            raise APIError(
                code="UNAUTHORIZED",
                message=(
                    f"bearer token is missing the required {exc.claim!r} claim"
                ),
                status_code=401,
            ) from exc
        except Exception as exc:
            raise APIError(
                code="UNAUTHORIZED",
                message="bearer token verification failed",
                status_code=401,
            ) from exc

        user_id = _claim_str(payload, self._user_claim)
        if not user_id:
            # A validly-signed token with no usable subject cannot identify a user.
            raise APIError(
                code="UNAUTHORIZED",
                message=f"token missing {self._user_claim!r} claim",
                status_code=401,
            )

        org = _claim_str(payload, self._org_claim) if self._org_claim else ""
        return _sso_user_context(
            user_id=user_id,
            email=_claim_str(payload, self._email_claim),
            display_name=_claim_str(payload, self._name_claim),
            groups=_coerce_groups(payload.get(self._groups_claim)),
            org=org,
            admin_group=self._admin_group,
        )

    def _decode_kwargs(self) -> dict[str, Any]:
        """PyJWT ``decode`` kwargs: algorithms + audience/issuer verification policy.

        ``exp`` is required, not merely verified when present. A token without
        one never expires, and in the shared-secret mode there is no issuer
        session to revoke it at — so a leaked one is a permanent credential.
        Both key sources decode here, so requiring it once covers them.
        """
        options = {"verify_aud": self._audience is not None, "require": ["exp"]}
        kwargs: dict[str, Any] = {"algorithms": self._algorithms, "options": options}
        if self._audience is not None:
            kwargs["audience"] = self._audience
        if self._verify_issuer is not None:
            kwargs["issuer"] = self._verify_issuer
        return kwargs

    async def _signing_key(self, token: str) -> Any:
        """Verification key for *token*: the HS256 secret, or a JWKS signing key.

        :meth:`PyJWKClient.get_signing_key_from_jwt` does synchronous HTTP + a
        JWKS parse, so it runs in a worker thread; the client caches keys across
        calls. In OIDC mode the discovery client is resolved (and cached) first.
        """
        if self._mode == "secret":
            return self._secret
        client = self._jwk_client
        if client is None:  # OIDC mode, first request (or after a prior discovery failure)
            client = await self._oidc_client()
        signing_key = await asyncio.to_thread(client.get_signing_key_from_jwt, token)
        return signing_key.key

    async def _oidc_client(self) -> PyJWKClient:
        """Resolve (and cache) the JWKS client via OIDC discovery; 503 on failure.

        Serialised so discovery runs at most once across concurrent first
        requests. On success the client is cached; on failure NOTHING is cached
        (``_jwk_client`` stays ``None``) and the next request retries — a 503 is
        surfaced meanwhile so a transient IdP outage is retryable, not a poisoned
        cache.
        """
        async with self._discovery_lock:
            if self._jwk_client is not None:  # another request won the race
                return self._jwk_client
            base = str(self._oidc_issuer or "").rstrip("/")
            discovery_url = f"{base}/.well-known/openid-configuration"
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.get(discovery_url)
                    response.raise_for_status()
                    document = response.json()
                jwks_uri = str((document or {}).get("jwks_uri") or "").strip()
                if not jwks_uri:
                    raise ValueError("OIDC discovery document is missing 'jwks_uri'")
            except Exception as exc:
                raise APIError(
                    code="IDENTITY_PROVIDER_UNAVAILABLE",
                    message=f"OIDC discovery failed for issuer {self._oidc_issuer!r}",
                    status_code=503,
                ) from exc
            self._jwk_client = PyJWKClient(jwks_uri)  # cache only after success
            return self._jwk_client


def _algorithms(default: Sequence[str]) -> list[str]:
    """Signature algorithms from ``ASTRABOX_JWT_ALGORITHMS`` or the path default."""
    raw = _env("ASTRABOX_JWT_ALGORITHMS")
    if raw:
        return [alg.strip() for alg in raw.split(",") if alg.strip()]
    return list(default)


def _claim_str(payload: Mapping[str, Any], claim: str) -> str:
    """Trimmed string value of *claim* in a decoded token payload (``""`` if absent)."""
    return str(payload.get(claim, "") or "").strip()
