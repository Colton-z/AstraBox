"""Sandbox turn preparation against the turn the platform admitted.

Each case runs the real persistence path: ``SessionRepository``,
``SessionSnapshotRepository`` and ``SessionEventRepository`` over a collection
backend, the turn worker's StartTurn projection
(``_TurnProjectionMixin._project_command_accepted``) and the live worker's
terminal commit (``_commit_turn_terminal_snapshot``). That projection records
the running turn only in the snapshot's turn slot, so preparation reads the
turn there, and the guard on the session row must neither depend on nor stand
in for a turn field of its own. The delivery cases hand a journaled input to an
engine client through ``TurnService.deliver_pending_inputs``, which must
prepare the sandbox before the engine can act on the input.

The SQLite store runs in the default lane; the same cases run on PostgreSQL
(``-m postgresql``) and MongoDB (``-m mongo``) in the database lanes.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator import turn_preparation
from astrabox.core.service.orchestrator.engine.base import EngineInputCommand
from astrabox.core.service.orchestrator.engine.input_delivery import InputDeliveryRefused
from astrabox.core.service.orchestrator.runtime.models import SessionRuntime
from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    build_turn_terminal_snapshot_updates,
)
from astrabox.core.service.orchestrator.session_kernel.workers.lifecycle.create import (
    _CreateSessionMixin,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn.bridge_terminal import (
    _commit_turn_terminal_snapshot,
)
from astrabox.core.service.orchestrator.session_kernel.workers.turn.projections import (
    _TurnProjectionMixin,
)
from astrabox.core.service.orchestrator.turn_preparation import prepared_sandbox_turn
from astrabox.core.service.orchestrator.turn_service import TurnService
from astrabox.persistence.repository import (
    session_event_repository,
    session_repository,
    session_snapshot_repository,
)
from astrabox.persistence.repository.session_event_repository import (
    SessionEventRepository,
)
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.repository.session_snapshot_repository import (
    SessionSnapshotRepository,
)
from astrabox.persistence.repository.sqlite.collection import AsyncCollection
from astrabox.persistence.repository.sqlite.engine import create_all, dispose_engines
from astrabox.seams import runtime_preparation as preparation_seam
from astrabox.seams import sandbox as sandbox_seam
from astrabox.seams.runtime_preparation import (
    TURN_PREPARATION_FAILED,
    TURN_PREPARATION_USER_ACTION_REQUIRED,
    PreparationUserActionRequired,
    RuntimeMaterials,
    RuntimePreparationProvider,
    TurnPreparationContext,
    register_runtime_preparation_provider,
)
from astrabox.seams.sandbox import register_sandbox
from tests._postgresql_support import resolve_postgresql_test_url

_SESSION = "session-1"
_PRINCIPAL = "principal-1"
_BACKEND = "turn-authority-provider"


class _Stores(NamedTuple):
    sessions: SessionRepository
    snapshots: SessionSnapshotRepository
    events: SessionEventRepository


class _Box:
    def __init__(self, sandbox_id: str) -> None:
        self.sandbox_id = sandbox_id


class _Backend:
    """The profile-exclusive sandbox backend the session is bound to."""

    name = _BACKEND
    sandbox_is_profile_exclusive = True

    def owns_sandbox(self, sandbox: Any) -> bool:
        return isinstance(sandbox, _Box)


class _PreparingProvider(RuntimePreparationProvider):
    """A deployment integration that records each turn preparation it runs."""

    name = "turn-authority-preparation"

    def __init__(self) -> None:
        self.prepared: list[TurnPreparationContext] = []
        self.failure: BaseException | None = None

    async def prepare_turn(self, *, context: TurnPreparationContext) -> RuntimeMaterials:
        self.prepared.append(context)
        if self.failure is not None:
            raise self.failure
        return RuntimeMaterials()


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> _PreparingProvider:
    monkeypatch.setattr(sandbox_seam, "_BACKENDS", dict(sandbox_seam._BACKENDS))
    monkeypatch.setattr(preparation_seam, "_PROVIDER", None)
    monkeypatch.setattr(turn_preparation, "_BACKGROUND_CLEANUPS", set())
    register_sandbox(_Backend())  # type: ignore[arg-type]
    registered = _PreparingProvider()
    register_runtime_preparation_provider(registered)
    return registered


@pytest.fixture(
    params=[
        pytest.param("sqlite", id="sqlite"),
        pytest.param("postgresql", id="postgresql", marks=pytest.mark.postgresql),
        pytest.param("mongo", id="mongo", marks=pytest.mark.mongo),
    ]
)
async def stores(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[_Stores]:
    backend = str(request.param)
    monkeypatch.setenv("ASTRABOX_RUNTIME_INDEX_CREATION_ENABLED", "true")
    # Only these collections belong to this case, even on a shared database.
    prefix = f"tpa_{uuid.uuid4().hex[:12]}"
    opened: dict[str, Any] = {}
    url = ""
    if backend == "mongo":
        pytest.importorskip("pymongo")
        from astrabox.persistence.repository import mongo

        async def open_collection(name: str) -> Any:
            return await mongo.get_async_collection(f"{prefix}_{name}")
    else:
        url = (
            resolve_postgresql_test_url()
            if backend == "postgresql"
            else f"sqlite+aiosqlite:///{tmp_path}/turn-authority.sqlite"
        )
        await create_all(url)

        async def open_collection(name: str) -> Any:
            return AsyncCollection(f"{prefix}_{name}", url)

    async def collection(name: str) -> Any:
        if name not in opened:
            opened[name] = await open_collection(name)
        return opened[name]

    for module in (session_repository, session_snapshot_repository, session_event_repository):
        monkeypatch.setattr(module, "get_async_collection", collection)
    try:
        yield _Stores(SessionRepository(), SessionSnapshotRepository(), SessionEventRepository())
    finally:
        if backend == "mongo":
            from astrabox.persistence.repository import mongo

            database = await mongo._get_database()
            for name in opened:
                await database.drop_collection(f"{prefix}_{name}")
            await mongo.close_for_current_loop("turn_preparation_turn_authority_test")
        else:
            for handle in opened.values():
                await handle.delete_many({})
            await dispose_engines(url)


def _context(
    *,
    turn_id: str,
    attempt_id: str = "attempt-1",
    principal_id: str = _PRINCIPAL,
    sandbox_id: str = "sandbox-1",
) -> TurnPreparationContext:
    return TurnPreparationContext(
        sandbox_backend=_BACKEND,
        sandbox_id=sandbox_id,
        session_id=_SESSION,
        principal_id=principal_id,
        turn_id=turn_id,
        engine_kind="claude_code",
        dispatch_attempt_id=attempt_id,
        sandbox_handle=_Box(sandbox_id),
    )


def _prepared(
    context: TurnPreparationContext,
    stores: _Stores,
) -> AbstractAsyncContextManager[None]:
    return prepared_sandbox_turn(
        context,
        sessions_repo=stores.sessions,
        session_snapshots_repo=stores.snapshots,
    )


async def _create_session(stores: _Stores) -> None:
    """A READY session on ``sandbox-1`` whose turn slot is empty, as creation leaves it."""
    await stores.sessions.create_session(
        {
            "session_id": _SESSION,
            "user_id": _PRINCIPAL,
            "state": "READY",
            "session_kind": "agent_chat",
            "sandbox_backend": _BACKEND,
            "sandbox_id": "sandbox-1",
        }
    )
    created = await stores.events.append_event(
        {
            "session_id": _SESSION,
            "channel": "lifecycle",
            "event_type": "session.created",
            "causation_id": "create-session",
            "correlation_id": "create-session",
            "payload": {
                "command_type": "CreateSession",
                "template_name": "agent-1",
                "permission_mode": None,
                "session_kind": "agent_chat",
            },
        }
    )
    await _CreateSessionMixin._initialize_create_snapshots(
        SimpleNamespace(_session_snapshots_repo=stores.snapshots),  # type: ignore[arg-type]
        session_id=_SESSION,
        event_seq=int(created["event_seq"]),
    )


async def _admit(stores: _Stores, turn_id: str, *, event_seq: int) -> None:
    """Admit a StartTurn through the turn worker's own projection."""
    await _TurnProjectionMixin._project_command_accepted(
        SimpleNamespace(_session_snapshots_repo=stores.snapshots),  # type: ignore[arg-type]
        session_id=_SESSION,
        turn_id=turn_id,
        command_event={"causation_id": f"command-{turn_id}", "event_seq": event_seq},
        command_type="StartTurn",
    )


