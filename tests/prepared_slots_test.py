"""Prepared-seat ledger: claim atomicity, TTL/orphan reaping, retirement.

The fake agent repository reproduces the document store's exact-match update
semantics, including the behaviour that bit the live path: a filter of
``{field: None}`` does NOT match a row where the field is absent.
"""

from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.agent import prepared_slots as ps
from astrabox.seams.sandbox import SandboxMemoryHeadroom


class FakeAgentRepo:
    def __init__(self, row: dict[str, Any]) -> None:
        self.rows = {str(row["agent_id"]): dict(row)}

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        row = self.rows.get(agent_id)
        return copy.deepcopy(row) if row is not None else None

    async def update_agent(self, agent_id: str, updates: dict[str, Any]) -> bool:
        row = self.rows.get(agent_id)
        if row is None:
            return False
        row.update(updates)
        return True

    async def compare_and_update_agent(
        self,
        agent_id: str,
        *,
        expected: dict[str, Any],
        updates: dict[str, Any],
    ) -> bool:
        row = self.rows.get(agent_id)
        if row is None:
            return False
        for key, value in expected.items():
            if value == {"$exists": False}:
                if key in row:
                    return False
                continue
            # Exact-match against the PRESENT value: an absent key matches
            # nothing, not even None — the live store's behaviour.
            if key not in row or row[key] != value:
                return False
        row.update(updates)
        return True


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _manifest(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "slot_id": "slot-abc",
        "state": "prepared",
        "engine_kind": "claude_code",
        "sandbox_id": "box-1",
        "isolated_session_id": "iso-1",
        "terminal_isolated_session_id": "iso-2",
        "uid": 2000,
        "gid": 2000,
        "home_dir": "/home/conversations/conv_x",
        "workspace_dir": "/workspace",
        "workspace_source_dir": "/home/conversations/conv_x/workspace",
        "runner_port": 9001,
        "runtime_generation": "generation-1",
        "spawn_fingerprint": "sf-1",
        "activation_mcp_servers": [],
        "runtime_identity": {"isolated_session_id": "iso-1"},
        "prepared_at": _iso(datetime.now(timezone.utc)),
    }
    base.update(overrides)
    return base


def _row(manifest: dict[str, Any] | None, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"agent_id": "agent-1"}
    if manifest is not None or "with_field" in overrides:
        row[ps.PREPARED_SLOTS_FIELD] = [] if manifest is None else [manifest]
    overrides.pop("with_field", None)
    row.update(overrides)
    return row


def _slots(repo: Any, agent_id: str = "agent-1") -> list[dict[str, Any]]:
    return list(repo.rows[agent_id].get(ps.PREPARED_SLOTS_FIELD) or [])


async def _claim_one(
    *, agent_id: str, session_id: str, expected_runtime_generation: str, agent_repo: Any,
) -> dict[str, Any] | None:
    claimed = await ps.claim_prepared_slots(
        agent_id=agent_id,
        session_ids=[session_id],
        expected_runtime_generation=expected_runtime_generation,
        agent_repo=agent_repo,
    )
    return claimed.get(session_id)


# ── claim ─────────────────────────────────────────────────────────────────


