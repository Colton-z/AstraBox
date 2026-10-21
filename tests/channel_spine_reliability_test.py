"""Channel spine races, fences and account boundaries, against the real PostgreSQL store.

The repository's compare-and-sets under concurrent writers (claims, steals,
leases, locks, answers), and the boundaries that keep one account's message
out of another account's conversation. Service-level cases send through the
real Session runtime service; a Session's runtime is Claude's adapter over
runner frames built from the SDK's own types (``tests/_claude_channel_session.py``).
Product flows (delivery, questions, recovery) are proven end to end.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest

import astrabox.core.service.orchestrator.channel_ingress_service as ingress_module
from astrabox.core.service.orchestrator.channel_ingress_service import (
    ChannelIngressService,
)
from astrabox.core.service.orchestrator.deployment_service import DeploymentService
from astrabox.core.service.orchestrator.session_runtime.feed import SessionFeed
from astrabox.core.service.orchestrator.session_runtime.service import SessionRuntimeService
from sqlalchemy import select

from astrabox.persistence.repository.channel_repository import (
    INBOUND_IGNORED,
    INBOUND_OBSERVED,
    INBOUND_RECEIVED,
    INBOUND_SETTLED,
    OUTBOX_PENDING,
    ChannelRepository,
)
from astrabox.persistence.repository.deployment_repository import DeploymentRepository
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.repository.session_runtime_repository import SessionRuntimeRepository
from astrabox.persistence.sql import fetch_all, fetch_one
from astrabox.persistence.sql.models.channels import ChannelOutbox
from astrabox.persistence.sql.models.sessions import SessionInput
from astrabox.seams.channel import (
    SIGNAL_AUTHORIZATION,
    SIGNAL_OBSERVATION,
    ChannelAttention,
    ChannelDeliveryReceipt,
    ChannelInbound,
    ChannelProvider,
    register_channel,
)
from astrabox.seams.channel_accounts import (
    ChannelAccountResolver,
    register_channel_account_resolver,
)
from tests._channel_store import seed_binding, seed_sessions
from tests._claude_channel_session import ClaudeSession

#: Sessions the tests here name directly; fake conversation starts add theirs.
_SESSIONS = ("s1", "creator", "alice", "bob", *(f"sess-{n}" for n in range(1, 21)))


@pytest.fixture(autouse=True)
async def _channel_store(database: str) -> None:
    """The bindings and Sessions every test here writes channel records of."""
    for deployment_id in ("wh", "wh-1"):
        await seed_binding(deployment_id)
    await seed_sessions(*_SESSIONS)


async def _record(deployment_id: str, dedup_key: str) -> dict[str, Any]:
    """The inbound record of one platform message."""
    record = await ChannelRepository().get_message_record(
        deployment_id=deployment_id, dedup_key=dedup_key,
    )
    assert record is not None, f"no record of message {dedup_key!r}"
    return record


async def _lease(
    repo: ChannelRepository, outbox_id: str, *, owner: str, now: float | None = None,
) -> int | None:
    """Lease a delivery row; its lease generation, or ``None``."""
    claimed = await repo.claim_outbox_for_delivery(outbox_id, owner=owner, now=now)
    return None if claimed is None else int(claimed["lease_generation"])


_PAYLOAD = {
    "content": "hello",
    "conversation_key": None,
    "reply_context": None,
    "reference": None,
    "participant": None,
    "creator_user_id": "creator",
}


async def _claim(
    repo: ChannelRepository,
    *,
    dedup_key: str | None = "m",
    payload: dict[str, Any] | None = None,
    now: float | None = None,
) -> tuple[dict[str, Any], str]:
    return await repo.claim_inbound(
        deployment_id="wh",
        dedup_key=dedup_key,
        channel_name="recorder",
        agent_id="agent-1",
        payload=payload or dict(_PAYLOAD),
        now=now,
    )


# ── work item semantics (real store) ─────────────────────────────────────────


async def test_claim_inbound_admits_exactly_one_winner() -> None:
    repo = ChannelRepository()
    results = await asyncio.gather(*(_claim(repo, dedup_key="msg-1") for _ in range(6)))
    claimed = [doc for doc, outcome in results if outcome == "claimed"]
    assert len(claimed) == 1
    # Un-settled losers report in_progress (a live owner holds it), never
    # duplicate (nothing has settled yet).
    assert all(o == "in_progress" for _, o in results if o != "claimed")


async def test_settled_item_dedupes_but_live_is_in_progress() -> None:
    repo = ChannelRepository()
    doc, _ = await _claim(repo)
    _d, before = await _claim(repo)
    assert before == "in_progress"
    assert await repo.begin_inbound_dispatch(
        item_id=str(doc["item_id"]),
        owner_token=str(doc["owner_token"]),
        generation=1,
    )
    assert await repo.settle_inbound(
        item_id=str(doc["item_id"]),
        owner_token=str(doc["owner_token"]),
        generation=1,
        session_id="sess-1",
    )
    winner, after = await _claim(repo)
    assert after == "duplicate" and winner["session_id"] == "sess-1"


async def test_expired_item_is_stolen_with_a_forward_fence() -> None:
    repo = ChannelRepository()
    old, _ = await _claim(repo, now=time.time() - 100_000)
    stolen, outcome = await _claim(repo)
    assert outcome == "claimed"
    assert int(stolen["generation"]) == int(old["generation"]) + 1
    assert stolen["owner_token"] != old["owner_token"]


async def test_stale_owner_cannot_settle_fail_or_bind_a_stolen_item() -> None:
    """Invariant B: after the steal, every write keyed to the old owner's
    (owner_token, generation) misses and changes NOTHING."""
    repo = ChannelRepository()
    old, _ = await _claim(repo, now=time.time() - 100_000)
    stolen, outcome = await _claim(repo)
    assert outcome == "claimed"
    old_owner = str(old["owner_token"])
    old_generation = int(old["generation"])

    assert not await repo.begin_inbound_dispatch(
        item_id=str(old["item_id"]), owner_token=old_owner, generation=old_generation
    )
    assert not await repo.settle_inbound(
        item_id=str(old["item_id"]), owner_token=old_owner,
        generation=old_generation, session_id="stale",
    )
    assert not await repo.fail_inbound(
        item_id=str(old["item_id"]), owner_token=old_owner,
        generation=old_generation, error="stale", backoff=(),
    )
    assert not await repo.bind_inbound_dispatch(
        item_id=str(old["item_id"]), owner_token=old_owner,
        generation=old_generation, session_id="stale",
    )
    current = await repo.get_inbound(str(old["item_id"]))
    assert current["state"] == INBOUND_RECEIVED
    assert current["owner_token"] == stolen["owner_token"]
    assert current.get("session_id") is None


async def test_recovery_claim_fences_forward_and_respects_live_renewal() -> None:
    repo = ChannelRepository()
    doc, _ = await _claim(repo)
    item_id, owner = str(doc["item_id"]), str(doc["owner_token"])
    # A live item (fresh lease) is not recoverable.
    assert await repo.claim_inbound_for_recovery(item_id=item_id, expected_generation=1) is None
    # Expire the lease via the failure watermark, then recover.
    await repo.begin_inbound_dispatch(item_id=item_id, owner_token=owner, generation=1)
    await repo.fail_inbound(
        item_id=item_id, owner_token=owner, generation=1, error="x", backoff=(-1.0,),
    )
    reclaimed = await repo.claim_inbound_for_recovery(item_id=item_id, expected_generation=1)
    assert reclaimed is not None and int(reclaimed["generation"]) == 2
    # A second sweeper holding the STALE generation loses.
    assert await repo.claim_inbound_for_recovery(item_id=item_id, expected_generation=1) is None


# ── conversation lock (real store) ───────────────────────────────────────────


async def test_conversation_lock_is_exclusive_then_releasable() -> None:
    repo = ChannelRepository()
    generation = await repo.acquire_conversation_lock(
        deployment_id="wh", conversation_key="c", owner="A"
    )
    assert generation is not None
    assert await repo.acquire_conversation_lock(
        deployment_id="wh", conversation_key="c", owner="B"
    ) is None
    await repo.release_conversation_lock(
        deployment_id="wh", conversation_key="c", owner="A", generation=generation
    )
    assert await repo.acquire_conversation_lock(
        deployment_id="wh", conversation_key="c", owner="B"
    ) is not None


async def test_crashed_then_revived_holder_cannot_release_the_successors_lock() -> None:
    """Invariant B on the lock: release matches (owner, generation)."""
    repo = ChannelRepository()
    stale_generation = await repo.acquire_conversation_lock(
        deployment_id="wh", conversation_key="c", owner="A", now=time.time() - 100_000
    )
    successor_generation = await repo.acquire_conversation_lock(
        deployment_id="wh", conversation_key="c", owner="B"
    )
    assert stale_generation is not None and successor_generation is not None
    # A revives and releases with its OLD generation: must be a no-op even
    # though A re-uses the same owner string.
    await repo.release_conversation_lock(
        deployment_id="wh", conversation_key="c", owner="A", generation=stale_generation
    )
    assert await repo.acquire_conversation_lock(
        deployment_id="wh", conversation_key="c", owner="C"
    ) is None, "B still holds the lock"


# ── outbox semantics (real store) ────────────────────────────────────────────


async def _outbox(repo: ChannelRepository, **overrides: Any) -> dict[str, Any]:
    fields = {
        "deployment_id": "wh",
        "channel_name": "recorder",
        "session_id": "s1",
        "response_seq": 1,
        "response_id": "reply-1",
        "reply_context": {"callback_url": "http://x"},
    }
    fields.update(overrides)
    return await repo.create_outbox_entry(**fields)


async def test_outbox_creation_is_idempotent_per_destination_session_and_response() -> None:
    """Invariant C: the row is born bound to its exact reply, and projecting the
    same output again re-creates the SAME row (no double delivery intent)."""
    repo = ChannelRepository()
    first = await _outbox(repo)
    again = await _outbox(repo)
    assert first["outbox_id"] == again["outbox_id"]
    assert again["response_id"] == "reply-1"
    other_reply = await _outbox(repo, response_id="reply-2", response_seq=2)
    assert other_reply["outbox_id"] != first["outbox_id"]


async def test_outbox_lease_admits_one_deliverer() -> None:
    repo = ChannelRepository()
    entry = await _outbox(repo)
    generations = await asyncio.gather(
        *(_lease(repo, str(entry["outbox_id"]), owner=f"o{i}") for i in range(4))
    )
    assert len([g for g in generations if g is not None]) == 1


async def _outbox_row(outbox_id: str) -> dict[str, Any]:
    """The stored outbox row, read past the repository's fenced writes."""
    outbox = ChannelOutbox.__table__
    row = await fetch_one(select(outbox).where(outbox.c.outbox_id == outbox_id))
    assert row is not None, f"no outbox row {outbox_id!r}"
    return dict(row)


