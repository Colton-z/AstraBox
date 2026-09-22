"""Prepared-slot allocator: claim atomicity, TTL/orphan reaping, publish CAS.

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
from unittest.mock import AsyncMock

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.agent import prepared_slots as ps


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


class _InitialSlotLease:
    def __init__(self, *, winner: str = "pool-box") -> None:
        self.winner = winner
        self.candidates: list[str | None] = []
        self.session_ids: list[str | None] = []
        self.released: list[str] = []

    async def place_in_agent_box(self, **kwargs: Any) -> Any:
        candidate = kwargs.get("candidate")
        self.candidates.append(candidate)
        self.session_ids.append(kwargs.get("session_id"))
        if candidate is None:
            return None
        return SimpleNamespace(sandbox_id=self.winner)

    async def release(self, binding: Any) -> None:
        self.released.append(str(binding.sandbox_id))


class _InitialSlotProvider:
    def __init__(self, *, destruction_confirmed: bool = True) -> None:
        self.destruction_confirmed = destruction_confirmed
        self.destroyed: list[str] = []

    async def confirm_destroyed(self, sandbox_id: str) -> Any:
        self.destroyed.append(sandbox_id)
        return SimpleNamespace(
            confirmed=self.destruction_confirmed,
            detail="gone" if self.destruction_confirmed else "still visible",
        )


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
        "activation_token": "tok",
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
        row[ps.PREPARED_SLOT_FIELD] = manifest
    overrides.pop("with_field", None)
    row.update(overrides)
    return row


# ── foreground-start priority over shared-box refill ──────────────────────


def test_initial_shared_slot_waits_for_sdk_member_then_places_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrabox.core.service.orchestrator.agent import client_pool

    handle = SimpleNamespace(sandbox_id="pool-box")
    claim = SimpleNamespace(sandbox=handle, sandbox_id="pool-box")
    acquire = AsyncMock(side_effect=[None, claim])
    lease = _InitialSlotLease()
    provider = _InitialSlotProvider()
    template = SimpleNamespace(agent_id="agent-1")
    repo = FakeAgentRepo({"agent_id": "agent-1"})

    monkeypatch.setattr(client_pool, "acquire_agent_client_pool", acquire)
    monkeypatch.setattr(ps.asyncio, "sleep", AsyncMock())

    binding, pooled_handle = asyncio.run(
        ps._place_initial_shared_slot(
            template,
            runtime_manager="runtime-manager",
            repo=repo,
            provider=provider,
            lease=lease,
            identity={
                "home_dir": "/home/conversations/slot-1",
                "workspace_dir": "/workspace",
                "workspace_source_dir": "/home/conversations/slot-1/workspace",
            },
            slot_id="slot-1",
            generation="generation-1",
        )
    )

    assert binding.sandbox_id == "pool-box"
    assert pooled_handle is handle
    assert lease.candidates == [None, None, "pool-box"]
    assert lease.session_ids == ["slot-1", "slot-1", "slot-1"]
    assert acquire.await_count == 2
    assert all(call.kwargs["session_id"] is None for call in acquire.await_args_list)
    assert all(call.kwargs["assignment_id"] == "slot-1" for call in acquire.await_args_list)
    assert provider.destroyed == []


def test_initial_shared_slot_discards_pool_candidate_lost_to_resident(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrabox.core.service.orchestrator.agent import client_pool

    handle = SimpleNamespace(sandbox_id="pool-box")
    lease = _InitialSlotLease(winner="resident-box")
    provider = _InitialSlotProvider()
    monkeypatch.setattr(
        client_pool,
        "acquire_agent_client_pool",
        AsyncMock(
            return_value=SimpleNamespace(sandbox=handle, sandbox_id="pool-box")
        ),
    )

    binding, pooled_handle = asyncio.run(
        ps._place_initial_shared_slot(
            SimpleNamespace(agent_id="agent-1"),
            runtime_manager=object(),
            repo=FakeAgentRepo({"agent_id": "agent-1"}),
            provider=provider,
            lease=lease,
            identity={
                "home_dir": "/home/conversations/slot-1",
                "workspace_dir": "/workspace",
                "workspace_source_dir": "/home/conversations/slot-1/workspace",
            },
            slot_id="slot-1",
            generation="generation-1",
        )
    )

    assert binding.sandbox_id == "resident-box"
    assert pooled_handle is None
    assert lease.candidates == [None, "pool-box"]
    assert provider.destroyed == ["pool-box"]


def test_initial_shared_slot_releases_resident_when_loser_cleanup_is_unconfirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrabox.core.service.orchestrator.agent import client_pool

    lease = _InitialSlotLease(winner="resident-box")
    provider = _InitialSlotProvider(destruction_confirmed=False)
    monkeypatch.setattr(
        client_pool,
        "acquire_agent_client_pool",
        AsyncMock(
            return_value=SimpleNamespace(
                sandbox=SimpleNamespace(sandbox_id="pool-box"),
                sandbox_id="pool-box",
            )
        ),
    )

    with pytest.raises(APIError) as caught:
        asyncio.run(
            ps._place_initial_shared_slot(
                SimpleNamespace(agent_id="agent-1"),
                runtime_manager=object(),
                repo=FakeAgentRepo({"agent_id": "agent-1"}),
                provider=provider,
                lease=lease,
                identity={
                    "home_dir": "/home/conversations/slot-1",
                    "workspace_dir": "/workspace",
                    "workspace_source_dir": (
                        "/home/conversations/slot-1/workspace"
                    ),
                },
                slot_id="slot-1",
                generation="generation-1",
            )
        )

    assert caught.value.code == "SANDBOX_CLEANUP_UNCONFIRMED"
    assert caught.value.data["leaked_sandbox_id"] == "pool-box"
    assert provider.destroyed == ["pool-box"]
    assert lease.released == ["resident-box"]


def test_initial_shared_slot_fails_when_supplier_publishes_no_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrabox.core.service.orchestrator.agent import client_pool

    monkeypatch.setattr(
        client_pool,
        "agent_client_pool_first_member_timeout_seconds",
        lambda: 0.0,
    )
    monkeypatch.setattr(
        client_pool,
        "acquire_agent_client_pool",
        AsyncMock(return_value=None),
    )

    with pytest.raises(APIError) as caught:
        asyncio.run(
            ps._place_initial_shared_slot(
                SimpleNamespace(agent_id="agent-1"),
                runtime_manager=object(),
                repo=FakeAgentRepo({"agent_id": "agent-1"}),
                provider=_InitialSlotProvider(),
                lease=_InitialSlotLease(),
                identity={
                    "home_dir": "/home/conversations/slot-1",
                    "workspace_dir": "/workspace",
                    "workspace_source_dir": "/home/conversations/slot-1/workspace",
                },
                slot_id="slot-1",
                generation="generation-1",
            )
        )

    assert caught.value.code == "SANDBOX_CLIENT_POOL_UNAVAILABLE"
    assert "published no box" in caught.value.message


# ── claim ─────────────────────────────────────────────────────────────────


def test_concurrent_claims_have_exactly_one_winner() -> None:
    repo = FakeAgentRepo(_row(_manifest()))

    async def run() -> list[dict[str, Any] | None]:
        return list(
            await asyncio.gather(
                *(
                    ps.claim_prepared_slot(
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
    stored = repo.rows["agent-1"][ps.PREPARED_SLOT_FIELD]
    assert stored["state"] == "claimed"
    assert stored["claimed_session_id"] == winners[0]["claimed_session_id"]


def test_claim_refuses_stale_generation_and_expired_slot() -> None:
    stale = FakeAgentRepo(_row(_manifest(runtime_generation="generation-old")))
    expired = FakeAgentRepo(
        _row(
            _manifest(
                prepared_at=_iso(
                    datetime.now(timezone.utc)
                    - timedelta(seconds=ps.PREPARED_SLOT_TTL_SECONDS + 60)
                )
            )
        )
    )

    async def run() -> tuple[Any, Any]:
        a = await ps.claim_prepared_slot(
            agent_id="agent-1",
            session_id="s1",
            expected_runtime_generation="generation-1",
            agent_repo=stale,
        )
        b = await ps.claim_prepared_slot(
            agent_id="agent-1",
            session_id="s1",
            expected_runtime_generation="generation-1",
            agent_repo=expired,
        )
        return a, b

    a, b = asyncio.run(run())
    assert a is None and b is None
    # Neither refusal mutates the manifest — destruction belongs to the reaper.
    assert stale.rows["agent-1"][ps.PREPARED_SLOT_FIELD]["state"] == "prepared"
    assert expired.rows["agent-1"][ps.PREPARED_SLOT_FIELD]["state"] == "prepared"


@pytest.mark.parametrize(
    ("case", "level", "phrase"),
    [
        ("no_generation", "WARNING", "carries no runtime generation"),
        ("stale_manifest", "WARNING", "stale generation"),
        ("no_manifest", "INFO", "no prepared unit"),
        ("expired", "INFO", "exceeded its TTL"),
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
                        - timedelta(seconds=ps.PREPARED_SLOT_TTL_SECONDS + 60)
                    )
                )
            )
        )

    with caplog.at_level("INFO", logger=ps.logger.name):
        claimed = asyncio.run(
            ps.claim_prepared_slot(
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
        assert repo.rows["agent-1"][ps.PREPARED_SLOT_FIELD] is not None
        await ps.clear_claimed_slot(
            agent_id="agent-1", slot_id="slot-abc", agent_repo=repo
        )
        assert repo.rows["agent-1"][ps.PREPARED_SLOT_FIELD] is None

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
            - timedelta(seconds=ps.PREPARED_SLOT_TTL_SECONDS + 1)
        )
    )
    assert ps._manifest_is_reapable(
        expired, current_runtime_generation="generation-1"
    )

    unparsable = _manifest(prepared_at="not-a-timestamp")
    assert ps._manifest_is_reapable(
        unparsable, current_runtime_generation="generation-1"
    )

    # With a renewal lead, a slot that would expire before the next sweep is
    # already the reaper's; one that outlasts the lead is left alone.
    due_soon = _manifest(
        prepared_at=_iso(
            datetime.now(timezone.utc)
            - timedelta(seconds=ps.PREPARED_SLOT_TTL_SECONDS - 100)
        )
    )
    assert ps._manifest_is_reapable(
        due_soon, current_runtime_generation="generation-1", renewal_lead_seconds=300
    ) == "prepared slot is due for renewal before its TTL"
    assert (
        ps._manifest_is_reapable(
            due_soon, current_runtime_generation="generation-1", renewal_lead_seconds=50
        )
        is None
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

    assert repo.rows["agent-1"][ps.PREPARED_SLOT_FIELD] == claimed


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
            if expected == {ps.PREPARED_SLOT_FIELD: prepared}:
                self.rows[agent_id][ps.PREPARED_SLOT_FIELD] = dict(claimed)
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

    assert repo.rows["agent-1"][ps.PREPARED_SLOT_FIELD] == claimed
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

    retained = repo.rows["agent-1"][ps.PREPARED_SLOT_FIELD]
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
        await repo.compare_and_update_agent(
            "agent-1",
            expected={ps.PREPARED_SLOT_FIELD: manifest},
            updates={ps.PREPARED_SLOT_FIELD: None},
        )

    monkeypatch.setattr(ps, "discard_prepared_slot", fake_discard)

    # Adopted: the Session row records the slot's isolated session.
    adopted_repo = FakeAgentRepo(_row(dict(orphan)))

    async def adopted(_manifest_arg: dict[str, Any]) -> bool:
        return True

    monkeypatch.setattr(ps, "_claimed_session_adopted_placement", adopted)
    reason = asyncio.run(
        ps.reap_slot_manifest_if_stale(_Template(), agent_repo=adopted_repo)
    )
    assert reason
    assert adopted_repo.rows["agent-1"][ps.PREPARED_SLOT_FIELD] is None
    assert discards == []

    # Not adopted: the placement itself is the leak and must be destroyed.
    lost_repo = FakeAgentRepo(_row(dict(orphan)))

    async def not_adopted(_manifest_arg: dict[str, Any]) -> bool:
        return False

    monkeypatch.setattr(ps, "_claimed_session_adopted_placement", not_adopted)
    reason = asyncio.run(
        ps.reap_slot_manifest_if_stale(_Template(), agent_repo=lost_repo)
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

    assert repo.rows["agent-1"][ps.PREPARED_SLOT_FIELD] == manifest


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
        plugin_repos=[],
        credential_vault_ids=[],
    )


def test_gateway_append_retries_without_overwriting_a_concurrent_claim() -> None:
    """A refill may append while a Session claims the published sibling."""

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
            ps.PREPARED_SLOT_FIELD: {"slot_id": "slot-published"},
            ps.GATEWAY_ENTRIES_FIELD: [sibling],
        }
    )

    asyncio.run(ps.record_gateway_entry(repo, "agent-1", slot_id="slot-new"))

    assert repo.raced is True
    assert repo.rows["agent-1"][ps.GATEWAY_ENTRIES_FIELD] == [
        {"slot_id": "slot-published", "session_id": "session-claimed"},
        {"slot_id": "slot-new", "session_id": None},
    ]


def test_gateway_claim_retries_without_dropping_a_concurrent_append() -> None:
    """Claim updates the exact list it read, then re-reads after a lost CAS."""

    class AppendWinsFirst(FakeAgentRepo):
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
                self.rows[agent_id][ps.GATEWAY_ENTRIES_FIELD].append(
                    {"slot_id": "slot-sibling", "session_id": None}
                )
                return False
            return await super().compare_and_update_agent(
                agent_id,
                expected=expected,
                updates=updates,
            )

    repo = AppendWinsFirst(
        {
            "agent_id": "agent-1",
            ps.GATEWAY_ENTRIES_FIELD: [
                {"slot_id": "slot-claiming", "session_id": None}
            ],
        }
    )

    asyncio.run(
        ps.mark_gateway_entry_claimed(
            agent_id="agent-1",
            slot_id="slot-claiming",
            session_id="session-1",
            agent_repo=repo,
        )
    )

    assert repo.rows["agent-1"][ps.GATEWAY_ENTRIES_FIELD] == [
        {"slot_id": "slot-claiming", "session_id": "session-1"},
        {"slot_id": "slot-sibling", "session_id": None},
    ]


def test_pruning_keeps_the_published_slot_the_caller_did_not_name() -> None:
    """A published, unclaimed slot must keep its future key-GC bookkeeping."""

    repo = FakeAgentRepo(
        {
            "agent_id": "agent-1",
            ps.PREPARED_SLOT_FIELD: {"slot_id": "slot-published"},
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


def test_shared_prepare_sends_only_the_new_workload_and_keeps_the_mcp_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provider, not a stale Agent ledger, carries live sibling secrets."""

    from astrabox.core.service.orchestrator.engine import provisioning
    from astrabox.core.service.orchestrator.runtime import mcp_credentials
    from astrabox.seams.egress_credentials import (
        MCPHeaderEgressCredential,
        SandboxEgressCredentialPlan,
        workload_credential_name,
    )

    applied: list[SandboxEgressCredentialPlan] = []
    recorded: list[str] = []
    mcp_plan = SandboxEgressCredentialPlan(
        mcp=(
            MCPHeaderEgressCredential(
                name="astrabox-mcp-gateway-test",
                server_url="https://mcp.test/server",
                headers={"Authorization": "Bearer mcp-secret"},
            ),
        )
    )

    class _Handle:
        pass

    class _Provider:
        name = "fake"
        supports_create_network_policy = True
        supports_egress_credential_injection = True

        async def connect(self, sandbox_id: str) -> _Handle:
            assert sandbox_id == "sandbox-1"
            return _Handle()

        async def apply_credential_vault(
            self, handle: Any, *, vault_write: SandboxEgressCredentialPlan
        ) -> None:
            assert isinstance(handle, _Handle)
            # The sibling claims after this prepare composed its write but
            # before the provider applies it. A plan reconstructed from the
            # earlier ledger snapshot would now put the shared key back.
            await ps.mark_gateway_entry_claimed(
                agent_id="agent-1",
                slot_id="slot-claimed",
                session_id="session-claimed",
                agent_repo=repo,
            )
            applied.append(vault_write)

    async def resolve_mcp(*_args: Any, **_kwargs: Any) -> SandboxEgressCredentialPlan:
        assert _args == ()
        assert _kwargs["vault_enabled"] is True
        assert _kwargs["template"].agent_id == "agent-1"
        return mcp_plan

    async def record(
        _repo: Any, _agent_id: str, *, slot_id: str
    ) -> None:
        recorded.append(slot_id)

    settings = SimpleNamespace(sandbox_credential_vault_enabled=True)
    monkeypatch.setattr(ps, "load_astrabox_settings", lambda: settings)
    monkeypatch.setattr(provisioning, "load_astrabox_settings", lambda: settings)
    monkeypatch.setattr(
        mcp_credentials,
        "resolve_agent_mcp_credential_plan",
        resolve_mcp,
    )
    monkeypatch.setattr(ps, "record_gateway_entry", record)
    provider = _Provider()
    repo = FakeAgentRepo(
        {
            "agent_id": "agent-1",
            ps.PREPARED_SLOT_FIELD: {"slot_id": "slot-claimed"},
            ps.GATEWAY_ENTRIES_FIELD: [
                {"slot_id": "slot-claimed", "session_id": None}
            ],
        }
    )

    composed = asyncio.run(
        ps._compose_gateway_vault(
            _gateway_template(),
            backend_provider=provider,
            credential_request=_gateway_credential_request(),
            repo=repo,
            agent_id="agent-1",
            slot_id="slot-new",
            sandbox_id="sandbox-1",
        )
    )

    assert composed[2] is True
    assert recorded == ["slot-new"]
    assert repo.rows["agent-1"][ps.GATEWAY_ENTRIES_FIELD] == [
        {"slot_id": "slot-claimed", "session_id": "session-claimed"}
    ]
    assert len(applied) == 1
    assert applied[0].mcp == mcp_plan.mcp
    substitutions = applied[0].model[0].substitutions
    assert [item.name for item in substitutions] == [
        workload_credential_name("slot-new")
    ]
    assert substitutions[0].secret_value == "sk-shared-key"
    assert workload_credential_name("slot-claimed") not in {
        item.name for item in substitutions
    }


