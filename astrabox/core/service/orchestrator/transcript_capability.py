"""Per-session transcript capability token — a stateless signed capability.

The transcript endpoints are a sandbox→backend channel with no user cookie, so
they authenticate on a per-session capability token embedded in the URL (see
``api/routes/transcript.py``). The token is ``HMAC-SHA256(signing_key,
session_id)``: the backend mints it into the base URL it hands each sandbox,
and the endpoint verifies it against the path ``session_id`` — no database
read, and multi-replica-safe because the signing key is shared, not per-process.

Forging another session's token requires the signing key, so a caller can only
reach the single session whose token it holds — never another user's.

Signing key resolution (all shared across replicas):
1. ``ASTRABOX_TRANSCRIPT_SIGNING_KEY`` if set (an operator-provisioned secret);
2. else derived from ``ASTRABOX_VAULT_MASTER_KEY`` with domain separation (the
   deployment already shares this across replicas for the vault) — so a
   multi-tenant deploy that has a vault needs no new configuration;
3. with the default ``ASTRABOX_SECRET_STORE=local``, else derived (same domain
   separation) from the deployment master key generated once into
   ``<state_dir>/vault.key``. This is the zero-config single-node path. A state
   directory that cannot hold the key fails during startup. A KMS-backed store
   exposes no raw master key, so its stateless replicas must set
   ``ASTRABOX_TRANSCRIPT_SIGNING_KEY`` explicitly.

``ASTRABOX_LOCAL_MODE=1`` (single-tenant, network-isolated dev run) keeps its
fixed local-development key; the fail-closed refusal remains as the backstop
should every path above be unavailable — the fence's trust root must never
silently become a public key.

Operational note — the token travels in the URL path (``/api/v1/sbxcap/{token}``;
the sandbox side has no header-injection hook, so the path is the only channel).
This process's own uvicorn access log masks the token segment
(:mod:`astrabox.api.access_log_privacy`, installed by ``create_app``), but any
TLS terminator / reverse proxy in front keeps its own access log: treat those
logs as credential-bearing, or exclude the ``sbxcap`` prefix there too. The
blast radius of a leaked token is bounded — it grants one session's transcript
endpoints, nothing else — and rotating the signing key invalidates every
outstanding token at once.
"""

from __future__ import annotations

import hashlib
import hmac
import os

from astrabox.common.logger.logger_factory import get_logger

logger = get_logger(__name__)

_DERIVE_DOMAIN = b"astrabox-transcript-capability-v1"
_LOCAL_DEV_KEY = b"astrabox-local-dev-transcript-key-not-for-multitenant"

_SIGNING_KEY_UNCONFIGURED = (
    "transcript capability signing key is not configured while capability "
    "enforcement is on and ASTRABOX_LOCAL_MODE is off: the fence's trust root "
    "would be a PUBLIC hardcoded key, so any holder of it could forge another "
    "tenant's token and defeat tenant isolation. Set "
    "ASTRABOX_TRANSCRIPT_SIGNING_KEY (or ASTRABOX_VAULT_MASTER_KEY), or — for a "
    "single-tenant local/dev run only — set ASTRABOX_LOCAL_MODE=1 (or "
    "ASTRABOX_TRANSCRIPT_CAPABILITY_REQUIRED=false)."
)