async def test_stale_deliverer_cannot_complete_or_fail_the_successors_row() -> None:
    """Invariant B on delivery: DELIVERED/DEAD/attempts writes match the
    fenced lease; a deliverer whose lease was reclaimed changes nothing."""
    repo = ChannelRepository()
    entry = await _outbox(repo)
    outbox_id = str(entry["outbox_id"])
    stale_generation = await _lease(repo, outbox_id, owner="stale")
    assert stale_generation is not None
    # Once the stale deliverer's lease has lapsed, a successor takes the row.
    successor_generation = await _lease(
        repo, outbox_id, owner="live", now=time.time() + 100_000,
    )
    assert successor_generation is not None

    assert not await repo.mark_outbox_delivered(
        outbox_id, owner="stale", generation=stale_generation
    )
    assert not await repo.record_outbox_failure(
        outbox_id, owner="stale", generation=stale_generation,
        error="stale", backoff=(), retry=False,
    )
    assert not await repo.record_delivery_aliases(
        outbox_id, owner="stale", generation=stale_generation, aliases=["zombie"]
    )
    current = await _outbox_row(outbox_id)
    assert current["state"] == "SENDING"
    assert int(current["attempts"]) == 0
    assert current["delivery_aliases"] == []
    assert await repo.mark_outbox_delivered(
        outbox_id, owner="live", generation=successor_generation
    )


