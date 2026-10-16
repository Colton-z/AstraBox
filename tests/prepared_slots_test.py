"""Prepared-seat ledger: claim atomicity, TTL and retirement races.

The ledger is the real store: a claim, clear or retirement is decided by the
statements the repository runs, so the races here are run against them.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from astrabox.core.service.orchestrator.agent import prepared_slots as ps
from astrabox.persistence.repository.agent_repository import AgentRepository
from astrabox.seams.sandbox import SandboxMemoryHeadroom
from tests._agent_ledger import (
    admissions,
    create_agent,
    put_admissions,
    put_slots,
    set_runtime,
    slots,
)

pytestmark = pytest.mark.usefixtures("database")


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _now() -> datetime:
    # A stored time comes back with all six fractional digits; one that has
    # them already reads back exactly as written.
    return datetime.now(timezone.utc).replace(microsecond=500000)


def _manifest(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "slot_id": "slot-abc",
        "state": "prepared",
        "placement": "shared_slot",
        "engine_kind": "claude_code",
        "sandbox_id": "box-1",
        "sandbox_backend": "fake",
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
        "prepared_at": _iso(_now()),
    }
    base.update(overrides)
    return base


async def _agent(*manifests: dict[str, Any], **runtime: Any) -> AgentRepository:
    """The Agent ``agent-1`` with ``manifests`` as its slots and ``runtime`` as its record."""
    repo = await create_agent("agent-1")
    if manifests:
        await put_slots("agent-1", *manifests)
    if runtime:
        await set_runtime("agent-1", **runtime)
    return repo


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


def _memory(monkeypatch: pytest.MonkeyPatch, memory: AsyncMock) -> None:
    monkeypatch.setattr(ps, "sandbox_for_name", lambda _name: SimpleNamespace(
        name="fake", read_memory_headroom=memory,
    ))


# ── claim ─────────────────────────────────────────────────────────────────


async def test_concurrent_claims_have_exactly_one_winner(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = await _agent(_manifest())
    _memory(monkeypatch, AsyncMock(
        return_value=SandboxMemoryHeadroom(limit_bytes=4 * 1024**3, current_bytes=0)
    ))

    results = await asyncio.gather(
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

    winners = [r for r in results if r is not None]
    assert len(winners) == 1, results
    stored = (await slots("agent-1"))[0]
    assert stored["state"] == "claimed"
    assert stored["claimed_session_id"] == winners[0]["claimed_session_id"]
    # The winner's admission is the only one the claim recorded.
    assert [entry["session_id"] for entry in await admissions("agent-1")] == [
        winners[0]["claimed_session_id"]
    ]


@pytest.mark.parametrize("manifest", [
    _manifest(runtime_generation="generation-old"),
    _manifest(prepared_at=_iso(_now() - timedelta(seconds=ps.prepared_seat_ttl_seconds() + 60))),
], ids=["stale-generation", "expired"])
async def test_claim_refuses_stale_generation_and_expired_slot(manifest: dict[str, Any]) -> None:
    repo = await _agent(manifest)

    claimed = await _claim_one(
        agent_id="agent-1",
        session_id="s1",
        expected_runtime_generation="generation-1",
        agent_repo=repo,
    )

    assert claimed is None
    # The refusal does not mutate the manifest — destruction belongs to the reaper.
    assert await slots("agent-1") == [manifest]


# ── reap policy ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "reason",
    [
        "Agent runtime generation changed",
        "Agent runtime preparation is disabled",
    ],
)
async def test_config_retirement_preserves_a_fresh_claim_handoff(
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claimed = _manifest(
        state="claimed",
        claimed_session_id="session-starting",
        claimed_at=_iso(_now()),
    )
    repo = await _agent(claimed)

    async def forbidden_retirement(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a fresh Session claim entered resource retirement")

    monkeypatch.setattr(ps, "_retire_manifest", forbidden_retirement)

    await ps.retire_prepared_runtime("agent-1", reason=reason, agent_repo=repo)

    assert await slots("agent-1") == [claimed]


async def test_config_retirement_losing_to_a_claim_does_not_destroy_the_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _manifest()
    repo = await _agent(prepared)
    _memory(monkeypatch, AsyncMock(
        return_value=SandboxMemoryHeadroom(limit_bytes=4 * 1024**3, current_bytes=0)
    ))

    async def claimed_meanwhile(_manifest_arg: dict[str, Any]) -> bool:
        # A Session claims the slot after the retirement read it.
        assert await _claim_one(
            agent_id="agent-1", session_id="session-starting",
            expected_runtime_generation="generation-1", agent_repo=repo,
        ) is not None
        return False

    monkeypatch.setattr(ps, "_claimed_session_adopted_placement", claimed_meanwhile)
    discards: list[str] = []

    async def forbidden_discard(
        _template: Any,
        manifest: dict[str, Any],
        **_kwargs: Any,
    ) -> None:
        discards.append(str(manifest.get("slot_id") or ""))

    monkeypatch.setattr(ps, "discard_prepared_slot", forbidden_discard)

    await ps.retire_prepared_runtime(
        "agent-1",
        reason="Agent runtime generation changed",
        agent_repo=repo,
    )

    [stored] = await slots("agent-1")
    assert (stored["state"], stored["claimed_session_id"]) == ("claimed", "session-starting")
    assert discards == []


# ── gateway-key bookkeeping ───────────────────────────────────────────────


async def _capacity_claim(repo: AgentRepository) -> dict[str, Any] | None:
    return await _claim_one(
        agent_id="agent-1", session_id="session-1",
        expected_runtime_generation="generation-1", agent_repo=repo,
    )


async def _capacity_fixture(
    monkeypatch: pytest.MonkeyPatch, *, free_reservations: int,
    admitted: list[dict[str, Any]],
) -> tuple[AgentRepository, AsyncMock]:
    from astrabox.core.service.orchestrator.runtime.shared_sandbox_lease import (
        CONVERSATION_MEMORY_RESERVE_BYTES,
    )

    repo = await _agent(_manifest(), sandbox_id="box-1", sandbox_backend="fake")
    if admitted:
        await put_admissions("agent-1", *admitted)
    memory = AsyncMock(return_value=SandboxMemoryHeadroom(
        limit_bytes=512 * 1024**2 + free_reservations * CONVERSATION_MEMORY_RESERVE_BYTES,
        current_bytes=512 * 1024**2,
    ))
    _memory(monkeypatch, memory)
    return repo, memory


async def test_claim_rechecks_memory_after_another_admission_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrabox.core.service.orchestrator.runtime.shared_sandbox_lease import (
        admissions_young_since,
    )

    repo, memory = await _capacity_fixture(monkeypatch, free_reservations=1, admitted=[])
    reading = memory.return_value

    async def probe(_box: str) -> SandboxMemoryHeadroom:
        if memory.await_count == 1:
            # A sibling is admitted to the box between the claim's read and its write.
            ledger = await repo.read_ledger("agent-1")
            assert ledger is not None
            assert await repo.admit_to_box(
                "agent-1", sandbox_id="box-1", session_id="sibling",
                admissions_revision=ledger["_admissions_revision"],
                young_since=admissions_young_since(),
            )
        return reading

    memory.side_effect = probe
    assert await _capacity_claim(repo) is None
    assert memory.await_count == 2
    assert (await slots("agent-1"))[0]["state"] == "retiring"
    assert [entry["session_id"] for entry in await admissions("agent-1")] == ["sibling"]


async def test_capacity_refusal_cannot_retire_a_slot_claimed_by_another_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, memory = await _capacity_fixture(monkeypatch, free_reservations=0, admitted=[])
    roomy = SandboxMemoryHeadroom(limit_bytes=4 * 1024**3, current_bytes=0)
    full = SandboxMemoryHeadroom(limit_bytes=512 * 1024**2, current_bytes=512 * 1024**2)

    async def read_memory(_box: str) -> SandboxMemoryHeadroom:
        if memory.await_count == 1:
            # Another worker claims the slot while this one reads a full box.
            memory.side_effect = None
            memory.return_value = roomy
            assert await _claim_one(
                agent_id="agent-1", session_id="winner",
                expected_runtime_generation="generation-1", agent_repo=repo,
            ) is not None
        return full

    memory.side_effect = read_memory
    assert await _capacity_claim(repo) is None
    [stored] = await slots("agent-1")
    assert (stored["state"], stored["claimed_session_id"]) == ("claimed", "winner")


async def test_slot_expiring_during_the_capacity_probe_is_not_claimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _now()

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return now

    repo = await _agent(_manifest(prepared_at=now.isoformat()), sandbox_id="box-1", sandbox_backend="fake")
    monkeypatch.setattr(ps, "datetime", Clock)

    async def delayed_probe(_box: str) -> SandboxMemoryHeadroom:
        nonlocal now
        now += timedelta(seconds=ps.prepared_seat_ttl_seconds() + 1)
        return SandboxMemoryHeadroom(limit_bytes=4 * 1024**3, current_bytes=0)

    _memory(monkeypatch, AsyncMock(side_effect=delayed_probe))
    assert await _capacity_claim(repo) is None
    assert (await slots("agent-1"))[0]["state"] == "prepared"
    assert await admissions("agent-1") == []
