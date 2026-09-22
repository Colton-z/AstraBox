"""Channel spine reliability for durable work, fenced ownership, and delivery.

The invariant ledger for docs/channel-spine.md, pinned against the REAL
sqlite collection backend (per-test tmp store) for repository semantics,
plus service-level flows with fakes:

* The work item is durable (full payload) before any ack; a
  crashed worker's item is re-driven by the reconciler from the database,
  never from a platform redelivery;
* Items, conversation locks, and delivery leases carry
  owner_token + monotonic generation; a stale worker's settle/fail/complete
  writes miss their CAS predicates and change nothing;
* attach-not-append — a re-drive whose dispatch token already produced a
  command attaches to it instead of running a second turn;
* Delivery reads the exact bound turn's assistant text, never
  the session's latest;
* The HTTP trigger and any trusted source converge on
  ``ChannelIngressService.ingest``; the receipt statuses are the ack
  contract.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest

import astrabox.core.service.orchestrator.channel_ingress_service as ingress_module
from astrabox.core.service.orchestrator.channel_ingress_service import (
    ChannelIngressService,
)
from astrabox.core.service.orchestrator.channel_spine_reconciler import (
    ChannelSpineReconciler,
)
from astrabox.core.service.orchestrator.deployment_service import DeploymentService
from astrabox.persistence.repository.channel_repository import (
    INBOUND_DEAD,
    INBOUND_IGNORED,
    INBOUND_RECEIVED,
    INBOUND_SETTLED,
    OUTBOX_DEAD,
    OUTBOX_DELIVERED,
    ChannelRepository,
    _OUTBOX_LEASE_SECONDS,
    _scoped_id,
)
from astrabox.persistence.repository.deployment_repository import DeploymentRepository
from astrabox.seams.channel import (
    ChannelAttention,
    ChannelInbound,
    ChannelProvider,
    register_channel,
)


def _item_id(deployment_id: str, key: str) -> str:
    return _scoped_id("inbound", deployment_id, key)


@pytest.fixture(autouse=True)
def _isolated_sqlite_state(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    from astrabox.config.settings import get_settings

    monkeypatch.setenv("ASTRABOX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ASTRABOX_DB_BACKEND", "sqlite")
    monkeypatch.delenv("ASTRABOX_DB_URL", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


_PAYLOAD = {
    "content": "hello",
    "conversation_key": None,
    "reply_context": None,
    "attention": None,
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


async def test_work_item_is_durable_with_full_payload_before_ack() -> None:
    """Invariant A: what claim persisted is enough to drive the turn later —
    content and all — with no help from the source."""
    repo = ChannelRepository()
    doc, outcome = await _claim(repo, payload={**_PAYLOAD, "content": "the message"})
    assert outcome == "claimed"
    stored = await repo.get_inbound(str(doc["_id"]))
    assert stored is not None
    assert stored["state"] == INBOUND_RECEIVED
    assert stored["payload"]["content"] == "the message"
    assert stored["owner_token"] and int(stored["generation"]) == 1


async def test_anonymous_inbound_still_gets_a_recoverable_work_item() -> None:
    repo = ChannelRepository()
    first, o1 = await _claim(repo, dedup_key=None)
    second, o2 = await _claim(repo, dedup_key=None)
    assert (o1, o2) == ("claimed", "claimed")
    assert first["_id"] != second["_id"], "no dedup without a platform message id"
    assert (await repo.get_inbound(str(first["_id"])))["payload"]["content"] == "hello"


async def test_settled_item_dedupes_but_live_is_in_progress() -> None:
    repo = ChannelRepository()
    doc, _ = await _claim(repo)
    _d, before = await _claim(repo)
    assert before == "in_progress"
    assert await repo.begin_inbound_dispatch(
        item_id=str(doc["_id"]),
        owner_token=str(doc["owner_token"]),
        generation=1,
    )
    assert await repo.settle_inbound(
        item_id=str(doc["_id"]),
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
        item_id=str(old["_id"]), owner_token=old_owner, generation=old_generation
    )
    assert not await repo.settle_inbound(
        item_id=str(old["_id"]), owner_token=old_owner,
        generation=old_generation, session_id="stale",
    )
    assert not await repo.fail_inbound(
        item_id=str(old["_id"]), owner_token=old_owner,
        generation=old_generation, error="stale", retry_at_epoch=None,
    )
    assert not await repo.bind_inbound_dispatch(
        item_id=str(old["_id"]), owner_token=old_owner,
        generation=old_generation, session_id="stale",
    )
    current = await repo.get_inbound(str(old["_id"]))
    assert current["state"] == INBOUND_RECEIVED
    assert current["owner_token"] == stolen["owner_token"]
    assert current.get("session_id") is None


async def test_steal_preserves_command_binding_for_attach() -> None:
    repo = ChannelRepository()
    doc, _ = await _claim(repo, now=time.time() - 100_000)
    await repo.bind_inbound_dispatch(
        item_id=str(doc["_id"]),
        owner_token=str(doc["owner_token"]),
        generation=1,
        session_id="sess-1",
        command_id="cmd-1",
        turn_id="turn-1",
    )
    stolen, outcome = await _claim(repo)
    assert outcome == "claimed"
    assert stolen["command_id"] == "cmd-1" and stolen["turn_id"] == "turn-1"


async def test_failed_item_retries_via_watermark_then_dies_with_evidence() -> None:
    repo = ChannelRepository()
    doc, _ = await _claim(repo)
    item_id, owner = str(doc["_id"]), str(doc["owner_token"])
    assert await repo.begin_inbound_dispatch(item_id=item_id, owner_token=owner, generation=1)
    # Retry: back to RECEIVED with the lease expiring at the watermark.
    assert await repo.fail_inbound(
        item_id=item_id, owner_token=owner, generation=1,
        error="turn stream failed", retry_at_epoch=time.time() - 1,
    )
    current = await repo.get_inbound(item_id)
    assert current["state"] == INBOUND_RECEIVED and int(current["attempts"]) == 1
    listed = await repo.list_recoverable_inbound()
    assert [d["_id"] for d in listed] == [item_id]
    # Exhausted: DEAD, terminal, with evidence.
    reclaimed = await repo.claim_inbound_for_recovery(item_id=item_id, expected_generation=1)
    assert reclaimed is not None
    assert await repo.fail_inbound(
        item_id=item_id, owner_token=str(reclaimed["owner_token"]),
        generation=int(reclaimed["generation"]), error="final", retry_at_epoch=None,
    )
    dead = await repo.get_inbound(item_id)
    assert dead["state"] == INBOUND_DEAD and dead["last_error"] == "final"
    assert await repo.list_recoverable_inbound() == []


async def test_dead_item_is_revived_only_by_an_explicit_redelivery() -> None:
    repo = ChannelRepository()
    doc, _ = await _claim(repo)
    item_id, owner = str(doc["_id"]), str(doc["owner_token"])
    await repo.begin_inbound_dispatch(item_id=item_id, owner_token=owner, generation=1)
    await repo.fail_inbound(
        item_id=item_id, owner_token=owner, generation=1, error="final", retry_at_epoch=None
    )
    assert await repo.list_recoverable_inbound() == [], "DEAD is not reconciler-recoverable"
    revived, outcome = await _claim(repo, payload={**_PAYLOAD, "content": "redelivered"})
    assert outcome == "claimed"
    assert revived["state"] == INBOUND_RECEIVED
    assert int(revived["generation"]) == 2
    assert revived["payload"]["content"] == "redelivered"
    assert revived["last_error"] == "final", "prior evidence stays on the row"


async def test_recovery_claim_fences_forward_and_respects_live_renewal() -> None:
    repo = ChannelRepository()
    doc, _ = await _claim(repo)
    item_id, owner = str(doc["_id"]), str(doc["owner_token"])
    # A live item (fresh lease) is not recoverable.
    assert await repo.claim_inbound_for_recovery(item_id=item_id, expected_generation=1) is None
    # Expire the lease via the failure watermark, then recover.
    await repo.begin_inbound_dispatch(item_id=item_id, owner_token=owner, generation=1)
    await repo.fail_inbound(
        item_id=item_id, owner_token=owner, generation=1,
        error="x", retry_at_epoch=time.time() - 1,
    )
    reclaimed = await repo.claim_inbound_for_recovery(item_id=item_id, expected_generation=1)
    assert reclaimed is not None and int(reclaimed["generation"]) == 2
    # A second sweeper holding the STALE generation loses.
    assert await repo.claim_inbound_for_recovery(item_id=item_id, expected_generation=1) is None


async def test_ignored_is_a_terminal_ack_safe_classification() -> None:
    repo = ChannelRepository()
    doc, _ = await _claim(repo)
    assert await repo.mark_inbound_ignored(
        item_id=str(doc["_id"]),
        owner_token=str(doc["owner_token"]),
        generation=1,
        reason="attention_policy=mentions with no attention signal",
    )
    again, outcome = await _claim(repo)
    assert outcome == "ignored" and again["state"] == INBOUND_IGNORED
    assert await repo.list_recoverable_inbound() == []


# ── conversation lock (real store) ───────────────────────────────────────────


async def test_conversation_lock_is_exclusive_then_releasable() -> None:
    repo = ChannelRepository()
    await repo.upsert_conversation(
        deployment_id="wh", conversation_key="c", session_id="s1", agent_id="d"
    )
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
    """Invariant B on the lock: release CAS-matches (owner, generation)."""
    repo = ChannelRepository()
    await repo.upsert_conversation(
        deployment_id="wh", conversation_key="c", session_id="s1", agent_id="d"
    )
    stale_generation = await repo.acquire_conversation_lock(
        deployment_id="wh", conversation_key="c", owner="A", now=time.time() - 100_000
    )
    successor_generation = await repo.acquire_conversation_lock(
        deployment_id="wh", conversation_key="c", owner="B"
    )
    assert successor_generation is not None
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
        "work_item_id": "inbound:item-1",
        "deployment_id": "wh",
        "channel_name": "recorder",
        "session_id": "s1",
        "command_id": "cmd-1",
        "turn_id": "turn-1",
        "reply_context": {"callback_url": "http://x"},
    }
    fields.update(overrides)
    return await repo.create_outbox_entry(**fields)


async def test_outbox_creation_is_idempotent_per_work_item_and_command() -> None:
    """Invariant C: the row is born bound to its exact command/turn, and a
    re-driven settle re-creates the SAME row (no double delivery intent)."""
    repo = ChannelRepository()
    first = await _outbox(repo)
    again = await _outbox(repo)
    assert first["_id"] == again["_id"]
    assert again["command_id"] == "cmd-1" and again["turn_id"] == "turn-1"
    other_turn = await _outbox(repo, command_id="cmd-2", turn_id="turn-2")
    assert other_turn["_id"] != first["_id"]


async def test_outbox_lease_admits_one_deliverer() -> None:
    repo = ChannelRepository()
    entry = await _outbox(repo)
    generations = await asyncio.gather(
        *(
            repo.claim_outbox_for_delivery(str(entry["_id"]), owner=f"o{i}")
            for i in range(4)
        )
    )
    assert len([g for g in generations if g is not None]) == 1


async def test_stale_deliverer_cannot_complete_or_fail_the_successors_row() -> None:
    """Invariant B on delivery: DELIVERED/DEAD/attempts writes CAS on the
    fenced lease; a deliverer whose lease was reclaimed changes nothing."""
    repo = ChannelRepository()
    entry = await _outbox(repo)
    outbox_id = str(entry["_id"])
    stale_generation = await repo.claim_outbox_for_delivery(
        outbox_id, owner="stale", now=time.time() - 100_000
    )
    assert stale_generation is not None
    successor_generation = await repo.claim_outbox_for_delivery(outbox_id, owner="live")
    assert successor_generation is not None

    assert not await repo.mark_outbox_delivered(
        outbox_id, owner="stale", generation=stale_generation
    )
    assert not await repo.record_outbox_failure(
        outbox_id, owner="stale", generation=stale_generation,
        error="stale", next_attempt_at=None,
    )
    assert not await repo.record_delivery_aliases(
        outbox_id, owner="stale", generation=stale_generation, aliases=["zombie"]
    )
    current = await repo.get_outbox_entry(outbox_id)
    assert current["state"] == "SENDING"
    assert int(current["attempts"]) == 0
    assert current["delivery_aliases"] == []
    assert await repo.mark_outbox_delivered(
        outbox_id, owner="live", generation=successor_generation
    )


async def test_retry_holds_the_outbox_lease_across_attempts() -> None:
    repo = ChannelRepository()
    entry = await _outbox(repo)
    outbox_id = str(entry["_id"])
    generation = await repo.claim_outbox_for_delivery(outbox_id, owner="A")
    assert generation is not None
    assert await repo.record_outbox_failure(
        outbox_id, owner="A", generation=generation,
        error="attempt 1", next_attempt_at="soon",
    )
    # Still leased: a concurrent sweep cannot re-lease between attempts.
    assert await repo.claim_outbox_for_delivery(outbox_id, owner="B") is None
    current = await repo.get_outbox_entry(outbox_id)
    assert current["state"] == "SENDING" and int(current["attempts"]) == 1


async def test_outbox_exhaustion_marks_dead_and_leaves_the_sweep() -> None:
    repo = ChannelRepository()
    entry = await _outbox(repo)
    outbox_id = str(entry["_id"])
    generation = await repo.claim_outbox_for_delivery(outbox_id, owner="A")
    assert await repo.record_outbox_failure(
        outbox_id, owner="A", generation=generation, error="final", next_attempt_at=None
    )
    assert (await repo.get_outbox_entry(outbox_id))["state"] == OUTBOX_DEAD
    assert await repo.list_pending_outbox() == []


async def test_crashed_deliverer_expired_sending_row_is_swept() -> None:
    repo = ChannelRepository()
    entry = await _outbox(repo)
    outbox_id = str(entry["_id"])
    assert await repo.claim_outbox_for_delivery(
        outbox_id, owner="crashed", now=time.time() - 100_000
    ) is not None
    listed = await repo.list_pending_outbox()
    assert [d["_id"] for d in listed] == [outbox_id]
    assert await repo.claim_outbox_for_delivery(outbox_id, owner="sweeper") is not None


async def test_aliases_are_persisted_before_delivered() -> None:
    repo = ChannelRepository()
    entry = await _outbox(repo)
    outbox_id = str(entry["_id"])
    generation = await repo.claim_outbox_for_delivery(outbox_id, owner="A")
    assert await repo.record_delivery_aliases(
        outbox_id, owner="A", generation=generation, aliases=["msg_777"]
    )
    assert await repo.mark_outbox_delivered(outbox_id, owner="A", generation=generation)
    current = await repo.get_outbox_entry(outbox_id)
    assert current["delivery_aliases"] == ["msg_777"]
    assert current["state"] == OUTBOX_DELIVERED


def test_outbox_lease_ttl_covers_the_longest_retry_backoff() -> None:
    """If the lease could expire mid-backoff-sleep, a concurrent sweep would
    double-deliver — the exact race the lease exists to prevent."""
    from astrabox.core.service.orchestrator.channel_ingress_service import (
        _OUTBOX_RETRY_BACKOFF_SECONDS,
    )

    assert _OUTBOX_LEASE_SECONDS > max(_OUTBOX_RETRY_BACKOFF_SECONDS) + 60


# ── service flows (fakes for kernel/agent/session, real channel repo) ────────


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


class _FakeKernel:
    """Durable command journal + per-turn assistant messages, faked together.

    ``stream`` appends the command (like the kernel's durable
    ``command.accepted``) and, unless told to fail, records the turn's
    assistant message — so attach queries and turn-bound delivery reads
    behave like the real projection.
    """

    def __init__(self) -> None:
        self.commands: list[dict[str, Any]] = []
        self.turn_texts: dict[tuple[str, str], str] = {}
        self.session_details: dict[str, dict[str, Any]] = {}
        self.interaction_answers: list[tuple[str, str, dict[str, Any]]] = []
        self.timeline: list[tuple[str, str]] = []
        self.stream_calls = 0
        self.fail_next_stream = False
        self.append_before_failing = False

    async def find_command_by_client_message_id(
        self, session_id: str, *, client_message_id: str
    ) -> dict[str, Any] | None:
        for c in self.commands:
            if (
                c["session_id"] == session_id
                and c["payload"]["client_message_id"] == client_message_id
            ):
                return c
        return None

    def record_command(
        self, session_id: str, client_message_id: str
    ) -> tuple[str, str]:
        command_id, turn_id = f"cmd-{uuid.uuid4().hex[:6]}", f"turn-{uuid.uuid4().hex[:6]}"
        self.commands.append(
            {
                "session_id": session_id,
                "turn_id": turn_id,
                "causation_id": command_id,
                "payload": {"client_message_id": client_message_id},
            }
        )
        return command_id, turn_id

    async def get_assistant_message_for_turn(
        self, session_id: str, *, turn_id: str
    ) -> dict[str, Any] | None:
        text = self.turn_texts.get((session_id, turn_id))
        return {"content": text} if text is not None else None

    async def get_session(self, user: Any, session_id: str) -> dict[str, Any]:
        return dict(
            self.session_details.get(
                session_id,
                {
                    "state": "READY",
                    "current_turn_id": None,
                    "pending_interaction": None,
                },
            )
        )

    async def supersede_pending_interaction(
        self,
        user: Any,
        session_id: str,
        interaction_id: str,
    ) -> dict[str, Any]:
        self.interaction_answers.append(
            (session_id, interaction_id, {"decline": True})
        )
        self.timeline.append(("decline", interaction_id))
        self.session_details[session_id] = {
            "state": "READY",
            "current_turn_id": None,
            "pending_interaction": None,
        }
        return {"interaction_id": interaction_id, "answered": True}

    async def resume_command_stream(
        self, user: Any, session_id: str, *, command_id: str
    ) -> Any:
        command = next(
            row for row in self.commands
            if row["session_id"] == session_id and row["causation_id"] == command_id
        )
        if (session_id, command["turn_id"]) not in self.turn_texts:
            raise RuntimeError("fixture command has no settled output")
        yield {"type": "finish"}

    def stream(self, user: Any, session_id: str, content: str, **kwargs: Any) -> Any:
        self.stream_calls += 1
        fail = self.fail_next_stream
        append = not fail or self.append_before_failing
        self.fail_next_stream = False

        async def _agen() -> Any:
            self.timeline.append(("turn", content))
            if append:
                _command_id, turn_id = self.record_command(
                    session_id, str(kwargs.get("client_message_id") or "")
                )
                if not fail:
                    self.turn_texts[(session_id, turn_id)] = "the reply"
            if fail:
                raise RuntimeError("turn crashed")
            yield {"type": "finish"}

        return _agen()


def _spine(
    session_states: dict[str, str] | None = None,
    *,
    binding_extra: dict[str, Any] | None = None,
) -> tuple[DeploymentService, ChannelIngressService, _FakeKernel, list[Any]]:
    binding_row = {
        "deployment_id": "wh-1",
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
    agent_repo.get_agent.return_value = {"user_id": "creator", "template_name": "t"}
    agent_service = AsyncMock()
    counter = {"n": 0}

    async def _start(user, agent_id):
        counter["n"] += 1
        return {"session_id": f"sess-{counter['n']}"}

    agent_service.start_conversation.side_effect = _start
    sessions_repo = AsyncMock()

    async def _get_session(session_id):
        return {"state": (session_states or {}).get(session_id, "READY")}

    sessions_repo.get_session.side_effect = _get_session
    kernel = _FakeKernel()
    spawned: list[Any] = []
    ingress = ChannelIngressService(
        deployment_repo=deployment_repo,
        agent_repo=agent_repo,
        agent_service_getter=lambda: agent_service,
        stream_message_events_ds=kernel.stream,
        sessions_repo=sessions_repo,
        session_events_repo=kernel,
        resume_command_stream=kernel.resume_command_stream,
        message_view=kernel,
        session_detail_getter=kernel.get_session,
        supersede_pending_interaction=kernel.supersede_pending_interaction,
        spawn_background_task=lambda coro, name="": spawned.append(coro),
    )
    service = DeploymentService(
        deployment_repo=deployment_repo,
        agent_repo=agent_repo,
        agent_service_getter=lambda: agent_service,
        stream_message_events_ds=kernel.stream,
        dispatch_turn_input=AsyncMock(),
        sessions_repo=sessions_repo,
        spawn_background_task=lambda coro, name="": spawned.append(coro),
        agent_config=AsyncMock(),
        channel_ingress=ingress,
    )
    service._test_agent_service = agent_service  # type: ignore[attr-defined]
    return service, ingress, kernel, spawned


async def _drain_spawned(spawned: list[Any]) -> None:
    while spawned:
        coro = spawned.pop(0)
        await asyncio.wait_for(coro, timeout=10)


def _close_spawned(spawned: list[Any]) -> None:
    for coro in spawned:
        coro.close()
    spawned.clear()


async def test_redelivery_in_flight_then_settled() -> None:
    provider = _RecorderChannel()
    provider.inbound = ChannelInbound(content="hi", dedup_key="msg-42")
    register_channel(provider)
    service, _ingress, _kernel, spawned = _spine()

    first = await service.trigger("wh-1", headers={}, raw_body=b"{}")
    assert first["status"] == "accepted" and first["session_id"] == "sess-1"

    # Redelivery WHILE the item is live: in_progress ack carrying the
    # session, no new session, no second dispatch.
    inflight = await service.trigger("wh-1", headers={}, raw_body=b"{}")
    assert inflight["status"] == "in_progress"
    assert inflight["session_id"] == "sess-1"
    assert service._test_agent_service.start_conversation.await_count == 1  # type: ignore[attr-defined]

    await _drain_spawned(spawned)
    settled = await service.trigger("wh-1", headers={}, raw_body=b"{}")
    assert settled["status"] == "duplicate" and settled["session_id"] == "sess-1"
    assert service._test_agent_service.start_conversation.await_count == 1  # type: ignore[attr-defined]
    _close_spawned(spawned)


async def test_settled_turn_delivers_the_bound_turns_text() -> None:
    provider = _RecorderChannel()
    provider.inbound = ChannelInbound(
        content="hi", dedup_key="msg-1", reply_context={"cb": "x"}
    )
    register_channel(provider)
    service, _ingress, kernel, spawned = _spine()
    await service.trigger("wh-1", headers={}, raw_body=b"{}")
    await _drain_spawned(spawned)
    assert provider.delivered == [({"cb": "x"}, "the reply")]
    assert kernel.stream_calls == 1


async def test_delivery_reads_the_exact_turn_not_the_sessions_latest() -> None:
    """Invariant C: a newer turn in the same session cannot leak into this
    reply."""
    provider = _RecorderChannel()
    register_channel(provider)
    _service, ingress, kernel, _spawned = _spine()
    repo = ingress._channel_repo
    command_id, turn_id = kernel.record_command("sess-9", "tok")
    kernel.turn_texts[("sess-9", turn_id)] = "first turn's reply"
    _newer_command, newer_turn = kernel.record_command("sess-9", "tok2")
    kernel.turn_texts[("sess-9", newer_turn)] = "SECOND turn's reply"
    entry = await repo.create_outbox_entry(
        work_item_id="item-1",
        deployment_id="wh-1",
        channel_name="recorder",
        session_id="sess-9",
        command_id=command_id,
        turn_id=turn_id,
        reply_context={"cb": "x"},
    )
    await ingress._deliver_outbox_row(entry)
    assert provider.delivered == [({"cb": "x"}, "first turn's reply")]


async def test_crashed_worker_item_is_re_driven_from_the_database() -> None:
    """Invariant A end-to-end: the ack'd message survives losing the process —
    the reconciler re-drives from the persisted payload; no redelivery needed."""
    provider = _RecorderChannel()
    provider.inbound = ChannelInbound(
        content="hi", dedup_key="msg-7", reply_context={"cb": "x"}
    )
    register_channel(provider)
    service, ingress, kernel, spawned = _spine()
    receipt = await service.trigger("wh-1", headers={}, raw_body=b"{}")
    assert receipt["status"] == "accepted"
    # The process "crashes": the spawned drive never runs, its lease lapses.
    _close_spawned(spawned)
    item_id = _item_id("wh-1", "msg-7")
    repo = ingress._channel_repo
    stored = await repo.get_inbound(item_id)
    await repo.renew_inbound_lease(  # force-expire via a direct write below
        item_id=item_id,
        owner_token=str(stored["owner_token"]),
        generation=1,
        now=time.time() - 100_000,
    )
    recovered = await ingress.recover_inbound()
    assert recovered == 1
    final = await repo.get_inbound(item_id)
    assert final["state"] == INBOUND_SETTLED
    assert provider.delivered == [({"cb": "x"}, "the reply")]
    assert kernel.stream_calls == 1


async def test_recovery_attaches_to_the_prior_dispatch_instead_of_re_running() -> None:
    """Attach-not-append: a worker that died AFTER the kernel accepted the
    command must not cause a second turn — recovery finds the command by its
    dispatch token, observes its durable output, settles, delivers."""
    provider = _RecorderChannel()
    provider.inbound = ChannelInbound(
        content="hi", dedup_key="msg-8", reply_context={"cb": "x"}
    )
    register_channel(provider)
    service, ingress, kernel, spawned = _spine()
    await service.trigger("wh-1", headers={}, raw_body=b"{}")
    _close_spawned(spawned)  # worker died before driving
    repo = ingress._channel_repo
    item_id = _item_id("wh-1", "msg-8")
    stored = await repo.get_inbound(item_id)
    # The prior worker's kernel dispatch DID land (command + output durable)…
    _command_id, turn_id = kernel.record_command("sess-1", f"{item_id}::a0")
    kernel.turn_texts[("sess-1", turn_id)] = "the reply"
    kernel.session_details["sess-1"] = {
        "state": "WAITING_INPUT",
        "current_turn_id": "newer-turn",
        "pending_interaction": {
            "interaction_id": "newer-question",
            "presentation": "form",
            "tool_name": "AskUserQuestion",
        },
    }
    # …but the worker died before settling; its lease lapses.
    await repo.renew_inbound_lease(
        item_id=item_id,
        owner_token=str(stored["owner_token"]),
        generation=1,
        now=time.time() - 100_000,
    )
    assert await ingress.recover_inbound() == 1
    final = await repo.get_inbound(item_id)
    assert final["state"] == INBOUND_SETTLED
    assert final["turn_id"] == turn_id, "bound to the ORIGINAL dispatch"
    assert kernel.stream_calls == 0, "no second turn was dispatched"
    assert kernel.interaction_answers == [], (
        "attaching an older work item must not decline a newer interaction"
    )
    assert provider.delivered == [({"cb": "x"}, "the reply")]


async def test_new_channel_message_declines_open_question_before_its_turn() -> None:
    provider = _RecorderChannel()
    provider.inbound = ChannelInbound(
        content="the newer message",
        dedup_key="msg-supersedes-question",
        conversation_key="thread-1",
    )
    register_channel(provider)
    service, ingress, kernel, spawned = _spine()

    receipt = await service.trigger("wh-1", headers={}, raw_body=b"{}")
    assert receipt["status"] == "accepted"
    kernel.session_details["sess-1"] = {
        "state": "WAITING_INPUT",
        "current_turn_id": "question-turn",
        "pending_interaction": {
            "interaction_id": "question-1",
            "presentation": "form",
            "tool_name": "AnotherEngineQuestion",
        },
    }

    await _drain_spawned(spawned)

    assert kernel.interaction_answers == [
        ("sess-1", "question-1", {"decline": True})
    ]
    assert kernel.timeline == [
        ("decline", "question-1"),
        ("turn", "the newer message"),
    ]
    item = await ingress._channel_repo.get_inbound(
        _item_id("wh-1", "msg-supersedes-question")
    )
    assert item is not None and item["state"] == INBOUND_SETTLED


async def test_failed_turn_retries_with_a_fresh_token_after_backoff() -> None:
    """A RECORDED failure is not a crash: the retry runs a NEW turn (fresh
    dispatch token), bounded by the backoff schedule."""
    provider = _RecorderChannel()
    provider.inbound = ChannelInbound(content="hi", dedup_key="msg-9")
    register_channel(provider)
    service, ingress, kernel, spawned = _spine()
    kernel.fail_next_stream = True
    kernel.append_before_failing = True
    await service.trigger("wh-1", headers={}, raw_body=b"{}")
    await _drain_spawned(spawned)  # the drive records the failure
    repo = ingress._channel_repo
    item_id = _item_id("wh-1", "msg-9")
    failed = await repo.get_inbound(item_id)
    assert failed["state"] == INBOUND_RECEIVED and int(failed["attempts"]) == 1
    # Redelivery inside the backoff window: still in_progress, not a re-drive.
    redelivered = await service.trigger("wh-1", headers={}, raw_body=b"{}")
    assert redelivered["status"] == "in_progress"
    # Past the watermark (simulated by sweeping with a future clock), the
    # reconciler re-drives with the NEXT token ::a1.
    watermark_passed = time.time() + 3600
    recovered_items = await repo.list_recoverable_inbound(now=watermark_passed)
    assert [d["_id"] for d in recovered_items] == [item_id]
    reclaimed = await repo.claim_inbound_for_recovery(
        item_id=item_id,
        expected_generation=int(failed["generation"]),
        now=watermark_passed,
    )
    assert reclaimed is not None
    await ingress._drive_work_item(reclaimed)
    final = await repo.get_inbound(item_id)
    assert final["state"] == INBOUND_SETTLED
    assert kernel.stream_calls == 2, "the retry legitimately ran a new turn"
    assert {c["payload"]["client_message_id"] for c in kernel.commands} == {
        f"{item_id}::a0", f"{item_id}::a1",
    }
    _close_spawned(spawned)


async def test_attention_policy_ignores_without_a_signal_and_admits_with_one() -> None:
    provider = _RecorderChannel()
    provider.inbound = ChannelInbound(content="hi", dedup_key="msg-10")
    register_channel(provider)
    service, ingress, _kernel, spawned = _spine(
        binding_extra={"attention_policy": "mentions"}
    )
    ignored = await service.trigger("wh-1", headers={}, raw_body=b"{}")
    assert ignored["status"] == "ignored"
    assert spawned == [], "no dispatch for an ignored message"
    item_id = _item_id("wh-1", "msg-10")
    assert (await ingress._channel_repo.get_inbound(item_id))["state"] == INBOUND_IGNORED

    provider.inbound = ChannelInbound(
        content="hi @agent",
        dedup_key="msg-11",
        attention=ChannelAttention(is_mention=True),
    )
    admitted = await service.trigger("wh-1", headers={}, raw_body=b"{}")
    assert admitted["status"] == "accepted"
    _close_spawned(spawned)


async def test_conversation_key_routes_to_the_same_session() -> None:
    provider = _RecorderChannel()
    register_channel(provider)
    service, _ingress, _kernel, spawned = _spine()
    provider.inbound = ChannelInbound(content="a", dedup_key="m1", conversation_key="room")
    first = await service.trigger("wh-1", headers={}, raw_body=b"{}")
    await _drain_spawned(spawned)
    provider.inbound = ChannelInbound(content="b", dedup_key="m2", conversation_key="room")
    second = await service.trigger("wh-1", headers={}, raw_body=b"{}")
    await _drain_spawned(spawned)
    assert first["session_id"] == second["session_id"] == "sess-1"
    assert service._test_agent_service.start_conversation.await_count == 1  # type: ignore[attr-defined]


async def test_terminated_mapped_session_is_replaced() -> None:
    provider = _RecorderChannel()
    register_channel(provider)
    service, _ingress, _kernel, spawned = _spine(
        session_states={"sess-1": "TERMINATED"}
    )
    provider.inbound = ChannelInbound(content="a", dedup_key="m1", conversation_key="room")
    first = await service.trigger("wh-1", headers={}, raw_body=b"{}")
    assert first["session_id"] == "sess-1"
    _close_spawned(spawned)
    # sess-1 is TERMINATED when the next message resolves the mapping: the
    # spine starts a replacement and CAS-swaps it into the conversation.
    provider.inbound = ChannelInbound(content="b", dedup_key="m2", conversation_key="room")
    second = await service.trigger("wh-1", headers={}, raw_body=b"{}")
    assert second["session_id"] == "sess-2"
    _close_spawned(spawned)


async def test_outbox_retries_then_delivers() -> None:
    provider = _RecorderChannel(fail_times=2)
    provider.inbound = ChannelInbound(
        content="hi", dedup_key="m1", reply_context={"cb": "x"}
    )
    register_channel(provider)
    monkey_backoff = (0.0, 0.0, 0.0)
    import astrabox.core.service.orchestrator.channel_ingress_service as mod
    original = mod._OUTBOX_RETRY_BACKOFF_SECONDS
    mod._OUTBOX_RETRY_BACKOFF_SECONDS = monkey_backoff
    try:
        service, _ingress, _kernel, spawned = _spine()
        await service.trigger("wh-1", headers={}, raw_body=b"{}")
        await _drain_spawned(spawned)
    finally:
        mod._OUTBOX_RETRY_BACKOFF_SECONDS = original
    assert len(provider.delivered) == 1


async def test_outbox_exhaustion_marks_dead_and_turn_stays_settled() -> None:
    """Provider failure records retry/dead evidence without altering the turn."""
    provider = _RecorderChannel(fail_times=99)
    provider.inbound = ChannelInbound(
        content="hi", dedup_key="m1", reply_context={"cb": "x"}
    )
    register_channel(provider)
    import astrabox.core.service.orchestrator.channel_ingress_service as mod
    original = mod._OUTBOX_RETRY_BACKOFF_SECONDS
    mod._OUTBOX_RETRY_BACKOFF_SECONDS = (0.0,)
    try:
        service, ingress, _kernel, spawned = _spine()
        await service.trigger("wh-1", headers={}, raw_body=b"{}")
        await _drain_spawned(spawned)
    finally:
        mod._OUTBOX_RETRY_BACKOFF_SECONDS = original
    repo = ingress._channel_repo
    item_id = _item_id("wh-1", "m1")
    item = await repo.get_inbound(item_id)
    assert item["state"] == INBOUND_SETTLED, "delivery failure never unsettles the turn"
    assert await repo.list_pending_outbox() == [], "DEAD rows leave the sweep"
    outbox = await repo.get_outbox_entry(
        _scoped_id("outbox", item_id, str(item["command_id"]))
    )
    assert outbox is not None and outbox["state"] == OUTBOX_DEAD
    assert outbox["last_error"].endswith("callback 5xx")
    assert provider.delivered == []


async def test_reconciler_tick_recovers_both_surfaces() -> None:
    provider = _RecorderChannel()
    provider.inbound = ChannelInbound(
        content="hi", dedup_key="m-tick", reply_context={"cb": "x"}
    )
    register_channel(provider)
    service, ingress, _kernel, spawned = _spine()
    await service.trigger("wh-1", headers={}, raw_body=b"{}")
    _close_spawned(spawned)
    repo = ingress._channel_repo
    item_id = _item_id("wh-1", "m-tick")
    stored = await repo.get_inbound(item_id)
    await repo.renew_inbound_lease(
        item_id=item_id, owner_token=str(stored["owner_token"]),
        generation=1, now=time.time() - 100_000,
    )
    reconciler = ChannelSpineReconciler(
        ingress_service=ingress, spawn_background_task=lambda coro, name="": coro
    )
    summary = await reconciler.scan_once()
    assert summary["inbound_recovered"] == 1
    assert (await repo.get_inbound(item_id))["state"] == INBOUND_SETTLED
    assert provider.delivered == [({"cb": "x"}, "the reply")]


async def test_ingest_is_reachable_without_http() -> None:
    """A trusted source durably claims a typed inbound before acknowledging it."""
    provider = _RecorderChannel()
    register_channel(provider)
    _service, ingress, _kernel, spawned = _spine()
    receipt = await ingress.ingest(
        "wh-1",
        ChannelInbound(content="from a broker", dedup_key="b-1"),
    )
    assert receipt.status == "accepted"
    stored = await ingress._channel_repo.get_inbound(receipt.item_id)
    assert stored is not None and stored["payload"]["content"] == "from a broker"
    assert stored["state"] == INBOUND_RECEIVED
    _close_spawned(spawned)


# ── Streaming delivery, receipts, reply chains, and broker sources ──────────


class _FakeFrames:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    def add_text(self, session_id: str, command_id: str, seq: int, delta: str) -> None:
        self.frames.append(
            {
                "session_id": session_id,
                "command_id": command_id,
                "frame_seq": seq,
                "type": "text-delta",
                "delta": delta,
            }
        )

    async def list_frames(
        self,
        session_id: str,
        *,
        command_id: str | None = None,
        turn_id: str | None = None,
        after_seq: int = -1,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        out = [
            f
            for f in self.frames
            if f["session_id"] == session_id
            and (command_id is None or f["command_id"] == command_id)
            and int(f["frame_seq"]) > after_seq
        ]
        return sorted(out, key=lambda f: int(f["frame_seq"]))[:limit]


from astrabox.seams.channel import (  # noqa: E402
    ChannelDeliveryHandle,
    ChannelDeliveryReceipt,
    ChannelEvent,
    ChannelSourceEnvelope,
)


class _RecordingHandle(ChannelDeliveryHandle):
    def __init__(self) -> None:
        self.events: list[ChannelEvent] = []
        self.closed = False

    async def emit(self, event: ChannelEvent) -> ChannelDeliveryReceipt | None:
        self.events.append(event)
        if event.type == "turn_started":
            return ChannelDeliveryReceipt(message_ids=["card-1"])
        if event.type == "settled":
            return ChannelDeliveryReceipt(message_ids=["card-1", "card-final"])
        return None

    async def close(self) -> None:
        self.closed = True


class _StreamingChannel(ChannelProvider):
    name = "streamer"
    supports_streaming_delivery = True

    def __init__(self) -> None:
        self.inbound = ChannelInbound(content="mapped")
        self.opened_with: list[list[str]] = []
        self.handles: list[_RecordingHandle] = []

    def verify_and_resolve(self, *, headers, raw_body, binding) -> ChannelInbound:
        return self.inbound

    async def open_delivery(
        self,
        *,
        reply_context: dict[str, Any],
        prior_aliases: list[str],
        binding: dict[str, Any],
    ) -> ChannelDeliveryHandle:
        _ = binding
        self.opened_with.append(list(prior_aliases))
        handle = _RecordingHandle()
        self.handles.append(handle)
        return handle


def _streaming_spine(
    session_states: dict[str, str] | None = None,
) -> tuple[DeploymentService, ChannelIngressService, _FakeKernel, _FakeFrames, list[Any]]:
    binding_row = {
        "deployment_id": "wh-1",
        "agent_id": "agent-1",
        "scene": "channel:streamer",
        "prompt_prefix": "",
        "secret": "s",
        "enabled": True,
    }
    deployment_repo = AsyncMock()
    deployment_repo.get_by_id.return_value = binding_row
    agent_repo = AsyncMock()
    agent_repo.get_agent.return_value = {"user_id": "creator", "template_name": "t"}
    agent_service = AsyncMock()
    counter = {"n": 0}

    async def _start(user, agent_id):
        counter["n"] += 1
        return {"session_id": f"sess-{counter['n']}"}

    agent_service.start_conversation.side_effect = _start
    sessions_repo = AsyncMock()

    async def _get_session(session_id):
        return {"state": (session_states or {}).get(session_id, "READY")}

    sessions_repo.get_session.side_effect = _get_session
    kernel = _FakeKernel()
    frames = _FakeFrames()
    kernel.list_frames = frames.list_frames

    def _stream_with_frames(user: Any, session_id: str, content: str, **kwargs: Any) -> Any:
        kernel.stream_calls += 1

        async def _agen() -> Any:
            command_id, turn_id = kernel.record_command(
                session_id, str(kwargs.get("client_message_id") or "")
            )
            frames.add_text(session_id, command_id, 1, "Hel")
            frames.add_text(session_id, command_id, 2, "lo")
            kernel.turn_texts[(session_id, turn_id)] = "Hello"
            yield {"type": "finish"}

        return _agen()

    spawned: list[Any] = []
    ingress = ChannelIngressService(
        deployment_repo=deployment_repo,
        agent_repo=agent_repo,
        agent_service_getter=lambda: agent_service,
        stream_message_events_ds=_stream_with_frames,
        sessions_repo=sessions_repo,
        session_events_repo=kernel,
        resume_command_stream=kernel.resume_command_stream,
        message_view=kernel,
        session_detail_getter=kernel.get_session,
        supersede_pending_interaction=kernel.supersede_pending_interaction,
        spawn_background_task=lambda coro, name="": spawned.append(coro),
    )
    service = DeploymentService(
        deployment_repo=deployment_repo,
        agent_repo=agent_repo,
        agent_service_getter=lambda: agent_service,
        stream_message_events_ds=_stream_with_frames,
        dispatch_turn_input=AsyncMock(),
        sessions_repo=sessions_repo,
        spawn_background_task=lambda coro, name="": spawned.append(coro),
        agent_config=AsyncMock(),
        channel_ingress=ingress,
    )
    return service, ingress, kernel, frames, spawned


async def test_streaming_provider_observes_ordered_progress_and_settlement() -> None:
    """Streaming emits ordered progress and one settlement.

    The outbox row ends DELIVERED with aliases persisted before completion.
    """
    provider = _StreamingChannel()
    provider.inbound = ChannelInbound(
        content="hi", dedup_key="s-1", reply_context={"chat": "c1"}
    )
    register_channel(provider)
    service, ingress, kernel, _frames, spawned = _streaming_spine()
    receipt = await service.trigger("wh-1", headers={}, raw_body=b"{}")
    assert receipt["status"] == "accepted"
    await _drain_spawned(spawned)

    (handle,) = provider.handles
    kinds = [e.type for e in handle.events]
    assert kinds[0] == "turn_started"
    assert kinds[-1] == "settled"
    assert kinds.count("settled") == 1
    assert [e.seq for e in handle.events] == sorted(e.seq for e in handle.events)
    progress = [e for e in handle.events if e.type == "progress"]
    assert progress and progress[-1].text == "Hello", "progress carries coalesced text"
    assert handle.events[-1].text == "Hello"
    assert handle.closed

    item_id = _item_id("wh-1", "s-1")
    item = await ingress._channel_repo.get_inbound(item_id)
    outbox = await ingress._channel_repo.get_outbox_entry(
        _scoped_id("outbox", item_id, str(item["command_id"]))
    )
    assert outbox["state"] == OUTBOX_DELIVERED
    assert outbox["delivery_aliases"] == ["card-1", "card-final"]
    assert kernel.stream_calls == 1


async def test_crashed_streaming_delivery_resumes_with_prior_aliases() -> None:
    """A retry after delivery creation updates the same aliases.

    Resuming the delivery must not emit a second ``turn_started`` event.
    """
    provider = _StreamingChannel()
    provider.inbound = ChannelInbound(
        content="hi", dedup_key="s-2", reply_context={"chat": "c1"}
    )
    register_channel(provider)
    service, ingress, _kernel, _frames, spawned = _streaming_spine()
    await service.trigger("wh-1", headers={}, raw_body=b"{}")
    await _drain_spawned(spawned)
    repo = ingress._channel_repo
    item_id = _item_id("wh-1", "s-2")
    item = await repo.get_inbound(item_id)
    outbox_id = _scoped_id("outbox", item_id, str(item["command_id"]))

    # Simulate the crash-after-create window: rewind the row to SENDING with
    # an expired lease, aliases + cursor already durable.
    collection_entry = await repo.get_outbox_entry(outbox_id)
    from astrabox.persistence.repository.backend import get_async_collection
    from astrabox.persistence.repository.channel_repository import OUTBOX_COLLECTION

    collection = await get_async_collection(OUTBOX_COLLECTION)
    await collection.update_one(
        {"_id": outbox_id},
        {
            "$set": {
                "state": "SENDING",
                "lease_owner": "crashed",
                "lease_expires_epoch": time.time() - 100_000,
                "delivery_aliases": ["card-1"],
            }
        },
    )
    swept = await ingress.sweep_pending_outbox()
    assert swept == 1
    resumed_handle = provider.handles[-1]
    assert provider.opened_with[-1] == ["card-1"], "resume carries prior aliases"
    kinds = [e.type for e in resumed_handle.events]
    assert "turn_started" not in kinds, "no duplicate card creation on resume"
    assert kinds[-1] == "settled"
    assert (await repo.get_outbox_entry(outbox_id))["state"] == OUTBOX_DELIVERED
    _ = collection_entry


async def test_simple_provider_receipt_aliases_resolve_reply_chains() -> None:
    """A delivered platform alias resumes its conversation.

    An unknown reply reference starts a new chain.
    """

    class _ReceiptChannel(_RecorderChannel):
        name = "receipter"

        async def deliver_outbound(self, *, reply_context, text, binding):
            _ = binding
            self.delivered.append((reply_context, text))
            return ChannelDeliveryReceipt(message_ids=["msg-777"])

    provider = _ReceiptChannel()
    register_channel(provider)
    service, ingress, _kernel, spawned = _spine(
        binding_extra={"scene": "channel:receipter"}
    )
    provider.inbound = ChannelInbound(
        content="a", dedup_key="r-1", conversation_key="room-7",
        reply_context={"cb": "x"},
    )
    first = await service.trigger("wh-1", headers={}, raw_body=b"{}")
    await _drain_spawned(spawned)
    resolved = await ingress._channel_repo.resolve_alias(
        deployment_id="wh-1", alias="msg-777"
    )
    assert resolved is not None and resolved["conversation_key"] == "room-7"

    # A reply quoting the delivered message (reference only, no conversation
    # key from the platform) lands in the same conversation/session.
    provider.inbound = ChannelInbound(content="b", dedup_key="r-2", reference="msg-777")
    second = await service.trigger("wh-1", headers={}, raw_body=b"{}")
    assert second["session_id"] == first["session_id"]
    _close_spawned(spawned)

    # An unknown reference starts a new chain (after the bounded window).
    import astrabox.core.service.orchestrator.channel_ingress_service as mod
    original = (mod._REFERENCE_RESOLVE_ATTEMPTS, mod._REFERENCE_RESOLVE_WAIT_SECONDS)
    mod._REFERENCE_RESOLVE_ATTEMPTS, mod._REFERENCE_RESOLVE_WAIT_SECONDS = 1, 0.0
    try:
        provider.inbound = ChannelInbound(
            content="c", dedup_key="r-3", reference="never-delivered"
        )
        third = await service.trigger("wh-1", headers={}, raw_body=b"{}")
    finally:
        mod._REFERENCE_RESOLVE_ATTEMPTS, mod._REFERENCE_RESOLVE_WAIT_SECONDS = original
    assert third["session_id"] != first["session_id"]
    _close_spawned(spawned)


class _OneShotEnvelope(ChannelSourceEnvelope):
    def __init__(self, deployment_id: str, inbound: ChannelInbound, repo: Any) -> None:
        self.deployment_id = deployment_id
        self.inbound = inbound
        self._repo = repo
        self.acked: Any = None
        self.nacked: str | None = None
        self.claim_was_durable_at_ack: bool | None = None

    async def ack(self, receipt: Any) -> None:
        stored = await self._repo.get_inbound(receipt.item_id)
        self.claim_was_durable_at_ack = stored is not None
        self.acked = receipt

    async def nack(self, error: str) -> None:
        self.nacked = error


async def test_broker_source_acks_only_after_the_durable_claim() -> None:
    """A broker source acknowledges only after the typed inbound is durable."""
    from astrabox.core.service.orchestrator.channel_source_host import (
        ChannelSourceHost,
    )

    provider = _RecorderChannel()
    register_channel(provider)
    _service, ingress, _kernel, spawned = _spine()
    envelope = _OneShotEnvelope(
        "wh-1",
        ChannelInbound(content="from the broker", dedup_key="b-2"),
        ingress._channel_repo,
    )
    envelope.source_cursor = 7
    deployment_repo = AsyncMock()

    async def _advance_after_claim(*_args: Any, **_kwargs: Any) -> bool:
        assert await ingress._channel_repo.get_inbound(_item_id("wh-1", "b-2")) is not None
        return True

    deployment_repo.advance_channel_source_cursor.side_effect = _advance_after_claim
    host = ChannelSourceHost(
        ingress_service=ingress,
        deployment_repo=deployment_repo,
        spawn_background_task=lambda coro, name="": coro,
    )
    handled = await host._handle_envelope("recorder", envelope)
    assert handled is True
    assert envelope.nacked is None
    assert envelope.acked is not None and envelope.acked.status == "accepted"
    assert envelope.claim_was_durable_at_ack is True
    deployment_repo.advance_channel_source_cursor.assert_awaited_once_with(
        "wh-1", scene="channel:recorder", source_cursor=7
    )
    _close_spawned(spawned)


async def test_channel_source_cursor_is_discovered_and_advances_monotonically() -> None:
    repo = DeploymentRepository()
    await repo.upsert(
        "channel-1",
        {
            "deployment_id": "channel-1",
            "agent_id": "agent-1",
            "scene": "channel:telegram",
            "secret": "secret",
            "enabled": True,
            "deleted": False,
        },
    )
    await repo.upsert(
        "webhook-1",
        {
            "deployment_id": "webhook-1",
            "agent_id": "agent-1",
            "scene": "hmac",
            "enabled": True,
            "deleted": False,
        },
    )

    assert [row["deployment_id"] for row in await repo.list_channel_bindings()] == [
        "channel-1"
    ]
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


async def test_broker_source_nacks_when_ingest_fails() -> None:
    from astrabox.core.service.orchestrator.channel_source_host import (
        ChannelSourceHost,
    )

    provider = _RecorderChannel()
    register_channel(provider)
    _service, ingress, _kernel, spawned = _spine()
    ingress._deployment_repo.get_by_id.return_value = None  # binding vanished
    envelope = _OneShotEnvelope(
        "wh-1",
        ChannelInbound(content="x", dedup_key="b-3"),
        ingress._channel_repo,
    )
    host = ChannelSourceHost(
        ingress_service=ingress,
        deployment_repo=AsyncMock(),
        spawn_background_task=lambda coro, name="": coro,
    )
    await host._handle_envelope("recorder", envelope)
    assert envelope.acked is None
    assert envelope.nacked is not None
    _close_spawned(spawned)


async def test_source_host_starts_loops_only_for_sourcing_providers() -> None:
    from astrabox.core.service.orchestrator.channel_source_host import (
        ChannelSourceHost,
    )

    class _SourcingChannel(_RecorderChannel):
        name = "sourcing_recorder"
        supports_source = True

        def open_source(self, *, binding) -> Any:
            _ = binding

            async def _gen():
                if False:  # pragma: no cover - never yields in this test
                    yield None

            return _gen()

    register_channel(_RecorderChannel())
    register_channel(_SourcingChannel())
    spawned_names: list[str] = []

    class _Task:
        def done(self) -> bool:
            return False

        def cancel(self, *_a: Any) -> None:
            return None

    def _spawn(coro: Any, name: str = "") -> Any:
        spawned_names.append(name)
        coro.close()
        return _Task()

    repo = AsyncMock()
    repo.list_channel_bindings.return_value = [
        {
            "deployment_id": "source-binding",
            "scene": "channel:sourcing_recorder",
            "secret": "secret",
            "enabled": True,
        },
        {
            "deployment_id": "plain-binding",
            "scene": "channel:recorder",
            "secret": "secret",
            "enabled": True,
        },
    ]
    host = ChannelSourceHost(
        ingress_service=AsyncMock(),
        deployment_repo=repo,
        spawn_background_task=_spawn,
    )
    await host._reconcile_once()
    assert "channel-source-sourcing_recorder-source-binding" in spawned_names
    assert "channel-source-recorder-plain-binding" not in spawned_names, (
        "a provider without the source capability must not get a consume loop"
    )
    host.quiesce()


async def test_source_reconcile_waits_for_a_disabled_binding_to_stop() -> None:
    from astrabox.core.service.orchestrator.channel_source_host import (
        ChannelSourceHost,
    )

    stopped = asyncio.Event()

    async def _existing_consumer() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            stopped.set()

    task = asyncio.create_task(_existing_consumer())
    await asyncio.sleep(0)
    repo = AsyncMock()
    repo.list_channel_bindings.return_value = []
    host = ChannelSourceHost(
        ingress_service=AsyncMock(),
        deployment_repo=repo,
        spawn_background_task=lambda *_args, **_kwargs: pytest.fail(
            "a disabled binding must not start a replacement consumer"
        ),
    )
    host._tasks["source-binding"] = task
    host._fingerprints["source-binding"] = "prior"

    await host.reconcile()

    assert stopped.is_set(), "reconcile returned before the source connector stopped"
    assert task.done()
    assert "source-binding" not in host._tasks


# ── crash windows around outbox creation and settlement ──────────────────────


async def test_crash_between_outbox_create_and_settle_recovers_exactly_once() -> None:
    """The reply intent is durable BEFORE the item goes terminal. A
    worker that died after creating the outbox row but before settling leaves
    a LIVE item; recovery attaches to the same command, re-creates the SAME
    row (idempotent id), settles, and delivers exactly once."""
    provider = _RecorderChannel()
    register_channel(provider)
    _service, ingress, kernel, spawned = _spine()
    repo = ingress._channel_repo

    item, outcome = await repo.claim_inbound(
        deployment_id="wh-1",
        dedup_key="crash-1",
        channel_name="recorder",
        agent_id="agent-1",
        payload={**_PAYLOAD, "reply_context": {"cb": "x"}},
    )
    assert outcome == "claimed"
    item_id = str(item["_id"])
    assert await repo.begin_inbound_dispatch(
        item_id=item_id, owner_token=str(item["owner_token"]), generation=1
    )
    # The dead worker got this far: session bound, command appended (durable
    # in the kernel), assistant output durable, outbox row created — then it
    # died BEFORE settle_inbound.
    await repo.bind_inbound_dispatch(
        item_id=item_id, owner_token=str(item["owner_token"]), generation=1,
        session_id="sess-1",
    )
    command_id, turn_id = kernel.record_command("sess-1", f"{item_id}::a0")
    kernel.turn_texts[("sess-1", turn_id)] = "the reply"
    await repo.create_outbox_entry(
        work_item_id=item_id,
        deployment_id="wh-1",
        channel_name="recorder",
        session_id="sess-1",
        command_id=command_id,
        turn_id=turn_id,
        reply_context={"cb": "x"},
    )
    await repo.renew_inbound_lease(
        item_id=item_id, owner_token=str(item["owner_token"]), generation=1,
        now=time.time() - 100_000,
    )

    assert await ingress.recover_inbound() == 1
    final = await repo.get_inbound(item_id)
    assert final["state"] == INBOUND_SETTLED
    assert provider.delivered == [({"cb": "x"}, "the reply")]
    assert kernel.stream_calls == 0, "recovery attached; no second turn"
    # Nothing left for the sweep: exactly once.
    assert await ingress.sweep_pending_outbox() == 0
    assert provider.delivered == [({"cb": "x"}, "the reply")]
    _close_spawned(spawned)


async def test_stolen_live_worker_cannot_append_after_its_renew_misses() -> None:
    """The renew before the command append is a FENCE — a worker whose
    claim was stolen mid-wait aborts without dispatching a turn."""
    provider = _RecorderChannel()
    provider.inbound = ChannelInbound(content="hi", dedup_key="steal-1")
    register_channel(provider)
    _service, ingress, kernel, spawned = _spine()
    repo = ingress._channel_repo

    item, outcome = await repo.claim_inbound(
        deployment_id="wh-1",
        dedup_key="steal-1",
        channel_name="recorder",
        agent_id="agent-1",
        payload=dict(_PAYLOAD),
    )
    assert outcome == "claimed"
    item_id = str(item["_id"])

    stolen: dict[str, Any] = {}

    async def _wait_and_get_stolen(session_id: str) -> bool:
        # While worker A sits in the ready-wait, its lease lapses and the
        # reconciler (worker B) steals the item.
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
        return True

    ingress._wait_session_ready = _wait_and_get_stolen  # type: ignore[method-assign]
    await ingress._drive_work_item(dict(item))

    assert kernel.stream_calls == 0, "the stale worker must not append a command"
    current = await repo.get_inbound(item_id)
    assert current["owner_token"] == stolen["owner_token"]
    assert int(current["generation"]) == int(stolen["generation"])
    assert current["state"] == "DISPATCHING", "the successor's drive is untouched"
    _close_spawned(spawned)


async def test_superseded_outbox_row_never_delivers_stale_text() -> None:
    """A row bound to a failed-then-retried attempt's command goes DEAD with
    evidence instead of delivering that attempt's stale output."""
    provider = _RecorderChannel()
    register_channel(provider)
    _service, ingress, kernel, _spawned = _spine()
    repo = ingress._channel_repo

    item, _ = await repo.claim_inbound(
        deployment_id="wh-1",
        dedup_key="sup-1",
        channel_name="recorder",
        agent_id="agent-1",
        payload=dict(_PAYLOAD),
    )
    item_id = str(item["_id"])
    await repo.begin_inbound_dispatch(
        item_id=item_id, owner_token=str(item["owner_token"]), generation=1
    )
    # The winning attempt settled under command Y…
    await repo.bind_inbound_dispatch(
        item_id=item_id, owner_token=str(item["owner_token"]), generation=1,
        session_id="sess-1", command_id="cmd-Y", turn_id="turn-Y",
    )
    await repo.settle_inbound(
        item_id=item_id, owner_token=str(item["owner_token"]), generation=1,
        session_id="sess-1",
    )
    # …while a row from the SUPERSEDED attempt (command X) is still pending.
    kernel.turn_texts[("sess-1", "turn-X")] = "stale text from the failed attempt"
    stale_row = await repo.create_outbox_entry(
        work_item_id=item_id,
        deployment_id="wh-1",
        channel_name="recorder",
        session_id="sess-1",
        command_id="cmd-X",
        turn_id="turn-X",
        reply_context={"cb": "x"},
    )
    await ingress._deliver_outbox_row(stale_row)
    assert provider.delivered == []
    row = await repo.get_outbox_entry(str(stale_row["_id"]))
    assert row["state"] == OUTBOX_DEAD
    assert "superseded" in row["last_error"]


