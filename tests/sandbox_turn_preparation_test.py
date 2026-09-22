"""Contract tests for fail-closed sandbox turn preparation."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import FrozenInstanceError, replace
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator import turn_preparation
from astrabox.core.service.orchestrator.turn_preparation import prepared_sandbox_turn
from astrabox.seams import SEAMS_API_VERSION
from astrabox.seams import sandbox as sandbox_seam
from astrabox.seams.sandbox import (
    TURN_PREPARATION_FAILED,
    SandboxProvider,
    SandboxTurnContext,
    register_sandbox,
)


class _TestSandboxHandle:
    def __init__(self, sandbox_id: str) -> None:
        self.sandbox_id = sandbox_id


class _RecordingProvider:
    supports_turn_preparation = True
    sandbox_is_profile_exclusive = True
    turn_preparation_contract_version = 1

    def __init__(self, name: str, *, error: BaseException | None = None) -> None:
        self.name = name
        self.error = error
        self.contexts: list[SandboxTurnContext] = []

    async def prepare_turn(self, *, context: SandboxTurnContext) -> None:
        self.contexts.append(context)
        await asyncio.sleep(0)
        if self.error is not None:
            raise self.error

    def owns_sandbox(self, sandbox: Any) -> bool:
        return isinstance(sandbox, _TestSandboxHandle)


class _NoPreparationProvider:
    supports_turn_preparation = False
    sandbox_is_profile_exclusive = False

    def __init__(self, name: str) -> None:
        self.name = name


class _LegacyStructuralProvider:
    """Provider shape from before turn-preparation attributes existed."""

    name = "legacy-structural-provider"


class _BadSignatureProvider:
    name = "bad-preparation-signature"
    supports_turn_preparation = True
    sandbox_is_profile_exclusive = True
    turn_preparation_contract_version = 1

    async def prepare_turn(self, *, turn: object) -> None:
        _ = turn

    def owns_sandbox(self, sandbox: Any) -> bool:
        return isinstance(sandbox, _TestSandboxHandle)


class _SyncPreparationProvider:
    name = "sync-preparation"
    supports_turn_preparation = True
    sandbox_is_profile_exclusive = True
    turn_preparation_contract_version = 1

    def prepare_turn(self, *, context: SandboxTurnContext) -> None:
        _ = context

    def owns_sandbox(self, sandbox: Any) -> bool:
        return isinstance(sandbox, _TestSandboxHandle)


class _EnabledWithoutOverrideProvider(SandboxProvider):
    name = "missing-preparation-override"
    supports_turn_preparation = True
    sandbox_is_profile_exclusive = True
    turn_preparation_contract_version = 1

    def owns_sandbox(self, sandbox: Any) -> bool:
        return isinstance(sandbox, _TestSandboxHandle)

    def connection_config(self, **kwargs: Any) -> None:
        _ = kwargs

    def secret_material(self, **kwargs: Any) -> str:
        _ = kwargs
        return "secret"

    def build_dataplane(self, **kwargs: Any) -> Any:
        _ = kwargs
        return object()

    async def connect(self, sandbox_id: str) -> Any:
        return sandbox_id

    async def kill(self, sandbox_id: str) -> bool:
        return bool(sandbox_id)


class _HungProvider(_RecordingProvider):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.cancelled = False

    async def prepare_turn(self, *, context: SandboxTurnContext) -> None:
        self.contexts.append(context)
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.finished.set()


class _SwallowingProvider(_RecordingProvider):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.returned = asyncio.Event()

    async def prepare_turn(self, *, context: SandboxTurnContext) -> None:
        self.contexts.append(context)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        self.returned.set()


class _GuardRepo:
    def __init__(self, row: dict[str, Any]) -> None:
        self.row = dict(row)
        self.guard: dict[str, Any] | None = None
        self.quarantined = asyncio.Event()

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        return dict(self.row) if self.row.get("session_id") == session_id else None

    async def claim_turn_preparation_guard(
        self,
        session_id: str,
        *,
        principal_id: str,
        sandbox_backend: str,
        sandbox_id: str,
        turn_id: str,
        attempt_id: str,
        owner_token: str,
    ) -> dict[str, Any] | None:
        if (
            self.row.get("session_id") != session_id
            or self.row.get("user_id") != principal_id
            or self.row.get("sandbox_backend") != sandbox_backend
            or self.row.get("sandbox_id") != sandbox_id
            or str(self.row.get("current_turn_id") or "") != turn_id
        ):
            return None
        if self.guard is not None and self.guard.get("sandbox_id") == sandbox_id:
            return None
        self.guard = {
            "state": "ACTIVE",
            "sandbox_id": sandbox_id,
            "turn_id": turn_id,
            "attempt_id": attempt_id,
            "owner_token": owner_token,
        }
        return dict(self.guard)

    async def quarantine_turn_preparation_guard(
        self,
        session_id: str,
        *,
        sandbox_id: str,
        attempt_id: str,
        owner_token: str,
        reason: str,
    ) -> bool:
        if not self._matches(
            session_id, sandbox_id, attempt_id, owner_token, "ACTIVE"
        ):
            return False
        assert self.guard is not None
        self.guard.update(state="QUARANTINED", reason=reason)
        self.quarantined.set()
        return True

    async def release_turn_preparation_guard(
        self,
        session_id: str,
        *,
        sandbox_id: str,
        attempt_id: str,
        owner_token: str,
        expected_state: str,
    ) -> bool:
        if not self._matches(
            session_id, sandbox_id, attempt_id, owner_token, expected_state
        ):
            return False
        self.guard = None
        return True

    def _matches(
        self,
        session_id: str,
        sandbox_id: str,
        attempt_id: str,
        owner_token: str,
        state: str,
    ) -> bool:
        return bool(
            self.row.get("session_id") == session_id
            and self.guard is not None
            and self.guard.get("sandbox_id") == sandbox_id
            and self.guard.get("attempt_id") == attempt_id
            and self.guard.get("owner_token") == owner_token
            and self.guard.get("state") == state
        )


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox_seam, "_BACKENDS", dict(sandbox_seam._BACKENDS))
    monkeypatch.setattr(turn_preparation, "_BACKGROUND_CLEANUPS", set())


def _context(
    backend: str,
    *,
    principal_id: str = "principal-1",
    attempt_id: str = "attempt-1",
    sandbox_id: str = "sandbox-1",
) -> SandboxTurnContext:
    return SandboxTurnContext(
        sandbox_backend=backend,
        sandbox_id=sandbox_id,
        session_id="session-1",
        principal_id=principal_id,
        turn_id="turn-1",
        engine_kind="claude_code",
        dispatch_attempt_id=attempt_id,
        sandbox_handle=_TestSandboxHandle(sandbox_id),
    )


async def _run(
    context: SandboxTurnContext,
    *,
    persisted_session: object = None,
    timeout_seconds: float = 1.0,
    sessions_repo: _GuardRepo | None = None,
) -> _GuardRepo:
    if persisted_session is None:
        persisted_session = {
            "session_id": context.session_id,
            "user_id": "principal-1",
            "sandbox_backend": context.sandbox_backend,
            "sandbox_id": context.sandbox_id,
            "current_turn_id": context.turn_id,
        }

    if sessions_repo is None:
        sessions_repo = _GuardRepo(
            persisted_session if isinstance(persisted_session, dict) else {}
        )
    async with prepared_sandbox_turn(
        context,
        sessions_repo=sessions_repo,
        timeout_seconds=timeout_seconds,
    ):
        pass
    return sessions_repo


def test_registration_rejects_unsafe_or_invalid_capabilities() -> None:
    unversioned = _RecordingProvider("unversioned-preparation")
    unversioned.turn_preparation_contract_version = None
    with pytest.raises(RuntimeError, match="contract version"):
        register_sandbox(unversioned)  # type: ignore[arg-type]
    no_ownership = _RecordingProvider("missing-live-ownership")
    no_ownership.owns_sandbox = None  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="ownership validation"):
        register_sandbox(no_ownership)  # type: ignore[arg-type]
    shared = _RecordingProvider("unsafe-shared")
    shared.sandbox_is_profile_exclusive = False
    with pytest.raises(RuntimeError, match="shared sandbox"):
        register_sandbox(shared)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="signature"):
        register_sandbox(_BadSignatureProvider())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="must be async"):
        register_sandbox(_SyncPreparationProvider())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="does not override"):
        register_sandbox(_EnabledWithoutOverrideProvider())


def test_profile_exclusive_capability_is_additive_without_api_bump() -> None:
    provider = _RecordingProvider("exclusive-additive")
    register_sandbox(provider)  # type: ignore[arg-type]
    # Pins the tree's current contract version: turn preparation is additive and
    # never moved it; later breaking seam changes advanced the shared version.
    assert SEAMS_API_VERSION == 4
    assert sandbox_seam.sandbox_for_name(provider.name) is provider


async def test_default_provider_is_a_noop_for_legacy_context() -> None:
    provider = _NoPreparationProvider("no-preparation")
    register_sandbox(provider)  # type: ignore[arg-type]
    await _run(
        _context(provider.name, principal_id=""),
        persisted_session={},
    )


def test_pre_change_structural_provider_registers_without_new_attributes() -> None:
    provider = _LegacyStructuralProvider()

    register_sandbox(provider)  # type: ignore[arg-type]

    assert sandbox_seam.sandbox_for_name(provider.name) is provider


async def test_context_is_immutable_minimal_and_carries_persisted_principal() -> None:
    provider = _RecordingProvider("recording-preparation")
    register_sandbox(provider)  # type: ignore[arg-type]
    context = _context(provider.name)

    await _run(context)

    assert provider.contexts == [context]
    assert provider.contexts[0].principal_id == "principal-1"
    assert not hasattr(provider.contexts[0], "session")
    assert "principal-1" not in repr(context)
    assert "sandbox_handle" not in repr(context)
    assert not hasattr(context, "dataplane")
    with pytest.raises(FrozenInstanceError):
        context.turn_id = "changed"  # type: ignore[misc]


async def test_provider_receives_canonical_principal_but_claim_matches_raw_owner() -> None:
    provider = _RecordingProvider("canonical-principal")
    register_sandbox(provider)  # type: ignore[arg-type]
    context = _context(provider.name, principal_id="principal-1")

    await _run(
        context,
        persisted_session={
            "session_id": context.session_id,
            "user_id": "  principal-1  ",
            "sandbox_backend": context.sandbox_backend,
            "sandbox_id": context.sandbox_id,
            "current_turn_id": context.turn_id,
        },
    )

    assert provider.contexts[0].principal_id == "principal-1"


async def test_durable_guard_spans_hook_and_engine_write_scope() -> None:
    provider = _RecordingProvider("guard-spans-write")
    register_sandbox(provider)  # type: ignore[arg-type]
    first = _context(provider.name, attempt_id="attempt-a")
    second = _context(provider.name, attempt_id="attempt-b")
    repo = _GuardRepo(
        {
            "session_id": first.session_id,
            "user_id": first.principal_id,
            "sandbox_backend": first.sandbox_backend,
            "sandbox_id": first.sandbox_id,
            "current_turn_id": first.turn_id,
        }
    )
    write_scope_entered = asyncio.Event()
    release_write = asyncio.Event()

    async def _first_attempt() -> None:
        async with prepared_sandbox_turn(first, sessions_repo=repo):
            write_scope_entered.set()
            await release_write.wait()

    first_task = asyncio.create_task(_first_attempt())
    await write_scope_entered.wait()
    with pytest.raises(APIError) as blocked:
        async with prepared_sandbox_turn(second, sessions_repo=repo):
            pytest.fail("a second replica entered the guarded write scope")
    assert blocked.value.evidence["reason"] == "guard_conflict"
    assert [item.dispatch_attempt_id for item in provider.contexts] == ["attempt-a"]

    release_write.set()
    await first_task
    assert repo.guard is None


@pytest.mark.parametrize(
    ("context_principal", "persisted_principal", "reason"),
    [
        ("", "principal-1", "principal_missing"),
        ("principal-1", None, "principal_missing"),
        ("request-actor", "principal-1", "principal_mismatch"),
    ],
)
async def test_principal_is_validated_before_provider_effects(
    context_principal: str,
    persisted_principal: object,
    reason: str,
) -> None:
    provider = _RecordingProvider("principal-validation")
    register_sandbox(provider)  # type: ignore[arg-type]

    persisted_session = {
        "session_id": "session-1",
        "user_id": persisted_principal,
            "sandbox_backend": provider.name,
            "sandbox_id": "sandbox-1",
            "current_turn_id": "turn-1",
    }
    with pytest.raises(APIError) as raised:
        await _run(
            _context(provider.name, principal_id=context_principal),
            persisted_session=persisted_session,
        )

    assert raised.value.code == TURN_PREPARATION_FAILED
    assert raised.value.evidence["reason"] == reason
    assert provider.contexts == []


@pytest.mark.parametrize(
    ("field", "persisted_value", "reason"),
    [
        ("session_id", "other-session", "session_missing"),
        ("sandbox_backend", "other-backend", "backend_mismatch"),
        ("sandbox_id", "other-sandbox", "sandbox_mismatch"),
        ("current_turn_id", "other-turn", "turn_owner_mismatch"),
    ],
)
async def test_persisted_binding_and_turn_mismatch_fail_before_provider_effects(
    field: str,
    persisted_value: str,
    reason: str,
) -> None:
    provider = _RecordingProvider("binding-validation")
    register_sandbox(provider)  # type: ignore[arg-type]
    context = _context(provider.name)
    persisted_session = {
        "session_id": context.session_id,
        "user_id": context.principal_id,
        "sandbox_backend": context.sandbox_backend,
        "sandbox_id": context.sandbox_id,
        "current_turn_id": context.turn_id,
        field: persisted_value,
    }

    with pytest.raises(APIError) as raised:
        await _run(context, persisted_session=persisted_session)

    assert raised.value.evidence["reason"] == reason
    assert provider.contexts == []


@pytest.mark.parametrize(
    ("context_update", "reason"),
    [
        (
            {"sandbox_handle": _TestSandboxHandle("other-sandbox")},
            "live_handle_mismatch",
        ),
        (
            {"sandbox_handle": type("ForeignHandle", (), {"sandbox_id": "sandbox-1"})()},
            "live_handle_backend_mismatch",
        ),
    ],
)
async def test_unverified_live_objects_fail_before_guard_or_provider_effects(
    context_update: dict[str, Any],
    reason: str,
) -> None:
    provider = _RecordingProvider("live-binding-validation")
    register_sandbox(provider)  # type: ignore[arg-type]
    context = replace(_context(provider.name), **context_update)
    repo = _GuardRepo(
        {
            "session_id": context.session_id,
            "user_id": context.principal_id,
            "sandbox_backend": context.sandbox_backend,
            "sandbox_id": context.sandbox_id,
            "current_turn_id": context.turn_id,
        }
    )

    with pytest.raises(APIError) as raised:
        await _run(context, sessions_repo=repo)

    assert raised.value.evidence["reason"] == reason
    assert provider.contexts == []
    assert repo.guard is None


async def test_session_reload_failure_is_stable_and_secret_safe() -> None:
    provider = _RecordingProvider("session-reload-failure")
    register_sandbox(provider)  # type: ignore[arg-type]
    context = _context(provider.name)

    class _FailingReloadRepo(_GuardRepo):
        async def get_session(self, session_id: str) -> dict[str, Any] | None:
            raise RuntimeError("database credential SECRET-reload-token")

    repo = _FailingReloadRepo(
        {
            "session_id": context.session_id,
            "user_id": context.principal_id,
            "sandbox_backend": context.sandbox_backend,
            "sandbox_id": context.sandbox_id,
            "current_turn_id": context.turn_id,
        }
    )
    with pytest.raises(APIError) as raised:
        await _run(context, sessions_repo=repo)

    assert raised.value.evidence["reason"] == "session_reload_failed"
    assert "SECRET-reload-token" not in repr(vars(raised.value))
    assert provider.contexts == []


async def test_provider_failure_is_secret_safe(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "credential SECRET-token-xyz must not cross the wire"
    provider = _RecordingProvider(
        "failing-preparation",
        error=RuntimeError(secret),
    )
    register_sandbox(provider)  # type: ignore[arg-type]

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(APIError) as raised:
            await _run(_context(provider.name))

    error = raised.value
    assert error.code == TURN_PREPARATION_FAILED
    assert error.debug_message == "provider preparation failed"
    assert "SECRET-token-xyz" not in repr(vars(error))
    assert "SECRET-token-xyz" not in caplog.text
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


async def test_provider_invocation_shape_failure_releases_guard_without_leaking() -> None:
    provider = _RecordingProvider("provider-construction-failure")
    register_sandbox(provider)  # type: ignore[arg-type]
    context = _context(provider.name)
    repo = _GuardRepo(
        {
            "session_id": context.session_id,
            "user_id": context.principal_id,
            "sandbox_backend": context.sandbox_backend,
            "sandbox_id": context.sandbox_id,
            "current_turn_id": context.turn_id,
        }
    )

    def _malformed_prepare(*, context: SandboxTurnContext) -> None:
        _ = context
        raise TypeError("provider credential SECRET-construction-token")

    provider.prepare_turn = _malformed_prepare  # type: ignore[method-assign]
    with pytest.raises(APIError) as raised:
        await _run(context, sessions_repo=repo)

    assert raised.value.evidence["reason"] == "provider_error"
    assert "SECRET-construction-token" not in repr(vars(raised.value))
    assert repo.guard is None


async def test_host_clock_timeout_fails_before_late_provider_completion() -> None:
    provider = _SwallowingProvider("swallowing-preparation")
    register_sandbox(provider)  # type: ignore[arg-type]

    with pytest.raises(APIError) as raised:
        await _run(_context(provider.name), timeout_seconds=0.01)

    assert raised.value.evidence["reason"] == "timeout"
    await asyncio.wait_for(provider.returned.wait(), timeout=1)


async def test_durable_quarantine_blocks_another_attempt_until_hook_finishes() -> None:
    release = asyncio.Event()
    finished = asyncio.Event()

    class _HoldingProvider(_RecordingProvider):
        async def prepare_turn(self, *, context: SandboxTurnContext) -> None:
            self.contexts.append(context)
            if len(self.contexts) > 1:
                return
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            finally:
                finished.set()

    provider = _HoldingProvider("local-quarantine")
    register_sandbox(provider)  # type: ignore[arg-type]
    context = _context(provider.name)
    repo = _GuardRepo(
        {
            "session_id": context.session_id,
            "user_id": context.principal_id,
            "sandbox_backend": context.sandbox_backend,
            "sandbox_id": context.sandbox_id,
            "current_turn_id": context.turn_id,
        }
    )
    with pytest.raises(APIError) as first:
        await _run(context, timeout_seconds=0.01, sessions_repo=repo)
    assert first.value.evidence["reason"] == "timeout"
    await asyncio.wait_for(repo.quarantined.wait(), timeout=1)

    with pytest.raises(APIError) as second:
        await _run(
            _context(provider.name, attempt_id="attempt-2"),
            timeout_seconds=0.5,
            sessions_repo=repo,
        )
    assert second.value.evidence["reason"] == "guard_conflict"
    assert len(provider.contexts) == 1

    release.set()
    await asyncio.wait_for(finished.wait(), timeout=1)
    for _ in range(20):
        if repo.guard is None:
            break
        await asyncio.sleep(0.01)
    assert repo.guard is None
    await _run(
        _context(provider.name, attempt_id="attempt-3"),
        sessions_repo=repo,
    )
    assert len(provider.contexts) == 2


async def test_provider_self_cancel_is_not_caller_cancellation() -> None:
    provider = _RecordingProvider(
        "self-cancel-preparation",
        error=asyncio.CancelledError(),
    )
    register_sandbox(provider)  # type: ignore[arg-type]

    with pytest.raises(APIError) as raised:
        await _run(_context(provider.name))

    assert raised.value.evidence["reason"] == "provider_self_cancelled"


async def test_caller_cancellation_is_preserved_and_hook_is_reaped() -> None:
    provider = _HungProvider("caller-cancel-preparation")
    register_sandbox(provider)  # type: ignore[arg-type]
    context = _context(provider.name)
    repo = _GuardRepo(
        {
            "session_id": context.session_id,
            "user_id": context.principal_id,
            "sandbox_backend": context.sandbox_backend,
            "sandbox_id": context.sandbox_id,
            "current_turn_id": context.turn_id,
        }
    )
    task = asyncio.create_task(_run(context, sessions_repo=repo))
    await provider.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.wait_for(provider.finished.wait(), timeout=1)
    assert provider.cancelled is True
    for _ in range(20):
        if repo.guard is None:
            break
        await asyncio.sleep(0.01)
    assert repo.guard is None


async def test_caller_cancellation_during_engine_write_releases_exact_guard() -> None:
    provider = _RecordingProvider("caller-cancel-during-write")
    register_sandbox(provider)  # type: ignore[arg-type]
    context = _context(provider.name)
    repo = _GuardRepo(
        {
            "session_id": context.session_id,
            "user_id": context.principal_id,
            "sandbox_backend": context.sandbox_backend,
            "sandbox_id": context.sandbox_id,
            "current_turn_id": context.turn_id,
        }
    )
    write_started = asyncio.Event()

    async def _write() -> None:
        async with prepared_sandbox_turn(context, sessions_repo=repo):
            write_started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(_write())
    await write_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert repo.guard is None
    assert provider.contexts == [context]


async def test_release_child_self_cancel_does_not_cancel_successful_write() -> None:
    provider = _RecordingProvider("release-self-cancel")
    register_sandbox(provider)  # type: ignore[arg-type]
    context = _context(provider.name)

    class _SelfCancellingReleaseRepo(_GuardRepo):
        async def release_turn_preparation_guard(
            self,
            session_id: str,
            **kwargs: Any,
        ) -> bool:
            raise asyncio.CancelledError

    repo = _SelfCancellingReleaseRepo(
        {
            "session_id": context.session_id,
            "user_id": context.principal_id,
            "sandbox_backend": context.sandbox_backend,
            "sandbox_id": context.sandbox_id,
            "current_turn_id": context.turn_id,
        }
    )
    wrote = False

    async with prepared_sandbox_turn(context, sessions_repo=repo):
        wrote = True

    await asyncio.sleep(0)
    assert wrote is True
    assert repo.guard is not None


# ── typed user-action failures + long-turn re-preparation ────────────────────


async def test_user_action_required_surfaces_the_validated_payload() -> None:
    from astrabox.seams.sandbox import (
        TURN_PREPARATION_USER_ACTION_REQUIRED,
        TurnPreparationUserActionRequired,
    )

    provider = _RecordingProvider(
        "user-action-provider",
        error=TurnPreparationUserActionRequired(
            message="Authorize the agent to act on your behalf",
            url="https://idp.example/confirm?token=abc",
            status="authorization_pending",
        ),
    )
    register_sandbox(provider)  # type: ignore[arg-type]
    context = _context("user-action-provider")

    with pytest.raises(APIError) as raised:
        repo = await _run(context)

    assert raised.value.code == TURN_PREPARATION_USER_ACTION_REQUIRED
    assert raised.value.status_code == 409, "user action, not an infrastructure fault"
    assert raised.value.message == "Authorize the agent to act on your behalf"
    user_action = raised.value.evidence["user_action"]
    assert user_action["url"] == "https://idp.example/confirm?token=abc"
    assert user_action["status"] == "authorization_pending"


@pytest.mark.parametrize(
    "bad",
    [
        dict(message="visit\x00this", url="https://x.example"),          # control chars
        dict(message="ok", url="javascript:alert(1)"),                    # non-http url
        dict(message="x" * 501, url=None),                                # oversized
        dict(message="", url="https://x.example"),                        # empty message
    ],
)
async def test_invalid_user_action_payload_stays_swallowed(bad: dict[str, Any]) -> None:
    """The escape hatch never widens: a payload that fails validation gets
    the generic swallowed failure, exactly like any other provider error."""
    from astrabox.seams.sandbox import TurnPreparationUserActionRequired

    provider = _RecordingProvider(
        "bad-user-action-provider",
        error=TurnPreparationUserActionRequired(**bad),
    )
    register_sandbox(provider)  # type: ignore[arg-type]
    context = _context("bad-user-action-provider")

    with pytest.raises(APIError) as raised:
        await _run(context)

    assert raised.value.code == TURN_PREPARATION_FAILED
    assert raised.value.evidence["reason"] == "provider_error"
    assert raised.value.message == "sandbox turn preparation failed"


class _ExpiringCredentialProvider(_RecordingProvider):
    """Succeeds every preparation; optionally starts failing after the first
    (a credential system that revokes mid-turn)."""

    turn_preparation_validity_seconds = 0.05

    def __init__(self, name: str, *, fail_after_first: bool = False) -> None:
        super().__init__(name)
        self.fail_after_first = fail_after_first

    async def prepare_turn(self, *, context: SandboxTurnContext) -> None:
        self.contexts.append(context)
        await asyncio.sleep(0)
        if self.fail_after_first and len(self.contexts) > 1:
            raise RuntimeError("credential mint revoked")


async def test_long_write_gets_re_prepared_before_validity_lapses() -> None:
    provider = _ExpiringCredentialProvider("expiring-credential-provider")
    register_sandbox(provider)  # type: ignore[arg-type]
    context = _context("expiring-credential-provider")
    repo = _GuardRepo(
        {
            "session_id": context.session_id,
            "user_id": "principal-1",
            "sandbox_backend": context.sandbox_backend,
            "sandbox_id": context.sandbox_id,
            "current_turn_id": context.turn_id,
        }
    )

    async with prepared_sandbox_turn(context, sessions_repo=repo, timeout_seconds=1.0):
        await asyncio.sleep(0.2)  # the engine write outlives the 0.05s validity

    assert len(provider.contexts) >= 2, "re-preparation ran under the live guard"
    assert repo.guard is None, "healthy refreshes release the guard cleanly"


async def test_failed_refresh_quarantines_instead_of_releasing() -> None:
    provider = _ExpiringCredentialProvider(
        "revoking-credential-provider", fail_after_first=True
    )
    register_sandbox(provider)  # type: ignore[arg-type]
    context = _context("revoking-credential-provider")
    repo = _GuardRepo(
        {
            "session_id": context.session_id,
            "user_id": "principal-1",
            "sandbox_backend": context.sandbox_backend,
            "sandbox_id": context.sandbox_id,
            "current_turn_id": context.turn_id,
        }
    )

    async with prepared_sandbox_turn(context, sessions_repo=repo, timeout_seconds=1.0):
        await asyncio.sleep(0.2)

    assert repo.guard is not None, "a suspect sandbox is never released cleanly"
    assert repo.guard["state"] == "QUARANTINED"
    assert repo.guard["reason"] == "refresh_failed"


def test_registration_rejects_invalid_validity_declarations() -> None:
    provider = _RecordingProvider("validity-without-capability")
    provider.supports_turn_preparation = False  # type: ignore[misc]
    provider.turn_preparation_validity_seconds = 60.0  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="without enabling"):
        register_sandbox(provider)  # type: ignore[arg-type]

    negative = _RecordingProvider("negative-validity")
    negative.turn_preparation_validity_seconds = -1.0  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="positive number"):
        register_sandbox(negative)  # type: ignore[arg-type]
