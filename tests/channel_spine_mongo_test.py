"""Real-Mongo conformance for the channel spine's fenced CAS predicates.

The sqlite collection emulation and real Mongo can diverge on query
semantics (the provisioning lane learned this the hard way — see HANDOFF §8
history); every CAS shape the spine relies on ($in state guards, owner+
generation predicates, $inc fencing, expiry re-checks inside the predicate)
gets pinned here against a real server. Run with ``-m mongo``.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import pytest

pymongo = pytest.importorskip("pymongo")

pytestmark = pytest.mark.mongo

from astrabox.persistence.repository.channel_repository import (  # noqa: E402
    INBOUND_RECEIVED,
    INBOUND_SETTLED,
    ChannelRepository,
)

_PAYLOAD = {
    "content": "hello",
    "conversation_key": None,
    "reply_context": None,
    "attention": None,
    "reference": None,
    "participant": None,
    "creator_user_id": "creator",
}


def _binding() -> str:
    # Unique binding per test run: mongo state persists across runs.
    return f"wh-{uuid.uuid4().hex}"


async def _claim(
    repo: ChannelRepository, deployment_id: str, **overrides: Any
) -> tuple[dict[str, Any], str]:
    fields: dict[str, Any] = {
        "deployment_id": deployment_id,
        "dedup_key": "m",
        "channel_name": "recorder",
        "agent_id": "agent-1",
        "payload": dict(_PAYLOAD),
    }
    fields.update(overrides)
    return await repo.claim_inbound(**fields)


async def test_real_mongo_work_item_claim_steal_and_stale_owner_fencing() -> None:
    repo = ChannelRepository()
    deployment_id = _binding()
    old, outcome = await _claim(repo, deployment_id, now=time.time() - 100_000)
    assert outcome == "claimed"
    stolen, outcome2 = await _claim(repo, deployment_id)
    assert outcome2 == "claimed"
    assert int(stolen["generation"]) == 2

    # The stale owner's settle/fail/bind all miss their (owner, generation)
    # predicates on real Mongo and change nothing.
    assert not await repo.begin_inbound_dispatch(
        item_id=str(old["_id"]),
        owner_token=str(old["owner_token"]),
        generation=1,
    )
    assert not await repo.settle_inbound(
        item_id=str(old["_id"]),
        owner_token=str(old["owner_token"]),
        generation=1,
        session_id="stale",
    )
    current = await repo.get_inbound(str(old["_id"]))
    assert current["state"] == INBOUND_RECEIVED
    assert current["owner_token"] == stolen["owner_token"]

    # The live owner's full path works: begin -> settle -> duplicate ack.
    assert await repo.begin_inbound_dispatch(
        item_id=str(stolen["_id"]),
        owner_token=str(stolen["owner_token"]),
        generation=2,
    )
    assert await repo.settle_inbound(
        item_id=str(stolen["_id"]),
        owner_token=str(stolen["owner_token"]),
        generation=2,
        session_id="sess-1",
    )
    winner, after = await _claim(repo, deployment_id)
    assert after == "duplicate" and winner["session_id"] == "sess-1"
    assert winner["state"] == INBOUND_SETTLED


async def test_real_mongo_recovery_claim_reverifies_expiry_in_the_predicate() -> None:
    repo = ChannelRepository()
    deployment_id = _binding()
    doc, _ = await _claim(repo, deployment_id)
    item_id = str(doc["_id"])
    # Fresh lease: not recoverable, even when asked with the right generation.
    assert await repo.claim_inbound_for_recovery(item_id=item_id, expected_generation=1) is None
    # Expired lease: recoverable exactly once per generation.
    await repo.fail_inbound(
        item_id=item_id,
        owner_token=str(doc["owner_token"]),
        generation=1,
        error="x",
        retry_at_epoch=time.time() - 1,
    )
    listed = await repo.list_recoverable_inbound()
    assert item_id in [d["_id"] for d in listed]
    first = await repo.claim_inbound_for_recovery(item_id=item_id, expected_generation=1)
    assert first is not None and int(first["generation"]) == 2
    assert await repo.claim_inbound_for_recovery(item_id=item_id, expected_generation=1) is None


async def test_real_mongo_outbox_fenced_lease_and_atomic_attempts() -> None:
    repo = ChannelRepository()
    entry = await repo.create_outbox_entry(
        work_item_id=f"item-{uuid.uuid4().hex}",
        deployment_id=_binding(),
        channel_name="recorder",
        session_id="s1",
        command_id="cmd-1",
        turn_id="turn-1",
        reply_context={"cb": "x"},
    )
    outbox_id = str(entry["_id"])
    stale_generation = await repo.claim_outbox_for_delivery(
        outbox_id, owner="stale", now=time.time() - 100_000
    )
    assert stale_generation == 1
    live_generation = await repo.claim_outbox_for_delivery(outbox_id, owner="live")
    assert live_generation == 2

    assert not await repo.mark_outbox_delivered(
        outbox_id, owner="stale", generation=stale_generation
    )
    assert not await repo.record_outbox_failure(
        outbox_id, owner="stale", generation=stale_generation,
        error="stale", next_attempt_at=None,
    )
    current = await repo.get_outbox_entry(outbox_id)
    assert current["state"] == "SENDING" and int(current["attempts"]) == 0

    assert await repo.record_outbox_failure(
        outbox_id, owner="live", generation=live_generation,
        error="attempt", next_attempt_at="soon",
    )
    assert await repo.record_delivery_aliases(
        outbox_id, owner="live", generation=live_generation, aliases=["msg_1"]
    )
    assert await repo.mark_outbox_delivered(
        outbox_id, owner="live", generation=live_generation
    )
    final = await repo.get_outbox_entry(outbox_id)
    assert final["state"] == "DELIVERED"
    assert int(final["attempts"]) == 1
    assert final["delivery_aliases"] == ["msg_1"]


async def test_real_mongo_conversation_lock_generation_fencing() -> None:
    repo = ChannelRepository()
    deployment_id = _binding()
    await repo.upsert_conversation(
        deployment_id=deployment_id, conversation_key="c", session_id="s1", agent_id="a"
    )
    stale = await repo.acquire_conversation_lock(
        deployment_id=deployment_id, conversation_key="c", owner="A",
        now=time.time() - 100_000,
    )
    successor = await repo.acquire_conversation_lock(
        deployment_id=deployment_id, conversation_key="c", owner="B"
    )
    assert stale is not None and successor == stale + 1
    await repo.release_conversation_lock(
        deployment_id=deployment_id, conversation_key="c", owner="A", generation=stale
    )
    assert await repo.acquire_conversation_lock(
        deployment_id=deployment_id, conversation_key="c", owner="C"
    ) is None, "B still holds the lock after A's stale release"
