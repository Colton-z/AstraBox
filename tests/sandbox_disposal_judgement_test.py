"""Verify the shared sandbox disposal verdicts.

The suite covers these rules in order:

1. **"I could not tell" cannot be collapsed.** Both verdict types raise on
   ``bool()``. This is the property that makes the rest enforceable rather than
   advisory: ``if killed:`` does not compile into an optimistic default any
   more, it raises.
2. **Destruction and the severing of a name are paired, one way.** A name is
   released only by a CONFIRMED destruction OF THAT SANDBOX — never a different
   one (the ABA), never an unconfirmed or refused one, never none at all.
3. **Ownership comes from a fact the resource carries.** Absent metadata is
   UNKNOWN, not unclaimed; another deployment's box is FOREIGN; only a box
   naming this deployment AND this session is MINE.
4. **Confirmation takes two observations.** The delete's own account of itself
   is not evidence that the box is gone.
"""

from __future__ import annotations

from typing import Any

import pytest

from astrabox.core.service.orchestrator.sandbox_names import (
    UNDESTROYED_SANDBOX_IDS,
    keep_name_updates,
    read_undestroyed,
    release_name_updates,
)
import astrabox.seams.sandbox as sandbox_seam
from astrabox.seams.sandbox import (
    SANDBOX_LIFECYCLE_PROBE_FAILED,
    SANDBOX_LIFECYCLE_PROBE_NOT_FOUND,
    SANDBOX_LIFECYCLE_PROBE_OK,
    SANDBOX_MANAGED_BY_METADATA_KEY,
    SANDBOX_MANAGED_BY_METADATA_VALUE,
    SANDBOX_SESSION_ID_METADATA_KEY,
    SandboxLifecycleProbeResult,
    SandboxProvider,
)
from astrabox.seams.sandbox_disposal import (
    SANDBOX_CLAIM_FOREIGN,
    SANDBOX_CLAIM_MINE,
    SANDBOX_CLAIM_UNCLAIMED,
    SANDBOX_CLAIM_UNKNOWN,
    SANDBOX_DESTRUCTION_NOTHING_NAMED,
    SANDBOX_DESTRUCTION_RETAINED,
    SandboxClaim,
    SandboxDestruction,
    UnjudgedSandbox,
    claim_from_metadata,
    may_sever_last_name,
)


# ── 1. An unresolved verdict is an answer, and cannot be folded away ─────────


def test_a_destruction_verdict_has_no_truth_value() -> None:
    """Boolean coercion cannot collapse distinct destruction outcomes."""
    for verdict in (
        SandboxDestruction.confirmed_gone("sb-1", detail="gone"),
        SandboxDestruction.unconfirmed("sb-1", detail="unknown"),
        SandboxDestruction.refused("sb-1", detail="foreign"),
        SandboxDestruction.retained("sb-1", detail="owned by agent"),
        SandboxDestruction.nothing_named(detail="nothing"),
    ):
        with pytest.raises(UnjudgedSandbox):
            bool(verdict)
        with pytest.raises(UnjudgedSandbox):
            if verdict:  # noqa: SIM103 - the point is that this line raises
                pass


def test_a_claim_has_no_truth_value() -> None:
    for claim in (
        SandboxClaim.mine("sb-1", detail="ours"),
        SandboxClaim.foreign("sb-1", detail="theirs"),
        SandboxClaim.unclaimed("sb-1", detail="gone"),
        SandboxClaim.unknown("sb-1", detail="could not ask"),
    ):
        with pytest.raises(UnjudgedSandbox):
            bool(claim)


def test_a_verdict_outside_the_vocabulary_is_refused() -> None:
    """A fifth outcome cannot be smuggled in as a string."""
    with pytest.raises(ValueError):
        SandboxDestruction(outcome="PROBABLY", sandbox_id="sb-1")
    with pytest.raises(ValueError):
        SandboxClaim(verdict="SORT_OF", sandbox_id="sb-1")


def test_a_confirmed_destruction_must_name_the_sandbox_it_destroyed() -> None:
    with pytest.raises(ValueError):
        SandboxDestruction.confirmed_gone("", detail="gone")


# ── 2. the pairing rule ──────────────────────────────────────────────────────


