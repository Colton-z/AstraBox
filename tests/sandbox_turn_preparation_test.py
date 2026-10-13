"""Contract tests for fail-closed sandbox turn preparation."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import FrozenInstanceError, replace
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator import turn_preparation
from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    build_turn_active_snapshot_updates,
)
from astrabox.core.service.orchestrator.turn_preparation import prepared_sandbox_turn
from astrabox.persistence.repository.session_repository import (
    _turn_preparation_quarantine_reason,
)
from astrabox.seams import runtime_preparation as preparation_seam
from astrabox.seams import sandbox as sandbox_seam
from astrabox.seams.runtime_preparation import (
    TURN_PREPARATION_FAILED,
    TURN_PREPARATION_USER_ACTION_REQUIRED,
    PreparationUserActionRequired,
    RuntimeFile,
    RuntimeMaterials,
    RuntimePreparationProvider,
    TurnPreparationContext,
    register_runtime_preparation_provider,
)
from astrabox.seams.sandbox import register_sandbox

_BACKEND = "exclusive-backend"


class _Files:
    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    async def write_bytes(self, path: str, content: bytes, **kwargs: Any) -> None:
        self.writes.append({"path": path, "content": content, **kwargs})


class _TestSandboxHandle:
    def __init__(self, sandbox_id: str) -> None:
        self.sandbox_id = sandbox_id
        self.files = _Files()


class _Backend:
    """The session's sandbox backend, which recognises its own live boxes."""

    def __init__(self, name: str, *, profile_exclusive: bool = True) -> None:
        self.name = name
        self.sandbox_is_profile_exclusive = profile_exclusive

    def owns_sandbox(self, sandbox: Any) -> bool:
        return isinstance(sandbox, _TestSandboxHandle)


class _RecordingProvider(RuntimePreparationProvider):
    def __init__(
        self,
        name: str = "recording-preparation",
        *,
        error: BaseException | None = None,
        materials: RuntimeMaterials | None = None,
    ) -> None:
        self.name = name
        self.error = error
        self.materials = materials if materials is not None else RuntimeMaterials()
        self.contexts: list[TurnPreparationContext] = []

    async def prepare_turn(self, *, context: TurnPreparationContext) -> RuntimeMaterials:
        self.contexts.append(context)
        await asyncio.sleep(0)
        if self.error is not None:
            raise self.error
        return self.materials


class _StartupOnlyProvider(RuntimePreparationProvider):
    name = "startup-only"


class _HungProvider(_RecordingProvider):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.cancelled = False

    async def prepare_turn(self, *, context: TurnPreparationContext) -> RuntimeMaterials:
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

    async def prepare_turn(self, *, context: TurnPreparationContext) -> RuntimeMaterials:
        self.contexts.append(context)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        self.returned.set()
        return RuntimeMaterials()


class _SlotSnapshots:
    """The session's snapshot with ``turn_id`` admitted into its turn slot."""

    def __init__(self, turn_id: str) -> None:
        self.snapshot = {
            "session_id": "session-1",
            **build_turn_active_snapshot_updates(
                conversation_state="PROCESSING",
                turn_id=turn_id,
            ),
        }

    async def get_snapshot(self, session_id: str) -> dict[str, Any] | None:
        if self.snapshot["session_id"] != session_id:
            return None
        return dict(self.snapshot)


class _GuardRepo:
    """The session row's guard with the claim filter of ``SessionRepository``.

    A quarantine stores its reason the way the real repository does.
    """

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
        self.guard.update(
            state="QUARANTINED",
            reason=_turn_preparation_quarantine_reason(reason),
        )
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
    monkeypatch.setattr(preparation_seam, "_PROVIDER", None)
    monkeypatch.setattr(turn_preparation, "_BACKGROUND_CLEANUPS", set())
    register_sandbox(_Backend(_BACKEND))  # type: ignore[arg-type]