async def test_a_failed_attempt_waits_out_its_backoff_before_any_deliverer_retries() -> None:
    repo = ChannelRepository()
    entry = await _outbox(repo)
    outbox_id = str(entry["outbox_id"])
    generation = await _lease(repo, outbox_id, owner="A")
    assert generation is not None
    assert await repo.record_outbox_failure(
        outbox_id, owner="A", generation=generation, error="attempt 1", backoff=(60.0,),
    ) == OUTBOX_PENDING
    # Not due yet: no replica re-delivers before the backoff has passed.
    assert await repo.list_pending_outbox() == []
    assert await _lease(repo, outbox_id, owner="B") is None
    current = await _outbox_row(outbox_id)
    assert int(current["attempts"]) == 1 and current["lease_owner"] is None
    later = time.time() + 61
    assert [row["outbox_id"] for row in await repo.list_pending_outbox(now=later)] == [outbox_id]
    assert await _lease(repo, outbox_id, owner="B", now=later) is not None


async def test_replies_claimed_together_are_leased_only_in_their_sessions_order() -> None:
    repo = ChannelRepository()
    rows = [
        await _outbox(repo, response_id=f"reply-{seq}", response_seq=seq) for seq in range(1, 6)
    ]
    claims = await asyncio.gather(*(
        _lease(repo, str(row["outbox_id"]), owner=f"deliverer-{index}")
        for index, row in enumerate(rows * 3)
    ))
    leased = {
        str(row["outbox_id"])
        for row, generation in zip(rows * 3, claims, strict=True)
        if generation is not None
    }
    assert leased == {str(rows[0]["outbox_id"])}
    assert len([generation for generation in claims if generation is not None]) == 1


async def test_racing_first_writes_of_one_alias_record_it_once() -> None:
    """Two deliveries can report the same platform message at once; neither
    may fail, and the alias keeps the time it was first recorded."""
    repo = ChannelRepository()

    async def record() -> None:
        await repo.record_question_aliases(
            deployment_id="wh", aliases=["msg-1"], conversation_key="room",
            session_id="s1", interaction_ref="ref-1", question_index=0,
        )

    await asyncio.gather(*(record() for _ in range(8)))
    first = await repo.resolve_alias(deployment_id="wh", alias="msg-1")
    await record()
    again = await repo.resolve_alias(deployment_id="wh", alias="msg-1")
    assert first is not None and again is not None
    assert first["created_at"] == again["created_at"]
    assert (again["conversation_key"], again["interaction_ref"]) == ("room", "ref-1")


# ── service flows (real Session runtime service and channel store) ───────────


class _RecorderChannel(ChannelProvider):
    name = "recorder"

    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.delivered: list[tuple[dict[str, Any], str]] = []
        self.inbound = ChannelInbound(content="mapped")

    def verify_and_resolve(self, *, headers, raw_body, binding) -> ChannelInbound:
        return self.inbound

    async def deliver_outbound(self, *, reply_context, text, binding) -> None:
        _ = binding
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("callback 5xx")
        self.delivered.append((reply_context, text))


class _ReaderHost:
    """The runtime service's reader host: a test records each Session's runtime itself."""

    async def ensure(self, session_id: str) -> None:
        return None


class _Runtimes:
    """Each Session's Claude runtime, recorded the way its reader records it.

    :meth:`answer_inputs` has every Session's runtime take the inputs the
    Session holds and answer each with ``reply_text``; ``reply_text = None``
    leaves the runtimes to the test.
    """

    def __init__(self, ingress: ChannelIngressService) -> None:
        self.ingress = ingress
        self.reply_text: str | None = "the reply"
        self._sessions: dict[str, ClaudeSession] = {}

    def session(self, session_id: str) -> ClaudeSession:
        if session_id not in self._sessions:
            self._sessions[session_id] = ClaudeSession(
                session_id, consumers=[self.ingress.on_facts],
            )
        return self._sessions[session_id]

    async def answer_inputs(self) -> None:
        if self.reply_text is None:
            return
        held = SessionInput.__table__
        rows = await fetch_all(
            select(held.c.session_id, held.c.input_id, held.c.content)
            .where(held.c.state == "pending")
            .order_by(held.c.seq)
        )
        for row in rows:
            await self.session(str(row["session_id"])).reply(
                str(row["input_id"]), str(row["content"]), self.reply_text,
            )


async def _inputs(session_id: str | None = None) -> list[dict[str, Any]]:
    """The inputs Sessions accepted, in acceptance order."""
    accepted = SessionInput.__table__
    statement = select(accepted).order_by(accepted.c.seq)
    if session_id is not None:
        statement = statement.where(accepted.c.session_id == session_id)
    return [dict(row) for row in await fetch_all(statement)]


class _PendingTasks(list):
    ingress: ChannelIngressService
    runtimes: _Runtimes