async def _settle(stores: _Stores, turn_id: str, *, event_seq: int) -> None:
    """Complete the turn through the live worker's terminal commit."""
    committed = await _commit_turn_terminal_snapshot(
        SimpleNamespace(_session_snapshots_repo=stores.snapshots),
        session_id=_SESSION,
        turn_id=turn_id,
        event_seq=event_seq,
        updates=build_turn_terminal_snapshot_updates(
            turn_id=turn_id,
            status="COMPLETED",
            error_text=None,
            command_id=f"command-{turn_id}",
        ),
    )
    assert committed is not None


async def _slot(stores: _Stores) -> tuple[str, str | None]:
    snapshot = await stores.snapshots.get_snapshot(_SESSION)
    assert snapshot is not None
    return snapshot["conversation_state"], snapshot["current_turn_id"]


async def _session_row(stores: _Stores) -> dict[str, Any]:
    row = await stores.sessions.get_session(_SESSION)
    assert row is not None
    return row


async def _guard(stores: _Stores) -> dict[str, Any] | None:
    return (await _session_row(stores)).get("turn_preparation_guard")


async def test_an_admitted_turn_is_prepared_and_reaches_its_engine_write(
    stores: _Stores,
    provider: _PreparingProvider,
) -> None:
    await _create_session(stores)
    await _admit(stores, "turn-1", event_seq=2)
    # Admission puts the turn in the snapshot's slot and nowhere else.
    assert await _slot(stores) == ("PROCESSING", "turn-1")
    assert (await _session_row(stores)).get("current_turn_id") is None

    context = _context(turn_id="turn-1")
    held: list[dict[str, Any] | None] = []
    async with _prepared(context, stores):
        # Inside the engine-write scope: preparation already ran and the
        # durable guard names this attempt.
        assert provider.prepared == [context]
        held.append(await _guard(stores))

    assert len(held) == 1
    guard = held[0]
    assert guard is not None
    assert (guard["state"], guard["sandbox_id"], guard["turn_id"], guard["attempt_id"]) == (
        "ACTIVE",
        "sandbox-1",
        "turn-1",
        "attempt-1",
    )
    assert await _guard(stores) is None
    # The guard came and went without recording a turn of its own.
    assert await _slot(stores) == ("PROCESSING", "turn-1")
    assert (await _session_row(stores)).get("current_turn_id") is None