def test_lease_floor_exceeds_the_drive_paths_bounded_waits() -> None:
    """The TTL floor must sit above the lock wait plus the session-ready wait,
    or a legitimately-waiting worker gets stolen from."""
    from astrabox.core.service.orchestrator.channel_ingress_service import (
        _SESSION_READY_TIMEOUT_SECONDS,
    )
    from astrabox.persistence.repository.channel_repository import (
        _INBOUND_LEASE_FLOOR_SECONDS,
    )

    assert _INBOUND_LEASE_FLOOR_SECONDS > 2 * _SESSION_READY_TIMEOUT_SECONDS


# ── post-delivery alias enrichment ───────────────────────────────────────────


async def test_alias_enrichment_attaches_the_late_id_to_the_chain() -> None:
    """Deliver (aliases persisted) → enrich with the late-materialized
    quotable id → an inbound referencing it resumes the original session.
    Idempotent under redelivery."""
    from astrabox.seams.channel import ChannelAliasLink

    class _ReceiptChannel(_RecorderChannel):
        name = "enricher"

        async def deliver_outbound(self, *, reply_context, text, binding):
            _ = binding
            self.delivered.append((reply_context, text))
            return ChannelDeliveryReceipt(message_ids=["carrier-1"])

    provider = _ReceiptChannel()
    register_channel(provider)
    service, ingress, _kernel, spawned = _spine(
        binding_extra={"scene": "channel:enricher"}
    )
    provider.inbound = ChannelInbound(
        content="a", dedup_key="e-1", conversation_key="room-9",
        reply_context={"cb": "x"},
    )
    first = await service.trigger("wh-1", headers={}, raw_body=b"{}")
    await _drain_spawned(spawned)

    # The robot's own echo arrives: carrier-1 ↔ open-msg-77.
    receipt = await ingress.ingest(
        "wh-1",
        ChannelInbound(
            content="",
            alias_link=ChannelAliasLink(
                existing_alias="carrier-1", new_alias="open-msg-77"
            ),
        ),
    )
    assert receipt.status == "enriched"
    # Redelivery of the echo converges on the same document.
    again = await ingress.ingest(
        "wh-1",
        ChannelInbound(
            content="",
            alias_link=ChannelAliasLink(
                existing_alias="carrier-1", new_alias="open-msg-77"
            ),
        ),
    )
    assert again.status == "enriched"

    # A quote of the card carries ONLY the late id — and lands in the chain.
    provider.inbound = ChannelInbound(
        content="b", dedup_key="e-2", reference="open-msg-77"
    )
    second = await service.trigger("wh-1", headers={}, raw_body=b"{}")
    assert second["session_id"] == first["session_id"]
    _close_spawned(spawned)