def _spine(
    session_states: dict[str, str] | None = None,
    *,
    binding_extra: dict[str, Any] | None = None,
) -> tuple[DeploymentService, ChannelIngressService, _Runtimes, _PendingTasks]:
    binding_row = {
        "deployment_id": "wh-1",
        "execution_target": "astrabox",
        "agent_id": "agent-1",
        "scene": "channel:recorder",
        "prompt_prefix": "",
        "secret": "s",
        "enabled": True,
        **(binding_extra or {}),
    }
    deployment_repo = AsyncMock()
    deployment_repo.get_by_id.return_value = binding_row
    agent_repo = AsyncMock()
    agent_repo.get_agent.return_value = {"agent_id": "agent-1", "name": "agent-1", "created_by": "creator"}
    agent_service = AsyncMock()
    counter = {"n": 0}

    async def _start(user, agent_id, *, source_type="agent"):
        counter["n"] += 1
        session_id = f"sess-{counter['n']}"
        await seed_sessions(
            session_id, user_id=user.user_id,
            state=(session_states or {}).get(session_id, "READY"),
        )
        return {"session_id": session_id}

    agent_service.start_conversation.side_effect = _start
    agent_service.start_integration_conversation.side_effect = _start
    sessions = SessionRepository()
    facts = SessionRuntimeRepository()
    session_runtime = SessionRuntimeService(
        sessions=sessions, repo=facts, feed=SessionFeed(), host=_ReaderHost(),  # type: ignore[arg-type]
    )
    spawned = _PendingTasks()
    ingress = ChannelIngressService(
        deployment_repo=deployment_repo,
        agent_repo=agent_repo,
        agent_service_getter=lambda: agent_service,
        sessions_repo=sessions,
        session_runtime=session_runtime,
        session_facts=facts,
        spawn_background_task=lambda coro, name="": spawned.append(coro),
    )
    runtimes = _Runtimes(ingress)
    spawned.ingress = ingress
    spawned.runtimes = runtimes
    service = DeploymentService(
        deployment_repo=deployment_repo,
        agent_repo=agent_repo,
        agent_service_getter=lambda: agent_service,
        sessions_repo=sessions,
        session_runtime=session_runtime,
        session_facts=facts,
        agent_config=AsyncMock(),
        channel_ingress=ingress,
    )
    service._test_agent_service = agent_service  # type: ignore[attr-defined]
    return service, ingress, runtimes, spawned


async def _run_spawned(spawned: _PendingTasks) -> None:
    while spawned:
        await asyncio.wait_for(spawned.pop(0), timeout=10)


async def _drain_spawned(spawned: _PendingTasks) -> None:
    """Run the drives, let the runtimes answer what was sent, then deliver.

    Deliveries start from the fact consumer as replies are recorded; the
    reconciler's projection and sweep then find nothing left.
    """
    await _run_spawned(spawned)
    await spawned.runtimes.answer_inputs()
    await _run_spawned(spawned)
    await spawned.ingress.project_session_outputs()
    await spawned.ingress.sweep_pending_outbox()
    await _run_spawned(spawned)


def _close_spawned(spawned: list[Any]) -> None:
    for coro in spawned:
        coro.close()
    spawned.clear()


# ── channel sources and their cursor ────────────────────────────────────────


async def test_channel_source_cursor_is_discovered_and_advances_monotonically() -> None:
    repo = DeploymentRepository()
    await repo.create(
        "channel-1",
        {
            "agent_id": "agent-1",
            "created_by": "creator",
            "scene": "channel:telegram",
            "secret": "secret",
            "enabled": True,
        },
    )
    await repo.create(
        "webhook-1",
        {"agent_id": "agent-1", "created_by": "creator", "scene": "hmac", "enabled": True},
    )

    assert [
        row["deployment_id"] for row in await repo.list_channel_bindings()
    ] == ["channel-1", "wh", "wh-1"]
    assert await repo.advance_channel_source_cursor(
        "channel-1", scene="channel:telegram", source_cursor=8
    )
    assert await repo.advance_channel_source_cursor(
        "channel-1", scene="channel:telegram", source_cursor=7
    )
    stored = await repo.get_by_id("channel-1")
    assert stored is not None and stored["source_cursor"] == 8

    await repo.update_for_deployment(
        "channel-1", "agent-1", {"enabled": False}
    )
    assert not await repo.advance_channel_source_cursor(
        "channel-1", scene="channel:telegram", source_cursor=9
    )


# ── the fence before a message is sent ──────────────────────────────────────


async def test_a_stolen_worker_cannot_send_after_its_renew_misses() -> None:
    """The renew before the send is a FENCE: a worker whose item was stolen
    while it handled the conversation's question aborts without sending."""
    provider = _RecorderChannel()
    provider.inbound = ChannelInbound(content="hi", dedup_key="steal-1")
    register_channel(provider)
    _service, ingress, _runtimes, spawned = _spine()
    repo = ingress._channel_repo

    item, outcome = await repo.claim_inbound(
        deployment_id="wh-1",
        dedup_key="steal-1",
        channel_name="recorder",
        agent_id="agent-1",
        payload=dict(_PAYLOAD),
    )
    assert outcome == "claimed"
    item_id = str(item["item_id"])

    stolen: dict[str, Any] = {}

    async def _stolen_meanwhile(*_args: Any, **_kwargs: Any) -> bool:
        # While worker A handles the conversation's question, its lease lapses
        # and the reconciler (worker B) steals the item.
        await repo.renew_inbound_lease(
            item_id=item_id,
            owner_token=str(item["owner_token"]),
            generation=int(item["generation"]),
            now=time.time() - 100_000,
        )
        taken = await repo.claim_inbound_for_recovery(
            item_id=item_id, expected_generation=int(item["generation"])
        )
        assert taken is not None
        stolen.update(taken)
        return False

    ingress._settle_pending_interaction = _stolen_meanwhile  # type: ignore[method-assign]
    await ingress._drive_work_item(dict(item))

    assert await _inputs() == [], "the stale worker must not send the message"
    current = await repo.get_inbound(item_id)
    assert current["owner_token"] == stolen["owner_token"]
    assert int(current["generation"]) == int(stolen["generation"])
    assert current["state"] == "DISPATCHING", "the successor's drive is untouched"
    _close_spawned(spawned)


# ── execution accounts ──────────────────────────────────────────────────────


async def test_execution_accounts_keep_distinct_conversations_and_restore_their_own_history() -> None:
    provider = _RecorderChannel()
    register_channel(provider)
    service, ingress, _runtimes, spawned = _spine()
    binding = service._deployment_repo.get_by_id.return_value
    sessions: list[str] = []
    for number, account in enumerate(["", "alice", "bob", "alice", ""]):
        binding["execution_user_id"] = account
        provider.inbound = ChannelInbound(
            content="next turn", dedup_key=f"identity-{number}", conversation_key="room",
        )
        receipt = await service.trigger("wh-1", headers={}, raw_body=b"{}")
        sessions.append(receipt["session_id"])
        await _drain_spawned(spawned)
        item = await _record("wh-1", f"identity-{number}")
        assert item["state"] == INBOUND_SETTLED
        assert item["payload"]["creator_user_id"] == (account or "creator")
        assert item["payload"]["execution_user_id"] == account
    assert len(set(sessions[:3])) == 3
    assert sessions[3:] == [sessions[1], sessions[0]]
    assert service._test_agent_service.start_conversation.await_count == 1
    assert service._test_agent_service.start_integration_conversation.await_count == 2


