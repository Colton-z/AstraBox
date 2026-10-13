"""Durably fenced orchestration for sandbox turn preparation.

The public provider surface lives in :mod:`astrabox.seams.runtime_preparation`.  Core owns
the effect boundary: reload the durable session and its turn slot, claim a
no-TTL attempt guard and confirm the turn still holds the slot, run the
untrusted hook under a host-clock deadline, keep the guard across the engine
write, and release only by exact attempt CAS.

The guard is stored on the session row beside the sandbox binding and the
principal it protects; which turn is running is recorded only in the session's
snapshot (``session_snapshots.current_turn_id``). The repository backends
compare-and-set one document at a time, so the claim is atomic with the
binding and the principal, and the turn is read twice: before the claim, and
again while the guard is held. A claim fills only an empty guard slot or one
naming a sandbox other than the session's current binding, so a claimant whose
turn ended between the two reads displaced no current owner and releases its
own guard. A claimant that finds its turn in the slot during the hold held the
guard while that turn was running.

The capability is limited to profile-exclusive sandboxes. Shared sandboxes do
not provide the required sandbox-keyed distributed fence or immutable
attempt-scoped egress identity; a process-local lock is not a valid substitute.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    snapshot_active_turn_id,
)
from astrabox.seams.runtime_preparation import (
    TURN_PREPARATION_FAILED,
    TURN_PREPARATION_USER_ACTION_REQUIRED,
    PreparationUserActionRequired,
    RuntimeMaterials,
    TurnPreparationContext,
    prepares_turns,
    runtime_preparation_provider,
    validated_user_action,
)
from astrabox.seams.sandbox import sandbox_for_name

logger = get_logger(__name__)

TURN_PREPARATION_TIMEOUT_SECONDS = 30.0

#: Re-preparation runs at this fraction of the provider-declared validity —
#: early enough that a slow refresh (bounded by the same host-clock deadline)
#: still lands before the credential lapses.
TURN_PREPARATION_REFRESH_FRACTION = 0.8
# Cleanup tasks retain the provider task and repository operation after the
# caller has returned.  A process crash leaves the durable guard in place, so
# another replica fails closed instead of time-stealing it.
_BACKGROUND_CLEANUPS: set[asyncio.Task[Any]] = set()


def _failure(
    context: TurnPreparationContext,
    *,
    reason: str,
    detail: str,
) -> APIError:
    return APIError(
        code=TURN_PREPARATION_FAILED,
        message="sandbox turn preparation failed",
        status_code=502,
        debug_message=detail,
        evidence={
            "reason": reason,
            "sandbox_backend": context.sandbox_backend,
            "sandbox_id": context.sandbox_id,
            "session_id": context.session_id,
            "turn_id": context.turn_id,
            "engine_kind": context.engine_kind,
            "dispatch_attempt_id": context.dispatch_attempt_id,
        },
    )


def _turn_owner_mismatch(context: TurnPreparationContext) -> APIError:
    return _failure(
        context,
        reason="turn_owner_mismatch",
        detail="persisted active turn does not match turn context",
    )


def sandbox_turn_context(
    *,
    session: dict[str, Any],
    session_id: str,
    runtime: Any,
    turn_id: str,
) -> TurnPreparationContext:
    """Describe one engine write to ``runtime`` on behalf of ``turn_id``.

    The backend and the principal come from the persisted session, the sandbox
    from the runtime the write goes to (the session's binding when the runtime
    names none), and every write gets a fresh dispatch attempt id.
    """
    principal_id = session.get("user_id")
    return TurnPreparationContext(
        sandbox_backend=str(session.get("sandbox_backend") or "").strip().lower(),
        sandbox_id=(
            str(getattr(runtime, "sandbox_id", "") or "").strip()
            or str(session.get("sandbox_id") or "").strip()
        ),
        session_id=session_id,
        principal_id=principal_id.strip() if isinstance(principal_id, str) else "",
        turn_id=turn_id,
        engine_kind=str(getattr(runtime, "engine_kind", "") or "").strip().lower(),
        dispatch_attempt_id=str(uuid.uuid4()),
        runtime_identity=dict(getattr(runtime, "runtime_identity", None) or {}),
        sandbox_handle=getattr(runtime, "sandbox", None),
    )


def _validated_user_action(
    exc: PreparationUserActionRequired,
) -> dict[str, Any] | None:
    return validated_user_action({
        "message": getattr(exc, "message", None),
        "status": getattr(exc, "status", "user_action_required"),
        "url": getattr(exc, "url", None),
    })


def _user_action_failure(
    context: TurnPreparationContext,
    user_action: dict[str, Any],
) -> APIError:
    """A distinct, user-visible, machine-non-retryable preparation outcome.

    409 (not 502): the platform is healthy and the session recoverable — the
    turn is blocked on the user completing the provider's flow (e.g. visiting
    an authorization URL), after which a retry succeeds.
    """
    return APIError(
        code=TURN_PREPARATION_USER_ACTION_REQUIRED,
        message=str(user_action["message"]),
        status_code=409,
        debug_message="provider preparation requires user action",
        evidence={
            "reason": "user_action_required",
            "user_action": dict(user_action),
            "sandbox_backend": context.sandbox_backend,
            "sandbox_id": context.sandbox_id,
            "session_id": context.session_id,
            "turn_id": context.turn_id,
            "engine_kind": context.engine_kind,
            "dispatch_attempt_id": context.dispatch_attempt_id,
        },
    )


def _validate_context(
    context: TurnPreparationContext,
    *,
    persisted_session: object,
    active_turn_id: str | None,
) -> str:
    """Check the context against the session row and the snapshot's turn slot.

    ``active_turn_id`` is :func:`snapshot_active_turn_id` of the session's
    snapshot. Returns the raw stored principal for the guard's claim filter.
    """
    identifiers = {
        "sandbox_backend": context.sandbox_backend,
        "sandbox_id": context.sandbox_id,
        "session_id": context.session_id,
        "turn_id": context.turn_id,
        "engine_kind": context.engine_kind,
        "dispatch_attempt_id": context.dispatch_attempt_id,
    }
    missing = [
        name
        for name, value in identifiers.items()
        if not isinstance(value, str) or not value.strip()
    ]
    if missing:
        raise _failure(
            context,
            reason="incomplete_context",
            detail="persisted turn context is incomplete",
        )
    if not isinstance(persisted_session, dict):
        raise _failure(
            context,
            reason="session_missing",
            detail="persisted session row is unavailable",
        )
    persisted_session_id = persisted_session.get("session_id")
    persisted_principal_id = persisted_session.get("user_id")
    persisted_backend = str(
        persisted_session.get("sandbox_backend") or ""
    ).strip().lower()
    persisted_sandbox_id = str(
        persisted_session.get("sandbox_id") or ""
    ).strip()
    if persisted_session_id != context.session_id:
        raise _failure(
            context,
            reason="session_mismatch",
            detail="persisted session identity does not match turn context",
        )
    if persisted_backend != context.sandbox_backend:
        raise _failure(
            context,
            reason="backend_mismatch",
            detail="persisted sandbox backend does not match turn context",
        )
    if persisted_sandbox_id != context.sandbox_id:
        raise _failure(
            context,
            reason="sandbox_mismatch",
            detail="persisted sandbox binding does not match turn context",
        )
    if active_turn_id != context.turn_id:
        raise _turn_owner_mismatch(context)
    if (
        not isinstance(persisted_principal_id, str)
        or not persisted_principal_id.strip()
        or not isinstance(context.principal_id, str)
        or not context.principal_id.strip()
    ):
        raise _failure(
            context,
            reason="principal_missing",
            detail="persisted session principal is missing or invalid",
        )
    if context.principal_id != persisted_principal_id.strip():
        raise _failure(
            context,
            reason="principal_mismatch",
            detail="turn context principal does not match persisted session owner",
        )
    # Return the raw stored value for the atomic repository filter.  Providers
    # receive only the canonical, stripped context value above.
    return persisted_principal_id


def _validated_live_context(
    provider: Any,
    context: TurnPreparationContext,
) -> TurnPreparationContext:
    """Bind optional live objects to the already-validated durable identity."""
    handle = context.sandbox_handle
    if handle is None:
        return context

    try:
        underlying = getattr(handle, "sandbox", None)
        if underlying is None or underlying is handle:
            underlying = handle
        live_sandbox_id = ""
        for attribute in ("code_interpreter_id", "sandbox_id", "id"):
            value = getattr(underlying, attribute, None)
            if value:
                live_sandbox_id = str(value).strip()
                break
    except Exception:
        raise _failure(
            context,
            reason="live_handle_invalid",
            detail="live sandbox identity could not be validated",
        ) from None
    if live_sandbox_id != context.sandbox_id:
        raise _failure(
            context,
            reason="live_handle_mismatch",
            detail="live sandbox identity does not match persisted binding",
        )
    try:
        owns_sandbox = getattr(provider, "owns_sandbox", None)
        provider_owns_handle = bool(
            callable(owns_sandbox) and owns_sandbox(underlying)
        )
    except Exception:
        provider_owns_handle = False
    if not provider_owns_handle:
        raise _failure(
            context,
            reason="live_handle_backend_mismatch",
            detail="persisted sandbox backend does not own the live handle",
        )
    return replace(context, sandbox_handle=underlying)


def _track_background(task: asyncio.Task[Any], *, label: str) -> None:
    _BACKGROUND_CLEANUPS.add(task)

    def _done(done: asyncio.Task[Any]) -> None:
        _BACKGROUND_CLEANUPS.discard(done)
        if done.cancelled():
            return
        error = done.exception()
        if error is not None:
            logger.error("turn preparation background cleanup failed label=%s", label)

    task.add_done_callback(_done)


async def _release_active_guard(
    sessions_repo: Any,
    context: TurnPreparationContext,
    *,
    owner_token: str,
) -> bool:
    async def _release() -> bool:
        return bool(
            await sessions_repo.release_turn_preparation_guard(
                context.session_id,
                sandbox_id=context.sandbox_id,
                attempt_id=context.dispatch_attempt_id,
                owner_token=owner_token,
                expected_state="ACTIVE",
            )
        )

    task = asyncio.create_task(
        _release(),
        name=f"turn-prepare-release:{context.session_id}:{context.dispatch_attempt_id}",
    )
    _track_background(task, label="release-active")
    try:
        return bool(await asyncio.shield(task))
    except asyncio.CancelledError:
        caller = asyncio.current_task()
        if caller is not None and caller.cancelling():
            # The repository task keeps running under shield and retains its
            # strong reference; genuine caller cancellation remains cancellation.
            raise
        # The child repository operation cancelled itself.  That is an
        # unconfirmed release, not cancellation of the engine-write caller.
        logger.error(
            "turn preparation active guard release cancelled session=%s "
            "sandbox=%s attempt=%s",
            context.session_id,
            context.sandbox_id,
            context.dispatch_attempt_id,
        )
        return False
    except Exception:
        logger.error(
            "turn preparation active guard release failed session=%s sandbox=%s "
            "attempt=%s",
            context.session_id,
            context.sandbox_id,
            context.dispatch_attempt_id,
        )
        return False


async def _read_active_turn_id(
    session_snapshots_repo: Any,
    context: TurnPreparationContext,
) -> str | None:
    return snapshot_active_turn_id(
        await session_snapshots_repo.get_snapshot(context.session_id)
    )


async def _confirm_turn_holds_slot(
    sessions_repo: Any,
    session_snapshots_repo: Any,
    context: TurnPreparationContext,
    *,
    owner_token: str,
) -> None:
    """Re-read the turn slot while this coordinator holds the guard.

    The claim's compare-and-set covers the session row only, so the turn is
    confirmed here, after the claim and before any provider effect. On a
    mismatch or a failed read the exact guard is released: nothing has run
    against the sandbox under it.
    """
    try:
        active_turn_id = await _read_active_turn_id(session_snapshots_repo, context)
    except asyncio.CancelledError:
        await _release_active_guard(sessions_repo, context, owner_token=owner_token)
        raise
    except Exception:
        await _release_active_guard(sessions_repo, context, owner_token=owner_token)
        raise _failure(
            context,
            reason="session_reload_failed",
            detail="persisted active turn could not be reloaded under the guard",
        ) from None
    if active_turn_id != context.turn_id:
        await _release_active_guard(sessions_repo, context, owner_token=owner_token)
        raise _turn_owner_mismatch(context)


def _schedule_abandoned_cleanup(
    provider_task: asyncio.Task[Any],
    *,
    sessions_repo: Any,
    context: TurnPreparationContext,
    owner_token: str,
    reason: str,
) -> None:
    provider_task.cancel()

    async def _cleanup() -> None:
        quarantined = False
        try:
            quarantined = bool(
                await sessions_repo.quarantine_turn_preparation_guard(
                    context.session_id,
                    sandbox_id=context.sandbox_id,
                    attempt_id=context.dispatch_attempt_id,
                    owner_token=owner_token,
                    reason=reason,
                )
            )
        except Exception:
            # ACTIVE remains durable and therefore still blocks every replica.
            logger.error(
                "turn preparation guard quarantine failed session=%s sandbox=%s "
                "attempt=%s",
                context.session_id,
                context.sandbox_id,
                context.dispatch_attempt_id,
            )
        with contextlib.suppress(BaseException):
            await provider_task
        if not quarantined:
            return
        try:
            released = await sessions_repo.release_turn_preparation_guard(
                context.session_id,
                sandbox_id=context.sandbox_id,
                attempt_id=context.dispatch_attempt_id,
                owner_token=owner_token,
                expected_state="QUARANTINED",
            )
        except Exception:
            released = False
        if not released:
            logger.error(
                "turn preparation quarantine release unconfirmed session=%s "
                "sandbox=%s attempt=%s",
                context.session_id,
                context.sandbox_id,
                context.dispatch_attempt_id,
            )

    cleanup_task = asyncio.create_task(
        _cleanup(),
        name=f"turn-prepare-quarantine:{context.session_id}:{context.dispatch_attempt_id}",
    )
    _track_background(cleanup_task, label="quarantine-reaper")
    logger.warning(
        "turn preparation abandoned session=%s sandbox=%s attempt=%s reason=%s",
        context.session_id,
        context.sandbox_id,
        context.dispatch_attempt_id,
        reason,
    )


async def _prepare_and_deliver(provider: Any, context: TurnPreparationContext) -> None:
    """Run the hook and write the files it returns, as one untrusted step.

    The startup environment is fixed for the runtime's life, so a turn may
    refresh files only. Delivery shares the hook's deadline and failure path.
    """
    from astrabox.core.service.orchestrator.runtime.preparation import (
        deliver_runtime_files,
    )

    materials = await provider.prepare_turn(context=context)
    if not isinstance(materials, RuntimeMaterials) or materials.environment:
        raise ValueError("turn preparation returns files only")
    if materials.files:
        if context.sandbox_handle is None:
            raise ValueError("turn materials require the live sandbox")
        await deliver_runtime_files(
            context.sandbox_handle,
            runtime_identity=context.runtime_identity,
            session_id=context.session_id,
            sandbox_id=context.sandbox_id,
            files=materials.files,
        )


async def _refresh_loop(
    provider: Any,
    context: TurnPreparationContext,
    *,
    validity_seconds: float,
    timeout_seconds: float,
    failed: asyncio.Event,
) -> None:
    """Re-prepare before the provider-declared validity lapses.

    Runs only while the same dispatch attempt's engine write is live, under
    the same held guard — re-preparation is the already-idempotent hook, so a
    replayed injection is safe. Any failure (timeout, exception,
    self-cancel) sets ``failed`` and stops: the caller quarantines the guard
    at release time instead of releasing it cleanly.
    """
    interval = max(0.1, validity_seconds * TURN_PREPARATION_REFRESH_FRACTION)
    while True:
        await asyncio.sleep(interval)
        refresh_task = asyncio.create_task(
            _prepare_and_deliver(provider, context),
            name=(
                f"turn-prepare-refresh:{context.sandbox_backend}:"
                f"{context.sandbox_id}:{context.dispatch_attempt_id}"
            ),
        )
        try:
            _done, pending = await asyncio.wait(
                {refresh_task}, timeout=max(0.0, timeout_seconds)
            )
        except asyncio.CancelledError:
            refresh_task.cancel()
            with contextlib.suppress(BaseException):
                await refresh_task
            raise
        if pending:
            refresh_task.cancel()
            with contextlib.suppress(BaseException):
                await refresh_task
            failed.set()
            logger.error(
                "turn preparation refresh exceeded the host-clock deadline "
                "session=%s sandbox=%s attempt=%s",
                context.session_id, context.sandbox_id, context.dispatch_attempt_id,
            )
            return
        if refresh_task.cancelled() or refresh_task.exception() is not None:
            failed.set()
            logger.error(
                "turn preparation refresh failed session=%s sandbox=%s attempt=%s",
                context.session_id, context.sandbox_id, context.dispatch_attempt_id,
            )
            return


@contextlib.asynccontextmanager
async def prepared_sandbox_turn(
    context: TurnPreparationContext,
    *,
    sessions_repo: Any,
    session_snapshots_repo: Any,
    timeout_seconds: float = TURN_PREPARATION_TIMEOUT_SECONDS,
) -> AsyncIterator[None]:
    """Fence provider preparation and the immediately following engine write.

    ``sessions_repo`` holds the session's sandbox binding, its principal and
    the guard; ``session_snapshots_repo`` holds the conversation's turn slot.
    Only the turn in that slot is prepared.
    """
    provider = runtime_preparation_provider()
    if provider is None or not prepares_turns(provider):
        yield
        return
    try:
        backend = sandbox_for_name(context.sandbox_backend)
    except asyncio.CancelledError:
        raise
    except Exception:
        raise _failure(
            context,
            reason="provider_resolution_failed",
            detail="sandbox provider resolution failed",
        ) from None
    if not bool(getattr(backend, "sandbox_is_profile_exclusive", False)):
        raise _failure(
            context,
            reason="unsafe_shared_sandbox",
            detail="turn preparation requires a profile-exclusive sandbox",
        )

    try:
        persisted_session = await sessions_repo.get_session(context.session_id)
        active_turn_id = await _read_active_turn_id(session_snapshots_repo, context)
    except asyncio.CancelledError:
        raise
    except Exception:
        raise _failure(
            context,
            reason="session_reload_failed",
            detail="persisted session could not be reloaded",
        ) from None
    principal_id = _validate_context(
        context,
        persisted_session=persisted_session,
        active_turn_id=active_turn_id,
    )
    context = _validated_live_context(backend, context)
    # The public dispatch attempt id is provider-facing and may be replayed by
    # an idempotent transport.  A fresh private owner token distinguishes this
    # coordinator invocation at every durable CAS/read-after-write boundary.
    guard_owner_token = str(uuid.uuid4())
    try:
        claimed = await sessions_repo.claim_turn_preparation_guard(
            context.session_id,
            principal_id=principal_id,
            sandbox_backend=context.sandbox_backend,
            sandbox_id=context.sandbox_id,
            turn_id=context.turn_id,
            attempt_id=context.dispatch_attempt_id,
            owner_token=guard_owner_token,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        raise _failure(
            context,
            reason="guard_claim_failed",
            detail="turn preparation guard could not be claimed",
        ) from None
    if not isinstance(claimed, dict):
        raise _failure(
            context,
            reason="guard_conflict",
            detail="another turn preparation attempt owns this sandbox",
        )
    await _confirm_turn_holds_slot(
        sessions_repo,
        session_snapshots_repo,
        context,
        owner_token=guard_owner_token,
    )

    try:
        provider_awaitable = _prepare_and_deliver(provider, context)
        provider_task = asyncio.create_task(
            provider_awaitable,
            name=(
                f"turn-prepare:{context.sandbox_backend}:"
                f"{context.sandbox_id}:{context.dispatch_attempt_id}"
            ),
        )
    except asyncio.CancelledError:
        await _release_active_guard(
            sessions_repo, context, owner_token=guard_owner_token
        )
        caller = asyncio.current_task()
        if caller is not None and caller.cancelling():
            raise
        raise _failure(
            context,
            reason="provider_self_cancelled",
            detail="provider preparation cancelled itself",
        ) from None
    except Exception:
        await _release_active_guard(
            sessions_repo, context, owner_token=guard_owner_token
        )
        raise _failure(
            context,
            reason="provider_error",
            detail="provider preparation failed",
        ) from None
    try:
        _done, pending = await asyncio.wait(
            {provider_task}, timeout=max(0.0, timeout_seconds)
        )
    except asyncio.CancelledError:
        _schedule_abandoned_cleanup(
            provider_task,
            sessions_repo=sessions_repo,
            context=context,
            owner_token=guard_owner_token,
            reason="caller_cancelled",
        )
        raise
    if pending:
        _schedule_abandoned_cleanup(
            provider_task,
            sessions_repo=sessions_repo,
            context=context,
            owner_token=guard_owner_token,
            reason="deadline_expired",
        )
        raise _failure(
            context,
            reason="timeout",
            detail="provider preparation exceeded the host-clock deadline",
        )
    if provider_task.cancelled():
        await _release_active_guard(
            sessions_repo, context, owner_token=guard_owner_token
        )
        raise _failure(
            context,
            reason="provider_self_cancelled",
            detail="provider preparation cancelled itself",
        )
    provider_error = provider_task.exception()
    if provider_error is not None:
        await _release_active_guard(
            sessions_repo, context, owner_token=guard_owner_token
        )
        if isinstance(provider_error, PreparationUserActionRequired):
            user_action = _validated_user_action(provider_error)
            if user_action is not None:
                raise _user_action_failure(context, user_action) from None
        raise _failure(
            context,
            reason="provider_error",
            detail="provider preparation failed",
        ) from None

    refresh_failed = asyncio.Event()
    refresher: asyncio.Task[Any] | None = None
    validity = getattr(provider, "turn_validity_seconds", None)
    if isinstance(validity, (int, float)) and not isinstance(validity, bool) and validity > 0:
        refresher = asyncio.create_task(
            _refresh_loop(
                provider,
                context,
                validity_seconds=float(validity),
                timeout_seconds=timeout_seconds,
                failed=refresh_failed,
            ),
            name=(
                f"turn-prepare-refresher:{context.session_id}:"
                f"{context.dispatch_attempt_id}"
            ),
        )
        _track_background(refresher, label="refresh-loop")
    try:
        yield
    finally:
        if refresher is not None and not refresher.done():
            refresher.cancel()
            with contextlib.suppress(BaseException):
                await refresher
        if refresh_failed.is_set():
            # The prepared state expired or broke mid-write: the sandbox's
            # provider-owned identity is suspect and the write may have
            # partially used it. Never release cleanly — quarantine, exactly
            # as a failed initial preparation does, and let durable recovery
            # prove the sandbox safe or replace its binding. (A failed
            # quarantine write leaves the guard ACTIVE, which blocks later
            # attempts just as hard.)
            with contextlib.suppress(Exception):
                await sessions_repo.quarantine_turn_preparation_guard(
                    context.session_id,
                    sandbox_id=context.sandbox_id,
                    attempt_id=context.dispatch_attempt_id,
                    owner_token=guard_owner_token,
                    reason="refresh_failed",
                )
            logger.error(
                "turn preparation refresh failed mid-write; guard quarantined "
                "session=%s sandbox=%s attempt=%s",
                context.session_id,
                context.sandbox_id,
                context.dispatch_attempt_id,
            )
        else:
            released = await _release_active_guard(
                sessions_repo, context, owner_token=guard_owner_token
            )
            if not released:
                # Never retry the engine write: it may already have succeeded.
                # The uncleared guard intentionally blocks later attempts until
                # durable recovery proves the old sandbox safe or replaces its
                # binding.
                logger.error(
                    "turn preparation guard release unconfirmed after engine "
                    "write session=%s sandbox=%s attempt=%s",
                    context.session_id,
                    context.sandbox_id,
                    context.dispatch_attempt_id,
                )


__all__ = [
    "TURN_PREPARATION_REFRESH_FRACTION",
    "TURN_PREPARATION_TIMEOUT_SECONDS",
    "prepared_sandbox_turn",
    "sandbox_turn_context",
]