def test_only_a_confirmed_destruction_of_this_sandbox_releases_its_name() -> None:
    assert may_sever_last_name(
        SandboxDestruction.confirmed_gone("sb-1", detail="gone"), sandbox_id="sb-1"
    )


def test_a_proof_about_another_sandbox_does_not_release_this_pointer() -> None:
    """The ABA case, expressed once instead of at every pointer.

    A caller read pointer A, stalled while a concurrent rebuild moved the
    pointer to a live B, then confirmed A's death. Its release must not land on
    B — the outcome alone would say yes.
    """
    assert not may_sever_last_name(
        SandboxDestruction.confirmed_gone("sb-A", detail="gone"), sandbox_id="sb-B"
    )


@pytest.mark.parametrize(
    "destruction",
    [
        None,
        SandboxDestruction.unconfirmed("sb-1", detail="the probe failed"),
        SandboxDestruction.refused("sb-1", detail="claim was UNKNOWN"),
        SandboxDestruction.retained("sb-1", detail="owned by agent"),
        SandboxDestruction.nothing_named(detail="no id"),
    ],
)
def test_nothing_short_of_confirmation_releases_a_name(
    destruction: SandboxDestruction | None,
) -> None:
    assert not may_sever_last_name(destruction, sandbox_id="sb-1")


def test_an_unconfirmed_destruction_hands_back_the_id_that_must_survive() -> None:
    assert (
        SandboxDestruction.unconfirmed("sb-1", detail="x").leaked_sandbox_id == "sb-1"
    )
    assert (
        SandboxDestruction.refused("sb-1", detail="x").leaked_sandbox_id == "sb-1"
    )
    retained = SandboxDestruction.retained("sb-1", detail="agent still owns it")
    assert retained.outcome == SANDBOX_DESTRUCTION_RETAINED
    assert retained.leaked_sandbox_id is None
    assert SandboxDestruction.confirmed_gone("sb-1", detail="x").leaked_sandbox_id is None
    assert SandboxDestruction.nothing_named(detail="x").leaked_sandbox_id is None


# ── 3. ownership comes from a fact the box carries ───────────────────────────


def _metadata(session_id: str, *, managed_by: str = SANDBOX_MANAGED_BY_METADATA_VALUE) -> dict[str, Any]:
    return {
        SANDBOX_SESSION_ID_METADATA_KEY: session_id,
        SANDBOX_MANAGED_BY_METADATA_KEY: managed_by,
    }


def _claim(metadata: dict[str, Any] | None, *, expected: str | None) -> SandboxClaim:
    return claim_from_metadata(
        sandbox_id="sb-1",
        metadata=metadata,
        expected_session_id=expected,
        session_id_key=SANDBOX_SESSION_ID_METADATA_KEY,
        managed_by_key=SANDBOX_MANAGED_BY_METADATA_KEY,
        managed_by_value=SANDBOX_MANAGED_BY_METADATA_VALUE,
    )


def test_a_box_naming_this_deployment_and_this_session_is_mine() -> None:
    claim = _claim(_metadata("sess-1"), expected="sess-1")
    assert claim.verdict == SANDBOX_CLAIM_MINE
    assert claim.may_destroy


def test_a_box_naming_another_session_is_foreign() -> None:
    claim = _claim(_metadata("sess-2"), expected="sess-1")
    assert claim.verdict == SANDBOX_CLAIM_FOREIGN
    assert not claim.may_destroy


def test_a_box_naming_another_deployment_is_foreign_even_on_a_matching_session() -> None:
    """Two deployments sharing a control plane can mint the same session id."""
    claim = _claim(_metadata("sess-1", managed_by="someone-else"), expected="sess-1")
    assert claim.verdict == SANDBOX_CLAIM_FOREIGN


def test_a_control_plane_that_did_not_answer_is_unknown_not_unclaimed() -> None:
    claim = _claim(None, expected="sess-1")
    assert claim.verdict == SANDBOX_CLAIM_UNKNOWN
    assert not claim.may_destroy


def test_a_box_carrying_no_ownership_metadata_is_unknown() -> None:
    """Absence of a claim has never been evidence of absence of an owner."""
    claim = _claim({}, expected="sess-1")
    assert claim.verdict == SANDBOX_CLAIM_UNKNOWN


