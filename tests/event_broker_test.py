"""SessionEventBroker fanout + backpressure — the SSE distribution seam.

Every live turn publishes its translated frames through ``SessionEventBroker``;
each connected client holds one subscribed ``asyncio.Queue``. These tests pin
the delivery contract the SSE endpoints stand on: fan-out to all subscribers of
a session, isolation between sessions, and the fact that the broker keeps NO
history (a late subscriber misses everything published before it subscribed).

Backpressure note: the queue is BOUNDED (``maxsize=1000``), so a stalled
consumer cannot drive unbounded memory growth. This is an ORDERED protocol
(e.g. a text-start frame must precede the text-delta frames for the same
block), so a naive drop-OLDEST ring buffer is unsafe: evicting the oldest
queued frame to make room for the newest can silently discard an opening
frame while a later, dependent frame survives — corruption, not truncation.
Instead, a subscriber whose queue is still full when the next event arrives
is DISCONNECTED: it receives one ``OVERFLOW_SENTINEL`` and is dropped from
the listener set, so it stops receiving live events entirely and the client
is expected to fall back to the durable resume path (``GET .../ai-stream``)
instead of consuming corrupted history. The last test pins that contract
precisely.

`asyncio_mode = "auto"` (see pyproject) runs these bare ``async def test_*``
coroutines directly — no decorator needed.
"""

from __future__ import annotations

from astrabox.core.service.orchestrator.event_broker import (
    OVERFLOW_SENTINEL,
    SessionEventBroker,
)


async def test_publish_fans_out_to_all_subscribers_and_isolates_sessions() -> None:
    broker = SessionEventBroker()
    a1 = await broker.subscribe("s")
    a2 = await broker.subscribe("s")
    other = await broker.subscribe("other")

    await broker.publish("s", {"n": 1})

    # Every subscriber of "s" gets the event...
    assert a1.get_nowait() == {"n": 1}
    assert a2.get_nowait() == {"n": 1}
    # ...and a subscriber of a different session gets nothing.
    assert other.empty()


async def test_late_subscriber_misses_events_published_before_it_subscribed() -> None:
    # The broker holds no backlog: whatever happened before you subscribe is
    # gone; you only see events from your subscription point forward.
    broker = SessionEventBroker()
    early = await broker.subscribe("s")
    await broker.publish("s", {"n": 1})

    late = await broker.subscribe("s")
    assert late.empty(), "late subscriber must not receive replayed history"

    await broker.publish("s", {"n": 2})
    assert early.get_nowait() == {"n": 1}
    assert early.get_nowait() == {"n": 2}
    assert late.get_nowait() == {"n": 2}  # only the post-subscribe event


async def test_unsubscribe_stops_delivery_without_affecting_other_subscribers() -> None:
    broker = SessionEventBroker()
    keep = await broker.subscribe("s")
    drop = await broker.subscribe("s")

    await broker.unsubscribe("s", drop)
    await broker.publish("s", {"n": 1})

    assert keep.get_nowait() == {"n": 1}  # survivor still served
    assert drop.empty(), "unsubscribed queue must receive nothing further"


async def test_publish_after_all_unsubscribed_and_to_unknown_session_is_noop() -> None:
    broker = SessionEventBroker()
    q = await broker.subscribe("s")
    await broker.unsubscribe("s", q)

    # Session is pruned once its last listener leaves (white-box: no leaked key).
    assert "s" not in broker._listeners

    # Publishing to a drained-then-empty session and to a never-seen session must
    # not raise — the SSE producer keeps running after every client disconnects.
    await broker.publish("s", {"n": 1})
    await broker.publish("never-existed", {"n": 2})


async def test_unsubscribe_is_idempotent_and_safe_for_unknown_queue() -> None:
    broker = SessionEventBroker()
    q = await broker.subscribe("s")

    await broker.unsubscribe("s", q)
    # Second unsubscribe of the same queue, and unsubscribe of an unknown session,
    # are both no-ops rather than errors.
    await broker.unsubscribe("s", q)
    await broker.unsubscribe("ghost", q)


async def test_overflowing_subscriber_is_disconnected_with_sentinel() -> None:
    # Fill a subscriber's queue to capacity (bounded at maxsize=1000) without
    # draining it, simulating a lagging client.
    broker = SessionEventBroker()
    lagging = await broker.subscribe("cap")
    for i in range(1000):
        await broker.publish("cap", {"i": i})
    assert lagging.qsize() == 1000

    # The next publish finds the queue full. Rather than silently evicting the
    # oldest frame and keeping this subscriber alive (which could corrupt an
    # ordered protocol), the broker disconnects it: one slot is freed for a
    # terminal OVERFLOW_SENTINEL, and the subscriber is dropped from the
    # listener set.
    await broker.publish("cap", {"i": 1000})

    assert lagging.qsize() == 1000, "queue stays bounded at maxsize"
    drained = [lagging.get_nowait() for _ in range(1000)]
    # The oldest frame (i=0) was evicted to make room for the sentinel, which
    # is the LAST item the disconnected subscriber ever sees; FIFO order among
    # the retained frames is preserved and the newest publish (i=1000) never
    # reached this subscriber at all.
    assert drained[:-1] == [{"i": i} for i in range(1, 1000)]
    assert drained[-1] == OVERFLOW_SENTINEL
    assert lagging.empty()

    # Disconnected means disconnected: further publishes to the same session
    # deliver nothing more to this queue...
    await broker.publish("cap", {"i": 1001})
    assert lagging.empty()

    # ...while the session itself is still very much alive: a fresh subscriber
    # keeps getting live events normally.
    fresh = await broker.subscribe("cap")
    await broker.publish("cap", {"i": 1002})
    assert fresh.get_nowait() == {"i": 1002}