async def test_recovery_keeps_the_execution_account_captured_before_configuration_changes() -> None:
    provider = _RecorderChannel()
    register_channel(provider)
    service, ingress, _runtimes, spawned = _spine(binding_extra={"execution_user_id": "alice"})
    provider.inbound = ChannelInbound(content="work", dedup_key="frozen-owner", conversation_key="room")
    receipt = await service.trigger("wh-1", headers={}, raw_body=b"{}")
    _close_spawned(spawned)
    service._deployment_repo.get_by_id.return_value["execution_user_id"] = "bob"
    repo = ingress._channel_repo
    item = await _record("wh-1", "frozen-owner")
    await repo.renew_inbound_lease(
        item_id=str(item["item_id"]), owner_token=str(item["owner_token"]),
        generation=1, now=time.time() - 100_000,
    )
    assert await ingress.recover_inbound() == 1
    await _drain_spawned(spawned)
    recovered = await repo.get_inbound(str(item["item_id"]))
    assert recovered["state"] == INBOUND_SETTLED
    assert recovered["session_id"] == receipt["session_id"]
    assert recovered["payload"]["creator_user_id"] == "alice"
    assert await repo.get_conversation(
        deployment_id="wh-1", conversation_key="room", execution_user_id="bob",
    ) is None


async def test_execution_account_conversation_locks_cannot_unlock_another_account() -> None:
    repo = ChannelRepository()
    for account in ["", "alice", "bob"]:
        await repo.upsert_conversation(
            deployment_id="wh", conversation_key="room", session_id=account or "creator",
            execution_user_id=account,
        )
    alice = await repo.acquire_conversation_lock(
        deployment_id="wh", conversation_key="room", owner="worker", execution_user_id="alice",
    )
    bob = await repo.acquire_conversation_lock(
        deployment_id="wh", conversation_key="room", owner="worker", execution_user_id="bob",
    )
    assert alice == bob == 1
    await repo.release_conversation_lock(
        deployment_id="wh", conversation_key="room", owner="worker", generation=alice,
        execution_user_id="alice",
    )
    assert await repo.acquire_conversation_lock(
        deployment_id="wh", conversation_key="room", owner="next", execution_user_id="bob",
    ) is None
    assert await repo.acquire_conversation_lock(
        deployment_id="wh", conversation_key="room", owner="next", execution_user_id="alice",
    ) == 2


async def _delivered_reference_spine(conversation_key=None):

    class ReceiptChannel(_RecorderChannel):
        async def deliver_outbound(self, *, reply_context, text, binding):
            self.delivered.append((reply_context, text))
            return ChannelDeliveryReceipt(message_ids=["private-reply"])

    provider = ReceiptChannel()
    register_channel(provider)
    service, ingress, runtimes, spawned = _spine(binding_extra={"execution_user_id": "alice"})
    provider.inbound = ChannelInbound(
        content="private message", dedup_key="private", conversation_key=conversation_key,
        reply_context={"cb": "x"},
    )
    await service.trigger("wh-1", headers={}, raw_body=b"{}")
    await _drain_spawned(spawned)
    return service, ingress, runtimes, spawned, provider


@pytest.mark.parametrize("conversation_key", [None, "room"])
@pytest.mark.parametrize("reply_conversation_key", [None, "room"])
async def test_reply_reference_cannot_cross_execution_accounts(conversation_key, reply_conversation_key) -> None:
    service, ingress, _runtimes, spawned, provider = await _delivered_reference_spine(conversation_key)
    sent = len(await _inputs())
    service._deployment_repo.get_by_id.return_value["execution_user_id"] = "bob"
    provider.inbound = ChannelInbound(
        content="continue", dedup_key="cross", reference="private-reply",
        conversation_key=reply_conversation_key,
    )
    receipt = await service.trigger("wh-1", headers={}, raw_body=b"{}")
    assert receipt["status"] == "ignored"
    assert receipt["reason"] == "CHANNEL_REFERENCE_OWNER_MISMATCH"
    assert len(await _inputs()) == sent
    assert not spawned
    rejected = await _record("wh-1", "cross")
    assert rejected["state"] == INBOUND_IGNORED
    assert rejected["context_excluded"] is True
    assert not rejected.get("session_id")
    duplicate = await service.trigger("wh-1", headers={}, raw_body=b"{}")
    assert duplicate["status"] == "ignored"
    assert not spawned


@pytest.mark.parametrize("recorded", [False, True])
async def test_source_acknowledges_a_cross_account_refusal_only_after_durable_classification(recorded, monkeypatch) -> None:
    from types import SimpleNamespace
    from astrabox.core.service.orchestrator.channel_source_host import ChannelSourceHost

    service, ingress, _runtimes, spawned, provider = await _delivered_reference_spine("room")
    service._deployment_repo.get_by_id.return_value["execution_user_id"] = "bob"
    if not recorded:
        monkeypatch.setattr(ChannelRepository, "mark_inbound_ignored", AsyncMock(return_value=False))
    host = ChannelSourceHost(
        ingress_service=ingress, deployment_repo=service._deployment_repo,
        spawn_background_task=lambda coro, **kwargs: spawned.append(coro),
    )
    envelope = SimpleNamespace(
        deployment_id="wh-1", source_cursor=2, ack=AsyncMock(), nack=AsyncMock(),
        inbound=ChannelInbound(
            content="continue", dedup_key="cross-source", reference="private-reply", conversation_key="room",
        ),
    )
    sent = len(await _inputs())
    assert await host._handle_envelope(provider.name, envelope) is recorded
    assert len(await _inputs()) == sent
    assert not spawned
    if recorded:
        envelope.ack.assert_awaited_once()
        envelope.nack.assert_not_awaited()
        receipt = envelope.ack.await_args.args[0]
        assert receipt.status == "ignored"
        assert receipt.ack_extra["reason"] == "CHANNEL_REFERENCE_OWNER_MISMATCH"
        service._deployment_repo.advance_channel_source_cursor.assert_awaited_once()
    else:
        envelope.ack.assert_not_awaited()
        envelope.nack.assert_awaited_once()
        service._deployment_repo.advance_channel_source_cursor.assert_not_awaited()