def test_a_caller_that_cannot_name_its_session_gets_unknown() -> None:
    claim = _claim(_metadata("sess-1"), expected=None)
    assert claim.verdict == SANDBOX_CLAIM_UNKNOWN
    assert claim.session_id is None, (
        "an UNKNOWN claim must not hand back an attribution it just said it "
        "could not make"
    )


def test_an_unclaimed_box_still_licenses_no_destruction() -> None:
    """UNCLAIMED means one authority said nothing is bound — not that nothing is.

    The pointer that would name a box is written AFTER its create returns, so
    "no row names it" is exactly what an in-flight provision looks like.
    """
    assert not SandboxClaim.unclaimed("sb-1", detail="no row names it").may_destroy
    assert SandboxClaim.unclaimed("sb-1", detail="x").verdict == SANDBOX_CLAIM_UNCLAIMED


# ── 4. confirmation takes two observations ───────────────────────────────────


class _Backend(SandboxProvider):
    """A provider with only the by-id lifecycle filled in.

    ``confirm_destroyed`` is NOT overridden: the seam's own implementation is
    what these tests exercise, because it is the single minting point every
    backend's destruction verdict comes from.
    """

    name = "test_backend"

    def __init__(
        self,
        *,
        kill_result: bool = True,
        kill_error: Exception | None = None,
        probe: SandboxLifecycleProbeResult | None = None,
        probe_error: Exception | None = None,
        probe_sequence: list[SandboxLifecycleProbeResult] | None = None,
    ) -> None:
        self.kill_result = kill_result
        self.kill_error = kill_error
        self._probe = probe
        self._probe_error = probe_error
        self._probe_sequence = list(probe_sequence or [])
        self.probe_calls = 0
        self.killed: list[str] = []

    def connection_config(self, **_kwargs: Any) -> Any:
        return None

    def secret_material(self, *, settings: Any) -> str:
        return ""

    def build_dataplane(self, **_kwargs: Any) -> Any:
        raise NotImplementedError

    async def connect(self, sandbox_id: str) -> Any:
        raise NotImplementedError

    async def kill(self, sandbox_id: str) -> bool:
        self.killed.append(sandbox_id)
        if self.kill_error is not None:
            raise self.kill_error
        return self.kill_result

    async def probe(self, sandbox_id: str) -> SandboxLifecycleProbeResult:
        self.probe_calls += 1
        if self._probe_error is not None:
            raise self._probe_error
        if self._probe_sequence:
            return self._probe_sequence.pop(0)
        if self._probe is None:
            raise NotImplementedError
        return self._probe


async def test_a_delete_the_control_plane_backs_up_is_confirmed() -> None:
    backend = _Backend(
        probe=SandboxLifecycleProbeResult(probe_status=SANDBOX_LIFECYCLE_PROBE_NOT_FOUND)
    )
    destruction = await backend.confirm_destroyed("sb-1")
    assert destruction.confirmed
    assert destruction.sandbox_id == "sb-1"


