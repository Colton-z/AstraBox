"""Admission seam — the injection point for enterprise quota/rate policy.

The base admits everything; a deployment registers an AdmissionController to
cap per-user concurrent turns / sandboxes without forking the hot path. These
tests pin the seam contract: default allow-all, register-override, the
enforce_admission 429-on-deny helper, and the FAIL-OPEN rule (a raising
controller admits, never takes turns down).
"""

from __future__ import annotations

import pytest

import astrabox.seams.admission as adm
from astrabox.common.utils.errors import APIError


@pytest.fixture(autouse=True)
def _restore_controller():
    original = adm.get_admission_controller()
    yield
    adm._controller = original


async def test_default_admits_everything() -> None:
    decision = await adm.check_admission(
        adm.AdmissionRequest(kind=adm.ADMISSION_KIND_TURN, user_id="u1")
    )
    assert decision.allowed is True


async def test_registered_controller_can_deny() -> None:
    class DenyTurns(adm.AdmissionController):
        async def check(self, request: adm.AdmissionRequest) -> adm.AdmissionDecision:
            if request.kind == adm.ADMISSION_KIND_TURN:
                return adm.AdmissionDecision.deny(
                    "per-user turn cap reached", retry_after_seconds=5
                )
            return adm.AdmissionDecision.allow()

    adm.register_admission_controller(DenyTurns())

    # enforce_admission raises 429 with the reason + retry hint on a turn.
    with pytest.raises(APIError) as exc:
        await adm.enforce_admission(
            adm.AdmissionRequest(kind=adm.ADMISSION_KIND_TURN, user_id="u1")
        )
    assert exc.value.status_code == 429
    assert exc.value.code == "ADMISSION_DENIED"
    assert exc.value.data.get("retry_after_seconds") == 5

    # A sandbox request is still allowed (enforce returns without raising).
    await adm.enforce_admission(
        adm.AdmissionRequest(kind=adm.ADMISSION_KIND_SANDBOX, user_id="u1")
    )


async def test_enforce_allows_when_admitted() -> None:
    await adm.enforce_admission(
        adm.AdmissionRequest(kind=adm.ADMISSION_KIND_TURN, user_id="u1")
    )  # no raise


async def test_fail_open_on_controller_exception() -> None:
    class Broken(adm.AdmissionController):
        async def check(self, request: adm.AdmissionRequest) -> adm.AdmissionDecision:
            raise RuntimeError("quota backend down")

    adm.register_admission_controller(Broken())
    decision = await adm.check_admission(
        adm.AdmissionRequest(kind=adm.ADMISSION_KIND_TURN, user_id="u1")
    )
    assert decision.allowed is True, "a raising controller must fail OPEN (admit)"


def test_register_rejects_non_controller() -> None:
    with pytest.raises(RuntimeError, match="must subclass AdmissionController"):
        adm.register_admission_controller(object())  # type: ignore[arg-type]


async def test_recover_session_is_admission_gated() -> None:
    """Recovery is subject to the same sandbox admission gate as creation.

    Recovery cold-recreates the sandbox, so a user could loop /recover to mint
    sandboxes around a create-time cap. The gate
    must sit in recover_session too — a denying sandbox controller turns
    recovery into the same 429 as create, BEFORE any lifecycle work runs."""
    from unittest.mock import AsyncMock, Mock

    from astrabox.common.utils.user_context import UserContext
    from astrabox.core.service.orchestrator.session_kernel.service_mixins.lifecycle import (
        LifecycleCommandsMixin,
    )

    class DenySandboxes(adm.AdmissionController):
        async def check(self, request: adm.AdmissionRequest) -> adm.AdmissionDecision:
            if request.kind == adm.ADMISSION_KIND_SANDBOX:
                return adm.AdmissionDecision.deny("sandbox cap reached")
            return adm.AdmissionDecision.allow()

    adm.register_admission_controller(DenySandboxes())

    # Plain Mock, not spec'd: the composed service's collaborators
    # (_reconcile_runtime_binding etc.) live on OTHER mixins, and the denial
    # must fire before any of them is reached anyway.
    fake = Mock()
    fake._must_get_owned_session = AsyncMock(
        return_value={"session_id": "sess-1", "agent_id": "dep-1"}
    )

    with pytest.raises(APIError) as exc:
        await LifecycleCommandsMixin.recover_session(
            fake, UserContext(user_id="u1"), "sess-1"
        )
    assert exc.value.status_code == 429
    assert exc.value.code == "ADMISSION_DENIED"
    # Denied BEFORE lifecycle work: nothing may have been reconciled/recreated.
    fake._reconcile_runtime_binding.assert_not_called()