@pytest.mark.parametrize(
    ("context_overrides", "reason"),
    [
        pytest.param({"turn_id": "turn-other"}, "turn_owner_mismatch", id="other-turn"),
        pytest.param(
            {"principal_id": "principal-2"}, "principal_mismatch", id="other-principal"
        ),
        pytest.param({"sandbox_id": "sandbox-2"}, "sandbox_mismatch", id="other-sandbox"),
    ],
)
async def test_a_context_the_session_does_not_own_is_refused_before_any_effect(
    stores: _Stores,
    provider: _PreparingProvider,
    context_overrides: dict[str, str],
    reason: str,
) -> None:
    await _create_session(stores)
    await _admit(stores, "turn-1", event_seq=2)

    with pytest.raises(APIError) as refused:
        async with _prepared(_context(**{"turn_id": "turn-1", **context_overrides}), stores):
            pytest.fail("a context the session does not own reached the engine write")

    assert refused.value.code == TURN_PREPARATION_FAILED
    assert refused.value.evidence["reason"] == reason
    assert provider.prepared == []
    assert await _guard(stores) is None
    assert await _slot(stores) == ("PROCESSING", "turn-1")


async def test_a_turn_that_loses_the_slot_while_claiming_releases_its_guard(
    stores: _Stores,
    provider: _PreparingProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = stores.sessions
    await _create_session(stores)
    await _admit(stores, "turn-1", event_seq=2)
    claim = sessions.claim_turn_preparation_guard
    landed: list[dict[str, Any] | None] = []

    async def claim_while_the_turn_settles(*args: Any, **kwargs: Any) -> Any:
        claimed = await claim(*args, **kwargs)
        if not landed:
            landed.append(claimed)
            # turn-1 passed the read before the claim; it now settles and
            # turn-2 is admitted before its coordinator reads the slot again.
            await _settle(stores, "turn-1", event_seq=3)
            await _admit(stores, "turn-2", event_seq=4)
        return claimed

    monkeypatch.setattr(sessions, "claim_turn_preparation_guard", claim_while_the_turn_settles)

    with pytest.raises(APIError) as refused:
        async with _prepared(_context(turn_id="turn-1"), stores):
            pytest.fail("a settled turn reached the engine write")

    assert refused.value.evidence["reason"] == "turn_owner_mismatch"
    assert provider.prepared == []
    # The claim landed; its coordinator released exactly the guard it took.
    claimed = landed[0]
    assert claimed is not None
    assert (claimed["turn_id"], claimed["attempt_id"]) == ("turn-1", "attempt-1")
    assert await _guard(stores) is None
    assert await _slot(stores) == ("PROCESSING", "turn-2")

    async with _prepared(_context(turn_id="turn-2", attempt_id="attempt-2"), stores):
        pass
    assert [context.turn_id for context in provider.prepared] == ["turn-2"]


async def test_one_coordinator_holds_the_sandbox_and_a_finished_one_cannot_touch_its_successor(
    stores: _Stores,
    provider: _PreparingProvider,
) -> None:
    sessions = stores.sessions
    await _create_session(stores)
    await _admit(stores, "turn-1", event_seq=2)
    holding = asyncio.Event()
    finish = asyncio.Event()
    held: list[dict[str, Any]] = []

    async def coordinator(attempt_id: str) -> None:
        async with _prepared(_context(turn_id="turn-1", attempt_id=attempt_id), stores):
            guard = await _guard(stores)
            assert guard is not None
            held.append(guard)
            holding.set()
            await finish.wait()

    # Two replicas dispatch the same admitted turn at once.
    replicas = [
        asyncio.create_task(coordinator(attempt_id))
        for attempt_id in ("attempt-a", "attempt-b")
    ]
    await asyncio.wait_for(holding.wait(), timeout=30)
    done, _pending = await asyncio.wait(
        replicas, timeout=30, return_when=asyncio.FIRST_COMPLETED
    )
    assert len(done) == 1
    loser = done.pop()
    with pytest.raises(APIError) as refused:
        loser.result()
    assert refused.value.evidence["reason"] == "guard_conflict"
    assert len(provider.prepared) == 1
    assert len(held) == 1
    finish.set()
    winner = next(task for task in replicas if task is not loser)
    await asyncio.wait_for(winner, timeout=30)
    assert await _guard(stores) is None

    # The next turn's coordinator takes the released guard. The finished
    # owner's private token can neither release, quarantine nor re-claim it.
    finished = held[0]
    stale = {
        "sandbox_id": "sandbox-1",
        "attempt_id": finished["attempt_id"],
        "owner_token": finished["owner_token"],
    }
    await _settle(stores, "turn-1", event_seq=3)
    await _admit(stores, "turn-2", event_seq=4)
    async with _prepared(_context(turn_id="turn-2", attempt_id="attempt-c"), stores):
        current = await _guard(stores)
        assert current is not None
        assert current["attempt_id"] == "attempt-c"
        assert not await sessions.release_turn_preparation_guard(
            _SESSION, expected_state="ACTIVE", **stale
        )
        assert not await sessions.quarantine_turn_preparation_guard(
            _SESSION, reason="deadline_expired", **stale
        )
        assert (
            await sessions.claim_turn_preparation_guard(
                _SESSION,
                principal_id=_PRINCIPAL,
                sandbox_backend=_BACKEND,
                turn_id="turn-1",
                **stale,
            )
            is None
        )
        assert await _guard(stores) == current

    assert await _guard(stores) is None
    assert [context.turn_id for context in provider.prepared] == ["turn-1", "turn-2"]


async def test_a_rebound_session_replaces_an_abandoned_guard(
    stores: _Stores,
    provider: _PreparingProvider,
) -> None:
    sessions = stores.sessions
    await _create_session(stores)
    await _admit(stores, "turn-1", event_seq=2)
    # A coordinator claims for turn-1 and its process dies before releasing:
    # the guard stays ACTIVE, with no expiry, after the turn settles.
    abandoned = await sessions.claim_turn_preparation_guard(
        _SESSION,
        principal_id=_PRINCIPAL,
        sandbox_backend=_BACKEND,
        sandbox_id="sandbox-1",
        turn_id="turn-1",
        attempt_id="attempt-lost",
        owner_token="lost-owner",
    )
    assert abandoned is not None
    await _settle(stores, "turn-1", event_seq=3)
    await _admit(stores, "turn-2", event_seq=4)

    with pytest.raises(APIError) as fenced:
        async with _prepared(_context(turn_id="turn-2", attempt_id="attempt-2"), stores):
            pytest.fail("the sandbox under an abandoned guard was prepared again")
    assert fenced.value.evidence["reason"] == "guard_conflict"

    # Recovery binds the session to a replacement sandbox; the abandoned guard
    # names the old one and gives way to the admitted turn.
    assert await sessions.update_session(_SESSION, {"sandbox_id": "sandbox-2"})
    replacement = _context(turn_id="turn-2", attempt_id="attempt-3", sandbox_id="sandbox-2")
    async with _prepared(replacement, stores):
        guard = await _guard(stores)
        assert guard is not None
        assert (guard["sandbox_id"], guard["turn_id"], guard["attempt_id"]) == (
            "sandbox-2",
            "turn-2",
            "attempt-3",
        )

    assert provider.prepared == [replacement]
    assert await _guard(stores) is None
    assert await _slot(stores) == ("PROCESSING", "turn-2")


# ── handing an input to the engine ───────────────────────────────────────────


class _EngineClient:
    """An engine client that notes what had happened when it took each input."""

    def __init__(
        self,
        stores: _Stores,
        provider: _PreparingProvider,
        refreshes: list[int],
    ) -> None:
        self._stores = stores
        self._provider = provider
        self._refreshes = refreshes
        #: ``(command_id, credential refreshes, preparations, guard)`` per
        #: input taken.
        self.taken: list[tuple[str, int, int, dict[str, Any] | None]] = []

    @property
    def is_live(self) -> bool:
        return True

    @property
    def engine_session_key(self) -> str | None:
        return None

    async def bind_conversation(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a delivery pass never binds the conversation")

    async def deliver(self, command: EngineInputCommand) -> None:
        self.taken.append(
            (
                command.command_id,
                len(self._refreshes),
                len(self._provider.prepared),
                await _guard(self._stores),
            )
        )

    async def begin_delivery(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a delivery pass never begins a turn")

    def iter_turn_events(self, receipt: Any) -> Any:
        raise AssertionError("a delivery pass never streams")

    async def cancel_turn(self, receipt: Any) -> bool:
        raise AssertionError("a delivery pass never cancels")

    async def interrupt_active_turn(self) -> bool:
        raise AssertionError("a delivery pass never interrupts")

    async def get_capabilities(self) -> Any:
        raise AssertionError("a delivery pass never reads capabilities")

    async def close(self) -> None:
        return None


async def _accept_start_turn(stores: _Stores, turn_id: str) -> dict[str, Any]:
    """Journal a StartTurn the way the input endpoint accepts it, then admit it."""
    command_id = f"command-{turn_id}"
    accepted, created = await stores.events.try_claim_event(
        {
            "session_id": _SESSION,
            "channel": "command",
            "turn_id": turn_id,
            "event_type": "command.accepted",
            "causation_id": command_id,
            "correlation_id": command_id,
            "payload": {
                "command_type": "StartTurn",
                "author_user_id": _PRINCIPAL,
                "interaction_id": None,
                "content": "hello",
                "interaction_response": None,
                "permission_mode": None,
                "client_message_id": f"client-{turn_id}",
                "input_id": str(uuid.uuid5(uuid.NAMESPACE_URL, turn_id)),
            },
        }
    )
    assert created
    await _admit(stores, turn_id, event_seq=int(accepted["event_seq"]))
    return accepted


def _delivery_service(
    stores: _Stores,
    engine: _EngineClient,
    *,
    refresh_credentials: Callable[[], Awaitable[None]],
) -> TurnService:
    """A ``TurnService`` whose ensured runtime carries ``engine`` on ``sandbox-1``."""
    runtime = SessionRuntime(
        session_id=_SESSION,
        agent=None,
        engine_kind="claude_code",
        sandbox=_Box("sandbox-1"),
        sandbox_id="sandbox-1",
        engine_client=engine,
        prepare_engine_input=refresh_credentials,
    )
    service = object.__new__(TurnService)
    service._input_delivery_locks = {}
    service._sessions_repo = stores.sessions
    service._session_snapshots_repo = stores.snapshots
    service._session_events_repo = stores.events

    async def ensure(_session: dict[str, Any], **_kwargs: Any) -> SessionRuntime:
        return runtime

    service.ensure_runtime_for_input_delivery = ensure  # type: ignore[method-assign]
    return service


async def test_an_input_reaches_the_engine_only_after_it_is_prepared(
    stores: _Stores,
    provider: _PreparingProvider,
) -> None:
    await _create_session(stores)
    accepted = await _accept_start_turn(stores, "turn-1")
    # Each refresh notes how many sandbox preparations had finished before it.
    refreshes: list[int] = []

    async def refresh_credentials() -> None:
        refreshes.append(len(provider.prepared))

    engine = _EngineClient(stores, provider, refreshes)
    service = _delivery_service(stores, engine, refresh_credentials=refresh_credentials)

    async def deliver() -> None:
        await service.deliver_pending_inputs(
            user=UserContext(user_id=_PRINCIPAL),
            session={"session_id": _SESSION},
            session_id=_SESSION,
            requested_command_id=accepted["causation_id"],
            turn_id="turn-1",
            permission_mode=None,
        )

    # The request that accepted the turn hands its input over. An engine may
    # start on an input the moment its client takes it, so the runtime's
    # credentials and the provider's preparation, under a held guard, must be
    # in place by then.
    await deliver()
    command_id, refreshed, prepared, guard = engine.taken[0]
    assert command_id == accepted["causation_id"]
    assert refreshes == [0], "credentials are refreshed before the sandbox is prepared"
    assert (refreshed, prepared) == (1, 1)
    assert guard is not None
    assert (guard["state"], guard["turn_id"], guard["attempt_id"]) == (
        "ACTIVE",
        "turn-1",
        provider.prepared[0].dispatch_attempt_id,
    )
    assert await _guard(stores) is None

    # The turn's worker delivers the same command again. The client already
    # took it, so the re-send writes nothing and prepares nothing.
    await deliver()
    assert engine.taken[1] == (command_id, 1, 1, None)


async def _expired_credentials() -> None:
    raise APIError(
        code="VAULT_CREDENTIAL_EXPIRED",
        message="vault credential 'mcp' is expired and has no refresh token",
        status_code=502,
    )


@pytest.mark.parametrize(
    ("provider_failure", "refresh_fails", "code", "status_code"),
    [
        pytest.param(
            RuntimeError("credential service unavailable"),
            False,
            TURN_PREPARATION_FAILED,
            502,
            id="provider-error",
        ),
        pytest.param(
            PreparationUserActionRequired(
                message="Authorize the agent to act for you",
                url="https://idp.example/confirm",
            ),
            False,
            TURN_PREPARATION_USER_ACTION_REQUIRED,
            409,
            id="user-action",
        ),
        pytest.param(None, True, "VAULT_CREDENTIAL_EXPIRED", 502, id="expired-credentials"),
    ],
)
async def test_a_failed_preparation_keeps_the_input_from_the_engine(
    stores: _Stores,
    provider: _PreparingProvider,
    provider_failure: BaseException | None,
    refresh_fails: bool,
    code: str,
    status_code: int,
) -> None:
    await _create_session(stores)
    accepted = await _accept_start_turn(stores, "turn-1")
    provider.failure = provider_failure
    refreshes: list[int] = []

    async def refresh_credentials() -> None:
        refreshes.append(len(provider.prepared))

    engine = _EngineClient(stores, provider, refreshes)
    settled: list[InputDeliveryRefused] = []

    async def settle(refused: InputDeliveryRefused) -> None:
        settled.append(refused)

    service = _delivery_service(
        stores,
        engine,
        refresh_credentials=_expired_credentials if refresh_fails else refresh_credentials,
    )
    with pytest.raises(InputDeliveryRefused) as refused:
        await service.deliver_pending_inputs(
            user=UserContext(user_id=_PRINCIPAL),
            session={"session_id": _SESSION},
            session_id=_SESSION,
            requested_command_id=accepted["causation_id"],
            turn_id="turn-1",
            permission_mode=None,
            on_refused=settle,
        )

    # The engine never took the input; the accepting request settles the
    # turn with the refusal before answering it.
    assert engine.taken == []
    assert (refused.value.code, refused.value.status_code) == (code, status_code)
    assert settled == [refused.value]
    assert await _guard(stores) is None
    if refresh_fails:
        assert provider.prepared == [], "the sandbox is not prepared for stale credentials"
    if code == TURN_PREPARATION_USER_ACTION_REQUIRED:
        assert refused.value.evidence["user_action"]["url"] == "https://idp.example/confirm"


async def test_user_action_refusal_survives_replay_and_session_reload(stores: _Stores) -> None:
    from astrabox.core.service.orchestrator.engine.input_delivery import refusal_record, settled_input_outcome
    from astrabox.core.service.orchestrator.session_kernel.service_mixins.session_read import SessionReadRenderingMixin

    await _create_session(stores)
    accepted = await _accept_start_turn(stores, "turn-action")
    action = {"status": "user_action_required", "message": "Consent required", "url": "https://idp.example/confirm"}
    refused = InputDeliveryRefused(code=TURN_PREPARATION_USER_ACTION_REQUIRED,
        message="Consent required", status_code=409, evidence={"user_action": action, "private": "not public"})
    await stores.events.append_event({
        "session_id": _SESSION, "channel": "conversation", "turn_id": "turn-action",
        "event_type": "turn.failed", "causation_id": accepted["causation_id"],
        "payload": {"failure_phase": "pre_dispatch", "refusal": refusal_record(refused)},
    })
    replay = await settled_input_outcome(stores.events, _SESSION, accepted["causation_id"])
    assert replay is not None and replay.evidence == {"user_action": action}
    reader = SessionReadRenderingMixin()
    reader._session_events_repo = stores.events
    restored = await reader._build_delivery_failure(_SESSION,
        snapshot={"delivery_state": "NOT_RECEIVED", "last_turn_id": "turn-action"})
    assert restored is not None
    assert restored["user_action"] == action and restored["resend_as_new"] is True
    assert restored["client_message_id"] == accepted["payload"]["client_message_id"]


async def test_session_user_action_http_scopes_operations_and_unknown_status(stores: _Stores, monkeypatch) -> None:
    import logging
    from collections import deque
    from astrabox.common.logger.logger_factory import MODULE_APP_NAME

    application_logger = logging.getLogger(MODULE_APP_NAME)
    monkeypatch.setattr(application_logger, "handlers", list(application_logger.handlers))
    from astrabox.core.service.orchestrator import admin_error_capture

    # This route test owns no observability worker; isolate import-time capture.
    monkeypatch.setattr(admin_error_capture, "_bootstrap_drain", lambda: None)
    monkeypatch.setattr(admin_error_capture, "_buffer", deque(maxlen=2000))
    import httpx
    from fastapi import FastAPI
    from astrabox.api.routes import session_user_actions as routes
    from astrabox.api.routes._shared import handle_api_error, handle_unknown_error
    from astrabox.seams import runtime_preparation as seam

    class Provider(seam.RuntimePreparationProvider):
        name = "user-action-deployment"
        ready = False
        broken = False
        malformed = False

        async def read_user_actions(self, *, context):
            assert context.principal_id == _PRINCIPAL and context.session_id == _SESSION
            if self.broken:
                raise RuntimeError("private-provider-diagnostic")
            if self.malformed:
                return seam.SessionUserActionState(ready=False, user_action={
                    "message": "Unsafe link", "url": "javascript:alert(1)",
                })
            return seam.SessionUserActionState(ready=True) if self.ready else seam.SessionUserActionState(
                ready=False, user_action={"message": "Approve access", "status": "pending", "url": None},
                actions=(seam.SessionUserActionCommand("confirm", "Check approval"),))

        async def perform_user_action(self, *, context, action_id):
            assert action_id == "confirm"
            self.ready = True
            return await self.read_user_actions(context=context)

    await _create_session(stores)
    monkeypatch.setattr(seam, "_PROVIDER", None)
    user = UserContext(user_id=_PRINCIPAL)

    async def identity(request):
        return user

    monkeypatch.setattr(routes, "_resolve_user", identity)
    app = FastAPI()
    app.include_router(routes.router)
    app.add_exception_handler(APIError, handle_api_error)
    app.add_exception_handler(Exception, handle_unknown_error)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver") as client:
        url = f"/api/v1/sessions/{_SESSION}/user-action"
        assert (await client.get(url)).json()["data"]["ready"] is True
        provider = Provider()
        seam.register_runtime_preparation_provider(provider)
        blocked = (await client.get(url)).json()["data"]
        assert blocked["ready"] is False and blocked["user_action"]["url"] is None
        assert (await client.post(url, json={"action_id": "stale"})).status_code == 409
        assert (await client.post(url, json={"action_id": "confirm", "principal_id": "other"})).status_code == 422
        user = UserContext(user_id="other")
        assert (await client.get(url)).status_code == 404
        user = UserContext(user_id=_PRINCIPAL, api_scopes=["astrabox:read"])
        assert (await client.post(url, json={"action_id": "confirm"})).status_code == 403
        user = UserContext(user_id=_PRINCIPAL)
        assert (await client.post(url, json={"action_id": "confirm"})).json()["data"]["ready"] is True
        assert (await client.get(url)).json()["data"]["ready"] is True
        assert (await client.post(url, json={"action_id": "confirm"})).status_code == 409
        provider.broken = True
        failure = await client.get(url)
        assert failure.status_code == 502
        assert "private-provider-diagnostic" not in failure.text
        provider.broken = False
        provider.malformed = True
        invalid = await client.get(url)
        assert invalid.status_code == 502
        assert "javascript:" not in invalid.text
