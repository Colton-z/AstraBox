"""Turn-boundary signal classifiers in ``stream_errors``.

These pure, stateless functions classify exceptions and payloads at the turn
boundary without needing a worker fixture. ``turn_service.py`` re-exports each
one under its underscored name; the final tests require those exports to be the
same objects so both modules use one implementation.

Two edge cases need direct coverage:

* ``is_sandbox_gone_error`` recurses into ``BaseExceptionGroup.exceptions``
  AND chases ``__cause__`` chains, with a self-referential-cause guard
  (``cause is not exc``) that must not infinite-recurse.
* ``extract_dispatch_error_payload`` prefers a generic ``.payload`` dict
  attribute over ``APIError.data``, and falls back to ``.data`` when
  ``.payload`` is present but is not itself a dict.
"""

from __future__ import annotations

from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator import stream_errors, turn_service
from astrabox.core.service.orchestrator.stream_errors import (
    AGENT_CHAT_WAKE_POLL_INTERVAL_SECONDS,
    AGENT_CHAT_WAKE_TIMEOUT_SECONDS,
    RUNTIME_ENSURE_ATTACHED,
    RUNTIME_ENSURE_ATTACH_FAILED,
    RUNTIME_ENSURE_BINDING_MISSING,
    RuntimeEnsureResult,
    dispatch_cancelled_before_claim,
    extract_dispatch_error_payload,
    get_machine_id,
    is_active_sandbox_dispatch_error,
    is_recoverable_turn_attach_error,
    is_sandbox_gone_error,
    normalize_pending_interaction,
    pending_interaction_matches_turn,
)


class _WithPayload(Exception):
    """Mimics a dispatch error carrying a generic `.payload` dict."""

    def __init__(self, payload: Any, message: str = "dispatch error") -> None:
        super().__init__(message)
        self.payload = payload


