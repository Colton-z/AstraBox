"""Signed browser capability shared by the LiteLLM host and proxy adapters."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from collections.abc import Mapping, Sequence
from typing import Any

import jwt

CAPABILITY_PREFIX = "astrabox-litellm-cap-v1."
CAPABILITY_AUDIENCE = "astrabox-litellm"
ADMIN_UI_PURPOSE = "litellm-admin-ui"
AGENT_CONSOLE_PURPOSE = "litellm-agent-console"

#: LiteLLM's key type for "may call the LLM API, may not manage the proxy".
#: ``handle_key_type`` turns it into ``allowed_routes: ["llm_api_routes"]``, a
#: preset LiteLLM maintains. Naming the type rather than listing routes is what
#: keeps a route LiteLLM adds to inference from needing a change here. The
#: preset carries the MCP call routes and no management route; see
#: ``ensure_sandbox_inference_key`` for what a key of this type reaches on a
#: deployed gateway.
SANDBOX_INFERENCE_KEY_TYPE = "llm_api"

# Native Files has a provider-specific request shape. LiteLLM separately gates
# auth=true passthrough routes using key metadata, even for llm_api keys.
MODEL_INFERENCE_PASSTHROUGH_ROUTES = (
    "/v1/deepseek/v1/files",
)


def inference_key_metadata(metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Retain key identity and add the gateway's native model protocol grants."""
    if metadata is not None and not isinstance(metadata, Mapping):
        raise ValueError("model key metadata must be a mapping")
    result = dict(metadata or {})
    existing = result.get("allowed_passthrough_routes")
    routes = [] if existing is None else existing
    if not isinstance(routes, list) or any(not isinstance(route, str) or not route.strip() for route in routes):
        raise ValueError("model key allowed_passthrough_routes must be a list of non-empty paths")
    routes = list(routes)
    for route in MODEL_INFERENCE_PASSTHROUGH_ROUTES:
        if route not in routes:
            routes.append(route)
    result["allowed_passthrough_routes"] = routes
    return result


def sandbox_inference_key(secret: str) -> str:
    """Derive the sandbox's virtual key value from the deployment's signing key.

    Derived rather than stored because LiteLLM returns a key's plaintext only
    at creation. Storing it would add a deployment secret to distribute, and a
    second replica generating its own would leave the first replica's boxes
    holding a key that replica cannot name. A derivation gives every replica
    the same value with nothing to persist, and ``/key/generate`` accepts a
    caller-supplied ``key``, so the derived value is the value LiteLLM holds.

    Deriving from the signing secret means rotating that secret rotates this
    key. That is the intended coupling: anyone who can read the secret can
    already mint the browser capabilities the gateway trusts.
    """

    material = hmac.new(
        str(secret).encode("utf-8"),
        b"astrabox-sandbox-inference",
        hashlib.sha256,
    ).digest()
    suffix = base64.urlsafe_b64encode(material).decode("ascii").rstrip("=")
    return f"sk-astrabox-sandbox-{suffix}"


def session_inference_key(secret: str, session_id: str) -> str:
    """Derive one Session's virtual key from the deployment's signing key.

    Same reasoning as :func:`sandbox_inference_key`, applied per Session: the
    claim path mints the key and the egress vault spends it, and a derivation
    lets any replica (or a re-claim after recovery) name the same value with
    nothing stored. The Session id in the derivation input is what makes the
    key carry exactly one Session's identity at the gateway.
    """

    normalized = str(session_id or "").strip()
    if not normalized:
        raise ValueError("a session inference key requires a session id")
    material = hmac.new(
        str(secret).encode("utf-8"),
        f"astrabox-session:{normalized}".encode(),
        hashlib.sha256,
    ).digest()
    suffix = base64.urlsafe_b64encode(material).decode("ascii").rstrip("=")
    return f"sk-astrabox-session-{suffix}"


def looks_like_access_token(credential: str) -> bool:
    """Whether this is shaped like the OIDC provider's signed access token.

    Casdoor issues compact JWS: three base64url segments whose header decodes to
    JSON. LiteLLM's own keys are ``sk-``-prefixed opaque strings and never take
    this shape, so the two credential families stay distinguishable without the
    adapter having to know LiteLLM's key format.
    """

    segments = credential.split(".")
    if len(segments) != 3 or not all(segments):
        return False
    header = segments[0]
    padded = header + "=" * (-len(header) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii")).lstrip().startswith(b"{")
    except (ValueError, UnicodeEncodeError):
        return False

def mint_litellm_capability(
    *,
    secret: str,
    purpose: str,
    subject: str,
    email: str | None,
    display_name: str | None,
    org_id: str | None,
    roles: Sequence[str],
    ttl_seconds: int,
    extra_claims: Mapping[str, Any] | None = None,
) -> str:
    """Mint one short browser session understood only by the proxy adapter."""

    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": "astrabox",
        "aud": CAPABILITY_AUDIENCE,
        "use": purpose,
        "sub": subject,
        "email": email,
        "name": display_name,
        "org_id": org_id,
        "roles": list(roles),
        "iat": now,
        "exp": now + int(ttl_seconds),
    }
    claims.update(dict(extra_claims or {}))
    return CAPABILITY_PREFIX + jwt.encode(claims, secret, algorithm="HS256")


def verify_litellm_capability(
    token: str,
    *,
    secret: str,
    purposes: Sequence[str],
) -> dict[str, Any]:
    """Verify one proxy-adapter browser session and return its claims."""

    value = str(token or "").strip()
    if not value.startswith(CAPABILITY_PREFIX):
        raise jwt.InvalidTokenError("not a LiteLLM browser capability")
    claims = jwt.decode(
        value[len(CAPABILITY_PREFIX) :],
        secret,
        algorithms=["HS256"],
        audience=CAPABILITY_AUDIENCE,
        issuer="astrabox",
    )
    if str(claims.get("use") or "") not in set(purposes):
        raise jwt.InvalidTokenError("browser capability has the wrong purpose")
    if not str(claims.get("sub") or "").strip():
        raise jwt.InvalidTokenError("browser capability has no subject")
    return claims


__all__ = [
    "ADMIN_UI_PURPOSE",
    "AGENT_CONSOLE_PURPOSE",
    "SANDBOX_INFERENCE_KEY_TYPE",
    "MODEL_INFERENCE_PASSTHROUGH_ROUTES",
    "inference_key_metadata",
    "sandbox_inference_key",
    "CAPABILITY_AUDIENCE",
    "CAPABILITY_PREFIX",
    "looks_like_access_token",
    "mint_litellm_capability",
    "verify_litellm_capability",
]
