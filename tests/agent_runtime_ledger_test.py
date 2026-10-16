"""The Agent's shared-box ledger refuses stale writers, in the store."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from tests._agent_ledger import (
    create_agent,
    put_slots,
    runtime,
    set_runtime,
    slots,
)

pytestmark = pytest.mark.usefixtures("database")


def _seat(slot_id: str, sandbox_id: str, **fields: Any) -> dict[str, Any]:
    return {
        "slot_id": slot_id,
        "state": "prepared",
        "placement": "shared_slot",
        "sandbox_id": sandbox_id,
        "sandbox_backend": "open_sandbox",
        "runtime_generation": "generation-1",
        "prepared_at": datetime.now(timezone.utc).isoformat(),
        **fields,
    }


def _young_since() -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=2)


async def test_every_fenced_write_refuses_a_reading_another_write_moved() -> None:
    repo = await create_agent("agent-1")
    await put_slots("agent-1", _seat("slot-1", "box-1"))
    stale = await repo.read_ledger("agent-1")
    assert stale is not None

    # Another writer admits a conversation and adds a seat.
    assert await repo.admit_to_box(
        "agent-1", sandbox_id="box-1", session_id="other",
        admissions_revision=stale["_admissions_revision"], young_since=_young_since(),
    )
    assert await repo.adopt_pool_box(
        "agent-1", seats=[_seat("slot-2", "box-2")], resident=("box-2", "open_sandbox", None),
    )

    assert not await repo.admit_to_box(
        "agent-1", sandbox_id="box-1", session_id="late",
        admissions_revision=stale["_admissions_revision"], young_since=_young_since(),
    )
    assert not await repo.claim_resident_box(
        "agent-1", observed={"sandbox_id": None, "sandbox_backend": None},
        admissions_revision=stale["_admissions_revision"], sandbox_id="box-3",
        sandbox_backend="open_sandbox", resident_generation=None, session_id="late",
        young_since=_young_since(),
    )
    assert await repo.apply_slot_claims(
        "agent-1", slots_revision=stale["_slots_revision"],
        admissions_revision=stale["_admissions_revision"],
        claims=[("slot-1", "late")], withdrawals=[], young_since=_young_since(),
    ) is None
    assert not await repo.publish_runtime_generation(
        "agent-1", observed=stale, runtime_generation="g", sandbox_generation="s",
        client_pool_epoch="e",
    )
    assert [slot["state"] for slot in await slots("agent-1")] == ["prepared", "prepared"]
    record = await runtime("agent-1")
    assert (record["sandbox_id"], record["runtime_generation"]) == ("box-2", None)


async def test_pool_boxes_taken_at_once_keep_every_seat_and_one_resident_box() -> None:
    repo = await create_agent("agent-1")

    adopted = await asyncio.gather(
        *(
            repo.adopt_pool_box(
                "agent-1",
                seats=[_seat(f"{box}-seat-{n}", box) for n in range(2)],
                resident=(box, "open_sandbox", "box-generation"),
            )
            for box in ("box-a", "box-b")
        )
    )

    assert adopted == [True, True]
    assert sorted(slot["slot_id"] for slot in await slots("agent-1")) == [
        "box-a-seat-0", "box-a-seat-1", "box-b-seat-0", "box-b-seat-1",
    ]
    record = await runtime("agent-1")
    assert record["sandbox_id"] in {"box-a", "box-b"}
    assert (record["sandbox_backend"], record["resident_generation"]) == (
        "open_sandbox", "box-generation",
    )


async def test_a_client_pool_is_recorded_only_for_the_current_epoch_and_keeps_the_one_it_displaces() -> None:
    repo = await create_agent("agent-1")
    await set_runtime("agent-1", client_pool_epoch="epoch-2")

    assert await repo.record_client_pool(
        "agent-1", client_pool_epoch="epoch-1", name="pool-old-epoch", backend="open_sandbox",
    ) == "superseded"
    assert await repo.record_client_pool(
        "agent-1", client_pool_epoch="epoch-2", name="pool-a", backend="open_sandbox",
    ) == "recorded"
    assert await repo.record_client_pool(
        "agent-1", client_pool_epoch="epoch-2", name="pool-b", backend="open_sandbox",
    ) == "recorded"
    assert await repo.record_client_pool(
        "missing", client_pool_epoch="epoch-2", name="pool-c", backend="open_sandbox",
    ) == "missing"

    stored = await repo.get_agent("agent-1")
    assert stored is not None and stored["_client_pool_name"] == "pool-b"
    assert await repo.list_retiring_client_pools("agent-1") == [
        {"name": "pool-a", "backend": "open_sandbox"}
    ]


async def test_a_lease_realignment_read_before_a_renewal_is_refused() -> None:
    repo = await create_agent("agent-1")
    await set_runtime("agent-1", sandbox_id="box-1", sandbox_backend="open_sandbox")
    read = await repo.get_agent("agent-1")
    assert read is not None and "expires_at" not in read

    assert await repo.realign_resident_lease(
        "agent-1", sandbox_id="box-1", observed_expires_at=None,
        expires_at="2099-01-01T00:00:00+00:00",
    )
    # A realignment that read the lease before that one is not applied over it.
    assert not await repo.realign_resident_lease(
        "agent-1", sandbox_id="box-1", observed_expires_at=None,
        expires_at="2000-01-01T00:00:00+00:00",
    )
    stored = await repo.get_agent("agent-1")
    assert stored is not None and stored["expires_at"] == "2099-01-01T00:00:00+00:00"
