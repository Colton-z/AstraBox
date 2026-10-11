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
import json
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
    INBOUND_AUTHORIZED,
    INBOUND_DEAD,
    INBOUND_IGNORED,
    INBOUND_OBSERVED,
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
    revived, outcome = await _claim(repo)
    assert outcome == "claimed"
    assert revived["state"] == INBOUND_RECEIVED
    assert int(revived["generation"]) == 2
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
        "deployment_id": "wh",
        "channel_name": "recorder",
        "session_id": "s1",
        "turn_id": "turn-1",
        "response_seq": 1,
        "response_id": overrides.get("turn_id", "turn-1"),
        "start_after_seq": 0,
        "reply_context": {"callback_url": "http://x"},
    }
    fields.update(overrides)
    return await repo.create_outbox_entry(**fields)


async def test_outbox_creation_is_idempotent_per_destination_session_and_response() -> None:
    """Invariant C: the row is born bound to its exact turn and response, and a
    re-driven settle re-creates the SAME row (no double delivery intent)."""
    repo = ChannelRepository()
    first = await _outbox(repo)
    again = await _outbox(repo)
    assert first["_id"] == again["_id"]
    assert again["turn_id"] == "turn-1"
    other_turn = await _outbox(repo, turn_id="turn-2")
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
        self.superseded: list[tuple[str, str]] = []
        self.timeline: list[tuple[str, str]] = []
        self.stream_calls = 0
        self.fail_next_stream = False
        self.append_before_failing = False

    async def session_output_cursor(self, session_id):
        return len([key for key in self.turn_texts if key[0] == session_id])

    async def read_session_output(self, session_id, *, after_seq):
        from astrabox.core.service.orchestrator.session_kernel.service_mixins.session_output import (
            SessionOutputBatch, SessionOutputResponse,
        )
        messages = [(turn, text) for (session, turn), text in self.turn_texts.items() if session == session_id]
        return SessionOutputBatch(
            responses=[SessionOutputResponse(
                response_id=turn, turn_id=turn, start_after_seq=index,
                response_seq=index + 1, text=text, complete=True,
            ) for index, (turn, text) in enumerate(messages) if index + 1 > after_seq],
            after_seq=len(messages),
        )

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

    async def answer_pending_interaction(
        self,
        user: Any,
        session_id: str,
        interaction_id: str,
        answer: dict[str, Any],
    ) -> dict[str, Any]:
        self.interaction_answers.append((session_id, interaction_id, dict(answer)))
        self.timeline.append(("answer", interaction_id))
        self.session_details[session_id] = {
            "state": "BUSY",
            "current_turn_id": "question-turn",
            "pending_interaction": None,
        }
        return {"interaction_id": interaction_id, "answered": True}

    async def supersede_pending_interaction(
        self,
        user: Any,
        session_id: str,
        interaction_id: str,
    ) -> dict[str, Any]:
        self.superseded.append((session_id, interaction_id))
        self.timeline.append(("supersede", interaction_id))
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


class _PendingTasks(list):
    ingress: ChannelIngressService


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
    owners: dict[str, str] = {}

    async def _start(user, agent_id, *, source_type="agent"):
        counter["n"] += 1
        session_id = f"sess-{counter['n']}"
        owners[session_id] = user.user_id
        return {"session_id": session_id}

    agent_service.start_conversation.side_effect = _start
    agent_service.start_integration_conversation.side_effect = _start
    sessions_repo = AsyncMock()

    async def _get_session(session_id):
        return {
            "state": (session_states or {}).get(session_id, "READY"),
            "user_id": owners.get(session_id, "creator"),
        }

    sessions_repo.get_session.side_effect = _get_session
    kernel = _FakeKernel()
    spawned = _PendingTasks()
    ingress = ChannelIngressService(
        deployment_repo=deployment_repo,
        agent_repo=agent_repo,
        agent_service_getter=lambda: agent_service,
        stream_message_events_ds=kernel.stream,
        sessions_repo=sessions_repo,
        session_events_repo=kernel,
        resume_command_stream=kernel.resume_command_stream,
        read_session_output=kernel.read_session_output,
        session_output_cursor=kernel.session_output_cursor,
        message_view=kernel,
        session_detail_getter=kernel.get_session,
        answer_pending_interaction=kernel.answer_pending_interaction,
        supersede_pending_interaction=kernel.supersede_pending_interaction,
        spawn_background_task=lambda coro, name="": spawned.append(coro),
    )
    spawned.ingress = ingress
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