async def test_refused_references_leave_context_but_every_accounts_room_messages_join_it() -> None:
    """What was said in a room is context for whoever the agent answers there,
    whichever account a message ran as; a refused reference never is."""
    repo = ChannelRepository()
    # Recorded as ingress records a room message: it continues the room.
    payload = {
        **_PAYLOAD, "conversation_key": "room", "conversation": "room",
        "binding_revision": "revision",
        "creator_user_id": "bob", "retain_context": True, "provider_ignore_reason": None,
        "message_timestamp": "2026-09-30T00:00:00+00:00", "participant": "participant",
    }
    ordinary, _ = await _claim(repo, dedup_key="ordinary", payload={
        **payload, "content": "ordinary context", "reply_text": "ordinary context",
    })
    blocked, _ = await _claim(repo, dedup_key="blocked", payload={
        **payload, "content": "rejected reference", "reply_text": "rejected reference",
    })
    await _claim(repo, dedup_key="foreign", payload={
        **payload, "creator_user_id": "alice", "content": "private", "reply_text": "private",
    })
    for item, reason, excluded in [
        (ordinary, "message does not mention the bot", False),
        (blocked, "CHANNEL_REFERENCE_OWNER_MISMATCH", True),
    ]:
        assert await repo.mark_inbound_ignored(
            item_id=item["item_id"], owner_token=item["owner_token"], generation=item["generation"],
            reason=reason, exclude_from_context=excluded,
        )
    current, _ = await _claim(repo, dedup_key="current", payload=payload)
    context = await repo.collect_unsubmitted_context(current)
    assert [entry["content"] for entry in context] == ["private", "ordinary context"]
    assert ordinary["item_id"] in {entry["item_id"] for entry in context}


# ── observation and authorization signals ───────────────────────────────────


_MENTION = ChannelAttention(is_mention=True)


def _observation(dedup_key: str, *, participant: str = "u-1", content: str = "the message") -> ChannelInbound:
    return ChannelInbound(
        content=content, dedup_key=dedup_key, participant=participant,
        conversation_key="room", message_timestamp="2026-10-01T00:00:00Z",
        retain_context=True, signal=SIGNAL_OBSERVATION,
    )


def _authorization(dedup_key: str, *, participant: str = "u-1") -> ChannelInbound:
    return ChannelInbound(
        content="", dedup_key=dedup_key, participant=participant, attention=_MENTION,
        reply_context={"cb": "x"}, signal=SIGNAL_AUTHORIZATION,
    )


async def test_authorization_naming_another_sender_is_refused_and_says_so() -> None:
    """The observation recorded who wrote the message. An authorization for
    that message naming a different sender is not that message's trigger: it
    must not run the observed text as someone else's request. The record
    leaves retained context and the authorizing sender is told."""
    provider = _RecorderChannel()
    register_channel(provider)
    service, ingress, _runtimes, spawned = _spine(binding_extra={"updated_at": "rev-1"})
    provider.inbound = _observation("m-x", participant="u-1")
    assert (await service.trigger("wh-1", headers={}, raw_body=b"{}"))["status"] == "observed"
    provider.inbound = _authorization("m-x", participant="u-2")
    receipt = await service.trigger("wh-1", headers={}, raw_body=b"{}")
    assert receipt["status"] == "ignored"
    assert receipt["reason"] == "CHANNEL_SIGNAL_IDENTITY_CONFLICT"
    record = await _record("wh-1", "m-x")
    assert record["state"] == INBOUND_IGNORED and record["context_excluded"] is True
    assert provider.delivered == [({"cb": "x"}, ingress_module._IDENTITY_CONFLICT_NOTICE)]
    assert spawned == [] and await _inputs() == []


async def test_concurrent_admissions_of_one_observed_message_run_it_once() -> None:
    """A context feed recorded the message; its trigger then arrives several
    times at once, a redelivery racing the original. Exactly one admission
    wins, so the observed message is sent once with one accepted receipt."""
    provider = _RecorderChannel()
    register_channel(provider)
    service, ingress, _runtimes, spawned = _spine(binding_extra={"updated_at": "rev-1"})
    provider.inbound = _observation("m-race")
    await service.trigger("wh-1", headers={}, raw_body=b"{}")
    provider.inbound = _authorization("m-race")
    receipts = await asyncio.gather(*(
        service.trigger("wh-1", headers={}, raw_body=b"{}") for _ in range(4)
    ))
    statuses = sorted(receipt["status"] for receipt in receipts)
    assert statuses == ["accepted", "in_progress", "in_progress", "in_progress"]
    await _drain_spawned(spawned)
    (sent,) = await _inputs()
    assert sent["content"] == "the message", "the observed text is the input"
    record = await _record("wh-1", "m-race")
    assert record["state"] == INBOUND_SETTLED


async def test_redelivery_never_replaces_an_admitted_payload() -> None:
    """A redelivery that steals an expired item, or revives a DEAD one, runs
    the message that was admitted. Its input and execution account stay,
    even when the platform resends other content under the same id or the
    binding now runs as someone else."""
    repo = ChannelRepository()
    original = {**_PAYLOAD, "content": "accepted input", "creator_user_id": "alice"}
    changed = {**_PAYLOAD, "content": "replacement", "creator_user_id": "bob"}
    await _claim(repo, dedup_key="m-keep", payload=original, now=time.time() - 100_000)
    stolen, outcome = await _claim(repo, dedup_key="m-keep", payload=changed)
    assert outcome == "claimed"
    admitted = ("content", "creator_user_id")
    assert {key: stolen["payload"][key] for key in admitted} == {
        key: original[key] for key in admitted
    }
    item_id, owner = str(stolen["item_id"]), str(stolen["owner_token"])
    generation = int(stolen["generation"])
    assert await repo.begin_inbound_dispatch(item_id=item_id, owner_token=owner, generation=generation)
    assert await repo.fail_inbound(
        item_id=item_id, owner_token=owner, generation=generation,
        error="final", backoff=(),
    )
    revived, outcome = await _claim(repo, dedup_key="m-keep", payload=changed)
    assert outcome == "claimed" and revived["state"] == INBOUND_RECEIVED
    assert {key: revived["payload"][key] for key in admitted} == {
        key: original[key] for key in admitted
    }