def _context(
    *,
    principal_id: str = "principal-1",
    attempt_id: str = "attempt-1",
    sandbox_id: str = "sandbox-1",
) -> TurnPreparationContext:
    return TurnPreparationContext(
        sandbox_backend=_BACKEND,
        sandbox_id=sandbox_id,
        session_id="session-1",
        principal_id=principal_id,
        turn_id="turn-1",
        engine_kind="claude_code",
        dispatch_attempt_id=attempt_id,
        sandbox_handle=_TestSandboxHandle(sandbox_id),
    )


async def _run(
    context: TurnPreparationContext,
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
        }

    if sessions_repo is None:
        sessions_repo = _GuardRepo(
            persisted_session if isinstance(persisted_session, dict) else {}
        )
    async with prepared_sandbox_turn(
        context,
        sessions_repo=sessions_repo,
        session_snapshots_repo=_SlotSnapshots(context.turn_id),
        timeout_seconds=timeout_seconds,
    ):
        pass
    return sessions_repo


@pytest.mark.parametrize(
    "installed", [None, _StartupOnlyProvider()], ids=["no-provider", "startup-only"]
)
async def test_without_turn_preparation_the_write_proceeds_untouched(
    installed: RuntimePreparationProvider | None,
) -> None:
    if installed is not None:
        register_runtime_preparation_provider(installed)
    # Neither the backend, the session row nor the guard is consulted.
    context = replace(_context(principal_id=""), sandbox_backend="unregistered")
    entered = False
    async with prepared_sandbox_turn(
        context, sessions_repo=object(), session_snapshots_repo=object()
    ):
        entered = True
    assert entered


async def test_a_shared_sandbox_backend_is_refused_before_any_effect() -> None:
    register_sandbox(_Backend("shared-backend", profile_exclusive=False))  # type: ignore[arg-type]
    provider = _RecordingProvider()
    register_runtime_preparation_provider(provider)

    with pytest.raises(APIError) as raised:
        async with prepared_sandbox_turn(
            replace(_context(), sandbox_backend="shared-backend"),
            sessions_repo=object(),
            session_snapshots_repo=object(),
        ):
            pytest.fail("a shared sandbox reached the engine write")

    assert raised.value.evidence["reason"] == "unsafe_shared_sandbox"
    assert provider.contexts == []


async def test_turn_files_reach_the_live_box_privately_before_the_write() -> None:
    provider = _RecordingProvider(
        materials=RuntimeMaterials(files=(RuntimeFile("token.json", b"refreshed"),))
    )
    register_runtime_preparation_provider(provider)
    context = replace(
        _context(),
        runtime_identity={
            "linux_user": "workload",
            "home_dir": "/home/workload",
            "workspace_dir": "/home/workload/workspace",
        },
    )
    handle = context.sandbox_handle
    assert isinstance(handle, _TestSandboxHandle)

    async with prepared_sandbox_turn(
        context,
        sessions_repo=_GuardRepo(
            {
                "session_id": context.session_id,
                "user_id": context.principal_id,
                "sandbox_backend": context.sandbox_backend,
                "sandbox_id": context.sandbox_id,
            }
        ),
        session_snapshots_repo=_SlotSnapshots(context.turn_id),
    ):
        assert handle.files.writes == [
            {
                "path": context.file_path("token.json"),
                "content": b"refreshed",
                "mode": 0o600,
                "owner": "workload",
                "group": "workload",
            }
        ]
    assert not context.file_path("token.json").startswith("/home/workload/workspace")


async def test_turn_environment_is_refused_as_a_provider_error() -> None:
    provider = _RecordingProvider(materials=RuntimeMaterials(environment={"TOKEN": "x"}))
    register_runtime_preparation_provider(provider)
    context = _context()
    repo = _GuardRepo(
        {
            "session_id": context.session_id,
            "user_id": context.principal_id,
            "sandbox_backend": context.sandbox_backend,
            "sandbox_id": context.sandbox_id,
        }
    )

    with pytest.raises(APIError) as raised:
        await _run(context, sessions_repo=repo)

    assert raised.value.code == TURN_PREPARATION_FAILED
    assert raised.value.evidence["reason"] == "provider_error"
    assert repo.guard is None