async def _drain_spawned(spawned: _PendingTasks) -> None:
    while spawned:
        await asyncio.wait_for(spawned.pop(0), timeout=10)
    await spawned.ingress.project_session_outputs()
    await spawned.ingress.sweep_pending_outbox()
    while spawned:
        await asyncio.wait_for(spawned.pop(0), timeout=10)


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
    _command_id, turn_id = kernel.record_command("sess-9", "tok")
    kernel.turn_texts[("sess-9", turn_id)] = "first turn's reply"
    _newer_command, newer_turn = kernel.record_command("sess-9", "tok2")
    kernel.turn_texts[("sess-9", newer_turn)] = "SECOND turn's reply"
    entry = await repo.create_outbox_entry(
        deployment_id="wh-1",
        channel_name="recorder",
        session_id="sess-9",
        turn_id=turn_id, response_id=turn_id, start_after_seq=0,
        response_seq=1,
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
    await _drain_spawned(spawned)
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
    await repo.subscribe_to_output(
        session_id="sess-1", deployment_id="wh-1", channel_name="recorder",
        reply_context={"cb": "x"}, conversation_key=None, after_seq=0, streaming=False,
    )
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
    await _drain_spawned(spawned)
    final = await repo.get_inbound(item_id)
    assert final["state"] == INBOUND_SETTLED
    assert final["turn_id"] == turn_id, "bound to the ORIGINAL dispatch"
    assert kernel.stream_calls == 0, "no second turn was dispatched"
    assert kernel.superseded == [], (
        "attaching an older work item must not stop a newer interaction's turn"
    )
    assert provider.delivered == [({"cb": "x"}, "the reply")]


async def test_new_channel_message_stops_open_question_before_its_turn() -> None:
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

    assert kernel.superseded == [("sess-1", "question-1")]
    assert kernel.timeline == [
        ("supersede", "question-1"),
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


async def test_attention_policy_keeps_unaddressed_messages_as_context_and_admits_addressed_ones() -> None:
    provider = _RecorderChannel()
    provider.inbound = ChannelInbound(content="hi", dedup_key="msg-10")
    register_channel(provider)
    service, ingress, kernel, spawned = _spine(
        binding_extra={"attention_policy": "mentions"}
    )
    observed = await service.trigger("wh-1", headers={}, raw_body=b"{}")
    assert observed["status"] == "observed"
    assert spawned == [], "no dispatch for an unaddressed message"
    item_id = _item_id("wh-1", "msg-10")
    repo = ingress._channel_repo
    record = await repo.get_inbound(item_id)
    # The classification is written with the record: no owner and no lease,
    # so a process lost right after the write leaves nothing for recovery to
    # drive as a turn.
    assert record["state"] == INBOUND_OBSERVED and record["owner_token"] is None
    assert await repo.list_recoverable_inbound(now=time.time() + 365 * 86400) == []
    assert await ingress.recover_inbound() == 0
    assert kernel.stream_calls == 0

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
        _scoped_id("outbox", "wh-1", json.dumps([item["session_id"], item["turn_id"]]))
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
    await _drain_spawned(spawned)
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

    async def _start(user, agent_id, *, source_type="agent"):
        counter["n"] += 1
        return {"session_id": f"sess-{counter['n']}"}

    agent_service.start_conversation.side_effect = _start
    sessions_repo = AsyncMock()

    async def _get_session(session_id):
        return {"state": (session_states or {}).get(session_id, "READY"), "user_id": "creator"}

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

    spawned = _PendingTasks()
    ingress = ChannelIngressService(
        deployment_repo=deployment_repo,
        agent_repo=agent_repo,
        agent_service_getter=lambda: agent_service,
        stream_message_events_ds=_stream_with_frames,
        sessions_repo=sessions_repo,
        session_events_repo=kernel,
        resume_command_stream=kernel.resume_command_stream,
        read_session_output=kernel.read_session_output,
        session_output_cursor=kernel.session_output_cursor,
        message_view=kernel,
        session_detail_getter=kernel.get_session,
        answer_pending_interaction=kernel.answer_pending_interaction,
        supersede_pending_interaction=kernel.supersede_pending_interaction,
        spawn_background_task=lambda coro, name="": spawned.append(coro),
    )
    spawned.ingress = ingress
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


async def test_streaming_events_name_the_reply_by_its_response_and_turn() -> None:
    """A streaming provider tells one reply from another by the response it
    renders and the Session turn that produced it, which every event of a
    delivery carries at projection version 2. The projection names no
    command: a reply the engine produces on its own has none."""
    import dataclasses

    provider = _StreamingChannel()
    provider.inbound = ChannelInbound(
        content="hi", dedup_key="s-2", reply_context={"chat": "c1"}
    )
    register_channel(provider)
    service, ingress, _kernel, _frames, spawned = _streaming_spine()
    assert (await service.trigger("wh-1", headers={}, raw_body=b"{}"))["status"] == "accepted"
    await _drain_spawned(spawned)

    (handle,) = provider.handles
    item = await ingress._channel_repo.get_inbound(_item_id("wh-1", "s-2"))
    response = await ingress._channel_repo.get_outbox_entry(
        _scoped_id("outbox", "wh-1", json.dumps([item["session_id"], item["turn_id"]]))
    )
    assert {(event.version, event.response_id, event.turn_id) for event in handle.events} == {
        (2, response["response_id"], item["turn_id"]),
    }
    assert "command_id" not in {field.name for field in dataclasses.fields(ChannelEvent)}
    assert "command_id" not in response


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
        _scoped_id("outbox", "wh-1", json.dumps([item["session_id"], item["turn_id"]]))
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
    outbox_id = _scoped_id("outbox", "wh-1", json.dumps([item["session_id"], item["turn_id"]]))

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
    await _drain_spawned(spawned)
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
    _command_id, turn_id = kernel.record_command("sess-1", f"{item_id}::a0")
    kernel.turn_texts[("sess-1", turn_id)] = "the reply"
    await repo.create_outbox_entry(
        deployment_id="wh-1",
        channel_name="recorder",
        session_id="sess-1",
        turn_id=turn_id, response_id=turn_id, start_after_seq=0,
        response_seq=1,
        reply_context={"cb": "x"},
    )
    await repo.renew_inbound_lease(
        item_id=item_id, owner_token=str(item["owner_token"]), generation=1,
        now=time.time() - 100_000,
    )

    assert await ingress.recover_inbound() == 1
    await _drain_spawned(spawned)
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


async def test_durable_mainline_reply_is_not_filtered_by_input_attempt() -> None:
    """A committed mainline reply survives an input attempt being superseded.

    The Session subscription decides visibility; the input work item's current
    command cannot remove a reply that remains in the conversation.
    """
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
    kernel.turn_texts[("sess-1", "turn-X")] = "committed mainline reply"
    stale_row = await repo.create_outbox_entry(
        deployment_id="wh-1",
        channel_name="recorder",
        session_id="sess-1",
        turn_id="turn-X", response_id="turn-X", start_after_seq=0,
        response_seq=1,
        reply_context={"cb": "x"},
    )
    await ingress._deliver_outbox_row(stale_row)
    assert provider.delivered == [({"cb": "x"}, "committed mainline reply")]
    row = await repo.get_outbox_entry(str(stale_row["_id"]))
    assert row["state"] == OUTBOX_DELIVERED
    assert row["last_error"] is None


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


async def test_execution_accounts_keep_distinct_conversations_and_restore_their_own_history() -> None:
    provider = _RecorderChannel()
    register_channel(provider)
    service, ingress, _kernel, spawned = _spine()
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
        item = await ingress._channel_repo.get_inbound(_item_id("wh-1", f"identity-{number}"))
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
    service, ingress, _kernel, spawned = _spine(binding_extra={"execution_user_id": "alice"})
    provider.inbound = ChannelInbound(content="work", dedup_key="frozen-owner", conversation_key="room")
    receipt = await service.trigger("wh-1", headers={}, raw_body=b"{}")
    _close_spawned(spawned)
    service._deployment_repo.get_by_id.return_value["execution_user_id"] = "bob"
    repo = ingress._channel_repo
    item = await repo.get_inbound(_item_id("wh-1", "frozen-owner"))
    await repo.renew_inbound_lease(
        item_id=str(item["_id"]), owner_token=str(item["owner_token"]),
        generation=1, now=time.time() - 100_000,
    )
    assert await ingress.recover_inbound() == 1
    await _drain_spawned(spawned)
    recovered = await repo.get_inbound(str(item["_id"]))
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
            agent_id="agent", execution_user_id=account,
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
    from astrabox.seams.channel import ChannelDeliveryReceipt

    class ReceiptChannel(_RecorderChannel):
        async def deliver_outbound(self, *, reply_context, text, binding):
            self.delivered.append((reply_context, text))
            return ChannelDeliveryReceipt(message_ids=["private-reply"])

    provider = ReceiptChannel()
    register_channel(provider)
    service, ingress, kernel, spawned = _spine(binding_extra={"execution_user_id": "alice"})
    provider.inbound = ChannelInbound(
        content="private turn", dedup_key="private", conversation_key=conversation_key,
        reply_context={"cb": "x"},
    )
    await service.trigger("wh-1", headers={}, raw_body=b"{}")
    await _drain_spawned(spawned)
    return service, ingress, kernel, spawned, provider


@pytest.mark.parametrize("conversation_key", [None, "room"])
@pytest.mark.parametrize("reply_conversation_key", [None, "room"])
async def test_reply_reference_cannot_cross_execution_accounts(conversation_key, reply_conversation_key) -> None:
    service, ingress, kernel, spawned, provider = await _delivered_reference_spine(conversation_key)
    calls = kernel.stream_calls
    service._deployment_repo.get_by_id.return_value["execution_user_id"] = "bob"
    provider.inbound = ChannelInbound(
        content="continue", dedup_key="cross", reference="private-reply",
        conversation_key=reply_conversation_key,
    )
    receipt = await service.trigger("wh-1", headers={}, raw_body=b"{}")
    assert receipt["status"] == "ignored"
    assert receipt["reason"] == "CHANNEL_REFERENCE_OWNER_MISMATCH"
    assert kernel.stream_calls == calls
    assert not spawned
    rejected = await ingress._channel_repo.get_inbound(_item_id("wh-1", "cross"))
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

    service, ingress, kernel, spawned, provider = await _delivered_reference_spine("room")
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
    calls = kernel.stream_calls
    assert await host._handle_envelope(provider.name, envelope) is recorded
    assert kernel.stream_calls == calls
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


async def test_same_account_reference_keeps_the_explicit_channel_conversation() -> None:
    service, ingress, _kernel, spawned, provider = await _delivered_reference_spine("room-a")
    original = await ingress._channel_repo.get_inbound(_item_id("wh-1", "private"))
    provider.inbound = ChannelInbound(
        content="continue", dedup_key="same-account", reference="private-reply", conversation_key="room-b",
    )
    receipt = await service.trigger("wh-1", headers={}, raw_body=b"{}")
    assert receipt["status"] == "accepted"
    assert receipt["session_id"] != original["session_id"]
    await _drain_spawned(spawned)


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
            item_id=item["_id"], owner_token=item["owner_token"], generation=item["generation"],
            reason=reason, exclude_from_context=excluded,
        )
    current, _ = await _claim(repo, dedup_key="current", payload=payload)
    context = await repo.collect_unsubmitted_context(current)
    assert [entry["content"] for entry in context] == ["private", "ordinary context"]
    assert ordinary["_id"] in {entry["item_id"] for entry in context}


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


async def test_authorization_whose_observation_never_arrives_dies_and_says_so() -> None:
    """A platform's trigger names a message whose content travels on a
    separate feed. If that content never arrives, the authorization must not
    wait forever or run anything: after its wait it dies with evidence, the
    participant who invoked the agent is told, and an observation that turns
    up afterwards runs nothing."""
    provider = _RecorderChannel()
    register_channel(provider)
    service, ingress, kernel, spawned = _spine(binding_extra={"updated_at": "rev-1"})
    provider.inbound = _authorization("late-1")
    receipt = await service.trigger("wh-1", headers={}, raw_body=b"{}")
    assert receipt["status"] == "authorized"
    assert spawned == []
    repo = ingress._channel_repo
    item_id = _item_id("wh-1", "late-1")
    assert (await repo.get_inbound(item_id))["state"] == INBOUND_AUTHORIZED
    assert await ingress.expire_authorizations() == 0, "still within its wait"

    after_wait = time.time() + 24 * 3600 + 1
    assert await ingress.expire_authorizations(now=after_wait) == 1
    dead = await repo.get_inbound(item_id)
    assert dead["state"] == INBOUND_DEAD and dead["last_error"]
    assert provider.delivered == [({"cb": "x"}, ingress_module._AUTHORIZATION_EXPIRED_NOTICE)]
    assert await ingress.expire_authorizations(now=after_wait) == 0, "expired once"

    provider.inbound = _observation("late-1")
    late = await service.trigger("wh-1", headers={}, raw_body=b"{}")
    assert late["status"] == "observed"
    assert spawned == [] and kernel.stream_calls == 0


async def test_authorization_naming_another_sender_is_refused_and_says_so() -> None:
    """The observation recorded who wrote the message. An authorization for
    that message naming a different sender is not that message's trigger: it
    must not run the observed text as someone else's request. The record
    leaves retained context and the authorizing sender is told."""
    provider = _RecorderChannel()
    register_channel(provider)
    service, ingress, kernel, spawned = _spine(binding_extra={"updated_at": "rev-1"})
    provider.inbound = _observation("m-x", participant="u-1")
    assert (await service.trigger("wh-1", headers={}, raw_body=b"{}"))["status"] == "observed"
    provider.inbound = _authorization("m-x", participant="u-2")
    receipt = await service.trigger("wh-1", headers={}, raw_body=b"{}")
    assert receipt["status"] == "ignored"
    assert receipt["reason"] == "CHANNEL_SIGNAL_IDENTITY_CONFLICT"
    record = await ingress._channel_repo.get_inbound(_item_id("wh-1", "m-x"))
    assert record["state"] == INBOUND_IGNORED and record["context_excluded"] is True
    assert provider.delivered == [({"cb": "x"}, ingress_module._IDENTITY_CONFLICT_NOTICE)]
    assert spawned == [] and kernel.stream_calls == 0


async def test_concurrent_admissions_of_one_observed_message_run_it_once() -> None:
    """A context feed recorded the message; its trigger then arrives several
    times at once, a redelivery racing the original. Exactly one admission
    wins, so the observed message gets one turn and one accepted receipt."""
    provider = _RecorderChannel()
    register_channel(provider)
    service, ingress, kernel, spawned = _spine(binding_extra={"updated_at": "rev-1"})
    provider.inbound = _observation("m-race")
    await service.trigger("wh-1", headers={}, raw_body=b"{}")
    provider.inbound = _authorization("m-race")
    receipts = await asyncio.gather(*(
        service.trigger("wh-1", headers={}, raw_body=b"{}") for _ in range(4)
    ))
    statuses = sorted(receipt["status"] for receipt in receipts)
    assert statuses == ["accepted", "in_progress", "in_progress", "in_progress"]
    await _drain_spawned(spawned)
    assert kernel.stream_calls == 1
    assert ("turn", "the message") in kernel.timeline, "the observed text is the input"
    record = await ingress._channel_repo.get_inbound(_item_id("wh-1", "m-race"))
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
    assert stolen["payload"] == original
    item_id, owner = str(stolen["_id"]), str(stolen["owner_token"])
    generation = int(stolen["generation"])
    assert await repo.begin_inbound_dispatch(item_id=item_id, owner_token=owner, generation=generation)
    assert await repo.fail_inbound(
        item_id=item_id, owner_token=owner, generation=generation,
        error="final", retry_at_epoch=None,
    )
    revived, outcome = await _claim(repo, dedup_key="m-keep", payload=changed)
    assert outcome == "claimed" and revived["state"] == INBOUND_RECEIVED
    assert revived["payload"] == original


async def test_retained_context_quotes_participants_without_the_prompt_prefix() -> None:
    """The binding's prompt prefix frames the turn's own input once. Context
    rows quote what each participant wrote: repeating the prefix inside every
    quoted message would attribute the operator's instruction to them."""
    provider = _RecorderChannel()
    register_channel(provider)
    service, ingress, _kernel, spawned = _spine(binding_extra={
        "updated_at": "rev-1", "prompt_prefix": "Answer briefly.",
        "attention_policy": "mentions",
    })
    retained = {"conversation_key": "room", "participant": "u-1", "retain_context": True}
    provider.inbound = ChannelInbound(
        content="earlier words", dedup_key="m-a",
        message_timestamp="2026-10-01T00:00:00Z", **retained,
    )
    assert (await service.trigger("wh-1", headers={}, raw_body=b"{}"))["status"] == "observed"
    provider.inbound = ChannelInbound(
        content="the question", dedup_key="m-b", attention=_MENTION,
        message_timestamp="2026-10-01T00:00:01Z", **retained,
    )
    assert (await service.trigger("wh-1", headers={}, raw_body=b"{}"))["status"] == "accepted"
    await _drain_spawned(spawned)
    item = await ingress._channel_repo.get_inbound(_item_id("wh-1", "m-b"))
    assert [row["content"] for row in item["frozen_input"]["context"]] == ["earlier words"]
    turn_input = item["frozen_input"]["content"]
    assert turn_input.count("Answer briefly.") == 1
    assert turn_input.endswith("Answer briefly.\n\nthe question")


# ── conversations a provider groups by reply chain ──────────────────────────


class _ReplyTreeChannel(_RecorderChannel):
    """A provider that groups a room's messages by reply chain.

    It does what a downstream reply-tree provider would: a message that
    replies to nothing starts a conversation named by its own id, and a reply
    asks core to continue the conversation of the message it replies to. The
    provider never tracks a chain; when core recorded no such message, the
    reply starts a conversation rooted at the message it names.
    """

    name = "reply-tree"

    async def deliver_outbound(self, *, reply_context, text, binding):
        self.delivered.append((reply_context, text))
        return ChannelDeliveryReceipt(message_ids=[f"bot-{len(self.delivered)}"])

    def say(self, message_id: str, *, at: str, replies_to: str | None = None,
            mention: bool = False) -> None:
        self.inbound = ChannelInbound(
            content=f"text of {message_id}", dedup_key=message_id,
            conversation_key=f"room/tree/{replies_to or message_id}",
            reference=replies_to, conversation_from_reference=replies_to is not None,
            reply_context={"to": message_id}, participant="u-1", retain_context=True,
            message_timestamp=at, attention=_MENTION if mention else ChannelAttention(),
        )


async def test_a_provider_groups_a_rooms_messages_by_reply_chain(monkeypatch) -> None:
    """Core keeps which conversation every recorded message belongs to, so a
    provider can group messages by reply chain while naming only the message
    each one replies to: a reply to a reply, or to the agent's own answer,
    continues the conversation where the chain started, and reads that
    conversation's unaddressed replies as context."""
    import astrabox.core.service.orchestrator.channel_ingress_service as ingress_module

    monkeypatch.setattr(ingress_module, "_REFERENCE_RESOLVE_WAIT_SECONDS", 0.0)
    provider = _ReplyTreeChannel()
    register_channel(provider)
    service, ingress, kernel, spawned = _spine(binding_extra={
        "scene": "channel:reply-tree", "attention_policy": "mentions", "updated_at": "rev-1",
    })

    async def say(message_id: str, at: str, **kwargs: Any) -> dict[str, Any]:
        provider.say(message_id, at=at, **kwargs)
        return await service.trigger("wh-1", headers={}, raw_body=b"{}")

    first = await say("m1", "2026-10-01T00:00:01Z", mention=True)
    await _drain_spawned(spawned)
    assert (await say("m2", "2026-10-01T00:00:02Z", replies_to="m1"))["status"] == "observed"
    third = await say("m3", "2026-10-01T00:00:03Z", replies_to="m2", mention=True)
    assert third["session_id"] == first["session_id"]
    await _drain_spawned(spawned)
    item = await ingress._channel_repo.get_inbound(_item_id("wh-1", "m3"))
    assert [row["message_id"] for row in item["frozen_input"]["context"]] == ["m2"]
    assert "text of m2" in item["frozen_input"]["content"]
    to_agent = await say("m4", "2026-10-01T00:00:04Z", replies_to="bot-1", mention=True)
    assert to_agent["session_id"] == first["session_id"]
    await _drain_spawned(spawned)

    new_request = await say("m5", "2026-10-01T00:00:05Z", mention=True)
    unknown = await say("m6", "2026-10-01T00:00:06Z", replies_to="m0", mention=True)
    await _drain_spawned(spawned)
    sessions = {first["session_id"], new_request["session_id"], unknown["session_id"]}
    assert len(sessions) == 3
    conversation = await ingress._channel_repo.get_conversation(
        deployment_id="wh-1", conversation_key="room/tree/m0",
    )
    assert conversation is not None and conversation["session_id"] == unknown["session_id"]
    assert kernel.stream_calls == 5


async def test_a_recovered_message_takes_the_lock_of_the_conversation_it_resolved() -> None:
    """A message can reach its conversation through the reply it quotes rather
    than its own key. A worker that dies after binding the message's session
    leaves recovery a session that is already bound, so recovery must still
    serialise on that conversation: the resolved conversation is stored with
    the session."""
    provider = _ReplyTreeChannel()
    register_channel(provider)
    service, ingress, kernel, spawned = _spine(binding_extra={"scene": "channel:reply-tree"})
    provider.inbound = ChannelInbound(
        content="a question", dedup_key="r-1", conversation_key="room", reply_context={"to": "r-1"},
    )
    assert (await service.trigger("wh-1", headers={}, raw_body=b"{}"))["status"] == "accepted"
    await _drain_spawned(spawned)
    provider.inbound = ChannelInbound(
        content="a follow-up", dedup_key="r-2", reference="bot-1", reply_context={"to": "r-2"},
    )
    receipt = await service.trigger("wh-1", headers={}, raw_body=b"{}")
    assert receipt["status"] == "accepted"
    _close_spawned(spawned)  # the worker dies before driving
    repo = ingress._channel_repo
    item_id = _item_id("wh-1", "r-2")
    stored = await repo.get_inbound(item_id)
    assert stored["payload"]["conversation"] is None
    assert stored["resolved_conversation_key"] == "room"
    await repo.renew_inbound_lease(
        item_id=item_id, owner_token=str(stored["owner_token"]),
        generation=1, now=time.time() - 100_000,
    )
    assert await ingress.recover_inbound() == 1
    await _drain_spawned(spawned)
    assert (await repo.get_inbound(item_id))["state"] == INBOUND_SETTLED
    conversation = await repo.get_conversation(deployment_id="wh-1", conversation_key="room")
    assert conversation is not None and conversation["session_id"] == receipt["session_id"]
    assert conversation["lock_generation"] == 2 and conversation["lock_owner"] is None
    assert kernel.stream_calls == 2


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
    service, ingress, kernel, spawned = _spine(binding_extra={
        "execution_mode": "linked", "attention_policy": "mentions", "updated_at": "rev-1",
    })
    return resolver, provider, service, ingress, kernel, spawned


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
    resolver, provider, service, ingress, kernel, spawned = _linked_spine(monkeypatch)
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
    item_id = _item_id("wh-1", "m3")
    kept = await ingress._channel_repo.get_inbound(item_id)
    assert kept["payload"]["execution_user_id"] == "alice" and not kept["session_id"]
    resolver.links["im:p1"] = "carol"
    await _expire_and_recover(ingress, item_id)
    await _drain_spawned(spawned)
    recovered = await ingress._channel_repo.get_inbound(item_id)
    assert recovered["state"] == INBOUND_SETTLED
    assert (await sessions.get_session(recovered["session_id"]))["user_id"] == "alice"
    assert resolver.asked == ["im:p1", "im:p2", "im:p1"]
    assert kernel.stream_calls == 3


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
    resolver, provider, service, ingress, kernel, spawned = _linked_spine(monkeypatch)
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
    record = await ingress._channel_repo.get_inbound(_item_id("wh-1", "q-1"))
    assert record["state"] == INBOUND_IGNORED and record["ignored_reason"] == reason
    assert record["context_excluded"] is True and not record["session_id"]
    assert [destination for destination, _ in provider.delivered] == [{"to": "q-1"}]
    assert resolver.asked == ([] if sender is None else [sender])
    assert kernel.stream_calls == 0 and not spawned
    again = await service.trigger("wh-1", headers={}, raw_body=b"{}")
    assert again["status"] == "ignored" and len(provider.delivered) == 1


async def test_an_unavailable_directory_holds_a_linked_message_for_retry(monkeypatch) -> None:
    """When the account resolver cannot answer, the message is neither refused
    nor run as anyone: it stays to be driven again, and runs as its linked
    account once the directory answers."""
    resolver, provider, service, ingress, kernel, spawned = _linked_spine(monkeypatch)
    resolver.links = {"im:p1": "alice"}
    resolver.unavailable = True
    provider.inbound = ChannelInbound(
        content="question", dedup_key="q-1", conversation_key="room", participant="im:p1",
        reply_context={"to": "q-1"}, attention=_MENTION,
    )
    with pytest.raises(RuntimeError, match="directory unavailable"):
        await service.trigger("wh-1", headers={}, raw_body=b"{}")
    item_id = _item_id("wh-1", "q-1")
    held = await ingress._channel_repo.get_inbound(item_id)
    assert held["state"] == INBOUND_RECEIVED and not held["session_id"]
    assert held["payload"]["execution_user_id"] == "" and kernel.stream_calls == 0
    resolver.unavailable = False
    await _expire_and_recover(ingress, item_id)
    await _drain_spawned(spawned)
    settled = await ingress._channel_repo.get_inbound(item_id)
    assert settled["state"] == INBOUND_SETTLED
    assert (await ingress._sessions_repo.get_session(settled["session_id"]))["user_id"] == "alice"
