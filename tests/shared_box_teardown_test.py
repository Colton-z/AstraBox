"""Ending one conversation destroys the box only when nobody else is in it.

Under the shared tenancy a box belongs to the AGENT: several conversations live
in it, each in its own isolated session with its own POSIX owner, home and
listening runner. Every one of those dies with the box. So the two paths that
destroy a box on a conversation's behalf — the normal teardown, and the cleanup
that runs when a runtime fails on its way up — must stop at the isolated session
while a sibling is still in there.

The other half of that rule is what these tests also pin: once the LAST
conversation leaves, the box is nobody's and has to go. Retaining it regardless
kept it for the whole lease instead — four hours by default — so an Agent
ended up holding one box per peak conversation, and the Pool it borrowed them
from ran dry for everyone else.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from astrabox.core.service.orchestrator.runtime_manager import (
    RemoteAgentRuntimeManager,
    SessionRuntime,
)
from astrabox.core.service.orchestrator import runtime_manager as runtime_manager_module
from astrabox.core.service.orchestrator.sandbox_names import keep_name_updates
from astrabox.seams.sandbox import SandboxAllocation
from astrabox.seams.sandbox_disposal import (
    SANDBOX_DESTRUCTION_REFUSED,
    SANDBOX_DESTRUCTION_RETAINED,
    SandboxDestruction,
)

BOX = "box-shared"
ISO = "iso-7"


def _runtime(
    session_id: str,
    *,
    sandbox_id: str = BOX,
    isolated_session_id: str | None = ISO,
    engine_kind: str = "claude_code",
) -> SessionRuntime:
    """Build an engine result with client state and no provider executor."""

    return SessionRuntime(
        session_id=session_id,
        agent=None,
        engine_kind=engine_kind,
        sandbox_id=sandbox_id,
        isolated_session_id=isolated_session_id,
    )


def _wire_persisted_release(
    monkeypatch: Any,
    *,
    fail: bool = False,
) -> list[tuple[str, str]]:
    """Let the platform release the placement from its durable Session row."""

    closed: list[tuple[str, str]] = []

    class _Sessions:
        async def get_session(self, session_id: str) -> dict[str, Any]:
            return {
                "session_id": session_id,
                "sandbox_id": BOX,
                "sandbox_backend": "open_sandbox",
                "runtime_identity": {"isolated_session_id": ISO},
            }

    class _Provider:
        async def close_isolated_session(
            self,
            sandbox_id: str,
            isolated_session_id: str,
        ) -> None:
            if fail:
                raise RuntimeError("the lifecycle API said no")
            closed.append((sandbox_id, isolated_session_id))

    monkeypatch.setattr(runtime_manager_module, "SessionRepository", _Sessions)
    monkeypatch.setattr(
        runtime_manager_module,
        "sandbox_for_name",
        lambda _name: _Provider(),
    )
    return closed


class _Occupants:
    """The rows the manager reads to decide whether the box is still somebody's.

    Both shapes matter and they are not the same fact: ``bound`` is a
    conversation whose sandbox_id has settled, ``starting`` is one still coming
    up, which has only its startup allocation to be seen by.
    """

    def __init__(
        self,
        *,
        bound: list[str] | None = None,
        starting: list[str] | None = None,
    ) -> None:
        self._bound = list(bound or [])
        self._starting = list(starting or [])

    async def list_sessions_by_sandbox_id(self, sandbox_id: str) -> list[dict[str, Any]]:
        assert sandbox_id == BOX
        return [{"session_id": sid, "sandbox_id": BOX} for sid in self._bound]

    async def list_startup_allocation_candidates(
        self, *, limit: int
    ) -> list[dict[str, Any]]:
        # Asked for explicitly: the repository's default page is ordered oldest
        # first, which is the one page a joiner would not be on.
        assert limit >= 500, f"the occupancy question must ask for a full page, got {limit}"
        return [
            {"session_id": sid, "startup_allocation": {"sandbox_id": BOX}}
            for sid in self._starting
        ]

    async def get_session_including_deleted(
        self, session_id: str
    ) -> dict[str, Any]:
        return {"session_id": session_id, "agent_id": "a1"}


class _Agents:
    """Agent rows, for the arm of the occupancy question no session can answer.

    A prepared slot exists precisely BEFORE a conversation claims it, so it is
    recorded on the Agent row and every session query misses it.
    """

    def __init__(
        self,
        *,
        slot_in_box: bool = False,
        admissions: list[dict[str, Any]] | None = None,
        current_box: str = BOX,
    ) -> None:
        self._slot_in_box = slot_in_box
        self._admissions = list(admissions or [])
        self._current_box = current_box

    def _row(self) -> dict[str, Any]:
        manifest = {"sandbox_id": BOX} if self._slot_in_box else None
        return {
            "agent_id": "a1",
            "sandbox_id": self._current_box,
            "_prepared_slot": manifest,
            "box_admissions": self._admissions,
        }

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        return self._row() if agent_id == "a1" else None

    async def list_agents_by_sandbox_id(self, sandbox_id: str) -> list[dict[str, Any]]:
        assert sandbox_id == BOX
        return [self._row()] if self._current_box == sandbox_id else []

    async def compare_and_update_agent(
        self,
        agent_id: str,
        *,
        expected: dict[str, Any],
        updates: dict[str, Any],
    ) -> bool:
        # Teardown withdraws the departing conversation's own admission; it
        # owns no other Agent field.
        assert set(updates) == {"box_admissions"}, updates
        row = self._row()
        if agent_id != "a1" or any(row.get(k) != v for k, v in expected.items()):
            return False
        self._admissions = list(updates["box_admissions"])
        return True


def _with_agents(monkeypatch: Any, agents: _Agents) -> None:
    import astrabox.persistence.repository.agent_repository as agent_repository

    monkeypatch.setattr(agent_repository, "AgentRepository", lambda: agents)


def _answer_census(
    monkeypatch: Any, mgr: RemoteAgentRuntimeManager, live_sessions: int
) -> None:
    """Make the box itself report ``live_sessions`` isolated sessions."""

    class _Census:
        async def count_live_isolated_sessions(self, sandbox_id: str) -> int:
            return live_sessions

    async def _provider(sandbox_id: str) -> Any:
        return _Census()

    monkeypatch.setattr(mgr, "resolve_sandbox_provider", _provider)


def _manager(
    monkeypatch: Any,
    occupants: _Occupants | None = None,
    *,
    live_sessions: int = 0,
    destroys: bool = False,
) -> tuple[RemoteAgentRuntimeManager, list[str]]:
    """A manager whose every box-destroy is recorded instead of performed.

    ``live_sessions`` is what the BOX says it still holds, the last lens the
    occupancy question consults. It is asked after the departing
    conversation's own agent and terminal sessions are closed, so any live
    session is occupancy and zero is the only empty answer.

    A destroy fails the test unless ``destroys`` says the case expects the box
    to go; then it is recorded and confirmed.
    """
    mgr = RemoteAgentRuntimeManager(
        sessions_repo=occupants if occupants is not None else _Occupants()
    )
    destroyed: list[str] = []

    async def _destroy(session_id: str, sandbox_id: str) -> Any:
        destroyed.append(sandbox_id)
        if not destroys:
            raise AssertionError(f"a shared box was destroyed: {sandbox_id}")
        return SandboxDestruction.confirmed_gone(sandbox_id, detail="destroyed")

    monkeypatch.setattr(mgr, "_destroy_and_forget_pending", _destroy)
    _answer_census(monkeypatch, mgr, live_sessions)
    return mgr, destroyed


async def test_terminating_one_conversation_closes_its_session_not_the_box(
    monkeypatch: Any,
) -> None:
    # A sibling is named rather than assumed: with nobody else in the box the
    # correct answer is the opposite one, so a test that leaves this out is
    # asserting a rule it never states.
    _with_agents(monkeypatch, _Agents())
    closed = _wire_persisted_release(monkeypatch)
    mgr, destroyed = _manager(monkeypatch, _Occupants(bound=["s-sibling"]))
    runtime = _runtime("s-1")
    mgr._runtimes["s-1"] = runtime

    verdict = await mgr.terminate_runtime("s-1")

    assert closed == [(BOX, ISO)]
    assert destroyed == []
    # The box was intentionally retained by its Agent, while this
    # conversation's isolated placement was successfully released. This is
    # neither a destruction nor a leak to record on the deleted session row.
    assert verdict.outcome == SANDBOX_DESTRUCTION_RETAINED
    assert verdict.leaked_sandbox_id is None
    assert keep_name_updates(verdict, row={}) == {}
    assert not verdict.confirmed


async def test_a_failed_start_inside_a_shared_box_leaves_the_box_standing(
    monkeypatch: Any,
) -> None:
    """The startup allocation records exact release authority before failure."""

    class _Sessions:
        def __init__(self) -> None:
            self.row: dict[str, Any] = {"session_id": "s-2", "state": "CREATING"}

        async def get_session(self, session_id: str) -> dict[str, Any] | None:
            return dict(self.row) if session_id == "s-2" else None

        async def get_session_including_deleted(
            self, session_id: str
        ) -> dict[str, Any] | None:
            return await self.get_session(session_id)

        async def record_startup_allocation(
            self,
            session_id: str,
            allocation: dict[str, Any],
        ) -> bool:
            assert session_id == "s-2"
            self.row["startup_allocation"] = dict(allocation)
            return True

        async def clear_startup_allocation(
            self,
            session_id: str,
            *,
            allocation: dict[str, Any],
        ) -> bool:
            assert session_id == "s-2"
            current = self.row.get("startup_allocation")
            if not isinstance(current, dict) or current != allocation:
                return current != allocation
            self.row["startup_allocation"] = None
            return True

    class _Provider:
        async def probe(self, sandbox_id: str) -> Any:
            _ = sandbox_id
            return SimpleNamespace(probe_status="OK")

        async def close_isolated_session(
            self,
            sandbox_id: str,
            isolated_session_id: str,
        ) -> None:
            closed.append((sandbox_id, isolated_session_id))

    sessions = _Sessions()
    mgr = RemoteAgentRuntimeManager(sessions_repo=sessions)
    destroyed: list[str] = []
    closed: list[tuple[str, str]] = []

    async def _destroy(session_id: str, sandbox_id: str) -> Any:
        _ = session_id
        destroyed.append(sandbox_id)
        raise AssertionError(f"a shared box was destroyed: {sandbox_id}")

    monkeypatch.setattr(
        runtime_manager_module,
        "sandbox_for_name",
        lambda _name: _Provider(),
    )
    monkeypatch.setattr(mgr, "_destroy_and_forget_pending", _destroy)
    await mgr.record_startup_allocation(
        "s-2",
        SandboxAllocation(
            sandbox_id=BOX,
            sandbox_backend="open_sandbox",
            scope="isolated_sessions",
            isolated_session_ids=(ISO,),
        ),
    )
    cleanup = await mgr.cleanup_startup_allocation("s-2")

    assert cleanup.released is True
    assert cleanup.leaked_sandbox_id is None
    assert closed == [(BOX, ISO)]
    assert destroyed == []
    assert sessions.row["startup_allocation"] is None


async def test_the_last_conversation_out_takes_the_agent_box_with_it(
    monkeypatch: Any,
) -> None:
    """Nobody left in it means it is nobody's, and a kept box is a borrowed one.

    Under the shared tenancy the box is lent by a Pool with a hard ceiling.
    Holding it until the lease expires does not merely waste it — it is one
    fewer box every other Agent can ever be given, and the Pool answers the
    next borrower that it cannot lend.
    """

    _with_agents(monkeypatch, _Agents())
    mgr = RemoteAgentRuntimeManager(sessions_repo=_Occupants())
    destroyed: list[str] = []

    async def _destroy(session_id: str, sandbox_id: str) -> Any:
        _ = session_id
        destroyed.append(sandbox_id)
        return SandboxDestruction.confirmed_gone(sandbox_id, detail="destroyed")

    monkeypatch.setattr(mgr, "_destroy_and_forget_pending", _destroy)
    _answer_census(monkeypatch, mgr, 0)
    closed = _wire_persisted_release(monkeypatch)
    mgr._runtimes["s-last"] = _runtime("s-last")

    verdict = await mgr.terminate_runtime("s-last")

    # The isolated session is still closed first: the box goes either way, but
    # an unclosed session is what would be left behind if it did not.
    assert closed == [(BOX, ISO)]
    assert destroyed == [BOX]
    assert verdict.confirmed


async def test_a_box_holding_a_prepared_slot_is_not_destroyed(
    monkeypatch: Any,
) -> None:
    """A prepared slot remains an occupant after the Agent prefers another box.

    Its whole purpose is to exist before a conversation claims it, so it lives
    on the Agent row. A full box may stop being the row's top-level sandbox while
    its prepared-slot manifest still names that box; the departing Session is
    the durable link to the Agent whose complete inventory must be inspected.
    """

    _with_agents(
        monkeypatch,
        _Agents(slot_in_box=True, current_box="box-now-preferred"),
    )
    _wire_persisted_release(monkeypatch)
    mgr, destroyed = _manager(monkeypatch, _Occupants(), live_sessions=-1)
    mgr._runtimes["s-slotted"] = _runtime("s-slotted")

    verdict = await mgr.terminate_runtime("s-slotted")

    assert destroyed == []
    assert verdict.outcome == SANDBOX_DESTRUCTION_RETAINED


async def test_a_conversation_still_starting_up_keeps_the_box(
    monkeypatch: Any,
) -> None:
    """A joiner is an occupant before it is a bound one.

    It has no sandbox_id on its row yet — only a startup allocation — so a
    check that read bound sessions alone would destroy the box out from under
    a conversation in the middle of joining it.
    """

    _with_agents(monkeypatch, _Agents())
    _wire_persisted_release(monkeypatch)
    mgr, destroyed = _manager(monkeypatch, _Occupants(starting=["s-joining"]))
    mgr._runtimes["s-5"] = _runtime("s-5")

    verdict = await mgr.terminate_runtime("s-5")

    assert destroyed == []
    assert verdict.outcome == SANDBOX_DESTRUCTION_RETAINED


async def test_an_unanswerable_occupancy_question_keeps_the_box(
    monkeypatch: Any,
) -> None:
    """Not knowing is not the same as knowing it is empty.

    Being wrong this way costs the box its lease, which is what the old
    unconditional retain cost every time. Being wrong the other way destroys a
    box somebody is working in.
    """

    class _Broken(_Occupants):
        async def list_sessions_by_sandbox_id(self, sandbox_id: str) -> Any:
            raise RuntimeError("the session store is unavailable")

    _with_agents(monkeypatch, _Agents())
    _wire_persisted_release(monkeypatch)
    mgr, destroyed = _manager(monkeypatch, None)
    mgr._sessions_repo = _Broken()
    mgr._runtimes["s-6"] = _runtime("s-6")

    verdict = await mgr.terminate_runtime("s-6")

    assert destroyed == []
    assert verdict.outcome == SANDBOX_DESTRUCTION_RETAINED


async def test_a_release_that_fails_still_never_destroys_the_box(
    monkeypatch: Any,
) -> None:
    """An unclosed session is a leak inside the box; destroying it is worse.

    The uid, the home and the runner stay alive and nothing reclaims them until
    the box goes — which is why this is logged as an error. It is still the
    right trade: the siblings keep working.
    """
    _wire_persisted_release(monkeypatch, fail=True)
    mgr, destroyed = _manager(monkeypatch)
    runtime = _runtime("s-3")
    mgr._runtimes["s-3"] = runtime

    verdict = await mgr.terminate_runtime("s-3")

    assert destroyed == []
    assert verdict.outcome == SANDBOX_DESTRUCTION_REFUSED
    assert verdict.leaked_sandbox_id == BOX
    assert keep_name_updates(verdict, row={}) == {
        "undestroyed_sandbox_ids": [BOX]
    }


async def test_the_per_session_tenancy_still_destroys_its_own_box(
    monkeypatch: Any,
) -> None:
    """Per-session tenancy still destroys the conversation-owned box."""
    mgr = RemoteAgentRuntimeManager()
    destroyed: list[str] = []

    async def _destroy(session_id: str, sandbox_id: str) -> Any:
        destroyed.append(sandbox_id)
        return object()

    monkeypatch.setattr(mgr, "_destroy_and_forget_pending", _destroy)
    mgr._runtimes["s-4"] = _runtime(
        "s-4",
        sandbox_id="box-own",
        isolated_session_id=None,
    )

    await mgr.terminate_runtime("s-4")
    assert destroyed == ["box-own"]


async def test_restart_closes_the_durable_isolated_session_not_the_shared_box(
    monkeypatch: Any,
) -> None:
    """After a server restart there is no SessionRuntime, only the row.

    Its durable isolated-session id must retain the same teardown semantics as
    the live path; treating the fallback sandbox id as conversation-owned
    destroys every sibling in the Agent's box.
    """
    mgr = RemoteAgentRuntimeManager()
    closed: list[tuple[str, str]] = []

    class _Sessions:
        async def get_session(self, session_id: str) -> dict[str, Any]:
            return {
                "session_id": session_id,
                "sandbox_id": BOX,
                "sandbox_backend": "open_sandbox",
                "runtime_identity": {"isolated_session_id": ISO},
            }

    class _Provider:
        async def close_isolated_session(
            self, sandbox_id: str, isolated_session_id: str
        ) -> None:
            closed.append((sandbox_id, isolated_session_id))

    async def _never_destroy(session_id: str, sandbox_id: str) -> Any:
        raise AssertionError(f"restart destroyed shared box {sandbox_id}")

    monkeypatch.setattr(runtime_manager_module, "SessionRepository", _Sessions)
    monkeypatch.setattr(runtime_manager_module, "sandbox_for_name", lambda _name: _Provider())
    monkeypatch.setattr(mgr, "_destroy_and_forget_pending", _never_destroy)

    verdict = await mgr.terminate_runtime("s-restart", fallback_sandbox_id=BOX)

    assert closed == [(BOX, ISO)]
    assert verdict.outcome == SANDBOX_DESTRUCTION_RETAINED
    assert verdict.sandbox_id == BOX
    assert verdict.leaked_sandbox_id is None
    assert keep_name_updates(verdict, row={}) == {}
async def test_a_freshly_admitted_joiner_keeps_the_box(
    monkeypatch: Any,
) -> None:
    """An admission is an occupant before any session query can see one.

    The lease charges the box at the instant of the join; the joiner's own
    startup allocation lands only after its placement completes. During that
    window the admissions ledger on the Agent row is the only record. That row
    can already prefer a newer box, so the departing Session is what locates
    the complete Agent inventory; filtering by the top-level box first misses
    the admission and destroys the box under the joiner.
    """

    import time as _time

    _with_agents(
        monkeypatch,
        _Agents(
            admissions=[
                {
                    "sandbox_id": BOX,
                    "at": _time.time(),
                    "session_id": "s-joining",
                }
            ],
            current_box="box-now-preferred",
        ),
    )
    _wire_persisted_release(monkeypatch)
    mgr, destroyed = _manager(monkeypatch, _Occupants(), live_sessions=-1)
    mgr._runtimes["s-leaving"] = _runtime("s-leaving")

    verdict = await mgr.terminate_runtime("s-leaving")

    assert destroyed == []
    assert verdict.outcome == SANDBOX_DESTRUCTION_RETAINED


async def test_a_conversations_own_admission_does_not_hold_its_box(
    monkeypatch: Any,
) -> None:
    """A short-lived conversation must still give the box back.

    Its own admission is younger than the grace when it terminates; reading
    that entry as a stranger would retain an empty box for the ledger's whole
    grace and, with no later terminate to re-ask, for the rest of the lease.
    Expired strangers' admissions are no occupants either — the ledger's own
    grace is the authority on when an admission stops counting.
    """

    import time as _time

    _with_agents(
        monkeypatch,
        _Agents(
            admissions=[
                {"sandbox_id": BOX, "at": _time.time(), "session_id": "s-brief"},
                {"sandbox_id": BOX, "at": _time.time() - 3600.0, "session_id": "s-old"},
            ]
        ),
    )
    _wire_persisted_release(monkeypatch)
    mgr = RemoteAgentRuntimeManager(sessions_repo=_Occupants())
    destroyed: list[str] = []

    async def _destroy(session_id: str, sandbox_id: str) -> Any:
        _ = session_id
        destroyed.append(sandbox_id)
        return SandboxDestruction.confirmed_gone(sandbox_id, detail="destroyed")

    monkeypatch.setattr(mgr, "_destroy_and_forget_pending", _destroy)
    _answer_census(monkeypatch, mgr, 0)
    mgr._runtimes["s-brief"] = _runtime("s-brief")

    verdict = await mgr.terminate_runtime("s-brief")

    assert destroyed == [BOX]
    assert verdict.confirmed
async def test_a_runtime_without_an_executor_handle_still_closes_its_session(
    monkeypatch: Any,
) -> None:
    """The box-service engines publish agent=None; their sessions still end.

    p125: a dsh delete answered SESSION_SANDBOX_DESTRUCTION_REFUSED because
    the release path found no executor handle to close the isolated session
    with and declared it unclosable — while the session row carried
    everything the post-restart path already closes sessions from.
    """

    closed: list[tuple[str, str]] = []

    class _Provider:
        async def close_isolated_session(self, sandbox_id: str, child: str) -> None:
            closed.append((sandbox_id, child))

    class _Sessions(_Occupants):
        async def get_session(self, session_id: str) -> dict[str, Any]:
            return {
                "session_id": session_id,
                "sandbox_id": BOX,
                "sandbox_backend": "open_sandbox",
                "runtime_identity": {
                    "isolated_session_id": ISO,
                    "terminal_isolated_session_id": "iso-term",
                },
            }

    import astrabox.core.service.orchestrator.runtime_manager as rm

    monkeypatch.setattr(rm, "sandbox_for_name", lambda name: _Provider())
    monkeypatch.setattr(
        rm, "SessionRepository", lambda: _Sessions(), raising=False
    )
    _with_agents(monkeypatch, _Agents())
    mgr = RemoteAgentRuntimeManager(sessions_repo=_Occupants())
    destroyed: list[str] = []

    async def _destroy(session_id: str, sandbox_id: str) -> Any:
        _ = session_id
        destroyed.append(sandbox_id)
        return SandboxDestruction.confirmed_gone(sandbox_id, detail="destroyed")

    monkeypatch.setattr(mgr, "_destroy_and_forget_pending", _destroy)
    _answer_census(monkeypatch, mgr, 0)
    mgr._runtimes["s-armless"] = SessionRuntime(
        session_id="s-armless",
        agent=None,
        engine_kind="deepseek_harness",
        sandbox_id=BOX,
        isolated_session_id=ISO,
    )

    verdict = await mgr.terminate_runtime("s-armless")

    # Both the terminal and the agent session were closed through the row,
    # and with nobody left the empty box was still given back.
    assert (BOX, "iso-term") in closed
    assert (BOX, ISO) in closed
    assert verdict.confirmed
    assert destroyed == [BOX]
async def test_the_reaper_gives_back_an_abandoned_box_and_keeps_a_lived_in_one(
    monkeypatch: Any,
) -> None:
    """A lane whose sessions end without a terminate must not strand boxes.

    p138: fifteen boxes from three finished lanes filled the node and the
    fourth lane could not build one. The sweep asks the terminate's own
    occupancy question with nobody excluded and destroys only on confirmed
    evidence; a box with any occupant is kept untouched.
    """

    import astrabox.persistence.repository.agent_repository as agent_repository

    class _Repo:
        def __init__(self, *, occupied: bool) -> None:
            self.occupied = occupied
            self.cleared: list[str] = []

        async def list_agents_with_resident_boxes(self, *, limit: int = 200):
            return [{"agent_id": "a1", "sandbox_id": BOX, "_prepared_slot": None,
                     "box_admissions": []}]

        async def list_agents_by_sandbox_id(self, sandbox_id: str):
            return [{"agent_id": "a1", "_prepared_slot": None, "box_admissions": []}]

        async def compare_and_update_agent(self, agent_id, *, expected, updates):
            self.cleared.append(agent_id)
            return True

    async def _run(occupied: bool) -> tuple[list[str], _Repo, dict[str, int]]:
        repo = _Repo(occupied=occupied)
        monkeypatch.setattr(agent_repository, "AgentRepository", lambda: repo)
        occupants = _Occupants(bound=["s-live"] if occupied else [])
        mgr = RemoteAgentRuntimeManager(sessions_repo=occupants)
        destroyed: list[str] = []

        async def _destroy(sandbox_id: str) -> Any:
            destroyed.append(sandbox_id)
            return SandboxDestruction.confirmed_gone(sandbox_id, detail="gone")

        monkeypatch.setattr(mgr, "destroy_sandbox_by_id", _destroy)
        _answer_census(monkeypatch, mgr, 0)
        summary = await mgr.reap_abandoned_agent_boxes()
        return destroyed, repo, summary

    destroyed, repo, summary = await _run(occupied=False)
    assert destroyed == [BOX]
    assert repo.cleared == ["a1"]
    assert summary["agent_boxes_reaped"] == 1

    destroyed, repo, summary = await _run(occupied=True)
    assert destroyed == []
    assert repo.cleared == []
    assert summary["agent_boxes_kept"] == 1


async def test_a_box_that_still_holds_a_prepared_pair_survives_an_empty_ledger(
    monkeypatch: Any,
) -> None:
    """The box is asked last, and it can contradict every ledger above it.

    Each platform-side lens is written at some point in a placement, so each
    has a window where a conversation is real and unrecorded — and a claim of
    a prepared slot passes through all of them. A box was deleted two seconds
    after one conversation released it while a third conversation's runner
    reported ready inside it in the same second (p168). execd's session table
    has no such window.
    """

    _with_agents(monkeypatch, _Agents())
    _wire_persisted_release(monkeypatch)
    # The departing conversation has already closed its two sessions. The
    # remaining agent+terminal pair is the prepared slot seen in the live E2E.
    mgr, destroyed = _manager(monkeypatch, _Occupants(), live_sessions=2)
    mgr._runtimes["s-census"] = _runtime("s-census")

    verdict = await mgr.terminate_runtime("s-census")

    assert destroyed == []
    assert verdict.outcome == SANDBOX_DESTRUCTION_RETAINED


async def test_a_box_with_no_live_isolated_sessions_is_destroyed(
    monkeypatch: Any,
) -> None:
    """The census must not keep an actually empty box alive forever.

    The departing conversation's agent and terminal sessions are closed before
    occupancy is checked. Zero is therefore the only empty answer; treating two
    as empty deletes a prepared slot's matching pair.
    """

    _with_agents(monkeypatch, _Agents())
    _wire_persisted_release(monkeypatch)
    mgr, destroyed = _manager(
        monkeypatch, _Occupants(), live_sessions=0, destroys=True
    )
    mgr._runtimes["s-alone"] = _runtime("s-alone")

    await mgr.terminate_runtime("s-alone")

    assert destroyed == [BOX]


async def test_a_box_that_will_not_answer_the_census_adds_no_occupant(
    monkeypatch: Any,
) -> None:
    """Silence is not an occupant, and treating it as one leaked boxes.

    This started out asserting the opposite — a silent box was kept, on the
    reasoning that not knowing is not knowing it is empty. The failure mode
    runs the other way: a box whose data plane will not answer is
    overwhelmingly a box that is GONE, and retaining those keeps a dead box
    standing indefinitely — the census is retried on a timer, so each retry
    renews the reprieve — while its pool slot stays unusable.
    """

    _with_agents(monkeypatch, _Agents())
    _wire_persisted_release(monkeypatch)
    mgr, destroyed = _manager(
        monkeypatch, _Occupants(), live_sessions=-1, destroys=True
    )
    mgr._runtimes["s-silent"] = _runtime("s-silent")

    await mgr.terminate_runtime("s-silent")

    assert destroyed == [BOX], "the ledgers said empty; silence must not veto"


async def test_a_silent_box_with_a_bound_sibling_is_still_kept(
    monkeypatch: Any,
) -> None:
    """Silence adds nothing — it does not subtract either.

    The ledgers keep their say, so a box a live session still names survives
    a census that cannot answer.
    """

    _with_agents(monkeypatch, _Agents())
    _wire_persisted_release(monkeypatch)
    mgr, destroyed = _manager(
        monkeypatch, _Occupants(bound=["s-sibling"]), live_sessions=-1
    )
    mgr._runtimes["s-silent-2"] = _runtime("s-silent-2")

    verdict = await mgr.terminate_runtime("s-silent-2")

    assert destroyed == []
    assert verdict.outcome == SANDBOX_DESTRUCTION_RETAINED