def test_a_claim_is_in_the_ledger_before_its_credential_is_repointed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key must be durably owned before either external write can happen."""

    repo = FakeAgentRepo(
        {
            "agent_id": "agent-1",
            ps.GATEWAY_ENTRIES_FIELD: [{"slot_id": "slot-1", "session_id": None}],
        }
    )
    seen_at_mint: list[Any] = []

    class _Endpoint:
        async def ensure_session_credential(self, *, context: Any) -> str:
            _ = context
            row = await repo.get_agent("agent-1")
            seen_at_mint.extend(row[ps.GATEWAY_ENTRIES_FIELD])
            return "sk-session-key"

    class _Provider:
        async def apply_credential_vault(
            self,
            sandbox: Any,
            *,
            vault_write: Any,
            create_if_missing: bool,
        ) -> None:
            _ = (sandbox, vault_write)
            assert create_if_missing is False

    from astrabox.seams import model as model_seam

    monkeypatch.setattr(ps, "AgentRepository", lambda: repo)
    monkeypatch.setattr(model_seam, "model_endpoint_for_name", lambda _n: _Endpoint())

    asyncio.run(
        ps.repoint_slot_gateway_credential(
            object(),
            backend_provider=_Provider(),
            credential_request=_gateway_credential_request(),
            template=_gateway_template(),
            claimed={"slot_id": "slot-1", "gateway_substitution": True},
            session_id="session-9",
            user_id=None,
        )
    )

    # Read at the first wire call, not at the end: the window is what matters.
    assert seen_at_mint == [{"slot_id": "slot-1", "session_id": "session-9"}]


def test_the_published_slot_is_read_from_the_manifest_not_the_caller() -> None:
    """An Agent with no published slot contributes nothing, not a blank id."""

    empty = FakeAgentRepo({"agent_id": "agent-2"})
    assert asyncio.run(ps._published_manifest_slot(empty, "agent-2")) == set()

    blank = FakeAgentRepo(
        {"agent_id": "agent-3", ps.PREPARED_SLOT_FIELD: {"slot_id": "  "}}
    )
    assert asyncio.run(ps._published_manifest_slot(blank, "agent-3")) == set()

    named = FakeAgentRepo(
        {"agent_id": "agent-4", ps.PREPARED_SLOT_FIELD: {"slot_id": "slot-x"}}
    )
    assert asyncio.run(ps._published_manifest_slot(named, "agent-4")) == {"slot-x"}


# ── publish CAS vs the absent-field defect ────────────────────────────────


def test_publish_cas_is_decidable_when_field_was_never_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live defect: {field: None} matches nothing on a row without the
    field, so the first refill of every Agent lost its own publish CAS. The
    explicit None initialisation makes the publish decidable."""

    repo = FakeAgentRepo({"agent_id": "agent-1"})  # field genuinely absent

    async def run() -> bool:
        row = await repo.get_agent("agent-1")
        assert ps.PREPARED_SLOT_FIELD not in row
        # The allocator's init step:
        if ps.PREPARED_SLOT_FIELD not in row:
            await repo.update_agent("agent-1", {ps.PREPARED_SLOT_FIELD: None})
        return await repo.compare_and_update_agent(
            "agent-1",
            expected={ps.PREPARED_SLOT_FIELD: None},
            updates={ps.PREPARED_SLOT_FIELD: _manifest()},
        )

    assert asyncio.run(run()) is True

    # And the defect itself stays encoded: without the init write the CAS
    # must lose against an absent field.
    bare = FakeAgentRepo({"agent_id": "agent-1"})

    async def run_bare() -> bool:
        return await bare.compare_and_update_agent(
            "agent-1",
            expected={ps.PREPARED_SLOT_FIELD: None},
            updates={ps.PREPARED_SLOT_FIELD: _manifest()},
        )

    assert asyncio.run(run_bare()) is False


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