async def test_context_is_immutable_minimal_and_carries_persisted_principal() -> None:
    provider = _RecordingProvider("recording-preparation")
    register_runtime_preparation_provider(provider)
    context = _context()

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
    register_runtime_preparation_provider(provider)
    context = _context(principal_id="principal-1")

    await _run(
        context,
        persisted_session={
            "session_id": context.session_id,
            "user_id": "  principal-1  ",
            "sandbox_backend": context.sandbox_backend,
            "sandbox_id": context.sandbox_id,
        },
    )

    assert provider.contexts[0].principal_id == "principal-1"


async def test_durable_guard_spans_hook_and_engine_write_scope() -> None:
    provider = _RecordingProvider("guard-spans-write")
    register_runtime_preparation_provider(provider)
    first = _context(attempt_id="attempt-a")
    second = _context(attempt_id="attempt-b")
    repo = _GuardRepo(
        {
            "session_id": first.session_id,
            "user_id": first.principal_id,
            "sandbox_backend": first.sandbox_backend,
            "sandbox_id": first.sandbox_id,
        }
    )
    snapshots = _SlotSnapshots(first.turn_id)
    write_scope_entered = asyncio.Event()
    release_write = asyncio.Event()

    async def _first_attempt() -> None:
        async with prepared_sandbox_turn(
            first, sessions_repo=repo, session_snapshots_repo=snapshots
        ):
            write_scope_entered.set()
            await release_write.wait()

    first_task = asyncio.create_task(_first_attempt())
    await write_scope_entered.wait()
    with pytest.raises(APIError) as blocked:
        async with prepared_sandbox_turn(
            second, sessions_repo=repo, session_snapshots_repo=snapshots
        ):
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
    register_runtime_preparation_provider(provider)

    persisted_session = {
        "session_id": "session-1",
        "user_id": persisted_principal,
        "sandbox_backend": _BACKEND,
        "sandbox_id": "sandbox-1",
    }
    with pytest.raises(APIError) as raised:
        await _run(
            _context(principal_id=context_principal),
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
    ],
)
async def test_persisted_binding_mismatch_fails_before_provider_effects(
    field: str,
    persisted_value: str,
    reason: str,
) -> None:
    provider = _RecordingProvider("binding-validation")
    register_runtime_preparation_provider(provider)
    context = _context()
    persisted_session = {
        "session_id": context.session_id,
        "user_id": context.principal_id,
        "sandbox_backend": context.sandbox_backend,
        "sandbox_id": context.sandbox_id,
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
    register_runtime_preparation_provider(provider)
    context = replace(_context(), **context_update)
    repo = _GuardRepo(
        {
            "session_id": context.session_id,
            "user_id": context.principal_id,
            "sandbox_backend": context.sandbox_backend,
            "sandbox_id": context.sandbox_id,
        }
    )

    with pytest.raises(APIError) as raised:
        await _run(context, sessions_repo=repo)

    assert raised.value.evidence["reason"] == reason
    assert provider.contexts == []
    assert repo.guard is None


async def test_session_reload_failure_is_stable_and_secret_safe() -> None:
    provider = _RecordingProvider("session-reload-failure")
    register_runtime_preparation_provider(provider)
    context = _context()

    class _FailingReloadRepo(_GuardRepo):
        async def get_session(self, session_id: str) -> dict[str, Any] | None:
            raise RuntimeError("database credential SECRET-reload-token")

    repo = _FailingReloadRepo(
        {
            "session_id": context.session_id,
            "user_id": context.principal_id,
            "sandbox_backend": context.sandbox_backend,
            "sandbox_id": context.sandbox_id,
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
    register_runtime_preparation_provider(provider)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(APIError) as raised:
            await _run(_context())

    error = raised.value
    assert error.code == TURN_PREPARATION_FAILED
    assert error.debug_message == "provider preparation failed"
    assert "SECRET-token-xyz" not in repr(vars(error))
    assert "SECRET-token-xyz" not in caplog.text
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


async def test_provider_invocation_shape_failure_releases_guard_without_leaking() -> None:
    provider = _RecordingProvider("provider-construction-failure")
    register_runtime_preparation_provider(provider)
    context = _context()
    repo = _GuardRepo(
        {
            "session_id": context.session_id,
            "user_id": context.principal_id,
            "sandbox_backend": context.sandbox_backend,
            "sandbox_id": context.sandbox_id,
        }
    )

    def _malformed_prepare(*, context: TurnPreparationContext) -> None:
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
    register_runtime_preparation_provider(provider)

    with pytest.raises(APIError) as raised:
        await _run(_context(), timeout_seconds=0.01)

    assert raised.value.evidence["reason"] == "timeout"
    await asyncio.wait_for(provider.returned.wait(), timeout=1)


async def test_durable_quarantine_blocks_another_attempt_until_hook_finishes() -> None:
    release = asyncio.Event()
    finished = asyncio.Event()

    class _HoldingProvider(_RecordingProvider):
        async def prepare_turn(self, *, context: TurnPreparationContext) -> RuntimeMaterials:
            self.contexts.append(context)
            if len(self.contexts) > 1:
                return RuntimeMaterials()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            finally:
                finished.set()
            return RuntimeMaterials()

    provider = _HoldingProvider("local-quarantine")
    register_runtime_preparation_provider(provider)
    context = _context()
    repo = _GuardRepo(
        {
            "session_id": context.session_id,
            "user_id": context.principal_id,
            "sandbox_backend": context.sandbox_backend,
            "sandbox_id": context.sandbox_id,
        }
    )
    with pytest.raises(APIError) as first:
        await _run(context, timeout_seconds=0.01, sessions_repo=repo)
    assert first.value.evidence["reason"] == "timeout"
    await asyncio.wait_for(repo.quarantined.wait(), timeout=1)

    with pytest.raises(APIError) as second:
        await _run(
            _context(attempt_id="attempt-2"),
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
        _context(attempt_id="attempt-3"),
        sessions_repo=repo,
    )
    assert len(provider.contexts) == 2


async def test_provider_self_cancel_is_not_caller_cancellation() -> None:
    provider = _RecordingProvider(
        "self-cancel-preparation",
        error=asyncio.CancelledError(),
    )
    register_runtime_preparation_provider(provider)

    with pytest.raises(APIError) as raised:
        await _run(_context())

    assert raised.value.evidence["reason"] == "provider_self_cancelled"


async def test_caller_cancellation_is_preserved_and_hook_is_reaped() -> None:
    provider = _HungProvider("caller-cancel-preparation")
    register_runtime_preparation_provider(provider)
    context = _context()
    repo = _GuardRepo(
        {
            "session_id": context.session_id,
            "user_id": context.principal_id,
            "sandbox_backend": context.sandbox_backend,
            "sandbox_id": context.sandbox_id,
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
    register_runtime_preparation_provider(provider)
    context = _context()
    repo = _GuardRepo(
        {
            "session_id": context.session_id,
            "user_id": context.principal_id,
            "sandbox_backend": context.sandbox_backend,
            "sandbox_id": context.sandbox_id,
        }
    )
    write_started = asyncio.Event()

    async def _write() -> None:
        async with prepared_sandbox_turn(
            context,
            sessions_repo=repo,
            session_snapshots_repo=_SlotSnapshots(context.turn_id),
        ):
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
    register_runtime_preparation_provider(provider)
    context = _context()

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
        }
    )
    wrote = False

    async with prepared_sandbox_turn(
        context,
        sessions_repo=repo,
        session_snapshots_repo=_SlotSnapshots(context.turn_id),
    ):
        wrote = True

    await asyncio.sleep(0)
    assert wrote is True
    assert repo.guard is not None


# ── typed user-action failures + long-turn re-preparation ────────────────────


async def test_user_action_required_surfaces_the_validated_payload() -> None:
    provider = _RecordingProvider(
        "user-action-provider",
        error=PreparationUserActionRequired(
            message="Authorize the agent to act on your behalf",
            url="https://idp.example/confirm?token=abc",
            status="authorization_pending",
        ),
    )
    register_runtime_preparation_provider(provider)
    context = _context()

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
    provider = _RecordingProvider(
        "bad-user-action-provider",
        error=PreparationUserActionRequired(**bad),
    )
    register_runtime_preparation_provider(provider)
    context = _context()

    with pytest.raises(APIError) as raised:
        await _run(context)

    assert raised.value.code == TURN_PREPARATION_FAILED
    assert raised.value.evidence["reason"] == "provider_error"
    assert raised.value.message == "sandbox turn preparation failed"


class _ExpiringCredentialProvider(_RecordingProvider):
    """Succeeds every preparation; optionally starts failing after the first
    (a credential system that revokes mid-turn)."""

    turn_validity_seconds = 0.05

    def __init__(self, name: str, *, fail_after_first: bool = False) -> None:
        super().__init__(name)
        self.fail_after_first = fail_after_first

    async def prepare_turn(self, *, context: TurnPreparationContext) -> RuntimeMaterials:
        self.contexts.append(context)
        await asyncio.sleep(0)
        if self.fail_after_first and len(self.contexts) > 1:
            raise RuntimeError("credential mint revoked")
        return RuntimeMaterials()


async def test_long_write_gets_re_prepared_before_validity_lapses() -> None:
    provider = _ExpiringCredentialProvider("expiring-credential-provider")
    register_runtime_preparation_provider(provider)
    context = _context()
    repo = _GuardRepo(
        {
            "session_id": context.session_id,
            "user_id": "principal-1",
            "sandbox_backend": context.sandbox_backend,
            "sandbox_id": context.sandbox_id,
        }
    )

    async with prepared_sandbox_turn(
        context,
        sessions_repo=repo,
        session_snapshots_repo=_SlotSnapshots(context.turn_id),
        timeout_seconds=1.0,
    ):
        await asyncio.sleep(0.2)  # the engine write outlives the 0.05s validity

    assert len(provider.contexts) >= 2, "re-preparation ran under the live guard"
    assert repo.guard is None, "healthy refreshes release the guard cleanly"


async def test_failed_refresh_quarantines_instead_of_releasing() -> None:
    provider = _ExpiringCredentialProvider(
        "revoking-credential-provider", fail_after_first=True
    )
    register_runtime_preparation_provider(provider)
    context = _context()
    repo = _GuardRepo(
        {
            "session_id": context.session_id,
            "user_id": "principal-1",
            "sandbox_backend": context.sandbox_backend,
            "sandbox_id": context.sandbox_id,
        }
    )

    async with prepared_sandbox_turn(
        context,
        sessions_repo=repo,
        session_snapshots_repo=_SlotSnapshots(context.turn_id),
        timeout_seconds=1.0,
    ):
        await asyncio.sleep(0.2)

    assert repo.guard is not None, "a suspect sandbox is never released cleanly"
    assert repo.guard["state"] == "QUARANTINED"
    assert repo.guard["reason"] == "refresh_failed"


def test_registration_rejects_sync_hooks_and_invalid_validity() -> None:
    class _SyncHook(RuntimePreparationProvider):
        name = "sync-preparation"

        def prepare_turn(self, *, context: TurnPreparationContext) -> RuntimeMaterials:  # type: ignore[override]
            return RuntimeMaterials()

    with pytest.raises(TypeError, match="async preparation methods"):
        register_runtime_preparation_provider(_SyncHook())

    negative = _RecordingProvider("negative-validity")
    negative.turn_validity_seconds = -1.0
    with pytest.raises(TypeError, match="positive number"):
        register_runtime_preparation_provider(negative)