async def test_alias_enrichment_never_creates_a_chain() -> None:
    """An enrichment whose anchor was never delivered reports unmatched and
    persists nothing resolvable."""
    from astrabox.seams.channel import ChannelAliasLink

    provider = _RecorderChannel()
    register_channel(provider)
    _service, ingress, _kernel, spawned = _spine()
    import astrabox.core.service.orchestrator.channel_ingress_service as mod
    original = (mod._REFERENCE_RESOLVE_ATTEMPTS, mod._REFERENCE_RESOLVE_WAIT_SECONDS)
    mod._REFERENCE_RESOLVE_ATTEMPTS, mod._REFERENCE_RESOLVE_WAIT_SECONDS = 1, 0.0
    try:
        receipt = await ingress.ingest(
            "wh-1",
            ChannelInbound(
                content="",
                alias_link=ChannelAliasLink(
                    existing_alias="never-delivered", new_alias="open-msg-1"
                ),
            ),
        )
    finally:
        mod._REFERENCE_RESOLVE_ATTEMPTS, mod._REFERENCE_RESOLVE_WAIT_SECONDS = original
    assert receipt.status == "enrich_unmatched"
    assert await ingress._channel_repo.resolve_alias(
        deployment_id="wh-1", alias="open-msg-1"
    ) is None
    _close_spawned(spawned)