def _is_local_mode() -> bool:
    return str(os.getenv("ASTRABOX_LOCAL_MODE", "") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _capability_required() -> bool:
    return str(
        os.getenv("ASTRABOX_TRANSCRIPT_CAPABILITY_REQUIRED", "true") or "true"
    ).strip().lower() not in ("0", "false", "no", "off")


def _configured_key() -> bytes | None:
    explicit = str(os.getenv("ASTRABOX_TRANSCRIPT_SIGNING_KEY", "") or "").strip()
    if explicit:
        return explicit.encode("utf-8")
    master = str(os.getenv("ASTRABOX_VAULT_MASTER_KEY", "") or "").strip()
    if master:
        # Domain-separated subkey so the transcript signer is never the raw
        # vault key (different purpose, different blast radius).
        return hmac.new(master.encode("utf-8"), _DERIVE_DOMAIN, hashlib.sha256).digest()
    if _is_local_mode():
        # Local mode keeps its fixed dev key (below) rather than minting a
        # per-checkout key file — a dev wipes the state dir freely, and a token
        # that survives that wipe is worth more than key uniqueness with no
        # cross-user surface to protect.
        return None
    # The local provider owns the deployment master key. A non-local provider
    # raises here because it cannot supply raw signing material; this promotes
    # the missing explicit signing key to a startup failure.
    from astrabox.providers.secret_store import load_master_key

    return hmac.new(load_master_key(), _DERIVE_DOMAIN, hashlib.sha256).digest()


def _signing_key() -> bytes:
    configured = _configured_key()
    if configured is not None:
        return configured
    # FAIL-CLOSED: the public local-dev key is used only in an explicit
    # single-tenant local run. In any non-local deployment with capability
    # enforcement on, refusing here (rather than signing/verifying with a
    # public key) keeps a single environment error from disabling tenant
    # isolation. validate_signing_key_config() promotes this to a boot-time
    # failure.
    if _capability_required() and not _is_local_mode():
        raise RuntimeError(_SIGNING_KEY_UNCONFIGURED)
    return _LOCAL_DEV_KEY


def validate_signing_key_config() -> None:
    """Boot-time gate: fail loudly if the fence's trust root is a public key.

    Called from the app lifespan so a misconfigured multi-tenant deploy refuses
    to start rather than serving a forgeable fence until the first transcript
    request. A no-op in local mode or when capability enforcement is off.
    """
    if not _capability_required() or _is_local_mode():
        return
    if _configured_key() is None:
        raise RuntimeError(_SIGNING_KEY_UNCONFIGURED)


def mint_transcript_capability_token(session_id: str) -> str:
    """The capability token for ``session_id`` (hex HMAC)."""
    sid = str(session_id or "").strip()
    return hmac.new(_signing_key(), sid.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_transcript_capability_token(session_id: str, token: str) -> bool:
    """Constant-time check that ``token`` is ``session_id``'s capability token."""
    presented = str(token or "").strip()
    if not presented:
        return False
    return hmac.compare_digest(presented, mint_transcript_capability_token(session_id))


#: Domain separator for box-scoped tokens. Without it a box token and a session
#: token are the same construction over different ids, so one would authorize
#: the other's routes for any pair of equal ids. The separator contains a NUL,
#: which no id the platform mints can carry, so the two input spaces cannot
#: overlap however the ids are chosen.
_BOX_SUBJECT_DOMAIN = b"astrabox-sandbox-box-v1\x00"


def mint_sandbox_box_capability_token(sandbox_id: str) -> str:
    """The capability token for one box (hex HMAC), distinct from a session's.

    A box token authorizes exactly one thing — reporting that this box is going
    away — and it is held by the box itself, which is the only party that can
    know first. It says nothing about any conversation: which sessions that
    news affects is the platform's to look up, not the box's to assert.
    """
    box = str(sandbox_id or "").strip()
    return hmac.new(
        _signing_key(), _BOX_SUBJECT_DOMAIN + box.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def verify_sandbox_box_capability_token(sandbox_id: str, token: str) -> bool:
    """Constant-time check that ``token`` is ``sandbox_id``'s box token."""
    presented = str(token or "").strip()
    if not presented:
        return False
    return hmac.compare_digest(presented, mint_sandbox_box_capability_token(sandbox_id))


def mint_runtime_state_capability_token(owner_key: str) -> str:
    """Authorize native state custody for one canonical workspace owner."""
    if not owner_key:
        raise ValueError("runtime state capability requires an owner")
    return hmac.new(
        _signing_key(),
        b"astrabox-runtime-state\x00" + owner_key.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_runtime_state_capability_token(owner_key: str, token: str) -> bool:
    """Verify workspace state access without accepting a conversation token."""
    return bool(token) and hmac.compare_digest(
        token, mint_runtime_state_capability_token(owner_key)
    )
