"""OpenSandbox metadata projections for provider-neutral identities."""

from __future__ import annotations

import base64
import hashlib
import re
import uuid

_LABEL_VALUE_RE = re.compile(r"^[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?$")
_LABEL_VALUE_MAX_LENGTH = 63
_CLIENT_POOL_SESSION_RE = re.compile(r"^client-pool-(?P<pool>[0-9a-f]{12})-[0-9a-f]{24}$")


def client_pool_tag(pool_name: str) -> str:
    """The digest of ``pool_name`` a member's identity carries in place of the name.

    A pool name need not fit a label value beside a unique suffix; twelve hex
    characters of its SHA-256 do.
    """

    return hashlib.sha256(pool_name.encode("utf-8")).hexdigest()[:12]


def client_pool_tag_of(session_id: str) -> str | None:
    """The :func:`client_pool_tag` of the pool a member identity belongs to, or None."""

    match = _CLIENT_POOL_SESSION_RE.fullmatch(session_id)
    return match.group("pool") if match is not None else None


def is_client_pool_identity(session_id: str, assignment_id: str) -> bool:
    """Recognize the provider's member identity until platform adoption.

    The identity covers creation, idle inventory, and acquisition before the
    platform publishes its owner. While the platform records the member's
    pool, the SDK controls retirement in all three states; an empty idle
    snapshot alone does not transfer that authority. A member of a pool the
    platform has stopped recording is never handed out again, and the
    ownerless reap destroys it
    (:meth:`~.sandbox.OpenSandboxSandboxProvider.unclaimed_sandbox_refusal`).
    """

    if _CLIENT_POOL_SESSION_RE.fullmatch(session_id) is None:
        return False
    try:
        assignment = uuid.UUID(assignment_id)
    except ValueError:
        return False
    return assignment.version == 4 and str(assignment) == assignment_id


def _identity_metadata_value(
    value: str,
    *,
    identity_name: str,
    digest_prefix: str,
) -> str:
    """Project one platform identity onto OpenSandbox's label-value wire."""

    identity = str(value or "").strip()
    if not identity:
        raise ValueError(f"sandbox {identity_name} must be non-empty")
    if len(identity) <= _LABEL_VALUE_MAX_LENGTH and _LABEL_VALUE_RE.fullmatch(
        identity
    ):
        return identity
    digest = base64.b32encode(
        hashlib.sha256(identity.encode("utf-8")).digest()
    ).decode("ascii")
    return f"{digest_prefix}-{digest.rstrip('=').lower()}"


def assignment_metadata_value(assignment_id: str) -> str:
    """Project one logical assignment onto OpenSandbox's label-value wire.

    OpenSandbox validates metadata values as Kubernetes labels. Legal values
    remain readable; every other value becomes the full SHA-256 digest encoded
    as unpadded base32, which fits the label grammar without truncation.
    """

    return _identity_metadata_value(
        assignment_id,
        identity_name="assignment_id",
        digest_prefix="a",
    )


def session_metadata_value(session_id: str) -> str:
    """Project one platform runtime-owner identity onto OpenSandbox's label wire."""

    return _identity_metadata_value(
        session_id,
        identity_name="session_id",
        digest_prefix="s",
    )
