"""Central verdicts for sandbox destruction and reference release.

Destroying a sandbox and clearing its last persisted reference are separate
decisions. A dangling reference can be reconciled, but a running sandbox with
no reference cannot be addressed. Therefore a reference may be released only
after confirming destruction of that exact sandbox.

Verdicts use resource ownership metadata, a live binding authority, or a
completed enumeration. Missing or incomplete evidence produces an unknown
verdict and permits no disposal. Verdict objects raise
:class:`UnjudgedSandbox` when coerced to ``bool``, forcing callers to select an
explicit outcome. They also carry the sandbox id so
:func:`may_sever_last_name` can reject stale ABA evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

#: The claim verdicts. A fifth value is not addable by accident:
#: :class:`SandboxClaim` validates against exactly this set.
SANDBOX_CLAIM_MINE = "MINE"
SANDBOX_CLAIM_FOREIGN = "FOREIGN"
SANDBOX_CLAIM_UNCLAIMED = "UNCLAIMED"
SANDBOX_CLAIM_UNKNOWN = "UNKNOWN"

_CLAIM_VERDICTS = frozenset(
    {
        SANDBOX_CLAIM_MINE,
        SANDBOX_CLAIM_FOREIGN,
        SANDBOX_CLAIM_UNCLAIMED,
        SANDBOX_CLAIM_UNKNOWN,
    }
)

#: The destruction outcomes.
SANDBOX_DESTRUCTION_CONFIRMED = "CONFIRMED"
SANDBOX_DESTRUCTION_UNCONFIRMED = "UNCONFIRMED"
SANDBOX_DESTRUCTION_REFUSED = "REFUSED"
SANDBOX_DESTRUCTION_RETAINED = "RETAINED"
SANDBOX_DESTRUCTION_NOTHING_NAMED = "NOTHING_NAMED"

_DESTRUCTION_OUTCOMES = frozenset(
    {
        SANDBOX_DESTRUCTION_CONFIRMED,
        SANDBOX_DESTRUCTION_UNCONFIRMED,
        SANDBOX_DESTRUCTION_REFUSED,
        SANDBOX_DESTRUCTION_RETAINED,
        SANDBOX_DESTRUCTION_NOTHING_NAMED,
    }
)


class UnjudgedSandbox(TypeError):
    """Raised when a disposal verdict is used as if it were a boolean.

    Both verdict types deliberately have no truth value. ``if verdict:`` would
    have to pick one of the three-or-four outcomes to mean "yes" and fold the
    rest into "no", and the folding is precisely the defect this module exists
    to remove — an outcome that could not be determined would silently become
    "there is nothing there". Ask for the outcome you mean
    (:attr:`SandboxDestruction.confirmed`, :attr:`SandboxClaim.may_destroy`,
    or the ``verdict``/``outcome`` string itself).
    """


@dataclass(frozen=True, slots=True)
class SandboxClaim:
    """Who holds one sandbox — as an answer that is allowed to be undetermined.

    ``verdict`` is one of the four ``SANDBOX_CLAIM_*`` constants:

    * ``MINE`` — the box carries an ownership fact that names the asker. The
      only verdict that licenses destroying it.
    * ``FOREIGN`` — it carries an ownership fact that names somebody else.
    * ``UNCLAIMED`` — a live authority was consulted and said nothing is bound
      to this box. It licenses reporting, never destruction: "no row names it"
      and "no one is using it" are different sentences, and only the first one
      was actually checked.
    * ``UNKNOWN`` — the question could not be answered: the control plane did
      not reply, the box carries no ownership metadata, the enumeration
      failed. Treated exactly as claimed.

    ``detail`` is prose for a log line or an operator: WHY the verdict is what
    it is, in the answering side's own words. It is never parsed.
    """

    verdict: str
    sandbox_id: str
    detail: str = ""
    session_id: str | None = None

    def __post_init__(self) -> None:
        if self.verdict not in _CLAIM_VERDICTS:
            raise ValueError(
                f"unknown sandbox claim verdict {self.verdict!r} "
                f"(expected one of {sorted(_CLAIM_VERDICTS)})"
            )

    def __bool__(self) -> bool:
        raise UnjudgedSandbox(
            "a SandboxClaim has no truth value: ask for .may_destroy, or read "
            f"its .verdict (this one is {self.verdict!r}: {self.detail})"
        )

    @property
    def may_destroy(self) -> bool:
        """True only for ``MINE``.

        ``UNCLAIMED`` deliberately does not license destruction here: a box
        nothing points at may still be a box somebody is mid-way through
        creating, and the authority that would have named it writes its
        pointer after the create returns. A create in flight can therefore look
        unclaimed even though the caller owns it.
        """
        return self.verdict == SANDBOX_CLAIM_MINE

    @classmethod
    def mine(cls, sandbox_id: str, *, detail: str, session_id: str | None = None) -> "SandboxClaim":
        return cls(
            verdict=SANDBOX_CLAIM_MINE,
            sandbox_id=str(sandbox_id or "").strip(),
            detail=detail,
            session_id=session_id,
        )

    @classmethod
    def foreign(cls, sandbox_id: str, *, detail: str, session_id: str | None = None) -> "SandboxClaim":
        return cls(
            verdict=SANDBOX_CLAIM_FOREIGN,
            sandbox_id=str(sandbox_id or "").strip(),
            detail=detail,
            session_id=session_id,
        )

    @classmethod
    def unclaimed(cls, sandbox_id: str, *, detail: str) -> "SandboxClaim":
        return cls(
            verdict=SANDBOX_CLAIM_UNCLAIMED,
            sandbox_id=str(sandbox_id or "").strip(),
            detail=detail,
        )

    @classmethod
    def unknown(cls, sandbox_id: str, *, detail: str) -> "SandboxClaim":
        return cls(
            verdict=SANDBOX_CLAIM_UNKNOWN,
            sandbox_id=str(sandbox_id or "").strip(),
            detail=detail,
        )


@dataclass(frozen=True, slots=True)
class SandboxDestruction:
    """What happened when one sandbox's destruction was attempted.

    ``outcome`` is one of the five ``SANDBOX_DESTRUCTION_*`` constants:

    * ``CONFIRMED`` — this exact box is gone, and something OTHER than the
      delete call itself says so (a second observation, or a control plane
      that reports the box absent). The only outcome that licenses releasing
      the box's last name.
    * ``UNCONFIRMED`` — destruction was attempted and its result could not be
      established: the request failed, timed out, or came back in a way that
      does not distinguish "this box is gone" from "this request never reached
      it". The name must be kept and the destruction retried.
    * ``REFUSED`` — nothing was attempted, because the attempt could not be
      made safely: the claim came back ``FOREIGN`` or ``UNKNOWN``, or the
      backend could not be resolved. Also keeps the name.
    * ``RETAINED`` — this operation successfully released a child resource
      inside the box, but intentionally left the box running because a
      longer-lived owner still owns it. It neither licenses severing that
      owner's last name nor creates an unaddressed-box leak for this caller.
    * ``NOTHING_NAMED`` — there was no sandbox id to act on at all. Not a
      success: it says only that this call had nothing to work with, which is
      why it does not license clearing a pointer either (a pointer that exists
      is by definition a name, so a caller holding one is never in this case).
    """

    outcome: str
    sandbox_id: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.outcome not in _DESTRUCTION_OUTCOMES:
            raise ValueError(
                f"unknown sandbox destruction outcome {self.outcome!r} "
                f"(expected one of {sorted(_DESTRUCTION_OUTCOMES)})"
            )

    def __bool__(self) -> bool:
        raise UnjudgedSandbox(
            "a SandboxDestruction has no truth value: ask for .confirmed, or "
            f"read its .outcome (this one is {self.outcome!r}: {self.detail})"
        )

    @property
    def confirmed(self) -> bool:
        """True only for ``CONFIRMED``."""
        return self.outcome == SANDBOX_DESTRUCTION_CONFIRMED

    @property
    def leaked_sandbox_id(self) -> str | None:
        """The id whose name must survive this call, or ``None``.

        ``UNCONFIRMED`` and ``REFUSED`` leave a box that may be running without
        another durable owner, and the id is the only way anything will ever
        address it again. ``RETAINED`` is different: the box is deliberately
        still running and remains named by its longer-lived owner.
        """
        if self.confirmed or self.outcome in {
            SANDBOX_DESTRUCTION_RETAINED,
            SANDBOX_DESTRUCTION_NOTHING_NAMED,
        }:
            return None
        return self.sandbox_id or None

    @classmethod
    def confirmed_gone(cls, sandbox_id: str, *, detail: str) -> "SandboxDestruction":
        resolved = str(sandbox_id or "").strip()
        if not resolved:
            raise ValueError("a confirmed destruction must name the sandbox it destroyed")
        return cls(
            outcome=SANDBOX_DESTRUCTION_CONFIRMED, sandbox_id=resolved, detail=detail
        )

    @classmethod
    def unconfirmed(cls, sandbox_id: str, *, detail: str) -> "SandboxDestruction":
        return cls(
            outcome=SANDBOX_DESTRUCTION_UNCONFIRMED,
            sandbox_id=str(sandbox_id or "").strip(),
            detail=detail,
        )

    @classmethod
    def refused(cls, sandbox_id: str, *, detail: str) -> "SandboxDestruction":
        return cls(
            outcome=SANDBOX_DESTRUCTION_REFUSED,
            sandbox_id=str(sandbox_id or "").strip(),
            detail=detail,
        )

    @classmethod
    def retained(cls, sandbox_id: str, *, detail: str) -> "SandboxDestruction":
        resolved = str(sandbox_id or "").strip()
        if not resolved:
            raise ValueError("a retained sandbox outcome must name its owner-held box")
        return cls(
            outcome=SANDBOX_DESTRUCTION_RETAINED,
            sandbox_id=resolved,
            detail=detail,
        )

    @classmethod
    def nothing_named(cls, *, detail: str) -> "SandboxDestruction":
        return cls(
            outcome=SANDBOX_DESTRUCTION_NOTHING_NAMED, sandbox_id="", detail=detail
        )


def may_sever_last_name(
    destruction: SandboxDestruction | None, *, sandbox_id: str
) -> bool:
    """The pairing rule: may this caller clear the last reference to ``sandbox_id``?

    Yes for exactly one shape of evidence — a ``CONFIRMED`` destruction that
    names THIS sandbox. Everything else is No, including:

    * no destruction was attempted (``None``);
    * a destruction that could not be confirmed, or was refused;
    * a confirmed destruction of a DIFFERENT sandbox. That is the ABA case,
      and it is the reason this takes the id separately instead of trusting
      the verdict's own: a caller that read pointer ``A``, stalled while a
      concurrent rebuild moved the pointer to ``B``, and then confirmed ``A``
      is dead, must not have its release land on the live ``B``.

    The caller still needs its own compare-and-set to make the release atomic
    against that rebuild; this predicate is what tells it the release is
    licensed at all.

    ``RETAINED`` is deliberately No here even though its box is not leaked: the
    box outlives this call under another owner, so whether a particular pointer
    to it may go is that caller's judgement about its own binding, not a
    general licence to drop the last name.
    """
    target = str(sandbox_id or "").strip()
    if not target or destruction is None:
        return False
    return destruction.confirmed and destruction.sandbox_id == target


def claim_from_metadata(
    *,
    sandbox_id: str,
    metadata: Mapping[str, Any] | None,
    expected_session_id: str | None,
    session_id_key: str,
    managed_by_key: str,
    managed_by_value: str,
) -> SandboxClaim:
    """Judge a box from the ownership fact it carries, and from nothing else.

    This is evidence kind (1): the create wrote who asked for the box, and the
    control plane hands it back. Two keys are read and both must agree before
    a box is anyone's to destroy:

    * ``managed_by_key`` must equal ``managed_by_value`` — this deployment
      created it. A box created by something else sharing the same control
      plane is FOREIGN even if the session ids happen to collide.
    * ``session_id_key`` must equal ``expected_session_id`` — this caller's
      session asked for it.

    ``metadata`` of ``None`` is a control plane that did not answer, which is
    UNKNOWN. Metadata that answered but carries neither key is also UNKNOWN
    and NOT ``UNCLAIMED``: a box created by a path that predates the metadata
    is still somebody's box, and absence of a claim has never been evidence of
    absence of an owner.

    ``expected_session_id`` of ``None`` means the caller cannot name the
    session it expects, so the second half of the test cannot be run — the
    result is UNKNOWN even for a box this deployment demonstrably created.
    """
    target = str(sandbox_id or "").strip()
    if metadata is None:
        return SandboxClaim.unknown(
            target,
            detail=(
                "the control plane returned no metadata for this sandbox, so "
                "its ownership could not be determined"
            ),
        )
    managed_by = str(metadata.get(managed_by_key) or "").strip()
    carried_session_id = str(metadata.get(session_id_key) or "").strip()
    expected = str(expected_session_id or "").strip()
    if not managed_by and not carried_session_id:
        return SandboxClaim.unknown(
            target,
            detail=(
                "this sandbox carries no ownership metadata "
                f"({managed_by_key!r}/{session_id_key!r} are both absent), so "
                "nothing about it can be attributed"
            ),
        )
    if managed_by != managed_by_value:
        return SandboxClaim.foreign(
            target,
            detail=(
                f"{managed_by_key}={managed_by!r} does not name this deployment "
                f"({managed_by_value!r})"
            ),
            session_id=carried_session_id or None,
        )
    if not expected:
        return SandboxClaim.unknown(
            target,
            detail=(
                "this deployment created the sandbox, but the caller named no "
                "session to check it against, so it cannot tell its own box "
                "from another session's"
            ),
            # `SandboxClaim.unknown` takes no session_id: an unknown claim must
            # not hand back an attribution it just said it could not make.
        )
    if carried_session_id != expected:
        return SandboxClaim.foreign(
            target,
            detail=(
                f"{session_id_key}={carried_session_id!r} belongs to another "
                f"session (this caller is {expected!r})"
            ),
            session_id=carried_session_id or None,
        )
    return SandboxClaim.mine(
        target,
        detail=(
            f"created by this deployment for session {expected!r} "
            f"({managed_by_key}={managed_by_value!r})"
        ),
        session_id=expected,
    )


__all__ = [
    "SANDBOX_CLAIM_FOREIGN",
    "SANDBOX_CLAIM_MINE",
    "SANDBOX_CLAIM_UNCLAIMED",
    "SANDBOX_CLAIM_UNKNOWN",
    "SANDBOX_DESTRUCTION_CONFIRMED",
    "SANDBOX_DESTRUCTION_NOTHING_NAMED",
    "SANDBOX_DESTRUCTION_REFUSED",
    "SANDBOX_DESTRUCTION_RETAINED",
    "SANDBOX_DESTRUCTION_UNCONFIRMED",
    "SandboxClaim",
    "SandboxDestruction",
    "UnjudgedSandbox",
    "claim_from_metadata",
    "may_sever_last_name",
]