# ── linked execution accounts ───────────────────────────────────────────────


class _LinkResolver(ChannelAccountResolver):
    """A directory a test controls, which counts the questions it is asked."""

    name = "test-links"

    def __init__(self) -> None:
        self.links: dict[str, str] = {}
        self.asked: list[str] = []
        self.unavailable = False

    async def resolve(self, *, deployment_id: str, channel: str, participant: str) -> str | None:
        self.asked.append(participant)
        if self.unavailable:
            raise RuntimeError("directory unavailable")
        return self.links.get(participant)


def _linked_spine(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, ...]:
    resolver = _LinkResolver()
    register_channel_account_resolver(resolver)
    monkeypatch.setenv("ASTRABOX_CHANNEL_ACCOUNT_RESOLVER", resolver.name)
    provider = _RecorderChannel()
    register_channel(provider)
    service, ingress, runtimes, spawned = _spine(binding_extra={
        "execution_mode": "linked", "attention_policy": "mentions", "updated_at": "rev-1",
    })
    return resolver, provider, service, ingress, runtimes, spawned


async def _expire_and_recover(ingress: ChannelIngressService, item_id: str) -> None:
    stored = await ingress._channel_repo.get_inbound(item_id)
    await ingress._channel_repo.renew_inbound_lease(
        item_id=item_id, owner_token=str(stored["owner_token"]),
        generation=int(stored["generation"]), now=time.time() - 100_000,
    )
    assert await ingress.recover_inbound() == 1


async def test_linked_participants_run_as_their_own_accounts_and_keep_them(monkeypatch) -> None:
    """Each participant's message runs as the account linked to that
    participant. The account a message's first drive resolved is kept on its
    record: a process lost before the message's session was bound, and a link
    changed in the meantime, do not change whom the message runs as."""
    resolver, provider, service, ingress, _runtimes, spawned = _linked_spine(monkeypatch)
    resolver.links = {"im:p1": "alice", "im:p2": "bob"}
    sessions = ingress._sessions_repo

    async def ask(dedup_key: str, participant: str, conversation_key: str) -> dict[str, Any]:
        provider.inbound = ChannelInbound(
            content=f"question {dedup_key}", dedup_key=dedup_key, attention=_MENTION,
            conversation_key=conversation_key, participant=participant,
            reply_context={"to": dedup_key},
        )
        return await service.trigger("wh-1", headers={}, raw_body=b"{}")

    first = await ask("m1", "im:p1", "room")
    second = await ask("m2", "im:p2", "room")
    assert first["session_id"] != second["session_id"]
    assert [(await sessions.get_session(receipt["session_id"]))["user_id"]
            for receipt in (first, second)] == ["alice", "bob"]
    await _drain_spawned(spawned)

    bind = ChannelRepository.bind_inbound_dispatch

    async def lost_before_binding(self: ChannelRepository, **kwargs: Any) -> bool:
        if kwargs.get("session_id"):
            monkeypatch.setattr(ChannelRepository, "bind_inbound_dispatch", bind)
            raise RuntimeError("process lost")
        return await bind(self, **kwargs)

    monkeypatch.setattr(ChannelRepository, "bind_inbound_dispatch", lost_before_binding)
    with pytest.raises(RuntimeError, match="process lost"):
        await ask("m3", "im:p1", "room-2")
    item_id = (await _record("wh-1", "m3"))["item_id"]
    kept = await ingress._channel_repo.get_inbound(item_id)
    assert kept["payload"]["execution_user_id"] == "alice" and not kept["session_id"]
    resolver.links["im:p1"] = "carol"
    await _expire_and_recover(ingress, item_id)
    await _drain_spawned(spawned)
    recovered = await ingress._channel_repo.get_inbound(item_id)
    assert recovered["state"] == INBOUND_SETTLED
    assert (await sessions.get_session(recovered["session_id"]))["user_id"] == "alice"
    assert resolver.asked == ["im:p1", "im:p2", "im:p1"]
    assert len(await _inputs()) == 3


@pytest.mark.parametrize(("sender", "reason"), [
    (None, "CHANNEL_PARTICIPANT_REQUIRED"),
    ("im:stranger", "CHANNEL_PARTICIPANT_NOT_LINKED"),
    ("im:p1", "CHANNEL_ACCOUNT_NOT_AUTHORIZED"),
])
async def test_a_linked_message_without_a_usable_account_is_refused_and_told(
    monkeypatch, sender, reason,
) -> None:
    """A message whose participant has no linked account, or whose account
    may not use the Agent, is refused and the participant is told why. It
    never runs as the Agent's owner, and a message that only joins the room's
    context is never resolved to an account at all."""
    resolver, provider, service, ingress, _runtimes, spawned = _linked_spine(monkeypatch)
    resolver.links = {"im:p1": "alice"}
    service._agent_repo.get_agent.return_value = {
        "user_id": "creator", "created_by": "creator", "visibility": "private",
    }
    provider.inbound = ChannelInbound(
        content="chatter", dedup_key="c-1", conversation_key="room", participant="im:p1",
        retain_context=True, message_timestamp="2026-10-01T00:00:00Z",
    )
    assert (await service.trigger("wh-1", headers={}, raw_body=b"{}"))["status"] == "observed"
    if reason != "CHANNEL_ACCOUNT_NOT_AUTHORIZED":
        service._agent_repo.get_agent.return_value = {"user_id": "creator", "template_name": "t"}
    provider.inbound = ChannelInbound(
        content="question", dedup_key="q-1", conversation_key="room", participant=sender,
        reply_context={"to": "q-1"}, attention=_MENTION,
    )
    receipt = await service.trigger("wh-1", headers={}, raw_body=b"{}")
    assert (receipt["status"], receipt["reason"]) == ("ignored", reason)
    record = await _record("wh-1", "q-1")
    assert record["state"] == INBOUND_IGNORED and record["ignored_reason"] == reason
    assert record["context_excluded"] is True and not record["session_id"]
    assert [destination for destination, _ in provider.delivered] == [{"to": "q-1"}]
    assert resolver.asked == ([] if sender is None else [sender])
    assert await _inputs() == [] and not spawned
    again = await service.trigger("wh-1", headers={}, raw_body=b"{}")
    assert again["status"] == "ignored" and len(provider.delivered) == 1


