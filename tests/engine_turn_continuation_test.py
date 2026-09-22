"""The answer continuation re-enters the parked engine stream, never a new turn.

The park design: the original bridge segment exits at the interaction
boundary (the SSE segment must close), the answer goes over the live
side-channel (``submit_interaction_response``), and the AnswerInteraction worker's
segment calls ``iter_engine_client_events(answer_continuation=True)`` to
consume the SAME engine stream to its terminal. These tests pin the two
properties that make that safe:

* the continuation performs NO sandbox turn preparation and NO new FIFO
  delivery — the input was delivered by the original segment, and a
  second delivery would start a second engine turn;
* it consumes ``iter_turn_events`` on the engine client's OWN
  ``active_receipt`` when there is one — the receipt object the original
  segment left on the client;
* and it still re-enters the stream when there is NOT one. A client that did
  not start the turn holds no receipt, which is every client after a platform
  restart; the receipt is pure data and the stream lives on the link, so
  "this process forgot" is not "the stream is gone".
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from astrabox.core.service.orchestrator.engine.emissions import (
    emission_from_translated_frame,
)
from astrabox.core.service.orchestrator.engine_turn import (
    iter_engine_client_events,
)


class _ContinuationEngineClient:
    def __init__(self, receipt: Any | None) -> None:
        self._receipt = receipt
        self.iterated_receipts: list[Any] = []

    @property
    def active_receipt(self) -> Any | None:
        return self._receipt

    @property
    def engine_session_key(self) -> str | None:
        return "sdk-session"

    async def iter_turn_events(self, receipt: Any):
        self.iterated_receipts.append(receipt)
        yield emission_from_translated_frame(
            {"type": "result", "finishReason": "stop"}
        )


class _Repo:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    async def update_session(self, session_id: str, updates: dict[str, Any]) -> None:
        self.updates.append(dict(updates))


def _as_async(sink: Any) -> Any:
    """A list-append sink as the async callback the seam expects."""
    if sink is None:
        return None

    async def _call(payload: dict[str, Any]) -> None:
        sink(payload)

    return _call


async def _drive(
    engine_client: _ContinuationEngineClient,
    *,
    on_query_committed: Any = None,
    parked_engine_anchor: Any = None,
) -> list[dict[str, Any]]:
    runtime = SimpleNamespace(
        lock=asyncio.Lock(),
        current_task=None,
        engine_client=engine_client,
        engine_kind="claude_code",
        sandbox_id="box-1",
        sandbox=None,
        interrupting=False,
        conversation_bound=True,
    )
    return [
        event
        async for event in iter_engine_client_events(
            session={
                "session_id": "s-1",
                "user_id": "owner",
                "sandbox_id": "box-1",
                "sandbox_backend": "open_sandbox",
            },
            session_id="s-1",
            effective_content="",
            turn_id="t-1",
            runtime=runtime,
            interaction_permission_mode=None,
            on_query_committed=_as_async(on_query_committed),
            emit_timing=lambda *args, **kwargs: None,
            client_message_id=None,
            sessions_repo=_Repo(),  # type: ignore[arg-type]
            broker=object(),  # type: ignore[arg-type]
            answer_continuation=True,
            parked_engine_anchor=parked_engine_anchor,
        )
    ]


async def test_continuation_resumes_the_active_receipt_without_new_input() -> None:
    receipt = SimpleNamespace(
        engine_turn_id="engine-turn-1",
        engine_session_key="",
        input_id=None,
        input_consumed=True,
    )
    engine_client = _ContinuationEngineClient(receipt)

    events = await _drive(engine_client)

    assert engine_client.iterated_receipts == [receipt]
    assert events[0]["type"] == "ack"
    assert events[0]["engine_turn_id"] == "engine-turn-1"
    result_events = [e for e in events if e.get("type") == "result"]
    assert result_events, f"continuation did not reach the stream terminal: {events}"


#: What the original segment persisted on the snapshot before it parked.
_PARKED_ANCHOR = {"engine_kind": "claude_code", "engine_turn_id": "engine-turn-1"}


async def test_continuation_resumes_on_a_client_that_never_started_the_turn() -> None:
    # Replaces "refuses loudly when the runtime holds no receipt". That refusal
    # read a MISSING IN-MEMORY FIELD as a missing stream: after a platform
    # restart the reattached client has no `active_receipt` while the box is
    # still running the turn and still sending frames. The old behaviour left
    # the answered turn parked in WAITING_INPUT until the spec's 180s budget
    # ran out — and it announced READY while doing it, so nothing looked
    # wrong.
    #
    # Whether a stream exists is the RUNTIME's question and is answered before
    # this path runs (ensure_runtime); a link that is really gone fails from
    # the link. So the continuation rebuilds the receipt and consumes.
    engine_client = _ContinuationEngineClient(receipt=None)

    events = await _drive(engine_client, parked_engine_anchor=_PARKED_ANCHOR)

    assert len(engine_client.iterated_receipts) == 1, "it must consume the stream"
    rebuilt = engine_client.iterated_receipts[0]
    assert rebuilt is not None
    assert rebuilt.engine_turn_id, "the rebuilt receipt still names its turn"
    assert rebuilt.input_consumed is True, (
        "a parked interaction proves the original SDK input boundary was crossed; "
        "without that fact the reattached Claude adapter discards every later "
        "event frame while still surfacing interaction frames"
    )
    result_events = [e for e in events if e.get("type") == "result"]
    assert result_events, f"continuation did not reach the stream terminal: {events}"


async def test_a_rebuilt_receipt_carries_the_durable_engine_turn_id() -> None:
    # Rebuilt from the anchor the original segment persisted, so the reattached
    # client is indistinguishable from the one that started the turn. Three
    # things key off this id, and an invented one fails all three silently: the
    # anchor conflict check (RuntimeError, kills the continuation),
    # `iter_turn_events` (drops a ResultMessage whose command stamp does not
    # match — the turn's own terminal, discarded, after which the bridge waits
    # out its quiet interval), and the ack the bridge observes. All measured.
    committed: list[dict[str, Any]] = []
    engine_client = _ContinuationEngineClient(receipt=None)

    events = await _drive(
        engine_client,
        on_query_committed=committed.append,
        parked_engine_anchor=_PARKED_ANCHOR,
    )

    assert engine_client.iterated_receipts[0].engine_turn_id == "engine-turn-1"
    # The callback carries the answer projection — the write that settles the
    # answered interaction. Suppressing the call to hide a bad anchor left the
    # gate active and the session in WAITING_INPUT; with a real id there is
    # nothing to hide.
    assert len(committed) == 1
    assert committed[0]["engine_turn_id"] == "engine-turn-1"
    acks = [e for e in events if e.get("type") == "ack"]
    assert len(acks) == 1 and acks[0]["engine_turn_id"] == "engine-turn-1"


async def test_a_continuation_without_a_durable_anchor_refuses() -> None:
    # No anchor is the one case where there really is nothing to resume from:
    # inventing an id would consume the stream under a name nothing else agrees
    # with, which is worse than saying so.
    engine_client = _ContinuationEngineClient(receipt=None)

    events = await _drive(engine_client)

    assert engine_client.iterated_receipts == []
    assert events[0]["type"] == "error"
    assert "durable engine anchor" in str(events[0].get("message") or "")


async def test_the_original_receipt_still_reports_its_anchor() -> None:
    # The unchanged path: when this process DID start the turn, the receipt is
    # the real one and its anchor is a fact worth committing.
    committed: list[dict[str, Any]] = []
    receipt = SimpleNamespace(
        engine_turn_id="engine-turn-1",
        engine_session_key="",
        input_id=None,
        input_consumed=True,
    )
    engine_client = _ContinuationEngineClient(receipt=receipt)

    events = await _drive(engine_client, on_query_committed=committed.append)

    assert len(committed) == 1
    assert committed[0]["engine_turn_id"] == "engine-turn-1"
    acks = [e for e in events if e.get("type") == "ack"]
    assert acks and acks[0]["engine_turn_id"] == "engine-turn-1"