async def test_source_host_nacks_unmatched_enrichment_for_redelivery() -> None:
    from astrabox.core.service.orchestrator.channel_source_host import (
        ChannelSourceHost,
    )
    from astrabox.seams.channel import ChannelAliasLink

    provider = _RecorderChannel()
    register_channel(provider)
    _service, ingress, _kernel, spawned = _spine()
    import astrabox.core.service.orchestrator.channel_ingress_service as mod
    original = (mod._REFERENCE_RESOLVE_ATTEMPTS, mod._REFERENCE_RESOLVE_WAIT_SECONDS)
    mod._REFERENCE_RESOLVE_ATTEMPTS, mod._REFERENCE_RESOLVE_WAIT_SECONDS = 1, 0.0
    try:
        envelope = _OneShotEnvelope(
            "wh-1",
            ChannelInbound(
                content="",
                alias_link=ChannelAliasLink(
                    existing_alias="not-yet-durable", new_alias="open-msg-2"
                ),
            ),
            ingress._channel_repo,
        )
        host = ChannelSourceHost(
            ingress_service=ingress,
            deployment_repo=AsyncMock(),
            spawn_background_task=lambda coro, name="": coro,
        )
        await host._handle_envelope("recorder", envelope)
    finally:
        mod._REFERENCE_RESOLVE_ATTEMPTS, mod._REFERENCE_RESOLVE_WAIT_SECONDS = original
    assert envelope.acked is None
    assert envelope.nacked is not None, "unmatched enrichment must be redelivered"
    _close_spawned(spawned)