async def test_a_box_still_reported_after_a_successful_delete_is_not_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The delete said yes and the box is still there.

    Then the delete's answer came from somewhere that was not this sandbox
    ending — a base URL pointing elsewhere, a proxy answering for a route it
    does not have. Reading it as success is how a live box loses its name.
    """
    backend = _Backend(
        probe=SandboxLifecycleProbeResult(
            probe_status=SANDBOX_LIFECYCLE_PROBE_OK, sandbox_state="running"
        )
    )
    monkeypatch.setattr(sandbox_seam, "DESTRUCTION_SETTLE_BUDGET_S", 0.05)
    monkeypatch.setattr(sandbox_seam, "DESTRUCTION_SETTLE_INTERVAL_S", 0.01)
    destruction = await backend.confirm_destroyed("sb-1")
    assert not destruction.confirmed
    assert destruction.leaked_sandbox_id == "sb-1"
    # The verdict spent the settle budget before giving up: an accepted delete
    # on Kubernetes stays visible until pod teardown completes, so one look is
    # not evidence of an undead box.
    assert backend.probe_calls > 1


async def test_a_delete_that_failed_over_a_box_already_gone_is_confirmed() -> None:
    """The confirmation is the SECOND observation, not the first."""
    backend = _Backend(
        kill_error=RuntimeError("connection reset"),
        probe=SandboxLifecycleProbeResult(probe_status=SANDBOX_LIFECYCLE_PROBE_NOT_FOUND),
    )
    destruction = await backend.confirm_destroyed("sb-1")
    assert destruction.confirmed


async def test_an_unreachable_control_plane_confirms_nothing() -> None:
    backend = _Backend(
        probe=SandboxLifecycleProbeResult(
            probe_status=SANDBOX_LIFECYCLE_PROBE_FAILED, error_text="timed out"
        )
    )
    destruction = await backend.confirm_destroyed("sb-1")
    assert not destruction.confirmed
    assert "timed out" in destruction.detail


async def test_a_probe_that_raised_confirms_nothing() -> None:
    backend = _Backend(probe_error=RuntimeError("boom"))
    destruction = await backend.confirm_destroyed("sb-1")
    assert not destruction.confirmed


async def test_a_backend_with_no_probe_says_the_evidence_is_single() -> None:
    """The honest degraded form, not a hidden one."""
    backend = _Backend()  # probe raises NotImplementedError
    destruction = await backend.confirm_destroyed("sb-1")
    assert destruction.confirmed
    assert "no liveness probe" in destruction.detail

    refusing = _Backend(kill_result=False)
    assert not (await refusing.confirm_destroyed("sb-1")).confirmed


async def test_destroying_nothing_is_not_destroying_something() -> None:
    backend = _Backend()
    destruction = await backend.confirm_destroyed("  ")
    assert destruction.outcome == SANDBOX_DESTRUCTION_NOTHING_NAMED
    assert backend.killed == []
    assert not may_sever_last_name(destruction, sandbox_id="sb-1")


async def test_a_backend_that_cannot_say_whose_a_box_is_says_so() -> None:
    """The default claim is UNKNOWN naming the backend — never an optimistic one."""
    claim = await _Backend().claim_of("sb-1", expected_session_id="sess-1")
    assert claim.verdict == SANDBOX_CLAIM_UNKNOWN
    assert "test_backend" in claim.detail


# ── the name ledger: where a name goes when it may not be severed ────────────


def test_an_unconfirmed_destruction_puts_the_id_on_the_row() -> None:
    updates = keep_name_updates(
        SandboxDestruction.unconfirmed("sb-1", detail="x"), row={}
    )
    assert updates == {UNDESTROYED_SANDBOX_IDS: ["sb-1"]}


def test_a_confirmed_destruction_adds_no_name() -> None:
    assert keep_name_updates(
        SandboxDestruction.confirmed_gone("sb-1", detail="x"), row={}
    ) == {}


def test_an_intentionally_retained_agent_box_adds_no_leak_name() -> None:
    assert keep_name_updates(
        SandboxDestruction.retained("sb-1", detail="agent still owns it"), row={}
    ) == {}


def test_recording_the_same_failure_twice_does_not_grow_the_ledger() -> None:
    row = {UNDESTROYED_SANDBOX_IDS: ["sb-1"]}
    assert keep_name_updates(
        SandboxDestruction.unconfirmed("sb-1", detail="x"), row=row
    ) == {}


def test_releasing_a_pointer_needs_the_pairing_rule() -> None:
    row = {"sandbox_id": "sb-1", "sandbox_endpoint": "box:8000"}
    released = release_name_updates(
        SandboxDestruction.confirmed_gone("sb-1", detail="x"),
        sandbox_id="sb-1",
        row=row,
        also_clear=("sandbox_endpoint",),
    )
    assert released == {"sandbox_id": None, "sandbox_endpoint": None}

    kept = release_name_updates(
        SandboxDestruction.unconfirmed("sb-1", detail="x"),
        sandbox_id="sb-1",
        row=row,
        also_clear=("sandbox_endpoint",),
    )
    assert "sandbox_id" not in kept, "an unconfirmed destroy never clears the pointer"
    assert kept[UNDESTROYED_SANDBOX_IDS] == ["sb-1"]


def test_a_confirmed_destruction_takes_the_id_back_off_the_ledger() -> None:
    row = {"sandbox_id": "sb-1", UNDESTROYED_SANDBOX_IDS: ["sb-0", "sb-1"]}
    updates = release_name_updates(
        SandboxDestruction.confirmed_gone("sb-1", detail="x"),
        sandbox_id="sb-1",
        row=row,
    )
    assert updates["sandbox_id"] is None
    assert updates[UNDESTROYED_SANDBOX_IDS] == ["sb-0"]


def test_a_malformed_ledger_reads_as_empty_rather_than_raising() -> None:
    assert read_undestroyed({UNDESTROYED_SANDBOX_IDS: "sb-1"}) == []
    assert read_undestroyed({UNDESTROYED_SANDBOX_IDS: ["sb-1", "", None]}) == ["sb-1"]
    assert read_undestroyed(None) == []


def test_a_retained_box_releases_this_pointer_but_not_the_owners() -> None:
    """RETAINED is the case where this pointer was never the last name.

    The box is deliberately alive under a longer-lived owner — an Agent whose
    other conversations are working in it — and that owner names it. Keeping
    this caller's pointer leaves an ended conversation claiming a box it has no
    part of, which is what an archived Session was observed doing.
    """

    row = {"sandbox_id": "sb-1", "sandbox_endpoint": "box:8000"}
    released = release_name_updates(
        SandboxDestruction(
            outcome=SANDBOX_DESTRUCTION_RETAINED,
            sandbox_id="sb-1",
            detail="the agent-owned box remains durably named",
        ),
        sandbox_id="sb-1",
        row=row,
        also_clear=("sandbox_endpoint",),
    )
    assert released == {"sandbox_id": None, "sandbox_endpoint": None}
    # Not on the ledger: the ledger is for boxes nothing will ever address
    # again, and this one is being addressed by its owner right now.
    assert UNDESTROYED_SANDBOX_IDS not in released


def test_retaining_a_different_box_does_not_release_this_pointer() -> None:
    """The ABA control, which RETAINED must obey as strictly as CONFIRMED does.

    A caller that read pointer A, stalled while a rebuild moved the pointer to
    B, and then heard "A was retained" must not have its release land on B.
    """

    kept = release_name_updates(
        SandboxDestruction(
            outcome=SANDBOX_DESTRUCTION_RETAINED,
            sandbox_id="sb-OLD",
            detail="a different box was retained",
        ),
        sandbox_id="sb-NEW",
        row={"sandbox_id": "sb-NEW"},
    )
    assert kept == {}
async def test_an_accepted_delete_still_settling_confirms_once_the_box_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kubernetes answers the delete and keeps the record until teardown ends.

    The confirming observation re-asks within the settle budget, so a
    terminating box confirms instead of being reported undead — the p114
    shape: DELETE 204, an immediate GET still `running`, and the session
    delete answering 502 for a box that was seconds from gone.
    """

    monkeypatch.setattr(sandbox_seam, "DESTRUCTION_SETTLE_INTERVAL_S", 0.01)
    backend = _Backend(
        probe_sequence=[
            SandboxLifecycleProbeResult(
                probe_status=SANDBOX_LIFECYCLE_PROBE_OK, sandbox_state="running"
            ),
            SandboxLifecycleProbeResult(
                probe_status=SANDBOX_LIFECYCLE_PROBE_OK, sandbox_state="running"
            ),
            SandboxLifecycleProbeResult(
                probe_status=SANDBOX_LIFECYCLE_PROBE_NOT_FOUND
            ),
        ]
    )
    destruction = await backend.confirm_destroyed("sb-1")
    assert destruction.confirmed
    assert backend.probe_calls == 3


async def test_a_delete_that_was_not_accepted_does_not_wait_out_the_budget() -> None:
    """Only an ACCEPTED delete has a teardown window to wait for.

    A delete that raised proves nothing is terminating; polling would just
    stall every failed delete for the whole budget.
    """

    backend = _Backend(
        kill_error=RuntimeError("connection reset"),
        probe=SandboxLifecycleProbeResult(
            probe_status=SANDBOX_LIFECYCLE_PROBE_OK, sandbox_state="running"
        ),
    )
    destruction = await backend.confirm_destroyed("sb-1")
    assert not destruction.confirmed
    assert backend.probe_calls == 1