class _WithCode(Exception):
    """Mimics the typed SANDBOX_GONE error: a bare `.code` attribute."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ── extract_dispatch_error_payload: precedence ───────────────────────────────


def test_payload_attribute_wins_over_api_error_data() -> None:
    exc = APIError(code="X", message="y", status_code=500, data={"source": "data"})
    exc.payload = {"source": "payload"}  # a generic `.payload` also present
    assert extract_dispatch_error_payload(exc) == {"source": "payload"}


def test_non_dict_payload_falls_back_to_api_error_data() -> None:
    exc = APIError(code="X", message="y", status_code=500, data={"source": "data"})
    exc.payload = "not-a-dict"  # present but wrong shape -> must not short-circuit
    assert extract_dispatch_error_payload(exc) == {"source": "data"}


def test_api_error_with_non_dict_data_returns_none() -> None:
    exc = APIError(code="X", message="y", status_code=500, data="not-a-dict")
    assert extract_dispatch_error_payload(exc) is None


def test_plain_exception_with_neither_returns_none() -> None:
    assert extract_dispatch_error_payload(ValueError("boom")) is None


def test_returned_payload_is_a_copy_not_the_original_reference() -> None:
    original = {"code": "X"}
    result = extract_dispatch_error_payload(_WithPayload(original))
    assert result == original
    assert result is not original
    result["mutated"] = True
    assert "mutated" not in original


# ── is_active_sandbox_dispatch_error ──────────────────────────────────────────


def test_active_sandbox_dispatch_error_requires_exact_status_and_message() -> None:
    exc = _WithPayload({"status": "failed", "error": "sandbox turn is still active"})
    assert is_active_sandbox_dispatch_error(exc) is True


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "ok", "error": "sandbox turn is still active"},
        {"status": "failed", "error": "something else"},
        {},
        None,
    ],
)
def test_active_sandbox_dispatch_error_false_otherwise(payload: Any) -> None:
    assert is_active_sandbox_dispatch_error(_WithPayload(payload)) is False


# ── is_recoverable_turn_attach_error ─────────────────────────────────────────


def test_recoverable_turn_attach_error_defers_to_active_dispatch_check_first() -> None:
    # Payload matches BOTH "still active" (which alone forces False) AND the
    # exception message contains a recoverable token (which alone would force
    # True) -- the active-dispatch check must win outright.
    exc = _WithPayload(
        {"status": "failed", "error": "sandbox turn is still active"},
        message="turn transport attach requires dispatch attach_proof",
    )
    assert is_recoverable_turn_attach_error(exc) is False


@pytest.mark.parametrize(
    "code",
    [
        "SIDECAR_GENERATION_UNAVAILABLE",
        "SIDECAR_GENERATION_REVISION_MISMATCH",
        "SIDECAR_GENERATION_DRAINING",
    ],
)
def test_recoverable_turn_attach_error_true_for_known_codes(code: str) -> None:
    assert is_recoverable_turn_attach_error(_WithPayload({"code": code})) is True


def test_recoverable_turn_attach_error_true_for_message_token_case_insensitive() -> None:
    exc = RuntimeError("TURN TRANSPORT ATTACH REQUIRES DISPATCH ATTACH_PROOF")
    assert is_recoverable_turn_attach_error(exc) is True


def test_recoverable_turn_attach_error_false_for_unrelated_error() -> None:
    assert is_recoverable_turn_attach_error(RuntimeError("boom")) is False


def test_recoverable_turn_attach_error_recurses_into_exception_group() -> None:
    group = BaseExceptionGroup(
        "grp",
        [RuntimeError("boom"), RuntimeError("requested sidecar generation is draining")],
    )
    assert is_recoverable_turn_attach_error(group) is True


# ── is_sandbox_gone_error: BaseExceptionGroup recursion + __cause__ chase ────


def test_sandbox_gone_by_direct_code_attribute() -> None:
    assert is_sandbox_gone_error(_WithCode("SANDBOX_GONE")) is True


def test_sandbox_gone_by_payload_code_when_attribute_code_differs() -> None:
    exc = _WithCode("OTHER_CODE")
    exc.payload = {"code": "SANDBOX_GONE"}
    assert is_sandbox_gone_error(exc) is True


def test_sandbox_gone_false_for_unrelated_exception() -> None:
    assert is_sandbox_gone_error(ValueError("boom")) is False


def test_sandbox_gone_chases_the_cause_chain() -> None:
    root = _WithCode("SANDBOX_GONE")
    middle = RuntimeError("wrapped once")
    middle.__cause__ = root
    outer = RuntimeError("wrapped twice")
    outer.__cause__ = middle
    assert is_sandbox_gone_error(outer) is True


def test_sandbox_gone_self_referential_cause_does_not_infinite_recurse() -> None:
    exc = RuntimeError("self-caused")
    exc.__cause__ = exc
    assert is_sandbox_gone_error(exc) is False


def test_sandbox_gone_recurses_into_nested_exception_groups() -> None:
    inner = BaseExceptionGroup("inner", [RuntimeError("noise"), _WithCode("SANDBOX_GONE")])
    outer = BaseExceptionGroup("outer", [ValueError("noise2"), inner])
    assert is_sandbox_gone_error(outer) is True


def test_sandbox_gone_group_false_when_no_member_matches() -> None:
    group = BaseExceptionGroup("grp", [RuntimeError("a"), ValueError("b")])
    assert is_sandbox_gone_error(group) is False


def test_sandbox_gone_group_member_matches_via_its_own_cause() -> None:
    # Recursion (group -> member) and cause-chase (member -> cause) must compose.
    root = _WithCode("SANDBOX_GONE")
    member = RuntimeError("member")
    member.__cause__ = root
    group = BaseExceptionGroup("grp", [RuntimeError("noise"), member])
    assert is_sandbox_gone_error(group) is True


# ── dispatch_cancelled_before_claim ───────────────────────────────────────────


def test_dispatch_cancelled_before_claim_requires_all_four_fields() -> None:
    assert (
        dispatch_cancelled_before_claim(
            {"ok": True, "found": True, "status": "failed", "error": "dispatch cancelled"}
        )
        is True
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"ok": False, "found": True, "status": "failed", "error": "dispatch cancelled"},
        {"ok": True, "found": False, "status": "failed", "error": "dispatch cancelled"},
        {"ok": True, "found": True, "status": "ok", "error": "dispatch cancelled"},
        {"ok": True, "found": True, "status": "failed", "error": "something else"},
        None,
        "not-a-dict",
    ],
)
def test_dispatch_cancelled_before_claim_false_otherwise(payload: Any) -> None:
    assert dispatch_cancelled_before_claim(payload) is False


# ── get_machine_id ────────────────────────────────────────────────────────────


def test_get_machine_id_uses_hostname_env_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOSTNAME", "env-host")
    monkeypatch.setattr("os.getpid", lambda: 4242)
    assert get_machine_id() == "env-host-4242"


def test_get_machine_id_falls_back_to_socket_gethostname(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOSTNAME", raising=False)
    monkeypatch.setattr("socket.gethostname", lambda: "socket-host")
    monkeypatch.setattr("os.getpid", lambda: 99)
    assert get_machine_id() == "socket-host-99"


# ── RuntimeEnsureResult + status constants ────────────────────────────────────


def test_runtime_ensure_result_defaults() -> None:
    result = RuntimeEnsureResult(status=RUNTIME_ENSURE_ATTACHED)
    assert result.status == "ATTACHED"
    assert result.runtime is None
    assert result.error_text is None
    assert result.session is None
    assert result.dispatch_id is None
    assert result.dispatch_payload is None
    assert result.sandbox_gone is False


def test_runtime_ensure_result_full_construction() -> None:
    runtime = object()
    session = {"id": "s1"}
    result = RuntimeEnsureResult(
        status=RUNTIME_ENSURE_ATTACH_FAILED,
        runtime=runtime,
        error_text="down",
        session=session,
        dispatch_id="d1",
        dispatch_payload={"code": "X"},
        sandbox_gone=True,
    )
    assert result.status == "ATTACH_FAILED"
    assert result.runtime is runtime
    assert result.error_text == "down"
    assert result.session is session
    assert result.dispatch_id == "d1"
    assert result.dispatch_payload == {"code": "X"}
    assert result.sandbox_gone is True


def test_runtime_ensure_result_slots_reject_unknown_attributes() -> None:
    result = RuntimeEnsureResult(status=RUNTIME_ENSURE_BINDING_MISSING)
    with pytest.raises(AttributeError):
        result.not_a_real_field = "nope"  # type: ignore[attr-defined]


def test_status_constant_literal_values() -> None:
    assert RUNTIME_ENSURE_ATTACHED == "ATTACHED"
    assert RUNTIME_ENSURE_BINDING_MISSING == "BINDING_MISSING"
    assert RUNTIME_ENSURE_ATTACH_FAILED == "ATTACH_FAILED"
    assert AGENT_CHAT_WAKE_TIMEOUT_SECONDS == 300.0
    assert AGENT_CHAT_WAKE_POLL_INTERVAL_SECONDS == 2.0


# ── normalize_pending_interaction / pending_interaction_matches_turn ─────────


def test_normalize_pending_interaction_requires_id_and_tool_name() -> None:
    assert normalize_pending_interaction("not-a-dict") is None
    assert normalize_pending_interaction({}) is None
    assert normalize_pending_interaction({"interaction_id": "  ", "tool_name": "Bash"}) is None
    assert normalize_pending_interaction({"interaction_id": "i1", "tool_name": ""}) is None


def test_normalize_pending_interaction_returns_a_copy_with_original_values() -> None:
    value = {"interaction_id": "i1", "tool_name": "Bash", "turn_id": "t1"}
    result = normalize_pending_interaction(value)
    assert result == value
    assert result is not value


def test_pending_interaction_matches_turn_compares_as_stripped_strings() -> None:
    value = {"interaction_id": "i1", "tool_name": "Bash", "turn_id": " t1 "}
    assert pending_interaction_matches_turn(value, "t1") is True
    assert pending_interaction_matches_turn(value, "other") is False


def test_pending_interaction_matches_turn_false_when_invalid_shape() -> None:
    assert pending_interaction_matches_turn({"turn_id": "t1"}, "t1") is False


# ── turn_service re-exports the SAME objects (the move's aliasing contract) ──


@pytest.mark.parametrize(
    "public_name,aliased_name",
    [
        ("is_active_sandbox_dispatch_error", "_is_active_sandbox_dispatch_error"),
        ("is_recoverable_turn_attach_error", "_is_recoverable_turn_attach_error"),
        ("is_sandbox_gone_error", "_is_sandbox_gone_error"),
        ("get_machine_id", "_get_machine_id"),
        ("RuntimeEnsureResult", "_RuntimeEnsureResult"),
        ("RUNTIME_ENSURE_ATTACHED", "_RUNTIME_ENSURE_ATTACHED"),
        ("RUNTIME_ENSURE_BINDING_MISSING", "_RUNTIME_ENSURE_BINDING_MISSING"),
        ("RUNTIME_ENSURE_ATTACH_FAILED", "_RUNTIME_ENSURE_ATTACH_FAILED"),
        ("AGENT_CHAT_WAKE_TIMEOUT_SECONDS", "_AGENT_CHAT_WAKE_TIMEOUT_SECONDS"),
        ("AGENT_CHAT_WAKE_POLL_INTERVAL_SECONDS", "_AGENT_CHAT_WAKE_POLL_INTERVAL_SECONDS"),
        ("normalize_pending_interaction", "_normalize_pending_interaction"),
        ("pending_interaction_matches_turn", "_pending_interaction_matches_turn"),
    ],
)
def test_turn_service_reexports_the_same_object(public_name: str, aliased_name: str) -> None:
    assert getattr(turn_service, aliased_name) is getattr(stream_errors, public_name)


def test_dead_symbols_were_deleted_not_relocated() -> None:
    # Removed implementation details must not remain as compatibility aliases.
    for dead_name in (
        "_is_confirmed_dead_sandbox_probe",
        "_SANDBOX_CONFIRMED_DEAD_STATES",
        "_RUNTIME_ENSURE_ATTACH_OWNERSHIP_LOST",
    ):
        assert not hasattr(turn_service, dead_name)
        assert not hasattr(stream_errors, dead_name)