def test_concurrent_claims_have_exactly_one_winner(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = FakeAgentRepo(_row(_manifest()))
    monkeypatch.setattr(ps, "sandbox_for_name", lambda _name: SimpleNamespace(
        name="fake",
        read_memory_headroom=AsyncMock(
            return_value=SandboxMemoryHeadroom(limit_bytes=4 * 1024**3, current_bytes=0)
        ),
    ))

    async def run() -> list[dict[str, Any] | None]:
        return list(
            await asyncio.gather(
                *(
                    _claim_one(
                        agent_id="agent-1",
                        session_id=f"session-{i}",
                        expected_runtime_generation="generation-1",
                        agent_repo=repo,
                    )
                    for i in range(4)
                )
            )
        )

    results = asyncio.run(run())
    winners = [r for r in results if r is not None]
    assert len(winners) == 1, results
    stored = _slots(repo)[0]
    assert stored["state"] == "claimed"
    assert stored["claimed_session_id"] == winners[0]["claimed_session_id"]


def test_claim_refuses_stale_generation_and_expired_slot() -> None:
    stale = FakeAgentRepo(_row(_manifest(runtime_generation="generation-old")))
    expired = FakeAgentRepo(
        _row(
            _manifest(
                prepared_at=_iso(
                    datetime.now(timezone.utc)
                    - timedelta(seconds=ps.prepared_seat_ttl_seconds() + 60)
                )
            )
        )
    )

    async def run() -> tuple[Any, Any]:
        a = await _claim_one(
            agent_id="agent-1",
            session_id="s1",
            expected_runtime_generation="generation-1",
            agent_repo=stale,
        )
        b = await _claim_one(
            agent_id="agent-1",
            session_id="s1",
            expected_runtime_generation="generation-1",
            agent_repo=expired,
        )
        return a, b

    a, b = asyncio.run(run())
    assert a is None and b is None
    # Neither refusal mutates the manifest — destruction belongs to the reaper.
    assert _slots(stale)[0]["state"] == "prepared"
    assert _slots(expired)[0]["state"] == "prepared"


@pytest.mark.parametrize(
    ("case", "level", "phrase"),
    [
        ("no_generation", "WARNING", "carries no runtime generation"),
        ("stale_manifest", "WARNING", "stale generation"),
        ("no_manifest", "INFO", "no prepared unit"),
        ("expired", "INFO", "outlived their idle lease"),
    ],
)
def test_every_claim_miss_names_its_own_reason(
    case: str, level: str, phrase: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The caller sees one None for four different situations.

    Going cold is a legitimate outcome of all of them, so none of these
    raises. But two are contradictions — an Agent that wants prewarm with no
    pool identity, and a prepared unit nothing reaped — and an operator who
    can only see "this Agent stopped being fast" has to read the row out of
    the database by hand to tell which one happened. The severity is part of
    the contract: a first claim finding nothing prepared is ordinary, an
    Agent that lost its runtime generation is not.
    """

    expected_generation = "generation-1"
    if case == "no_generation":
        repo = FakeAgentRepo(_row(_manifest()))
        expected_generation = ""
    elif case == "stale_manifest":
        repo = FakeAgentRepo(_row(_manifest(runtime_generation="generation-old")))
    elif case == "no_manifest":
        repo = FakeAgentRepo(_row(None))
    else:
        repo = FakeAgentRepo(
            _row(
                _manifest(
                    prepared_at=_iso(
                        datetime.now(timezone.utc)
                        - timedelta(seconds=ps.prepared_seat_ttl_seconds() + 60)
                    )
                )
            )
        )

    with caplog.at_level("INFO", logger=ps.logger.name):
        claimed = asyncio.run(
            _claim_one(
                agent_id="agent-1",
                session_id="s1",
                expected_runtime_generation=expected_generation,
                agent_repo=repo,
            )
        )

    assert claimed is None
    matching = [r for r in caplog.records if phrase in r.getMessage()]
    assert len(matching) == 1, [r.getMessage() for r in caplog.records]
    assert matching[0].levelname == level


def test_clear_claimed_slot_only_clears_its_own_slot() -> None:
    claimed = _manifest(state="claimed", claimed_session_id="s1")
    repo = FakeAgentRepo(_row(claimed))

    async def run() -> None:
        await ps.clear_claimed_slot(
            agent_id="agent-1", slot_id="slot-OTHER", agent_repo=repo
        )
        assert _slots(repo) != []
        await ps.clear_claimed_slot(
            agent_id="agent-1", slot_id="slot-abc", agent_repo=repo
        )
        assert _slots(repo) == []

    asyncio.run(run())


# ── reap policy ───────────────────────────────────────────────────────────


def test_reapable_matrix() -> None:
    fresh = _manifest()
    assert (
        ps._manifest_is_reapable(
            fresh, current_runtime_generation="generation-1"
        )
        is None
    )

    stale_generation = _manifest(runtime_generation="generation-old")
    assert ps._manifest_is_reapable(
        stale_generation, current_runtime_generation="generation-1"
    )

    expired = _manifest(
        prepared_at=_iso(
            datetime.now(timezone.utc)
            - timedelta(seconds=ps.prepared_seat_ttl_seconds() + 1)
        )
    )
    assert ps._manifest_is_reapable(
        expired, current_runtime_generation="generation-1"
    )

    unparsable = _manifest(prepared_at="not-a-timestamp")
    assert ps._manifest_is_reapable(
        unparsable, current_runtime_generation="generation-1"
    )

    # A seat inside its idle lease is left alone, however close to its end.
    due_soon = _manifest(
        prepared_at=_iso(
            datetime.now(timezone.utc)
            - timedelta(seconds=ps.prepared_seat_ttl_seconds() - 100)
        )
    )
    assert (
        ps._manifest_is_reapable(
            due_soon, current_runtime_generation="generation-1"
        )
        is None
    )

    young_claim = _manifest(
        state="claimed",
        claimed_session_id="s1",
        claimed_at=_iso(datetime.now(timezone.utc)),
    )
    assert (
        ps._manifest_is_reapable(
            young_claim, current_runtime_generation="generation-1"
        )
        is None
    )

    orphan_claim = _manifest(
        state="claimed",
        claimed_session_id="s1",
        claimed_at=_iso(
            datetime.now(timezone.utc)
            - timedelta(seconds=ps.CLAIMED_SLOT_ORPHAN_SECONDS + 1)
        ),
    )
    assert ps._manifest_is_reapable(
        orphan_claim, current_runtime_generation="generation-1"
    )

    unknown = _manifest(state="weird")
    assert ps._manifest_is_reapable(
        unknown, current_runtime_generation="generation-1"
    )


class _Template:
    agent_id = "agent-1"
    engine_kind = "claude_code"
    runtime_generation = "generation-1"


@pytest.mark.parametrize(
    "reason",
    [
        "Agent runtime generation changed",
        "Agent runtime preparation is disabled",
    ],
)
def test_config_retirement_preserves_a_fresh_claim_handoff(
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claimed = _manifest(
        state="claimed",
        claimed_session_id="session-starting",
        claimed_at=_iso(datetime.now(timezone.utc)),
    )
    repo = FakeAgentRepo(_row(dict(claimed)))

    async def forbidden_retirement(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a fresh Session claim entered resource retirement")

    monkeypatch.setattr(ps, "_retire_manifest", forbidden_retirement)

    asyncio.run(
        ps.retire_prepared_runtime(
            "agent-1",
            reason=reason,
            agent_repo=repo,
        )
    )

    assert _slots(repo) == [claimed]


def test_config_retirement_losing_to_claim_cas_does_not_destroy_the_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _manifest()
    claimed = {
        **prepared,
        "state": "claimed",
        "claimed_session_id": "session-starting",
        "claimed_at": _iso(datetime.now(timezone.utc)),
    }

    class ClaimWinsRepo(FakeAgentRepo):
        async def compare_and_update_agent(
            self,
            agent_id: str,
            *,
            expected: dict[str, Any],
            updates: dict[str, Any],
        ) -> bool:
            _ = updates
            if expected == {ps.PREPARED_SLOTS_FIELD: [prepared]}:
                self.rows[agent_id][ps.PREPARED_SLOTS_FIELD] = [dict(claimed)]
                return False
            return await super().compare_and_update_agent(
                agent_id,
                expected=expected,
                updates=updates,
            )

    repo = ClaimWinsRepo(_row(dict(prepared)))
    discards: list[str] = []

    async def forbidden_discard(
        _template: Any,
        manifest: dict[str, Any],
        **_kwargs: Any,
    ) -> None:
        discards.append(str(manifest.get("slot_id") or ""))

    monkeypatch.setattr(ps, "discard_prepared_slot", forbidden_discard)

    asyncio.run(
        ps.retire_prepared_runtime(
            "agent-1",
            reason="Agent runtime generation changed",
            agent_repo=repo,
        )
    )

    assert _slots(repo) == [claimed]
    assert discards == []


def test_failed_config_retirement_keeps_its_cleanup_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _manifest()
    repo = FakeAgentRepo(_row(dict(prepared)))

    async def fail_discard(
        _template: Any,
        _manifest_arg: dict[str, Any],
        **_kwargs: Any,
    ) -> None:
        raise APIError(
            code="SANDBOX_RELEASE_FAILED",
            message="the provider did not confirm release",
            status_code=502,
        )

    monkeypatch.setattr(ps, "discard_prepared_slot", fail_discard)

    with pytest.raises(APIError, match="did not confirm release"):
        asyncio.run(
            ps.retire_prepared_runtime(
                "agent-1",
                reason="Agent runtime generation changed",
                agent_repo=repo,
            )
        )

    retained = _slots(repo)[0]
    assert retained["state"] == "retiring"
    assert retained["slot_id"] == prepared["slot_id"]
    assert retained["retire_reason"] == "Agent runtime generation changed"


def test_reap_orphaned_claim_keeps_adopted_placement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An adopted orphan loses only its manifest; an unadopted one is destroyed."""

    orphan = _manifest(
        state="claimed",
        claimed_session_id="s1",
        claimed_at=_iso(
            datetime.now(timezone.utc)
            - timedelta(seconds=ps.CLAIMED_SLOT_ORPHAN_SECONDS + 1)
        ),
    )
    discards: list[str] = []

    async def fake_discard(template: Any, manifest: dict[str, Any], **kw: Any) -> None:
        discards.append(str(manifest.get("slot_id")))
        repo = kw.get("agent_repo")
        await ps._change_slot(repo, "agent-1", manifest, None)

    monkeypatch.setattr(ps, "discard_prepared_slot", fake_discard)

    # Adopted: the Session row records the slot's isolated session.
    adopted_repo = FakeAgentRepo(_row(dict(orphan)))

    async def adopted(_manifest_arg: dict[str, Any]) -> bool:
        return True

    monkeypatch.setattr(ps, "_claimed_session_adopted_placement", adopted)
    reason = asyncio.run(
        ps.reap_stale_slot_manifests(_Template(), agent_repo=adopted_repo)
    )
    assert reason
    assert _slots(adopted_repo) == []
    assert discards == []

    # Not adopted: the placement itself is the leak and must be destroyed.
    lost_repo = FakeAgentRepo(_row(dict(orphan)))

    async def not_adopted(_manifest_arg: dict[str, Any]) -> bool:
        return False

    monkeypatch.setattr(ps, "_claimed_session_adopted_placement", not_adopted)
    reason = asyncio.run(
        ps.reap_stale_slot_manifests(_Template(), agent_repo=lost_repo)
    )
    assert reason
    assert discards == ["slot-abc"]


def test_failed_slot_release_keeps_the_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ReleaseFails:
        name = "fake"

        async def close_isolated_session(
            self, sandbox_id: str, session_id: str
        ) -> None:
            raise APIError(
                code="SANDBOX_RELEASE_FAILED",
                message=f"could not close {sandbox_id}/{session_id}",
                status_code=502,
            )

    manifest = _manifest(sandbox_backend="fake")
    repo = FakeAgentRepo(_row(manifest))
    monkeypatch.setattr(ps, "sandbox_for_name", lambda name: _ReleaseFails())

    with pytest.raises(APIError, match="could not close"):
        asyncio.run(
            ps.discard_prepared_slot(
                "agent-1", manifest, reason="activation failed", agent_repo=repo
            )
        )

    assert _slots(repo) == [manifest]


# ── gateway-key bookkeeping ───────────────────────────────────────────────


def _gateway_credential_request() -> Any:
    from astrabox.core.service.orchestrator.engine.provisioning import (
        ModelCredentialRequest,
    )
    from astrabox.seams.model import ResolvedModelAccess

    return ModelCredentialRequest(
        access=ResolvedModelAccess(
            configuration={"model": "test-model"},
            base_url="https://gateway.test",
            model_name="test-model",
            credential="sk-shared-key",
            credential_kind="bearer",
            endpoint_provider="test",
        ),
        request_paths=("v1/*",),
        missing_code="AGENT_RUNTIME_ERROR",
        missing_message="model access is missing",
    )


def _gateway_template() -> Any:
    return SimpleNamespace(
        agent_id="agent-1",
        engine_kind="claude_code",
        model_config={"endpoint_provider": "test"},
        networking={"type": "unrestricted"},
        mcp_servers={},
        packages=[],
        credential_vault_ids=[],
    )


def test_gateway_append_retries_without_overwriting_a_concurrent_claim() -> None:
    """A claim may append its entry while another Session claims a sibling."""

    sibling = {"slot_id": "slot-published", "session_id": None}

    class ClaimWinsFirst(FakeAgentRepo):
        raced = False

        async def compare_and_update_agent(
            self,
            agent_id: str,
            *,
            expected: dict[str, Any],
            updates: dict[str, Any],
        ) -> bool:
            desired = updates.get(ps.GATEWAY_ENTRIES_FIELD)
            if not self.raced and isinstance(desired, list) and len(desired) == 2:
                self.raced = True
                self.rows[agent_id][ps.GATEWAY_ENTRIES_FIELD] = [
                    {**sibling, "session_id": "session-claimed"}
                ]
                return False
            return await super().compare_and_update_agent(
                agent_id,
                expected=expected,
                updates=updates,
            )

    repo = ClaimWinsFirst(
        {
            "agent_id": "agent-1",
            ps.PREPARED_SLOTS_FIELD: [{"slot_id": "slot-published"}],
            ps.GATEWAY_ENTRIES_FIELD: [sibling],
        }
    )

    asyncio.run(
        ps.record_gateway_entry(repo, "agent-1", slot_id="slot-new", session_id="session-new")
    )

    assert repo.raced is True
    assert repo.rows["agent-1"][ps.GATEWAY_ENTRIES_FIELD] == [
        {"slot_id": "slot-published", "session_id": "session-claimed"},
        {"slot_id": "slot-new", "session_id": "session-new"},
    ]


def test_pruning_keeps_the_published_slot_the_caller_did_not_name() -> None:
    """A published, unclaimed slot must keep its future key-GC bookkeeping."""

    repo = FakeAgentRepo(
        {
            "agent_id": "agent-1",
            ps.PREPARED_SLOTS_FIELD: [{"slot_id": "slot-published"}],
            ps.GATEWAY_ENTRIES_FIELD: [
                {"slot_id": "slot-published", "session_id": None},
                {"slot_id": "slot-preparing", "session_id": None},
                {"slot_id": "slot-retired", "session_id": None},
            ],
        }
    )

    async def prune() -> list[dict[str, Any]]:
        keep = {"slot-preparing"} | await ps._published_manifest_slot(repo, "agent-1")
        return await ps._prune_gateway_entries(repo, "agent-1", keep_slots=keep)

    slots = sorted(str(entry["slot_id"]) for entry in asyncio.run(prune()))
    assert slots == ["slot-preparing", "slot-published"]
    # An unclaimed slot nothing names is still collected: this must not become
    # "keep everything", which would leak an entry per prepared slot forever.
    assert "slot-retired" not in slots


def test_prune_releases_a_session_key_only_after_its_delete_cas_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class ConcurrentAppendRepo(FakeAgentRepo):
        raced = False

        async def compare_and_update_agent(
            self,
            agent_id: str,
            *,
            expected: dict[str, Any],
            updates: dict[str, Any],
        ) -> bool:
            if not self.raced:
                self.raced = True
                events.append("lost-cas")
                self.rows[agent_id][ps.GATEWAY_ENTRIES_FIELD].append(
                    {"slot_id": "slot-live", "session_id": None}
                )
                return False
            won = await super().compare_and_update_agent(
                agent_id,
                expected=expected,
                updates=updates,
            )
            if won:
                events.append("won-cas")
            return won

    class TerminalSessions:
        async def get_session(self, session_id: str) -> dict[str, str]:
            assert session_id == "session-ended"
            return {"state": "TERMINATED"}

    class Endpoint:
        async def release_session_credential(self, *, context: Any) -> bool:
            assert context.conversation_id == "session-ended"
            events.append("released")
            return True

    from astrabox.persistence.repository import session_repository
    from astrabox.seams import model as model_seam

    monkeypatch.setattr(session_repository, "SessionRepository", TerminalSessions)
    monkeypatch.setattr(model_seam, "model_endpoint_for_name", lambda _name: Endpoint())
    repo = ConcurrentAppendRepo(
        {
            "agent_id": "agent-1",
            ps.GATEWAY_ENTRIES_FIELD: [
                {"slot_id": "slot-ended", "session_id": "session-ended"}
            ],
        }
    )

    kept = asyncio.run(
        ps._prune_gateway_entries(repo, "agent-1", keep_slots={"slot-live"})
    )

    assert kept == [{"slot_id": "slot-live", "session_id": None}]
    assert events == ["lost-cas", "won-cas", "released"]


def test_a_claim_records_its_key_owner_before_ended_sessions_are_collected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released: list[str] = []

    class Sessions:
        async def get_session(self, session_id: str) -> dict[str, str]:
            return {"state": "TERMINATED" if session_id == "session-ended" else "CREATING"}

    class Endpoint:
        async def release_session_credential(self, *, context: Any) -> bool:
            released.append(context.conversation_id)
            return True

    from astrabox.persistence.repository import session_repository
    from astrabox.seams import model as model_seam

    monkeypatch.setattr(session_repository, "SessionRepository", Sessions)
    monkeypatch.setattr(model_seam, "model_endpoint_for_name", lambda _name: Endpoint())
    repo = FakeAgentRepo(
        {
            "agent_id": "agent-1",
            ps.GATEWAY_ENTRIES_FIELD: [{"slot_id": "slot-ended", "session_id": "session-ended"}],
        }
    )
    monkeypatch.setattr(ps, "AgentRepository", lambda: repo)

    async def claim_then_collect() -> list[str]:
        await ps.record_gateway_entry(repo, "agent-1", slot_id="slot-new", session_id="session-new")
        released_during_claim = list(released)
        ps.schedule_gateway_entry_prune("agent-1")
        await asyncio.gather(*list(ps._GATEWAY_PRUNE_TASKS))
        return released_during_claim

    assert asyncio.run(claim_then_collect()) == []
    assert released == ["session-ended"]
    assert repo.rows["agent-1"][ps.GATEWAY_ENTRIES_FIELD] == [
        {"slot_id": "slot-new", "session_id": "session-new"}
    ]


def test_the_published_slot_is_read_from_the_manifest_not_the_caller() -> None:
    """An Agent with no published slot contributes nothing, not a blank id."""

    empty = FakeAgentRepo({"agent_id": "agent-2"})
    assert asyncio.run(ps._published_manifest_slot(empty, "agent-2")) == set()

    blank = FakeAgentRepo(
        {"agent_id": "agent-3", ps.PREPARED_SLOTS_FIELD: [{"slot_id": "  "}]}
    )
    assert asyncio.run(ps._published_manifest_slot(blank, "agent-3")) == set()

    named = FakeAgentRepo(
        {"agent_id": "agent-4", ps.PREPARED_SLOTS_FIELD: [{"slot_id": "slot-x"}]}
    )
    assert asyncio.run(ps._published_manifest_slot(named, "agent-4")) == {"slot-x"}


class _FakeSessions:
    """Just enough of the session store for the allocation release."""

    def __init__(self, allocation: Any) -> None:
        self.row: dict[str, Any] = {"session_id": "s1"}
        if allocation is not None:
            self.row["startup_allocation"] = allocation
        self.cleared: list[dict[str, Any]] = []

    async def get_session_including_deleted(self, session_id: str) -> dict[str, Any]:
        return dict(self.row)

    async def clear_startup_allocation(
        self, session_id: str, *, allocation: dict[str, Any]
    ) -> bool:
        self.cleared.append(dict(allocation))
        return True


def _with_sessions(monkeypatch: Any, sessions: _FakeSessions) -> None:
    from astrabox.persistence.repository import session_repository

    monkeypatch.setattr(
        session_repository, "SessionRepository", lambda: sessions
    )


def test_discarding_a_slot_releases_the_allocation_it_recorded(
    monkeypatch: Any,
) -> None:
    """Otherwise the cold start it falls back to dies on the conflict.

    Recording a startup allocation is a single-value contract — a second,
    different one for the same Session is refused so a retry cannot overwrite
    the last durable address of the first — so a discarded slot that keeps its
    allocation makes the fallback unreachable in exactly the case it exists
    for (p169).
    """

    allocation = {"sandbox_id": "box-1", "backend": "open_sandbox"}
    sessions = _FakeSessions(allocation)
    _with_sessions(monkeypatch, sessions)

    asyncio.run(
        ps.release_claimed_slot_allocation("s1", {"sandbox_id": "box-1"})
    )

    assert sessions.cleared == [allocation]


def test_an_allocation_naming_another_box_is_left_alone(
    monkeypatch: Any,
) -> None:
    """A placement that has moved on owns what it recorded.

    Clearing a stranger's allocation erases the last durable address of a
    live resource, which is the leak the single-value contract prevents.
    """

    sessions = _FakeSessions({"sandbox_id": "box-2"})
    _with_sessions(monkeypatch, sessions)

    asyncio.run(
        ps.release_claimed_slot_allocation("s1", {"sandbox_id": "box-1"})
    )

    assert sessions.cleared == []


def test_no_allocation_at_all_is_not_an_error(monkeypatch: Any) -> None:
    """The slot may have failed before it recorded anything."""

    sessions = _FakeSessions(None)
    _with_sessions(monkeypatch, sessions)

    asyncio.run(
        ps.release_claimed_slot_allocation("s1", {"sandbox_id": "box-1"})
    )

    assert sessions.cleared == []


def _capacity_claim(repo: FakeAgentRepo) -> dict[str, Any] | None:
    return asyncio.run(_claim_one(
        agent_id="agent-1", session_id="session-1",
        expected_runtime_generation="generation-1", agent_repo=repo,
    ))


def _capacity_fixture(
    monkeypatch: pytest.MonkeyPatch, *, free_reservations: int,
    admissions: list[dict[str, Any]],
) -> tuple[FakeAgentRepo, AsyncMock]:
    from astrabox.core.service.orchestrator.runtime.shared_sandbox_lease import (
        CONVERSATION_MEMORY_RESERVE_BYTES,
    )

    manifest = _manifest(placement="shared_slot", sandbox_backend="fake")
    repo = FakeAgentRepo(_row(
        manifest, sandbox_id="box-1", sandbox_backend="fake", box_admissions=admissions,
    ))
    memory = AsyncMock(return_value=SandboxMemoryHeadroom(
        limit_bytes=512 * 1024**2 + free_reservations * CONVERSATION_MEMORY_RESERVE_BYTES,
        current_bytes=512 * 1024**2,
    ))
    monkeypatch.setattr(ps, "sandbox_for_name", lambda _name: SimpleNamespace(
        name="fake", read_memory_headroom=memory,
    ))
    return repo, memory


def test_a_full_box_withdraws_its_prepared_slot_without_releasing_siblings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission = {"sandbox_id": "box-1", "session_id": "slot-abc", "at": ps.time.time()}
    repo, memory = _capacity_fixture(monkeypatch, free_reservations=0, admissions=[admission])
    assert _capacity_claim(repo) is None
    row = repo.rows["agent-1"]
    assert row["sandbox_id"] == "box-1"
    assert row[ps.BOX_ADMISSIONS] == [admission]
    manifest = _slots(repo)[0]
    assert manifest["state"] == "retiring"
    assert manifest["isolated_session_id"] == "iso-1"
    assert "claimed_session_id" not in manifest
    memory.assert_awaited_once_with("box-1")


@pytest.mark.parametrize("reservation_age", [1, 300])
def test_claim_refreshes_its_own_reservation_without_charging_it_twice(
    monkeypatch: pytest.MonkeyPatch, reservation_age: int,
) -> None:
    before = ps.time.time()
    admission = {"sandbox_id": "box-1", "session_id": "slot-abc", "at": before - reservation_age}
    repo, memory = _capacity_fixture(monkeypatch, free_reservations=1, admissions=[admission])
    assert _capacity_claim(repo) is not None
    entries = repo.rows["agent-1"][ps.BOX_ADMISSIONS]
    assert len(entries) == 1
    assert entries[0]["session_id"] == "session-1"
    assert entries[0]["sandbox_id"] == "box-1"
    assert entries[0]["at"] >= before
    memory.assert_awaited_once_with("box-1")


@pytest.mark.parametrize("sibling", ["another-session", None])
def test_claim_keeps_other_young_admissions_reserved(
    monkeypatch: pytest.MonkeyPatch, sibling: str | None,
) -> None:
    pending = {"sandbox_id": "box-1", "at": ps.time.time()}
    if sibling is not None:
        pending["session_id"] = sibling
    repo, _memory = _capacity_fixture(monkeypatch, free_reservations=1, admissions=[pending])
    assert _capacity_claim(repo) is None
    assert repo.rows["agent-1"][ps.BOX_ADMISSIONS] == [pending]
    assert _slots(repo)[0]["state"] == "retiring"


def test_claim_rechecks_memory_after_another_admission_wins_the_cas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, memory = _capacity_fixture(monkeypatch, free_reservations=1, admissions=[])
    original = repo.compare_and_update_agent

    async def cas(agent_id: str, *, expected: dict[str, Any], updates: dict[str, Any]) -> bool:
        if not repo.rows[agent_id][ps.BOX_ADMISSIONS]:
            repo.rows[agent_id][ps.BOX_ADMISSIONS] = [{
                "sandbox_id": "box-1", "session_id": "sibling", "at": ps.time.time(),
            }]
        return await original(agent_id, expected=expected, updates=updates)

    repo.compare_and_update_agent = cas  # type: ignore[method-assign]
    assert _capacity_claim(repo) is None
    assert memory.await_count == 2
    assert _slots(repo)[0]["state"] == "retiring"
    assert [e["session_id"] for e in repo.rows["agent-1"][ps.BOX_ADMISSIONS]] == ["sibling"]


def test_capacity_refusal_cannot_retire_a_slot_claimed_by_another_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, memory = _capacity_fixture(monkeypatch, free_reservations=0, admissions=[])
    manifest = _slots(repo)[0]
    winner = {**manifest, "state": "claimed", "claimed_session_id": "winner"}

    async def read_memory(_box: str) -> SandboxMemoryHeadroom:
        repo.rows["agent-1"][ps.PREPARED_SLOTS_FIELD] = [winner]
        return SandboxMemoryHeadroom(limit_bytes=512 * 1024**2, current_bytes=512 * 1024**2)

    memory.side_effect = read_memory
    assert _capacity_claim(repo) is None
    assert _slots(repo) == [winner]


def test_unexpected_capacity_probe_failure_preserves_the_prepared_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, memory = _capacity_fixture(monkeypatch, free_reservations=1, admissions=[])
    manifest = copy.deepcopy(_slots(repo)[0])
    memory.side_effect = RuntimeError("memory probe contract broken")
    with pytest.raises(RuntimeError, match="memory probe contract broken"):
        _capacity_claim(repo)
    assert _slots(repo) == [manifest]
    assert repo.rows["agent-1"][ps.BOX_ADMISSIONS] == []


def test_slot_expiring_during_the_capacity_probe_is_not_claimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return now

    monkeypatch.setattr(ps, "datetime", Clock)
    repo, memory = _capacity_fixture(monkeypatch, free_reservations=1, admissions=[])
    manifest = repo.rows["agent-1"][ps.PREPARED_SLOTS_FIELD][0]
    manifest["prepared_at"] = now.isoformat()

    async def delayed_probe(_box: str) -> SandboxMemoryHeadroom:
        nonlocal now
        now += timedelta(seconds=ps.prepared_seat_ttl_seconds() + 1)
        return SandboxMemoryHeadroom(limit_bytes=4 * 1024**3, current_bytes=0)

    memory.side_effect = delayed_probe
    assert _capacity_claim(repo) is None
    assert _slots(repo)[0]["state"] == "prepared"
    assert repo.rows["agent-1"][ps.BOX_ADMISSIONS] == []