async def test_an_unavailable_directory_holds_a_linked_message_for_retry(monkeypatch) -> None:
    """When the account resolver cannot answer, the message is neither refused
    nor run as anyone: it stays to be driven again, and runs as its linked
    account once the directory answers."""
    resolver, provider, service, ingress, _runtimes, spawned = _linked_spine(monkeypatch)
    resolver.links = {"im:p1": "alice"}
    resolver.unavailable = True
    provider.inbound = ChannelInbound(
        content="question", dedup_key="q-1", conversation_key="room", participant="im:p1",
        reply_context={"to": "q-1"}, attention=_MENTION,
    )
    with pytest.raises(RuntimeError, match="directory unavailable"):
        await service.trigger("wh-1", headers={}, raw_body=b"{}")
    item_id = (await _record("wh-1", "q-1"))["item_id"]
    held = await ingress._channel_repo.get_inbound(item_id)
    assert held["state"] == INBOUND_RECEIVED and not held["session_id"]
    assert held["payload"]["execution_user_id"] == "" and await _inputs() == []
    resolver.unavailable = False
    await _expire_and_recover(ingress, item_id)
    await _drain_spawned(spawned)
    settled = await ingress._channel_repo.get_inbound(item_id)
    assert settled["state"] == INBOUND_SETTLED
    assert (await ingress._sessions_repo.get_session(settled["session_id"]))["user_id"] == "alice"


# ── retained context and form answers (real store) ──────────────────────────


async def test_only_the_items_owner_consumes_the_context_its_turn_quoted() -> None:
    """Consuming a turn's context is part of settling its item, in one
    transaction under the item's fence: a worker whose item was taken over
    consumes nothing, and the owner's settle consumes exactly what it quoted."""
    repo = ChannelRepository()
    retained = {
        **_PAYLOAD, "conversation_key": "room", "conversation": "room",
        "binding_revision": "rev", "retain_context": True, "participant": "u-1",
    }
    quoted, _ = await repo.claim_inbound(
        deployment_id="wh", dedup_key="said", channel_name="recorder", agent_id="agent-1",
        state=INBOUND_OBSERVED, payload={
            **retained, "content": "said", "reply_text": "said",
            "message_timestamp": "2026-10-01T00:00:00Z",
        },
    )
    item, _ = await _claim(repo, dedup_key="wake", payload={
        **retained, "content": "wake", "reply_text": "wake",
        "message_timestamp": "2026-10-01T00:00:01Z",
    }, now=time.time() - 100_000)
    stale = {
        "item_id": item["item_id"], "owner_token": item["owner_token"],
        "generation": item["generation"],
    }
    assert await repo.begin_inbound_dispatch(**stale, now=time.time() - 100_000)
    context = await repo.collect_unsubmitted_context(item)
    assert await repo.freeze_inbound_input(**stale, content="rendered", context=context)
    successor = await repo.claim_inbound_for_recovery(
        item_id=item["item_id"], expected_generation=item["generation"],
    )
    assert successor is not None

    assert not await repo.settle_inbound(**stale, session_id="s1", submit_context=True)
    assert (await repo.get_inbound(quoted["item_id"]))["context_submitted"] is False
    assert await repo.settle_inbound(
        item_id=successor["item_id"], owner_token=successor["owner_token"],
        generation=successor["generation"], session_id="s1", submit_context=True,
    )
    assert (await repo.get_inbound(quoted["item_id"]))["context_submitted"] is True


async def test_a_forms_answers_are_kept_per_question_and_the_last_is_claimed_once() -> None:
    """Each question's answer is its own record, kept by the one participant
    who answered that question first; the last question's answer is claimed
    by one inbound message, and the engine is given every answer in order."""
    repo = ChannelRepository()
    entry = await _outbox(repo)
    generation = await _lease(repo, str(entry["outbox_id"]), owner="A")
    assert generation is not None
    view = {"interaction_id": "form-1", "presentation": "form", "questions": []}
    assert await repo.record_delivery_aliases(
        str(entry["outbox_id"]), owner="A", generation=generation, aliases=["card-1"],
        interaction={"ref": "ref-form", "interaction_id": "form-1", "view": view},
    )
    first = {"row": {"answer": "blue"}, "choice": None, "participant": "u-1"}
    racing = await asyncio.gather(*(
        repo.record_channel_answer(
            deployment_id="wh", ref="ref-form", question_index=0, answer=first,
        )
        for _ in range(4)
    ))
    assert sorted(racing) == [False, False, False, True]
    last = {"row": {"answer": "large"}, "choice": None, "participant": "u-2"}
    claims = await asyncio.gather(*(
        repo.stage_channel_answer(
            deployment_id="wh", ref="ref-form", item_id=f"item-{index}",
            question_index=1, answer=last,
        )
        for index in range(4)
    ))
    assert sorted(claims) == [False, False, False, True]
    record = await repo.get_channel_interaction(deployment_id="wh", ref="ref-form")
    assert record is not None
    assert record["question_index"] == 1 and record["answering_item"] == (
        f"item-{claims.index(True)}"
    )
    assert [(answer["row"], answer["participant"]) for answer in record["answers"]] == [
        ({"answer": "blue"}, "u-1"), ({"answer": "large"}, "u-2"),
    ]
    chain = await repo.resolve_alias(deployment_id="wh", alias="card-1")
    assert chain is not None and (chain["interaction_ref"], chain["question_index"]) == (
        "ref-form", 0,
    )
