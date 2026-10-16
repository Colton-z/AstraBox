"""Whose a sandbox is decides whether it may be destroyed.

Ownership comes from a fact the resource carries: absent metadata is UNKNOWN,
not unclaimed; another session's, deployment's or installation's box is
FOREIGN; and an UNCLAIMED box still licenses no destruction.
"""

from __future__ import annotations

from typing import Any

from astrabox.seams.sandbox import (
    SANDBOX_INSTALLATION_METADATA_KEY,
    SANDBOX_MANAGED_BY_METADATA_KEY,
    SANDBOX_MANAGED_BY_METADATA_VALUE,
    SANDBOX_SESSION_ID_METADATA_KEY,
)
from astrabox.seams.sandbox_disposal import (
    SANDBOX_CLAIM_FOREIGN,
    SANDBOX_CLAIM_UNCLAIMED,
    SANDBOX_CLAIM_UNKNOWN,
    SandboxClaim,
    claim_from_metadata,
)


# ── 3. ownership comes from a fact the box carries ───────────────────────────


_INSTALLATION = "this-installation"


def _metadata(
    session_id: str,
    *,
    managed_by: str = SANDBOX_MANAGED_BY_METADATA_VALUE,
    installation: str | None = _INSTALLATION,
) -> dict[str, Any]:
    return {
        SANDBOX_SESSION_ID_METADATA_KEY: session_id,
        SANDBOX_MANAGED_BY_METADATA_KEY: managed_by,
        **({SANDBOX_INSTALLATION_METADATA_KEY: installation} if installation else {}),
    }


def _claim(metadata: dict[str, Any] | None, *, expected: str | None) -> SandboxClaim:
    return claim_from_metadata(
        sandbox_id="sb-1",
        metadata=metadata,
        expected_session_id=expected,
        session_id_key=SANDBOX_SESSION_ID_METADATA_KEY,
        managed_by_key=SANDBOX_MANAGED_BY_METADATA_KEY,
        managed_by_value=SANDBOX_MANAGED_BY_METADATA_VALUE,
        installation_key=SANDBOX_INSTALLATION_METADATA_KEY,
        installation_value=_INSTALLATION,
    )


def test_a_box_naming_another_session_is_foreign() -> None:
    claim = _claim(_metadata("sess-2"), expected="sess-1")
    assert claim.verdict == SANDBOX_CLAIM_FOREIGN
    assert not claim.may_destroy


def test_a_box_naming_another_deployment_is_foreign_even_on_a_matching_session() -> None:
    """Two deployments sharing a control plane can mint the same session id."""
    claim = _claim(_metadata("sess-1", managed_by="someone-else"), expected="sess-1")
    assert claim.verdict == SANDBOX_CLAIM_FOREIGN


def test_a_box_of_another_astrabox_installation_is_foreign_on_a_matching_session() -> None:
    """Two installations on one Docker daemon both write managed-by=astrabox."""
    claim = _claim(_metadata("sess-1", installation="other-installation"), expected="sess-1")
    assert claim.verdict == SANDBOX_CLAIM_FOREIGN
    assert not claim.may_destroy


def test_a_box_carrying_no_ownership_metadata_is_unknown() -> None:
    """Absence of a claim has never been evidence of absence of an owner."""
    claim = _claim({}, expected="sess-1")
    assert claim.verdict == SANDBOX_CLAIM_UNKNOWN


def test_an_unclaimed_box_still_licenses_no_destruction() -> None:
    """UNCLAIMED means one authority said nothing is bound — not that nothing is.

    The pointer that would name a box is written AFTER its create returns, so
    "no row names it" is exactly what an in-flight provision looks like.
    """
    assert not SandboxClaim.unclaimed("sb-1", detail="no row names it").may_destroy
    assert SandboxClaim.unclaimed("sb-1", detail="x").verdict == SANDBOX_CLAIM_UNCLAIMED
